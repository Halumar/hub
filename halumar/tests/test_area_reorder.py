"""Batch reorder of the areas (PUT /api/areas/order).

One request stores the whole order; unknown, missing and duplicate IDs are
rejected; only moved areas are announced; the config sensor syncs once.
"""

import uuid
from types import SimpleNamespace

import fastapi
import pydantic
import pytest

import api.areas as areas_api
import api.ws as api_ws
from models.database import Database
from models.schemas import AreaOrderUpdate


@pytest.fixture
def out(monkeypatch):
    sent: list = []

    async def fake_broadcast(message, *_a, **_k):
        sent.append(message)

    async def fake_sync(db, ha_client):
        sent.append("config_sensor")

    async def fake_scoped(db, area_id, message):
        sent.append(message)
        return 1

    monkeypatch.setattr(api_ws, "broadcast", fake_broadcast)
    monkeypatch.setattr(api_ws, "broadcast_area_scoped", fake_scoped)
    monkeypatch.setattr(areas_api, "sync_config_sensor", fake_sync)
    return sent


def _request(db, auth="ingress"):
    state = SimpleNamespace(db=db, ha_client=None, auth_type=auth)
    return SimpleNamespace(state=state, app=SimpleNamespace(state=state))


async def _areas(db, n=3):
    ids = []
    now = Database.now()
    async with db.transaction():
        for i in range(n):
            area_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO areas (id, name, sort_order, is_active, created_at, "
                "updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (area_id, f"Area {i}", i, now, now),
            )
            ids.append(area_id)
    return ids


async def _order(db):
    rows = await db.fetchall("SELECT id FROM areas ORDER BY sort_order")
    return [r["id"] for r in rows]


async def test_whole_order_in_one_step(db, out):
    a, b, c = await _areas(db)
    result = await areas_api.reorder_areas(
        _request(db), AreaOrderUpdate(area_ids=[c, a, b])
    )
    assert result.model_dump() == {"status": "ok", "moved": 3}
    assert await _order(db) == [c, a, b]
    assert out[:3] == [
        {"type": "area_updated", "data": {"area_id": x}} for x in (c, a, b)
    ]
    assert out.count("config_sensor") == 1


async def test_only_moved_areas_are_announced(db, out):
    a, b, c = await _areas(db)
    result = await areas_api.reorder_areas(
        _request(db), AreaOrderUpdate(area_ids=[a, c, b])
    )
    assert result.moved == 2
    assert [m["data"]["area_id"] for m in out if isinstance(m, dict)] == [c, b]


async def test_same_order_changes_nothing(db, out):
    ids = await _areas(db)
    result = await areas_api.reorder_areas(_request(db), AreaOrderUpdate(area_ids=ids))
    assert result.moved == 0
    assert out == []


@pytest.mark.parametrize("case", ["missing", "unknown", "duplicate"])
async def test_incomplete_lists_are_rejected(db, out, case):
    a, b, c = await _areas(db)
    ids = {"missing": [b, a], "unknown": [c, b, a, "nope"], "duplicate": [c, c, a, b]}[
        case
    ]
    with pytest.raises(fastapi.HTTPException) as e:
        await areas_api.reorder_areas(_request(db), AreaOrderUpdate(area_ids=ids))
    assert e.value.status_code == 422
    assert await _order(db) == [a, b, c]
    assert out == []


def test_body_is_strict():
    with pytest.raises(pydantic.ValidationError):
        AreaOrderUpdate(area_ids=[])
    with pytest.raises(pydantic.ValidationError):
        AreaOrderUpdate(area_ids=["a"], sort="x")


async def test_clients_are_rejected(db, out):
    with pytest.raises(fastapi.HTTPException) as e:
        await areas_api.reorder_areas(
            _request(db, "client"), AreaOrderUpdate(area_ids=["a"])
        )
    assert e.value.status_code == 403


def test_route_is_registered():
    # Resolution before PUT /areas/{area_id} is covered by test_route_resolution.
    from main import app

    assert "put" in app.openapi()["paths"]["/api/areas/order"]
