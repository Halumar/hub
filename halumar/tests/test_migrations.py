"""Versioned migrations (``PRAGMA user_version``), never a data-losing
upgrade. Baseline is v1 (no upgrade path from pre-release
schemas); later steps are registered with ``@migration(N)``. The tests
register temporary steps to exercise the runner.
"""

import logging
import os
import sqlite3
from pathlib import Path

import pytest

import models.database as database
from models.database import Database, DatabaseUnavailableError
from services import db_migrations, db_snapshots


def _rows(path: str, sql: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def test_fresh_install_creates_baseline_without_steps(tmp_path, caplog):
    """No "Added column" cascade; schema and version in one go."""
    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    with caplog.at_level(logging.INFO):
        await db.initialize()
    try:
        assert db.created_fresh
        uv = await db.fetchone("PRAGMA user_version")
        assert list(uv.values()) == [database.SCHEMA_VERSION]
    finally:
        await db.close()
    assert "Added column" not in caplog.text
    assert "migrated" not in caplog.text
    assert "Database created (schema v" in caplog.text
    assert (
        _rows(db_path, "SELECT name FROM sqlite_master WHERE name='schema_version'")
        == []
    )
    assert not os.path.exists(db_snapshots.backup_dir(db_path)), (
        "a fresh install needs no pre-migration snapshot"
    )


async def test_second_start_runs_no_migration(tmp_path, caplog):
    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    await db.close()
    db = Database(db_path)
    with caplog.at_level(logging.INFO):
        await db.initialize()
    await db.close()
    assert "migrated" not in caplog.text
    assert f"Database at schema v{database.SCHEMA_VERSION}" in caplog.text


async def test_tables_without_version_are_not_upgraded(tmp_path):
    """A file this hub did not create is neither migrated nor replaced."""
    db_path = str(tmp_path / "hub.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE areas (id TEXT)")
    conn.commit()
    conn.close()
    db = Database(db_path)
    with pytest.raises(DatabaseUnavailableError):
        await db.initialize()
    assert _rows(db_path, "SELECT name FROM sqlite_master") == [("areas",)]
    assert _rows(db_path, "PRAGMA user_version") == [(0,)]


def _register(monkeypatch, version, fn, description="test step"):
    monkeypatch.setitem(
        db_migrations.MIGRATIONS,
        version,
        db_migrations.Migration(version, description, fn),
    )
    monkeypatch.setattr(database, "SCHEMA_VERSION", version)


async def _seed_current(db_path):
    db = Database(db_path)
    await db.initialize()
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("a1", "Wohnzimmer", now, now),
    )
    await db.set_setting("important", "data")
    await db.close()


async def test_schema_bump_keeps_all_data(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    await _seed_current(db_path)
    nxt = database.SCHEMA_VERSION + 1

    async def _add_column(conn):
        await db_migrations.add_missing_columns(conn, "areas", [("note", "TEXT")])

    _register(monkeypatch, nxt, _add_column)
    db = Database(db_path)
    await db.initialize()
    try:
        assert [r["name"] for r in await db.fetchall("SELECT name FROM areas")] == [
            "Wohnzimmer"
        ]
        assert await db.get_setting("important") == "data"
        assert "note" in await db_migrations.table_columns(db.connection, "areas")
        uv = await db.fetchone("PRAGMA user_version")
        assert list(uv.values()) == [nxt]
    finally:
        await db.close()

    # Consistent pre-migration snapshot of the old state.
    old = database.SCHEMA_VERSION - 1
    snaps = db_snapshots.restore_candidates(db_path)
    assert [os.path.basename(p).split("-")[:4] for p in snaps] == [
        ["hub", "pre", f"v{old}", f"v{nxt}"]
    ]
    assert _rows(snaps[0], "PRAGMA integrity_check") == [("ok",)]
    assert _rows(snaps[0], "PRAGMA user_version") == [(old,)]
    assert _rows(snaps[0], "SELECT name FROM areas") == [("Wohnzimmer",)]
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


async def test_failing_migration_rolls_back_completely(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    await _seed_current(db_path)
    before = database.SCHEMA_VERSION
    nxt = before + 1

    async def _half_then_fail(conn):
        await conn.execute("ALTER TABLE areas ADD COLUMN half TEXT")
        await conn.execute("UPDATE areas SET name = 'changed'")
        raise RuntimeError("step failed")

    _register(monkeypatch, nxt, _half_then_fail)
    db = Database(db_path)
    with pytest.raises(RuntimeError, match="step failed"):
        await db.initialize()
    assert db._db is None
    assert _rows(db_path, "PRAGMA user_version") == [(before,)]
    assert _rows(db_path, "SELECT name FROM areas") == [("Wohnzimmer",)]
    cols = [r[1] for r in _rows(db_path, "PRAGMA table_info(areas)")]
    assert "half" not in cols


async def test_migration_gap_stops_without_changes(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hub.db")
    await _seed_current(db_path)
    before = database.SCHEMA_VERSION
    monkeypatch.setattr(database, "SCHEMA_VERSION", before + 2)
    db = Database(db_path)
    with pytest.raises(RuntimeError, match="no migration registered"):
        await db.initialize()
    assert _rows(db_path, "PRAGMA user_version") == [(before,)]


async def test_newer_database_is_not_touched(tmp_path):
    """Downgrade: an older hub on a newer DB keeps data and version."""
    db_path = str(tmp_path / "hub.db")
    await _seed_current(db_path)
    newer = database.SCHEMA_VERSION + 2
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {newer}")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        assert await db.get_setting("important") == "data"
        uv = await db.fetchone("PRAGMA user_version")
        assert list(uv.values()) == [newer]
    finally:
        await db.close()


class _FailingConn:
    """Minimal aiosqlite stand-in: table_info works, ALTER fails."""

    def __init__(self, error):
        self.error = error

    def execute(self, sql, params=()):
        if sql.startswith("PRAGMA table_info"):
            return _Cursor([(0, "id", "TEXT", 0, None, 1)])
        raise self.error


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def fetchall(self):
        return self.rows


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.OperationalError("database or disk is full"),
    ],
)
async def test_column_migration_errors_are_not_swallowed(error):
    """Only "already there" is skipped (via table_info), never errors."""
    with pytest.raises(sqlite3.OperationalError):
        await db_migrations.add_missing_columns(
            _FailingConn(error), "areas", [("note", "TEXT")]
        )


async def test_existing_column_is_skipped_without_alter():
    added = await db_migrations.add_missing_columns(
        _FailingConn(AssertionError("no ALTER expected")), "areas", [("id", "TEXT")]
    )
    assert added == []


def test_split_sql_matches_schema_statements():
    stmts = db_migrations.split_sql(db_migrations.SCHEMA_SQL)
    assert all(sqlite3.complete_statement(s) for s in stmts)
    assert sum(s.startswith("CREATE TABLE") for s in stmts) == 25
    assert sum(s.startswith("CREATE TRIGGER") for s in stmts) == 3


def test_migration_registry_is_contiguous():
    versions = sorted(db_migrations.MIGRATIONS)
    expected = list(
        range(
            db_migrations.BASELINE_VERSION + 1,
            db_migrations.BASELINE_VERSION + 1 + len(versions),
        )
    )
    assert versions == expected
    assert database.SCHEMA_VERSION == db_migrations.latest_version()


def test_baseline_and_registry_guard():
    assert db_migrations.BASELINE_VERSION == 1
    with pytest.raises(ValueError):
        db_migrations.migration(1, "below baseline")(lambda conn: None)
    with pytest.raises(RuntimeError, match="unknown schema version"):
        db_migrations.steps(0, 2)


V8 = (Path(__file__).parent / "fixtures" / "schema_v8.sql").read_text()

CAN_ADD_DEVICES_SQL = """    can_add_devices INTEGER NOT NULL DEFAULT 0
        CHECK (can_add_devices IN (0, 1) AND NOT (is_guest = 1 AND can_add_devices = 1)),
"""
GUEST_INVITE_SQL = """    can_invite_guests INTEGER NOT NULL DEFAULT 0
        CHECK (can_invite_guests IN (0, 1) AND NOT (is_guest = 1 AND can_invite_guests = 1)),
    invited_by_client_id TEXT,
    invited_by_name TEXT,
"""


async def test_v1_database_migrates_to_latest_and_keeps_data(tmp_path):
    """token_requests and paired_clients.ha_user_released_at are
    added by migration 2, the client info and activity feed by
    migration 3; v1 data (areas, clients) stays."""
    db_path = str(tmp_path / "hub.db")
    conn = sqlite3.connect(db_path)
    v1 = V8
    for line in (
        "    ha_user_released_at TEXT,\n",
        "    app_build TEXT,\n",
        "    platform TEXT,\n",
        "    device_model TEXT,\n",
        "    protocol_version INTEGER,\n",
        "    tile TEXT,\n",
        CAN_ADD_DEVICES_SQL,
        GUEST_INVITE_SQL,
    ):
        assert line in v1
        v1 = v1.replace(line, "", 1)
    # token_requests (v2) and client_activity (v3) sit side by side.
    v1 = (
        v1[: v1.index("CREATE TABLE IF NOT EXISTS token_requests")]
        + v1[v1.index("CREATE TABLE IF NOT EXISTS client_notifications") :]
    )
    for stmt in db_migrations.split_sql(v1):
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO paired_clients (id, name, secret_hash, created_at) "
        "VALUES ('c1', 'iPhone', 'h', '2026-11-18T10:00:00+00:00')"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        uv = await db.fetchone("PRAGMA user_version")
        assert list(uv.values()) == [20] == [database.SCHEMA_VERSION]
        row = await db.fetchone(
            "SELECT name, ha_user_released_at, platform FROM paired_clients"
        )
        assert row == {"name": "iPhone", "ha_user_released_at": None, "platform": None}
        assert await db.fetchall("SELECT * FROM token_requests") == []
        assert await db.fetchall("SELECT * FROM client_activity") == []
        assert await db_migrations.missing_schema_parts(db.connection) == []
    finally:
        await db.close()


async def test_v3_database_gets_tile_column_and_keeps_rows(tmp_path):
    """Migration 4 adds area_entities.tile; existing placements keep
    their data and start with NULL (= default tile)."""
    db_path = str(tmp_path / "hub.db")
    conn = sqlite3.connect(db_path)
    v3 = V8.replace("    tile TEXT,\n", "", 1)
    assert v3 != V8
    for stmt in db_migrations.split_sql(v3):
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) "
        "VALUES ('a1', 'Wohnzimmer', 'x', 'x')"
    )
    conn.execute("INSERT INTO area_sections (id, area_id) VALUES ('s1', 'a1')")
    conn.execute(
        "INSERT INTO area_entities (id, area_id, entity_id, section_id, display_name, "
        "created_at) VALUES ('e1', 'a1', 'light.bed_light', 's1', 'Bett', 'x')"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        uv = await db.fetchone("PRAGMA user_version")
        assert list(uv.values()) == [database.SCHEMA_VERSION]
        row = await db.fetchone(
            "SELECT entity_id, display_name, tile FROM area_entities WHERE id = 'e1'"
        )
        assert row == {
            "entity_id": "light.bed_light",
            "display_name": "Bett",
            "tile": None,
        }
        assert await db_migrations.missing_schema_parts(db.connection) == []
    finally:
        await db.close()


async def test_v4_database_carries_members_and_guests_over_to_profiles(tmp_path):
    """Migrations 5-9 on an old database: the rights columns of v5/v6 come and
    go again; v9 gives every device access and a profile."""
    db_path = str(tmp_path / "hub.db")
    conn = sqlite3.connect(db_path)
    v4 = V8.replace(CAN_ADD_DEVICES_SQL, "", 1).replace(GUEST_INVITE_SQL, "", 1)
    assert v4 != V8
    for stmt in db_migrations.split_sql(v4):
        conn.execute(stmt)
    for cid, guest, expiry in (
        ("m1", 0, None),
        ("g1", 1, "2099-01-01T00:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO paired_clients (id, name, secret_hash, created_at, is_guest, "
            "expires_at) VALUES (?, 'x', 'h', 'x', ?, ?)",
            (cid, guest, expiry),
        )
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        uv = await db.fetchone("PRAGMA user_version")
        assert list(uv.values()) == [database.SCHEMA_VERSION]
        rows = await db.fetchall(
            "SELECT c.id, c.access, p.origin_suggestion FROM paired_clients c "
            "JOIN profiles p ON p.id = c.profile_id ORDER BY c.id"
        )
        assert rows == [
            {"id": "g1", "access": "guest", "origin_suggestion": "guest"},
            {"id": "m1", "access": "member", "origin_suggestion": "adult"},
        ]
        columns = await db_migrations.table_columns(db.connection, "paired_clients")
        assert "can_add_devices" not in columns and "is_guest" not in columns
        assert await db_migrations.missing_schema_parts(db.connection) == []
    finally:
        await db.close()


async def test_v12_android_slots_start_like_their_apple_counterpart(tmp_path):
    """A v11 hub gets the slots android_tablet, android_tv and wearos
    with the default profile of iPad, Apple TV and Apple Watch (whatever the
    admin chose there); slots that exist stay untouched, profiles too."""
    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    ipad = await db.get_setting("default_profile_ipados")
    adult = await db.get_setting("default_profile_ios")
    await db.set_setting("default_profile_tvos", adult)  # admin's choice
    await db.set_setting("default_profile_android", ipad)
    profiles_before = await db.fetchall("SELECT id, rev FROM profiles ORDER BY id")
    await db.close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "DELETE FROM settings WHERE key IN ('default_profile_android_tablet', "
        "'default_profile_android_tv', 'default_profile_wearos')"
    )
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        assert list((await db.fetchone("PRAGMA user_version")).values()) == [
            database.SCHEMA_VERSION
        ]
        assert await db.get_setting("default_profile_android_tablet") == ipad
        assert await db.get_setting("default_profile_android_tv") == adult
        assert await db.get_setting("default_profile_wearos") == adult
        assert await db.get_setting("default_profile_android") == ipad
        assert (
            await db.fetchall("SELECT id, rev FROM profiles ORDER BY id")
            == profiles_before
        )
    finally:
        await db.close()


async def test_v13_kinds_setup_devices_and_set_up_by(tmp_path):
    """A v12 hub gets profiles.kind (origin suggestion, guest default,
    parental controls, else person), add_devices/invite_guests become
    setup_devices in bodies and adjustments (from the effective old values),
    invited_by_* becomes set_up_by_* and QR-confirmed devices get their
    confirming device from the paired activity."""
    import json

    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    adult = await db.get_setting("default_profile_ios")
    guest = await db.get_setting("default_profile_guest")
    await db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE profiles DROP COLUMN kind")
    conn.execute("ALTER TABLE profile_revisions DROP COLUMN kind")
    conn.execute("ALTER TABLE paired_clients DROP COLUMN set_up_method")
    conn.execute(
        "ALTER TABLE paired_clients RENAME COLUMN set_up_by_client_id TO invited_by_client_id"
    )
    conn.execute(
        "ALTER TABLE paired_clients RENAME COLUMN set_up_by_name TO invited_by_name"
    )
    body = json.loads(
        conn.execute(
            "SELECT body_json FROM profiles WHERE id = ?", (adult,)
        ).fetchone()[0]
    )
    body["rules"].pop("setup_devices", None)
    body["rules"]["add_devices"] = True
    body["rules"]["invite_guests"] = False
    body["fixed"] = sorted([*body["fixed"], "invite_guests"])
    conn.execute(
        "UPDATE profiles SET body_json = ? WHERE id = ?", (json.dumps(body), adult)
    )
    conn.execute(
        "INSERT INTO profiles (id, name, child_protection, body_json, rev, created_at, "
        "updated_at) VALUES ('own-kid', 'Eigenes Kind', 1, '{}', 1, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO profiles (id, name, child_protection, body_json, rev, created_at, "
        "updated_at) VALUES ('own', 'Eigenes', 0, '{}', 1, 'x', 'x')"
    )
    now = "2026-11-20T10:00:00+00:00"
    for cid, name, extra in (
        ("phone", "iPhone Papa", {}),
        ("tv", "Apple TV", {}),
        (
            "g1",
            "Gast Guest One",
            {"access": "guest", "expires_at": "2099-01-01T00:00:00+00:00"},
        ),
    ):
        cols = {
            "id": cid,
            "name": name,
            "secret_hash": "h" + cid,
            "created_at": now,
            "profile_id": guest if cid == "g1" else adult,
            **extra,
        }
        if cid == "g1":
            cols.update(invited_by_client_id="phone", invited_by_name="iPhone Papa")
        conn.execute(
            f"INSERT INTO paired_clients ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
    conn.execute(
        "INSERT INTO device_overrides (client_id, rule_key, value_json, created_by, "
        "created_at) VALUES ('phone', 'invite_guests', 'true', 'admin', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO client_activity (client_id, ts, event, detail) VALUES "
        "('tv', ?, 'paired', ?)",
        (now, json.dumps({"method": "onboard", "confirmed_by": "phone"})),
    )
    conn.execute("PRAGMA user_version = 12")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        assert list((await db.fetchone("PRAGMA user_version")).values()) == [
            database.SCHEMA_VERSION
        ]
        kinds = {
            r["id"]: r["kind"]
            for r in await db.fetchall("SELECT id, kind FROM profiles")
        }
        assert kinds[adult] == "person"
        assert kinds[guest] == "guest"
        assert kinds["own-kid"] == "child" and kinds["own"] == "person"
        row = await db.fetchone("SELECT body_json FROM profiles WHERE id = ?", (adult,))
        stored = json.loads(row["body_json"])
        assert "add_devices" not in stored["rules"]
        assert stored["rules"]["setup_devices"] == {
            "enabled": True,
            "kinds": ["child", "panel", "tv", "person"],
        }
        assert "setup_devices" in stored["fixed"]
        assert "invite_guests" not in stored["fixed"]
        overs = await db.fetchall(
            "SELECT rule_key, value_json FROM device_overrides WHERE client_id = 'phone'"
        )
        assert [(o["rule_key"], json.loads(o["value_json"])) for o in overs] == [
            (
                "setup_devices",
                {
                    "enabled": True,
                    "kinds": [
                        "guest",
                        "child",
                        "panel",
                        "tv",
                        "house_sitter",
                        "person",
                    ],
                },
            )
        ]
        set_up = {
            r["id"]: (r["set_up_by_client_id"], r["set_up_by_name"], r["set_up_method"])
            for r in await db.fetchall(
                "SELECT id, set_up_by_client_id, set_up_by_name, set_up_method "
                "FROM paired_clients"
            )
        }
        assert set_up["g1"] == ("phone", "iPhone Papa", "invite")
        assert set_up["tv"] == ("phone", "iPhone Papa", "onboard")
        assert set_up["phone"] == (None, None, None)
    finally:
        await db.close()


async def test_v18_database_ends_with_key_lost_at(tmp_path):
    """v19 adds paired_clients.remote_lost_at, v20 renames it to key_lost_at."""
    import sqlite3 as _sqlite3

    from models import database
    from services import db_migrations

    path = tmp_path / "v18.db"
    db = database.Database(str(path))
    await db.initialize()
    await db.close()
    conn = _sqlite3.connect(path)
    conn.execute("ALTER TABLE paired_clients DROP COLUMN key_lost_at")
    conn.execute("PRAGMA user_version = 18")
    conn.commit()
    conn.close()
    db = database.Database(str(path))
    await db.initialize()
    try:
        cols = {
            r["name"] for r in await db.fetchall("PRAGMA table_info(paired_clients)")
        }
        assert "key_lost_at" in cols and "remote_lost_at" not in cols
        assert db_migrations.latest_version() == 20
        assert await db_migrations.missing_schema_parts(db.connection) == []
    finally:
        await db.close()


async def test_v20_moves_children_and_guest_kinds_to_hub_only(tmp_path):
    """v20: parental controls and the guest kinds on Hub only (guest kinds
    fixed), their control_path adjustments removed, a new revision and audit
    entry per changed profile, key_lost_at for the active devices of the guest
    kinds that hold no key now; members on Checked or Direct and panels stay
    as they are, every device stays paired. A second start runs nothing."""
    import json

    from services.profiles import catalog as cat
    from services.profiles import migration_v20
    from services.profiles import rows as profile_rows

    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    ids = {
        s: (
            await db.fetchone(
                "SELECT id FROM profiles WHERE origin_suggestion = ?", (s,)
            )
        )["id"]
        for s in ("adult", "child", "guest", "wall_panel")
    }
    async with db.transaction():
        ids["house_sitter"] = await profile_rows.insert_profile(
            db.connection,
            name="Haushüter",
            body=cat.materialize("house_sitter"),
            now="2026-11-01T10:00:00+00:00",
            actor=None,
            kind="house_sitter",
        )
    await db.close()

    # The database as a v19 hub left it.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "ALTER TABLE paired_clients RENAME COLUMN key_lost_at TO remote_lost_at"
    )
    old = {
        "adult": ("direct", False),
        "child": ("checked", False),
        "guest": ("checked", False),
        "house_sitter": ("checked", True),
        "wall_panel": ("checked", False),
    }
    for name, (path, remote) in old.items():
        body = json.loads(
            conn.execute(
                "SELECT body_json FROM profiles WHERE id = ?", (ids[name],)
            ).fetchone()[0]
        )
        body["rules"]["control_path"] = path
        body["fixed"] = [k for k in body["fixed"] if k != "control_path"]
        if name == "house_sitter":
            body["rules"]["remote_access"] = remote
        conn.execute(
            "UPDATE profiles SET body_json = ? WHERE id = ?",
            (json.dumps(body), ids[name]),
        )
    now = "2026-11-20T10:00:00+00:00"
    devices = {
        "phone": ("adult", "member", 1),
        "kid": ("child", "member", 1),
        "guest": ("guest", "guest", 1),
        "guest_off": ("guest", "guest", 0),
        "sitter": ("house_sitter", "guest", 1),
        "panel": ("wall_panel", "member", 1),
    }
    for cid, (profile, access, active) in devices.items():
        conn.execute(
            "INSERT INTO paired_clients (id, name, secret_hash, created_at, platform, "
            "access, expires_at, profile_id, is_active, ha_token_status, "
            "ha_token_encrypted, ha_token_minted_at) "
            "VALUES (?, ?, ?, ?, 'ios', ?, ?, ?, ?, 'active', 'enc', ?)",
            (
                cid,
                cid,
                "h" + cid,
                now,
                access,
                "2099-01-01T00:00:00+00:00" if access == "guest" else None,
                ids[profile],
                active,
                now,
            ),
        )
    for cid, value in (("kid", "checked"), ("guest", "checked"), ("phone", "checked")):
        conn.execute(
            "INSERT INTO device_overrides (client_id, rule_key, value_json, "
            "created_by, created_at) VALUES (?, 'control_path', ?, 'admin', ?)",
            (cid, json.dumps(value), now),
        )
    revs = dict(conn.execute("SELECT id, rev FROM profiles").fetchall())
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        assert list((await db.fetchone("PRAGMA user_version")).values()) == [20]
        assert await db_migrations.missing_schema_parts(db.connection) == []
        bodies = {
            r["id"]: (json.loads(r["body_json"]), r["rev"], r["updated_by"])
            for r in await db.fetchall(
                "SELECT id, body_json, rev, updated_by FROM profiles"
            )
        }
        for name in ("child", "guest", "house_sitter"):
            body, rev, by = bodies[ids[name]]
            assert body["rules"]["control_path"] == "hub", name
            assert rev == revs[ids[name]] + 1 and by == migration_v20.ACTOR
            assert (
                await db.fetchone(
                    "SELECT 1 AS x FROM profile_revisions WHERE profile_id = ? "
                    "AND rev = ?",
                    (ids[name], rev),
                )
            ) is not None
        assert "control_path" not in bodies[ids["child"]][0]["fixed"]
        assert "control_path" in bodies[ids["guest"]][0]["fixed"]
        assert "control_path" in bodies[ids["house_sitter"]][0]["fixed"]
        # Remote access of the house sitter stays as stored.
        assert bodies[ids["house_sitter"]][0]["rules"]["remote_access"] is True
        for name in ("adult", "wall_panel"):
            body, rev, _by = bodies[ids[name]]
            assert body["rules"]["control_path"] == old[name][0]
            assert rev == revs[ids[name]]
        overrides = {
            r["client_id"]
            for r in await db.fetchall(
                "SELECT client_id FROM device_overrides WHERE rule_key = 'control_path'"
            )
        }
        assert overrides == {"phone"}
        audit = await db.fetchall(
            "SELECT action, profile_id, client_id FROM profile_audit "
            "WHERE actor = ? ORDER BY id",
            (migration_v20.ACTOR,),
        )
        assert sorted(a["action"] for a in audit) == [
            "override_removed",
            "override_removed",
            "profile_updated",
            "profile_updated",
            "profile_updated",
        ]
        lost = {
            r["id"]: r["key_lost_at"]
            for r in await db.fetchall("SELECT id, key_lost_at FROM paired_clients")
        }
        # The active guest lost its key; the house sitter keeps a read-only
        # one (remote access), the inactive guest and the others are untouched.
        assert lost["guest"] is not None
        assert [k for k, v in lost.items() if v] == ["guest"]
        active = {
            r["id"]: r["is_active"]
            for r in await db.fetchall("SELECT id, is_active FROM paired_clients")
        }
        assert active == {cid: d[2] for cid, d in devices.items()}
    finally:
        await db.close()

    # A second start runs no migration; the step itself changes nothing more.
    db = Database(db_path)
    await db.initialize()
    try:
        before = await db.fetchall("SELECT id, rev FROM profiles ORDER BY id")
        report = await migration_v20.run(db.connection)
        assert report == {
            "profiles": [],
            "overrides_removed": 0,
            "keys_lost": ["guest"],
        }
        assert await db.fetchall("SELECT id, rev FROM profiles ORDER BY id") == before
    finally:
        await db.close()


async def test_v20_without_children_or_guest_profiles(tmp_path):
    """An installation without profiles of these kinds: only the column."""
    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    await db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM settings WHERE key LIKE 'default_profile_%'")
    conn.execute(
        "DELETE FROM profiles WHERE child_protection = 1 OR kind IN "
        "('guest', 'babysitter', 'house_sitter')"
    )
    conn.execute("ALTER TABLE paired_clients DROP COLUMN key_lost_at")
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    revs = conn.execute("SELECT id, rev FROM profiles ORDER BY id").fetchall()
    conn.close()
    db = Database(db_path)
    await db.initialize()
    try:
        assert list((await db.fetchone("PRAGMA user_version")).values()) == [20]
        assert [
            (r["id"], r["rev"])
            for r in await db.fetchall("SELECT id, rev FROM profiles ORDER BY id")
        ] == revs
        assert await db_migrations.missing_schema_parts(db.connection) == []
    finally:
        await db.close()


async def test_v20_watch_of_a_phone_moved_to_hub_only_goes_at_start(
    tmp_path, monkeypatch
):
    """A member phone on a house-sitter profile without remote access: the
    migration moves it to Hub only, and the start right after removes its
    watch (reason hub_only) with the watch's HA user."""
    import json

    from services import device_provisioning, watch_access
    from services.profiles import catalog as cat
    from services.profiles import rows as profile_rows

    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    async with db.transaction():
        pid = await profile_rows.insert_profile(
            db.connection,
            name="Haushüter",
            body=cat.materialize("house_sitter"),
            now="2026-11-01T10:00:00+00:00",
            actor=None,
            kind="house_sitter",
        )
    await db.close()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "ALTER TABLE paired_clients RENAME COLUMN key_lost_at TO remote_lost_at"
    )
    body = json.loads(
        conn.execute("SELECT body_json FROM profiles WHERE id = ?", (pid,)).fetchone()[
            0
        ]
    )
    body["rules"].update(control_path="checked", remote_access=False)
    body["fixed"] = [k for k in body["fixed"] if k != "control_path"]
    conn.execute(
        "UPDATE profiles SET body_json = ? WHERE id = ?", (json.dumps(body), pid)
    )
    adult = conn.execute(
        "SELECT id FROM profiles WHERE origin_suggestion = 'adult'"
    ).fetchone()[0]
    now = "2026-11-20T10:00:00+00:00"
    for cid, platform, profile, extra in (
        ("phone", "ios", pid, {}),
        (
            "watch",
            "watchos",
            adult,
            {
                "set_up_by_client_id": "phone",
                "set_up_method": "onboard",
                "seal_key": "k" * 43,
            },
        ),
    ):
        cols = {
            "id": cid,
            "name": cid,
            "secret_hash": "h" + cid,
            "created_at": now,
            "platform": platform,
            "profile_id": profile,
            "ha_token_status": "active",
            **extra,
        }
        conn.execute(
            f"INSERT INTO paired_clients ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
    conn.execute("PRAGMA user_version = 19")
    conn.commit()
    conn.close()

    gone = []

    async def _deprovision(client_id, reason):
        gone.append(client_id)

    async def _send(*_a, **_k):
        return True

    import api.websocket as ws

    monkeypatch.setattr(device_provisioning, "send_deprovision", _deprovision)
    monkeypatch.setattr(ws, "send_to_client", _send)
    db = Database(db_path)
    await db.initialize()
    try:
        assert await watch_access.remove_disallowed(db) == ["watch"]
        assert gone == ["watch"]
        ids = {r["id"] for r in await db.fetchall("SELECT id FROM paired_clients")}
        assert ids == {"phone"}
        assert await watch_access.remove_disallowed(db) == []
    finally:
        await db.close()
