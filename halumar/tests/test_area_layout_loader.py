"""Area layout as served to apps: sections with entities, live state and widgets.

Characterizes the WS get_area reply and the REST area/sections endpoints so
the shared section loader (services/layout.py) serves both unchanged: section
and item order, live state per entity, parsed widget config, counts.
"""

import json
import uuid
from types import SimpleNamespace

import api.areas as areas
from api.websocket import _handle_message
from models.database import Database


class _FakeHA:
    def __init__(self):
        self.calls = []

    async def get_state(self, entity_id):
        self.calls.append(entity_id)
        if entity_id == "sensor.broken":
            raise RuntimeError("HA unavailable")
        if entity_id == "light.missing":
            return None
        return {
            "state": f"state-of-{entity_id}",
            "attributes": {"friendly_name": entity_id},
            "last_changed": "2026-11-18T10:00:00+00:00",
            "last_updated": "2026-11-18T10:00:01+00:00",
        }


class _FakeWS:
    def __init__(self, db, ha):
        self.app = SimpleNamespace(
            state=SimpleNamespace(db=db, ha_client=ha, app_url="http://test")
        )
        self.url = SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _request(db, ha):
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="ingress"),
        app=SimpleNamespace(state=SimpleNamespace(db=db, ha_client=ha)),
    )


async def _seed(db):
    """Area with two sections (stored out of order), entities and widgets."""
    now = Database.now()
    area_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO areas (id, name, icon, sort_order, is_active, created_at, "
        "updated_at) VALUES (?, 'Layout', 'mdi:sofa', 0, 1, ?, ?)",
        (area_id, now, now),
    )
    second, first = str(uuid.uuid4()), str(uuid.uuid4())
    for sid, title, order in ((second, "Second", 1), (first, "First", 0)):
        await db.execute(
            "INSERT INTO area_sections (id, area_id, title, sort_order, "
            "column_span, columns) VALUES (?, ?, ?, ?, 1, 2)",
            (sid, area_id, title, order),
        )
    entities = [
        (first, "light.b", 1),
        (first, "light.a", 0),
        (first, "light.missing", 2),
        (second, "sensor.broken", 0),
    ]
    for sid, entity_id, order in entities:
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, "
            "sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, entity_id, sid, order, now),
        )
    widgets = [
        (second, "clock", {"format": "24h"}, 1),
        (second, "label", {"text": "Hi", "size": 2}, 0),
    ]
    for sid, wtype, config, order in widgets:
        await db.execute(
            "INSERT INTO area_widgets (id, area_id, section_id, widget_type, "
            "config, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, sid, wtype, json.dumps(config), order, now),
        )
    await db.commit()
    return area_id


async def test_ws_get_area_layout(db):
    area_id = await _seed(db)
    ha = _FakeHA()
    ws = _FakeWS(db, ha)

    await _handle_message(ws, {"type": "get_area", "area_id": area_id}, True, "ingress")

    reply = ws.sent[-1]
    assert reply["type"] == "area"
    assert reply["image_base_url"] == "http://test"
    data = reply["data"]
    assert data["entity_count"] == 4
    assert data["section_count"] == 2
    assert data["icon_svg"]
    assert [s["title"] for s in data["sections"]] == ["First", "Second"]

    first, second = data["sections"]
    assert [e["entity_id"] for e in first["entities"]] == [
        "light.a",
        "light.b",
        "light.missing",
    ]
    assert first["entities"][0]["state"] == {
        "state": "state-of-light.a",
        "attributes": {"friendly_name": "light.a"},
        "last_changed": "2026-11-18T10:00:00+00:00",
    }
    assert first["entities"][2]["state"] is None  # HA has no such entity
    assert second["entities"][0]["state"] is None  # HA error is swallowed
    assert first["widgets"] == []
    assert [w["widget_type"] for w in second["widgets"]] == ["label", "clock"]
    assert second["widgets"][0]["config"] == {"text": "Hi", "size": 2}

    # Entity rows keep all their columns; state is added last.
    assert list(first["entities"][0])[-1] == "state"
    assert {"id", "area_id", "section_id", "sort_order", "control_mode"} <= set(
        first["entities"][0]
    )
    # Sections carry their own columns plus entities and widgets, in that order.
    assert list(first)[-2:] == ["entities", "widgets"]
    # Live state is fetched once per entity, in layout order.
    assert ha.calls == ["light.a", "light.b", "light.missing", "sensor.broken"]


async def test_rest_area_detail_and_sections_match_ws(db):
    area_id = await _seed(db)
    ws = _FakeWS(db, _FakeHA())
    await _handle_message(ws, {"type": "get_area", "area_id": area_id}, True, "ingress")
    ws_sections = ws.sent[-1]["data"]["sections"]

    detail = await areas.get_area(_request(db, _FakeHA()), area_id)
    assert detail.entity_count == 4
    assert detail.section_count == 2
    assert [s.id for s in detail.sections] == [s["id"] for s in ws_sections]
    for rest_sec, ws_sec in zip(detail.sections, ws_sections):
        assert [e.entity_id for e in rest_sec.entities] == [
            e["entity_id"] for e in ws_sec["entities"]
        ]
        assert [e.state for e in rest_sec.entities] == [
            e["state"] for e in ws_sec["entities"]
        ]
        assert [w.config for w in rest_sec.widgets] == [
            w["config"] for w in ws_sec["widgets"]
        ]

    listed = await areas.list_sections(_request(db, None), area_id)
    assert [s.id for s in listed] == [s["id"] for s in ws_sections]
    assert [[e.entity_id for e in s.entities] for s in listed] == [
        [e["entity_id"] for e in s["entities"]] for s in ws_sections
    ]
    assert listed[1].widgets[1].config == {"format": "24h"}
