"""Tests for the Database module — CRUD, settings, backup/recovery, schema init."""

import os
import uuid

import pytest

from models.database import Database, SCHEMA_VERSION
from services import db_snapshots as snapshots


class TestDatabaseInit:
    """Database initialization and schema management."""

    async def test_initialize_creates_schema(self, db):
        """Fresh database should have all required tables."""
        tables = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {t["name"] for t in tables}
        expected = {
            "areas",
            "area_sections",
            "area_entities",
            "area_widgets",
            "area_tags",
            "paired_clients",
            "client_notifications",
            "settings",
            "area_excluded_entities",
        }
        assert expected.issubset(table_names), (
            f"Missing tables: {expected - table_names}"
        )

    async def test_schema_version_stored(self, db):
        """Schema version should be persisted."""
        row = await db.fetchone("PRAGMA user_version")
        assert list(row.values()) == [SCHEMA_VERSION]

    async def test_api_token_auto_generated(self, db):
        """First init should auto-generate an API token hash."""
        token_hash = await db.get_setting("api_token_hash")
        assert token_hash is not None
        assert len(token_hash) == 64  # SHA-256 hex

    async def test_pragmas_set(self, db):
        """WAL mode, foreign keys, and busy timeout should be configured."""
        row = await db.fetchone("PRAGMA journal_mode")
        assert row is not None
        assert dict(row).get("journal_mode", "").lower() == "wal"

        row = await db.fetchone("PRAGMA foreign_keys")
        assert row is not None
        # foreign_keys returns 1 when enabled
        assert list(dict(row).values())[0] == 1

        row = await db.fetchone("PRAGMA busy_timeout")
        assert row is not None
        assert list(dict(row).values())[0] == 5000


class TestDatabaseCRUD:
    """Basic CRUD operations."""

    async def test_insert_and_fetch_area(self, db):
        """Insert an area and retrieve it."""
        area_id = str(uuid.uuid4())
        now = Database.now()
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
            "VALUES (?, ?, 0, 1, ?, ?)",
            (area_id, "Living Room", now, now),
        )
        await db.commit()

        area = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
        assert area is not None
        assert area["name"] == "Living Room"
        assert area["is_active"] == 1

    async def test_unique_area_name_constraint(self, db):
        """Area names must be unique."""
        now = Database.now()
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
            "VALUES (?, ?, 0, 1, ?, ?)",
            (str(uuid.uuid4()), "Kitchen", now, now),
        )
        await db.commit()

        import aiosqlite

        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
                "VALUES (?, ?, 0, 1, ?, ?)",
                (str(uuid.uuid4()), "Kitchen", now, now),
            )

    async def test_fetchall_returns_list(self, db):
        """fetchall should return a list of dicts."""
        result = await db.fetchall("SELECT * FROM areas")
        assert isinstance(result, list)

    async def test_fetchone_returns_none_on_miss(self, db):
        """fetchone should return None when no match."""
        result = await db.fetchone("SELECT * FROM areas WHERE id = ?", ("nonexistent",))
        assert result is None

    async def test_cascade_delete_area(self, db_with_area):
        """Deleting an area should cascade to sections, entities, tags."""
        db, area_id, section_id = db_with_area

        # Add an entity
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (str(uuid.uuid4()), area_id, "light.test", section_id, Database.now()),
        )
        # Add a tag
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
            (area_id, "floor1"),
        )
        await db.commit()

        # Verify data exists
        entities = await db.fetchall(
            "SELECT * FROM area_entities WHERE area_id = ?", (area_id,)
        )
        assert len(entities) == 1

        # Delete area
        await db.execute("DELETE FROM areas WHERE id = ?", (area_id,))
        await db.commit()

        # Verify cascade
        entities = await db.fetchall(
            "SELECT * FROM area_entities WHERE area_id = ?", (area_id,)
        )
        assert len(entities) == 0

        sections = await db.fetchall(
            "SELECT * FROM area_sections WHERE area_id = ?", (area_id,)
        )
        assert len(sections) == 0

        tags = await db.fetchall(
            "SELECT * FROM area_tags WHERE area_id = ?", (area_id,)
        )
        assert len(tags) == 0


class TestDatabaseSettings:
    """Settings key-value store."""

    async def test_get_set_setting(self, db):
        """get_setting / set_setting round-trip."""
        await db.set_setting("test_key", "test_value")
        value = await db.get_setting("test_key")
        assert value == "test_value"

    async def test_set_setting_overwrites(self, db):
        """set_setting should overwrite existing value."""
        await db.set_setting("key", "v1")
        await db.set_setting("key", "v2")
        assert await db.get_setting("key") == "v2"

    async def test_get_setting_nonexistent(self, db):
        """get_setting returns None for missing keys."""
        assert await db.get_setting("nonexistent_key") is None


class TestDatabaseBackup:
    """Backup and corruption recovery."""

    async def test_daily_snapshot_creates_file(self, db):
        """A daily snapshot is written to <data>/backups (once per day)."""
        await db.execute(
            "INSERT INTO areas (id, name, created_at, updated_at) "
            "VALUES ('a1', 'Area', 'x', 'x')"
        )
        await db.commit()
        path = snapshots.take_daily_snapshot(db.db_path)
        assert path is not None
        assert os.path.exists(path)
        assert os.path.dirname(path) == snapshots.backup_dir(db.db_path)
        assert snapshots.take_daily_snapshot(db.db_path) is None

    async def test_restore_from_backup(self, tmp_path):
        """Database should be recoverable from a snapshot after corruption."""
        db_path = str(tmp_path / "test_restore.db")
        db = Database(db_path)
        await db.initialize()

        # Write some data (stays in the WAL: the snapshot reads through it)
        await db.set_setting("important", "data")
        snap = snapshots.create_snapshot(
            db_path, os.path.join(snapshots.backup_dir(db_path), "test_restore-x.db")
        )
        assert os.path.exists(snap)

        # Simulate corruption by closing and overwriting
        await db.close()
        with open(db_path, "wb") as f:
            f.write(b"corrupted data that is not valid sqlite")

        # Re-initialize should recover from the snapshot
        db2 = Database(db_path)
        await db2.initialize()

        # Data should be preserved from backup
        value = await db2.get_setting("important")
        assert value == "data"
        assert db2.recovery_notice and "restored" in db2.recovery_notice
        await db2.close()

    async def test_integrity_check_on_init(self, tmp_path):
        """Initialization should run integrity check on existing DB."""
        db_path = str(tmp_path / "test_integrity.db")
        db = Database(db_path)
        await db.initialize()
        await db.close()

        # Re-open (schema already exists, should pass integrity check)
        db2 = Database(db_path)
        await db2.initialize()
        # Should not raise
        row = await db2.fetchone("PRAGMA user_version")
        assert list(row.values()) == [SCHEMA_VERSION]
        await db2.close()


class TestDatabaseNow:
    """Timestamp helper."""

    def test_now_returns_iso_string(self):
        """Database.now() should return an ISO-format UTC timestamp."""
        ts = Database.now()
        assert isinstance(ts, str)
        assert "T" in ts
        # Should contain timezone info
        assert "+" in ts or "Z" in ts


class TestDatabaseClientOperations:
    """Paired client CRUD operations."""

    async def test_insert_and_fetch_client(self, db_with_client):
        """Insert and retrieve a paired client."""
        db, client_id, _, secret_hash = db_with_client
        client = await db.fetchone(
            "SELECT * FROM paired_clients WHERE id = ?", (client_id,)
        )
        assert client is not None
        assert client["name"] == "Test Client"
        assert client["secret_hash"] == secret_hash
        assert client["is_active"] == 1

    async def test_client_lookup_by_secret_hash(self, db_with_client):
        """Client should be findable by secret hash."""
        db, client_id, _, secret_hash = db_with_client
        client = await db.fetchone(
            "SELECT id FROM paired_clients WHERE secret_hash = ? AND is_active = 1",
            (secret_hash,),
        )
        assert client is not None
        assert client["id"] == client_id

    async def test_client_deactivate(self, db_with_client):
        """Deactivated clients should not be found by active lookup."""
        db, client_id, _, secret_hash = db_with_client
        await db.execute(
            "UPDATE paired_clients SET is_active = 0 WHERE id = ?",
            (client_id,),
        )
        await db.commit()

        client = await db.fetchone(
            "SELECT id FROM paired_clients WHERE secret_hash = ? AND is_active = 1",
            (secret_hash,),
        )
        assert client is None


class TestNotificationTable:
    """Client notification operations."""

    async def test_create_notification(self, db_with_client):
        """Insert and retrieve a notification."""
        db, client_id, _, _ = db_with_client
        notif_id = str(uuid.uuid4())
        now = Database.now()

        await db.execute(
            "INSERT INTO client_notifications "
            "(id, client_id, title, message, notification_type, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                notif_id,
                client_id,
                "Test Title",
                "Test Message",
                "normal",
                "pending",
                now,
            ),
        )
        await db.commit()

        notif = await db.fetchone(
            "SELECT * FROM client_notifications WHERE id = ?", (notif_id,)
        )
        assert notif is not None
        assert notif["title"] == "Test Title"
        assert notif["status"] == "pending"

    async def test_notification_cascade_on_client_delete(self, db_with_client):
        """Deleting a client should cascade-delete notifications."""
        db, client_id, _, _ = db_with_client
        notif_id = str(uuid.uuid4())
        now = Database.now()

        await db.execute(
            "INSERT INTO client_notifications "
            "(id, client_id, title, message, notification_type, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (notif_id, client_id, "Alert", "Body", "normal", "pending", now),
        )
        await db.commit()

        # Delete client
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (client_id,))
        await db.commit()

        # Notification should be gone
        notif = await db.fetchone(
            "SELECT * FROM client_notifications WHERE id = ?", (notif_id,)
        )
        assert notif is None
