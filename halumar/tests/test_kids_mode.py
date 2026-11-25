"""Kids mode: parental controls, client_config, control path, relay.

* Profile with parental controls per device: never guests, on the hub path
  (no key without remote access) and token rotation when a device gets one,
  activity.
* client_config.kids / rules and no HA token with parental controls.
* WS ``control`` and REST ``POST /api/client/control``: visibility, target
  stripping, the call as the device's HA user, rule_blocked with until,
  rate limit/cooldown, volume cap, override and "lift pause".
* ``state_subscribe`` / ``entity_state`` only for visible entities.
"""

import asyncio
import json
import sqlite3
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import api.websocket as ws
import pytest
from api import profiles as profiles_api
from api.auth import AuthMiddleware
from api.pairing import clients as clients_api
from api.ws.client import _get_client_config, _handle_client_message
from fastapi import HTTPException
from models.database import Database
from services import (
    hub_control,
    client_activity,
    crypto,
    kids_mode,
    kids_rules,
    state_relay,
)
from services.profiles import catalog
from services.profiles import rows as profile_rows

from .profile_helpers import add_client, starter_profile
from services import device_provisioning as dp
from starlette.websockets import WebSocketState

UTC = timezone.utc
PAUSE_ALL = {
    "enabled": True,
    "max_actions": 12,
    "max_entities": 4,
    "window_seconds": 60,
    "cooldown_seconds": 300,
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    ws._clients.clear()
    kids_mode.reset()
    state_relay.reset()

    async def _sender(_message):
        return True

    monkeypatch.setattr(dp, "_sender", _sender)
    yield
    ws._clients.clear()
    kids_mode.reset()
    state_relay.reset()
    crypto.reset_cache()
    dp.reset()


@pytest.fixture
def events(monkeypatch):
    log = {"broadcast": [], "to": []}

    async def _broadcast(message, *a, **k):
        log["broadcast"].append(message)

    async def _send_to(client_id, message):
        log["to"].append((client_id, message))
        return True

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    return log


class FakeHA:
    """HA client stand-in: states, time zone, Core info."""

    def __init__(self, states=None, tz="Europe/Zurich"):
        self.states = states or {}
        self.tz = tz
        self.invalidated = []

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def get_config(self):
        return {"time_zone": self.tz}

    async def get_core_info(self):
        return {"port": 80, "ssl": False}

    def invalidate_state_cache(self, entity_id=None):
        self.invalidated.append(entity_id)


@pytest.fixture
def calls(monkeypatch):
    """<ns>.control calls the hub makes as a device: (token, domain, action, data)."""
    log = []

    async def _caller(_ha, token, entity_id, action, data, grant):
        assert grant and len(grant) >= 16
        log.append(
            (
                token,
                entity_id.split(".", 1)[0],
                action,
                {**data, "entity_id": entity_id},
            )
        )
        return {
            "entity_id": entity_id,
            "ok": True,
            "executed": True,
            "reason": None,
            "until": None,
            "adjusted": None,
        }

    monkeypatch.setattr(hub_control, "ha_caller", _caller)
    return log


def _request(db, ha=None, auth="ingress", client_id=None):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth, client_id=client_id),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
        client=None,
    )


class _WS:
    def __init__(self, db, ha=None, client_id=None):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha, app_url="")
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host="192.0.2.20")
        self.sent = []
        self.application_state = WebSocketState.CONNECTED
        ws._clients[self] = {
            "authenticated": True,
            "client_id": client_id,
            "connected_at": time.time(),
            "ip": "192.0.2.20",
        }

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, payload):  # the connection's push queue
        self.sent.append(json.loads(payload))

    def last(self):
        return self.sent[-1] if self.sent else None


async def _client(db, *, token="device-token", secret=None, **columns):
    columns.setdefault("name", "iPad Child One")
    columns.setdefault("remote", False)
    extra = {"ha_token_status": "active" if token else "pending"}
    if token:
        extra["ha_token_encrypted"] = await crypto.encrypt_token(db, token)
    return await add_client(
        db, secret=secret, last_seen=Database.now(), **extra, **columns
    )


async def _area(db, name, entities=(), tags=(), tiles=None):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
            "VALUES (?, ?, 0, 1, ?, ?)",
            (area_id, name, now, now),
        )
        await db.execute(
            "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, "
            "columns) VALUES (?, ?, 'S', 0, 1, 2)",
            (section_id, area_id),
        )
        for i, eid in enumerate(entities):
            tile = (tiles or {}).get(eid)
            await db.execute(
                "INSERT INTO area_entities (id, area_id, section_id, entity_id, "
                "sort_order, tile, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    area_id,
                    section_id,
                    eid,
                    i,
                    json.dumps(tile) if tile else None,
                    now,
                ),
            )
        for tag in tags:
            await db.execute(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
            )
    return area_id


async def _kids_profile(db, name=None, rules=None, areas=None, entities=None):
    """A profile with parental controls. ``rules`` in the kids-rules
    shape of the profile level (None = the catalog defaults), ``areas`` /
    ``entities`` = exceptions (target -> kids rules)."""
    name = name or f"Child One {uuid.uuid4().hex[:4]}"
    body = catalog.materialize("child")
    if rules is not None:
        body["rules"].update(
            {
                "kids_blocked_types": rules.get("blocked_types", []),
                "kids_quiet_hours": rules.get("quiet_hours", []),
                "kids_rate_limit": rules.get("rate_limit"),
                "kids_volume_max": rules.get("volume_max"),
                "kids_color_lock": rules.get("color_lock", False),
                # Off unless the test asks for it.
                "kids_rate_limit_all": rules.get(
                    "rate_limit_all", {**PAUSE_ALL, "enabled": False}
                ),
            }
        )
    body["rules"]["kids_exceptions"] = [
        {"target": {"area_id": a}, "rules": r} for a, r in (areas or {}).items()
    ] + [{"target": {"entity_id": e}, "rules": r} for e, r in (entities or {}).items()]
    async with db.transaction():
        pid = await profile_rows.insert_profile(
            db, name=name, body=body, now=Database.now(), actor=None
        )
    return {"id": pid, "name": name}


async def _child(db, ha, events, rules=None, areas=None, entities=None, **client_cols):
    profile = await _kids_profile(db, rules=rules, areas=areas, entities=entities)
    client_id = await _client(db, profile_id=profile["id"], **client_cols)
    return profile, client_id


async def _control(db, ha, client_id, **msg):
    sock = _WS(db, ha, client_id)
    await _handle_client_message(
        sock, db, {"type": "control", "request_id": "r1", **msg}, "control", client_id
    )
    return sock.last()


async def test_profiles_admin_only(db):
    with pytest.raises(HTTPException) as e:
        await profiles_api.list_profiles(_request(db, auth="client"))
    assert e.value.status_code == 403


# ── device profile ──────────────────────────────────────────────────────────


async def test_assign_child_rotates_the_key_without_remote_access(
    db, events, monkeypatch
):
    """Parental controls on without remote access: no key on the device, the
    token the app knew is rotated (the hub keeps the new one)."""
    sent = []

    async def _sender(message):
        sent.append(message)
        return True

    monkeypatch.setattr(dp, "_sender", _sender)
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    ha = FakeHA()
    p = await _kids_profile(db, name="Child One")
    client_id = await _client(db)
    result = await profiles_api.assign_client_profile(
        _request(db, ha),
        client_id,
        profiles_api.ClientProfile(profile_id=p["id"]),
        dry_run=False,
    )
    assert result.profile_id == p["id"] and result.child_protection is True
    assert result.remote_allowed is False
    types_sent = [(m["type"], (m["data"].get("token_action"))) for m in sent]
    assert ("provision_client", "rotate") in types_sent
    assert await db.fetchone(
        "SELECT 1 FROM token_requests WHERE client_id = ?", (client_id,)
    )
    acts = await client_activity.list_events(db, client_id)
    assert any(
        a["event"] == "profile_changed" and a["detail"]["profile_name"] == "Child One"
        for a in acts
    )
    assert (client_id, {"type": "client_config_updated", "data": {}}) in events["to"]
    # Back to an adult profile: no rotation needed.
    sent.clear()
    adult = await starter_profile(db, "adult")
    await profiles_api.assign_client_profile(
        _request(db, ha),
        client_id,
        profiles_api.ClientProfile(profile_id=adult),
        dry_run=False,
    )
    assert not any(m["type"] == "provision_client" for m in sent)


async def test_assign_child_keeps_remote_access_and_the_key(db, events, monkeypatch):
    """Remote access is a normal rule with parental controls too;
    the device keeps its (now read-only) key for the way away, no rotation."""
    sent = []

    async def _sender(message):
        sent.append(message)
        return True

    monkeypatch.setattr(dp, "_sender", _sender)
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    ha = FakeHA()
    await db.set_setting("ha_url", "https://example.ui.nabu.casa")
    p = await _kids_profile(db, name="Child One")
    client_id = await _client(db, remote=True)
    result = await profiles_api.assign_client_profile(
        _request(db, ha),
        client_id,
        profiles_api.ClientProfile(profile_id=p["id"]),
        dry_run=False,
    )
    assert result.child_protection is True and result.remote_allowed is True
    assert result.has_ha_token is True
    assert not any(m["type"] == "provision_client" for m in sent)
    cfg = await _get_client_config(db, client_id, ha)
    assert cfg["ha_token"] == "device-token"
    assert cfg["ha_url"] == "https://example.ui.nabu.casa"
    assert cfg["kids"]["away"] == "ha" and cfg["kids"]["control"] == "hub"
    assert cfg["ha_enforcement"]["control_path"] == "checked"
    # Secure-child catch-up leaves the remote key alone.
    assert await dp.secure_keyless_tokens(db) == 0


async def test_child_remote_access_adjustment(db, events, monkeypatch):
    """An adjustment gives a child device remote access (key for
    the way away); taking it away rotates the key."""
    sent = []

    async def _sender(message):
        sent.append(message)
        return True

    monkeypatch.setattr(dp, "_sender", _sender)
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    ha = FakeHA()
    _, child = await _child(db, ha, events)
    cfg = await _get_client_config(db, child, ha)
    assert cfg["ha_token"] is None and cfg["kids"]["away"] == "none"
    assert cfg["ha_enforcement"] is None and cfg["ha_instance_id"] is None
    await profiles_api.put_client_override(
        _request(db),
        child,
        "remote_access",
        profiles_api.DeviceOverride(value=True),
        dry_run=False,
    )
    detail = await clients_api.get_client(_request(db), child)
    assert detail.remote_allowed is True and detail.has_ha_token is True
    assert not any(m["type"] == "provision_client" for m in sent)
    assert ("update_client_identity", True) in [
        (m["type"], m["data"].get("remote_allowed")) for m in sent
    ]
    cfg = await _get_client_config(db, child, ha)
    assert cfg["ha_token"] == "device-token" and cfg["kids"]["away"] == "ha"
    assert cfg["ha_enforcement"]["control_path"] == "checked"
    sent.clear()
    await profiles_api.delete_client_override(
        _request(db), child, "remote_access", dry_run=False
    )
    types_sent = [(m["type"], m["data"].get("token_action")) for m in sent]
    assert ("provision_client", "rotate") in types_sent
    cfg = await _get_client_config(db, child, ha)
    assert cfg["ha_token"] is None and cfg["ha_url"] is None
    assert cfg["kids"]["away"] == "none"


async def test_guest_never_gets_parental_controls(db, events):
    p = await _kids_profile(db, name="Child One")
    guest = await _client(db, is_guest=1, expires_at="2099-01-01T00:00:00+00:00")
    with pytest.raises(HTTPException) as e:
        await profiles_api.assign_client_profile(
            _request(db),
            guest,
            profiles_api.ClientProfile(profile_id=p["id"]),
            dry_run=False,
        )
    assert e.value.status_code == 422
    assert e.value.detail["code"] == "profile.guest_child_protection"
    with pytest.raises(HTTPException) as e:
        await profiles_api.assign_client_profile(
            _request(db),
            "missing",
            profiles_api.ClientProfile(profile_id=p["id"]),
            dry_run=False,
        )
    assert e.value.status_code == 404
    member = await _client(db)
    with pytest.raises(HTTPException) as e:
        await profiles_api.assign_client_profile(
            _request(db),
            member,
            profiles_api.ClientProfile(profile_id="nope"),
            dry_run=False,
        )
    assert e.value.status_code == 404
    # The database refuses it too (trigger), whatever the code path.
    with pytest.raises(sqlite3.IntegrityError):
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET profile_id = ? WHERE id = ?",
                (p["id"], guest),
            )


# ── client_config ───────────────────────────────────────────────────────────


async def test_client_config_profiles(db, events):
    ha = FakeHA()
    adult = await _client(db, name="Mama")
    cfg = await _get_client_config(db, adult, ha)
    assert cfg["profile"]["child_protection"] is False
    assert cfg["ha_token"] == "device-token" and "kids" not in cfg
    guest = await _client(db, is_guest=1, expires_at="2099-01-01T00:00:00+00:00")
    cfg = await _get_client_config(db, guest, ha)
    assert cfg["access"]["kind"] == "guest" and "kids" not in cfg
    area_id = await _area(db, "Kinderzimmer")
    profile, child = await _child(db, ha, events, areas={area_id: {"volume_max": 30}})
    cfg = await _get_client_config(db, child, ha)
    assert cfg["ha_token"] is None and cfg["ha_url"] is None
    assert cfg["profile"]["child_protection"] is True
    assert cfg["profile"]["id"] == profile["id"]
    kids = cfg["kids"]
    assert kids["control"] == "hub"
    assert kids["time_zone"] == "Europe/Zurich" and kids["override_until"] is None
    assert kids["rules"]["profile"] == kids_rules.DEFAULT_CHILD_RULES
    assert kids["rules"]["areas"] == {area_id: {"volume_max": 30}}
    assert kids["rules"]["entities"] == {}
    assert kids["away"] == "none"
    rule = cfg["rules"]["remote_access"]
    assert rule["value"] is False and rule["source"] != "boundary"
    # No key without remote access: the boundary of the hub path.
    assert cfg["rules"]["quick_access"]["reason"] == "boundary_hub_only"
    assert cfg["rules"]["kids_volume_max"]["value"] == 40
    assert cfg["rules"]["areas"]["enforcement"] == "hub"
    admin_cfg = await _get_client_config(db, "ingress")
    assert admin_cfg["profile"] is None and admin_cfg["rules"] == {}


# ── control ─────────────────────────────────────────────────────────────────

FREE = {}  # no rules at all


async def test_control_allowed_calls_ha_as_the_device(db, events, calls):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    _, child = await _child(db, ha, events, rules=FREE)
    answer = await _control(
        db,
        ha,
        child,
        entity_id="light.kids",
        service="turn_on",
        data={"brightness_pct": 40, "entity_id": "lock.front_door", "area_id": "x"},
    )
    assert answer == {
        "type": "control_ok",
        "request_id": "r1",
        "entity_id": "light.kids",
    }
    assert calls == [
        (
            "device-token",
            "light",
            "turn_on",
            {"brightness_pct": 40, "entity_id": "light.kids"},
        )
    ]
    assert ha.invalidated == ["light.kids"]


async def test_control_rejects_adults_invisible_and_bad_requests(db, events, calls):
    ha = FakeHA()
    await _area(db, "Wohnzimmer", ["light.living"], tags=["og"])
    adult = await _client(db)
    assert (await _control(db, ha, adult, entity_id="light.living", service="turn_on"))[
        "reason"
    ] == "not_hub_controlled"
    _, child = await _child(db, ha, events, rules=FREE)
    assert (
        await _control(db, ha, child, entity_id="lock.front_door", service="unlock")
    )["reason"] == "not_visible"
    assert (await _control(db, ha, child, entity_id="light.living", service="../x"))[
        "reason"
    ] == "invalid_request"
    assert (
        await _control(
            db, ha, child, entity_id="light.living", service="turn_on", data=[1]
        )
    )["reason"] == "invalid_request"
    big = {"x": "a" * 5000}
    assert (
        await _control(
            db, ha, child, entity_id="light.living", service="turn_on", data=big
        )
    )["reason"] == "invalid_request"
    sock = _WS(db, ha, "ingress")
    await _handle_client_message(
        sock,
        db,
        {"type": "control", "entity_id": "light.living", "service": "turn_on"},
        "control",
        "ingress",
    )
    assert sock.last()["reason"] == "not_hub_controlled"
    assert calls == []


async def test_control_respects_area_visibility_of_the_device(db, events, calls):
    ha = FakeHA()
    await _area(db, "Büro", ["light.office"], tags=["eltern"])
    await _area(db, "Kinderzimmer", ["light.kids"], tags=["kind"])
    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    assert (await _control(db, ha, child, entity_id="light.office", service="turn_on"))[
        "reason"
    ] == "not_visible"
    assert (await _control(db, ha, child, entity_id="light.kids", service="turn_on"))[
        "type"
    ] == "control_ok"


async def test_run_target_of_a_visible_tile_is_allowed(db, events, calls):
    ha = FakeHA()
    tile = {"tap_action": {"action": "run", "target_entity_id": "script.good_night"}}
    await _area(db, "Kinderzimmer", ["light.kids"], tiles={"light.kids": tile})
    _, child = await _child(db, ha, events, rules=FREE)
    assert (
        await _control(db, ha, child, entity_id="script.good_night", service="turn_on")
    )["type"] == "control_ok"


async def test_blocked_type_and_activity_throttle(db, events, calls):
    ha = FakeHA({"lock.front_door": {"state": "locked", "attributes": {}}})
    await _area(db, "Eingang", ["lock.front_door"])
    _, child = await _child(db, ha, events)
    for _ in range(3):
        answer = await _control(
            db, ha, child, entity_id="lock.front_door", service="unlock"
        )
    assert answer["type"] == "rule_blocked" and answer["reason"] == "blocked"
    assert (
        answer["level"] == "profile"
        and answer["until"] is None
        and answer["retry_after"] is None
    )
    acts = [
        a
        for a in await client_activity.list_events(db, child)
        if a["event"] == "rule_blocked"
    ]
    assert len(acts) == 1 and acts[0]["detail"]["entity_id"] == "lock.front_door"
    assert calls == []


async def test_device_class_block_fails_closed_without_state(db, events, calls):
    """With the state unknown a ``domain:class`` block of that domain
    blocks (control path and snapshot)."""
    from services import enforcement

    rules = {"blocked_types": ["cover:garage"]}
    known = FakeHA(
        {"cover.tor": {"state": "closed", "attributes": {"device_class": "garage"}}}
    )
    await _area(db, "Garage", ["cover.tor"])
    _, child = await _child(db, known, events, rules=rules)
    msg = {"entity_id": "cover.tor", "service": "open_cover"}
    assert (await _control(db, known, child, **msg))["type"] == "rule_blocked"
    assert (await _control(db, FakeHA(), child, **msg))["type"] == "rule_blocked"
    assert calls == []
    layers = {"rules": rules, "areas": {}, "entities": {}}
    assert enforcement._kids_entity(layers, "cover.tor", [], None)["blocked"] is True
    # A known state without that class stays allowed; other domains too.
    window = {"attributes": {"device_class": "window"}}
    assert enforcement._kids_entity(layers, "cover.tor", [], window)["blocked"] is False
    assert enforcement._kids_entity(layers, "light.x", [], None)["blocked"] is False


async def test_quiet_hours_until(db, events, calls, monkeypatch):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    rules = {"quiet_hours": [{"days": ["wed"], "start": "20:00", "end": "06:30"}]}
    _, child = await _child(db, ha, events, rules=rules)
    now = datetime(2026, 11, 18, 21, 0, tzinfo=UTC)  # Wed 22:00 Zurich
    monkeypatch.setattr(kids_mode, "utcnow", lambda: now)
    answer = await _control(db, ha, child, entity_id="light.kids", service="turn_on")
    assert answer["reason"] == "quiet_hours"
    assert answer["until"] == "2026-11-19T05:30:00+00:00"
    assert answer["retry_after"] == int(timedelta(hours=8, minutes=30).total_seconds())
    assert (await _control(db, ha, child, entity_id="light.kids", service="turn_off"))[
        "type"
    ] == "control_ok"
    # Parent release for 30 min.
    await profiles_api.start_override(
        _request(db), child, profiles_api.OverrideRequest(minutes=30)
    )
    monkeypatch.setattr(kids_mode, "utcnow", lambda: now)
    row = await db.fetchone(
        "SELECT kids_override_until FROM paired_clients WHERE id = ?", (child,)
    )
    assert row["kids_override_until"]
    # set_override uses the real clock; move "now" into the release window.
    release = kids_mode.parse_iso(row["kids_override_until"])
    monkeypatch.setattr(kids_mode, "utcnow", lambda: release - timedelta(minutes=1))
    assert (await _control(db, ha, child, entity_id="light.kids", service="turn_on"))[
        "type"
    ] == "control_ok"


async def test_release_until_bedtime_end(db, events, monkeypatch):
    """Parent release "until tomorrow morning" = end of the bedtime."""
    ha = FakeHA()
    rules = {
        "quiet_hours": [{"days": ["wed", "thu"], "start": "20:00", "end": "06:30"}]
    }
    _, child = await _child(db, ha, events, rules=rules)
    # Wed 16:00 Zurich: before bedtime -> end of the coming one (Thu 06:30).
    before = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
    monkeypatch.setattr(kids_mode, "utcnow", lambda: before)
    status = await profiles_api.start_override(
        _request(db, ha), child, profiles_api.OverrideRequest(until="bedtime_end")
    )
    assert status["override_until"] == "2026-11-19T05:30:00+00:00"
    # Thu 23:00 Zurich: inside bedtime -> its end (Fri 06:30).
    inside = datetime(2026, 11, 19, 22, 0, tzinfo=UTC)
    monkeypatch.setattr(kids_mode, "utcnow", lambda: inside)
    status = await profiles_api.start_override(
        _request(db, ha), child, profiles_api.OverrideRequest(until="bedtime_end")
    )
    assert status["override_until"] == "2026-11-20T05:30:00+00:00"
    acts = [
        a["detail"]
        for a in await client_activity.list_events(db, child)
        if a["event"] == "kids_override"
    ]
    assert acts[0] == {"action": "release", "until": "2026-11-20T05:30:00+00:00"}


async def test_release_until_morning_without_bedtime(db, events, monkeypatch):
    ha = FakeHA()
    _, child = await _child(db, ha, events, rules={"quiet_hours": []})
    now = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)  # Wed 16:00 Zurich
    monkeypatch.setattr(kids_mode, "utcnow", lambda: now)
    status = await profiles_api.start_override(
        _request(db, ha), child, profiles_api.OverrideRequest(until="bedtime_end")
    )
    assert status["override_until"] == "2026-11-19T06:00:00+00:00"  # 07:00 local


def test_release_request_needs_exactly_one():
    with pytest.raises(ValueError):
        profiles_api.OverrideRequest()
    with pytest.raises(ValueError):
        profiles_api.OverrideRequest(minutes=15, until="bedtime_end")
    with pytest.raises(ValueError):
        profiles_api.OverrideRequest(until="noon")


async def test_rate_limit_cooldown_and_lift(db, events, calls):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    rules = {
        "rate_limit": {"max_actions": 2, "window_seconds": 60, "cooldown_seconds": 300}
    }
    _, child = await _child(db, ha, events, rules=rules)
    for _ in range(2):
        assert (
            await _control(db, ha, child, entity_id="light.kids", service="toggle")
        )["type"] == "control_ok"
    answer = await _control(db, ha, child, entity_id="light.kids", service="toggle")
    assert answer["reason"] == "rate_limited" and 295 <= answer["retry_after"] <= 300
    assert answer["limit"] == {"max_actions": 2, "window_seconds": 60}
    answer = await _control(db, ha, child, entity_id="light.kids", service="toggle")
    assert answer["reason"] == "cooldown"
    status = await profiles_api.get_kids_status(_request(db), child)
    assert [c["entity_id"] for c in status["cooldowns"]] == ["light.kids"]
    await profiles_api.end_override(_request(db), child)
    assert (await profiles_api.get_kids_status(_request(db), child))["cooldowns"] == []
    assert (await _control(db, ha, child, entity_id="light.kids", service="toggle"))[
        "type"
    ] == "control_ok"
    acts = [a["event"] for a in await client_activity.list_events(db, child)]
    assert "kids_override" in acts and "rule_blocked" in acts


async def test_a_device_pause_is_one_log_entry(db, events, calls):
    """The attempt that starts the pause is the entry (kids_pause),
    every further attempt during it counts up; the alarm threshold still
    counts every attempt; a new pause gets a new entry."""
    from services import event_log

    ha = FakeHA(
        {
            "light.kids": {"state": "off", "attributes": {}},
            "light.desk": {"state": "off", "attributes": {}},
        }
    )
    await _area(db, "Kinderzimmer", ["light.kids", "light.desk"])
    rules = {
        "rate_limit": {"max_actions": 1, "window_seconds": 60, "cooldown_seconds": 300}
    }
    _, child = await _child(db, ha, events, rules=rules)
    event_log._recent_denials.clear()
    event_log._last_alarm.clear()
    assert (await _control(db, ha, child, entity_id="light.kids", service="toggle"))[
        "type"
    ] == "control_ok"
    reasons = [
        (await _control(db, ha, child, entity_id="light.kids", service="toggle"))[
            "reason"
        ]
        for _ in range(6)
    ]
    assert reasons == ["rate_limited"] + ["cooldown"] * 5

    async def denied():
        return await db.fetchall(
            "SELECT kind, entity_id, reason, count, until FROM event_log "
            "WHERE client_id = ? AND category = 'activity' AND result = 'denied' "
            "ORDER BY id",
            (child,),
        )

    rows = await denied()
    assert [(r["kind"], r["entity_id"], r["count"]) for r in rows] == [
        ("kids_pause", "light.kids", 6)
    ]
    assert rows[0]["reason"] == "blocked_kids_cooldown" and rows[0]["until"]
    # Rejections still reach the alarm threshold (3 in 10 minutes by default).
    alarms = await db.fetchall(
        "SELECT count FROM event_log WHERE client_id = ? AND category = 'alarm'",
        (child,),
    )
    assert len(alarms) == 1 and alarms[0]["count"] >= 3
    # Another device's own pause and a pause after the end are entries of their own.
    assert (await _control(db, ha, child, entity_id="light.desk", service="toggle"))[
        "type"
    ] == "control_ok"
    assert (await _control(db, ha, child, entity_id="light.desk", service="toggle"))[
        "reason"
    ] == "rate_limited"
    await profiles_api.end_override(_request(db), child)
    await _control(db, ha, child, entity_id="light.kids", service="toggle")
    assert (await _control(db, ha, child, entity_id="light.kids", service="toggle"))[
        "reason"
    ] == "rate_limited"
    assert [(r["kind"], r["entity_id"], r["count"]) for r in await denied()] == [
        ("kids_pause", "light.kids", 6),
        ("kids_pause", "light.desk", None),
        ("kids_pause", "light.kids", None),
    ]


async def test_rate_limit_without_pause_stays_a_rejection(db, events, calls):
    """Without a pause (cooldown 0) a rejection is no pause entry."""
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    rules = {
        "rate_limit": {"max_actions": 1, "window_seconds": 60, "cooldown_seconds": 0}
    }
    _, child = await _child(db, ha, events, rules=rules)
    await _control(db, ha, child, entity_id="light.kids", service="toggle")
    for _ in range(2):
        assert (
            await _control(db, ha, child, entity_id="light.kids", service="toggle")
        )["reason"] == "rate_limited"
    rows = await db.fetchall(
        "SELECT kind FROM event_log WHERE client_id = ? AND category = 'activity' "
        "AND result = 'denied'",
        (child,),
    )
    assert [r["kind"] for r in rows] == ["denied", "denied"]


async def test_pause_everything_after_too_many_devices(db, events, calls):
    # Four different lights are fine, the fifth pauses everything.
    lights = [f"light.l{i}" for i in range(6)]
    ha = FakeHA({e: {"state": "off", "attributes": {}} for e in lights})
    await _area(db, "Wohnzimmer", lights)
    _, child = await _child(
        db, ha, events, rules={"rate_limit": None, "rate_limit_all": PAUSE_ALL}
    )
    for e in lights[:4]:
        answer = await _control(db, ha, child, entity_id=e, service="toggle")
        assert answer["type"] == "control_ok"
    # The same light again does not count as a new device.
    assert (await _control(db, ha, child, entity_id=lights[0], service="toggle"))[
        "type"
    ] == "control_ok"
    answer = await _control(db, ha, child, entity_id=lights[4], service="toggle")
    assert answer["type"] == "rule_blocked" and answer["reason"] == "paused_all"
    assert answer["level"] == "profile" and 295 <= answer["retry_after"] <= 300
    assert answer["limit"] == {
        "max_actions": 12,
        "max_entities": 4,
        "window_seconds": 60,
    }
    # Everything is paused now, also a light that was fine before.
    answer = await _control(db, ha, child, entity_id=lights[0], service="toggle")
    assert answer["reason"] == "paused_all" and answer["until"]
    assert len(calls) == 5

    status = await profiles_api.get_kids_status(_request(db), child)
    assert status["paused_all_until"] == answer["until"] and status["cooldowns"] == []
    config = await _get_client_config(db, child, ha)
    assert config["kids"]["paused_all_until"] == answer["until"]
    assert (child, {"type": "client_config_updated", "data": {}}) in events["to"]
    rows = await db.fetchall(
        "SELECT kind, reason, entity_id, detail_json, count FROM event_log "
        "WHERE client_id = ? AND result = 'denied' ORDER BY id",
        (child,),
    )
    # One entry per pause, further attempts counted on it.
    assert [(r["kind"], r["reason"], r["count"]) for r in rows] == [
        ("kids_paused_all", "blocked_kids_paused_all", 2),
    ]
    detail = json.loads(rows[0]["detail_json"])
    assert detail["trigger"] == "entities" and detail["entities"] == lights[:5]

    # "Pause beenden" of the admin ends it.
    await profiles_api.end_override(_request(db), child)
    assert (await profiles_api.get_kids_status(_request(db), child))[
        "paused_all_until"
    ] is None
    assert "paused_all_until" not in (await _get_client_config(db, child, ha))["kids"]
    assert (await _control(db, ha, child, entity_id=lights[5], service="toggle"))[
        "type"
    ] == "control_ok"


async def test_pause_everything_after_too_many_actions(db, events, calls):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    limit = {**PAUSE_ALL, "max_actions": 3, "max_entities": 10}
    _, child = await _child(
        db, ha, events, rules={"rate_limit": None, "rate_limit_all": limit}
    )
    for _ in range(3):
        assert (
            await _control(db, ha, child, entity_id="light.kids", service="toggle")
        )["type"] == "control_ok"
    answer = await _control(db, ha, child, entity_id="light.kids", service="toggle")
    assert answer["reason"] == "paused_all"
    row = await db.fetchone(
        "SELECT detail_json FROM event_log WHERE kind = 'kids_paused_all'"
    )
    assert json.loads(row["detail_json"])["trigger"] == "actions"


async def test_pause_everything_off_and_during_a_release(db, events, calls):
    lights = [f"light.l{i}" for i in range(6)]
    ha = FakeHA({e: {"state": "off", "attributes": {}} for e in lights})
    await _area(db, "Wohnzimmer", lights)
    # Off (catalog default): six devices in a row are fine.
    _, child = await _child(db, ha, events, rules={"rate_limit": None})
    for e in lights:
        assert (await _control(db, ha, child, entity_id=e, service="toggle"))[
            "type"
        ] == "control_ok"
    # On, but a parent release runs: no pause of everything.
    _, other = await _child(
        db,
        ha,
        events,
        rules={"rate_limit": None, "rate_limit_all": PAUSE_ALL},
        kids_override_until=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )
    for e in lights:
        assert (await _control(db, ha, other, entity_id=e, service="toggle"))[
            "type"
        ] == "control_ok"
    assert kids_rules.counters.paused_all(other, datetime.now(UTC)) is None


async def test_parallel_requests_cannot_pass_the_limits(db, events, monkeypatch):
    """The slot is taken before the HA round trip, so ten parallel
    requests with a limit of two let two through; a failed call gives its
    slot back."""
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    rules = {
        "rate_limit": {"max_actions": 2, "window_seconds": 60, "cooldown_seconds": 300}
    }
    _, child = await _child(db, ha, events, rules=rules)
    executed = []

    async def _slow(_ha, token, entity_id, action, data, grant):
        await asyncio.sleep(0.02)  # HA round trip
        executed.append(entity_id)
        return {"entity_id": entity_id, "ok": True}

    monkeypatch.setattr(hub_control, "ha_caller", _slow)
    msg = {"type": "control", "entity_id": "light.kids", "service": "toggle"}
    answers = await asyncio.gather(
        *[
            hub_control.control(db, ha, child, dict(msg, request_id=f"r{i}"))
            for i in range(10)
        ]
    )
    assert [a["type"] for a in answers].count("control_ok") == 2
    assert len(executed) == 2

    # Pause everything: five devices at once, the limit is four.
    lights = [f"light.l{i}" for i in range(5)]
    ha2 = FakeHA({e: {"state": "off", "attributes": {}} for e in lights})
    await _area(db, "Wohnzimmer", lights)
    _, other = await _child(
        db, ha2, events, rules={"rate_limit": None, "rate_limit_all": PAUSE_ALL}
    )
    executed.clear()
    answers = await asyncio.gather(
        *[
            hub_control.control(db, ha2, other, dict(msg, entity_id=e, request_id=e))
            for e in lights
        ]
    )
    assert [a["type"] for a in answers].count("control_ok") == 4
    assert len(executed) == 4
    assert kids_rules.counters.paused_all(other, datetime.now(UTC)) is not None

    # A failing call does not keep its reserved slot.
    kids_mode.reset()
    kids_rules.counters.reset()

    async def _fail(*_a):
        await asyncio.sleep(0.01)
        raise hub_control.ControlFailure("ha_unavailable")

    monkeypatch.setattr(hub_control, "ha_caller", _fail)
    for i in range(3):
        answer = await hub_control.control(db, ha, child, dict(msg, request_id=f"f{i}"))
        assert answer["reason"] == "ha_unavailable"
    assert kids_rules.counters.recent(child, "light.kids") == []
    assert kids_rules.counters.device_recent(child) == []


@pytest.mark.parametrize("n", [5, 8])
async def test_a_burst_of_failing_calls_never_pauses_everything(
    db, events, monkeypatch, n
):
    """Nothing ran, so nothing counts: no pause, no kids_paused_all."""
    lights = [f"light.f{i}" for i in range(n)]
    ha = FakeHA({e: {"state": "off", "attributes": {}} for e in lights})
    await _area(db, "Flur", lights)
    _, child = await _child(
        db, ha, events, rules={"rate_limit": None, "rate_limit_all": PAUSE_ALL}
    )

    async def _fail(*_a):
        await asyncio.sleep(0.01)
        raise hub_control.ControlFailure("ha_unavailable")

    monkeypatch.setattr(hub_control, "ha_caller", _fail)
    msg = {"type": "control", "service": "toggle"}
    answers = await asyncio.gather(
        *[
            hub_control.control(db, ha, child, dict(msg, entity_id=e, request_id=e))
            for e in lights
        ]
    )
    assert {a.get("reason") for a in answers} == {"ha_unavailable"}
    assert kids_rules.counters.paused_all(child, datetime.now(UTC)) is None
    assert kids_rules.counters.device_recent(child) == []
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM event_log WHERE kind = 'kids_paused_all'"
    )
    assert row["n"] == 0


async def test_a_burst_of_succeeding_calls_pauses_once(db, events, monkeypatch):
    lights = [f"light.s{i}" for i in range(8)]
    ha = FakeHA({e: {"state": "off", "attributes": {}} for e in lights})
    await _area(db, "Flur", lights)
    _, child = await _child(
        db, ha, events, rules={"rate_limit": None, "rate_limit_all": PAUSE_ALL}
    )

    async def _slow(_ha, token, entity_id, action, data, grant):
        await asyncio.sleep(0.01)
        return {"entity_id": entity_id, "ok": True}

    monkeypatch.setattr(hub_control, "ha_caller", _slow)
    msg = {"type": "control", "service": "toggle"}
    answers = await asyncio.gather(
        *[
            hub_control.control(db, ha, child, dict(msg, entity_id=e, request_id=e))
            for e in lights
        ]
    )
    assert [a["type"] for a in answers].count("control_ok") == 4
    assert kids_rules.counters.paused_all(child, datetime.now(UTC)) is not None
    rows = await db.fetchall(
        "SELECT count FROM event_log WHERE kind = 'kids_paused_all'"
    )
    assert len(rows) == 1


async def test_cancel_during_the_token_gives_the_slot_back(db, events, monkeypatch):
    """A cancelled request (also while the token is decrypted) does
    not keep its reserved slot."""
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    _, child = await _child(db, ha, events, rules=FREE)

    async def _slow_decrypt(_db, _c):
        await asyncio.sleep(10)

    monkeypatch.setattr(crypto, "decrypt_token_with_status", _slow_decrypt)
    msg = {"type": "control", "entity_id": "light.kids", "service": "toggle"}
    task = asyncio.ensure_future(hub_control.control(db, ha, child, dict(msg)))
    await asyncio.sleep(0.05)
    assert len(kids_rules.counters.recent(child, "light.kids")) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kids_rules.counters.recent(child, "light.kids") == []
    assert kids_rules.counters.device_recent(child) == []


async def test_a_request_behind_a_slow_call_is_answered_not_queued(
    db, events, monkeypatch
):
    """The wait for the previous action of the device is short: a child
    mashing the button while HA is slow gets ha_unavailable at once."""
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    _, child = await _child(db, ha, events, rules=FREE)
    monkeypatch.setattr(hub_control, "CONTROL_LOCK_WAIT_SECONDS", 0.05)
    executed = []

    async def _slow(_ha, token, entity_id, action, data, grant):
        await asyncio.sleep(0.3)
        executed.append(entity_id)
        return {"entity_id": entity_id, "ok": True}

    monkeypatch.setattr(hub_control, "ha_caller", _slow)
    msg = {"type": "control", "entity_id": "light.kids", "service": "toggle"}
    started = time.monotonic()
    answers = await asyncio.gather(
        *[
            hub_control.control(db, ha, child, dict(msg, request_id=f"m{i}"))
            for i in range(4)
        ]
    )
    assert [a["type"] for a in answers].count("control_ok") == 1
    assert (
        sorted(a.get("reason") for a in answers if a["type"] != "control_ok")
        == ["ha_unavailable"] * 3
    )
    assert len(executed) == 1 and time.monotonic() - started < 1.0
    assert len(kids_rules.counters.recent(child, "light.kids")) == 1


async def test_a_slow_state_read_is_bounded_and_fails_closed(
    db, events, calls, monkeypatch
):
    """The state read of the control path waits at most STATE_READ_SECONDS
    (outside the device lock); without a state a device-class block holds."""

    class SlowHA(FakeHA):
        async def get_state(self, entity_id):
            await asyncio.sleep(2)
            return await super().get_state(entity_id)

    ha = SlowHA(
        {"cover.tor": {"state": "closed", "attributes": {"device_class": "garage"}}}
    )
    await _area(db, "Garage", ["cover.tor"])
    _, child = await _child(db, ha, events, rules={"blocked_types": ["cover:garage"]})
    monkeypatch.setattr(hub_control, "STATE_READ_SECONDS", 0.05)
    started = time.monotonic()
    answer = await _control(db, ha, child, entity_id="cover.tor", service="open_cover")
    assert answer["type"] == "rule_blocked"
    assert time.monotonic() - started < 1.0
    assert calls == []


async def test_override_needs_child(db, events):
    adult = await _client(db)
    with pytest.raises(HTTPException) as e:
        await profiles_api.start_override(
            _request(db), adult, profiles_api.OverrideRequest(minutes=15)
        )
    assert e.value.status_code == 409


async def test_volume_cap_adjusts(db, events, calls):
    ha = FakeHA(
        {"media_player.tv": {"state": "playing", "attributes": {"volume_level": 0.1}}}
    )
    await _area(db, "Wohnzimmer", ["media_player.tv"])
    _, child = await _child(db, ha, events, rules={"volume_max": 40})
    answer = await _control(
        db,
        ha,
        child,
        entity_id="media_player.tv",
        service="volume_set",
        data={"volume_level": 1.0},
    )
    assert answer["adjusted"] == {"volume_level": 0.4}
    assert calls[-1] == (
        "device-token",
        "media_player",
        "volume_set",
        {"volume_level": 0.4, "entity_id": "media_player.tv"},
    )


async def test_no_token_is_not_ready(db, events, calls):
    ha = FakeHA()
    await _area(db, "Kinderzimmer", ["light.kids"])
    _, child = await _child(db, ha, events, rules=FREE, token=None)
    assert (await _control(db, ha, child, entity_id="light.kids", service="turn_on"))[
        "reason"
    ] == "not_ready"


async def test_ha_failures_map_to_reasons(db, events, monkeypatch):
    ha = FakeHA()
    await _area(db, "Kinderzimmer", ["light.kids"])
    _, child = await _child(db, ha, events, rules=FREE)

    async def _fail(*_a):
        raise hub_control.ControlFailure("ha_unavailable")

    monkeypatch.setattr(hub_control, "ha_caller", _fail)
    assert (await _control(db, ha, child, entity_id="light.kids", service="turn_on"))[
        "reason"
    ] == "ha_unavailable"
    assert kids_rules.counters.recent(child, "light.kids") == []


async def test_call_control_as_device_uses_core_token_and_grant(monkeypatch):
    """The hub switches for a child device through <ns>.control."""
    seen = {}

    class _Resp:
        status_code = 200
        body: dict = {}

        def json(self):
            return self.body

    class _Client:
        def __init__(self, **kw):
            seen["kw"] = kw

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, json=json, headers=headers)
            return _Resp()

    ok = {
        "entity_id": "light.x",
        "ok": True,
        "executed": True,
        "reason": None,
        "until": None,
        "adjusted": None,
    }
    _Resp.body = {"changed_states": [], "service_response": {"results": [ok]}}
    monkeypatch.setattr(hub_control.httpx, "AsyncClient", _Client)
    monkeypatch.delenv("HUB_HA_CORE_URL", raising=False)
    result = await hub_control.call_control_as_device(
        FakeHA(), "tok", "light.x", "turn_on", {"brightness_pct": 5}, "g" * 20
    )
    assert result["ok"] is True
    assert (
        seen["url"]
        == "http://homeassistant:80/api/services/halumar/control?return_response"
    )
    assert seen["headers"] == {"Authorization": "Bearer tok"}
    assert seen["json"] == {
        "entity_ids": ["light.x"],
        "action": "turn_on",
        "data": {"brightness_pct": 5},
        "grant": "g" * 20,
    }

    _Resp.body = {
        "service_response": {
            "results": [
                {
                    **ok,
                    "ok": False,
                    "executed": False,
                    "reason": "blocked_kids_bedtime",
                    "until": "2026-11-20T05:30:00+00:00",
                }
            ]
        }
    }
    with pytest.raises(hub_control.ControlDenied) as d:
        await hub_control.call_control_as_device(
            FakeHA(), "tok", "light.x", "turn_on", {}, None
        )
    assert d.value.reason == "blocked_kids_bedtime"
    assert "grant" not in seen["json"]

    _Resp.status_code = 400  # integration not loaded: no <ns>.control
    with pytest.raises(hub_control.ControlFailure) as e:
        await hub_control.call_control_as_device(
            FakeHA(), "tok", "light.x", "x", {}, None
        )
    assert e.value.reason == "ha_unavailable"
    _Resp.status_code = 401
    with pytest.raises(hub_control.ControlFailure) as e:
        await hub_control.call_control_as_device(
            FakeHA(), "tok", "light.x", "x", {}, None
        )
    assert e.value.reason == "ha_error"


async def test_control_denied_by_the_integration_maps_to_answers(
    db, events, monkeypatch
):
    ha = FakeHA()
    await _area(db, "Kinderzimmer", ["light.kids"])
    _, child = await _child(db, ha, events, rules=FREE)

    async def _deny(_ha, _token, _entity_id, _action, _data, _grant):
        raise hub_control.ControlDenied(reason, "2026-11-20T05:30:00+00:00")

    monkeypatch.setattr(hub_control, "ha_caller", _deny)
    reason = "blocked_kids_bedtime"
    answer = await _control(db, ha, child, entity_id="light.kids", service="turn_on")
    assert answer["type"] == "rule_blocked" and answer["reason"] == "quiet_hours"
    reason = "blocked_area"
    answer = await _control(db, ha, child, entity_id="light.kids", service="turn_on")
    assert answer == {
        "type": "control_error",
        "request_id": "r1",
        "reason": "not_visible",
    }
    reason = "rules_missing"
    answer = await _control(db, ha, child, entity_id="light.kids", service="turn_on")
    assert answer["reason"] == "not_ready"
    assert kids_rules.counters.recent(child, "light.kids") == []
    # The log keeps the hub's entry of each refused call.
    rows = await db.fetchall(
        "SELECT kind, reason, source, control_path FROM event_log "
        "WHERE category = 'activity' ORDER BY id"
    )
    assert [(r["kind"], r["reason"]) for r in rows] == [
        ("kids_bedtime_attempt", "blocked_kids_bedtime"),
        ("denied", "blocked_area"),
        ("denied", "rules_missing"),
    ]
    assert {(r["source"], r["control_path"]) for r in rows} == {("hub", "hub")}


async def test_check_endpoint(db, events):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    p = await _kids_profile(db, name="Child One")
    res = await profiles_api.check_rules(
        _request(db, ha),
        p["id"],
        profiles_api.CheckRequest(
            entity_id="light.kids", service="turn_on", at="2026-11-18T21:00:00+00:00"
        ),
    )
    assert (
        res["allowed"] is False
        and res["reason"] == "quiet_hours"
        and res["level"] == "profile"
    )
    res = await profiles_api.check_rules(
        _request(db, ha),
        p["id"],
        profiles_api.CheckRequest(
            entity_id="light.kids", service="turn_on", at="2026-11-18T12:00:00+00:00"
        ),
    )
    assert res == {"allowed": True, "service": "turn_on"}
    adult = await starter_profile(db, "adult")
    with pytest.raises(HTTPException) as e:
        await profiles_api.check_rules(
            _request(db, ha),
            adult,
            profiles_api.CheckRequest(entity_id="light.kids", service="turn_on"),
        )
    assert e.value.status_code == 409


# ── REST control path ───────────────────────────────────────────────────────


def _rest_request(db, path, secret, body=None):
    from starlette.requests import Request

    payload = json.dumps(body or {}).encode()
    headers = [(b"content-type", b"application/json")]
    if secret:
        headers.append((b"authorization", f"Bearer {secret}".encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("192.0.2.30", 5555),
        "server": ("hub", 8100),
        "scheme": "https",
        "app": types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        ),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


async def test_rest_client_secret_only_on_control_path(db, events, calls):
    ha = FakeHA()
    await _area(db, "Kinderzimmer", ["light.kids"])
    secret = uuid.uuid4().hex
    _, child = await _child(db, ha, events, rules=FREE, secret=secret)
    mw = AuthMiddleware(app=None)
    seen = {}

    async def _next(request):
        seen["auth"] = (request.state.auth_type, request.state.client_id)
        return "OK"

    assert (
        await mw.dispatch(_rest_request(db, "/api/client/control", secret), _next)
        == "OK"
    )
    assert seen["auth"] == ("client", child)
    bad = await mw.dispatch(_rest_request(db, "/api/client/control", "wrong"), _next)
    assert bad.status_code == 401
    none = await mw.dispatch(_rest_request(db, "/api/client/control", None), _next)
    assert none.status_code == 401
    other = await mw.dispatch(_rest_request(db, "/api/areas", secret), _next)
    assert other.status_code == 401
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET is_active = 0 WHERE id = ?", (child,)
        )
    revoked = await mw.dispatch(_rest_request(db, "/api/client/control", secret), _next)
    assert revoked.status_code == 401


async def test_rest_control_answers(db, events, calls):
    ha = FakeHA({"lock.front_door": {"state": "locked", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids", "lock.front_door"])
    _, child = await _child(db, ha, events)
    req = _rest_request(
        db,
        "/api/client/control",
        None,
        {"entity_id": "light.kids", "service": "turn_off"},
    )
    req.state.auth_type, req.state.client_id = "client", child
    req.app.state.ha_client = ha
    ok = await profiles_api.client_control(req)
    assert ok["type"] == "control_ok"
    req = _rest_request(
        db,
        "/api/client/control",
        None,
        {"entity_id": "lock.front_door", "service": "unlock"},
    )
    req.state.auth_type, req.state.client_id = "client", child
    blocked = await profiles_api.client_control(req)
    assert (
        blocked.status_code == 403 and json.loads(blocked.body)["reason"] == "blocked"
    )
    req = _rest_request(
        db, "/api/client/control", None, {"entity_id": "x", "service": "turn_on"}
    )
    req.state.auth_type, req.state.client_id = "client", child
    bad = await profiles_api.client_control(req)
    assert bad.status_code == 422
    req = _rest_request(db, "/api/client/control", None, {})
    req.state.auth_type = "admin"
    with pytest.raises(HTTPException):
        await profiles_api.client_control(req)


# ── state relay ─────────────────────────────────────────────────────────────


async def test_state_subscribe_only_for_children_and_visible(db, events):
    ha = FakeHA()
    await _area(
        db, "Kinderzimmer", ["light.kids", "device_tracker.mama"], tags=["kind"]
    )
    await _area(db, "Büro", ["light.office"], tags=["eltern"])
    adult = await _client(db)
    sock = _WS(db, ha, adult)
    await _handle_client_message(
        sock, db, {"type": "state_subscribe"}, "state_subscribe", adult
    )
    assert sock.last()["reason"] == "not_hub_controlled"

    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    sock = _WS(db, ha, child)
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        await _handle_client_message(
            sock, db, {"type": "state_subscribe"}, "state_subscribe", child
        )
    finally:
        state_relay._ensure_running = original
    assert sock.last() == {"type": "state_subscribed", "entity_count": 2}
    assert await state_relay.dispatch_event("light.office", {"state": "on"}) == 0
    assert (
        await state_relay.dispatch_event(
            "light.kids", {"state": "on", "attributes": {}}
        )
        == 1
    )
    assert sock.last() == {
        "type": "entity_state",
        "entity_id": "light.kids",
        "state": {"state": "on", "attributes": {}},
    }
    await state_relay.dispatch_event(
        "device_tracker.mama",
        {"state": "home", "attributes": {"latitude": 47.1, "friendly_name": "Mama"}},
    )
    assert sock.last()["state"]["attributes"] == {"friendly_name": "Mama"}
    state_relay.unsubscribe(sock)
    assert state_relay.subscriber_count() == 0


async def test_state_subscribe_sends_a_snapshot_of_the_visible_states(db, events):
    ha = FakeHA(
        {
            "light.kids": {"entity_id": "light.kids", "state": "on", "attributes": {}},
            "device_tracker.mama": {
                "entity_id": "device_tracker.mama",
                "state": "home",
                "attributes": {"latitude": 47.1, "friendly_name": "Mama"},
            },
            "light.office": {"entity_id": "light.office", "state": "on"},
        }
    )
    await _area(
        db,
        "Kinderzimmer",
        ["light.kids", "device_tracker.mama", "sensor.gone"],
        tags=["kind"],
    )
    await _area(db, "Büro", ["light.office"], tags=["eltern"])
    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    sock = _WS(db, ha, child)
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        await _handle_client_message(
            sock, db, {"type": "state_subscribe"}, "state_subscribe", child
        )
    finally:
        state_relay._ensure_running = original
    assert sock.sent[0] == {"type": "state_subscribed", "entity_count": 3}
    snapshot = {m["entity_id"]: m["state"] for m in sock.sent[1:]}
    # Only visible entities with a state; GPS removed like every relayed state.
    assert set(snapshot) == {"device_tracker.mama", "light.kids"}
    assert all(m["type"] == "entity_state" for m in sock.sent[1:])
    assert snapshot["device_tracker.mama"]["attributes"] == {"friendly_name": "Mama"}
    state_relay.unsubscribe(sock)


class _StuckWS(_WS):
    """Takes the subscription answer, then never finishes a send."""

    stalled = False
    hang = None

    async def send_json(self, payload):
        if self.stalled:
            await self.hang.wait()
        await super().send_json(payload)

    async def send_text(self, payload):
        if self.stalled:
            await self.hang.wait()
        await super().send_text(payload)


async def _subscribed(db, child, ha, cls=_WS):
    sock = cls(db, ha, child)
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        await _handle_client_message(
            sock, db, {"type": "state_subscribe"}, "state_subscribe", child
        )
    finally:
        state_relay._ensure_running = original
    return sock


async def test_a_stalled_device_does_not_hold_up_the_relay(db, events):
    # Relayed states go through each connection's queue.
    ha = FakeHA({"light.kids": {"entity_id": "light.kids", "state": "off"}})
    await _area(db, "Kinderzimmer", ["light.kids"], tags=["kind"])
    _, first = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    _, second = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    stuck = await _subscribed(db, first, ha, cls=_StuckWS)
    fine = await _subscribed(db, second, ha)
    hang = asyncio.Event()
    stuck.hang, stuck.stalled = hang, True
    sent = await asyncio.wait_for(
        state_relay.dispatch_event("light.kids", {"state": "on", "attributes": {}}),
        1,
    )
    assert sent == 2
    assert fine.last()["state"]["state"] == "on"
    hang.set()
    state_relay.unsubscribe(stuck)
    state_relay.unsubscribe(fine)


async def test_relay_reconnect_sends_a_fresh_snapshot(db, events, monkeypatch):
    # Changes made while the relay's HA socket was down were not relayed.
    import websockets

    ha = FakeHA({"light.kids": {"entity_id": "light.kids", "state": "off"}})
    ha._token = "supervisor"
    await _area(db, "Kinderzimmer", ["light.kids"], tags=["kind"])
    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    sock = await _subscribed(db, child, ha)
    sock.sent.clear()
    connects = []
    second = asyncio.Event()

    class _Conn:
        def __init__(self, last):
            self.script = [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 1, "type": "result", "success": True},
            ]
            self.last = last

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, data):
            pass

        async def recv(self):
            if self.script:
                return json.dumps(self.script.pop(0))
            if self.last:
                second.set()
                await asyncio.Event().wait()
            ha.states["light.kids"]["state"] = "on"  # changed while it is down
            raise ConnectionError("HA restarted")

    def _connect(url, **kw):
        connects.append(url)
        return _Conn(last=len(connects) > 1)

    monkeypatch.setattr(websockets, "connect", _connect)
    monkeypatch.setattr(state_relay, "RECONNECT_MIN", 0.01)
    state_relay._ensure_running()
    await asyncio.wait_for(second.wait(), 2)
    for _ in range(50):
        if sock.sent:
            break
        await asyncio.sleep(0.01)
    assert len(connects) == 2
    assert [(m["entity_id"], m["state"]["state"]) for m in sock.sent] == [
        ("light.kids", "on")
    ]
    task = state_relay._task
    state_relay.unsubscribe(sock)
    with pytest.raises(asyncio.CancelledError):
        await task


class _SlowHA(FakeHA):
    """get_state reads now and returns later (REST, mirror lag)."""

    gate: asyncio.Event

    async def get_state(self, entity_id):
        state = dict(self.states.get(entity_id) or {})
        await self.gate.wait()
        return state


async def _snapshot_vs_live(db, events, ha, hold, release):
    await _area(db, "Kinderzimmer", ["light.kids"], tags=["kind"])
    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    sock = await _subscribed(db, child, ha)
    sock.sent.clear()
    hold(sock)
    snap = asyncio.create_task(state_relay.send_snapshot(sock, ha, queued=True))
    await asyncio.sleep(0.01)  # the snapshot is between reading and sending
    ha.states["light.kids"]["state"] = "on"
    await state_relay.dispatch_event("light.kids", {"state": "on", "attributes": {}})
    release()
    await snap
    for _ in range(20):
        await asyncio.sleep(0.01)
    state_relay.unsubscribe(sock)
    return [
        m["state"]["state"] for m in sock.sent if m.get("entity_id") == "light.kids"
    ]


async def test_snapshot_read_before_a_live_state_is_not_sent_after_it(db, events):
    # The slow read returned "off" after "on" went out.
    ha = _SlowHA({"light.kids": {"entity_id": "light.kids", "state": "off"}})
    ha.gate = asyncio.Event()
    ha.gate.set()

    def hold(_sock):
        ha.gate = asyncio.Event()

    seen = await _snapshot_vs_live(db, events, ha, hold, lambda: ha.gate.set())
    assert seen == ["on"]


async def test_snapshot_waiting_for_the_queue_reads_after_it(db, events):
    # The state was read, then the snapshot waited for the backlog.
    ha = FakeHA({"light.kids": {"entity_id": "light.kids", "state": "off"}})
    busy = {"v": 200}

    def hold(sock):
        state_relay._subscribers[sock].backlog = lambda: busy["v"]

    def release():
        busy["v"] = 0

    seen = await _snapshot_vs_live(db, events, ha, hold, release)
    assert seen[-1] == "on"


async def test_snapshot_waits_until_the_relay_listens(monkeypatch):
    # A change between the snapshot and the start of the HA subscription would be lost:
    # the snapshot is read only once a starting relay is subscribed (found in a live test).
    state_relay.reset()
    sent = []

    async def send(msg):
        sent.append(msg)

    class HA:
        async def get_state(self, entity_id):
            return {"entity_id": entity_id, "state": "on", "attributes": {}}

    key = object()
    state_relay._subscribers[key] = state_relay.Subscriber("c1", send, {"light.kids"})
    blocker = asyncio.Event()
    monkeypatch.setattr(
        state_relay, "_task", asyncio.get_running_loop().create_task(blocker.wait())
    )
    task = asyncio.get_running_loop().create_task(state_relay.send_snapshot(key, HA()))
    await asyncio.sleep(0.05)
    assert sent == [] and not task.done()
    state_relay._ready_event().set()
    assert await asyncio.wait_for(task, 1) == 1
    assert sent[0]["entity_id"] == "light.kids"
    # Without a running relay (or after the timeout) the snapshot goes out anyway.
    blocker.set()
    await asyncio.sleep(0)
    state_relay._ready_event().clear()
    assert await state_relay.wait_ready(0.01) is False
    state_relay.reset()


# ── history and forecast through the hub ──────────────────────


class ReadHA(FakeHA):
    """FakeHA with recorder history and weather.get_forecasts."""

    def __init__(self, states=None, rows=None, forecast=None, fail=None):
        super().__init__(states)
        self.rows = rows or []
        self.forecast = forecast or []
        self.fail = fail
        self.history_calls = []
        self.forecast_calls = []

    async def get_history(self, entity_id, start_time, end_time):
        self.history_calls.append((entity_id, start_time, end_time))
        if self.fail:
            raise self.fail
        return self.rows

    async def get_forecasts(self, entity_id, forecast_type):
        self.forecast_calls.append((entity_id, forecast_type))
        if self.fail:
            raise self.fail
        return self.forecast


async def _read(db, ha, client_id, msg):
    sock = _WS(db, ha, client_id)
    await _handle_client_message(sock, db, msg, msg["type"], client_id)
    return sock.last()


WEATHER = {
    "entity_id": "weather.home",
    "state": "cloudy",
    "attributes": {"supported_features": 3},
}


async def test_history_for_a_visible_entity_of_a_child(db, events):
    rows = [
        {"s": "21.4", "lu": 1763575200.0},
        {"s": "21.0", "lu": 1763580000.5, "lc": 1763579000.25},
        {"s": "unavailable", "lu": 1763583000.0},
        {"x": "broken"},
    ]
    ha = ReadHA(rows=rows)
    await _area(db, "Kinderzimmer", ["sensor.kids_temp"], tags=["kind"])
    await _area(db, "Büro", ["sensor.office_temp"], tags=["eltern"])
    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    answer = await _read(
        db,
        ha,
        child,
        {
            "type": "history_get",
            "request_id": "h1",
            "entity_id": "sensor.kids_temp",
            "hours": 24,
        },
    )
    assert answer["type"] == "history" and answer["request_id"] == "h1"
    assert answer["entity_id"] == "sensor.kids_temp" and answer["hours"] == 24
    assert answer["truncated"] is False
    assert [p["state"] for p in answer["states"]] == ["21.4", "21.0", "unavailable"]
    # last_changed wins over last_updated; UTC ISO 8601
    assert answer["states"][1]["last_changed"].startswith("2025-11-19T")
    assert answer["states"][1]["last_changed"].endswith("+00:00")
    entity, start, end = ha.history_calls[0]
    assert entity == "sensor.kids_temp"
    span = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    assert span == timedelta(hours=24)

    # not visible to this child (other area), bad hours, not a child
    answer = await _read(
        db,
        ha,
        child,
        {
            "type": "history_get",
            "request_id": "h2",
            "entity_id": "sensor.office_temp",
            "hours": 24,
        },
    )
    assert answer == {
        "type": "control_error",
        "request_id": "h2",
        "reason": "not_visible",
    }
    for hours in (0, 169, "24", True, None):
        answer = await _read(
            db,
            ha,
            child,
            {
                "type": "history_get",
                "request_id": "h3",
                "entity_id": "sensor.kids_temp",
                "hours": hours,
            },
        )
        assert answer["reason"] == "invalid_request"
    adult = await _client(db)
    answer = await _read(
        db,
        ha,
        adult,
        {
            "type": "history_get",
            "request_id": "h4",
            "entity_id": "sensor.kids_temp",
            "hours": 1,
        },
    )
    assert answer == {
        "type": "control_error",
        "request_id": "h4",
        "reason": "not_hub_controlled",
    }
    assert len(ha.history_calls) == 1


async def test_history_is_thinned_out_and_ha_failures_map(db, events):
    from services import hub_read

    rows = [{"s": str(i), "lu": 1763575200.0 + i} for i in range(2500)]
    points, truncated = hub_read.history_points(rows)
    assert truncated and len(points) == hub_read.HISTORY_MAX_POINTS
    assert points[0]["state"] == "0" and points[-1]["state"] == "2499"
    assert hub_read.history_points(rows[:10]) == (
        hub_read.history_points(rows[:10])[0],
        False,
    )

    await _area(db, "Kinderzimmer", ["sensor.kids_temp"])
    for fail, reason in (
        (ConnectionError("down"), "ha_unavailable"),
        (RuntimeError("Command failed"), "ha_error"),
    ):
        ha = ReadHA(fail=fail)
        _, child = await _child(db, ha, events, rules=FREE)
        answer = await _read(
            db,
            ha,
            child,
            {
                "type": "history_get",
                "request_id": "h1",
                "entity_id": "sensor.kids_temp",
                "hours": 8,
            },
        )
        assert answer == {"type": "control_error", "request_id": "h1", "reason": reason}


async def test_forecast_for_a_visible_weather_entity(db, events):
    entries = [
        {
            "datetime": "2026-11-21T11:00:00+00:00",
            "condition": "cloudy",
            "temperature": 8.0,
        },
        {"condition": "no datetime"},
    ] + [{"datetime": f"2026-11-22T{h:02d}:00:00+00:00"} for h in range(24)] * 20
    ha = ReadHA({"weather.home": WEATHER, "weather.office": WEATHER}, forecast=entries)
    await _area(db, "Garten", ["weather.home", "sensor.kids_temp"], tags=["kind"])
    await _area(db, "Büro", ["weather.office"], tags=["eltern"])
    _, child = await _child(db, ha, events, rules=FREE, tags='["kind"]')
    msg = {"type": "forecast_get", "request_id": "f1", "entity_id": "weather.home"}
    answer = await _read(db, ha, child, {**msg, "forecast_type": "hourly"})
    assert answer["type"] == "forecast" and answer["request_id"] == "f1"
    assert answer["forecast_type"] == "hourly"
    assert answer["forecast"][0]["condition"] == "cloudy"
    assert len(answer["forecast"]) == 240
    assert all("datetime" in e for e in answer["forecast"])
    assert ha.forecast_calls == [("weather.home", "hourly")]

    # twice_daily is not supported (features 3 = daily + hourly)
    answer = await _read(db, ha, child, {**msg, "forecast_type": "twice_daily"})
    assert answer["reason"] == "invalid_request"
    answer = await _read(db, ha, child, {**msg, "forecast_type": "weekly"})
    assert answer["reason"] == "invalid_request"
    # a sensor is no weather entity, an invisible weather entity is not visible
    answer = await _read(
        db,
        ha,
        child,
        {**msg, "entity_id": "sensor.kids_temp", "forecast_type": "daily"},
    )
    assert answer["reason"] == "invalid_request"
    answer = await _read(
        db, ha, child, {**msg, "entity_id": "weather.office", "forecast_type": "daily"}
    )
    assert answer["reason"] == "not_visible"
    assert len(ha.forecast_calls) == 1


async def test_read_requests_need_a_request_id_and_a_paired_device(db, events):
    ha = ReadHA({"weather.home": WEATHER})
    await _area(db, "Garten", ["weather.home"])
    _, child = await _child(db, ha, events, rules=FREE)
    answer = await _read(
        db,
        ha,
        child,
        {"type": "forecast_get", "entity_id": "weather.home", "forecast_type": "daily"},
    )
    assert answer == {
        "type": "control_error",
        "request_id": None,
        "reason": "invalid_request",
    }
    answer = await _read(
        db,
        ha,
        "ingress",
        {
            "type": "history_get",
            "request_id": "h1",
            "entity_id": "weather.home",
            "hours": 1,
        },
    )
    assert answer["reason"] == "not_hub_controlled"


async def test_sun_is_relayed_with_a_visible_weather_entity(db, events):
    ha = FakeHA(
        {
            "weather.home": WEATHER,
            "sun.sun": {
                "entity_id": "sun.sun",
                "state": "below_horizon",
                "attributes": {},
            },
        }
    )
    await _area(db, "Garten", ["weather.home"])
    _, child = await _child(db, ha, events, rules=FREE)
    sock = _WS(db, ha, child)
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        await _handle_client_message(
            sock, db, {"type": "state_subscribe"}, "state_subscribe", child
        )
    finally:
        state_relay._ensure_running = original
    assert sock.sent[0] == {"type": "state_subscribed", "entity_count": 2}
    assert {m["entity_id"] for m in sock.sent[1:]} == {"weather.home", "sun.sun"}
    # read only: the sun is not in the control scope
    assert "sun.sun" not in await hub_control.visible_entities(db, child)
    state_relay.unsubscribe(sock)


async def test_ha_client_reads_history_and_forecast_over_the_ha_socket(monkeypatch):
    from services.ha_client import HAClient

    client = HAClient("supervisor-token")
    sent = []

    async def command(msg):
        sent.append(msg)
        if msg["type"] == "history/history_during_period":
            return {"sensor.kids_temp": [{"s": "21.4", "lu": 1.0}]}
        return {
            "context": {},
            "response": {"weather.home": {"forecast": [{"datetime": "x"}]}},
        }

    monkeypatch.setattr(client, "_ws_command", command)
    try:
        assert await client.get_history("sensor.kids_temp", "a", "b") == [
            {"s": "21.4", "lu": 1.0}
        ]
        assert await client.get_forecasts("weather.home", "daily") == [
            {"datetime": "x"}
        ]
        assert sent[0] == {
            "type": "history/history_during_period",
            "start_time": "a",
            "end_time": "b",
            "entity_ids": ["sensor.kids_temp"],
            "minimal_response": True,
            "no_attributes": True,
            "significant_changes_only": True,
        }
        assert sent[1]["return_response"] is True
        assert sent[1]["target"] == {"entity_id": "weather.home"}
        assert sent[1]["service_data"] == {"type": "daily"}
        with pytest.raises(ValueError):
            await client.get_history("../x", "a", "b")
        with pytest.raises(ValueError):
            await client.get_forecasts("weather.home", "../daily")
    finally:
        await client.close()
