"""``async with db.transaction()`` on the shared connection.

Every coroutine shares one SQLite connection. Inside ``transaction()`` a
foreign ``commit()`` cannot persist half of a failed write and a foreign
``rollback()`` cannot undo a running import; plain call sites (``execute`` +
``commit``) keep working and wait for a running transaction.
"""

import asyncio
import sqlite3

import pytest


async def _area(db, area_id, name, tags=()):
    now = db.now()
    await db.execute(
        "INSERT INTO areas (id, name, description, created_at, updated_at) "
        "VALUES (?, ?, 'orig', ?, ?)",
        (area_id, name, now, now),
    )
    await db.executemany(
        "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
        [(area_id, t) for t in tags],
    )
    await db.commit()


def _on_disk(db, sql, params=()):
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


async def test_commit_on_success_rollback_on_error(db):
    async with db.transaction():
        await db.set_setting("k", "1")
    assert _on_disk(db, "SELECT value FROM settings WHERE key='k'") == [("1",)]

    with pytest.raises(ValueError):
        async with db.transaction():
            await db.set_setting("k", "2")
            raise ValueError("boom")
    assert await db.get_setting("k") == "1"
    assert _on_disk(db, "SELECT value FROM settings WHERE key='k'") == [("1",)]


async def test_nested_transaction_is_a_savepoint(db):
    async with db.transaction():
        await db.set_setting("outer", "1")
        with pytest.raises(KeyError):
            async with db.transaction():
                await db.set_setting("inner", "1")
                raise KeyError("inner fails")
        await db.set_setting("after", "1")
    assert await db.get_setting("outer") == "1"
    assert await db.get_setting("inner") is None
    assert await db.get_setting("after") == "1"


async def test_failed_unit_of_work_is_not_committed_by_a_foreign_commit(db):
    """Duplicate tags -> IntegrityError after UPDATE + DELETE. A
    plain commit() from another coroutine in the middle must neither persist
    the half-done write nor get lost itself."""
    await _area(db, "a1", "Tags", ["t1", "t2"])
    started = asyncio.Event()

    async def update_with_duplicate_tags():
        async with db.transaction():
            await db.execute("UPDATE areas SET description = 'partial' WHERE id = 'a1'")
            await db.execute("DELETE FROM area_tags WHERE area_id = 'a1'")
            started.set()
            await asyncio.sleep(0.05)
            await db.executemany(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
                [("a1", "dup"), ("a1", "dup")],
            )

    async def unrelated_legacy_write():
        await started.wait()
        await db.set_setting("last_seen", "now")  # execute + commit()

    results = await asyncio.gather(
        update_with_duplicate_tags(), unrelated_legacy_write(), return_exceptions=True
    )
    assert isinstance(results[0], sqlite3.IntegrityError)
    assert results[1] is None
    assert _on_disk(
        db, "SELECT tag FROM area_tags WHERE area_id='a1' ORDER BY tag"
    ) == [
        ("t1",),
        ("t2",),
    ]
    assert _on_disk(db, "SELECT description FROM areas WHERE id='a1'") == [("orig",)]
    assert _on_disk(db, "SELECT value FROM settings WHERE key='last_seen'") == [
        ("now",)
    ]


async def test_import_survives_foreign_commit_and_rollback(db):
    """A long import in one transaction; meanwhile other
    coroutines commit, roll back and hit a constraint error."""
    await _area(db, "c1", "Conflict")

    async def import_areas():
        async with db.transaction():
            for i in range(50):
                async with db.transaction():  # per-area savepoint
                    await _insert_area(db, f"imp{i}", f"Import {i:02d}")
                await asyncio.sleep(0)

    async def disturber():
        for _ in range(20):
            try:
                await db.execute(
                    "INSERT INTO areas (id, name, created_at, updated_at) "
                    "VALUES ('x', 'Conflict', 'n', 'n')"
                )
            except sqlite3.IntegrityError:
                await db.rollback()  # what create_area did on 409
            await db.set_setting("ping", "1")
            await asyncio.sleep(0)

    await asyncio.gather(import_areas(), disturber())
    assert _on_disk(db, "SELECT count(*) FROM areas WHERE id LIKE 'imp%'") == [(50,)]
    assert _on_disk(db, "SELECT value FROM settings WHERE key='ping'") == [("1",)]


async def _insert_area(db, area_id, name):
    now = db.now()
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (area_id, name, now, now),
    )


async def test_commit_inside_transaction_is_deferred(db):
    with pytest.raises(RuntimeError, match="abort"):
        async with db.transaction():
            await db.set_setting("deferred", "1")  # calls commit() internally
            assert _on_disk(db, "SELECT * FROM settings WHERE key='deferred'") == []
            raise RuntimeError("abort")
    assert await db.get_setting("deferred") is None


async def test_rollback_inside_transaction_is_an_error(db):
    with pytest.raises(RuntimeError, match="raise an exception instead"):
        async with db.transaction():
            await db.rollback()


async def test_pending_legacy_write_is_flushed_before_begin(db):
    """A call site between execute() and commit(): without the flush BEGIN
    IMMEDIATE would fail with "cannot start a transaction within a transaction"."""
    await db.execute("INSERT INTO settings (key, value) VALUES ('legacy', '1')")
    assert db.connection.in_transaction
    async with db.transaction():
        await db.set_setting("tx", "1")
    assert _on_disk(
        db, "SELECT key FROM settings WHERE key IN ('legacy','tx') ORDER BY key"
    ) == [
        ("legacy",),
        ("tx",),
    ]


async def test_reads_do_not_wait_for_a_transaction(db):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def long_tx():
        async with db.transaction():
            await db.set_setting("slow", "1")
            entered.set()
            await release.wait()

    task = asyncio.create_task(long_tx())
    await entered.wait()
    row = await asyncio.wait_for(db.fetchone("SELECT 1 AS one"), 1)
    assert row == {"one": 1}
    # ... while a foreign write waits for the end of the transaction.
    write = asyncio.create_task(db.set_setting("other", "1"))
    await asyncio.sleep(0.05)
    assert not write.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(task, write), 2)
    assert await db.get_setting("other") == "1"


async def test_cancelled_transaction_rolls_back_and_releases_the_lock(db):
    entered = asyncio.Event()

    async def tx():
        async with db.transaction():
            await db.set_setting("cancelled", "1")
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(tx())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not db.in_transaction
    assert await db.get_setting("cancelled") is None
    await asyncio.wait_for(db.set_setting("after", "1"), 1)


async def test_concurrent_transactions_are_serialised(db):
    await db.set_setting("counter", "0")

    async def increment():
        async with db.transaction():
            value = int(await db.get_setting("counter"))
            await asyncio.sleep(0.01)
            await db.set_setting("counter", str(value + 1))

    await asyncio.gather(*(increment() for _ in range(10)))
    assert await db.get_setting("counter") == "10"


async def test_task_spawned_in_transaction_is_foreign_after_it_ended(db):
    """A fire-and-forget task inherits the context; once the transaction is
    over its commit() must be real again, not a silent no-op."""
    go = asyncio.Event()

    async def background():
        await go.wait()
        await db.set_setting("bg", "1")

    async with db.transaction():
        task = asyncio.create_task(background())
    go.set()
    await task
    assert _on_disk(db, "SELECT value FROM settings WHERE key='bg'") == [("1",)]


def test_write_classification():
    from models.database import _is_write

    assert not _is_write("SELECT * FROM areas")
    assert not _is_write("  -- comment\n select 1")
    assert not _is_write("PRAGMA integrity_check")
    assert _is_write("PRAGMA user_version = 4")
    assert _is_write("INSERT INTO x VALUES (1)")
    assert _is_write("WITH a AS (SELECT 1) DELETE FROM x")
    assert not _is_write("WITH a AS (SELECT 1) SELECT * FROM a")
    assert _is_write("SAVEPOINT s1")
