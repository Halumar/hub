"""Corruption recovery keeps a valid backup intact.

A transient restore failure (a lock or I/O error) never overwrites or corrupts
a still-valid backup file: the current, corrupt database is never copied over
it. A valid backup is restored on corruption.
"""

import hashlib
import os

import pytest

from models.database import Database
from services.db_snapshots import backup_dir, create_snapshot


def _backup_path(db_path: str) -> str:
    name = os.path.splitext(os.path.basename(db_path))[0]
    return os.path.join(backup_dir(db_path), f"{name}-daily-20261118.db")


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


async def _make_db_with_valid_backup(db_path: str) -> tuple[Database, str]:
    """Initialize a DB, write data, checkpoint, and create a valid backup.

    Returns (closed-db-object, backup_path). Mirrors the tmp-db fixture
    pattern used in test_database.py's TestDatabaseBackup.
    """
    db = Database(db_path)
    await db.initialize()
    await db.set_setting("important", "data")
    create_snapshot(db_path, _backup_path(db_path))
    await db.close()

    # Drop WAL/SHM to simulate a clean on-disk state.
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    backup_path = _backup_path(db_path)
    assert os.path.exists(backup_path)
    return db, backup_path


def _corrupt_main_db(db_path: str) -> None:
    with open(db_path, "wb") as f:
        f.write(b"corrupted data that is not valid sqlite")


class TestTransientRestoreDoesNotClobberBackup:
    async def test_transient_restore_error_preserves_backup(
        self, tmp_path, monkeypatch
    ):
        """A transient restore failure must raise and leave the backup intact."""
        db_path = str(tmp_path / "test_h4.db")
        _, backup_path = await _make_db_with_valid_backup(db_path)

        backup_hash_before = _sha256(backup_path)
        _corrupt_main_db(db_path)

        # Force the corruption to be detected via the integrity-check path
        # (DB opens fine, but reports corruption), then simulate a transient
        # lock/IO error during the actual restore.
        async def _passes_open(self):
            return True

        async def _fails_integrity(self):
            return False

        async def _transient_restore(self):
            raise OSError("database is locked (transient)")

        monkeypatch.setattr(Database, "_integrity_check", _fails_integrity)
        monkeypatch.setattr(Database, "_try_restore_from_backup", _transient_restore)

        db2 = Database(db_path)
        # Initialize must FAIL LOUDLY rather than clobber the backup.
        with pytest.raises(OSError):
            await db2.initialize()
        if db2._db is not None:
            await db2.close()

        # The valid backup must be byte-for-byte unchanged.
        assert os.path.exists(backup_path), "backup file was deleted"
        assert _sha256(backup_path) == backup_hash_before, (
            "backup file was overwritten/corrupted during a transient restore error"
        )

        # And it must still be a valid SQLite DB holding the original data.
        import sqlite3

        conn = sqlite3.connect(backup_path)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            row = conn.execute(
                "SELECT value FROM settings WHERE key = 'important'"
            ).fetchone()
            assert row is not None and row[0] == "data"
        finally:
            conn.close()


class TestValidBackupStillRestores:
    async def test_happy_path_restore_still_works(self, tmp_path):
        """A valid backup is restored on corruption."""
        db_path = str(tmp_path / "test_h4_happy.db")
        _, backup_path = await _make_db_with_valid_backup(db_path)
        _corrupt_main_db(db_path)

        db2 = Database(db_path)
        await db2.initialize()
        # Data recovered from backup.
        assert await db2.get_setting("important") == "data"
        await db2.close()

        # Backup is preserved (not deleted) after a successful restore.
        assert os.path.exists(backup_path)
        # The corrupt live DB was archived under a distinct timestamped name,
        # NOT over the backup slot.
        archived = [
            p for p in os.listdir(tmp_path) if p.startswith("test_h4_happy.db.corrupt-")
        ]
        assert archived, "corrupt DB should be archived under .corrupt-<ts>"
