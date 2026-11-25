"""Snapshots (daily, rotation, HA backup hooks) and restore at start."""

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from brand import BRAND_NAME
from models.database import Database
from services import db_snapshots as snapshots

APP_ROOT = os.path.join(os.path.dirname(__file__), "..", "rootfs", "opt", "hub")


async def _seed(db_path, name="Wohnzimmer"):
    db = Database(db_path)
    await db.initialize()
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (name, name, now, now),
    )
    await db.commit()
    return db


def _names(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM areas ORDER BY name")]
    finally:
        conn.close()


async def test_daily_snapshot_rotation_keeps_seven(tmp_path):
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path)
    try:
        start = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
        for day in range(10):
            assert snapshots.take_daily_snapshot(db_path, start + timedelta(days=day))
        files = sorted(os.listdir(snapshots.backup_dir(db_path)))
        assert files == [f"hub-daily-202611{d:02d}.db" for d in range(4, 11)]
        assert snapshots.take_daily_snapshot(db_path, start + timedelta(days=9)) is None
    finally:
        await db.close()


async def test_daily_snapshot_skips_empty_database(tmp_path):
    """An empty snapshot must never become the newest restore point."""
    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    try:
        assert snapshots.take_daily_snapshot(db_path) is None
        assert not os.path.exists(snapshots.backup_dir(db_path))
    finally:
        await db.close()


async def test_snapshot_contains_uncheckpointed_wal_writes(tmp_path):
    """VACUUM INTO reads through the WAL (a copy of the main file would not)."""
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path)
    try:
        await db.set_setting("late", "only-in-wal")
        assert os.path.getsize(db_path + "-wal") > 0
        snap = snapshots.take_daily_snapshot(db_path)
        conn = sqlite3.connect(snap)
        assert conn.execute(
            "SELECT value FROM settings WHERE key='late'"
        ).fetchone() == ("only-in-wal",)
        conn.close()
    finally:
        await db.close()


async def test_snapshot_excludes_uncommitted_writes(tmp_path):
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path)
    try:
        await db.execute("INSERT INTO settings (key, value) VALUES ('pending', '1')")
        snap = snapshots.take_daily_snapshot(db_path)
        conn = sqlite3.connect(snap)
        assert (
            conn.execute("SELECT * FROM settings WHERE key='pending'").fetchall() == []
        )
        conn.close()
        await db.commit()
    finally:
        await db.close()


def test_verify_snapshot_rejects_damage_and_foreign_files(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all" * 100)
    assert snapshots.verify_snapshot(str(bad))
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE x (y)")
    conn.commit()
    conn.close()
    assert snapshots.verify_snapshot(str(other)) == f"not a {BRAND_NAME} database"


async def test_corruption_restores_newest_valid_snapshot(tmp_path):
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path, "Alt")
    old = snapshots.take_daily_snapshot(
        db_path, datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES ('n', 'Neu', 'x', 'x')"
    )
    await db.commit()
    new = snapshots.take_daily_snapshot(
        db_path, datetime(2026, 11, 2, tzinfo=timezone.utc)
    )
    await db.close()
    os.utime(old, (1, 1))
    # newest snapshot damaged -> the next one is used
    with open(new, "r+b") as fh:
        fh.write(b"\0" * 100)
    os.utime(new, None)
    with open(db_path, "r+b") as fh:
        fh.write(b"\0" * 100)

    db = Database(db_path)
    await db.initialize()
    try:
        assert [r["name"] for r in await db.fetchall("SELECT name FROM areas")] == [
            "Alt"
        ]
        assert "hub-daily-20261101.db" in db.recovery_notice
    finally:
        await db.close()
    assert [p for p in os.listdir(tmp_path) if ".corrupt-" in p]


async def test_missing_database_is_restored_from_snapshot(tmp_path):
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path)
    snapshots.take_daily_snapshot(db_path)
    await db.close()
    os.remove(db_path)

    db = Database(db_path)
    await db.initialize()
    try:
        assert [r["name"] for r in await db.fetchall("SELECT name FROM areas")] == [
            "Wohnzimmer"
        ]
        assert "missing" in db.recovery_notice
        assert not db.created_fresh
    finally:
        await db.close()


def _hook(action, db_path):
    return subprocess.run(
        [sys.executable, os.path.join(APP_ROOT, "backup_hook.py"), action],
        env={**os.environ, "HUB_DB_PATH": db_path},
        capture_output=True,
        text=True,
        timeout=60,
    )


async def test_backup_hooks_while_running(tmp_path):
    """pre: consistent snapshot + marker next to a running server; post: marker gone."""
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path)
    try:
        await db.set_setting("in_wal", "yes")
        pre = _hook("pre", db_path)
        assert pre.returncode == 0, pre.stdout + pre.stderr
        assert os.path.exists(snapshots.ha_backup_marker_path(db_path))
        snap = snapshots.ha_backup_path(db_path)
        assert snapshots.verify_snapshot(snap) is None
        conn = sqlite3.connect(snap)
        assert conn.execute("SELECT value FROM settings WHERE key='in_wal'").fetchone()
        conn.close()
        post = _hook("post", db_path)
        assert post.returncode == 0, post.stdout + post.stderr
        assert not os.path.exists(snapshots.ha_backup_marker_path(db_path))
        assert os.path.exists(snap), "snapshot stays as an extra restore point"
    finally:
        await db.close()


def test_backup_hook_without_database_is_a_noop(tmp_path):
    r = _hook("pre", str(tmp_path / "hub.db"))
    assert r.returncode == 0
    assert not os.path.exists(snapshots.ha_backup_marker_path(str(tmp_path / "x.db")))


def test_backup_hook_unknown_action(tmp_path):
    assert _hook("sideways", str(tmp_path / "hub.db")).returncode == 2


async def test_restored_ha_backup_starts_from_the_snapshot(tmp_path):
    """Simulate a restored HA backup: marker + snapshot, and a torn live copy
    (here: a db file with newer, half-copied content)."""
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path, "Backup-Stand")
    assert _hook("pre", db_path).returncode == 0
    # writes after the snapshot = what tar may catch half of
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES ('z', 'Danach', 'x', 'x')"
    )
    await db.commit()
    await db.close()

    db = Database(db_path)
    await db.initialize()
    try:
        assert [r["name"] for r in await db.fetchall("SELECT name FROM areas")] == [
            "Backup-Stand"
        ]
        assert "Home Assistant backup" in db.recovery_notice
        assert db.restored_from_ha_backup
    finally:
        await db.close()
    assert not os.path.exists(snapshots.ha_backup_marker_path(db_path))
    archives = [p for p in os.listdir(tmp_path) if ".pre-restore-" in p]
    main = [p for p in archives if not p.endswith(("-wal", "-shm"))]
    assert len(main) == 1, "live files of the restored backup are kept, not deleted"
    assert "Danach" in _names(str(tmp_path / main[0]))


async def test_marker_without_usable_snapshot_keeps_live_database(tmp_path):
    db_path = str(tmp_path / "hub.db")
    db = await _seed(db_path)
    await db.close()
    os.makedirs(snapshots.backup_dir(db_path), exist_ok=True)
    with open(snapshots.ha_backup_marker_path(db_path), "w") as fh:
        fh.write("{}")
    db = Database(db_path)
    await db.initialize()
    try:
        assert db.recovery_notice is None
        assert [r["name"] for r in await db.fetchall("SELECT name FROM areas")] == [
            "Wohnzimmer"
        ]
    finally:
        await db.close()
    assert not os.path.exists(snapshots.ha_backup_marker_path(db_path))


def test_config_yaml_backup_settings():
    """Hot backup with hooks; snapshots and archives excluded."""
    yaml = pytest.importorskip("yaml")

    with open(os.path.join(APP_ROOT, "..", "..", "..", "config.yaml")) as fh:
        cfg = yaml.safe_load(fh)
    assert cfg["backup"] == "hot"
    assert cfg["backup_pre"] == "python3 /opt/hub/backup_hook.py pre"
    assert cfg["backup_post"] == "python3 /opt/hub/backup_hook.py post"
    from pathlib import PurePath

    def excluded(rel):
        full = PurePath("/mnt/data/supervisor/addons/data/local_halumar") / rel
        return any(full.match(p) for p in cfg["backup_exclude"])

    assert excluded("backups/hub-daily-20261118.db")
    assert excluded("backups/hub-pre-v3-v4-20261118T1.db")
    assert excluded("hub.db.corrupt-20261118T1")
    assert excluded("hub.db.corrupt-20261118T1-wal")
    assert excluded(".hub.db.ab12cd34.tmp")
    # keys never go into HA backups, the local copy neither
    assert excluded(".hub_keyfile")
    assert excluded(".hub_keyfile.bak")
    for kept in (
        "hub.db",
        "hub.db-wal",
        "backups/hub-ha-backup.db",
        "backups/ha-backup.marker",
        "images/0123456789ab_card_image.png",
    ):
        assert not excluded(kept), kept
    assert cfg["options"]["reset_corrupt_database"] is False
    assert cfg["schema"]["reset_corrupt_database"] == "bool"


@pytest.mark.parametrize("missing", [True, False])
def test_latest_snapshot_info(tmp_path, missing):
    db_path = str(tmp_path / "hub.db")
    if missing:
        assert snapshots.latest_snapshot_info(db_path) is None
        return
    os.makedirs(snapshots.backup_dir(db_path))
    p = os.path.join(snapshots.backup_dir(db_path), "hub-daily-20261118.db")
    open(p, "wb").close()
    info = snapshots.latest_snapshot_info(db_path)
    assert info["file"] == "hub-daily-20261118.db"
