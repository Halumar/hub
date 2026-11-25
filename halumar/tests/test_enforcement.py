"""Enforcement in Home Assistant: rule snapshots, push, kids
check, integration events and client_config.ha_enforcement."""

import asyncio
import hashlib
import json
import os
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from api import integration as integration_api
from api import ws
from api.ws.client import _get_client_config
from models.database import Database
from models.schemas import IntegrationEventsRequest
from services import enforcement, integration_events, kids_mode, kids_rules
from services import crypto, quick_access

from .profile_helpers import add_client as _add_client
from .profile_helpers import starter_profile
from .test_kids_mode import _kids_profile

ADULT_USER = "a" * 32
GUEST_USER = "b" * 32
CHILD_USER = "c" * 32
PANEL_USER = "d" * 32


def _contracts_root() -> Path | None:
    # conftest sets CONTRACTS_DIR (also from a checkout next to the repo).
    root = os.environ.get("CONTRACTS_DIR")
    if root and (Path(root) / "schemas/common/enforcement-snapshot.json").exists():
        return Path(root)
    return None


def _validator(schema: str):
    root = _contracts_root()
    if root is None:
        pytest.skip("contract checkout with enforcement not found (set CONTRACTS_DIR)")
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")

    def retrieve(uri):
        path = Path(uri.replace("file://", ""))
        return referencing.Resource.from_contents(json.loads(path.read_text()))

    schema_path = root / "schemas" / schema
    body = json.loads(schema_path.read_text())
    body["$id"] = schema_path.as_uri()
    return jsonschema.Draft202012Validator(
        body, registry=referencing.Registry(retrieve=retrieve)
    )


def _errors(validator, value) -> list[str]:
    return [e.message for e in sorted(validator.iter_errors(value), key=str)][:3]


class FakeHA:
    def __init__(self, states=None):
        self.states = states or {}

    async def get_states(self):
        return [{"entity_id": k, **v} for k, v in self.states.items()]

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def get_config(self):
        return {"time_zone": "Europe/Zurich"}


STATES = {
    "light.living": {"state": "off", "attributes": {}},
    "lock.front_door": {"state": "locked", "attributes": {}},
    "cover.garage": {"state": "closed", "attributes": {"device_class": "garage"}},
    "scene.movie": {"state": "unknown", "attributes": {}},
    "media_player.kids": {"state": "on", "attributes": {"volume_level": 0.2}},
    "light.kids": {"state": "off", "attributes": {}},
}


async def add_client(db, **kw):
    """A device; one with an HA user holds its token (ha_enforcement comes
    only together with the token)."""
    if kw.get("ha_user_id") and "ha_token_status" not in kw:
        kw.update(
            ha_token_status="active",
            ha_token_encrypted=await crypto.encrypt_token(db, "device-token"),
        )
    return await _add_client(db, **kw)


@pytest.fixture(autouse=True)
def _keyfile(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    yield
    crypto.reset_cache()


@pytest.fixture(autouse=True)
def _reset():
    enforcement.reset_state()
    integration_events.reset()
    kids_mode.reset()
    quick_access.reset_state()
    yield
    enforcement.reset_state()
    integration_events.reset()


@pytest.fixture
def sent(monkeypatch):
    log = []

    async def _send(message):
        log.append(message)
        return True

    async def _send_to(_client_id, _message):
        return True

    monkeypatch.setattr(enforcement, "_send", _send)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    return log


async def _area(db, name, entities, tiles=None):
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
    return area_id


ALL_AREAS = {"mode": "all", "area_ids": [], "tags": [], "own_area": False}


async def _home(db):
    await _area(
        db,
        "Wohnzimmer",
        ["light.living", "lock.front_door", "cover.garage", "scene.movie"],
    )
    await _area(db, "Kinderzimmer", ["light.kids", "media_player.kids"])


async def _snapshots(db, ha=None):
    return {
        s["client_id"]: s for s in await enforcement.build_all(db, ha or FakeHA(STATES))
    }


def _canonical_hash(snapshot):
    body = {k: v for k, v in snapshot.items() if k not in ("rules_hash", "issued_at")}
    return hashlib.sha256(
        json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


# ── snapshots ───────────────────────────────────────────────────────────────


async def test_snapshots_follow_the_profiles_and_the_contract(db):
    await _home(db)
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        # A member who switches directly (the Adult suggestion is Checked).
        overrides={
            "areas": ALL_AREAS,
            "quick_access": "control",
            "control_path": "direct",
        },
    )
    guest = await add_client(
        db, name="Gast", guest=True, ha_user_id=GUEST_USER, tags=["*"]
    )
    child = await add_client(
        db,
        name="Kind",
        profile="child",
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )
    panel = await add_client(
        db,
        name="Panel",
        platform="ipados",
        profile="wall_panel",
        ha_user_id=PANEL_USER,
        overrides={"areas": ALL_AREAS},
    )
    no_user = await add_client(db, name="Ohne", platform="ios")
    snaps = await _snapshots(db)
    validator = _validator("common/enforcement-snapshot.json")
    for snap in snaps.values():
        assert _errors(validator, snap) == []
        assert snap["rules_hash"] == _canonical_hash(snap)
    assert no_user not in snaps

    a = snaps[adult]
    assert (a["ha_user_id"], a["control_path"], a["remote_access"]) == (
        ADULT_USER,
        "direct",
        True,
    )
    assert a["entities"]["light.living"] == {
        "local": "control",
        "remote": "control",
        "quick": "control",
    }
    # "When away" of the adult suggestion: lock and garage door only view.
    assert a["entities"]["lock.front_door"]["remote"] == "view"
    assert a["entities"]["cover.garage"]["remote"] == "view"
    assert a["entities"]["scene.movie"]["run"] is True
    assert a["kids"] is None

    g = snaps[guest]
    assert g["control_path"] == "checked"
    assert g["remote_access"] is False
    assert {e["remote"] for e in g["entities"].values()} == {"hidden"}
    assert {e["quick"] for e in g["entities"].values()} == {"off"}

    c = snaps[child]
    assert (c["control_path"], c["child_protection"], c["remote_access"]) == (
        "checked",
        True,
        False,
    )
    kids = c["kids"]
    assert kids["time_zone"] == "Europe/Zurich"
    assert set(kids["entities"]) == set(c["entities"])
    assert kids["entities"]["lock.front_door"]["blocked"] is True  # blocked type
    assert kids["entities"]["light.kids"]["blocked"] is False
    assert kids["entities"]["light.kids"]["quiet_hours"]
    assert kids["entities"]["media_player.kids"]["volume_max"] == 40
    assert kids["entities"]["light.kids"]["rate_limit"]["max_actions"] == 6

    assert snaps[panel]["control_path"] == "checked"


async def test_child_with_remote_access_has_remote_levels_and_a_key(
    db, monkeypatch, tmp_path
):
    """A child device with remote access gets remote levels in its
    snapshot (the integration checks the way away) and a read-only key."""
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    await _home(db)
    await db.set_setting("ha_url", "https://example.ui.nabu.casa")
    child = await add_client(
        db,
        name="Kind",
        profile="child",
        ha_user_id=CHILD_USER,
        ha_token_status="active",
        ha_token_encrypted=await crypto.encrypt_token(db, "kid-token"),
        overrides={"areas": ALL_AREAS, "remote_access": True},
    )
    snap = (await _snapshots(db))[child]
    assert _errors(_validator("common/enforcement-snapshot.json"), snap) == []
    assert (snap["control_path"], snap["child_protection"], snap["remote_access"]) == (
        "checked",
        True,
        True,
    )
    assert snap["entities"]["light.kids"]["remote"] == "control"
    assert snap["entities"]["lock.front_door"]["remote"] == "view"
    assert snap["entities"]["light.kids"]["quick"] == "off"
    assert snap["kids"]["entities"]["lock.front_door"]["blocked"] is True
    cfg = await _get_client_config(db, child)
    assert _errors(_validator("client-config.json"), cfg) == []
    assert cfg["ha_token"] == "kid-token"
    assert cfg["ha_url"] == "https://example.ui.nabu.casa"
    assert cfg["kids"]["away"] == "ha"
    assert cfg["ha_enforcement"]["control_path"] == "checked"
    crypto.reset_cache()


async def test_control_path_adjustment_and_client_config(db, events_none=None):
    await _home(db)
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides={"control_path": "checked"},
    )
    child = await add_client(db, name="Kind", profile="child", ha_user_id=CHILD_USER)
    snaps = await _snapshots(db)
    assert snaps[adult]["control_path"] == "checked"

    cfg = await _get_client_config(db, adult)
    assert cfg["ha_enforcement"] == {"control_path": "checked", "rules_hash": None}
    assert cfg["rules"]["control_path"]["value"] == "checked"
    assert cfg["rules"]["areas"]["enforcement"] == "ha_control"
    assert (await _get_client_config(db, child))["ha_enforcement"] is None

    validator = _validator("client-config.json")
    for cid in (adult, child):
        assert _errors(validator, await _get_client_config(db, cid)) == []


async def test_run_targets_and_hidden_entities(db):
    area = await _area(
        db,
        "Wohnzimmer",
        ["light.living", "script.hidden"],
        tiles={
            "light.living": {
                "tap_action": {"action": "run", "target_entity_id": "script.good_night"}
            }
        },
    )
    await db.execute(
        "UPDATE area_entities SET is_visible = 0 WHERE area_id = ? AND entity_id = ?",
        (area, "script.hidden"),
    )
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides={"areas": ALL_AREAS},
    )
    entities = (await _snapshots(db))[adult]["entities"]
    assert "script.hidden" not in entities
    assert entities["script.good_night"]["run"] is True


# ── push ────────────────────────────────────────────────────────────────────

# A member who switches directly (the Adult suggestion is Checked).
DIRECT = {"control_path": "direct"}


async def test_push_complete_changes_and_removed(db, sent):
    from services.profiles import store

    await _home(db)
    validator = _validator("ws/integration/enforcement_rules.json")
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides=DIRECT,
    )
    guest = await add_client(
        db, name="Gast", guest=True, ha_user_id=GUEST_USER, tags=["*"]
    )
    ha = FakeHA(STATES)
    assert await enforcement.push(db, ha, complete=True) == 2
    assert sent[-1]["data"]["complete"] is True
    assert _errors(validator, sent[-1]) == []
    assert await enforcement.push(db, ha) == 0  # nothing changed

    await store.put_override(
        db,
        adult,
        "control_path",
        value="checked",
        user_choice=False,
        expires_at=None,
        note=None,
        actor="test",
    )
    assert await enforcement.push(db, ha) == 1
    assert [s["client_id"] for s in sent[-1]["data"]["snapshots"]] == [adult]
    assert sent[-1]["data"]["snapshots"][0]["control_path"] == "checked"
    # Next to the snapshot, outside its versioned body.
    assert sent[-1]["data"]["control_paths"] == {adult: "checked"}
    hashes = await enforcement.rules_hashes(db, ha)
    assert set(hashes) == {adult, guest}
    cfg = await _get_client_config(db, adult)
    assert cfg["ha_enforcement"]["rules_hash"] == hashes[adult]

    await db.execute("UPDATE paired_clients SET is_active = 0 WHERE id = ?", (guest,))
    await enforcement.push(db, ha)
    assert sent[-1]["data"]["removed"] == [guest]
    assert _errors(validator, sent[-1]) == []


async def test_failed_snapshot_build_still_sends_the_control_path(
    db, sent, monkeypatch, caplog
):
    """A device whose snapshot cannot be built is no removal: push and fetch
    name its resolved control path (checked now), so the integration holds it
    read-only instead of keeping an earlier "direct"."""
    from services.profiles import store

    await _home(db)
    validator = _validator("ws/integration/enforcement_rules.json")
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides=DIRECT,
    )
    guest = await add_client(
        db, name="Gast", guest=True, ha_user_id=GUEST_USER, tags=["*"]
    )
    pending = await add_client(
        db, name="Neu", platform="ios", ha_user_id=None, ha_token_status="pending"
    )
    ha = FakeHA(STATES)
    await store.put_override(
        db,
        adult,
        "control_path",
        value="direct",
        user_choice=False,
        expires_at=None,
        note=None,
        actor="test",
    )
    await enforcement.push(db, ha, complete=True)
    assert sent[-1]["data"]["control_paths"][adult] == "direct"
    # The device being provisioned is named too, without a snapshot.
    assert sent[-1]["data"]["control_paths"][pending] in ("checked", "direct")
    assert pending not in {s["client_id"] for s in sent[-1]["data"]["snapshots"]}

    real = enforcement.build_snapshot

    async def broken(db_, ha_, entry, *a, **k):
        if entry["client"]["id"] == adult:
            raise RuntimeError("boom")
        return await real(db_, ha_, entry, *a, **k)

    monkeypatch.setattr(enforcement, "build_snapshot", broken)
    await store.put_override(
        db,
        adult,
        "control_path",
        value="checked",
        user_choice=False,
        expires_at=None,
        note=None,
        actor="test",
    )
    with caplog.at_level("ERROR", logger=enforcement.logger.name):
        assert await enforcement.push(db, ha) == 0
    message = sent[-1]["data"]
    assert message["removed"] == []
    assert adult not in {s["client_id"] for s in message["snapshots"]}
    assert message["control_paths"][adult] == "checked"
    assert _errors(validator, sent[-1]) == []
    assert "could not be built" in caplog.text and adult in caplog.text

    sent.clear()
    await enforcement.push(db, ha, complete=True)
    message = sent[-1]["data"]
    assert message["complete"] is True and message["removed"] == []
    assert message["control_paths"][adult] == "checked"
    assert {s["client_id"] for s in message["snapshots"]} == {guest}

    request = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
    )
    answer = await integration_api.integration_enforcement_rules(request)
    assert answer["control_paths"][adult] == "checked"
    assert adult not in {s["client_id"] for s in answer["snapshots"]}
    assert (
        _errors(_validator("integration/enforcement-rules.response.json"), answer) == []
    )


async def test_integration_session_gets_the_complete_set(db, sent):
    await _home(db)
    await add_client(db, name="Phone", platform="ios", ha_user_id=ADULT_USER)
    enforcement.register(db, FakeHA(STATES))
    await enforcement.integration_connected()
    assert sent and sent[0]["type"] == "enforcement_rules"
    assert sent[0]["data"]["complete"] is True


async def test_enforcement_rules_endpoint_and_status(db, sent):
    await _home(db)
    adult = await add_client(db, name="Phone", platform="ios", ha_user_id=ADULT_USER)
    request = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=FakeHA(STATES))
        ),
    )
    answer = await integration_api.integration_enforcement_rules(request)
    assert (
        _errors(_validator("integration/enforcement-rules.response.json"), answer) == []
    )
    assert [s["client_id"] for s in answer["snapshots"]] == [adult]
    assert answer["control_paths"] == {adult: answer["snapshots"][0]["control_path"]}
    hashes = await enforcement.rules_hashes(db, FakeHA(STATES))
    assert hashes == {adult: answer["snapshots"][0]["rules_hash"]}

    from fastapi import HTTPException

    request.state.auth_type = "admin"
    with pytest.raises(HTTPException):
        await integration_api.integration_enforcement_rules(request)


# ── kids check and grant ────────────────────────────────────────────────────


async def test_kids_check_in_parallel_keeps_the_pause_of_everything(db):
    """Every await of the decision happens before the counters are
    read, so a pause started by a parallel check is seen by the others."""
    lights = [f"light.p{i}" for i in range(6)]
    await _area(db, "Spielzimmer", lights)
    profile = await _kids_profile(
        db,
        rules={
            "rate_limit": None,
            "rate_limit_all": {
                "enabled": True,
                "max_actions": 12,
                "max_entities": 4,
                "window_seconds": 60,
                "cooldown_seconds": 300,
            },
        },
    )
    child = await add_client(
        db,
        name="Kind",
        profile_id=profile["id"],
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )

    class SlowHA(FakeHA):
        async def get_config(self):
            await asyncio.sleep(0.01)
            return await super().get_config()

    ha = SlowHA({e: {"state": "off", "attributes": {}} for e in lights})
    answers = await asyncio.gather(
        *[
            enforcement.kids_check(
                db, ha, client_id=child, entity_id=e, action="turn_on", grant=None
            )
            for e in lights
        ]
    )
    assert [a["allowed"] for a in answers].count(True) == 4
    assert {a["reason"] for a in answers if not a["allowed"]} == {"paused_all"}


async def test_kids_check_counts_pauses_and_honours_the_grant(db):
    await _home(db)
    profile = await _kids_profile(
        db,
        rules={
            "blocked_types": ["lock"],
            "quiet_hours": [],
            "rate_limit": {
                "max_actions": 2,
                "window_seconds": 60,
                "cooldown_seconds": 300,
            },
        },
    )
    child = await add_client(
        db,
        name="Kind",
        profile_id=profile["id"],
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )
    ha = FakeHA(STATES)
    check = {"client_id": child, "entity_id": "light.kids", "action": "turn_on"}
    validator = _validator("integration/kids-check.response.json")
    for _ in range(2):
        answer = await enforcement.kids_check(db, ha, grant=None, **check)
        assert answer == {"allowed": True, "reason": None, "until": None}
    answer = await enforcement.kids_check(db, ha, grant=None, **check)
    assert answer["allowed"] is False and answer["reason"] == "rate_limited"
    assert _errors(validator, answer) == []
    answer = await enforcement.kids_check(db, ha, grant=None, **check)
    assert answer["reason"] == "cooldown" and answer["until"]

    # A grant of the hub's own control path: allowed, not counted, single use.
    kids_rules.counters.reset()
    grant = enforcement.issue_grant(child, "light.kids", "turn_on")
    assert (await enforcement.kids_check(db, ha, grant=grant, **check))["allowed"]
    assert kids_rules.counters.recent(child, "light.kids") == []
    assert enforcement.consume_grant(grant, child, "light.kids", "turn_on") is False
    other = enforcement.issue_grant(child, "light.living", "turn_on")
    assert enforcement.consume_grant(other, child, "light.kids", "turn_on") is False

    # Blocked type, unknown entity, adult device.
    answer = await enforcement.kids_check(
        db,
        ha,
        client_id=child,
        entity_id="lock.front_door",
        action="unlock",
        grant=None,
    )
    assert answer["reason"] == "blocked"
    answer = await enforcement.kids_check(
        db, ha, client_id=child, entity_id="light.nowhere", action="turn_on", grant=None
    )
    assert answer["reason"] == "blocked"
    adult = await add_client(db, name="Phone", platform="ios", ha_user_id=ADULT_USER)
    answer = await enforcement.kids_check(
        db, ha, client_id=adult, entity_id="light.kids", action="turn_on", grant=None
    )
    assert answer["allowed"] is False


async def test_kids_check_pauses_everything_and_snapshot_carries_the_rule(db):
    # The integration asks for every action when rate_limit_all is on;
    # the fifth different entity pauses everything, also on the hub path.
    await _home(db)
    limit = {
        "enabled": True,
        "max_actions": 12,
        "max_entities": 2,
        "window_seconds": 60,
        "cooldown_seconds": 300,
    }
    profile = await _kids_profile(
        db, rules={"quiet_hours": [], "rate_limit": None, "rate_limit_all": limit}
    )
    child = await add_client(
        db,
        name="Kind",
        profile_id=profile["id"],
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )
    ha = FakeHA(STATES)
    snap = (await _snapshots(db, ha))[child]
    assert snap["kids"]["rate_limit_all"] == limit
    assert snap["kids"]["entities"]["light.kids"]["rate_limit"] is None
    validator = _validator("common/enforcement-snapshot.json")
    assert _errors(validator, snap) == []

    def check(entity_id):
        return enforcement.kids_check(
            db,
            ha,
            client_id=child,
            entity_id=entity_id,
            action="turn_on",
            grant=None,
        )

    assert (await check("light.kids"))["allowed"]
    assert (await check("light.living"))["allowed"]
    answer = await check("media_player.kids")
    assert answer["allowed"] is False and answer["reason"] == "paused_all"
    assert _errors(_validator("integration/kids-check.response.json"), answer) == []
    assert (await check("light.kids"))["reason"] == "paused_all"
    row = await db.fetchone(
        "SELECT entity_id, action FROM event_log WHERE kind = 'kids_paused_all'"
    )
    assert row == {"entity_id": "media_player.kids", "action": "turn_on"}

    # A pause release ends it and suspends the limit.
    kids_rules.counters.clear_client(child)
    assert (await check("media_player.kids"))["allowed"]


async def test_snapshot_without_pause_everything_keeps_its_shape(db):
    # A profile without the rule (catalog default off) has no
    # rate_limit_all in the snapshot, so its rules_hash does not change.
    await _home(db)
    profile = await _kids_profile(db, rules={"quiet_hours": []})
    child = await add_client(
        db,
        name="Kind",
        profile_id=profile["id"],
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )
    assert "rate_limit_all" not in (await _snapshots(db))[child]["kids"]


async def test_kids_check_bedtime(db):
    await _home(db)
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    profile = await _kids_profile(
        db,
        rules={"quiet_hours": [{"days": every_day, "start": "00:00", "end": "00:00"}]},
    )
    child = await add_client(
        db,
        name="Kind",
        profile_id=profile["id"],
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )
    answer = await enforcement.kids_check(
        db,
        FakeHA(STATES),
        client_id=child,
        entity_id="light.kids",
        action="turn_on",
        grant=None,
    )
    assert answer["allowed"] is False and answer["reason"] == "quiet_hours"


# ── events ──────────────────────────────────────────────────────────────────


def _fixtures(kind: str) -> list[tuple[str, dict]]:
    root = _contracts_root()
    if root is None:
        pytest.skip("contract checkout not found")
    folder = root / "fixtures" / "integration" / kind
    return [(p.name, json.loads(p.read_text())) for p in sorted(folder.glob("*.json"))]


async def test_events_intake_dedupes_and_hands_over(db):
    seen = []

    async def sink(events, dropped):
        seen.append((events, dropped))

    integration_events.add_sink(sink)
    for name, body in _fixtures("events.request"):
        if name.startswith("invalid"):
            with pytest.raises(ValidationError):
                IntegrationEventsRequest.model_validate(body)
            continue
        integration_events.reset()
        integration_events.add_sink(sink)
        req = IntegrationEventsRequest.model_validate(body)
        request = types.SimpleNamespace(
            state=types.SimpleNamespace(auth_type="integration"),
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
        )
        answer = await integration_api.integration_events_intake(request, req)
        assert answer == {"accepted": len(body["events"]), "duplicates": 0}
        again = await integration_api.integration_events_intake(request, req)
        assert again == {"accepted": 0, "duplicates": len(body["events"])}
    assert seen and all("id" in e for batch, _ in seen for e in batch)
    assert all(None not in e.values() for batch, _ in seen for e in batch)
    assert integration_events.totals()["duplicates"] > 0


async def test_failing_sink_does_not_break_the_intake():
    async def broken(events, dropped):
        raise RuntimeError("x")

    integration_events.add_sink(broken)
    event = {
        "id": str(uuid.uuid4()),
        "type": "integration_started",
        "at": datetime.now(UTC).isoformat(),
        "version": "2026.12.0",
    }
    assert await integration_events.ingest([event], 0) == (1, 0)


async def test_emit_ha_event_goes_to_the_integration(monkeypatch):
    import sys

    from api.ws.broadcast import send_to_integration  # noqa: F401 - loads the module

    broadcast_module = sys.modules["api.ws.broadcast"]

    got = []

    async def _send(message):
        got.append(message)
        return True

    monkeypatch.setattr(broadcast_module, "send_to_integration", _send)
    event = {"kind": "device_paired", "at": datetime.now(UTC).isoformat()}
    assert await integration_events.emit_ha_event(event)
    assert got == [{"type": "ha_event", "data": event}]


# ── time based changes ──────────────────────────────────────────────────────


async def test_expired_parental_release_changes_the_snapshot(db, sent):
    await _home(db)
    child = await add_client(
        db,
        name="Kind",
        profile="child",
        ha_user_id=CHILD_USER,
        overrides={"areas": ALL_AREAS},
    )
    soon = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    await db.execute(
        "UPDATE paired_clients SET kids_override_until = ? WHERE id = ?",
        (soon.isoformat(), child),
    )
    snap = (await _snapshots(db))[child]
    assert snap["kids"]["override_until"] == kids_mode.iso(soon)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await db.execute(
        "UPDATE paired_clients SET kids_override_until = ? WHERE id = ?",
        (past.isoformat(), child),
    )
    assert (await _snapshots(db))[child]["kids"]["override_until"] is None


async def test_notify_change_ignores_noise():
    enforcement.notify_change("ping")
    enforcement.notify_change("notification_created")
    assert enforcement._push_task is None


# ── snapshot before the app learns its token ───────────────────


async def test_token_delivery_sends_the_snapshot_before_the_app_hears(
    db, monkeypatch, tmp_path
):
    import services.crypto as crypto
    from models.schemas import ClientTokenDelivery

    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    await _home(db)
    client_id = await add_client(
        db,
        name="New phone",
        platform="ios",
        profile="adult",
        tags=["*"],
        ha_token_status="pending",
        ha_token_pending_since=Database.now(),
    )
    order: list[tuple[str, str]] = []
    tokens_during_push: list[str | None] = []

    async def _send(message):
        ids = [s["client_id"] for s in message["data"]["snapshots"]]
        order.append(("integration", ",".join(ids)))
        # An app asking right now must not get the token yet.
        tokens_during_push.append((await _get_client_config(db, client_id))["ha_token"])
        return True

    async def _send_to(target, message):
        order.append((target, message["type"]))
        return True

    monkeypatch.setattr(enforcement, "_send", _send)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    enforcement.register(db, FakeHA(STATES))
    request = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
    )
    await integration_api.integration_client_token(
        request,
        ClientTokenDelivery(
            client_id=client_id,
            ha_user_id=GUEST_USER,
            refresh_token_id="rt-1",
            token="t" * 40,
        ),
    )
    assert order[0] == ("integration", client_id)
    assert tokens_during_push == [None]
    assert order[-1] == (client_id, "client_config_updated")
    assert not enforcement.is_token_held(client_id)
    if enforcement._push_task is not None:
        enforcement._push_task.cancel()
    cfg = await _get_client_config(db, client_id)
    assert cfg["ha_token"] and cfg["ha_enforcement"]["rules_hash"]
    crypto.reset_cache()


def test_token_hold_nests_and_ends_on_errors():
    with enforcement.token_held("c1"):
        with enforcement.token_held("c1"):
            assert enforcement.is_token_held("c1")
        assert enforcement.is_token_held("c1")
    assert not enforcement.is_token_held("c1")
    with pytest.raises(RuntimeError):
        with enforcement.token_held("c1"):
            raise RuntimeError
    assert not enforcement.is_token_held("c1")


async def test_push_now_without_context_or_in_time(db, monkeypatch):
    assert await enforcement.push_now("client_token") is False

    import asyncio

    gate = asyncio.Event()
    sent = []

    async def _slow_send(message):
        await gate.wait()
        sent.append(message)
        return True

    await _home(db)
    await add_client(db, name="Phone", platform="ios", ha_user_id=ADULT_USER)
    monkeypatch.setattr(enforcement, "_send", _slow_send)
    enforcement.register(db, FakeHA(STATES))
    assert await enforcement.push_now("client_token", timeout=0.05) is False
    # The push is not cut: it finishes in the background.
    gate.set()
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert sent and sent[0]["type"] == "enforcement_rules"
    if enforcement._push_task is not None:
        await enforcement._push_task


async def test_starter_profiles_have_their_control_path(db):
    from services.profiles import store

    expected = {
        "adult": "checked",
        "guest": "hub",
        "wall_panel": "checked",
        "apple_tv": "checked",
        "child": "hub",
    }
    for suggestion, path in expected.items():
        row = await store.profile_row(db, await starter_profile(db, suggestion))
        assert store.body_of(row)["rules"]["control_path"] == path


# ── control path of an existing device follows the delivered snapshot


async def _set_path(db, client_id, value):
    from services.profiles import changes, store

    async def _write():
        await store.put_override(
            db,
            client_id,
            "control_path",
            value=value,
            user_choice=False,
            expires_at=None,
            note=None,
            actor="test",
        )

    _, change = await changes.run(db, _write, dry_run=False)
    return change


async def test_client_config_path_follows_the_delivered_snapshot(db, sent):
    await _home(db)
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides=DIRECT,
    )
    ha = FakeHA(STATES)
    await enforcement.push(db, ha, complete=True)
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "direct"

    # The profile says checked, the integration has not got it: the app keeps
    # switching directly while its HA user still may (and vice versa).
    await _set_path(db, adult, "checked")
    cfg = await _get_client_config(db, adult)
    assert cfg["rules"]["control_path"]["value"] == "checked"
    assert cfg["ha_enforcement"]["control_path"] == "direct"
    await enforcement.push(db, ha)
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "checked"

    await _set_path(db, adult, "direct")
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "checked"
    await enforcement.push(db, ha)
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "direct"


async def test_path_change_is_pushed_before_the_device_hears(db, monkeypatch):
    from services.profiles import changes

    await _home(db)
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides=DIRECT,
    )
    order: list[tuple[str, str]] = []
    paths_seen: list[str] = []

    async def _send(message):
        for s in message["data"]["snapshots"]:
            order.append(("integration", s["control_path"]))
        return True

    async def _send_to(target, message):
        order.append((target, message["type"]))
        if target == adult and message["type"] == "client_config_updated":
            cfg = await _get_client_config(db, adult)
            paths_seen.append(cfg["ha_enforcement"]["control_path"])
        return True

    monkeypatch.setattr(enforcement, "_send", _send)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    ha = FakeHA(STATES)
    enforcement.register(db, ha)
    await enforcement.push(db, ha, complete=True)
    order.clear()
    paths_seen.clear()

    change = await _set_path(db, adult, "checked")
    assert enforcement.path_changed(change.devices)
    await changes.apply_effects(db, change)
    assert order[0] == ("integration", "checked")
    assert (adult, "client_config_updated") in order
    assert paths_seen and set(paths_seen) == {"checked"}
    if enforcement._push_task is not None:
        enforcement._push_task.cancel()


async def test_path_changed_only_for_path_or_key(db):
    assert not enforcement.path_changed([])
    assert not enforcement.path_changed(
        [{"changes": [{"rule": "areas"}], "ha_key": "unchanged"}]
    )
    assert enforcement.path_changed(
        [{"changes": [{"rule": "control_path"}], "ha_key": "unchanged"}]
    )
    assert enforcement.path_changed([{"changes": [], "ha_key": "removed"}])


async def test_lost_push_keeps_the_old_path_and_resends_complete(db, monkeypatch):
    await _home(db)
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides=DIRECT,
    )
    log = []
    arrive = {"ok": True}

    async def _send(message):
        log.append(message)
        return arrive["ok"]

    async def _send_to(_target, _message):
        return True

    monkeypatch.setattr(enforcement, "_send", _send)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    ha = FakeHA(STATES)
    await enforcement.push(db, ha, complete=True)

    arrive["ok"] = False
    await _set_path(db, adult, "checked")
    await enforcement.push(db, ha)
    # Not arrived: the HA user is still a full user, so is the app's path.
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "direct"

    arrive["ok"] = True
    assert await enforcement.push(db, ha) == 1
    assert log[-1]["data"]["complete"] is True
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "checked"


async def test_fetched_set_counts_as_delivered(db, monkeypatch):
    await _home(db)
    adult = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        ha_user_id=ADULT_USER,
        overrides=DIRECT,
    )

    arrive = {"ok": True}

    async def _send(_message):
        return arrive["ok"]

    monkeypatch.setattr(enforcement, "_send", _send)
    monkeypatch.setattr(ws, "send_to_client", _send)
    ha = FakeHA(STATES)
    await enforcement.push(db, ha, complete=True)
    arrive["ok"] = False
    await _set_path(db, adult, "checked")
    await enforcement.push(db, ha)
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "direct"
    request = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
    )
    answer = await integration_api.integration_enforcement_rules(request)
    assert answer["snapshots"][0]["control_path"] == "checked"
    assert (await _get_client_config(db, adult))["ha_enforcement"][
        "control_path"
    ] == "checked"
