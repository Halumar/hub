"""A section over the item bound (for example from an HA sync) stays usable.

The layout batch of the admin UI sends every item of the area. Only a section
that grows above MAX_ITEMS_PER_SECTION is refused; keeping or shrinking it,
reordering another section or dragging items out is fine.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.areas as areas
from api import ws as api_ws
from models.database import Database
from models.schemas import AreaEntityUpdate, AreaWidgetCreate, SectionLayoutUpdate


async def _noop(*_a, **_k):
    return None


@pytest.fixture
async def full(db, monkeypatch):
    monkeypatch.setattr(areas, "_sync_sensors", _noop)
    monkeypatch.setattr(api_ws, "broadcast_area_scoped", _noop)
    monkeypatch.setattr(api_ws, "broadcast", _noop)
    now = Database.now()
    area_id, big, small = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    await db.execute(
        "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, 'Keller', ?, ?)",
        (area_id, now, now),
    )
    for sid, order in ((big, 0), (small, 1)):
        await db.execute(
            "INSERT INTO area_sections (id, area_id, title, sort_order) VALUES (?, ?, '', ?)",
            (sid, area_id, order),
        )
    items = []
    for i in range(61):
        sid = big if i < 60 else small
        eid = f"light.l{i}"
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, eid, sid, i, now),
        )
        items.append({"entity_id": eid, "section_id": sid, "sort_order": i})
    await db.commit()
    state = SimpleNamespace(db=db, ha_client=None, auth_type="ingress")
    req = SimpleNamespace(
        state=state, app=SimpleNamespace(state=state), client=SimpleNamespace(host="x")
    )
    return SimpleNamespace(
        db=db, req=req, area=area_id, big=big, small=small, items=items
    )


async def _count(db, sid):
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM area_entities WHERE section_id = ?", (sid,)
    )
    return row["n"]


def _layout(items):
    return SectionLayoutUpdate.model_validate({"items": items})


async def test_reorder_with_every_item_is_accepted(full):
    items = [dict(i, sort_order=100 - i["sort_order"]) for i in full.items]
    assert await areas.update_layout(full.req, full.area, _layout(items)) == {
        "status": "ok"
    }


async def test_moving_one_out_of_the_full_section_is_accepted(full):
    items = [dict(i) for i in full.items]
    items[0]["section_id"] = full.small
    await areas.update_layout(full.req, full.area, _layout(items))
    assert await _count(full.db, full.big) == 59


async def test_moving_one_into_the_full_section_is_refused(full):
    items = [dict(i) for i in full.items]
    items[-1]["section_id"] = full.big
    with pytest.raises(HTTPException) as exc:
        await areas.update_layout(full.req, full.area, _layout(items))
    assert exc.value.status_code == 400
    assert await _count(full.db, full.big) == 60  # rolled back


async def test_single_moves_and_widget_create(full):
    await areas.update_area_entity(
        full.req, full.area, "light.l0", AreaEntityUpdate(section_id=full.small)
    )
    with pytest.raises(HTTPException):
        await areas.update_area_entity(
            full.req, full.area, "light.l60", AreaEntityUpdate(section_id=full.big)
        )
    with pytest.raises(HTTPException):
        await areas.create_widget(
            full.req,
            full.area,
            AreaWidgetCreate(section_id=full.big, widget_type="clock"),
        )
    # Staying in the section (other field changed) is no growth.
    await areas.update_area_entity(
        full.req, full.area, "light.l5", AreaEntityUpdate(section_id=full.big, rows=2)
    )
    await areas.remove_entity_from_area(full.req, full.area, "light.l6")
    assert await _count(full.db, full.big) == 58
