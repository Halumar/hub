"""No hang on database start errors; /api/health probes the database.

A damaged database is never taken for a fresh install: the start stops with a
clear reason, or archives the file and starts empty when the reset option is
on. Any other start error closes the connection, so no aiosqlite worker thread
keeps the process alive. /api/health answers 503 while the database is locked,
closed or does not match the schema.
"""

import os
import sqlite3
from types import SimpleNamespace

import pytest

from models.database import Database, DatabaseUnavailableError


async def _seed(db_path: str) -> None:
    db = Database(db_path)
    await db.initialize()
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("a1", "Garage", now, now),
    )
    await db.commit()
    await db.close()


def _corrupt_table_page(db_path: str, table: str) -> None:
    """Overwrite the root page of ``table`` with garbage (b-tree header gone)."""
    conn = sqlite3.connect(db_path)
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    root = conn.execute(
        "SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)
    ).fetchone()[0]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(db_path, "r+b") as fh:
        fh.seek((root - 1) * page_size)
        fh.write(b"\xff" * page_size)


def _archives(tmp_path) -> list[str]:
    return sorted(p for p in os.listdir(tmp_path) if ".corrupt-" in p)


def _files(tmp_path) -> dict[str, bytes]:
    return {
        p: (tmp_path / p).read_bytes()
        for p in sorted(os.listdir(tmp_path))
        if (tmp_path / p).is_file()
    }


async def test_corrupt_table_page_is_not_a_fresh_install(tmp_path, caplog):
    """Damaged table page, no snapshot: no fresh install, no empty DB,
    nothing touched; the start stops with a clear reason."""
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    _corrupt_table_page(db_path, "settings")
    before = _files(tmp_path)

    db = Database(db_path)
    with caplog.at_level("INFO", logger="hub.database"):
        with pytest.raises(DatabaseUnavailableError) as err:
            await db.initialize()
    assert "Database is corrupt" in caplog.text
    assert "Database created" not in caplog.text
    assert "no usable snapshot" in err.value.reason
    assert "reset_corrupt_database" in err.value.action
    assert db._db is None, "connection (aiosqlite thread) must be closed"
    assert _files(tmp_path) == before, "damaged DB must stay exactly as it was"


async def test_corrupt_table_page_reset_option_archives_and_starts_empty(
    tmp_path,
):
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    _corrupt_table_page(db_path, "settings")

    db = Database(db_path, reset_on_corruption=True)
    await db.initialize()
    try:
        assert await db.fetchone("PRAGMA user_version")
        assert await db.fetchall("SELECT * FROM areas") == []
        assert "EMPTY" in db.recovery_notice
    finally:
        await db.close()
    assert _archives(tmp_path), "damaged DB must be archived, not overwritten"


async def test_header_corruption_without_backup_stops_and_keeps_file(tmp_path):
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    with open(db_path, "r+b") as fh:
        fh.write(b"\x00" * 100)
    before = _files(tmp_path)

    db = Database(db_path)
    with pytest.raises(DatabaseUnavailableError):
        await db.initialize()
    assert db._db is None
    assert _files(tmp_path) == before
    assert _archives(tmp_path) == []


async def test_header_corruption_reset_option_starts_empty(tmp_path):
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    with open(db_path, "r+b") as fh:
        fh.write(b"\x00" * 100)

    db = Database(db_path, reset_on_corruption=True)
    await db.initialize()
    try:
        row = await db.fetchone("PRAGMA integrity_check")
        assert list(row.values()) == ["ok"]
    finally:
        await db.close()
    assert _archives(tmp_path)


def test_archive_moves_wal_and_shm_with_the_damaged_db(tmp_path):
    """SQLite pairs a WAL with a DB by file name only: a stale -wal left next to
    a restored/new DB would be replayed onto it."""
    db_path = str(tmp_path / "hub.db")
    for suffix in ("", "-wal", "-shm"):
        with open(db_path + suffix, "wb") as fh:
            fh.write(b"x")
    archived = Database(db_path)._archive_corrupt_files()
    for suffix in ("", "-wal", "-shm"):
        assert not os.path.exists(db_path + suffix)
        assert os.path.exists(archived + suffix)


async def test_non_corruption_error_closes_connection_and_keeps_files(
    tmp_path, monkeypatch
):
    """A lock/I-O error stops the start cleanly: no recreate, no open thread."""
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)

    async def _locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Database, "_read_schema_version", _locked)
    db = Database(db_path)
    with pytest.raises(sqlite3.OperationalError):
        await db.initialize()
    assert db._db is None, "connection (aiosqlite thread) must be closed"
    assert _archives(tmp_path) == []
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT name FROM areas").fetchall() == [("Garage",)]
    conn.close()


def _request(db):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))


async def test_health_ok(db):
    from main import health_check

    body = await health_check(_request(db))
    assert body["status"] == "ok" and body["db"] == "ok"


async def test_health_503_when_db_locked_by_another_process(db):
    from main import health_check

    locker = sqlite3.connect(db.db_path, timeout=0.1)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        db_error = await db.probe(timeout=0.2)
        assert db_error and "not writable" in db_error
        resp = await health_check(_request(db))
        assert resp.status_code == 503
    finally:
        locker.rollback()
        locker.close()
    assert await db.probe(timeout=0.2) is None


async def test_health_503_when_db_closed(tmp_path):
    from main import health_check

    database = Database(str(tmp_path / "x.db"))
    await database.initialize()
    await database.close()
    resp = await health_check(_request(database))
    assert resp.status_code == 503


# ── database of a different build with the same version ──────────────


def _drop_column(db_path: str, table: str, column: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    conn.commit()
    conn.close()


async def test_schema_mismatch_stops_with_a_clear_reason(tmp_path):
    """A DB with the expected user_version but missing columns does not
    start; the reason names the mismatch."""
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    _drop_column(db_path, "paired_clients", "ha_user_id")
    before = _files(tmp_path)

    db = Database(db_path)
    with pytest.raises(DatabaseUnavailableError) as err:
        await db.initialize()
    assert "paired_clients.ha_user_id" in err.value.reason
    assert "different" in err.value.reason
    assert "reset_corrupt_database" in err.value.action
    assert db._db is None
    assert _files(tmp_path) == before, "nothing may be changed"


async def test_schema_mismatch_reset_option_archives_and_starts_empty(tmp_path):
    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    _drop_column(db_path, "paired_clients", "ha_user_id")

    db = Database(db_path, reset_on_corruption=True)
    await db.initialize()
    try:
        assert await db.fetchall("SELECT * FROM areas") == []
        assert "ha_user_id" in await _columns(db, "paired_clients")
        assert "EMPTY" in db.recovery_notice
    finally:
        await db.close()
    archived = [p for p in os.listdir(tmp_path) if ".mismatch-" in p]
    assert archived, "the old file must be kept"


async def test_schema_mismatch_is_health_503(tmp_path, monkeypatch):
    """The lifespan turns it into the error state: health 503 with reason and
    action, every other request 503 (not 500)."""
    from fastapi.testclient import TestClient

    import main

    db_path = str(tmp_path / "hub.db")
    await _seed(db_path)
    _drop_column(db_path, "paired_clients", "ha_user_id")
    monkeypatch.setenv("HUB_DB_PATH", db_path)
    monkeypatch.setattr(main, "_read_addon_options", lambda: {})

    async def _no_notify(*_a, **_k):
        return None

    monkeypatch.setattr(main, "_notify_ha", _no_notify)
    with TestClient(main.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 503
        body = health.json()
        assert "paired_clients.ha_user_id" in body["error"]
        assert "reset_corrupt_database" in body["action"]
        assert client.get("/api/pairing/clients").status_code == 503


async def _columns(db, table):
    return [r["name"] for r in await db.fetchall(f"PRAGMA table_info({table})")]
