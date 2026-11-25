"""A child's Apple Watch (contract protocol.md and setup.md, "A child's watch").

* The kids rule ``kids_watch`` lifts the parental-controls boundary of the
  phone's rule ``watch``: off by default for children, on only with remote
  access, never for guests; for adults ``watch`` applies unchanged.
* A watch whose phone has parental controls resolves with the phone's
  profile, adjustments and kids state: the same kids rules, a checked control
  path, and ONE counter / pause everything / release for both.
* Lifecycle: re-scoped (token rotated) when the phone comes under parental
  controls with ``kids_watch``, removed without it or when it goes later.
* Pairing: the child's phone binds the session, an adult with
  ``setup_devices`` kind child confirms it.
"""

from datetime import datetime, timedelta, timezone

import api.websocket as ws
import pytest
from api import ws as api_ws
from api.pairing import clients as clients_api
from models.database import Database
from services import (
    hub_control,
    enforcement,
    integration_events,
    kids_mode,
    kids_rules,
    onboarding,
    quick_access,
    watch_access,
)
from services.profiles import catalog
from services.profiles import changes as pchanges
from services.profiles import rows as profile_rows
from services.profiles import store
from services.profiles.catalog import ProfileError

from .profile_helpers import add_client, starter_profile
from .test_enforcement import (
    ALL_AREAS,
    STATES,
    FakeHA,
    _area,
    _errors,
    _home,
    _validator,
)
from .test_onboarding import (  # noqa: F401 - _clean_state is an autouse fixture
    HUB_ID,
    _ask,
    _clean_state,
    _phone,
    _send,
    _start,
    _tv_key,
    _WS,
)
from .test_watch_access import WATCH_INFO, _open

PHONE_USER = "e" * 32
WATCH_USER = "f" * 32
RATE_2 = {"max_actions": 2, "window_seconds": 60, "cooldown_seconds": 300}
PAUSE_ALL = {
    "enabled": True,
    "max_actions": 12,
    "max_entities": 2,
    "window_seconds": 60,
    "cooldown_seconds": 300,
}
ALL_DAY = [
    {
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "start": "00:00",
        "end": "23:59",
    }
]


@pytest.fixture(autouse=True)
def _reset():
    enforcement.reset_state()
    integration_events.reset()
    kids_mode.reset()
    kids_rules.counters.reset()
    quick_access.reset_state()
    watch_access.reset_state()
    yield
    enforcement.reset_state()
    kids_rules.counters.reset()
    watch_access.reset_state()


async def _profile(db, *, kids_watch=True, remote=True, child=True, kids=None):
    """A profile; ``kids`` = kids rules of the profile level (quiet hours,
    rate limits), off unless given."""
    body = catalog.materialize("child" if child else "adult")
    kids = kids or {}
    body["rules"].update(
        {
            "remote_access": remote,
            "kids_watch": kids_watch,
            "watch": True,
            "kids_blocked_types": kids.get("blocked_types", ["lock"]),
            "kids_quiet_hours": kids.get("quiet_hours", []),
            "kids_rate_limit": kids.get("rate_limit"),
            "kids_rate_limit_all": kids.get(
                "rate_limit_all", {**PAUSE_ALL, "enabled": False}
            ),
        }
    )
    body["fixed"] = [k for k in body["fixed"] if k != "kids_watch"]
    async with db.transaction():
        pid = await profile_rows.insert_profile(
            db,
            name=f"Child One {datetime.now().timestamp()}",
            body=body,
            now=Database.now(),
            actor=None,
        )
        if not child:
            await db.execute(
                "UPDATE profiles SET child_protection = 0, kind = 'person' "
                "WHERE id = ?",
                (pid,),
            )
    return pid


async def _pair(db, phone_id: str, **columns) -> str:
    """A watch set up through ``phone_id`` (as onboarding stores it)."""
    _priv, pub = _tv_key()
    watch = await add_client(
        db,
        name="Apple Watch",
        platform="watchos",
        set_up_by_client_id=phone_id,
        set_up_by_name="iPhone Child One",
        set_up_method="onboard",
        seal_key=pub,
        ha_user_id=WATCH_USER,
        **columns,
    )
    await db.store_client_token(
        watch, ha_user_id=WATCH_USER, refresh_token_id="rt-w", token="watch-token"
    )
    return watch


async def _child_phone(db, **profile):
    pid = await _profile(db, **profile)
    phone = await add_client(
        db,
        name="iPhone Child One",
        platform="ios",
        profile_id=pid,
        ha_user_id=PHONE_USER,
        overrides={"areas": ALL_AREAS},
    )
    return pid, phone


async def _write(db, write):
    _, change = await pchanges.run(db, write, dry_run=False)
    await pchanges.apply_effects(db, change)
    return change


@pytest.fixture
def to_phone(monkeypatch):
    """Messages the hub sends to single devices (phone sockets)."""
    log: list[tuple[str, dict]] = []

    async def _send_to(client_id, message):
        log.append((client_id, message))
        return True

    monkeypatch.setattr(api_ws, "send_to_client", _send_to)
    return log


# ── rules ───────────────────────────────────────────────────────────────────


async def test_rule_off_by_default_for_children_on_with_the_admin(db):
    child = await starter_profile(db, "child")
    phone = await add_client(
        db, platform="ios", profile_id=child, overrides={"remote_access": True}
    )
    rules = (await store.resolve_client(db, phone))[2]
    assert rules["kids_watch"]["value"] is False
    assert rules["watch"]["value"] is False
    assert rules["watch"]["reason"] == "boundary_kids_watch"
    await store.put_override(
        db,
        phone,
        "kids_watch",
        value=True,
        user_choice=False,
        expires_at=None,
        note=None,
        actor="admin",
    )
    rules = (await store.resolve_client(db, phone))[2]
    assert rules["watch"] == {**rules["watch"], "value": True, "source": "profile"}
    # The adjustment of watch itself is no longer refused by the boundary.
    await store.check_override(db, phone, "watch", False, False)


async def test_adults_and_guests_keep_their_rule(db):
    adult = await add_client(db, platform="ios", profile="adult")
    rules = (await store.resolve_client(db, adult))[2]
    assert rules["kids_watch"]["reason"] == "requires_child_protection"
    assert rules["watch"]["value"] is True
    guest = await add_client(
        db, platform="ios", guest=True, overrides={"watch": True, "remote_access": True}
    )
    rules = (await store.resolve_client(db, guest))[2]
    assert rules["watch"]["value"] is False
    assert rules["watch"]["reason"] == "boundary_guest"


async def test_rule_needs_remote_access(db):
    _pid, phone = await _child_phone(db, remote=False)
    rules = (await store.resolve_client(db, phone))[2]
    assert rules["kids_watch"]["reason"] == "requires_remote_access"
    assert rules["watch"]["value"] is False


# ── the watch follows its phone ─────────────────────────────────────────────


async def test_watch_of_a_child_is_a_child_device(db):
    await _home(db)
    pid, phone = await _child_phone(db, kids={"quiet_hours": ALL_DAY})
    watch = await _pair(db, phone)
    row, prof, rules = await store.resolve_client(db, watch)
    assert prof["id"] == pid and prof["child_protection"]
    assert row["kids_subject"] == phone and row["id"] == watch
    assert rules["control_path"]["value"] == "checked"
    assert rules["quick_access"]["value"] == "off"
    assert rules["kids_quiet_hours"]["value"] == ALL_DAY
    # Areas from the phone's adjustment.
    assert rules["areas"]["value"] == ALL_AREAS
    config = await _get_config(db, watch)
    assert config["profile"]["child_protection"] is True
    assert config["profile"]["id"] == pid
    assert config["kids"]["away"] == "ha"
    assert config["ha_token"] == "watch-token"
    assert config["ha_enforcement"]["control_path"] == "checked"
    # Admin device list: under its phone, with the kids badge.
    view = await clients_api.client_view(db, watch)
    assert view.child_protection is True and view.profile_id == pid
    assert view.set_up_by["client_id"] == phone
    # Snapshot for the integration: kids rules of the phone, checked path.
    snaps = {s["client_id"]: s for s in await enforcement.build_all(db, FakeHA(STATES))}
    snap = snaps[watch]
    assert snap["child_protection"] is True and snap["control_path"] == "checked"
    assert snap["kids"]["entities"]["light.kids"]["quiet_hours"] == ALL_DAY
    assert snap["kids"]["entities"]["lock.front_door"]["blocked"] is True


async def _get_config(db, client_id):
    from api.ws.client import _get_client_config

    return await _get_client_config(db, client_id, sealed_for_watch=True)


async def test_adults_watch_keeps_its_own_profile(db):
    phone = await add_client(db, platform="ios", profile="adult", name="iPhone")
    watch = await _pair(db, phone)
    row, prof, _rules = await store.resolve_client(db, watch)
    assert not prof["child_protection"]
    assert prof["id"] == await store.default_profile_for(db, "watchos", False)
    assert "kids_subject" not in row


async def test_adjusting_a_childs_watch_is_refused(db):
    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    with pytest.raises(ProfileError) as e:
        await store.check_override(db, watch, "areas", ALL_AREAS, False)
    assert e.value.code == "override.child_watch"


# ── one kids state ──────────────────────────────────────────────────────────


async def test_rate_limit_is_shared_by_phone_and_watch(db):
    await _home(db)
    _pid, phone = await _child_phone(db, kids={"rate_limit": RATE_2})
    watch = await _pair(db, phone)
    ha = FakeHA(STATES)

    async def check(cid):
        return await enforcement.kids_check(
            db, ha, client_id=cid, entity_id="light.kids", action="turn_on", grant=None
        )

    assert (await check(phone))["allowed"]
    assert (await check(watch))["allowed"]
    # Two actions in the window, whichever device did them: both are paused.
    third = await check(watch)
    assert third["allowed"] is False and third["reason"] == "rate_limited"
    assert (await check(phone))["reason"] == "cooldown"
    # One cooldown, kept on the phone.
    now = kids_mode.utcnow()
    assert kids_rules.counters.cooldown(phone, "light.kids", now) is not None
    assert kids_rules.counters.cooldown(watch, "light.kids", now) is None


async def test_pause_everything_pauses_both(db):
    lights = ["light.a", "light.b", "light.c", "light.d"]
    await _area(db, "Spielzimmer", lights)
    _pid, phone = await _child_phone(db, kids={"rate_limit_all": PAUSE_ALL})
    watch = await _pair(db, phone)
    ha = FakeHA({e: {"state": "off", "attributes": {}} for e in lights})

    async def check(cid, entity):
        return await enforcement.kids_check(
            db, ha, client_id=cid, entity_id=entity, action="turn_on", grant=None
        )

    assert (await check(phone, "light.a"))["allowed"]
    assert (await check(watch, "light.b"))["allowed"]
    # The third entity across both devices starts the pause of everything.
    started = await check(watch, "light.c")
    assert started["allowed"] is False and started["reason"] == "paused_all"
    assert (await check(phone, "light.d"))["reason"] == "paused_all"
    assert (await check(phone, "light.a"))["reason"] == "paused_all"
    assert kids_rules.counters.paused_all(phone, kids_mode.utcnow()) is not None
    # The watch's config shows the phone's pause.
    assert (await _get_config(db, watch))["kids"]["paused_all_until"]


async def test_bedtime_applies_to_the_watch_and_a_release_to_both(db):
    await _home(db)
    _pid, phone = await _child_phone(db, kids={"quiet_hours": ALL_DAY})
    watch = await _pair(db, phone)
    ha = FakeHA(STATES)
    answer = await enforcement.kids_check(
        db, ha, client_id=watch, entity_id="light.kids", action="turn_on", grant=None
    )
    assert answer["allowed"] is False and answer["reason"] == "quiet_hours"
    # The admin releases the phone: the watch is released too.
    until = datetime.now(timezone.utc) + timedelta(minutes=30)
    await kids_mode.set_override(db, phone, None, until_at=until)
    answer = await enforcement.kids_check(
        db, ha, client_id=watch, entity_id="light.kids", action="turn_on", grant=None
    )
    assert answer["allowed"] is True
    snaps = {s["client_id"]: s for s in await enforcement.build_all(db, ha)}
    assert (
        snaps[watch]["kids"]["override_until"] == snaps[phone]["kids"]["override_until"]
    )
    assert snaps[watch]["kids"]["override_until"] is not None


# ── lifecycle ───────────────────────────────────────────────────────────────


async def test_becoming_a_child_with_the_rule_rescopes_the_watch(db, to_phone):
    phone = await add_client(
        db,
        platform="ios",
        profile="adult",
        name="iPhone Child One",
        ha_user_id=PHONE_USER,
    )
    watch = await _pair(db, phone)
    assert not (await store.resolve_client(db, watch))[1]["child_protection"]
    child = await _profile(db)

    async def write():
        await store.assign(db, phone, child, discard_overrides=False)

    change = await _write(db, write)
    assert change.watches_rescoped == [watch] and change.watches_removed == []
    assert await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch,))
    _row, prof, rules = await store.resolve_client(db, watch)
    assert prof["id"] == child and rules["control_path"]["value"] == "checked"
    # The adult token it held is rotated.
    rotation = await db.fetchone(
        "SELECT token_action FROM token_requests WHERE client_id = ?", (watch,)
    )
    assert rotation == {"token_action": "rotate"}


async def test_becoming_a_child_without_the_rule_removes_the_watch(db, to_phone):
    phone = await add_client(
        db,
        platform="ios",
        profile="adult",
        name="iPhone Child One",
        ha_user_id=PHONE_USER,
    )
    watch = await _pair(db, phone)
    child = await _profile(db, kids_watch=False)

    async def write():
        await store.assign(db, phone, child, discard_overrides=False)

    change = await _write(db, write)
    assert change.watches_removed == [watch]
    assert not await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch,))
    assert (
        phone,
        {
            "type": "watch_access",
            "client_id": watch,
            "status": "gone",
            "reason": "parental_controls",
        },
    ) in to_phone


async def test_switching_the_rule_off_later_removes_the_watch(db, to_phone):
    pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    other_phone = await add_client(db, platform="ios", profile_id=pid, name="iPad")
    other_watch = await _pair(db, other_phone)
    # Remote access goes on one phone: its watch goes (kids_watch needs it).
    await _write(
        db,
        lambda: store.put_override(
            db,
            other_phone,
            "remote_access",
            value=False,
            user_choice=False,
            expires_at=None,
            note=None,
            actor="admin",
        ),
    )
    assert not await db.fetchone(
        "SELECT id FROM paired_clients WHERE id = ?", (other_watch,)
    )
    assert await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch,))
    # The admin switches "Allow a watch" off in the profile.
    row = await store.profile_row(db, pid)
    body = store.body_of(row)
    body["rules"]["kids_watch"] = False

    async def write():
        await store.save_profile(db, row, body=body, actor="admin")

    await _write(db, write)
    assert not await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch,))


async def test_watch_access_is_gone_while_the_rule_is_off(db):
    pid, phone = await _child_phone(db, kids_watch=False)
    watch = await _pair(db, phone)
    answer = await watch_access.message(db, None, watch, phone)
    assert answer["status"] == "gone"
    _pid, allowed_phone = await _child_phone(db)
    allowed_watch = await _pair(db, allowed_phone)
    answer = await watch_access.message(db, None, allowed_watch, allowed_phone)
    assert answer["status"] == "ready"


async def test_removing_the_phone_still_removes_its_watch(db, to_phone):
    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    assert await clients_api.end_client_access(db, None, phone)
    assert not await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch,))


# ── pairing with an adult's confirmation ────────────────────────────────────


def _child_socket(db, client_id):
    return _WS(db, host="192.0.2.30", authenticated=True, client_id=client_id)


async def _watch_session(db):
    watch_ws, priv, pub = await _start(db, name="Apple Watch", info=WATCH_INFO)
    return watch_ws, priv, pub, watch_ws.last()["session_id"], watch_ws.last()["code"]


async def test_child_binds_and_an_adult_confirms(db):
    pid, child = await _child_phone(db)
    child_ws = _child_socket(db, child)
    watch_ws, priv, pub, sid, code = await _watch_session(db)
    await _send(child_ws, db, {"type": "onboard_lookup", **_ask(sid, pub)})
    info = child_ws.last()
    assert info["type"] == "onboard_info", info
    assert info["grant"]["kinds"] == [] and info["grant"]["own_watch"] is True
    assert info["grant"]["child_watch"] == {
        "role": "child",
        "phone_name": "iPhone Child One",
    }
    info_schema = _validator("ws/server/onboard_info.json")
    assert _errors(info_schema, info) == []
    # The child cannot confirm it.
    await _send(
        child_ws,
        db,
        {
            "type": "onboard_confirm",
            **_ask(sid, pub),
            "kind": "child",
            "profile_id": pid,
        },
    )
    assert child_ws.last()["reason"] == onboarding.NOT_PERMITTED
    # An adult who may set up children looks it up with the code.
    adult_ws, adult = await _phone(
        db, overrides={"setup_devices": {"enabled": True, "kinds": ["child"]}}
    )
    await _send(adult_ws, db, {"type": "onboard_lookup", "code": code})
    info = adult_ws.last()
    assert info["type"] == "onboard_info", info
    assert _errors(info_schema, info) == []
    grant = info["grant"]
    assert grant["own_watch"] is False
    assert grant["child_watch"] == {"role": "adult", "phone_name": "iPhone Child One"}
    assert [k["kind"] for k in grant["kinds"]] == ["child"]
    entry = grant["kinds"][0]["profiles"][0]
    assert entry["id"] == pid and entry["area_choice"] is False
    await _send(
        adult_ws,
        db,
        {
            "type": "onboard_confirm",
            **_ask(sid, pub),
            "kind": "child",
            "profile_id": pid,
        },
    )
    ok = adult_ws.last()
    assert ok["type"] == "onboard_confirm_ok", ok
    watch = ok["client_id"]
    assert watch_ws.last()["type"] == "onboard_ok"
    row = await db.fetchone(
        "SELECT set_up_by_client_id, profile_id, seal_key FROM paired_clients "
        "WHERE id = ?",
        (watch,),
    )
    assert row["set_up_by_client_id"] == child
    assert row["profile_id"] == await store.default_profile_for(db, "watchos", False)
    _r, prof, _rules = await store.resolve_client(db, watch)
    assert prof["id"] == pid
    # The child's phone carries the watch's access, sealed to the watch.
    await db.store_client_token(
        watch, ha_user_id=WATCH_USER, refresh_token_id="rt", token="watch-token"
    )
    answer = await watch_access.message(db, None, watch, child)
    assert answer["status"] == "ready"
    plain = _open(priv, answer)
    assert plain["config"]["profile"]["child_protection"] is True


async def test_without_the_rule_the_child_cannot_start(db):
    _pid, child = await _child_phone(db, kids_watch=False)
    child_ws = _child_socket(db, child)
    _w, _priv, pub, sid, _code = await _watch_session(db)
    await _send(child_ws, db, {"type": "onboard_lookup", **_ask(sid, pub)})
    assert child_ws.last()["reason"] == onboarding.NOT_PERMITTED


async def test_only_an_adult_who_may_set_up_children(db):
    pid, child = await _child_phone(db)
    child_ws = _child_socket(db, child)
    _w, _priv, pub, sid, code = await _watch_session(db)
    await _send(child_ws, db, {"type": "onboard_lookup", **_ask(sid, pub)})
    adult_ws, _adult = await _phone(
        db, overrides={"setup_devices": {"enabled": True, "kinds": ["guest", "tv"]}}
    )
    await _send(adult_ws, db, {"type": "onboard_lookup", "code": code})
    assert adult_ws.last()["reason"] == onboarding.KIND_NOT_ALLOWED
    # Another child's phone cannot take the session over.
    _pid2, other_child = await _child_phone(db)
    other_ws = _child_socket(db, other_child)
    await _send(other_ws, db, {"type": "onboard_lookup", **_ask(sid, pub)})
    assert other_ws.last()["reason"] == onboarding.NOT_PERMITTED


async def test_confirm_fails_when_the_phone_lost_the_rule(db):
    pid, child = await _child_phone(db)
    child_ws = _child_socket(db, child)
    _w, _priv, pub, sid, _code = await _watch_session(db)
    await _send(child_ws, db, {"type": "onboard_lookup", **_ask(sid, pub)})
    adult_ws, _adult = await _phone(
        db, overrides={"setup_devices": {"enabled": True, "kinds": ["child"]}}
    )
    await store.put_override(
        db,
        child,
        "kids_watch",
        value=False,
        user_choice=False,
        expires_at=None,
        note=None,
        actor="admin",
    )
    await _send(
        adult_ws,
        db,
        {
            "type": "onboard_confirm",
            **_ask(sid, pub),
            "kind": "child",
            "profile_id": pid,
        },
    )
    assert adult_ws.last()["reason"] == onboarding.NOT_PERMITTED
    assert ws._clients  # the session's socket is still registered (not created)
    count = await db.fetchone(
        "SELECT COUNT(*) AS n FROM paired_clients WHERE platform = 'watchos'"
    )
    assert count["n"] == 0


# ── verification follow-ups ─────────────────────────────────────────────────


async def _rest_control(db, ha, client_id, entity="light.kids", service="turn_on"):
    import json as _json

    from api import profiles as profiles_api

    from .test_kids_mode import _rest_request

    request = _rest_request(
        db, "/api/client/control", None, {"entity_id": entity, "service": service}
    )
    request.state.auth_type, request.state.client_id = "client", client_id
    request.app.state.ha_client = ha
    answer = await profiles_api.client_control(request)
    return _json.loads(answer.body) if hasattr(answer, "body") else answer


async def test_hub_control_path_refuses_a_watch_and_counts_on_the_phone(
    db, monkeypatch
):
    """Point 1: the watch's secret switches nothing on the hub's own control
    path, and the phone's hub path shares the counter with the watch's
    kids-check (one helper, kids_mode.kids_subject)."""

    async def _caller(_ha, _token, entity_id, _action, _data, _grant):
        return {"entity_id": entity_id}

    monkeypatch.setattr(hub_control, "ha_caller", _caller)
    await _home(db)
    _pid, phone = await _child_phone(db, kids={"rate_limit": RATE_2})
    watch = await _pair(db, phone)
    await db.store_client_token(
        phone, ha_user_id=PHONE_USER, refresh_token_id="rt-p", token="phone-token"
    )
    ha = FakeHA(STATES)
    refused = await _rest_control(db, ha, watch)
    assert refused["type"] == "control_error"
    assert refused["reason"] == hub_control.ERR_NOT_HUB_CONTROLLED
    check = await enforcement.kids_check(
        db, ha, client_id=watch, entity_id="light.kids", action="turn_on", grant=None
    )
    assert check["allowed"]
    assert (await _rest_control(db, ha, phone))["type"] == "control_ok"
    third = await _rest_control(db, ha, phone)
    assert third["type"] == "rule_blocked", third
    assert kids_mode.kids_subject((await store.resolve_client(db, watch))[0]) == phone


async def _narrow_adult(db, **rules):
    body = catalog.materialize("adult")
    body["rules"].update({"watch": True, **rules})
    async with db.transaction():
        pid = await profile_rows.insert_profile(
            db,
            name=f"Teen {datetime.now().timestamp()}",
            body=body,
            now=Database.now(),
            actor=None,
        )
        await db.execute(
            "UPDATE profiles SET child_protection = 0, kind = 'person' WHERE id = ?",
            (pid,),
        )
    return pid


async def test_leaving_parental_controls_for_enough_keeps_and_rotates(db, to_phone):
    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    adult = await starter_profile(db, "adult")

    async def write():
        await store.assign(db, phone, adult, discard_overrides=False)

    await _write(db, write)
    assert await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch,))
    assert not (await store.resolve_client(db, watch))[1]["child_protection"]
    rotation = await db.fetchone(
        "SELECT token_action FROM token_requests WHERE client_id = ?", (watch,)
    )
    assert rotation == {"token_action": "rotate"}


async def test_catch_up_rotates_a_childs_watch_token_from_before(db):
    """Point 4: a lost re-scope rotation is caught up like a child device's."""
    from services import device_provisioning

    phone = await add_client(
        db,
        platform="ios",
        profile="adult",
        name="iPhone Child One",
        ha_user_id=PHONE_USER,
    )
    watch = await _pair(db, phone)
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_minted_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", watch),
        )
    child = await _profile(db)
    # Assignment with its audit row, but the rotation of the effects got lost.
    async with db.transaction():
        await store.assign(db, phone, child, discard_overrides=False)
        await profile_rows.audit(
            db,
            "profile_assigned",
            now=store.now_iso(),
            actor="admin",
            profile_id=child,
            client_id=phone,
            before={"profile_id": await starter_profile(db, "adult")},
            after={"profile_id": child},
            note=None,
            rule_key=None,
        )
    started = await device_provisioning.secure_keyless_tokens(db)
    assert started >= 1
    assert await db.fetchone(
        "SELECT 1 FROM token_requests WHERE client_id = ?", (watch,)
    )
    # Idempotent: the open rotation is reused.
    assert await device_provisioning.secure_keyless_tokens(db) == 0


async def test_a_watch_is_never_a_family_manager(db):
    """Point 5."""
    from fastapi import HTTPException

    from api import family as family_api
    from api.family import ClientFamily

    from .test_onboarding import _request

    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    adult_watch = await _pair(
        db, await add_client(db, platform="ios", profile="adult", name="iPhone")
    )
    for cid in (watch, adult_watch):
        with pytest.raises(HTTPException) as e:
            await family_api.put_client_family(
                _request(db),
                cid,
                ClientFamily(scope={"all": True, "client_ids": []}, notify_targets=[]),
            )
        assert e.value.status_code == 409


async def test_activity_of_a_childs_watch_counts_for_the_child(db):
    """Point 7: log context of a child's watch = the child's profile and owner."""
    from services import event_log

    pid, phone = await _child_phone(db)
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET owner_label = 'Child One' WHERE id = ?", (phone,)
        )
    watch = await _pair(db, phone)
    ctx = await event_log.client_context(db, watch)
    assert ctx["child_protection"] is True
    assert ctx["profile_id"] == pid and ctx["owner_label"] == "Child One"
    assert ctx["device_name"] == "Apple Watch"


async def test_temporary_allow_carries_its_end_to_watch(db):
    """Point 9: the phone's watch entry ends with a temporary kids_watch."""
    _pid, phone = await _child_phone(db, kids_watch=False)
    end = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0)
    await store.put_override(
        db,
        phone,
        "kids_watch",
        value=True,
        user_choice=False,
        expires_at=end.isoformat(),
        note=None,
        actor="admin",
    )
    rules = (await store.resolve_client(db, phone))[2]
    assert rules["watch"]["value"] is True
    assert rules["watch"]["expires_at"] == rules["kids_watch"]["expires_at"]
    assert rules["watch"]["expires_at"] is not None
    assert rules["watch"]["then"] is False


# ── an adult's watch follows its phone, never removed ─────────────


def _ov(db, cid, key, value, minutes=None):
    end = (
        (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        if minutes
        else None
    )
    return lambda: store.put_override(
        db,
        cid,
        key,
        value=value,
        user_choice=False,
        expires_at=end,
        note=None,
        actor="admin",
    )


async def _adult_pair(db):
    phone = await add_client(
        db, platform="ios", profile="adult", name="iPhone", ha_user_id=PHONE_USER
    )
    return phone, await _pair(db, phone)


async def _exists(db, cid):
    return bool(await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (cid,)))


async def _not_more_than_phone(db, watch, phone):
    from services import onboarding

    w = (await store.resolve_client(db, watch))[2]
    p = (await store.resolve_client(db, phone))[2]
    assert onboarding.stronger_rules(w, p) == []
    return w


@pytest.mark.parametrize(
    "key,value,minutes",
    [("remote_access", False, 60), ("quick_access", "off", 30)],
)
async def test_temporary_limit_of_the_phone_limits_the_watch_and_ends(
    db, to_phone, key, value, minutes
):
    from services.profiles import expiry_task

    phone, watch = await _adult_pair(db)
    own = (await store.resolve_client(db, watch))[2][key]["value"]
    change = await _write(db, _ov(db, phone, key, value, minutes))
    assert change.watches_removed == []
    assert await _exists(db, watch)
    w = await _not_more_than_phone(db, watch, phone)
    assert w[key]["value"] == value and w[key]["reason"] == "limited_by_phone"
    assert w[key]["expires_at"] and w[key]["then"] == own
    if key == "remote_access":
        assert w["remote_control"]["reason"] == "requires_remote_access"
        assert (await store.remote_allowed_map(db, [watch]))[watch] is False
    later = datetime.now(timezone.utc) + timedelta(minutes=minutes + 1)
    await expiry_task.expire_due(db, now=later)
    assert await _exists(db, watch)
    back = (await store.resolve_client(db, watch))[2][key]
    assert back["value"] == own and back["reason"] != "limited_by_phone"


async def test_access_hours_of_the_phone_limit_the_watch(db, to_phone):
    phone, watch = await _adult_pair(db)
    hours = {
        "enabled": True,
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "07:00",
                "end": "20:00",
            }
        ],
    }
    await _write(db, _ov(db, phone, "access_hours", hours))
    assert await _exists(db, watch)
    w = await _not_more_than_phone(db, watch, phone)
    assert w["access_hours"]["value"] == hours
    assert w["access_hours"]["reason"] == "limited_by_phone"


async def test_nightly_closing_and_unrelated_rules_keep_the_watch(db, to_phone):
    from services import access

    phone, watch = await _adult_pair(db)
    before = (await store.resolve_client(db, watch))[2]
    await access.check_transitions(db)
    await _write(db, _ov(db, phone, "header", False))
    assert await _exists(db, watch)
    after = (await store.resolve_client(db, watch))[2]
    assert all(
        after[k]["value"] == before[k]["value"]
        for k in ("remote_access", "quick_access")
    )


async def test_the_watchs_own_adjustment_is_kept_and_limited(db, to_phone):
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "control_path", "checked"))
    await _write(db, _ov(db, watch, "control_path", "direct"))
    await _write(db, _ov(db, phone, "header", False))
    assert await _exists(db, watch)
    w = await _not_more_than_phone(db, watch, phone)
    assert w["control_path"]["value"] == "checked"
    assert w["control_path"]["reason"] == "limited_by_phone"
    # The phone is let go: the watch's own adjustment applies again.
    await _write(db, lambda: store.delete_override(db, phone, "control_path"))
    if (await store.resolve_client(db, phone))[2]["control_path"]["value"] == "direct":
        w = (await store.resolve_client(db, watch))[2]
        assert w["control_path"]["value"] == "direct"
        assert w["control_path"]["reason"] != "limited_by_phone"


async def test_leaving_parental_controls_for_less_keeps_a_limited_watch(db, to_phone):
    """The child-profile → narrow adult profile attack stays closed, by the limit."""
    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    teen = await _narrow_adult(
        db, remote_access=False, control_path="checked", quick_access="off"
    )

    async def write():
        await store.assign(db, phone, teen, discard_overrides=False)

    change = await _write(db, write)
    assert change.watches_left == [watch] and change.watches_removed == []
    assert await _exists(db, watch)
    w = await _not_more_than_phone(db, watch, phone)
    assert w["remote_access"]["value"] is False
    assert w["control_path"]["value"] == "checked"
    assert w["quick_access"]["value"] == "off"
    rotation = await db.fetchone(
        "SELECT token_action FROM token_requests WHERE client_id = ?", (watch,)
    )
    assert rotation == {"token_action": "rotate"}


async def test_a_childs_watch_removal_is_previewed_and_named(db, to_phone):
    pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    _, dry = await pchanges.run(db, _ov(db, phone, "kids_watch", False), dry_run=True)
    assert dry.preview()["watches_removed"] == [
        {"client_id": watch, "name": "Apple Watch", "reason": "kids_watch_off"}
    ]
    assert await _exists(db, watch)
    await _write(db, _ov(db, phone, "kids_watch", False))
    assert not await _exists(db, watch)
    assert (
        phone,
        {
            "type": "watch_access",
            "client_id": watch,
            "status": "gone",
            "reason": "kids_watch_off",
        },
    ) in to_phone
    # Remote access off: named as such.
    _p2, phone2 = await _child_phone(db)
    watch2 = await _pair(db, phone2)
    _, dry = await pchanges.run(
        db, _ov(db, phone2, "remote_access", False), dry_run=True
    )
    assert dry.preview()["watches_removed"][0]["reason"] == "remote_access_off"


async def test_family_events_of_a_childs_watch_count_for_the_child(db):
    from services.family import events as family_events

    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    seen = []

    async def sink(event, _extra):
        seen.append(event)

    family_events.add_sink(sink)
    try:
        await family_events.kids_control(db, watch, "light.kids", "turn_on")
    finally:
        family_events.remove_sink(sink)
    assert seen[0]["client_id"] == phone
    assert seen[0]["device_name"] == "Apple Watch"
    entry = await family_events.log(
        db, "kids_control", client_id=watch, category="activity", event_kind=None
    )
    assert entry["client_id"] == phone
    assert entry["detail"]["watch"] == {"client_id": watch, "name": "Apple Watch"}


async def test_log_context_resolves_nothing_for_ordinary_devices(db, monkeypatch):
    from services import event_log

    phone = await add_client(db, platform="ios", profile="adult", name="iPhone")

    async def _boom(*_a, **_k):
        raise AssertionError("resolve_client on the log path")

    monkeypatch.setattr(store, "resolve_client", _boom)
    ctx = await event_log.client_context(db, phone)
    assert ctx["device_name"] == "iPhone"


async def test_temporary_remote_access_carries_its_end_to_watch(db):
    _pid, phone = await _child_phone(db, remote=False)
    end = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(microsecond=0)
    await store.put_override(
        db,
        phone,
        "remote_access",
        value=True,
        user_choice=False,
        expires_at=end.isoformat(),
        note=None,
        actor="admin",
    )
    rules = (await store.resolve_client(db, phone))[2]
    assert rules["watch"]["value"] is True
    assert rules["kids_watch"]["expires_at"] == rules["remote_access"]["expires_at"]
    assert rules["watch"]["expires_at"] == rules["remote_access"]["expires_at"]
    assert rules["watch"]["then"] is False


# ── the limit is the meet, never a replacement ─────────────────────


async def test_limit_never_widens_the_watchs_areas(db, to_phone):
    """Phone {Wohnen, Kinder}, watch restricted to {Garage}: the watch gets
    no areas at all, never the phone's."""
    await _home(db)
    garage = await _area(db, "Garage", ["cover.garage"])
    rows = await db.fetchall("SELECT id, name FROM areas")
    ids = {r["name"]: r["id"] for r in rows}
    phone = await add_client(
        db,
        platform="ios",
        profile="adult",
        name="iPhone",
        ha_user_id=PHONE_USER,
        overrides={
            "areas": {
                "mode": "selected",
                "area_ids": [ids["Wohnzimmer"], ids["Kinderzimmer"]],
                "tags": [],
                "own_area": False,
            }
        },
    )
    watch = await _pair(db, phone)
    await _write(
        db,
        _ov(
            db,
            watch,
            "areas",
            {"mode": "selected", "area_ids": [garage], "tags": [], "own_area": False},
        ),
    )
    w = (await store.resolve_client(db, watch))[2]["areas"]
    assert w["value"]["area_ids"] == [] and w["value"]["mode"] == "selected"
    snap = {s["client_id"]: s for s in await enforcement.build_all(db, FakeHA(STATES))}[
        watch
    ]
    assert snap["entities"] == {}


async def test_unreadable_phone_limits_the_watch_to_the_strictest(db, to_phone):
    phone, watch = await _adult_pair(db)
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET set_up_by_client_id = 'gone-phone' WHERE id = ?",
            (watch,),
        )
    for rules in (
        (await store.resolve_client(db, watch))[2],
        (await store.resolve_all(db, [watch]))[watch]["rules"],
    ):
        assert rules["remote_access"]["value"] is False
        assert rules["control_path"]["value"] == "checked"
        assert rules["quick_access"]["value"] == "off"
        assert rules["areas"]["value"]["area_ids"] == []
    assert (await store.remote_allowed_map(db, [watch]))[watch] is False


async def test_the_removal_reason_reaches_a_phone_that_asks_later(db, to_phone):
    _pid, phone = await _child_phone(db)
    watch = await _pair(db, phone)
    await _write(db, _ov(db, phone, "kids_watch", False))
    assert not await _exists(db, watch)
    later = await watch_access.message(db, None, watch, phone)
    assert later == {
        "type": "watch_access",
        "client_id": watch,
        "status": "gone",
        "reason": "kids_watch_off",
    }
    other, _ = await _adult_pair(db)
    assert "reason" not in await watch_access.message(db, None, watch, other)


ZONE = "Europe/Zurich"
PROPERTY_NOW = "2026-03-28T23:00:00Z"
PROPERTY_MOMENTS = (
    "2026-03-28T23:00:00Z",
    "2026-03-29T01:00:00Z",
    "2026-03-29T05:00:00Z",
)
ENTITIES = (
    ("light.x", None),
    ("cover.door", "garage"),
    ("cover.door", None),
    ("cover.door", "door"),
    ("cover.g", "garage"),
    ("cover.blinds", "blind"),
    ("cover.blinds", None),
    ("lock.a", None),
    ("switch.s", None),
)
ZONES = ("Europe/Zurich", "America/New_York", "Asia/Tokyo", None)
AREAS = ("a1", "a2", "a3", "a4", "a5")


def _instants():
    from datetime import datetime as _dt

    out = []
    for day in (
        "2026-03-08",
        "2026-03-28",
        "2026-03-29",
        "2026-10-25",
        "2026-11-01",
        "2026-11-23",
    ):
        base = _dt.fromisoformat(day + "T00:00:00+00:00") - timedelta(hours=3)
        out += [base + timedelta(minutes=15 * i) for i in range(4 * 30)]
    return out


def _property_pairs(n: int):
    import random

    from services.profiles import resolver as res

    cat = catalog.catalog()
    by_key = {r["key"]: r for r in cat["rules"]}
    keys = res.limited_keys(cat)
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    def value(rule, rnd):
        kind = rule["watch_limit"]
        if kind == "areas":
            if rnd.random() < 0.2:
                return {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
            return {
                "mode": "selected",
                "area_ids": rnd.sample(AREAS, rnd.randint(0, 3)),
                "tags": rnd.sample(["t1", "t2", "t3"], rnd.randint(0, 2)),
                "own_area": rnd.random() < 0.3,
            }
        if kind == "schedule":
            if rnd.random() < 0.25:
                return {"enabled": False, "windows": []}
            return {
                "enabled": True,
                "windows": [
                    {
                        "days": sorted(
                            rnd.sample(days, rnd.randint(1, 7)), key=days.index
                        ),
                        "start": f"{rnd.randint(0, 23):02d}:{rnd.choice((0, 15, 30, 45)):02d}",
                        "end": f"{rnd.randint(0, 23):02d}:{rnd.choice((0, 15, 30, 45)):02d}",
                    }
                    for _ in range(rnd.randint(0, 4))
                ],
            }
        if kind == "remote_levels":
            levels = ["hidden", "view", "control"]
            return {
                "default": rnd.choice(levels),
                "domains": {
                    d: rnd.choice(levels)
                    for d in rnd.sample(
                        [
                            "lock",
                            "light",
                            "cover",
                            "cover:garage",
                            "cover:door",
                            "switch",
                        ],
                        rnd.randint(0, 4),
                    )
                },
                "entities": {
                    e: rnd.choice(levels)
                    for e in rnd.sample(
                        ["light.x", "cover.door", "cover.g", "cover.blinds"],
                        rnd.randint(0, 2),
                    )
                },
            }
        if kind == "kinds":
            return {
                "enabled": rnd.random() < 0.6,
                "kinds": rnd.sample(["guest", "child", "tv", "person"], 2),
            }
        return rnd.choice(rule["order"])

    def body(rnd):
        b = catalog.materialize("adult")
        b["rules"].update({k: value(by_key[k], rnd) for k in keys})
        return {**b, "kind": "person", "child_protection": False}

    ends = [None, "2026-03-29T00:30:00Z", "2026-03-29T03:00:00Z"]
    for seed in range(n):
        rnd = random.Random(seed)
        tags = {a: rnd.sample(["t1", "t2", "t3"], rnd.randint(0, 2)) for a in AREAS}
        known = {
            e: rnd.choice(
                ("garage", "door", "blind", None) if e[:6] == "cover." else (None,)
            )
            for e in sorted({e for e, _ in ENTITIES})
            if rnd.random() < 0.6
        }
        world = {"tags": tags, "classes": known, "zone": rnd.choice(ZONES)}
        sides = []
        for platform in ("watchos", "ios"):
            sides.append(
                dict(
                    platform=platform,
                    access={"kind": "member", "expires_at": None},
                    profile=body(rnd),
                    overrides=[
                        {
                            "rule": k,
                            "value": value(by_key[k], rnd),
                            "expires_at": rnd.choice(ends),
                        }
                        for k in rnd.sample(keys, rnd.randint(0, 4))
                    ],
                    device={
                        "own_area_id": rnd.choice([None, "a1"]),
                        "has_remote_pin": False,
                    },
                    now=PROPERTY_NOW,
                )
            )
        yield cat, by_key, keys, sides, world


def _real(world):
    """(entity, class) pairs that can be real: a known entity with its class."""
    known = world["classes"]
    return [(e, c) for e, c in ENTITIES if e not in known or known[e] == c]


def _is_open(v, t, zone):
    from services import kids_rules

    if not v.get("enabled"):
        return True
    ws = v.get("windows") or []
    return bool(ws) and kids_rules.quiet_until(ws, t, zone) is not None


def _zones(world):
    return [world["zone"]] if world["zone"] else [z for z in ZONES if z]


def _runtime_leq(rule, a, b, world, instants):
    """``a`` gives at most ``b`` by what the hub does at runtime."""
    from services.profiles import resolver as res

    tags = world["tags"]
    kind = rule["watch_limit"]
    if kind == "areas":

        def reach(v):
            if v.get("mode") == "all":
                return None
            ts = set(v.get("tags", []))
            return set(v.get("area_ids", [])) | {
                x for x, t in tags.items() if ts & set(t)
            }

        ra, rb = reach(a), reach(b)
        return rb is None or (ra is not None and ra <= rb)
    if kind == "remote_levels":
        order = ["hidden", "view", "control"]
        return all(
            order.index(res.remote_level({"remote_control": {"value": a}}, e, c))
            <= order.index(res.remote_level({"remote_control": {"value": b}}, e, c))
            for e, c in _real(world)
        )
    if kind == "schedule":
        return all(
            _is_open(b, t, z)
            for z in _zones(world)
            for t in instants
            if _is_open(a, t, z)
        )
    return res.leq(rule, a, b)


def _tight(rule, got, w, p, world, instants):
    """Not stricter than necessary: a known entity gets the lower of both
    sides' levels; access hours with one side open or both the same are
    exactly the other side on every instant."""
    from services.profiles import resolver as res

    order = ["hidden", "view", "control"]
    if rule["watch_limit"] == "remote_levels":
        for e, c in world["classes"].items():
            g, a, b = (
                res.remote_level({"remote_control": {"value": v}}, e, c)
                for v in (got, w, p)
            )
            if g != min(a, b, key=order.index):
                return (e, c, g, a, b)
    if rule["watch_limit"] == "schedule":
        same = sorted(map(str, w.get("windows") or [])) == sorted(
            map(str, p.get("windows") or [])
        )
        if not w.get("enabled") or not p.get("enabled") or same:
            other = p if not w.get("enabled") else w
            for z in _zones(world):
                for t in instants:
                    if _is_open(got, t, z) != _is_open(other, t, z):
                        return (z, t, got, other)
    return None


def test_limit_property_never_more_than_either_side():
    """Random watch/phone pairs with random area tags, known device classes
    and hub zones (three, or not known) around the DST nights: projected
    (expires_at/then) and recomputed at three moments, the limited value never
    gives more than the watch's own nor its phone's – by runtime semantics
    (entities with their real classes, areas through their tags, access at
    real instants) – and, recomputed, is not stricter than necessary."""
    from services.profiles import resolver as res

    instants = _instants()

    def at(entry, rule, when):
        end = entry.get("expires_at")
        if end and res.parse_ts(when) >= res.parse_ts(end):
            then = entry.get("then")
            return then if then is not None else res._strictest(rule)
        return entry["value"]

    for cat, by_key, keys, (w_in, p_in), world in _property_pairs(150):
        kw = {
            "area_tags": world["tags"],
            "entity_classes": world["classes"],
            "time_zone": world["zone"],
        }
        first = res.limit_to(
            cat,
            res.resolve(cat, **w_in),
            res.resolve(cat, **p_in),
            now=PROPERTY_NOW,
            **kw,
        )
        for when in PROPERTY_MOMENTS:
            own = res.resolve(cat, **{**w_in, "now": when})
            phone = res.resolve(cat, **{**p_in, "now": when})
            again = res.limit_to(cat, own, phone, now=when, **kw)
            for key in keys:
                rule, w, p = by_key[key], own[key], phone[key]
                if "not_applicable" in (
                    w["source"],
                    p["source"],
                    first[key]["source"],
                    again[key]["source"],
                ):
                    continue
                # control_path hub counts as checked: a watch needs its own key.
                wv, pv = (
                    ("checked" if key == "control_path" and v == "hub" else v)
                    for v in (w["value"], p["value"])
                )
                for got in (at(first[key], rule, when), again[key]["value"]):
                    assert _runtime_leq(rule, got, wv, world, instants), (
                        key,
                        when,
                        got,
                    )
                    assert _runtime_leq(rule, got, pv, world, instants), (
                        key,
                        when,
                        got,
                    )
                assert (
                    _tight(
                        rule,
                        again[key]["value"],
                        w["value"],
                        p["value"],
                        world,
                        instants,
                    )
                    is None
                ), (key, when)


def test_limit_equals_the_contract_reference():
    import importlib.util
    import os
    from pathlib import Path

    from services.profiles import resolver as res

    root = os.environ.get("CONTRACTS_DIR")
    ref_path = Path(root or "") / "tools/profiles_ref.py"
    if not root or not ref_path.exists():
        pytest.skip("contract checkout with tools/profiles_ref.py not found")
    spec = importlib.util.spec_from_file_location("profiles_ref_kidwatch", ref_path)
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)
    for cat, _by_key, _keys, (w_in, p_in), world in _property_pairs(300):
        for kw in (
            {},
            {"area_tags": world["tags"], "time_zone": ZONE, "now": PROPERTY_NOW},
            {
                "area_tags": world["tags"],
                "entity_classes": world["classes"],
                "time_zone": world["zone"],
                "now": PROPERTY_NOW,
            },
        ):
            hub = res.limit_to(
                cat, res.resolve(cat, **w_in), res.resolve(cat, **p_in), **kw
            )
            want = ref.limit_to(
                cat, ref.resolve(cat, **w_in), ref.resolve(cat, **p_in), **kw
            )
            assert hub == want


async def test_garage_never_opens_from_away_through_a_phone_entity_key(db, to_phone):
    """The watch hides garages from away; its phone names cover.door (a
    garage) by entity: the watch's snapshot never gets cover.door control."""
    await _area(db, "Garage", ["cover.door"])
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "remote_access", True))
    await _write(db, _ov(db, watch, "remote_access", True))
    await _write(
        db,
        _ov(
            db,
            watch,
            "remote_control",
            {
                "default": "control",
                "domains": {"cover:garage": "hidden"},
                "entities": {},
            },
        ),
    )
    await _write(
        db,
        _ov(
            db,
            phone,
            "remote_control",
            {
                "default": "control",
                "domains": {},
                "entities": {"cover.door": "control"},
            },
        ),
    )
    states = {
        "cover.door": {"state": "closed", "attributes": {"device_class": "garage"}}
    }
    snaps = {s["client_id"]: s for s in await enforcement.build_all(db, FakeHA(states))}
    assert snaps[watch]["entities"].get("cover.door", {}).get("remote") != "control"


async def test_areas_meet_on_the_real_areas_by_tag(db, to_phone):
    """The phone grants rooms by tag, the watch pins rooms by id: the watch
    gets the pinned room that carries the tag."""
    up = await _area(db, "Oben", ["light.up"])
    down = await _area(db, "Unten", ["light.down"])
    async with db.transaction():
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, 'upstairs')", (up,)
        )
    phone, watch = await _adult_pair(db)
    await _write(
        db,
        _ov(
            db,
            phone,
            "areas",
            {
                "mode": "selected",
                "area_ids": [],
                "tags": ["upstairs"],
                "own_area": False,
            },
        ),
    )
    await _write(
        db,
        _ov(
            db,
            watch,
            "areas",
            {"mode": "selected", "area_ids": [up, down], "tags": [], "own_area": False},
        ),
    )
    w = (await store.resolve_client(db, watch))[2]["areas"]["value"]
    assert w["area_ids"] == [up]


async def test_unreadable_phone_is_logged_once(db, to_phone, caplog):
    phone, watch = await _adult_pair(db)
    store._unreadable_logged.discard(watch)
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET set_up_by_client_id = 'gone-phone' WHERE id = ?",
            (watch,),
        )
    with caplog.at_level("WARNING", logger="hub.profiles"):
        for _ in range(3):
            await store.resolve_client(db, watch)
            await store.resolve_all(db, [watch])
    assert sum("cannot be read" in r.message for r in caplog.records) == 1


def test_an_unclassified_rule_is_refused():
    from services.profiles import resolver as res

    rule = {"key": "new_rule", "watch_limit": "replace"}
    with pytest.raises(ValueError):
        res.meet(rule, 1, 2)


async def test_spring_forward_gap_never_opens_the_watch_when_the_phone_is_closed(
    db, to_phone, monkeypatch
):
    """The phone's windows touch in weekly minutes but not on the real night
    (Sun 02:30 does not exist, it means 03:30): the watch is closed when the
    phone is (03:15–03:30 CEST on 29 March 2026)."""
    from datetime import datetime as _dt

    from services import access, kids_rules

    monkeypatch.setattr(access, "time_zone", lambda: ZONE)
    phone, watch = await _adult_pair(db)
    phone_hours = {
        "enabled": True,
        "windows": [
            {"days": ["sat"], "start": "22:00", "end": "03:15"},
            {"days": ["sun"], "start": "02:30", "end": "16:00"},
        ],
    }
    own = {
        "enabled": True,
        "windows": [{"days": ["sat"], "start": "22:00", "end": "07:30"}],
    }
    await _write(db, _ov(db, phone, "access_hours", phone_hours))
    await _write(db, _ov(db, watch, "access_hours", own))
    rules = (await store.resolve_client(db, watch, now="2026-03-28T20:00:00Z"))[2]
    windows = rules["access_hours"]["value"]["windows"]
    night = _dt(2026, 3, 29, 1, 15, tzinfo=timezone.utc)
    assert kids_rules.quiet_until(phone_hours["windows"], night, ZONE) is None
    assert kids_rules.quiet_until(windows, night, ZONE) is None


async def _blinds_pair(db, cls):
    """The phone may control the blinds from away by entity key (default
    view); the watch's adult profile controls every cover but garages."""
    await _area(db, "Wohnen", ["cover.living_blinds"])
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "remote_access", True))
    await _write(db, _ov(db, watch, "remote_access", True))
    await _write(
        db,
        _ov(
            db,
            phone,
            "remote_control",
            {
                "default": "view",
                "domains": {},
                "entities": {"cover.living_blinds": "control"},
            },
        ),
    )
    states = {
        "cover.living_blinds": {
            "state": "open",
            "attributes": {"device_class": cls} if cls else {},
        }
    }
    snaps = {s["client_id"]: s for s in await enforcement.build_all(db, FakeHA(states))}
    return snaps[watch]["entities"].get("cover.living_blinds", {}).get("remote")


async def test_watch_keeps_control_its_phone_was_given_for_a_known_class(db, to_phone):
    """With HA's state the blinds are class blind: the watch's own profile
    controls them and its phone may, so the watch keeps controlling them."""
    assert await _blinds_pair(db, "blind") == "control"


async def test_a_known_garage_stays_limited_by_the_watchs_own_rule(db, to_phone):
    assert await _blinds_pair(db, "garage") == "view"


def test_an_entity_without_a_known_state_gets_the_lowest_level():
    from services.profiles import resolver as res

    rule = {r["key"]: r for r in catalog.catalog()["rules"]}["remote_control"]
    watch = catalog.materialize("adult")["rules"]["remote_control"]
    phone = {
        "default": "view",
        "domains": {},
        "entities": {"cover.living_blinds": "control"},
    }
    out = res.meet(rule, watch, phone, {"entity_classes": {}})
    assert (
        res.remote_level(
            {"remote_control": {"value": out}}, "cover.living_blinds", "blind"
        )
        == "view"
    )
    out = res.meet(
        rule, watch, phone, {"entity_classes": {"cover.living_blinds": "blind"}}
    )
    assert out["entities"] == {"cover.living_blinds": "control"}


def test_store_reads_device_classes_from_the_live_states(monkeypatch):
    class Mirror:
        mirror_ready = True

        class ws:
            @staticmethod
            def all_states():
                return [
                    {"entity_id": "cover.a", "attributes": {"device_class": "blind"}},
                    {"entity_id": "light.b", "attributes": {}},
                    {"entity_id": "cover.c", "attributes": {"device_class": ""}},
                ]

    monkeypatch.setattr(store, "_states_client", Mirror())
    assert store.entity_classes() == {
        "cover.a": "blind",
        "light.b": None,
        "cover.c": None,
    }
    monkeypatch.setattr(store, "_states_client", None)
    assert store.entity_classes() is None


async def test_same_rules_keep_the_watchs_own_entry(db, to_phone):
    """Phone and watch give the same (the phone's value only spelled with a
    key that changes nothing): no limited_by_phone, no badge."""
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "remote_access", True))
    await _write(db, _ov(db, watch, "remote_access", True))
    same = catalog.materialize("adult")["rules"]["remote_control"]
    await _write(
        db,
        _ov(
            db,
            phone,
            "remote_control",
            {**same, "domains": {**same["domains"], "cover": "control"}},
        ),
    )
    rc = (await store.resolve_client(db, watch))[2]["remote_control"]
    assert rc["reason"] != "limited_by_phone"
    assert rc["value"] == same


def _sat(start, end):
    return {"enabled": True, "windows": [{"days": ["sat"], "start": start, "end": end}]}


@pytest.mark.parametrize("zone", [ZONE, None])
async def test_watch_without_hours_gets_exactly_its_phones_window(
    db, to_phone, monkeypatch, zone
):
    """Phone Sat 17:00–02:30 (02:30 does not exist on the spring-forward
    night), watch without hours: nothing is intersected, nothing trimmed."""
    from services import access

    monkeypatch.setattr(access, "time_zone", lambda: zone)
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "access_hours", _sat("17:00", "02:30")))
    rules = (await store.resolve_client(db, watch, now="2026-03-28T20:00:00Z"))[2]
    assert rules["access_hours"]["value"] == _sat("17:00", "02:30")


async def test_identical_hours_stay_the_watchs_own(db, to_phone, monkeypatch):
    from services import access

    monkeypatch.setattr(access, "time_zone", lambda: ZONE)
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "access_hours", _sat("17:00", "02:30")))
    await _write(db, _ov(db, watch, "access_hours", _sat("17:00", "02:30")))
    rules = (await store.resolve_client(db, watch, now="2026-03-28T20:00:00Z"))[2]
    assert rules["access_hours"]["value"] == _sat("17:00", "02:30")
    assert rules["access_hours"]["reason"] != "limited_by_phone"


@pytest.mark.parametrize("zone, end", [(ZONE, "02:00"), (None, "01:00")])
async def test_different_hours_with_an_end_in_the_gap_are_trimmed(
    db, to_phone, monkeypatch, zone, end
):
    """Watch Sat 22:00–02:45, phone 22:00–03:15: in Zurich 02:45 means 03:45
    that night, so the watch ends at the gap's start; without a known zone
    every zone's jump counts (fail closed: 01:00, the earliest gap start that
    holds 02:45)."""
    from services import access

    monkeypatch.setattr(access, "time_zone", lambda: zone)
    phone, watch = await _adult_pair(db)
    await _write(db, _ov(db, phone, "access_hours", _sat("22:00", "03:15")))
    await _write(db, _ov(db, watch, "access_hours", _sat("22:00", "02:45")))
    rules = (await store.resolve_client(db, watch, now="2026-03-28T20:00:00Z"))[2]
    assert rules["access_hours"]["value"] == _sat("22:00", end)
    assert rules["access_hours"]["reason"] == "limited_by_phone"
