"""Family manager: right, scope, kids rules as device adjustments,
releases, requests of the children (first answer wins, timeout, limits),
HA notifications with answer buttons, activity, admin and integration REST.

Contract: ``docs/family.md``, vectors ``fixtures/profiles/family-check``.
"""

import json
import time
import types
import uuid
from datetime import timedelta
from pathlib import Path

import api.websocket as ws
import pytest
from api import family as family_api
from api.ws.client import _get_client_config, _handle_client_message
from fastapi import HTTPException
from models.database import SCHEMA_VERSION, Database
from namespace import FAMILY_ACTION_PREFIX
from services import crypto, hub_control, kids_mode, tasks
from services.family import check, events, notify, service
from services.family import store as fstore
from services.profiles import store as pstore
from starlette.websockets import WebSocketState

from .profile_helpers import add_client, starter_profile

VECTORS = Path(__file__).parent / "fixtures" / "profiles" / "family-check"
ALL_DAY = [
    {
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "start": "00:00",
        "end": "00:00",
    }
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    ws._clients.clear()
    kids_mode.reset()
    service.limiter.reset()
    service.reset_babysitters_seen()
    events.reset()
    notify.reset()
    yield
    ws._clients.clear()
    kids_mode.reset()
    service.limiter.reset()
    events.reset()
    notify.reset()
    crypto.reset_cache()


@pytest.fixture
def pushed(monkeypatch):
    log = {"broadcast": [], "to": [], "integration": []}

    async def _broadcast(message, *a, **k):
        log["broadcast"].append(message)

    async def _send_to(client_id, message):
        log["to"].append((client_id, message))
        return True

    async def _send_int(message):
        log["integration"].append(message)
        return True

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    monkeypatch.setattr(ws, "send_to_integration", _send_int)
    return log


@pytest.fixture
def calls(monkeypatch):
    log = []

    async def _caller(_ha, token, entity_id, service_name, data, grant):
        assert grant
        log.append(
            (entity_id.split(".", 1)[0], service_name, {**data, "entity_id": entity_id})
        )
        return {}

    monkeypatch.setattr(hub_control, "ha_caller", _caller)
    return log


class FakeHA:
    def __init__(self, states=None):
        self.states = states or {}
        self.services = []

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def get_config(self):
        return {"time_zone": "Europe/Zurich"}

    async def get_core_info(self):
        return {"port": 80, "ssl": False}

    def invalidate_state_cache(self, entity_id=None):
        pass

    async def call_service(self, domain, service_name, data=None, target=None):
        self.services.append((domain, service_name, data))
        return []


class _WS:
    def __init__(self, db, ha, client_id):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha, app_url="")
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host="192.0.2.30")
        self.sent = []
        self.application_state = WebSocketState.CONNECTED
        ws._clients[self] = {
            "authenticated": True,
            "client_id": client_id,
            "connected_at": time.time(),
        }

    async def send_json(self, payload):
        self.sent.append(payload)


def _request(db, ha=None, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth, client_id=None, actor="Admin One"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
        client=None,
    )


async def _area(db, name, entities):
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
            await db.execute(
                "INSERT INTO area_entities (id, area_id, section_id, entity_id, "
                "sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), area_id, section_id, eid, i, now),
            )
    return area_id


async def _house(db, *, manager_rule=True, child_overrides=None):
    """Manager One (adult iPhone with the right), Child One (child iPad), Child Two (child iPhone)."""
    manager_one = await add_client(
        db,
        name="iPhone Manager One",
        owner_label="Manager One",
        platform="ios",
        profile="adult",
        overrides={"family_manager": True} if manager_rule else None,
        last_seen=Database.now(),
    )
    token = await crypto.encrypt_token(db, "child_one-token")
    child_one = await add_client(
        db,
        name="iPad Child One",
        owner_label="Child One",
        platform="ipados",
        profile="child",
        overrides=child_overrides,
        ha_token_encrypted=token,
        ha_token_status="active",
        last_seen=Database.now(),
    )
    child_two = await add_client(
        db,
        name="iPhone Child Two",
        owner_label="Child Two",
        platform="ios",
        profile="child",
        last_seen=Database.now(),
    )
    return manager_one, child_one, child_two


async def _msg(db, ha, client_id, message, route="home"):
    return await service.handle_manager_message(db, ha, client_id, message, route)


# ── reference vectors ────────────────────────────────────────────────────────


def test_family_check_vectors_match_the_contract():
    files = sorted(
        p for p in VECTORS.glob("*.json") if not p.name.startswith("invalid")
    )
    assert files
    n = 0
    for path in files:
        vector = json.loads(path.read_text())
        for case in vector["cases"]:
            got = check.family_check(case["manager"], case["child"], case["op"])
            exp = case["expected"]
            assert got == (exp["allowed"], exp["reason"]), (
                f"{path.name}: {case['name']}"
            )
            n += 1
    assert n >= 30


# ── right and client_config ─────────────────────────────────────────────────


async def test_client_config_family_exactly_with_the_right(db, pushed):
    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)
    member_two = await add_client(
        db, name="iPhone Member Two", platform="ios", profile="adult"
    )
    guest = await add_client(db, name="Gast", guest=True, overrides=None)

    cfg = await _get_client_config(db, manager_one, ha)
    fam = cfg["family"]
    assert fam["scope"] == {"all": True, "client_ids": []}
    assert {c["client_id"] for c in fam["children"]} == {child_one, child_two}
    assert fam["open_requests"] == 0
    assert fam["notify"]["kids_release_requested"] is True
    assert fam["remote"] == "away"
    assert "family" not in await _get_client_config(db, member_two, ha)
    assert "family" not in await _get_client_config(db, guest, ha)
    child_cfg = await _get_client_config(db, child_one, ha)
    assert "family" not in child_cfg
    assert child_cfg["kids"]["releases"] == []
    req = child_cfg["kids"]["requests"]
    assert req["enabled"] is True
    assert req["recipients"] == [{"name": "Manager One", "role": "family_manager"}]
    assert req["open"] is None


async def test_child_starter_v2_leaves_bedtime_breaks_volume_open(db):
    prof = await pstore.profile_row(db, await starter_profile(db, "child"))
    fixed = set(pstore.body_of(prof)["fixed"])
    assert {"kids_quiet_hours", "kids_rate_limit", "kids_volume_max"}.isdisjoint(fixed)
    assert {"kids_blocked_types", "kids_exceptions"} <= fixed
    assert (
        prof["origin_version"] == 8
    )  # v3 panel sensors, v4 pause everything, v5 location, v6 home screen head,
    # v7 watch capped by kids_watch, v8 Hub only
    assert "kids_rate_limit_all" not in fixed


async def test_without_the_right_everything_is_refused(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db, manager_rule=False)
    for message in (
        {"type": "family_get_children"},
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "kids_volume_max",
            "value": 80,
        },
        {
            "type": "family_release",
            "client_ids": [child_one],
            "kind": "bedtime",
            "preset": "1h",
        },
    ):
        out = await _msg(db, ha, manager_one, message)
        assert out[0]["type"] == "family_error"
        assert out[0]["reason"] == "not_permitted"
    # a child device never manages (boundary) – not even itself
    out = await _msg(db, ha, child_one, {"type": "family_get_children"})
    assert out[0]["reason"] == "not_permitted"


# ── kids rules as device adjustments ────────────────────────────────────────


async def test_rule_set_is_an_adjustment_of_the_child_with_origin(db, pushed):
    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)
    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_rule_set",
            "request_id": "r1",
            "client_ids": [child_one],
            "rule": "kids_volume_max",
            "value": 60,
            "expires": "7d",
        },
    )
    assert out[0]["type"] == "family_result" and out[0]["request_id"] == "r1"
    assert out[0]["results"][0]["status"] == "ok"
    assert out[0]["results"][0]["until"]
    row = await db.fetchone(
        "SELECT * FROM device_overrides WHERE client_id = ? AND rule_key = ?",
        (child_one, "kids_volume_max"),
    )
    assert row["origin"] == "family_manager"
    assert row["created_by_client_id"] == manager_one
    assert row["created_by"] == "Manager One"
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["rules"]["kids_volume_max"]["set_by"] == "Manager One"
    assert cfg["kids"]["rules"]["profile"]["volume_max"] == 60
    # Child Two is untouched (shared profile, change per child)
    assert (await pstore.effective_rule(db, child_two, "kids_volume_max")) != 60
    audit = await db.fetchone(
        "SELECT * FROM profile_audit WHERE client_id = ? AND action = 'override_set'",
        (child_one,),
    )
    assert json.loads(audit["after_json"])["origin"] == "family_manager"
    assert (child_one, {"type": "client_config_updated", "data": {}}) in pushed["to"]
    assert any(m["type"] == "family_changed" for m in pushed["broadcast"])

    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_rule_reset",
            "client_ids": [child_one],
            "rule": "kids_volume_max",
        },
    )
    assert out[0]["results"][0]["status"] == "ok"
    assert not await db.fetchone(
        "SELECT 1 FROM device_overrides WHERE client_id = ? AND rule_key = ?",
        (child_one, "kids_volume_max"),
    )


async def test_rule_set_respects_fixed_manageable_scope_and_values(db, pushed):
    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)

    async def one(rule, value, target=None):
        out = await _msg(
            db,
            ha,
            manager_one,
            {
                "type": "family_rule_set",
                "client_ids": [target or child_one],
                "rule": rule,
                "value": value,
            },
        )
        return out[0]["results"][0]

    assert (await one("kids_blocked_types", []))["reason"] == "rule_fixed"
    assert (await one("remote_access", True))["reason"] == "rule_not_manageable"
    assert (await one("kids_exceptions", []))["reason"] == "rule_not_manageable"
    assert (await one("kids_volume_max", 300))["reason"] == "value_invalid"
    assert (await one("kids_volume_max", 50, target=manager_one))[
        "reason"
    ] == "not_your_child"
    # scope: only Child Two
    async with db.transaction():
        await fstore.put_scope(
            db, manager_one, scope={"all": False, "client_ids": [child_two]}, actor="t"
        )
    assert (await one("kids_volume_max", 50))["reason"] == "not_your_child"
    assert (await one("kids_volume_max", 50, target=child_two))["status"] == "ok"


async def test_areas_only_within_the_own_areas(db, pushed):
    ha = FakeHA()
    kitchen = await _area(db, "Küche", ["light.kitchen"])
    kids = await _area(db, "Kinderzimmer", ["light.kids"])
    manager_one, child_one, _ = await _house(db)
    async with db.transaction():
        await pstore.put_override(
            db,
            manager_one,
            "areas",
            value={
                "mode": "selected",
                "area_ids": [kids],
                "tags": [],
                "own_area": False,
            },
            user_choice=False,
            expires_at=None,
            note=None,
            actor="admin",
        )

    async def areas(value):
        out = await _msg(
            db,
            ha,
            manager_one,
            {
                "type": "family_rule_set",
                "client_ids": [child_one],
                "rule": "areas",
                "value": value,
            },
        )
        return out[0]["results"][0]

    bad = {"mode": "selected", "area_ids": [kitchen], "tags": [], "own_area": False}
    ok = {"mode": "selected", "area_ids": [kids], "tags": [], "own_area": False}
    every = {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
    assert (await areas(bad))["reason"] == "areas_not_allowed"
    assert (await areas(every))["reason"] == "areas_not_allowed"
    assert (await areas(ok))["status"] == "ok"


async def test_children_view(db, pushed):
    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)
    out = await _msg(
        db, ha, manager_one, {"type": "family_get_children", "request_id": "c"}
    )
    assert out[0]["type"] == "family_children" and out[0]["request_id"] == "c"
    by_id = {c["client_id"]: c for c in out[0]["children"]}
    assert set(by_id) == {child_one, child_two}
    mia_view = by_id[child_one]
    assert mia_view["owner_label"] == "Child One"
    assert mia_view["rules"]["kids_quiet_hours"]["manageable"] is True
    assert mia_view["rules"]["kids_blocked_types"]["manageable"] is False
    assert (
        mia_view["rules"]["kids_blocked_types"]["not_manageable_reason"] == "rule_fixed"
    )
    assert set(mia_view["status"]) >= {"bedtime_now", "bedtime_until", "next_bedtime"}
    assert mia_view["today"] == {"actions": 0, "blocked": 0, "requests": 0}


# ── releases ────────────────────────────────────────────────────────────────


async def test_bedtime_release_lets_the_child_switch(db, pushed, calls):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    manager_one, child_one, _ = await _house(
        db, child_overrides={"kids_quiet_hours": ALL_DAY}
    )
    control = {
        "type": "control",
        "request_id": "x",
        "entity_id": "light.kids",
        "service": "turn_on",
    }
    blocked = await hub_control.control(db, ha, child_one, control)
    assert blocked["type"] == "rule_blocked" and blocked["reason"] == "quiet_hours"

    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_release",
            "client_ids": [child_one],
            "kind": "bedtime",
            "preset": "30m",
        },
    )
    res = out[0]["results"][0]
    assert res["status"] == "ok" and res["until"]
    ok = await hub_control.control(db, ha, child_one, control)
    assert ok["type"] == "control_ok"
    cfg = await _get_client_config(db, child_one, ha)
    (rel,) = cfg["kids"]["releases"]
    assert rel["kind"] == "bedtime" and rel["by"]["name"] == "Manager One"

    out = await _msg(
        db,
        ha,
        manager_one,
        {"type": "family_release_end", "client_ids": [child_one], "kind": "bedtime"},
    )
    assert out[0]["results"][0]["status"] == "ok"
    again = await hub_control.control(db, ha, child_one, control)
    assert again["type"] == "rule_blocked"


async def test_entity_release_never_for_locks_and_not_against_fixed(db, pushed, calls):
    ha = FakeHA(
        {
            "camera.garden": {"state": "idle", "attributes": {}},
            "lock.front_door": {"state": "locked", "attributes": {}},
        }
    )
    await _area(db, "Garten", ["camera.garden", "lock.front_door"])
    manager_one, child_one, _ = await _house(db)

    async def release(entity_id):
        out = await _msg(
            db,
            ha,
            manager_one,
            {
                "type": "family_release",
                "client_ids": [child_one],
                "kind": "entity",
                "entity_id": entity_id,
                "preset": "15m",
            },
        )
        return out[0]["results"][0]

    assert (await release("lock.front_door"))["reason"] == "release_not_allowed"
    # camera is a blocked type and kids_blocked_types is fixed in "Kind"
    assert (await release("camera.garden"))["reason"] == "release_not_allowed"

    # the admin may (not limited by **Fixed** rules): the release opens the blocked type
    child = await service.child_of(db, ha, child_one)
    until = await service.start_release(
        db,
        ha,
        service.admin_manager(),
        child,
        "entity",
        "camera.garden",
        "15m",
        "admin_ui",
    )
    assert until
    ok = await hub_control.control(
        db,
        ha,
        child_one,
        {"type": "control", "entity_id": "camera.garden", "service": "turn_on"},
    )
    assert ok["type"] == "control_ok"


async def test_entity_release_fails_closed_while_the_state_is_unknown(db, pushed):
    """HA does not answer get_state: a release of an entity whose domain
    has a blocked ``domain:class`` (fixed) is refused like for that class."""
    known = FakeHA(
        {
            "cover.garage": {
                "entity_id": "cover.garage",
                "state": "closed",
                "attributes": {"device_class": "garage"},
            }
        }
    )
    manager_one, child_one, _ = await _house(db)
    child = await service.child_of(db, known, child_one)
    assert "kids_blocked_types" in child.fixed
    assert "cover:garage" in child.blocked_types()
    msg = {
        "type": "family_release",
        "client_ids": [child_one],
        "kind": "entity",
        "entity_id": "cover.garage",
        "preset": "2h",
    }
    for ha in (known, FakeHA()):
        (res,) = await _msg(db, ha, manager_one, msg)
        assert res["results"][0]["reason"] == "release_not_allowed"
    assert await fstore.releases_of(db, child_one) == []


async def test_pause_release_lifts_the_pause(db, pushed, calls):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    rate = {"max_actions": 1, "window_seconds": 60, "cooldown_seconds": 600}
    manager_one, child_one, _ = await _house(
        db, child_overrides={"kids_rate_limit": rate, "kids_quiet_hours": []}
    )
    control = {"type": "control", "entity_id": "light.kids", "service": "toggle"}
    assert (await hub_control.control(db, ha, child_one, control))[
        "type"
    ] == "control_ok"
    paused = await hub_control.control(db, ha, child_one, control)
    assert paused["reason"] == "rate_limited"
    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_release",
            "client_ids": [child_one],
            "kind": "pause",
            "preset": "15m",
        },
    )
    assert out[0]["results"][0]["status"] == "ok"
    assert (await hub_control.control(db, ha, child_one, control))[
        "type"
    ] == "control_ok"
    kinds = [
        r["kind"]
        for r in await db.fetchall(
            "SELECT kind FROM event_log WHERE client_id = ? ORDER BY id", (child_one,)
        )
    ]
    # The attempt that starts the pause is the pause entry.
    assert kinds[:4] == [
        "kids_control",
        "kids_pause",
        "family_release",
        "kids_control",
    ], kinds


async def test_releases_end_on_time(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    past = fstore.utcnow() - timedelta(minutes=1)
    async with db.transaction():
        await fstore.put_release(
            db, child_one, "bedtime", None, past, {"kind": "admin", "name": "A"}
        )
    assert await service.expire_releases(db, ha) == [child_one]
    assert await fstore.releases_of(db, child_one) == []
    assert (child_one, {"type": "client_config_updated", "data": {}}) in pushed["to"]


# ── requests of the children ────────────────────────────────────────────────


async def _ask(db, ha, child, category="stay_up", **extra):
    return await service.handle_child_message(
        db, ha, child, {"type": "kids_request", "category": category, **extra}
    )


async def test_request_answer_first_wins(db, pushed, monkeypatch):
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    papa = await add_client(
        db,
        name="iPhone Papa",
        owner_label="Papa",
        platform="ios",
        profile="adult",
        overrides={"family_manager": True},
    )
    _WS(db, ha, manager_one)
    (state,) = await _ask(db, ha, child_one, note="Film fertig schauen")
    assert state["type"] == "kids_request_state"
    req = state["request"]
    assert req["status"] == "open" and req["note"] == "Film fertig schauen"
    rid = req["request_id"]
    # Manager One is connected: family_request with can_approve
    sent = [
        m
        for cid, m in pushed["to"]
        if cid == manager_one and m["type"] == "family_request"
    ]
    assert sent and sent[-1]["request"]["can_approve"] is True
    # Papa is away (remote access) and not connected: relay through the integration
    relays = [m for m in pushed["integration"] if m["type"] == "family_relay"]
    assert relays and papa in relays[-1]["client_ids"]

    # a second open request is refused
    (err,) = await _ask(db, ha, child_one)
    assert err["type"] == "family_error" and err["reason"] == "request_limit"

    out = await _msg(
        db,
        ha,
        manager_one,
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "1h"},
    )
    assert out[0]["type"] == "family_request"
    assert out[0]["request"]["status"] == "approved"
    assert out[0]["request"]["answer"]["by"]["name"] == "Manager One"
    (rel,) = await fstore.releases_of(db, child_one)
    assert rel["kind"] == "bedtime" and rel["request_id"] == rid

    late = await _msg(
        db, ha, papa, {"type": "family_answer", "request_id": rid, "answer": "no"}
    )
    assert late[0]["reason"] == "already_answered"
    assert late[0]["request"]["answer"]["answer"] == "yes"
    child_states = [
        m
        for cid, m in pushed["to"]
        if cid == child_one and m["type"] == "kids_request_state"
    ]
    assert child_states[-1]["request"]["status"] == "approved"


async def test_request_timeout_cooldown_and_hourly_limit(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    async with db.transaction():
        await db.execute(
            "UPDATE kids_requests SET expires_at = ? WHERE id = ?",
            (fstore.iso(fstore.utcnow() - timedelta(seconds=1)), rid),
        )
    assert await service.expire_requests(db, ha) == [rid]
    assert (await fstore.request_row(db, rid))["status"] == "expired"

    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_answer",
            "request_id": rid,
            "answer": "no",
            "message": "come_to_me",
        },
    )
    assert out[0]["request"]["status"] == "declined"
    (err,) = await _ask(db, ha, child_one)
    assert err["reason"] == "cooldown" and err["retry_after"] > 0
    # another category is still possible
    (state,) = await _ask(db, ha, child_one, category="end_pause")
    assert state["type"] == "kids_request_state"
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["kids"]["requests"]["open"]["category"] == "end_pause"
    assert cfg["kids"]["requests"]["next_allowed_at"]


async def test_requests_disabled_without_managers_or_by_setting(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db, manager_rule=False)
    (err,) = await _ask(db, ha, child_one)
    assert err["reason"] == "requests_disabled"
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["kids"]["requests"]["enabled"] is False
    # adults cannot ask
    (err,) = await _ask(db, ha, manager_one)
    assert err["reason"] == "not_permitted"


async def test_unlock_request_needs_a_visible_entity(db, pushed):
    ha = FakeHA(
        {
            "media_player.tv": {
                "state": "off",
                "attributes": {"friendly_name": "Fernseher"},
            }
        }
    )
    await _area(db, "Wohnzimmer", ["media_player.tv"])
    manager_one, child_one, _ = await _house(db)
    (err,) = await _ask(
        db, ha, child_one, category="unlock_device", entity_id="lock.front_door"
    )
    assert err["reason"] == "invalid_request"
    (state,) = await _ask(
        db, ha, child_one, category="unlock_device", entity_id="media_player.tv"
    )
    assert state["request"]["entity_name"] == "Fernseher"


async def test_child_cancels_its_request(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    (out,) = await service.handle_child_message(
        db, ha, child_one, {"type": "kids_request_cancel", "request_id": rid}
    )
    assert out["request"]["status"] == "cancelled"
    (out,) = await service.handle_child_message(
        db, ha, manager_one, {"type": "kids_request_cancel", "request_id": rid}
    )
    assert out["reason"] == "not_permitted"


# ── HA notifications with answer buttons ────────────────────────────────────


async def test_actionable_notification_and_answer_from_ha_event(
    db, pushed, monkeypatch
):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    async with db.transaction():
        await fstore.put_scope(
            db,
            manager_one,
            notify_targets=["notify.mobile_app_iphone_alex", "notify.telegram"],
            actor="t",
        )
    sink = notify.make_sink(db, ha)
    events.add_sink(sink.handle)
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    mobile = [s for s in ha.services if s[1] == "mobile_app_iphone_alex"]
    telegram = [s for s in ha.services if s[1] == "telegram"]
    assert len(mobile) == 1 and len(telegram) == 1
    assert "actions" not in (telegram[0][2].get("data") or {})
    payload = mobile[0][2]
    assert payload["data"]["tag"].endswith(rid)
    titles = [a["title"] for a in payload["data"]["actions"]]
    assert titles == ["Ja, 30 Min.", "Ja, 1 Std.", "Nein"]
    tokens = await db.fetchall(
        "SELECT * FROM family_answer_tokens WHERE request_id = ?", (rid,)
    )
    assert len(tokens) == 3
    action = payload["data"]["actions"][1]["action"]
    assert action.startswith(FAMILY_ACTION_PREFIX)
    token = action[len(FAMILY_ACTION_PREFIX) :]
    # the hub never stores the token itself
    assert all(token not in json.dumps(t) for t in tokens)

    spawned = []
    monkeypatch.setattr(tasks, "spawn", lambda coro, name="": spawned.append(coro))
    notify.on_ha_event(db, ha)("mobile_app_notification_action", {"action": action})
    assert len(spawned) == 1
    await spawned[0]
    row = await fstore.request_row(db, rid)
    assert row["status"] == "approved"
    answer = json.loads(row["answer_json"])
    assert answer["preset"] == "1h" and answer["by"]["via"] == "ha_notification"
    # tokens are gone, the notification is cleared by its tag
    assert not await db.fetchall(
        "SELECT 1 FROM family_answer_tokens WHERE request_id = ?", (rid,)
    )
    assert ha.services[-1][2] == {
        "message": "clear_notification",
        "data": {"tag": payload["data"]["tag"]},
    }
    # the same token again: invalid (single use)
    result, _ = await notify.answer_by_token(db, ha, token)
    assert result == "invalid"


async def test_notify_answer_endpoint_results(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    req = _request(db, ha, auth="integration")
    out = await family_api.integration_family_notify_answer(
        req, family_api.NotifyAnswer(token="A" * 22)
    )
    assert out == {"result": "invalid", "request": None}


# ── activity ────────────────────────────────────────────────────────────────


async def test_activity_only_own_children(db, pushed, calls):
    ha = FakeHA(
        {"light.kids": {"state": "off", "attributes": {"friendly_name": "Licht"}}}
    )
    await _area(db, "Kinderzimmer", ["light.kids"])
    manager_one, child_one, child_two = await _house(
        db, child_overrides={"kids_quiet_hours": []}
    )
    await hub_control.control(
        db,
        ha,
        child_one,
        {"type": "control", "entity_id": "light.kids", "service": "turn_on"},
    )
    await hub_control.control(
        db,
        ha,
        child_two,
        {"type": "control", "entity_id": "light.kids", "service": "turn_on"},
    )
    async with db.transaction():
        await fstore.put_scope(
            db, manager_one, scope={"all": False, "client_ids": [child_one]}, actor="t"
        )
    out = await _msg(db, ha, manager_one, {"type": "family_get_activity", "limit": 10})
    act = out[0]
    assert act["type"] == "family_activity"
    assert {i["client_id"] for i in act["items"]} == {child_one}
    item = act["items"][0]
    assert item["kind"] == "control" and item["entity_name"] == "Licht"
    assert act["summary"][0]["actions"] == 1
    out = await _msg(
        db, ha, manager_one, {"type": "family_get_activity", "client_id": child_two}
    )
    assert out[0]["reason"] == "not_your_child"


# ── limits ──────────────────────────────────────────────────────────────────


async def test_write_rate_limit(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    message = {"type": "family_notify_set", "notify": {"kids_control": True}}
    for _ in range(30):
        assert (await _msg(db, ha, manager_one, message))[0]["type"] == "family_notify"
    out = await _msg(db, ha, manager_one, message)
    assert out[0]["reason"] == "rate_limited" and out[0]["retry_after"] > 0


async def test_notify_set_keeps_requests_on(db, pushed):
    ha = FakeHA()
    manager_one, _, _ = await _house(db)
    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_notify_set",
            "notify": {"kids_release_requested": False, "kids_control": True},
        },
    )
    assert out[0]["notify"]["kids_release_requested"] is True
    assert out[0]["notify"]["kids_control"] is True


# ── WebSocket dispatch ──────────────────────────────────────────────────────


async def test_ws_dispatch_and_admin_session(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    sock = _WS(db, ha, manager_one)
    await _handle_client_message(
        sock, db, {"type": "family_get_children"}, "family_get_children", manager_one
    )
    assert sock.sent[-1]["type"] == "family_children"
    admin = _WS(db, ha, "ingress")
    await _handle_client_message(
        admin, db, {"type": "family_get_children"}, "family_get_children", "ingress"
    )
    assert admin.sent[-1]["reason"] == "not_permitted"
    child = _WS(db, ha, child_one)
    await _handle_client_message(
        child,
        db,
        {"type": "kids_request", "category": "stay_up"},
        "kids_request",
        child_one,
    )
    assert child.sent[-1]["type"] == "kids_request_state"


# ── admin and integration REST ──────────────────────────────────────────────


async def test_admin_scope_overview_settings_and_answer(db, pushed):
    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)
    req = _request(db, ha)
    out = await family_api.put_client_family(
        req,
        manager_one,
        family_api.ClientFamily(
            scope=family_api.ChildrenScope(
                all=False, client_ids=[child_one, manager_one, str(uuid.uuid4())]
            ),
            notify_targets=["notify.mobile_app_iphone_alex"],
        ),
    )
    assert out["family_manager"] is True
    assert out["scope"] == {"all": False, "client_ids": [child_one]}
    assert [c["client_id"] for c in out["children"]] == [child_one]
    assert out["children"][0]["fixed_manageable"] == [
        "kids_color_lock",
        "kids_blocked_types",
    ]
    with pytest.raises(HTTPException) as e:
        await family_api.put_client_family(
            req,
            child_one,
            family_api.ClientFamily(
                scope=family_api.ChildrenScope(all=True, client_ids=[])
            ),
        )
    assert e.value.status_code == 409

    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    overview = await family_api.get_family(req)
    assert [m["client_id"] for m in overview["managers"]] == [manager_one]
    assert overview["managers"][0]["child_ids"] == [child_one]
    assert {c["client_id"]: c["manager_ids"] for c in overview["children"]} == {
        child_one: [manager_one],
        child_two: [],
    }
    assert [r["request_id"] for r in overview["open_requests"]] == [rid]

    settings = await family_api.get_family_settings(req)
    settings["request_timeout_minutes"] = 5
    assert (
        await family_api.put_family_settings(req, family_api.FamilySettings(**settings))
    )["request_timeout_minutes"] == 5

    answered = await family_api.answer_family_request(
        req, rid, family_api.FamilyAnswer(answer="no", message="tomorrow")
    )
    assert (
        answered["status"] == "declined" and answered["answer"]["by"]["kind"] == "admin"
    )
    overview = await family_api.get_family(req)
    assert overview["open_requests"] == []
    assert [r["request_id"] for r in overview["recent_requests"]] == [rid]
    with pytest.raises(HTTPException) as e:
        await family_api.answer_family_request(
            req, rid, family_api.FamilyAnswer(answer="no")
        )
    assert e.value.status_code == 409 and e.value.detail["code"] == "already_answered"
    with pytest.raises(HTTPException) as e:
        await family_api.get_family(_request(db, ha, auth="client"))
    assert e.value.status_code == 403


async def test_integration_relay_is_the_away_route(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    req = _request(db, ha, auth="integration")
    out = await family_api.integration_family(
        req,
        family_api.IntegrationFamily(
            client_id=manager_one, message={"type": "family_get_children"}
        ),
    )
    assert out["messages"][0]["type"] == "family_children"
    out = await family_api.integration_family(
        req,
        family_api.IntegrationFamily(
            client_id=manager_one,
            message={
                "type": "family_release",
                "client_ids": [child_one],
                "kind": "bedtime",
                "preset": "15m",
            },
        ),
    )
    assert out["messages"][0]["results"][0]["status"] == "ok"
    (rel,) = await fstore.releases_of(db, child_one)
    assert json.loads(rel["by_json"])["via"] == "away"
    # a child without remote access has no HA key: refused
    out = await family_api.integration_family(
        req,
        family_api.IntegrationFamily(
            client_id=child_one, message={"type": "kids_request", "category": "stay_up"}
        ),
    )
    assert out["messages"][0] == {
        "type": "family_error",
        "reason": "not_permitted",
        **{k: v for k, v in out["messages"][0].items() if k not in ("type", "reason")},
    }
    assert await fstore.open_request_of(db, child_one) is None
    # anything else is no relay message; admin tokens are refused
    with pytest.raises(HTTPException) as e:
        await family_api.integration_family(
            req,
            family_api.IntegrationFamily(
                client_id=child_one, message={"type": "control", "entity_id": "light.x"}
            ),
        )
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await family_api.integration_family(
            _request(db, ha),
            family_api.IntegrationFamily(
                client_id=manager_one, message={"type": "family_get_children"}
            ),
        )
    assert e.value.status_code == 403


async def test_child_with_remote_access_asks_away(db, pushed, monkeypatch):
    """A child device with remote access sends kids_request through
    the integration and gets kids_request_state relayed while it is away."""
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    monkeypatch.setattr(ws, "get_connected_client_ids", lambda: set())
    ha = FakeHA()
    manager_one, child_one, _ = await _house(
        db, child_overrides={"remote_access": True}
    )
    req = _request(db, ha, auth="integration")
    out = await family_api.integration_family(
        req,
        family_api.IntegrationFamily(
            client_id=child_one, message={"type": "kids_request", "category": "stay_up"}
        ),
    )
    (state,) = out["messages"]
    assert state["type"] == "kids_request_state"
    assert state["request"]["status"] == "open"
    rid = state["request"]["request_id"]
    relays = [m for m in pushed["integration"] if m["type"] == "family_relay"]
    assert any(
        r["client_ids"] == [child_one] and r["message"]["type"] == "kids_request_state"
        for r in relays
    )
    pushed["integration"].clear()
    await _msg(
        db,
        ha,
        manager_one,
        {"type": "family_answer", "request_id": rid, "answer": "no"},
    )
    (relay,) = [
        m
        for m in pushed["integration"]
        if m["type"] == "family_relay" and m["client_ids"] == [child_one]
    ]
    assert relay["message"]["request"]["status"] == "declined"
    # cancel goes the same way
    out = await family_api.integration_family(
        req,
        family_api.IntegrationFamily(
            client_id=child_one,
            message={"type": "kids_request_cancel", "request_id": rid},
        ),
    )
    assert out["messages"][0]["type"] == "kids_request_state"


async def test_child_at_home_gets_no_relay(db, pushed, monkeypatch):
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    ha = FakeHA()
    _, child_one, _ = await _house(db, child_overrides={"remote_access": True})
    monkeypatch.setattr(ws, "get_connected_client_ids", lambda: {child_one})
    await _ask(db, ha, child_one)
    assert not [
        m
        for m in pushed["integration"]
        if m["type"] == "family_relay" and m["client_ids"] == [child_one]
    ]
    assert any(
        cid == child_one for cid, m in pushed["to"] if m["type"] == "kids_request_state"
    )


async def test_losing_the_right_takes_effect_at_once(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    assert (await _msg(db, ha, manager_one, {"type": "family_get_children"}))[0][
        "type"
    ] == "family_children"
    async with db.transaction():
        await pstore.delete_override(db, manager_one, "family_manager")
    assert (await _msg(db, ha, manager_one, {"type": "family_get_children"}))[0][
        "reason"
    ] == "not_permitted"
    assert "family" not in await _get_client_config(db, manager_one, ha)


async def test_kids_requests_rule_is_optional_per_child(db, pushed):
    """ "Can I have more time?" is the kids rule kids_requests (not fixed in
    the child profile): a family manager switches it per child; off = refused."""
    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["rules"]["kids_requests"]["value"]["enabled"] is True
    assert cfg["kids"]["requests"]["categories"] == [
        "stay_up",
        "unlock_device",
        "end_pause",
    ]

    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "kids_requests",
            "value": {"enabled": True, "categories": ["stay_up"]},
        },
    )
    assert out[0]["results"][0]["status"] == "ok"
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["kids"]["requests"]["categories"] == ["stay_up"]
    assert cfg["rules"]["kids_requests"]["set_by"] == "Manager One"
    (err,) = await _ask(db, ha, child_one, category="end_pause")
    assert err["reason"] == "requests_disabled"

    await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "kids_requests",
            "value": {"enabled": False, "categories": ["stay_up"]},
        },
    )
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["kids"]["requests"]["enabled"] is False
    (err,) = await _ask(db, ha, child_one)
    assert err["reason"] == "requests_disabled"
    # Child Two keeps asking
    (state,) = await _ask(db, ha, child_two)
    assert state["type"] == "kids_request_state"


async def test_toddler_suggestion_has_requests_off(db):
    from services.profiles import catalog

    body = catalog.materialize("toddler")
    assert body["rules"]["kids_requests"]["enabled"] is False
    assert "kids_requests" not in body["fixed"]
    for sid in ("child_6_9", "child_10_12", "teen"):
        assert catalog.materialize(sid)["rules"]["kids_requests"]["enabled"] is True


# ── schema v11 (after the log tab v10) ──────────────────────────────────────


async def _downgrade(path, version: int) -> None:
    """A database as an older build left it: without the family part (v10) or
    also without the log tab (v9)."""
    import sqlite3

    conn = sqlite3.connect(path)
    for table in (
        "family_answer_tokens",
        "kids_requests",
        "kids_releases",
        "family_scopes",
    ):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("ALTER TABLE device_overrides DROP COLUMN origin")
    conn.execute("ALTER TABLE device_overrides DROP COLUMN created_by_client_id")
    if version < 10:
        conn.execute("DROP TABLE event_log")
        conn.execute("DROP TABLE log_notify_marks")
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


@pytest.mark.parametrize("start", [9, 10])
async def test_upgrade_to_v11_keeps_adjustments(tmp_path, start, caplog):
    from services import db_migrations

    path = str(tmp_path / "hub.db")
    db = Database(path)
    await db.initialize()
    child_one = await add_client(
        db,
        name="iPad Child One",
        platform="ipados",
        profile="child",
        overrides={"kids_volume_max": 55},
    )
    await db.close()
    await _downgrade(path, start)

    caplog.set_level("INFO")
    db = Database(path)
    await db.initialize()
    try:
        assert (await db.fetchone("PRAGMA user_version"))[
            "user_version"
        ] == SCHEMA_VERSION
        assert await db_migrations.missing_schema_parts(db.connection) == []
        row = await db.fetchone(
            "SELECT value_json, origin, created_by_client_id FROM device_overrides "
            "WHERE client_id = ?",
            (child_one,),
        )
        assert row == {
            "value_json": "55",
            "origin": "admin",
            "created_by_client_id": None,
        }
        steps = [
            r.getMessage()
            for r in caplog.records
            if "migrated to schema" in r.getMessage()
        ]
        assert any(s.endswith("v11: family manager") for s in steps), steps
        assert len(steps) == SCHEMA_VERSION - start
    finally:
        await db.close()


# ── log tab, rule snapshot and kids check ─────────────────────


async def test_family_events_land_in_the_log_and_as_ha_events(db, pushed, monkeypatch):
    import sys

    bc = sys.modules["api.ws.broadcast"]
    fired = []

    async def _int(message):
        fired.append(message)
        return True

    monkeypatch.setattr(bc, "send_to_integration", _int)
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    await _msg(
        db,
        ha,
        manager_one,
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "30m"},
    )
    await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_release",
            "client_ids": [child_one],
            "kind": "pause",
            "preset": "15m",
        },
    )
    await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "kids_volume_max",
            "value": 50,
        },
    )
    rows = await db.fetchall(
        "SELECT kind, event_kind, category, actor, action FROM event_log WHERE client_id = ? ORDER BY id",
        (child_one,),
    )
    kinds = [(r["kind"], r["event_kind"], r["category"]) for r in rows]
    assert ("kids_release_requested", "kids_release_requested", "activity") in kinds
    assert ("kids_request_answered", "kids_request_answered", "activity") in kinds
    assert ("family_release", "family_change", "change") in kinds
    assert ("override_set", "family_change", "change") in kinds
    answered = next(r for r in rows if r["kind"] == "kids_request_answered")
    assert answered["actor"] == "Manager One" and answered["action"] == "yes"
    events = [m["data"]["kind"] for m in fired if m["type"] == "ha_event"]
    assert {"kids_release_requested", "kids_request_answered", "family_change"} <= set(
        events
    )
    # the activity of the family manager is a read view of the same log
    out = await _msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_get_activity",
            "kinds": ["request", "answer", "release", "change"],
        },
    )
    got = {i["kind"] for i in out[0]["items"]}
    assert got == {"request", "answer", "release", "change"}, out[0]["items"]
    change = next(i for i in out[0]["items"] if i["kind"] == "change")
    assert change["rule"] == "kids_volume_max" and change["by"]["name"] == "Manager One"


async def test_snapshot_and_kids_check_honour_releases(db, pushed, monkeypatch):
    from services import enforcement
    from services.profiles import store as pstore

    from .test_enforcement import _errors, _validator

    states = {
        "light.kids": {"state": "off", "attributes": {}},
        "camera.garden": {"state": "idle", "attributes": {}},
    }

    class HA(FakeHA):
        async def get_states(self):
            return [{"entity_id": k, **v} for k, v in self.states.items()]

    ha = HA(states)
    await _area(db, "Kinderzimmer", ["light.kids", "camera.garden"])
    manager_one, child_one, _ = await _house(
        db,
        child_overrides={
            "kids_quiet_hours": ALL_DAY,
            "kids_rate_limit": {
                "max_actions": 1,
                "window_seconds": 60,
                "cooldown_seconds": 600,
            },
        },
    )
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_user_id = ? WHERE id = ?",
            ("c" * 32, child_one),
        )
    snap = await enforcement.build_one(db, ha, child_one)
    assert "bedtime_released_until" not in snap["kids"]
    check = {"client_id": child_one, "entity_id": "light.kids", "action": "turn_on"}
    assert (await enforcement.kids_check(db, ha, grant=None, **check))[
        "reason"
    ] == "quiet_hours"

    child = await service.child_of(db, ha, child_one)
    admin = service.admin_manager()
    await service.start_release(db, ha, admin, child, "bedtime", None, "1h", "admin_ui")
    await service.start_release(db, ha, admin, child, "pause", None, "15m", "admin_ui")
    await service.start_release(
        db, ha, admin, child, "entity", "camera.garden", "30m", "admin_ui"
    )
    snap = await enforcement.build_one(db, ha, child_one)
    kids = snap["kids"]
    assert kids["bedtime_released_until"] and kids["pause_released_until"]
    assert kids["entities"]["camera.garden"]["release_until"]
    assert (
        kids["entities"]["camera.garden"]["blocked"] is True
    )  # the type stays blocked after it
    assert "release_until" not in kids["entities"]["light.kids"]
    assert _errors(_validator("common/enforcement-snapshot.json"), snap) == []
    # kids check: bedtime and pause released, counted twice without a pause
    assert (await enforcement.kids_check(db, ha, grant=None, **check))["allowed"]
    assert (await enforcement.kids_check(db, ha, grant=None, **check))["allowed"]
    async with db.transaction():
        await pstore.delete_override(db, child_one, "kids_rate_limit")


async def test_time_zone_is_not_cached_while_ha_starts():
    """A failed first read of HA's time zone (HA restarting, 502)
    must not pin UTC for ten minutes."""

    class Starting:
        calls = 0

        async def get_config(self):
            Starting.calls += 1
            if Starting.calls == 1:
                raise RuntimeError("502 Bad Gateway")
            return {"time_zone": "Europe/Zurich"}

    kids_mode.reset()
    ha = Starting()
    assert await kids_mode.hub_time_zone(ha) is None
    assert await kids_mode.hub_time_zone(ha) == "Europe/Zurich"


# ── removal and access ────────────────────────────────────────


@pytest.fixture
def removal(monkeypatch):
    from services import device_provisioning as dp

    async def _none(*_a, **_k):
        return 0

    async def _sender(_message):
        return True

    monkeypatch.setattr(ws, "signal_client_removed", _none)
    monkeypatch.setattr(ws, "disconnect_client", _none)
    monkeypatch.setattr(dp, "_sender", _sender)


async def test_closed_child_device_does_not_ask(db, pushed):
    """Outside its access a child device asks nothing (not_permitted);
    withdrawing its own request stays possible."""
    from services import access

    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    async with db.transaction():
        await pstore.put_override(
            db,
            child_one,
            "access_hours",
            value={"enabled": True, "windows": []},
            user_choice=False,
            expires_at=None,
            note=None,
            actor="admin",
        )
    assert await access.client_open(db, child_one) is False
    (err,) = await _ask(db, ha, child_one, category="end_pause")
    assert err["type"] == "family_error" and err["reason"] == "not_permitted"
    (out,) = await service.handle_child_message(
        db, ha, child_one, {"type": "kids_request_cancel", "request_id": rid}
    )
    assert out["request"]["status"] == "cancelled"


async def test_removing_the_child_cancels_its_open_request(db, pushed, removal):
    """The open request ends as cancelled while the managers can still
    be found: they are told and the HA notification is cleared."""
    from api.pairing import clients as clients_api

    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    async with db.transaction():
        await fstore.put_scope(
            db, manager_one, notify_targets=["notify.mobile_app_alex"], actor="admin"
        )
    _WS(db, ha, manager_one)  # Manager One is connected
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    await notify.send_request(db, ha, await fstore.request_row(db, rid))
    assert notify._sent[rid] == {"notify.mobile_app_alex"}
    pushed["to"].clear()
    ha.services.clear()
    await clients_api.delete_client(_request(db, ha), child_one, ha_user_removal=None)
    assert (await fstore.request_row(db, rid))["status"] == "cancelled"
    told = [
        m for c, m in pushed["to"] if c == manager_one and m["type"] == "family_request"
    ]
    assert told and told[-1]["request"]["status"] == "cancelled"
    assert ha.services and rid not in notify._sent


async def test_releases_of_a_removed_babysitter_end(db, pushed, removal):
    """The releases a babysitter gave end when its device is removed,
    by the admin's delete at once and by the planner for any other way."""
    from api.pairing import clients as clients_api
    from services import access

    from . import test_access as acc

    ha = FakeHA()
    manager_one, child_one, child_two = await _house(db)
    sitter_profile = await acc._sitter_profile(db)
    released = {}
    until = fstore.utcnow() + timedelta(hours=2)
    for name, child in (("Sitter One", child_one), ("Child Three", child_two)):
        sitter = await acc._sitter(db, sitter_profile, name=f"iPhone {name}")
        by = {"kind": "babysitter", "client_id": sitter, "name": name}
        async with db.transaction():
            await fstore.put_release(db, child, "bedtime", None, until, by, None)
        released[sitter] = child
    (sitter_one, child_three) = list(released)
    assert await fstore.releases_of(db, child_one) and await fstore.releases_of(
        db, child_two
    )
    # The admin's delete ends Sitter One's release at once.
    await clients_api.delete_client(_request(db, ha), sitter_one, ha_user_removal=None)
    assert await fstore.releases_of(db, child_one) == []
    assert await fstore.releases_of(db, child_two)
    # Child Three's device goes another way (row gone): the planner ends it.
    async with db.transaction():
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (child_three,))
    await access.check_transitions(db)
    assert await fstore.releases_of(db, child_two) == []


async def test_yes_and_its_release_are_one_step(db, pushed, monkeypatch):
    """If the release cannot be stored, the request stays open."""
    ha = FakeHA()
    manager_one, child_one, _ = await _house(db)
    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]

    async def _fail(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(fstore, "put_release", _fail)
    with pytest.raises(RuntimeError):
        await _msg(
            db,
            ha,
            manager_one,
            {
                "type": "family_answer",
                "request_id": rid,
                "answer": "yes",
                "preset": "15m",
            },
        )
    assert (await fstore.request_row(db, rid))["status"] == "open"
