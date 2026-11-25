"""Layout broadcasts reach only the sessions that may read the area.

``entity_added``, ``entity_removed``, ``section_*``, ``widget_*``,
``layout_updated`` and ``area_updated`` are area-scoped like
``entity_updated``: they go to admin sessions, the integration and the
devices that may read the area, never to other paired devices.
"""

import importlib
import types
import json
import uuid
from types import SimpleNamespace

import pytest

import api.areas as areas
import api.integration as integration
import api.room_climate as room_climate
from api.ws import registry
from models.database import Database
from models.schemas import (
    AreaEntityCreate,
    AreaUpdate,
    AreaWidgetCreate,
    AreaWidgetUpdate,
    SectionCreate,
    SectionLayoutUpdate,
    SectionOrderUpdate,
    SectionUpdate,
)

from .profile_helpers import add_client

bc = importlib.import_module("api.ws.broadcast")


class _Sock:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class _HA:
    async def get_state(self, _entity_id):
        return None


def _request(db):
    state = SimpleNamespace(db=db, ha_client=_HA(), auth_type="ingress")
    return SimpleNamespace(
        state=state,
        app=SimpleNamespace(state=state),
        client=SimpleNamespace(host="127.0.0.1"),
    )


async def _noop(*_a, **_k):
    return None


@pytest.fixture
async def setup(db, monkeypatch):
    monkeypatch.setattr(areas, "_sync_sensors", _noop)
    area_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, 'Office', 0, 1, ?, ?)",
        (area_id, now, now),
    )
    await db.execute(
        "INSERT INTO area_tags (area_id, tag) VALUES (?, 'office')", (area_id,)
    )
    await db.commit()
    reader = await add_client(db, name="Reader", last_seen=now, tags=["office"])
    outsider = await add_client(db, name="Outsider", last_seen=now, tags=["kitchen"])
    socks = {
        cid: _Sock()
        for cid in (reader, outsider, "ingress", registry.INTEGRATION_CLIENT_ID)
    }
    monkeypatch.setattr(
        bc,
        "_clients",
        {s: {"authenticated": True, "client_id": cid} for cid, s in socks.items()},
    )
    return SimpleNamespace(
        db=db,
        req=_request(db),
        area_id=area_id,
        reader=reader,
        outsider=outsider,
        socks=socks,
    )


def _types(sock):
    return [m["type"] for m in sock.sent]


async def test_layout_edits_skip_devices_without_access(setup):
    s, req, a = setup, setup.req, setup.area_id
    section = await areas.create_section(req, a, SectionCreate(title="Desk"))
    await areas.update_section(req, a, section.id, SectionUpdate(title="Desk 2"))
    await areas.add_entity_to_area(
        req, a, AreaEntityCreate(entity_id="light.desk", section_id=section.id)
    )
    widget = await areas.create_widget(
        req, a, AreaWidgetCreate(section_id=section.id, widget_type="clock")
    )
    await areas.update_widget(req, a, widget.id, AreaWidgetUpdate(rows=2))
    await areas.update_layout(
        req,
        a,
        SectionLayoutUpdate(
            items=[
                {"entity_id": "light.desk", "section_id": section.id, "sort_order": 1}
            ]
        ),
    )
    await areas.reorder_sections(req, a, SectionOrderUpdate(section_ids=[section.id]))
    await areas.delete_widget(req, a, widget.id)
    await areas.remove_entity_from_area(req, a, "light.desk")
    await areas.delete_section(req, a, section.id)
    await areas.update_area(req, a, AreaUpdate(icon="mdi:desk"))

    expected = [
        "section_created",
        "section_updated",
        "entity_added",
        "widget_created",
        "widget_updated",
        "layout_updated",
        "layout_updated",
        "widget_deleted",
        "entity_removed",
        "section_deleted",
        "area_updated",
    ]
    assert _types(s.socks[s.reader]) == expected
    assert _types(s.socks["ingress"]) == expected
    assert _types(s.socks[registry.INTEGRATION_CLIENT_ID]) == expected
    assert s.socks[s.outsider].sent == []


async def test_visibility_change_tells_every_app_without_payload(setup):
    s = setup
    await areas.update_area(s.req, s.area_id, AreaUpdate(is_active=False))
    # Nobody but admin and integration may read an inactive area; the apps
    # that listed it learn from areas_changed (no area id in it).
    assert _types(s.socks["ingress"]) == ["area_updated"]
    assert s.socks[s.reader].sent == [{"type": "areas_changed", "data": {}}]
    assert s.socks[s.outsider].sent == [{"type": "areas_changed", "data": {}}]


async def test_unchanged_visibility_sends_no_areas_changed(setup):
    s = setup
    await areas.update_area(
        s.req, s.area_id, AreaUpdate(is_active=True, tags=["office"])
    )
    assert _types(s.socks[s.reader]) == ["area_updated"]
    assert s.socks[s.outsider].sent == []


@pytest.mark.parametrize("action", ["activate", "deactivate"])
async def test_integration_switch_is_area_scoped(setup, monkeypatch, action):
    s = setup
    req = _request(s.db)
    req.state.auth_type = "integration"
    monkeypatch.setattr(integration, "require_integration", lambda _r: None)
    if action == "activate":
        await s.db.execute("UPDATE areas SET is_active = 0")
        await s.db.commit()
        await integration.integration_activate_area(req, s.area_id)
        assert _types(s.socks[s.reader]) == ["area_updated", "areas_changed"]
    else:
        await integration.integration_deactivate_area(req, s.area_id)
        assert _types(s.socks[s.reader]) == ["areas_changed"]
    assert s.socks[s.outsider].sent == []


async def test_room_climate_update_is_area_scoped(setup, monkeypatch):
    s = setup
    service = room_climate.room_climate

    async def _settings(_db, _ha, _area):
        option = {"options": [{"entity_id": "sensor.desk_temp"}]}
        return {"temperature": option, "humidity": {"options": []}}

    monkeypatch.setattr(service, "settings", _settings)
    await room_climate.put_room_climate(
        s.req,
        s.area_id,
        room_climate.RoomClimateBody(
            temperature_entity_id="sensor.desk_temp", humidity_entity_id=None
        ),
    )
    assert _types(s.socks[s.reader]) == ["area_updated"]
    assert _types(s.socks["ingress"]) == ["area_updated"]
    assert s.socks[s.outsider].sent == []


@pytest.mark.parametrize("state", ["off", "on"])
async def test_area_switch_from_home_assistant_is_area_scoped(
    setup, monkeypatch, state
):
    """The HA switch path behaves like the integration switch: area_updated
    to the sessions that may read the area, the data-less areas_changed to the apps whose
    list holds it; a device outside the area's tags gets neither."""
    import namespace
    from services import ha_sensors

    s = setup
    if state == "on":
        await s.db.execute("UPDATE areas SET is_active = 0")
        await s.db.commit()
    monkeypatch.setattr(ha_sensors, "sync_config_sensor", _noop)
    monkeypatch.setattr(ha_sensors, "sync_area_sensor", _noop)
    entity_id = namespace.area_switch_id(ha_sensors._area_slug("Office", s.area_id))
    changed = await ha_sensors.handle_area_switch_state(
        s.db,
        types.SimpleNamespace(),
        {
            "entity_id": entity_id,
            "state": state,
            "attributes": {namespace.AREA_SWITCH_ID_ATTRIBUTE: s.area_id},
        },
    )
    assert changed
    assert s.socks[s.outsider].sent == []
    reader = _types(s.socks[s.reader])
    assert reader == (
        ["area_updated", "areas_changed"] if state == "on" else ["areas_changed"]
    )
    assert _types(s.socks["ingress"]) == ["area_updated"]
