"""Area, import and image handlers on ``db.transaction()``.

A long import stays intact while a parallel ``create_area`` (409) or a plain
``PUT /areas`` (commit) runs on the shared connection. The upload handler
removes the old image only after the new path is committed. The handlers are
called directly with a fake admin request; broadcasts and HA sensor syncs are
stubbed.
"""

import asyncio
import io
import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

import api.areas as areas
import api.export as export
import api.sync as sync
from api.export import import_areas
from models.schemas import AreaCreate, AreaUpdate, ImportRequest


async def _noop(*_a, **_k):
    return None


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(areas.api_ws, "broadcast", _noop)
    monkeypatch.setattr(areas, "_sync_sensors", _noop)
    monkeypatch.setattr(export, "sync_all_sensors", _noop)
    monkeypatch.setattr(sync, "sync_all_sensors", _noop)
    monkeypatch.setattr(areas, "IMAGE_DIR", str(tmp_path / "images"))


def _request(db, ha_client=None):
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="admin"),
        app=SimpleNamespace(state=SimpleNamespace(db=db, ha_client=ha_client)),
    )


def _on_disk(db, sql, params=()):
    """Committed state, read through a separate connection."""
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _payload(prefix, n_areas, n_entities, **extra):
    return ImportRequest(
        areas=[
            {
                "name": f"{prefix} {i:03d}",
                "tags": ["imp"],
                "sections": [
                    {
                        "title": "s",
                        "entities": [
                            {"entity_id": f"light.{prefix.lower()}_{i}_{j}"}
                            for j in range(n_entities)
                        ],
                    }
                ],
            }
            for i in range(n_areas)
        ],
        **extra,
    )


# ── Import ───────────────────────────────────────────────────────────────────


async def test_import_reports_invalid_entries_and_keeps_the_rest(db):
    data = ImportRequest(
        areas=[
            {"name": "Good"},
            {"name": "Bad color", "color": "red"},
            {"name": "Bad sections", "sections": "nope"},
            {"name": "Bad entity", "sections": [{"entities": ["light.x"]}]},
            {"name": "Bad widget", "sections": [{"widgets": [{"widget_type": {}}]}]},
        ]
    )
    result = await import_areas(_request(db), data)
    assert result["imported"] == 1
    assert result["failed"] == 4
    assert len(result["errors"]) == 4
    assert _on_disk(db, "SELECT name FROM areas") == [("Good",)]


async def test_import_with_only_invalid_entries_is_422(db):
    data = ImportRequest(areas=[{"name": ""}, {"name": "X", "tags": 5}])
    with pytest.raises(HTTPException) as exc:
        await import_areas(_request(db), data)
    assert exc.value.status_code == 422
    assert "Nothing imported" in exc.value.detail
    assert _on_disk(db, "SELECT count(*) FROM areas") == [(0,)]


async def test_import_database_error_rolls_back_everything(db, monkeypatch):
    """A database failure in area 3 must not leave areas 1-2 behind (503)."""
    real = db.executemany
    calls = {"n": 0}

    async def failing_executemany(sql, params):
        if "area_entities" in sql:
            calls["n"] += 1
            if calls["n"] == 3:
                raise sqlite3.OperationalError("disk I/O error")
        return await real(sql, params)

    monkeypatch.setattr(db, "executemany", failing_executemany)
    with pytest.raises(HTTPException) as exc:
        await import_areas(_request(db), _payload("Err", 5, 3))
    assert exc.value.status_code == 503
    monkeypatch.setattr(db, "executemany", real)
    assert _on_disk(db, "SELECT count(*) FROM areas") == [(0,)]
    assert _on_disk(db, "SELECT count(*) FROM area_entities") == [(0,)]
    # The connection is usable afterwards
    await areas.create_area(_request(db), AreaCreate(name="After"))
    assert _on_disk(db, "SELECT name FROM areas") == [("After",)]


async def test_import_skips_duplicate_entities_and_overwrites_atomically(db):
    first = ImportRequest(
        areas=[
            {
                "name": "Dup",
                "tags": ["a", "a"],
                "sections": [
                    {"entities": [{"entity_id": "light.a"}, {"entity_id": "light.a"}]},
                    {"entities": [{"entity_id": "light.a"}, {"entity_id": "light.b"}]},
                ],
            }
        ]
    )
    result = await import_areas(_request(db), first)
    assert result["imported"] == 1 and result["errors"] == []
    assert _on_disk(db, "SELECT entity_id FROM area_entities ORDER BY 1") == [
        ("light.a",),
        ("light.b",),
    ]

    # overwrite with an invalid entry: the old content stays complete
    bad = ImportRequest(
        areas=[{"name": "Dup", "sections": [{"entities": [1]}]}], overwrite=True
    )
    with pytest.raises(HTTPException):
        await import_areas(_request(db), bad)
    assert _on_disk(db, "SELECT count(*) FROM area_entities") == [(2,)]

    good = ImportRequest(
        areas=[{"name": "Dup", "sections": [{"entities": [{"entity_id": "x.y"}]}]}],
        overwrite=True,
    )
    result = await import_areas(_request(db), good)
    assert result["imported"] == 1
    assert _on_disk(db, "SELECT entity_id FROM area_entities") == [("x.y",)]
    assert _on_disk(db, "SELECT count(*) FROM area_tags") == [(0,)]


async def test_import_parallel_to_create_conflicts_and_updates(db):
    """Import || POST name conflict || PUT other area."""
    req = _request(db)
    await areas.create_area(req, AreaCreate(name="Conflict"))
    other = await areas.create_area(req, AreaCreate(name="Other"))
    done = asyncio.Event()
    codes: dict[int, int] = {}
    updates = {"n": 0}

    async def run_import():
        try:
            return await import_areas(
                req, _payload("Imp", 40, 50)
            )  # 50 = MAX_ITEMS_PER_SECTION
        finally:
            done.set()

    async def conflicts():
        while not done.is_set():
            try:
                await areas.create_area(req, AreaCreate(name="Conflict"))
                code = 201
            except HTTPException as e:
                code = e.status_code
            codes[code] = codes.get(code, 0) + 1
            await asyncio.sleep(0)

    async def puts():
        while not done.is_set():
            updates["n"] += 1
            await areas.update_area(
                req, other.id, AreaUpdate(description=f"upd {updates['n']}")
            )
            await asyncio.sleep(0)

    result, _, _ = await asyncio.gather(run_import(), conflicts(), puts())
    assert result["imported"] == 40 and result["failed"] == 0
    assert set(codes) == {409}
    rows = _on_disk(
        db,
        "SELECT a.name, (SELECT count(*) FROM area_entities e WHERE e.area_id=a.id),"
        " (SELECT count(*) FROM area_sections s WHERE s.area_id=a.id)"
        " FROM areas a WHERE a.name LIKE 'Imp %'",
    )
    assert len(rows) == 40
    assert all(ents == 50 and secs == 1 for _, ents, secs in rows)
    assert _on_disk(db, "SELECT description FROM areas WHERE id=?", (other.id,)) == [
        (f"upd {updates['n']}",)
    ]


# ── Area handlers ────────────────────────────────────────────────────────────


async def test_update_area_name_conflict_is_409_and_changes_nothing(db):
    req = _request(db)
    await areas.create_area(req, AreaCreate(name="Taken"))
    a = await areas.create_area(req, AreaCreate(name="Mine", tags=["t1"]))
    with pytest.raises(HTTPException) as exc:
        await areas.update_area(req, a.id, AreaUpdate(name="Taken", tags=["t2"]))
    assert exc.value.status_code == 409
    assert _on_disk(db, "SELECT tag FROM area_tags WHERE area_id=?", (a.id,)) == [
        ("t1",)
    ]
    with pytest.raises(HTTPException) as exc:
        await areas.update_area(req, "missing", AreaUpdate(description="x"))
    assert exc.value.status_code == 404


async def test_create_area_conflict_does_not_touch_a_running_transaction(db):
    """A 409 from create_area does not roll back the shared connection."""
    req = _request(db)
    await areas.create_area(req, AreaCreate(name="Conflict"))
    inside = asyncio.Event()

    async def unit_of_work():
        async with db.transaction():
            await db.set_setting("uow", "1")
            inside.set()
            await asyncio.sleep(0.05)

    async def conflict():
        await inside.wait()
        with pytest.raises(HTTPException):
            await areas.create_area(req, AreaCreate(name="Conflict"))

    await asyncio.gather(unit_of_work(), conflict())
    assert _on_disk(db, "SELECT value FROM settings WHERE key='uow'") == [("1",)]


# ── Images ──────────────────────────────────────────────────────────


def _upload(content: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename="x.png",
        headers=Headers({"content-type": "image/png"}),
    )


async def _card_path(db, area_id):
    row = await db.fetchone("SELECT card_image_path FROM areas WHERE id=?", (area_id,))
    return row["card_image_path"]


async def test_upload_keeps_old_image_until_new_path_is_committed(db, monkeypatch):
    req = _request(db)
    a = await areas.create_area(req, AreaCreate(name="Img"))
    await areas.upload_card_image(req, a.id, _upload(b"old"))
    old = await _card_path(db, a.id)
    assert open(old, "rb").read() == b"old"

    seen = {}
    real_commit = db.connection.commit

    async def observing_commit():
        # At commit time the old file must still exist and the new one too
        files = sorted(os.listdir(areas.IMAGE_DIR))
        seen["files"] = files
        seen["old_exists"] = os.path.exists(old)
        await real_commit()

    with monkeypatch.context() as m:
        m.setattr(db.connection, "commit", observing_commit)
        await areas.upload_card_image(req, a.id, _upload(b"new"))

    new = await _card_path(db, a.id)
    assert seen["old_exists"] and len(seen["files"]) == 2
    assert open(new, "rb").read() == b"new"
    assert not os.path.exists(old)
    assert os.listdir(areas.IMAGE_DIR) == [os.path.basename(new)]


async def test_upload_failing_commit_keeps_old_image(db, monkeypatch):
    req = _request(db)
    a = await areas.create_area(req, AreaCreate(name="Img"))
    await areas.upload_card_image(req, a.id, _upload(b"old"))
    old = await _card_path(db, a.id)

    async def broken_commit():
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as m, pytest.raises(sqlite3.OperationalError):
        m.setattr(db.connection, "commit", broken_commit)
        await areas.upload_card_image(req, a.id, _upload(b"new"))
    assert await _card_path(db, a.id) == old
    assert os.listdir(areas.IMAGE_DIR) == [os.path.basename(old)]


async def test_parallel_uploads_leave_exactly_the_referenced_file(db):
    req = _request(db)
    a = await areas.create_area(req, AreaCreate(name="Img"))
    await asyncio.gather(
        *(
            areas.upload_card_image(req, a.id, _upload(bytes([i]) * 10))
            for i in range(5)
        )
    )
    current = await _card_path(db, a.id)
    assert os.listdir(areas.IMAGE_DIR) == [os.path.basename(current)]


async def test_delete_area_removes_images_after_commit(db):
    req = _request(db)
    a = await areas.create_area(req, AreaCreate(name="Img"))
    await areas.upload_card_image(req, a.id, _upload(b"img"))
    await areas.upload_bg_dark(req, a.id, _upload(b"dark"))
    assert len(os.listdir(areas.IMAGE_DIR)) == 2
    await areas.delete_area(req, a.id)
    assert os.listdir(areas.IMAGE_DIR) == []
    assert _on_disk(db, "SELECT count(*) FROM areas") == [(0,)]


# ── Sync from HA ─────────────────────────────────────────────────────────────


class _FakeHA:
    def __init__(self, ha_areas, entities=(), devices=()):
        self._areas = list(ha_areas)
        self._entities = list(entities)
        self._devices = list(devices)

    async def get_areas(self):
        return self._areas

    async def get_entity_registry(self):
        return self._entities

    async def get_devices(self):
        return self._devices


async def test_sync_from_ha_name_conflict_is_reported_not_500(db):
    req0 = _request(db)
    await areas.create_area(req0, AreaCreate(name="Küche"))
    ha = _FakeHA(
        [
            {"area_id": "kitchen", "name": "Küche"},
            {"area_id": "office", "name": "Büro"},
        ],
        entities=[
            {"entity_id": "light.desk", "area_id": "office"},
            {"entity_id": "light.lamp", "device_id": "d1"},
            {"entity_id": "light.off", "area_id": "office", "disabled_by": "user"},
        ],
        devices=[{"id": "d1", "area_id": "office"}],
    )
    body = sync.SyncFromHARequest(include_entities=True)
    result = await sync.sync_from_ha(_request(db, ha), body)
    assert result.created == 1 and len(result.errors) == 1
    assert result.entities_added == 2
    assert sorted(
        r[0] for r in _on_disk(db, "SELECT entity_id FROM area_entities")
    ) == ["light.desk", "light.lamp"]


async def test_sync_from_ha_section_has_no_default_heading(db):
    """The section of a synced area is untitled like POST /areas;
    the apps showed "Default" as a heading."""
    ha = _FakeHA(
        [{"area_id": "office", "name": "Büro"}],
        entities=[{"entity_id": "light.desk", "area_id": "office"}],
    )
    body = sync.SyncFromHARequest(include_entities=True)
    await sync.sync_from_ha(_request(db, ha), body)
    assert _on_disk(db, "SELECT title FROM area_sections") == [("",)]


# ── Lint ────────────────────────────────────────────────────────────


def test_no_new_transaction_call_sites_outside_the_allowlist():
    """Only the files of the allowlist may still call commit() etc."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
    import check_db_commits as lint

    findings = lint.scan(lint.DEFAULT_ROOT)
    allow = lint.load_allowlist(lint.DEFAULT_ALLOWLIST)
    too_many = [p for p in lint.check(findings, allow) if "allowed" in p]
    assert too_many == []
