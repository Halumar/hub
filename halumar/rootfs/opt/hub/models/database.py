"""Database module for Halumar.

One shared aiosqlite connection (WAL). Schema changes run as versioned
migrations (``services/db_migrations.py``, ``PRAGMA user_version``), never as
a recreate. Writes that belong together go through
``async with db.transaction():`` (BEGIN IMMEDIATE, commit/rollback, global
write lock). Snapshots live in ``<data>/backups``
(``services/db_snapshots.py``).

Start-up order (``initialize``):
1. ``/data`` from a restored HA backup -> use the consistent backup snapshot.
2. Database file missing but snapshots present -> restore the newest.
3. Open, read the version, integrity check. Damaged -> restore the newest
   valid snapshot; without one the start stops with
   :class:`DatabaseUnavailableError` and the damaged files stay untouched,
   unless the app option ``reset_corrupt_database`` is on.
4. Fresh install -> final schema in one transaction. Older -> snapshot, then
   one transaction per migration. Newer (downgrade) -> left as it is.
5. Current or migrated version: every table/column of the schema must exist.
   A database of a different pre-release build with the same version number
   stops the start (:class:`DatabaseUnavailableError`), or is archived
   and replaced by an empty one with ``reset_corrupt_database``.
"""

import asyncio
import contextvars
import hashlib
import logging
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite

from brand import BRAND_NAME
from services import db_snapshots as snapshots
from services.atomic_io import atomic_copy
from services.crypto import encrypt_token
from services.profiles.migration_v9 import MigrationCheckError
from services.db_migrations import (
    SCHEMA_SQL,
    latest_version,
    missing_schema_parts,
    seed_fresh,
    split_sql,
    steps as migration_steps,
)

logger = logging.getLogger("hub.database")

# ha_token_status of a paired client: "pending" until the
# integration delivered the token of the device's own HA user, then "active".
HA_TOKEN_PENDING = "pending"
HA_TOKEN_ACTIVE = "active"

_UNSET = object()

SCHEMA_VERSION = latest_version()

__all__ = [
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "Database",
    "DatabaseUnavailableError",
    "HA_TOKEN_ACTIVE",
    "HA_TOKEN_PENDING",
]


MIGRATION_CHECK_ACTION = (
    "Restore the previous add-on version, or turn on the add-on option "
    "'reset_corrupt_database' and restart to start with an empty database "
    "(pair the apps again and set up the profiles; turn the option off afterwards)."
)


class _CorruptDatabase(Exception):
    """Raised internally when an existing database fails the integrity check."""


class DatabaseUnavailableError(RuntimeError):
    """The database is damaged and there is nothing safe to restore.

    The hub starts in an error state (``/api/health`` 503 with ``reason``
    and ``action``) instead of replacing the user's data with an empty
    database. The damaged files are left exactly where they are.
    """

    def __init__(self, reason: str, action: str):
        super().__init__(reason)
        self.reason = reason
        self.action = action


def _is_corruption(exc: BaseException) -> bool:
    """True for errors that mean "the file is damaged", not "busy" or "I/O".

    sqlite3 reports a damaged file as a plain DatabaseError ("file is not a
    database", "database disk image is malformed"); OperationalError covers
    locks, I/O and permission problems, which must not trigger a recreate.
    """
    if isinstance(exc, _CorruptDatabase):
        return True
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    if "malformed" in msg or "not a database" in msg:
        return True
    return type(exc) is sqlite3.DatabaseError


_WRITE_IN_CTE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def _is_write(sql: str) -> bool:
    """Conservative: everything that is not clearly a read counts as a write."""
    text = sql.lstrip()
    while text.startswith("--") or text.startswith("/*"):
        end = text.find("\n") if text.startswith("--") else text.find("*/")
        if end < 0:
            return True
        text = text[end + (1 if text.startswith("--") else 2) :].lstrip()
    first = text.split(None, 1)[0].upper() if text else ""
    if first in ("SELECT", "EXPLAIN", "VALUES"):
        return False
    if first == "PRAGMA":
        return "=" in text
    if first == "WITH":
        return bool(_WRITE_IN_CTE.search(text))
    return True


class _Tx:
    """State of the running ``transaction()`` (one at a time per Database)."""

    __slots__ = ("db", "active", "depth")

    def __init__(self, db: "Database"):
        self.db = db
        self.active = True
        self.depth = 0


# The transaction the current coroutine (and tasks it spawned) runs in.
_current_tx: contextvars.ContextVar[_Tx | None] = contextvars.ContextVar(
    "hub_db_tx", default=None
)


CORRUPT_ACTION = (
    f"Restore a Home Assistant backup of the {BRAND_NAME} add-on (Settings > System > "
    "Backups), or turn on the add-on option 'reset_corrupt_database' and restart "
    "to start with an empty database (the damaged file is kept as "
    "hub.db.corrupt-<time> in the add-on data; turn the option off again "
    "afterwards)."
)


SCHEMA_MISMATCH_ACTION = (
    "Turn on the add-on option 'reset_corrupt_database' and restart to start with "
    "an empty database (the old file is kept as hub.db.mismatch-<time> in the "
    "add-on data; pair the apps again and turn the option off afterwards), or "
    f"restore a Home Assistant backup made with this {BRAND_NAME} version."
)


class Database:
    """Async SQLite database wrapper."""

    def __init__(self, db_path: str, *, reset_on_corruption: bool = False):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._tx: _Tx | None = None
        # Opt-in (app option reset_corrupt_database): a damaged database
        # without a usable snapshot is archived and replaced by an empty one.
        self.reset_on_corruption = reset_on_corruption
        # Set when the start restored or replaced the database; main.py
        # reports it (log, HA notification, /api/health).
        self.recovery_notice: str | None = None
        # True when this start used the snapshot of a restored HA backup; the
        # key file is not in HA backups, main.py tells check_keyfile.
        self.restored_from_ha_backup = False
        self.created_fresh = False

    # ------------------------------------------------------------------
    # Opening, recovery, schema
    # ------------------------------------------------------------------

    def _corrupt_archive_path(self, kind: str = "corrupt") -> str:
        """Distinct, timestamped archive name next to the database."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        return f"{self.db_path}.{kind}-{ts}"

    def _archive_corrupt_files(self, kind: str = "corrupt") -> str:
        """Move the DB AND its -wal/-shm to a timestamped archive name.

        The WAL of a replaced database must never stay next to a restored or
        new database file: SQLite associates a WAL with a database by name
        only and would replay the stale frames onto it.
        """
        archive = self._corrupt_archive_path(kind)
        for suffix in ("", "-wal", "-shm"):
            src = self.db_path + suffix
            if os.path.exists(src):
                os.replace(src, archive + suffix)
        return archive

    async def _place_snapshot(self, snapshot: str) -> None:
        """Copy a verified snapshot to the database path (atomic, fsynced)."""
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                raise RuntimeError(
                    f"refusing to restore next to a stale {self.db_path}{suffix}"
                )
        await asyncio.to_thread(atomic_copy, snapshot, self.db_path)

    @staticmethod
    def _describe_snapshot(path: str) -> str:
        created = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        return f"{os.path.basename(path)} ({created.strftime('%Y-%m-%d %H:%M')} UTC)"

    async def _restore_before_open(self) -> None:
        """Steps 1 and 2 of the start-up order (see module docstring)."""
        snap = await asyncio.to_thread(
            snapshots.pending_ha_backup_restore, self.db_path
        )
        if snap:
            archived = None
            if any(os.path.exists(self.db_path + s) for s in ("", "-wal", "-shm")):
                archived = self._archive_corrupt_files("pre-restore")
            await self._place_snapshot(snap)
            snapshots.clear_ha_backup_marker(self.db_path)
            self.restored_from_ha_backup = True
            self.recovery_notice = (
                f"{BRAND_NAME} data was restored from a Home Assistant backup; the "
                f"consistent snapshot {self._describe_snapshot(snap)} is used."
                + (f" The previous files were kept as {archived}." if archived else "")
            )
            logger.warning(self.recovery_notice)
            return

        if os.path.exists(self.db_path):
            return
        snap = await asyncio.to_thread(snapshots.newest_valid_snapshot, self.db_path)
        if snap is None:
            return
        if any(os.path.exists(self.db_path + s) for s in ("-wal", "-shm")):
            self._archive_corrupt_files("orphan")
        await self._place_snapshot(snap)
        self.recovery_notice = (
            f"The {BRAND_NAME} database file was missing and has been restored from "
            f"the snapshot {self._describe_snapshot(snap)}."
        )
        logger.warning(self.recovery_notice)

    async def _try_restore_from_backup(self) -> bool:
        """Replace the damaged database with the newest valid snapshot.

        Returns:
            True  -- a valid snapshot was found and copied into place.
            False -- there is NO valid snapshot (none, or all damaged).

        Raises:
            Any exception from the restore step itself (closing the
            connection, archiving, copying). Those are transient (lock/IO)
            and MUST NOT be swallowed: the snapshot stays untouched and the
            operator can intervene.
        """
        snap = await asyncio.to_thread(snapshots.newest_valid_snapshot, self.db_path)
        if snap is None:
            logger.warning(
                "No usable snapshot in %s", snapshots.backup_dir(self.db_path)
            )
            return False
        if self._db:
            await self._db.close()
            self._db = None
        corrupt_path = self._archive_corrupt_files()
        await self._place_snapshot(snap)
        self.recovery_notice = (
            f"The {BRAND_NAME} database was damaged and has been restored from the "
            f"snapshot {self._describe_snapshot(snap)}. Changes made after that "
            f"are missing. The damaged file was kept as {corrupt_path}."
        )
        logger.warning(self.recovery_notice)
        return True

    async def _integrity_check(self) -> bool:
        """Run PRAGMA integrity_check on the database.

        Returns True if the database passes integrity checks.
        """
        try:
            async with self._db.execute("PRAGMA integrity_check") as cursor:
                row = await cursor.fetchone()
                result = row[0] if row else "unknown"
                if result == "ok":
                    logger.info("Database integrity check: OK")
                    return True
                else:
                    logger.error("Database integrity check FAILED: %s", result)
                    return False
        except Exception as e:
            logger.error("Database integrity check error: %s", e)
            return False

    async def _configure_pragmas(self) -> None:
        """Set pragmas for performance and reliability."""
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA busy_timeout=5000")

    async def _open(self) -> None:
        """Open the connection and set pragmas (first statement reads the header)."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._configure_pragmas()

    async def _close_quietly(self) -> None:
        """Close the connection, never raise (used on failure paths)."""
        if self._db is None:
            return
        try:
            await self._db.close()
        except Exception as e:  # noqa: BLE001 - best effort on a failure path
            logger.error("Closing the database after a failure failed: %s", e)
        self._db = None

    async def _read_schema_version(self) -> int:
        """Return the schema version (``PRAGMA user_version``); 0 = empty file.

        A file with tables but without a version was not created by this
        add-on (a fresh install writes schema and version in one transaction)
        and is handled like a damaged database: restore a snapshot or stop,
        never "upgrade" or recreate it silently.
        """
        async with self._db.execute("PRAGMA user_version") as cursor:
            user_version = (await cursor.fetchone())[0]
        async with self._db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ) as cursor:
            tables = (await cursor.fetchone())[0]
        if not tables:
            return 0
        if user_version <= 0:
            raise _CorruptDatabase(
                f"{tables} tables but no schema version (not a {BRAND_NAME} database)"
            )
        return user_version

    async def _recover_from_corruption(self) -> bool:
        """Restore the newest snapshot; without one stop (or reset if opted in).

        Returns True if a snapshot was restored (caller re-inspects it), False
        if an empty database was created (option ``reset_corrupt_database``).
        Raises :class:`DatabaseUnavailableError` otherwise. Transient restore
        errors propagate (see _try_restore_from_backup).
        """
        if await self._try_restore_from_backup():
            await self._open()
            return True

        await self._close_quietly()
        if not self.reset_on_corruption:
            reason = (
                f"The {BRAND_NAME} database {self.db_path} is damaged and there is no "
                "usable snapshot to restore. Nothing was changed or deleted."
            )
            logger.error("%s %s", reason, CORRUPT_ACTION)
            raise DatabaseUnavailableError(reason, CORRUPT_ACTION)

        corrupt_path = self._archive_corrupt_files()
        await self._open()
        await self._create_schema()
        self.recovery_notice = (
            f"The {BRAND_NAME} database was damaged and has been replaced by an EMPTY "
            "database (option reset_corrupt_database). The damaged file was kept "
            f"as {corrupt_path}. Turn the option off again."
        )
        logger.error(self.recovery_notice)
        return False

    async def _open_and_inspect(self) -> int:
        """Open the DB and return its schema version; corruption -> recovery.

        Corruption (unreadable header, malformed pages, failed integrity
        check of an existing DB) goes through _recover_from_corruption at most
        once. Any other error (locked, I/O, permissions) propagates untouched:
        the files are left alone and the caller stops cleanly.
        """
        for attempt in (1, 2):
            try:
                if self._db is None:
                    await self._open()
                version = await self._read_schema_version()
                if version != 0 and not await self._integrity_check():
                    raise _CorruptDatabase("integrity check failed")
                return version
            except Exception as e:
                if not _is_corruption(e) or attempt == 2:
                    raise
                logger.error("Database is corrupt (%s). Trying to recover...", e)
                if not await self._recover_from_corruption():
                    return SCHEMA_VERSION
        raise AssertionError("unreachable")

    async def initialize(self) -> None:
        """Initialize database and create/upgrade the schema.

        On ANY failure the connection is closed before the error propagates:
        an open aiosqlite worker thread would otherwise keep the process
        alive after the app startup failed (container "running", nothing
        answering, no restart).
        """
        logger.info("Initializing database at %s", self.db_path)
        try:
            await self._initialize()
        except BaseException:
            await self._close_quietly()
            raise

    async def _initialize(self) -> None:
        await self._restore_before_open()
        current_version = await self._open_and_inspect()

        if current_version == 0:
            await self._create_schema()
            logger.info("Database created (schema v%d)", SCHEMA_VERSION)
        elif current_version > SCHEMA_VERSION:
            # Older add-on on a newer database (downgrade): keep the
            # data and the recorded version, change nothing.
            logger.warning(
                "Database schema v%d is newer than this add-on (v%d). "
                "Keeping data and schema version unchanged.",
                current_version,
                SCHEMA_VERSION,
            )
        elif current_version < SCHEMA_VERSION:
            await self._migrate(current_version)
            await self._verify_schema()
        else:
            logger.info("Database at schema v%d", current_version)
            await self._verify_schema()

        # Ensure API token exists (auto-generate on first start)
        await self._ensure_api_token()

    async def _verify_schema(self) -> None:
        """Stop with a clear error if the schema is not the one of this version.

        A database of a pre-release build can carry the same version
        number with different tables, and there is no upgrade path from
        pre-release builds. Such a database would fail every request that
        touches a missing column while health stays green, so the hub
        starts in the error state instead (health 503, Ingress page, HA
        notification), or, with the option ``reset_corrupt_database``,
        archives the file and starts empty.
        """
        missing = await missing_schema_parts(self._db)
        if not missing:
            return
        shown = ", ".join(missing[:5]) + (" …" if len(missing) > 5 else "")
        if not self.reset_on_corruption:
            reason = (
                f"The {BRAND_NAME} database {self.db_path} was written by a "
                f"different {BRAND_NAME} build and does not match this version "
                f"(missing: {shown}). Nothing was changed or deleted."
            )
            logger.error("%s %s", reason, SCHEMA_MISMATCH_ACTION)
            raise DatabaseUnavailableError(reason, SCHEMA_MISMATCH_ACTION)

        await self._close_quietly()
        archived = self._archive_corrupt_files("mismatch")
        await self._open()
        await self._create_schema()
        self.recovery_notice = (
            f"The {BRAND_NAME} database did not match this version (missing: "
            f"{shown}) and has been replaced by an EMPTY database (option "
            f"reset_corrupt_database). The old file was kept as {archived}. Pair "
            "the apps again and turn the option off again."
        )
        logger.error(self.recovery_notice)

    async def _create_schema(self) -> None:
        """Fresh install: the final schema and its version in one transaction."""
        async with self.transaction():
            for stmt in split_sql(SCHEMA_SQL):
                await self._db.execute(stmt)
            await seed_fresh(self._db)
            await self._set_version(SCHEMA_VERSION)
        self.created_fresh = True

    async def _set_version(self, version: int) -> None:
        await self._db.execute(f"PRAGMA user_version = {int(version)}")

    async def _migrate(self, current_version: int) -> None:
        """Snapshot, then one transaction per migration step."""
        target = SCHEMA_VERSION
        plan = migration_steps(current_version, target)
        snapshot = await asyncio.to_thread(
            snapshots.take_pre_migration_snapshot,
            self.db_path,
            current_version,
            target,
        )
        logger.warning(
            "Upgrading database schema v%d -> v%d (snapshot: %s)",
            current_version,
            target,
            snapshot,
        )
        for step in plan:
            if step.foreign_keys_off:
                await self._db.execute("PRAGMA foreign_keys=OFF")
            try:
                async with self.transaction():
                    await step.apply(self._db)
                    if step.foreign_keys_off:
                        await self._check_foreign_keys(step.version)
                    await self._set_version(step.version)
            except MigrationCheckError as e:
                reason = (
                    f"The {BRAND_NAME} database could not be carried over to schema "
                    f"v{step.version}: {e}. Nothing was changed."
                )
                logger.error("%s Report: %s", reason, e.report)
                if not self.reset_on_corruption:
                    raise DatabaseUnavailableError(
                        reason, MIGRATION_CHECK_ACTION
                    ) from e
                # Fallback "set up again": archive, start empty.
                await self._db.execute("PRAGMA foreign_keys=ON")
                await self._close_quietly()
                archived = self._archive_corrupt_files("migration")
                await self._open()
                await self._create_schema()
                self.recovery_notice = (
                    f"{reason} The database was replaced by an EMPTY one (option "
                    f"reset_corrupt_database); the old file was kept as {archived}. "
                    "Pair the apps again and turn the option off again."
                )
                logger.error(self.recovery_notice)
                return
            finally:
                if step.foreign_keys_off:
                    await self._db.execute("PRAGMA foreign_keys=ON")
            logger.info(
                "Database migrated to schema v%d: %s", step.version, step.description
            )

    async def _check_foreign_keys(self, version: int) -> None:
        async with self._db.execute("PRAGMA foreign_key_check") as cursor:
            rows = await cursor.fetchall()
        if rows:
            raise RuntimeError(
                f"migration v{version} left foreign key violations: "
                f"{[tuple(r) for r in rows][:5]}"
            )

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self):
        """Unit of work: ``async with db.transaction(): ...``.

        BEGIN IMMEDIATE under a global write lock; commit on success, rollback
        on any exception (including cancellation). While it runs, writes and
        ``commit()``/``rollback()`` from other coroutines wait, so nobody can
        commit or roll back half of it. Inside it, ``commit()``
        is a no-op (the transaction commits at the end) and a nested
        ``transaction()`` becomes a SAVEPOINT.

        Do not await other actors (HA calls, WebSocket round trips) inside:
        everything that writes waits for the lock meanwhile.
        """
        conn = self.connection
        current = _current_tx.get()
        if current is not None and current.active and current.db is self:
            current.depth += 1
            name = f"nha_sp_{current.depth}"
            await conn.execute(f"SAVEPOINT {name}")
            try:
                yield self
            except BaseException:
                await conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                await conn.execute(f"RELEASE SAVEPOINT {name}")
                raise
            else:
                await conn.execute(f"RELEASE SAVEPOINT {name}")
            finally:
                current.depth -= 1
            return

        async with self._write_lock:
            tx = _Tx(self)
            token = _current_tx.set(tx)
            self._tx = tx
            try:
                await self._begin_immediate(conn)
                try:
                    yield self
                except BaseException:
                    await self._rollback_quietly(conn)
                    raise
                try:
                    await conn.commit()
                except BaseException:
                    await self._rollback_quietly(conn)
                    raise
            finally:
                tx.active = False
                self._tx = None
                _current_tx.reset(token)

    @staticmethod
    async def _begin_immediate(conn: aiosqlite.Connection) -> None:
        for attempt in range(3):
            try:
                await conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as e:
                if "within a transaction" not in str(e) or attempt == 2:
                    raise
                # A call site that executes now and commits later left pending
                # writes on the shared connection. Commit them first, exactly
                # what its own commit() would do.
                logger.debug("Committing pending writes before BEGIN IMMEDIATE")
                await conn.commit()

    @staticmethod
    async def _rollback_quietly(conn: aiosqlite.Connection) -> None:
        try:
            await conn.rollback()
        except Exception as e:  # noqa: BLE001 - the original error matters
            logger.error("Rollback failed: %s", e)

    def _in_own_transaction(self) -> bool:
        tx = self._tx
        return tx is not None and _current_tx.get() is tx

    async def _wait_for_foreign_transaction(self, sql: str) -> None:
        """Writes from outside a running transaction wait until it ended.

        Reads do not wait (they may see its uncommitted rows, as before);
        waiting for reads could deadlock a transaction that awaits a reader.
        """
        if self._tx is None or self._in_own_transaction() or not _is_write(sql):
            return
        async with self._write_lock:
            pass

    @property
    def in_transaction(self) -> bool:
        """True while a ``transaction()`` is running (any coroutine)."""
        return self._tx is not None

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def probe(self, timeout: float = 2.0) -> str | None:
        """Health probe for /api/health: None if usable, else a short reason.

        1. The shared connection answers a read within ``timeout`` (detects a
           closed connection or a stuck worker thread).
        2. A separate short-lived connection can take the write lock within
           ``timeout`` (detects a database locked by someone else, where
           reads still work but every write fails). Skipped while our own
           connection holds an open transaction: then the lock is ours.
        """
        if self._db is None:
            return "database not initialized"
        try:
            await asyncio.wait_for(
                self.fetchone("SELECT count(*) FROM settings"),
                timeout,
            )
        except asyncio.TimeoutError:
            return "database read timed out"
        except Exception as e:  # noqa: BLE001 - reported, not raised
            return f"database read failed: {type(e).__name__}: {e}"

        if self._db.in_transaction:
            return None

        def _write_lock_probe() -> None:
            conn = sqlite3.connect(self.db_path, timeout=timeout)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.rollback()
            finally:
                conn.close()

        try:
            await asyncio.to_thread(_write_lock_probe)
        except Exception as e:  # noqa: BLE001 - reported, not raised
            return f"database not writable: {type(e).__name__}: {e}"
        return None

    # ------------------------------------------------------------------
    # Statement helpers
    # ------------------------------------------------------------------

    @property
    def connection(self) -> aiosqlite.Connection:
        """Raw connection. Prefer the helpers below (they honour the write
        lock); never call ``commit()``/``rollback()`` on it directly."""
        assert self._db is not None, "Database not initialized"
        return self._db

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        """Execute SQL statement."""
        await self._wait_for_foreign_transaction(sql)
        return await self.connection.execute(sql, params)

    async def executemany(self, sql: str, params: list) -> aiosqlite.Cursor:
        """Execute SQL statement with many params."""
        await self._wait_for_foreign_transaction(sql)
        return await self.connection.executemany(sql, params)

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        """Fetch one row as dict."""
        await self._wait_for_foreign_transaction(sql)
        async with self.connection.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        """Fetch all rows as dicts."""
        await self._wait_for_foreign_transaction(sql)
        async with self.connection.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def commit(self) -> None:
        """Commit pending writes of call sites outside ``transaction()``.

        Inside ``transaction()`` this is a no-op: the transaction commits as
        a whole. Outside it waits for a running transaction, so it can never
        commit half of someone else's unit of work.
        """
        if self._in_own_transaction():
            return
        async with self._write_lock:
            await self.connection.commit()

    async def rollback(self) -> None:
        """Roll back pending writes outside ``transaction()`` (same lock as commit)."""
        if self._in_own_transaction():
            raise RuntimeError(
                "rollback() inside db.transaction(): raise an exception instead"
            )
        async with self._write_lock:
            await self.connection.rollback()

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Database closed")

    async def _ensure_api_token(self) -> None:
        """Ensure an API token exists. Auto-generate one on first start.

        The token is stored as a SHA-256 hash immediately; the plaintext is
        neither logged nor persisted (the admin regenerates it to see one).
        """
        has_hash = await self.get_setting("api_token_hash")

        if has_hash:
            # Already has a hashed token - nothing to do
            return

        # Fresh install - generate, hash, store hash only
        token = secrets.token_hex(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        async with self.transaction():
            await self.execute(
                "INSERT INTO settings (key, value) VALUES ('api_token_hash', ?)",
                (token_hash,),
            )
        logger.info(
            "Generated new API token (first start). "
            "Retrieve it via Settings > API token > Regenerate."
        )

    async def get_setting(self, key: str) -> str | None:
        """Get a setting value by key."""
        row = await self.fetchone("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        """Set a setting value (insert or update)."""
        await self.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.commit()

    # ------------------------------------------------------------------
    # Per-device HA user. The integration creates the HA user and
    # its token; the hub only stores the delivered token (encrypted) and the
    # identity it reports to the integration.
    # ------------------------------------------------------------------

    async def store_client_token(
        self,
        client_id: str,
        *,
        ha_user_id: str,
        refresh_token_id: str,
        token: str,
    ) -> bool:
        """Store the token of the device's HA user and mark it active.

        Only for a device that is still active and not pending deletion, in
        the same statement (a revoke between the caller's check and
        this write must not leave an ``active`` token). False if not stored.
        """
        encrypted = await encrypt_token(self, token)
        async with self.transaction():
            cursor = await self.execute(
                "UPDATE paired_clients SET ha_user_id = ?, ha_refresh_token_id = ?, "
                "ha_token_encrypted = ?, ha_token_status = ?, "
                "ha_token_pending_since = NULL, ha_token_minted_at = ? "
                "WHERE id = ? AND is_active = 1 AND pending_deletion_at IS NULL",
                (
                    ha_user_id,
                    refresh_token_id,
                    encrypted,
                    HA_TOKEN_ACTIVE,
                    self.now(),
                    client_id,
                ),
            )
            if not cursor.rowcount:
                return False
            # The delivered token answers every outstanding request.
            await self.execute(
                "DELETE FROM token_requests WHERE client_id = ?", (client_id,)
            )
        return True

    async def mark_client_pending(self, client_id: str) -> None:
        """Drop an unusable token: the device waits for a new one."""
        async with self.transaction():
            await self.execute(
                "UPDATE paired_clients SET ha_token_status = ?, "
                "ha_token_pending_since = ?, ha_token_encrypted = NULL "
                "WHERE id = ?",
                (HA_TOKEN_PENDING, self.now(), client_id),
            )
            # A new token is needed anyway: start over with a fresh backoff.
            await self.execute(
                "DELETE FROM token_requests WHERE client_id = ?", (client_id,)
            )

    async def set_client_identity(
        self,
        client_id: str,
        *,
        name=_UNSET,
        owner_label=_UNSET,
    ) -> bool:
        """Update the identity fields that were passed. True if a row changed.

        ``owner_label`` None clears it. Remote access is the profile rule
        ``remote_access``, not a column.
        """
        updates: list[str] = []
        params: list = []
        if name is not _UNSET:
            updates.append("name = ?")
            params.append(name)
        if owner_label is not _UNSET:
            updates.append("owner_label = ?")
            params.append(owner_label)
        if not updates:
            return False
        params.append(client_id)
        async with self.transaction():
            cursor = await self.execute(
                f"UPDATE paired_clients SET {', '.join(updates)} WHERE id = ?",
                tuple(params),
            )
        return cursor.rowcount > 0

    async def set_client_remote_pin(
        self, client_id: str, pin_hash: str | None, pin_salt: str | None
    ) -> bool:
        """Store (or clear with None) the device's remote PIN (PBKDF2 only)."""
        async with self.transaction():
            cursor = await self.execute(
                "UPDATE paired_clients SET settings_pin_hash = ?, settings_pin_salt = ? "
                "WHERE id = ?",
                (pin_hash, pin_salt, client_id),
            )
        return cursor.rowcount > 0

    async def has_encrypted_tokens(self) -> bool:
        """True if any Fernet ciphertext (device HA token) is stored.

        Decides whether a lost key file may be replaced by a new key (nothing
        to lose) or must stop the start.
        """
        row = await self.fetchone(
            "SELECT 1 AS x FROM paired_clients "
            "WHERE ha_token_encrypted IS NOT NULL AND ha_token_encrypted != '' "
            "LIMIT 1"
        )
        return row is not None

    async def set_pending_hidden(self, client_id: str) -> None:
        """Mark a pending-deletion tombstone as hidden from the clients list.

        Sets ``pending_hidden_at`` to now. Does NOT delete the row: the
        tombstone persists as a background marker so a returning app is still
        rejected (4001) on reconnect and only then removed.
        """
        await self.execute(
            "UPDATE paired_clients SET pending_hidden_at = ? WHERE id = ?",
            (self.now(), client_id),
        )
        await self.commit()

    @staticmethod
    def now() -> str:
        """Get current UTC timestamp as ISO string."""
        return datetime.now(timezone.utc).isoformat()
