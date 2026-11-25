"""Database snapshots: daily, before migrations, for HA backups.

All snapshots are written with ``VACUUM INTO`` through a separate, short-lived
connection. That reads one consistent state of the database including the
WAL (a file copy of ``hub.db`` alone misses the WAL, a copy of the
db/-wal pair can be torn), and it only sees committed data.
Every snapshot is integrity-checked before it replaces anything.

Layout (``<data>/backups/``)::

    hub-daily-YYYYMMDD.db          one per UTC day, newest 7 kept
    hub-pre-vN-vM-<ts>.db          before every schema migration, newest 3 kept
    hub-ha-backup.db               written by the backup_pre hook
    ha-backup.marker                    exists only while an HA backup runs

HA backups (``config.yaml``: ``backup_pre``/``backup_post``): the pre hook
writes ``hub-ha-backup.db`` and the marker, the Supervisor tars ``/data``,
the post hook removes the marker. A restored HA backup therefore contains the
marker; on the next start :func:`pending_ha_backup_restore` tells the database
to use the consistent snapshot instead of the live db/-wal pair, which the
Supervisor copied while it was being written. If the hub was stopped during
the backup no hook runs, and the closed live database is consistent anyway.

Run as a script for the Supervisor hooks: ``python3 backup_hook.py pre|post``.
"""

import glob
import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from brand import BRAND_NAME
from services.atomic_io import atomic_write_text, fsync_dir

logger = logging.getLogger("hub.snapshots")

BACKUP_DIRNAME = "backups"
DAILY_KEEP = 7
PRE_MIGRATION_KEEP = 3
HA_BACKUP_MARKER = "ha-backup.marker"


def backup_dir(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), BACKUP_DIRNAME)


def _stem(db_path: str) -> str:
    return os.path.splitext(os.path.basename(db_path))[0]


def daily_path(db_path: str, day: datetime | None = None) -> str:
    day = day or datetime.now(timezone.utc)
    return os.path.join(
        backup_dir(db_path), f"{_stem(db_path)}-daily-{day.strftime('%Y%m%d')}.db"
    )


def pre_migration_path(db_path: str, from_version: int, to_version: int) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return os.path.join(
        backup_dir(db_path),
        f"{_stem(db_path)}-pre-v{from_version}-v{to_version}-{ts}.db",
    )


def ha_backup_path(db_path: str) -> str:
    return os.path.join(backup_dir(db_path), f"{_stem(db_path)}-ha-backup.db")


def ha_backup_marker_path(db_path: str) -> str:
    return os.path.join(backup_dir(db_path), HA_BACKUP_MARKER)


def verify_snapshot(path: str) -> str | None:
    """None if ``path`` is an intact Halumar database, else the reason.

    Opened ``immutable`` (no locks, no -wal/-shm created next to it): snapshot
    files are never written while they are checked.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    except sqlite3.Error as e:
        return f"cannot open: {e}"
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return f"integrity check failed: {row[0] if row else 'no result'}"
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "areas" not in tables or "settings" not in tables:
            return f"not a {BRAND_NAME} database"
    except sqlite3.Error as e:
        return f"unreadable: {e}"
    finally:
        conn.close()
    return None


def create_snapshot(db_path: str, dest: str, timeout: float = 10.0) -> str:
    """Write a verified snapshot of ``db_path`` to ``dest`` (atomic). Raises."""
    if not os.path.isfile(db_path):
        raise FileNotFoundError(db_path)
    directory = os.path.dirname(dest)
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(
        directory, f".{os.path.basename(dest)}.{uuid.uuid4().hex[:8]}.tmp"
    )
    # mode=rw: never create an empty database when the file vanished meanwhile.
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=timeout)
    try:
        conn.execute("VACUUM INTO ?", (tmp,))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    finally:
        conn.close()
    try:
        problem = verify_snapshot(tmp)
        if problem:
            raise sqlite3.DatabaseError(f"snapshot of {db_path} is unusable: {problem}")
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    fsync_dir(directory)
    return dest


def _rotate(pattern: str, keep: int) -> list[str]:
    """Delete all but the ``keep`` newest files matching ``pattern`` (by name)."""
    files = sorted(glob.glob(pattern))
    removed = files[:-keep] if keep > 0 else files
    for path in removed:
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("Could not remove old snapshot %s: %s", path, e)
    return removed


def _has_user_data(db_path: str) -> bool:
    conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=10.0)
    try:
        for table in ("areas", "paired_clients", "device_overrides"):
            try:
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return True
            except sqlite3.OperationalError:
                continue
        return False
    finally:
        conn.close()


def take_daily_snapshot(db_path: str, now: datetime | None = None) -> str | None:
    """Create today's snapshot if it does not exist yet; rotate to 7.

    Returns the new path, or None if today's snapshot exists or the database
    holds no user data yet (an empty snapshot must never become the newest
    restore point and replace a damaged database with nothing).
    """
    dest = daily_path(db_path, now)
    if os.path.exists(dest):
        return None
    if not _has_user_data(db_path):
        return None
    create_snapshot(db_path, dest)
    removed = _rotate(
        os.path.join(backup_dir(db_path), f"{_stem(db_path)}-daily-*.db"), DAILY_KEEP
    )
    logger.info(
        "Daily database snapshot written to %s (%d old removed)", dest, len(removed)
    )
    return dest


def take_pre_migration_snapshot(
    db_path: str, from_version: int, to_version: int
) -> str:
    dest = create_snapshot(
        db_path, pre_migration_path(db_path, from_version, to_version)
    )
    _rotate(
        os.path.join(backup_dir(db_path), f"{_stem(db_path)}-pre-v*.db"),
        PRE_MIGRATION_KEEP,
    )
    return dest


def restore_candidates(db_path: str) -> list[str]:
    """All snapshot files, newest first (by mtime)."""
    paths = glob.glob(os.path.join(backup_dir(db_path), f"{_stem(db_path)}-*.db"))
    return sorted(paths, key=lambda p: os.path.getmtime(p), reverse=True)


def newest_valid_snapshot(db_path: str) -> str | None:
    for path in restore_candidates(db_path):
        problem = verify_snapshot(path)
        if problem is None:
            return path
        logger.error("Snapshot %s is not usable: %s", path, problem)
    return None


def latest_snapshot_info(db_path: str) -> dict | None:
    """Newest snapshot for /api/health: ``{"file", "created"}`` or None."""
    candidates = [
        p
        for p in restore_candidates(db_path)
        if os.path.dirname(p) == backup_dir(db_path)
    ]
    if not candidates:
        return None
    newest = candidates[0]
    created = datetime.fromtimestamp(os.path.getmtime(newest), timezone.utc)
    return {
        "file": os.path.basename(newest),
        "created": created.replace(microsecond=0).isoformat(),
    }


def pending_ha_backup_restore(db_path: str) -> str | None:
    """Snapshot to restore when this /data comes from an HA backup, else None.

    The marker is only present between the pre and post hook, i.e. inside the
    backup archive. Found at start-up it means: this data was restored from an
    HA backup (or the Supervisor died mid-backup); the live db/-wal pair may be
    torn, the snapshot next to the marker is consistent.
    """
    marker = ha_backup_marker_path(db_path)
    if not os.path.exists(marker):
        return None
    snap = ha_backup_path(db_path)
    if os.path.isfile(snap) and verify_snapshot(snap) is None:
        return snap
    logger.error(
        "HA backup marker found but the backup snapshot %s is missing or damaged; "
        "keeping the live database",
        snap,
    )
    clear_ha_backup_marker(db_path)
    return None


def clear_ha_backup_marker(db_path: str) -> None:
    marker = ha_backup_marker_path(db_path)
    if os.path.exists(marker):
        os.remove(marker)
        fsync_dir(os.path.dirname(marker))


def run_backup_hook(action: str, db_path: str) -> int:
    """Supervisor ``backup_pre``/``backup_post`` entry point. Exit code 0 = ok.

    pre: consistent snapshot + marker. A failure fails the add-on backup (the
    Supervisor reports it) instead of silently archiving a torn database.
    post: remove the marker (the snapshot stays as an extra restore point).
    """
    if action == "pre":
        if not os.path.isfile(db_path):
            print(f"no database at {db_path}, nothing to snapshot")
            return 0
        dest = create_snapshot(db_path, ha_backup_path(db_path), timeout=30.0)
        atomic_write_text(
            ha_backup_marker_path(db_path),
            json.dumps(
                {
                    "snapshot": os.path.basename(dest),
                    "created": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
        print(f"snapshot for HA backup written to {dest}")
        return 0
    if action == "post":
        clear_ha_backup_marker(db_path)
        print("HA backup marker removed")
        return 0
    print(f"unknown action {action!r}, expected pre or post")
    return 2
