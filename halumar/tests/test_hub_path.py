"""The hub path for devices on "Hub only" (services/hub_control.py).

A device on the hub path without parental controls (a guest, a babysitter,
a member on Hub only) holds no Home Assistant key unless remote access gives
it a read-only one. It reads (areas, states, history, forecast, still images)
and switches through the hub, which checks the device, its access and its
scope and then calls ``<ns>.control`` with the device's token and without a
grant. The tests run against the real handlers with a fake Home Assistant
and cover the abuse cases a hub can decide: other areas and entities,
targets in ``data``, REST twin, forged grants, closed access, a watch
secret, no-escalation, flooding and what leaves the hub.
"""

import asyncio
import json
import time
import types
import uuid

import api.websocket as ws
import pytest
from api import integration as integration_api
from api import profiles as profiles_api
from api.ws.client import _get_client_config, _handle_client_message
from models.database import Database
from services import (
    crypto,
    enforcement,
    event_log,
    hub_control,
    hub_read,
    kids_mode,
    onboarding,
    state_relay,
)
from services import device_provisioning as dp
from services.profiles import changes as pchanges
from services.profiles import resolver as res
from services.profiles import store
from starlette.websockets import WebSocketState

from .profile_helpers import add_client, starter_profile

STATES = {
    "light.living": {"entity_id": "light.living", "state": "off", "attributes": {}},
    "light.office": {"entity_id": "light.office", "state": "on", "attributes": {}},
    "scene.evening": {"entity_id": "scene.evening", "state": "x", "attributes": {}},
    "camera.door": {
        "entity_id": "camera.door",
        "state": "idle",
        "attributes": {
            "access_token": "secret-camera-token",
            "entity_picture": "/api/camera_proxy/camera.door?token=secret",
        },
    },
    "camera.garage": {"entity_id": "camera.garage", "state": "idle", "attributes": {}},
    "media_player.tv": {
        "entity_id": "media_player.tv",
        "state": "playing",
        "attributes": {"device_class": "tv"},
    },
    "person.mama": {
        "entity_id": "person.mama",
        "state": "home",
        "attributes": {"latitude": 47.1, "longitude": 8.5, "friendly_name": "Mama"},
    },
    "weather.home": {
        "entity_id": "weather.home",
        "state": "sunny",
        "attributes": {"supported_features": 1},
    },
}
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    ws._clients.clear()
    kids_mode.reset()
    state_relay.reset()
    event_log.reset()

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
    log = {"to": [], "integration": []}

    async def _send_to(client_id, message):
        log["to"].append((client_id, message))
        return True

    async def _broadcast(message, *a, **k):
        return None

    async def _sender(message):
        log["integration"].append(message)
        return True

    monkeypatch.setattr(ws, "send_to_client", _send_to)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    monkeypatch.setattr(dp, "_sender", _sender)
    return log


class FakeHA:
    """Home Assistant stand-in: states, time zone, history, forecast and the
    image proxies (served with the hub's own access)."""

    def __init__(self, states=None, images=None):
        self.states = dict(STATES if states is None else states)
        self.images = images if images is not None else {}
        self.image_calls = []
        self.sizes = []
        self.history_calls = []

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def get_config(self):
        return {"time_zone": "Europe/Zurich"}

    async def get_core_info(self):
        return {"port": 80, "ssl": False}

    def invalidate_state_cache(self, entity_id=None):
        pass

    async def get_history(self, entity_id, start, end):
        self.history_calls.append(entity_id)
        return [{"s": "21.5", "lu": 1764000000.0}]

    async def get_forecasts(self, entity_id, kind):
        return [{"datetime": "2026-11-25T12:00:00+00:00", "temperature": 4}]

    async def get_image(
        self, proxy, entity_id, *, max_bytes, timeout, width=None, height=None
    ):
        self.image_calls.append((proxy, entity_id, width))
        self.sizes.append((width, height))
        answer = self.images.get(entity_id, ("image/jpeg", JPEG))
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def calls(monkeypatch):
    """``<ns>.control`` calls the hub makes as a device, like the integration
    would get them: the device's own token, a grant only for children."""
    log = []

    async def _caller(_ha, token, entity_id, action, data, grant):
        log.append(
            {
                "token": token,
                "entity_id": entity_id,
                "action": action,
                "data": data,
                "grant": grant,
            }
        )
        return {"entity_id": entity_id, "ok": True, "executed": True}

    monkeypatch.setattr(hub_control, "ha_caller", _caller)
    return log


class _WS:
    def __init__(self, db, ha, client_id):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(
                db=db, ha_client=ha, app_url="https://hub.example.test"
            )
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

    async def send_text(self, payload):
        self.sent.append(json.loads(payload))

    def last(self):
        return self.sent[-1] if self.sent else None


async def _area(db, name, entities=(), tags=(), tiles=None, hidden=()):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, "
            "updated_at) VALUES (?, ?, 0, 1, ?, ?)",
            (area_id, name, now, now),
        )
        await db.execute(
            "INSERT INTO area_sections (id, area_id, title, sort_order, "
            "column_span, columns) VALUES (?, ?, 'S', 0, 1, 2)",
            (section_id, area_id),
        )
        for i, eid in enumerate([*entities, *hidden]):
            tile = (tiles or {}).get(eid)
            await db.execute(
                "INSERT INTO area_entities (id, area_id, section_id, entity_id, "
                "sort_order, tile, is_visible, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    area_id,
                    section_id,
                    eid,
                    i,
                    json.dumps(tile) if tile else None,
                    0 if eid in hidden else 1,
                    now,
                ),
            )
        for tag in tags:
            await db.execute(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
            )
    return area_id


async def _device(db, *, token="device-token", **columns):
    extra = {"ha_token_status": "active" if token else "pending"}
    if token:
        extra["ha_token_encrypted"] = await crypto.encrypt_token(db, token)
    columns.setdefault("name", f"iPhone {uuid.uuid4().hex[:4]}")
    columns.setdefault("platform", "ios")
    return await add_client(db, last_seen=Database.now(), **extra, **columns)


async def _guest(db, tags=("gast",), **columns):
    """A key-less guest on the guest default profile (Hub only, fixed)."""
    return await _device(db, guest=True, tags=list(tags), **columns)


async def _hub_member(db, **columns):
    """A member on Hub only (adjustment of the Adult profile), without
    remote access unless asked."""
    overrides = {"control_path": "hub", **columns.pop("overrides", {})}
    columns.setdefault("remote", False)
    return await _device(db, profile="adult", overrides=overrides, **columns)


async def _send(db, ha, client_id, message):
    sock = _WS(db, ha, client_id)
    await _handle_client_message(sock, db, message, message["type"], client_id)
    return sock


async def _control(db, ha, client_id, **msg):
    sock = await _send(
        db, ha, client_id, {"type": "control", "request_id": "r1", **msg}
    )
    return sock.last()


async def _home(db):
    """The living room for the guests, the office for nobody else."""
    living = await _area(
        db,
        "Wohnzimmer",
        ["light.living", "camera.door", "media_player.tv", "weather.home"],
        tags=["gast"],
        tiles={
            "light.living": {
                "tap_action": {"action": "run", "target_entity_id": "scene.evening"}
            }
        },
        hidden=["camera.garage"],
    )
    office = await _area(db, "Büro", ["light.office", "person.mama"], tags=["eltern"])
    return living, office


# ── who is on the hub path, what client_config says ─────────────────────────


async def test_hub_only_devices_get_no_key_and_hub_markers(db):
    await _home(db)
    guest = await _guest(db)
    member = await _hub_member(db)
    checked = await _device(db, profile="adult")
    for cid in (guest, member):
        cfg = await _get_client_config(db, cid, FakeHA())
        assert cfg["rules"]["control_path"]["value"] == "hub"
        assert cfg["ha_token"] is None and cfg["ha_enforcement"] is None
        assert cfg["ha_instance_id"] is None and cfg["ha_url"] is None
        assert cfg["rules"]["areas"]["enforcement"] == "hub"
        assert cfg["rules"]["quick_access"]["value"] == "off"
        assert "kids" not in cfg
        assert await hub_control.uses_hub_path(db, cid)
    cfg = await _get_client_config(db, member, FakeHA())
    assert cfg["rules"]["quick_access"]["reason"] == "boundary_hub_only"
    assert cfg["rules"]["watch"]["reason"] == "boundary_hub_only"
    cfg = await _get_client_config(db, checked, FakeHA())
    assert cfg["ha_token"] == "device-token"
    assert cfg["ha_enforcement"]["control_path"] == "checked"
    assert not await hub_control.uses_hub_path(db, checked)


async def test_hub_only_with_remote_access_holds_a_read_only_key(db):
    member = await _hub_member(db, remote=True)
    cfg = await _get_client_config(db, member, FakeHA())
    assert cfg["rules"]["control_path"]["value"] == "hub"
    assert cfg["ha_token"] == "device-token"
    assert cfg["ha_enforcement"]["control_path"] == "checked"
    assert cfg["rules"]["watch"]["reason"] != "boundary_hub_only"
    # Still on the hub path at home.
    assert await hub_control.uses_hub_path(db, member)


async def test_hub_only_never_reaches_a_snapshot(db):
    """The integration refuses a snapshot with the value hub: every device on
    the hub path is checked in Home Assistant (read-only device user)."""
    await _home(db)
    guest = await _guest(db, ha_user_id="a" * 32)
    member = await _hub_member(db, ha_user_id="b" * 32, remote=True)
    rule_set = await enforcement.build_set(db, FakeHA())
    paths = {s["client_id"]: s["control_path"] for s in rule_set.snapshots}
    assert paths[guest] == paths[member] == "checked"
    assert "hub" not in rule_set.paths.values()
    assert enforcement.control_path({"control_path": {"value": "hub"}}) == "checked"
    assert enforcement.control_path({}) == "checked"
    assert (await enforcement.ha_enforcement(db, member, {}))["control_path"] in (
        "checked",
        "direct",
    )


# ── C1.1 areas, C1.4 control scope ──────────────────────────────────────────


async def test_area_outside_the_scope_is_not_found(db):
    _living, office = await _home(db)
    guest = await _guest(db)
    sock = await _send(db, FakeHA(), guest, {"type": "get_area", "area_id": office})
    missing = await _send(
        db, FakeHA(), guest, {"type": "get_area", "area_id": str(uuid.uuid4())}
    )
    assert sock.last() == missing.last()
    assert "Area not found" in json.dumps(sock.last())


async def test_control_in_scope_calls_the_integration_without_grant(db, calls):
    await _home(db)
    guest = await _guest(db, token="guest-token")
    ok = await _control(
        db, FakeHA(), guest, entity_id="light.living", service="turn_on"
    )
    assert ok == {"type": "control_ok", "request_id": "r1", "entity_id": "light.living"}
    # The run target of a visible tile.
    ok = await _control(
        db, FakeHA(), guest, entity_id="scene.evening", service="turn_on"
    )
    assert ok["type"] == "control_ok"
    assert [(c["token"], c["entity_id"], c["grant"]) for c in calls] == [
        ("guest-token", "light.living", None),
        ("guest-token", "scene.evening", None),
    ]
    # No kids state for a device without parental controls.
    from services import kids_rules

    assert not kids_rules.counters.recent(guest, "light.living")


async def test_control_outside_the_scope_is_not_visible(db, calls):
    living, office = await _home(db)
    guest = await _guest(db)
    async with db.transaction():
        # The office is only linked by a notification: readable, not switchable.
        await db.execute(
            "INSERT INTO client_notifications (id, client_id, created_at, "
            "action_type, action_target, status) VALUES (?, ?, ?, 'area', ?, "
            "'delivered')",
            (str(uuid.uuid4()), guest, Database.now(), office),
        )
    ha = FakeHA()
    for entity_id in (
        "light.office",  # another area (linked by the notification)
        "camera.garage",  # hidden placement
        "scene.unplaced",  # unplaced scene
        "light.unknown",  # unknown entity
    ):
        answer = await _control(db, ha, guest, entity_id=entity_id, service="turn_on")
        assert answer["type"] == "control_error" and answer["reason"] == "not_visible"
    assert calls == []
    # The linked area itself is readable.
    sock = await _send(db, ha, guest, {"type": "get_area", "area_id": office})
    assert sock.last().get("type") != "error"


async def test_run_target_of_a_hidden_tile_is_not_visible(db, calls):
    await _area(
        db,
        "Wohnzimmer",
        ["light.living"],
        tags=["gast"],
        hidden=["switch.hidden"],
        tiles={
            "switch.hidden": {
                "tap_action": {"action": "run", "target_entity_id": "script.secret"}
            }
        },
    )
    guest = await _guest(db)
    answer = await _control(
        db, FakeHA(), guest, entity_id="script.secret", service="turn_on"
    )
    assert answer["reason"] == "not_visible" and calls == []


# ── C1.5 request shape ──────────────────────────────────────────────────────


async def test_targets_in_data_are_dropped_and_large_data_refused(db, calls):
    await _home(db)
    guest = await _guest(db)
    ok = await _control(
        db,
        FakeHA(),
        guest,
        entity_id="light.living",
        service="turn_on",
        data={
            "entity_id": "light.office",
            "area_id": "x",
            "device_id": "y",
            "brightness": 10,
        },
    )
    assert ok["type"] == "control_ok"
    assert calls[-1]["data"] == {"brightness": 10}
    big = await _control(
        db,
        FakeHA(),
        guest,
        entity_id="light.living",
        service="turn_on",
        data={"x": "y" * 5000},
    )
    assert big["reason"] == "invalid_request"
    assert len(calls) == 1


# ── C1.6 REST twin ──────────────────────────────────────────────────────────


def _rest(db, ha, client_id, body):
    from starlette.requests import Request

    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/client/control",
        "raw_path": b"/api/client/control",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("192.0.2.30", 5555),
        "server": ("hub", 8100),
        "scheme": "https",
        "app": types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    req = Request(scope, receive)
    req.state.auth_type, req.state.client_id = "client", client_id
    return req


async def test_rest_control_answers_like_the_socket(db, calls):
    await _home(db)
    guest = await _guest(db)
    ha = FakeHA()
    ok = await profiles_api.client_control(
        _rest(db, ha, guest, {"entity_id": "light.living", "service": "turn_on"})
    )
    assert ok["type"] == "control_ok"
    hidden = await profiles_api.client_control(
        _rest(db, ha, guest, {"entity_id": "light.office", "service": "turn_on"})
    )
    assert hidden.status_code == 403
    assert json.loads(hidden.body)["reason"] == "not_visible"
    keyed = await _device(db, profile="adult")
    refused = await profiles_api.client_control(
        _rest(db, ha, keyed, {"entity_id": "light.living", "service": "turn_on"})
    )
    assert refused.status_code == 403
    assert json.loads(refused.body)["reason"] == "not_hub_controlled"


# ── C1.7 grants ─────────────────────────────────────────────────────────────


async def test_a_forged_or_replayed_grant_gives_nothing(db, calls):
    """Non-children never get a grant; kids-check answers blocked for a
    device without parental controls, whatever grant it names."""
    await _home(db)
    guest = await _guest(db, remote=True)
    await _control(db, FakeHA(), guest, entity_id="light.living", service="turn_on")
    assert calls[-1]["grant"] is None
    forged = await enforcement.kids_check(
        db,
        FakeHA(),
        client_id=guest,
        entity_id="light.living",
        action="turn_on",
        grant="forged-grant-value-that-was-never-issued",
    )
    assert forged["allowed"] is False
    grant = enforcement.issue_grant("other-device", "light.living", "turn_on")
    assert not enforcement.consume_grant(grant, guest, "light.living", "turn_on")
    # Single use.
    assert not enforcement.consume_grant(
        grant, "other-device", "light.living", "turn_on"
    )


# ── C1.8 closed access ──────────────────────────────────────────────────────


async def test_closed_access_blocks_control_and_every_read(db, calls):
    await _home(db)
    guest = await _guest(db, access_switch=0)
    ha = FakeHA()
    blocked = await _control(db, ha, guest, entity_id="light.living", service="turn_on")
    assert blocked["type"] == "rule_blocked" and blocked["reason"] == "access_closed"
    for message in (
        {
            "type": "history_get",
            "request_id": "h",
            "entity_id": "light.living",
            "hours": 2,
        },
    ):
        sock = await _send(db, ha, guest, message)
        assert sock.last()["reason"] == "not_visible"
    assert calls == [] and ha.history_calls == []


async def test_closing_access_rotates_the_key_of_a_hub_device_with_remote_access(
    db, events
):
    from services import access

    guest = await _guest(db, remote=True)
    keyless = await _guest(db)
    await access.check_transitions(db)
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET access_switch = 0 WHERE id IN (?, ?)",
            (guest, keyless),
        )
    await access.check_transitions(db)
    assert await dp.rotation_pending(db, guest)
    # No key on the device: nothing to rotate.
    assert not await dp.rotation_pending(db, keyless)


# ── C1.9 the old key after the migration or after remote access ends ────────


async def test_lost_key_is_rotated_until_the_new_token_is_accepted(
    db, events, monkeypatch
):
    """Losing remote access on Hub only rotates the token; the old one counts
    as held (has_ha_token) until the hub accepted the new one, the retry
    loop repeats the rotation after a lost delivery and the withdrawal is
    logged once at the acceptance."""
    from api.pairing import clients as clients_api

    member = await _hub_member(db, remote=True)
    await dp.store_delivered_token(
        db, member, ha_user_id="u-m", refresh_token_id="rt-1", token="app-token"
    )
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_minted_at = ? WHERE id = ?",
            ("2026-01-01T08:00:00+00:00", member),
        )

    async def _remote_off():
        await store.put_override(
            db,
            member,
            "remote_access",
            value=False,
            user_choice=False,
            expires_at=None,
            note=None,
            actor="test",
        )

    _, change = await pchanges.run(db, _remote_off, dry_run=False)
    assert change.ha_key[member] == "removed"
    await pchanges.apply_effects(db, change)
    rotations = [
        m
        for m in events["integration"]
        if m["type"] == "provision_client" and m["data"]["client_id"] == member
    ]
    assert rotations and rotations[-1]["data"]["token_action"] == "rotate"
    assert await dp.stale_keys(db) == {member}
    assert (await clients_api.client_view(db, member)).has_ha_token is True
    assert (await _get_client_config(db, member, FakeHA()))["ha_token"] is None

    async def _logged():
        return await db.fetchall(
            "SELECT id FROM event_log WHERE kind = 'ha_key_withdrawn' AND client_id = ?",
            (member,),
        )

    assert await _logged() == []
    # The delivery got lost and the integration stayed silent past the TTL:
    # the retry loop starts the rotation again (a restart keeps key_lost_at).
    later = dp._now() + dp.ROTATION_TTL_SECONDS + 60
    monkeypatch.setattr(dp, "_now", lambda: later)
    events["integration"].clear()
    await dp.retry_once(db, connected=True)
    assert [m["data"]["token_action"] for m in events["integration"]] == ["rotate"]
    # Integration still away: the open rotation is kept, nothing new starts.
    assert await dp.secure_keyless_tokens(db) == 0
    await dp.store_delivered_token(
        db, member, ha_user_id="u-m", refresh_token_id="rt-2", token="hub-token"
    )
    assert len(await _logged()) == 1
    assert await dp.stale_keys(db) == set()
    assert (await clients_api.client_view(db, member)).has_ha_token is False
    assert await dp.secure_keyless_tokens(db) == 0


async def test_moving_a_member_to_hub_only_withdraws_the_key(db, events):
    member = await _device(db, profile="adult", remote=False)

    async def _to_hub():
        await store.put_override(
            db,
            member,
            "control_path",
            value="hub",
            user_choice=False,
            expires_at=None,
            note=None,
            actor="test",
        )

    _, dry = await pchanges.run(db, _to_hub, dry_run=True)
    assert dry.preview()["devices"][0]["ha_key"] == "removed"
    _, change = await pchanges.run(db, _to_hub, dry_run=False)
    await pchanges.apply_effects(db, change)
    row = await db.fetchone(
        "SELECT key_lost_at FROM paired_clients WHERE id = ?", (member,)
    )
    assert row["key_lost_at"]
    assert await dp.rotation_pending(db, member)


async def test_remote_access_on_hub_only_restores_the_key_without_rotation(db, events):
    member = await _hub_member(db)

    async def _remote_on():
        await store.put_override(
            db,
            member,
            "remote_access",
            value=True,
            user_choice=False,
            expires_at=None,
            note=None,
            actor="test",
        )

    _, change = await pchanges.run(db, _remote_on, dry_run=False)
    assert change.ha_key[member] == "restored"
    await pchanges.apply_effects(db, change)
    assert not await dp.rotation_pending(db, member)
    assert (await _get_client_config(db, member, FakeHA()))[
        "ha_token"
    ] == "device-token"


# ── C1.11 a watch ───────────────────────────────────────────────────────────


async def test_a_watch_secret_is_refused_on_the_hub_path(db, calls):
    await _home(db)
    watch = await _device(db, platform="watchos", profile="adult")
    ha = FakeHA()
    answer = await _control(db, ha, watch, entity_id="light.living", service="turn_on")
    assert answer["reason"] == "not_hub_controlled"
    for message in (
        {"type": "state_subscribe"},
        {
            "type": "history_get",
            "request_id": "h",
            "entity_id": "light.living",
            "hours": 2,
        },
    ):
        sock = await _send(db, ha, watch, message)
        assert sock.last()["reason"] == "not_hub_controlled"
    assert calls == []


async def test_a_watch_of_a_hub_only_phone_is_removed(db, events, monkeypatch):
    """A phone that moves to Hub only without remote access may carry no
    watch: the write removes it with reason hub_only."""
    from services import watch_access

    removed = []

    async def _remove(db_, watch_id, why, reason=None):
        removed.append((watch_id, why, reason))

    monkeypatch.setattr(watch_access, "_remove", _remove)
    phone = await _device(db, profile="adult", remote=False)
    watch = await _device(
        db,
        platform="watchos",
        profile="adult",
        set_up_by_client_id=phone,
        set_up_method="onboard",
        seal_key="k" * 43,
    )

    async def _to_hub():
        await store.put_override(
            db,
            phone,
            "control_path",
            value="hub",
            user_choice=False,
            expires_at=None,
            note=None,
            actor="test",
        )

    _, dry = await pchanges.run(db, _to_hub, dry_run=True)
    assert dry.preview()["watches_removed"][0]["reason"] == "hub_only"
    _, change = await pchanges.run(db, _to_hub, dry_run=False)
    await pchanges.apply_effects(db, change)
    assert removed == [(watch, "watch_removed_hub_only", "hub_only")]
    assert await watch_access.child_watch_allowed(db, phone) is False


# ── C1.12 no escalation ─────────────────────────────────────────────────────


async def test_a_hub_only_device_hands_out_no_key(db):
    mine = res.resolve(
        store.cat.catalog(),
        **store.device_inputs(
            {"platform": "ios", "access": "member"},
            {
                "body_json": json.dumps(
                    {"schema_version": 1, "rules": {"control_path": "hub"}}
                ),
                "child_protection": 0,
                "kind": "person",
            },
            [],
        ),
    )
    for value in ("checked", "direct"):
        new = {**mine, "control_path": {**mine["control_path"], "value": value}}
        assert "control_path" in onboarding.stronger_rules(new, mine)
    assert "control_path" not in onboarding.stronger_rules(mine, mine)
    # With remote access the phone holds a read-only key: a TV or a watch
    # (Checked there) is no escalation, Direct still is.
    remote = {**mine, "remote_access": {**mine["remote_access"], "value": True}}
    tv = {**mine, "control_path": {**mine["control_path"], "value": "checked"}}
    assert "control_path" not in onboarding.stronger_rules(tv, remote)
    full = {**mine, "control_path": {**mine["control_path"], "value": "direct"}}
    assert "control_path" in onboarding.stronger_rules(full, remote)


# ── C1.2, C1.13, C1.14 reads ────────────────────────────────────────────────


async def test_state_subscribe_relays_only_the_scope_without_tokens(db):
    await _home(db)
    guest = await _guest(db)
    ha = FakeHA()
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        sock = await _send(db, ha, guest, {"type": "state_subscribe"})
    finally:
        state_relay._ensure_running = original
    assert sock.sent[0]["type"] == "state_subscribed"
    snapshot = {m["entity_id"]: m["state"] for m in sock.sent[1:]}
    assert set(snapshot) == {
        "light.living",
        "camera.door",
        "media_player.tv",
        "weather.home",
        "scene.evening",
        "sun.sun",
    } - {"sun.sun"}
    door = snapshot["camera.door"]["attributes"]
    assert "access_token" not in door and "entity_picture" not in door
    assert await state_relay.dispatch_event("light.office", {"state": "on"}) == 0
    assert await state_relay.dispatch_event("person.mama", {"state": "home"}) == 0
    # A new subscription within 10 s: rate_limited with retry_after.
    again = await _send(db, ha, guest, {"type": "state_subscribe"})
    assert again.last()["reason"] == "rate_limited"
    assert 1 <= again.last()["retry_after"] <= 10
    state_relay.unsubscribe(sock)


async def test_relay_drops_an_entity_after_the_scope_changed(db):
    living, _office = await _home(db)
    guest = await _guest(db)
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        sock = await _send(db, FakeHA({}), guest, {"type": "state_subscribe"})
    finally:
        state_relay._ensure_running = original
    assert await state_relay.dispatch_event("light.living", {"state": "on"}) == 1
    async with db.transaction():
        await db.execute("DELETE FROM area_tags WHERE area_id = ?", (living,))
    for sub in state_relay._subscribers.values():
        sub.refreshed_at = 0.0
    assert await state_relay.dispatch_event("light.living", {"state": "off"}) == 0
    state_relay.unsubscribe(sock)


async def test_snapshot_covers_every_visible_entity(db):
    entities = [f"sensor.s{i:04d}" for i in range(700)]
    await _area(db, "Gross", entities, tags=["gast"])
    guest = await _guest(db)
    ha = FakeHA({e: {"entity_id": e, "state": "1", "attributes": {}} for e in entities})
    original = state_relay._ensure_running
    state_relay._ensure_running = lambda: None
    try:
        sock = await _send(db, ha, guest, {"type": "state_subscribe"})
    finally:
        state_relay._ensure_running = original
    assert sock.sent[0] == {"type": "state_subscribed", "entity_count": 700}
    assert len(sock.sent) == 701
    state_relay.unsubscribe(sock)


async def test_history_and_forecast_follow_the_scope_and_the_limit(db):
    await _home(db)
    guest = await _guest(db)
    ha = FakeHA()

    async def read(message):
        return (await _send(db, ha, guest, message)).last()

    ok = await read(
        {
            "type": "history_get",
            "request_id": "h1",
            "entity_id": "light.living",
            "hours": 2,
        }
    )
    assert ok["type"] == "history"
    forecast = await read(
        {
            "type": "forecast_get",
            "request_id": "f1",
            "entity_id": "weather.home",
            "forecast_type": "daily",
        }
    )
    assert forecast["type"] == "forecast"
    for entity_id in ("light.office", "sun.sun", "person.mama"):
        hidden = await read(
            {
                "type": "history_get",
                "request_id": "h2",
                "entity_id": entity_id,
                "hours": 2,
            }
        )
        assert hidden["reason"] == "not_visible"
    for _ in range(hub_control.READS_PER_WINDOW - 5):
        await read(
            {
                "type": "history_get",
                "request_id": "h3",
                "entity_id": "light.living",
                "hours": 1,
            }
        )
    limited = await read(
        {
            "type": "history_get",
            "request_id": "h4",
            "entity_id": "light.living",
            "hours": 1,
        }
    )
    assert limited["type"] == "control_error" and limited["reason"] == "rate_limited"
    assert limited["request_id"] == "h4" and limited["retry_after"] >= 1
    assert ha.history_calls.count("light.office") == 0


async def test_keyed_devices_off_the_hub_path_get_no_reads(db):
    await _home(db)
    keyed = await _device(db, profile="adult", tags=["gast"])
    ha = FakeHA()
    for message in (
        {"type": "state_subscribe"},
        {
            "type": "history_get",
            "request_id": "h",
            "entity_id": "light.living",
            "hours": 2,
        },
    ):
        sock = await _send(db, ha, keyed, message)
        assert sock.last()["reason"] == "not_hub_controlled"


async def test_parallel_controls_of_one_device_are_serialized(db, monkeypatch):
    await _home(db)
    guest = await _guest(db)
    running = []
    most = []

    async def _slow(_ha, token, entity_id, action, data, grant):
        running.append(entity_id)
        most.append(len(running))
        await asyncio.sleep(0.05)
        running.remove(entity_id)
        return {"entity_id": entity_id, "ok": True}

    monkeypatch.setattr(hub_control, "ha_caller", _slow)
    answers = await asyncio.gather(
        *(
            hub_control.control(
                db,
                FakeHA(),
                guest,
                {
                    "request_id": f"r{i}",
                    "entity_id": "light.living",
                    "service": "toggle",
                },
            )
            for i in range(3)
        )
    )
    assert all(a["type"] == "control_ok" for a in answers)
    assert max(most) == 1


# ── location ────────────────────────────────────────────────────────────────


async def test_location_report_is_open_to_the_hub_path_only(db):
    from services import location

    from datetime import datetime, timezone

    guest = await _guest(db)
    keyed = await _device(db, profile="adult")
    fix = {
        "latitude": 47.0,
        "longitude": 8.0,
        "accuracy": 10.0,
        "background": False,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with pytest.raises(location.LocationError) as e:
        await location.report(db, keyed, fix, via="ws")
    assert e.value.reason == "not_hub_controlled"
    try:
        await location.report(db, guest, fix, via="ws")
    except location.LocationError as err:
        # The rule decides for a device on the hub path, not the path.
        assert err.reason != "not_hub_controlled"


# ── integration status ──────────────────────────────────────────────────────


async def test_integration_status_names_the_key_of_hub_devices(db):
    guest = await _guest(db)
    member = await _hub_member(db, remote=True)
    req = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
    )
    status = await integration_api.integration_status(req)
    by_id = {c["id"]: c for c in status["clients"]}
    assert by_id[guest]["has_ha_token"] is False
    assert by_id[member]["has_ha_token"] is True
    assert by_id[member]["remote_allowed"] is True


async def test_hub_read_module_answers_not_hub_controlled_for_unknown(db):
    answer = await hub_read.history(
        db,
        FakeHA(),
        "no-such-device",
        {"request_id": "h", "entity_id": "light.x", "hours": 1},
    )
    assert answer["reason"] == "not_hub_controlled"


async def test_starter_profiles_and_defaults_of_the_hub_path(db):
    for suggestion, path in (("guest", "hub"), ("child", "hub"), ("adult", "checked")):
        row = await store.profile_row(db, await starter_profile(db, suggestion))
        assert store.body_of(row)["rules"]["control_path"] == path


async def test_device_list_names_the_control_path_and_the_key(db, events):
    """GET /api/pairing/clients: control_path after the platform mapping and
    has_ha_token for every key state."""
    from api.pairing import clients as clients_api

    keyless = await _guest(db)
    remote = await _hub_member(db, remote=True)
    checked = await _device(db, profile="adult", remote=False)
    direct = await _device(db, profile="adult", overrides={"control_path": "direct"})
    tv = await _device(db, platform="tvos", overrides={"control_path": "hub"})
    pending = await _hub_member(db, remote=True, token=None)

    async def view(cid):
        v = await clients_api.client_view(db, cid)
        return v.control_path, v.has_ha_token, v.ha_token_status

    assert await view(keyless) == ("hub", False, "active")
    assert await view(remote) == ("hub", True, "active")
    assert await view(checked) == ("checked", True, "active")
    assert await view(direct) == ("direct", True, "active")
    # A TV never runs without a key: hub becomes checked.
    assert await view(tv) == ("checked", True, "active")
    # Being set up: no token yet.
    assert await view(pending) == ("hub", False, "pending")
    # Being revoked: the old key works until the rotated token is accepted.
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_minted_at = ? WHERE id = ?",
            ("2026-01-01T08:00:00+00:00", checked),
        )

    async def _to_hub():
        await store.put_override(
            db,
            checked,
            "control_path",
            value="hub",
            user_choice=False,
            expires_at=None,
            note=None,
            actor="test",
        )

    _, change = await pchanges.run(db, _to_hub, dry_run=False)
    await pchanges.apply_effects(db, change)
    assert await view(checked) == ("hub", True, "active")
    await dp.store_delivered_token(
        db, checked, ha_user_id="u-c", refresh_token_id="rt-c", token="hub-token"
    )
    assert await view(checked) == ("hub", False, "active")
    listed = {v.id: v.control_path for v in await clients_api.client_views(db)}
    assert listed[tv] == "checked" and listed[keyless] == "hub"


async def test_end_of_a_temporary_checked_withdraws_keys_and_the_watch(
    db, events, monkeypatch
):
    """A phone on Hub only with a temporary Checked: while it runs the phone
    and its watch hold keys and quick access and the watch carry its end;
    when it ends the hub withdraws the phone's key, removes the watch (reason
    hub_only) and tells the devices."""
    from datetime import datetime, timedelta, timezone

    from services import watch_access
    from services.profiles import expiry_task

    removed = []

    async def _remove(db_, watch_id, why, reason=None):
        removed.append((watch_id, reason))

    monkeypatch.setattr(watch_access, "_remove", _remove)
    from services.profiles import catalog
    from services.profiles import rows as profile_rows

    body = catalog.materialize("adult")
    body["rules"].update(control_path="hub", remote_access=False)
    async with db.transaction():
        hub_profile = await profile_rows.insert_profile(
            db, name="Nur Hub", body=body, now=Database.now(), actor=None
        )
    phone = await _device(db, profile_id=hub_profile)
    watch = await _device(
        db,
        platform="watchos",
        profile="adult",
        set_up_by_client_id=phone,
        set_up_method="onboard",
        seal_key="k" * 43,
    )
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_minted_at = ? WHERE id = ?",
            ("2026-01-01T08:00:00+00:00", phone),
        )
    end = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)

    async def _temporary_checked():
        await store.put_override(
            db,
            phone,
            "control_path",
            value="checked",
            user_choice=False,
            expires_at=end.isoformat(),
            note=None,
            actor="test",
        )

    _, change = await pchanges.run(db, _temporary_checked, dry_run=False)
    await pchanges.apply_effects(db, change)
    cfg = await _get_client_config(db, phone, FakeHA())
    assert cfg["ha_token"] == "device-token"
    assert cfg["rules"]["watch"]["value"] is True
    assert (
        cfg["rules"]["watch"]["expires_at"] and cfg["rules"]["watch"]["then"] is False
    )
    watch_cfg = await _get_client_config(db, watch, FakeHA(), sealed_for_watch=True)
    assert watch_cfg["ha_token"] == "device-token"

    events["to"].clear()
    assert await expiry_task.expire_due(db, now=end + timedelta(seconds=1)) == [phone]
    assert removed == [(watch, "hub_only")]
    assert await dp.rotation_pending(db, phone)
    assert await dp.stale_keys(db) == {phone}
    cfg = await _get_client_config(db, phone, FakeHA())
    assert cfg["ha_token"] is None and cfg["rules"]["watch"]["value"] is False
    assert (phone, {"type": "client_config_updated", "data": {}}) in events["to"]
    # Even before the removal lands, the watch gets no key any more.
    watch_cfg = await _get_client_config(db, watch, FakeHA(), sealed_for_watch=True)
    assert watch_cfg["ha_token"] is None and watch_cfg["ha_enforcement"] is None


def _contract_checks():
    """``client_config_checks`` of the contract's tools/validate.py (the
    semantic checks of client_config), None without a checkout."""
    import importlib.util
    import os
    from pathlib import Path

    root = os.environ.get("CONTRACTS_DIR")
    path = Path(root or "") / "tools/validate.py"
    if not root or not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("contract_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_client_configs_pass_the_contract_semantic_checks(db):
    """Every key state the hub sends passes validate.client_config_checks:
    no key, a read-only key, a full key, a key whose token has not arrived
    (token_error pending), a closed access; the checker itself rejects the
    contract's case of a key without token state."""
    import os
    from pathlib import Path

    validate = _contract_checks()
    if validate is None:
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    await _home(db)
    devices = {
        "guest": await _guest(db),
        "hub_member": await _hub_member(db),
        "hub_remote": await _hub_member(db, remote=True),
        "checked": await _device(db, profile="adult", remote=False),
        "direct": await _device(
            db, profile="adult", overrides={"control_path": "direct"}
        ),
        "pending": await _device(db, profile="adult", token=None),
        "hub_pending": await _hub_member(db, remote=True, token=None),
        "closed": await _device(db, profile="adult", access_switch=0),
        "child": await _device(db, profile="child"),
        "tv": await _device(db, platform="tvos"),
    }
    for name, cid in devices.items():
        cfg = await _get_client_config(db, cid, FakeHA())
        assert validate.client_config_checks(cfg) == [], (name, cfg.get("token_error"))
    pending = await _get_client_config(db, devices["pending"], FakeHA())
    assert pending["ha_token"] is None and pending["token_error"] == "pending"
    assert "token_error" not in await _get_client_config(db, devices["guest"], FakeHA())
    fixtures = Path(os.environ["CONTRACTS_DIR"]) / "fixtures/client-config"
    for name in (
        "invalid-semantic-key-without-token-state.json",
        "invalid-semantic-pending-with-enforcement.json",
        "invalid-semantic-hub-pending-with-instance-id.json",
    ):
        bad = json.loads((fixtures / name).read_text())
        assert validate.client_config_checks(bad) != [], name
    good = json.loads((fixtures / "member-hub-remote-pending.json").read_text())
    assert validate.client_config_checks(good) == []
    # The hub's own pending Hub only device with remote access has the same
    # key fields as the contract's case.
    mine = await _get_client_config(db, devices["hub_pending"], FakeHA())
    for field in ("ha_token", "ha_enforcement", "ha_instance_id", "token_error"):
        assert mine.get(field) == good.get(field), field


async def test_a_pending_key_turns_into_the_token_with_a_config_update(
    db, events, monkeypatch
):
    """Holds a key, no token yet: token_error pending, ha_token and
    ha_enforcement null (on the hub path also ha_instance_id). The token
    the integration delivers is sent with client_config_updated."""
    from models.schemas import ClientTokenDelivery

    await db.set_setting("ha_instance_id", "0" * 32)
    member = await _hub_member(db, remote=True, token=None)
    cfg = await _get_client_config(db, member, FakeHA())
    assert cfg["token_error"] == "pending"
    assert cfg["ha_token"] is None and cfg["ha_enforcement"] is None
    assert cfg["ha_instance_id"] is None
    monkeypatch.setattr(enforcement, "_send", lambda _m: asyncio.sleep(0, True))
    req = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
    )
    await integration_api.integration_client_token(
        req,
        ClientTokenDelivery(
            client_id=member,
            ha_user_id="e" * 32,
            refresh_token_id="rt-1",
            token="t" * 40,
        ),
    )
    if enforcement._push_task is not None:
        enforcement._push_task.cancel()
    assert (member, {"type": "client_config_updated", "data": {}}) in events["to"]
    cfg = await _get_client_config(db, member, FakeHA())
    assert cfg["ha_token"] == "t" * 40 and "token_error" not in cfg
    assert cfg["ha_enforcement"]["control_path"] == "checked"
    assert cfg["ha_instance_id"] == "0" * 32


async def test_start_removes_watches_their_phones_no_longer_allow(
    db, events, monkeypatch
):
    """At start every watch whose phone's rules no longer allow it goes (a
    missed write or expiry, the upgrade to Hub only); its HA user is
    removed with it. Allowed watches stay; a second run removes nothing."""
    from services import watch_access

    async def _watch(phone):
        return await _device(
            db,
            platform="watchos",
            profile="adult",
            set_up_by_client_id=phone,
            set_up_method="onboard",
            seal_key="k" * 43,
        )

    hub_phone = await _hub_member(db)
    keyed_phone = await _device(db, profile="adult", remote=False)
    remote_phone = await _hub_member(db, remote=True)
    gone = await _watch(hub_phone)
    kept = await _watch(keyed_phone)
    lifted = await _watch(remote_phone)
    deprovisioned = []

    async def _deprovision(client_id, reason):
        deprovisioned.append(client_id)

    monkeypatch.setattr(dp, "send_deprovision", _deprovision)
    assert await watch_access.remove_disallowed(db) == [gone]
    assert await watch_access.remove_disallowed(db) == []
    assert deprovisioned == [gone]
    rows = {r["id"] for r in await db.fetchall("SELECT id FROM paired_clients")}
    assert gone not in rows and {kept, lifted} <= rows
    # The phone hears why.
    told = [m for cid, m in events["to"] if cid == hub_phone]
    assert told[-1] == {
        "type": "watch_access",
        "client_id": gone,
        "status": "gone",
        "reason": "hub_only",
    }


async def test_start_removes_a_childs_watch_without_kids_watch(db, events, monkeypatch):
    from services import watch_access

    phone = await _device(db, profile="child")
    watch = await _device(
        db,
        platform="watchos",
        profile="adult",
        set_up_by_client_id=phone,
        set_up_method="onboard",
        seal_key="k" * 43,
    )

    async def _deprovision(client_id, reason):
        return None

    monkeypatch.setattr(dp, "send_deprovision", _deprovision)
    assert await watch_access.remove_disallowed(db) == [watch]
    reason = [m for cid, m in events["to"] if cid == phone][-1]["reason"]
    assert reason in ("remote_access_off", "kids_watch_off")


async def test_area_detail_carries_no_hidden_placement(db):
    area = await _area(
        db,
        "Wohnzimmer",
        ["light.living"],
        tags=["gast"],
        hidden=["camera.garage", "person.mama"],
    )
    guest = await _guest(db)
    msg = (
        await _send(db, FakeHA(), guest, {"type": "get_area", "area_id": area})
    ).last()
    assert msg["type"] == "area"
    ids = [e["entity_id"] for s in msg["data"]["sections"] for e in s["entities"]]
    assert ids == ["light.living"]


async def test_relay_follows_a_shrinking_scope_at_once(db):
    """The scope shrinks (area tags, a profile write, access): the next state
    is checked against the new scope, not after the 30 s refresh."""
    import importlib

    ws_broadcast = importlib.import_module("api.ws.broadcast")
    area = await _area(db, "Wohnzimmer", ["light.living"], tags=["gast"])
    guest = await _guest(db)
    sock = _WS(db, FakeHA(), guest)
    sent = []

    async def _sink(message):
        sent.append(message)

    await state_relay.subscribe(sock, db, FakeHA(), guest, _sink, start=False)
    assert await state_relay.dispatch_event("light.living", {"state": "on"}) == 1
    async with db.transaction():
        await db.execute("DELETE FROM area_tags WHERE area_id = ?", (area,))
    # Every admin change of areas or profiles is broadcast.
    await ws_broadcast.broadcast({"type": "areas_changed", "data": {}})
    assert await state_relay.dispatch_event("light.living", {"state": "off"}) == 0
    async with db.transaction():
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, 'gast')", (area,)
        )
    state_relay.rescope()
    assert await state_relay.dispatch_event("light.living", {"state": "on"}) == 1
    # Access closes: the device hears client_config_updated, its scope is empty.
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET access_switch = 0 WHERE id = ?", (guest,)
        )
    await ws_broadcast.send_to_client(
        guest, {"type": "client_config_updated", "data": {}}
    )
    assert await state_relay.dispatch_event("light.living", {"state": "off"}) == 0
    state_relay.unsubscribe(sock)


async def test_a_device_whose_rules_cannot_be_resolved_is_shown_as_such(
    db, monkeypatch
):
    """Never shown as Hub only without a key: control_path null, and
    has_ha_token says whether a token exists at all."""
    from api.pairing import clients as clients_api

    guest = await _guest(db)
    original = store.resolve_all

    async def _without(db_, client_ids=None, *a, **k):
        out = await original(db_, client_ids, *a, **k)
        out.pop(guest, None)
        return out

    monkeypatch.setattr(store, "resolve_all", _without)
    view = await clients_api.client_view(db, guest)
    assert view.control_path is None and view.has_ha_token is True
    assert view.remote_allowed is None
    monkeypatch.setattr(store, "resolve_all", original)
    view = await clients_api.client_view(db, guest)
    assert view.control_path == "hub" and view.has_ha_token is False


async def test_the_cause_of_unresolved_rules_is_logged_once(db, monkeypatch, caplog):
    import logging

    from api.pairing import clients as clients_api

    guest = await _guest(db)
    original = store.resolve_all

    async def _broken(db_, client_ids=None, *a, **k):
        if client_ids and guest in client_ids:
            raise ValueError("unknown profile body version 9")
        return await original(db_, client_ids, *a, **k)

    monkeypatch.setattr(store, "resolve_all", _broken)
    clients_api._unresolved_logged.clear()
    with caplog.at_level(logging.WARNING, logger="hub.api.pairing"):
        for _ in range(3):
            view = await clients_api.client_view(db, guest)
            assert view.control_path is None
    lines = [
        r.getMessage() for r in caplog.records if "cannot be resolved" in r.getMessage()
    ]
    assert len(lines) == 1 and "ValueError" in lines[0] and guest in lines[0]


async def test_an_unexpected_control_failure_keeps_the_request_id(db, monkeypatch):
    guest = await _guest(db)

    async def _boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(hub_control, "control", _boom)
    answer = await _control(db, FakeHA(), guest, entity_id="light.x", service="turn_on")
    assert answer == {
        "type": "control_error",
        "request_id": "r1",
        "reason": "ha_error",
    }


async def test_one_failing_watch_does_not_stop_the_others(db, events, monkeypatch):
    from services import watch_access

    phone = await _hub_member(db)
    watches = [
        await _device(
            db,
            platform="watchos",
            profile="adult",
            set_up_by_client_id=phone,
            set_up_method="onboard",
            seal_key="k" * 43,
        )
        for _ in range(2)
    ]
    calls = []

    async def _remove(db_, watch_id, why, reason=None):
        calls.append(watch_id)
        if len(calls) == 1:
            raise RuntimeError("Home Assistant not reachable")

    monkeypatch.setattr(watch_access, "_remove", _remove)
    removed = await watch_access.remove_disallowed(db)
    assert len(calls) == 2 and removed == [calls[1]]
    assert sorted(calls) == sorted(watches)


async def _relayed(db, change):
    """States relayed for light.living after ``change(area)``."""
    ha = FakeHA()
    area = await _area(db, "Wohnzimmer", ["light.living"], tags=["gast"])
    guest = await _guest(db)
    sock = _WS(db, ha, guest)

    async def _sink(_m):
        pass

    await state_relay.subscribe(sock, db, ha, guest, _sink, start=False)
    await change(area)
    n = await state_relay.dispatch_event(
        "light.living", {"state": "on", "attributes": {}}
    )
    state_relay.unsubscribe(sock)
    return n


async def test_relay_stops_at_once_when_an_area_is_switched_off(db):
    async def _off(area):
        async with db.transaction():
            await db.execute("UPDATE areas SET is_active = 0 WHERE id = ?", (area,))
        await ws.announce_area_active(db, area, False)

    assert await _relayed(db, _off) == 0


async def test_relay_stops_at_once_when_a_placement_is_hidden(db):
    async def _hide(area):
        async with db.transaction():
            await db.execute(
                "UPDATE area_entities SET is_visible = 0 WHERE area_id = ?", (area,)
            )
        await ws.broadcast_area_scoped(
            db, area, {"type": "entity_updated", "data": {"area_id": area}}
        )

    assert await _relayed(db, _hide) == 0


async def test_a_watch_removal_cut_short_is_completed(db, events, monkeypatch):
    """A removal stopped by its time bound: deleted already → the
    deprovision goes out at once; not deleted yet → the next run does it."""
    import asyncio

    from services import watch_access

    phone = await _hub_member(db)
    watch = await _device(
        db,
        platform="watchos",
        profile="adult",
        set_up_by_client_id=phone,
        set_up_method="onboard",
        seal_key="k" * 43,
    )
    sent = []

    async def _deprovision(client_id, reason):
        sent.append(client_id)

    monkeypatch.setattr(dp, "send_deprovision", _deprovision)
    monkeypatch.setattr(watch_access, "REMOVE_SECONDS", 0.05)

    async def _stuck_before_delete(*_a, **_k):
        await asyncio.sleep(1)

    monkeypatch.setattr(watch_access, "_remove", _stuck_before_delete)
    assert await watch_access.remove_disallowed(db) == []
    assert sent == []

    async def _stuck_after_delete(db_, watch_id, *_a, **_k):
        async with db_.transaction():
            await db_.execute("DELETE FROM paired_clients WHERE id = ?", (watch_id,))
        await asyncio.sleep(1)

    monkeypatch.setattr(watch_access, "_remove", _stuck_after_delete)
    assert await watch_access.remove_disallowed(db) == [watch]
    assert sent == [watch]
    assert await watch_access.remove_disallowed(db) == []


async def test_a_tv_assigned_to_hub_only_shows_its_key_and_why(db):
    """An admin assigns a TV to a profile on Hub only after pairing: the TV
    holds a read-only key (Hub only resolves to Checked there), and both the
    device list and the effective rules say so, with the reason."""
    from api.pairing import clients as clients_api

    from services.profiles import catalog
    from services.profiles import rows as profile_rows

    body = catalog.materialize("adult")
    body["rules"].update(control_path="hub", remote_access=False)
    async with db.transaction():
        hub = await profile_rows.insert_profile(
            db, name="Nur Hub TV", body=body, now=Database.now(), actor=None
        )
    tv = await _device(db, platform="tvos")
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET profile_id = ? WHERE id = ?", (hub, tv)
        )
    view = await clients_api.client_view(db, tv)
    assert view.control_path == "checked" and view.has_ha_token is True
    effective = await store.effective_view(db, tv)
    cp = effective["rules"]["control_path"]
    assert (cp["value"], cp["reason"], cp["profile_value"]) == (
        "checked",
        "platform_needs_key",
        "hub",
    )


async def test_a_new_device_without_rules_gets_not_ready(db, monkeypatch):
    """Right after pairing the integration may not hold the device's rules
    yet and refuses the call (HTTP 400): the device hears not_ready, never
    ha_unavailable, and the hub pushes the rules first."""
    await _home(db)
    guest = await _guest(db)
    pushed = []

    async def _push_now(reason, timeout=3.0):
        pushed.append(reason)
        return False

    async def _refused(*_a, **_k):
        raise hub_control.ControlFailure(hub_control.ERR_HA_UNAVAILABLE, 400)

    monkeypatch.setattr(enforcement, "push_now", _push_now)
    monkeypatch.setattr(hub_control, "ha_caller", _refused)
    answer = await _control(
        db, FakeHA(), guest, entity_id="light.living", service="turn_on"
    )
    assert answer["reason"] == "not_ready"
    assert pushed == ["hub_control"]
    # Once the rules are there, a refusal of Home Assistant stays what it is.
    monkeypatch.setattr(enforcement, "_pushed", {guest: "hash"})
    answer = await _control(
        db, FakeHA(), guest, entity_id="light.living", service="turn_on"
    )
    assert answer["reason"] == "ha_unavailable"
    # While the new token is held back for the first push: not_ready.
    with enforcement.token_held(guest):
        answer = await _control(
            db, FakeHA(), guest, entity_id="light.living", service="turn_on"
        )
    assert answer["reason"] == "not_ready"


async def test_reads_answer_ha_unavailable_when_home_assistant_is_down(db):
    import httpx

    await _home(db)
    guest = await _guest(db)

    class _Refused(Exception):
        """Like websockets' InvalidStatus: the proxy answered 502."""

        def __init__(self):
            super().__init__("server rejected WebSocket connection: HTTP 502")
            self.response = types.SimpleNamespace(status_code=502)

    class DownHA(FakeHA):
        async def get_history(self, *a, **k):
            raise _Refused()

        async def get_forecasts(self, *a, **k):
            raise ConnectionError("down")

    ha = DownHA()
    history = (
        await _send(
            db,
            ha,
            guest,
            {
                "type": "history_get",
                "request_id": "h",
                "entity_id": "light.living",
                "hours": 1,
            },
        )
    ).last()
    assert history["reason"] == "ha_unavailable"
    forecast = (
        await _send(
            db,
            ha,
            guest,
            {
                "type": "forecast_get",
                "request_id": "f",
                "entity_id": "weather.home",
                "forecast_type": "daily",
            },
        )
    ).last()
    assert forecast["reason"] == "ha_unavailable"
    request = httpx.Request("GET", "http://supervisor/core/api/x")
    for status, want in ((502, True), (503, True), (504, True), (500, False)):
        err = httpx.HTTPStatusError(
            "x", request=request, response=httpx.Response(status, request=request)
        )
        assert hub_control.unreachable(err) is want
    assert hub_control.unreachable(RuntimeError("Command failed")) is False


async def test_a_withdrawn_key_goes_at_once(db, events, monkeypatch):
    """A device that holds no key gets its rotated token stored without the
    switch-over wait: the integration removes the old token right after the
    answer. A real rotation keeps the wait."""
    from models.schemas import ClientTokenDelivery

    waited = []

    async def _deliver(client_id, grace_seconds=None):
        waited.append(client_id)
        return True

    monkeypatch.setattr(ws, "deliver_rotated_config", _deliver)
    monkeypatch.setattr(enforcement, "_send", lambda _m: asyncio.sleep(0, True))
    req = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
    )
    keyless = await _guest(db, ha_user_id="a" * 32)
    keyed = await _device(db, profile="adult", ha_user_id="b" * 32)
    for cid, user in ((keyless, "a" * 32), (keyed, "b" * 32)):
        await integration_api.integration_client_token(
            req,
            ClientTokenDelivery(
                client_id=cid,
                ha_user_id=user,
                refresh_token_id="rt-" + cid,
                token="t" * 40,
            ),
        )
    if enforcement._push_task is not None:
        enforcement._push_task.cancel()
    assert waited == [keyed]
    assert (keyless, {"type": "client_config_updated", "data": {}}) in events["to"]


async def test_deleting_an_area_removes_its_switch_state(monkeypatch):
    from services import ha_sensors

    deleted, posted = [], []

    async def _delete(_ha, entity_id):
        deleted.append(entity_id)

    async def _post(_ha, entity_id, payload):
        posted.append(entity_id)

    monkeypatch.setattr(ha_sensors, "_delete_state", _delete)
    monkeypatch.setattr(ha_sensors, "_post_state", _post)
    ha = types.SimpleNamespace()
    await ha_sensors.delete_area_switch(ha, "Wohnzimmer", "area-1")
    assert posted == [] and len(deleted) == 1
    assert deleted[0].startswith("input_boolean.")
