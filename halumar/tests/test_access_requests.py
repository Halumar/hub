"""Ask for early access.

Contract: ``docs/access.md#early-access``, ``data/access-requests.json``,
vector ``fixtures/profiles/access-hours/early-access.json``.
"""

import json
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
from api import access_requests as access_api
from api import family as family_api
from api.pairing import clients as clients_api
from api.ws.client import _get_client_config, _handle_client_message
from fastapi import HTTPException
from services import access, access_requests, enforcement, event_log, kids_mode, tasks
from services.family import store as fstore
from services.profiles import store as pstore

from . import test_family as fam
from .profile_helpers import add_client
from .test_access import _int_request, _sitter_profile
from .test_family import FakeHA, _request

_clean = fam._clean
pushed = fam.pushed

EVERY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Friday 2026-11-20 15:20 Europe/Zurich (14:20 UTC); babysitting from 17:00 local.
FRI_AFTERNOON = datetime(2026, 11, 20, 14, 20, tzinfo=timezone.utc)
WINDOW_START = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_requests():
    access_requests.reset()
    yield
    access_requests.reset()


@pytest.fixture
def clock(monkeypatch):
    state = {"now": FRI_AFTERNOON}
    monkeypatch.setattr(access, "utcnow", lambda: state["now"])
    monkeypatch.setattr(access_requests, "utcnow", lambda: state["now"])
    monkeypatch.setattr(kids_mode, "_tz_cache", (time.monotonic(), "Europe/Zurich"))
    return state


@pytest.fixture
def connected(monkeypatch):
    ids: set[str] = set()
    from api import ws

    monkeypatch.setattr(ws, "get_connected_client_ids", lambda: set(ids))
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    return ids


async def _house(db, *, sitter_remote=False):
    """Papa set Sitter Two (babysitter) up; Mama may set up babysitters; Oma is a
    family manager; Onkel is an adult without rights; Child One is a child."""
    papa = await add_client(
        db,
        name="iPhone Papa",
        owner_label="Papa",
        platform="ios",
        profile="adult",
        overrides={"setup_devices": {"enabled": True, "kinds": ["guest"]}},
        remote=True,
    )
    mama = await add_client(
        db,
        name="iPhone Mama",
        owner_label="Mama",
        platform="ios",
        profile="adult",
        overrides={
            "setup_devices": {"enabled": True, "kinds": ["babysitter", "guest"]}
        },
    )
    oma = await add_client(
        db,
        name="iPad Oma",
        owner_label="Oma",
        platform="ipados",
        profile="adult",
        overrides={"family_manager": True},
    )
    onkel = await add_client(
        db, name="iPhone Onkel", owner_label="Onkel", platform="ios", profile="adult"
    )
    child_one = await add_client(
        db,
        name="iPad Child One",
        owner_label="Child One",
        platform="ipados",
        profile="child",
    )
    pid = await _sitter_profile(db)
    sitter_two = await add_client(
        db,
        name="iPhone Sitter Two",
        owner_label="Sitter Two",
        platform="ios",
        guest=True,
        expires_at="2099-01-01T00:00:00+00:00",
        profile_id=pid,
        set_up_by_client_id=papa,
        set_up_by_name="iPhone Papa",
        set_up_method="onboard",
        remote=sitter_remote or None,
    )
    return types.SimpleNamespace(
        papa=papa,
        mama=mama,
        oma=oma,
        onkel=onkel,
        child_one=child_one,
        sitter_two=sitter_two,
        pid=pid,
    )


async def _msg(db, ha, client_id, message, route="home"):
    return await access_requests.handle_message(db, ha, client_id, message, route)


async def _ask(db, ha, client_id, **extra):
    return await _msg(db, ha, client_id, {"type": "access_request", **extra})


# ── data and rule ───────────────────────────────────────────────────────────


def test_catalog_matches_the_contract_draft():
    cat = access_requests.catalog()
    assert access_requests.duration_ids() == ["until_window_start", "1h", "3h", "today"]
    assert access_requests.decline_messages() == [
        "not_now",
        "not_today",
        "later",
        "call_me",
    ]
    lim = access_requests.limits()
    assert lim["request_timeout_minutes"] == 10
    assert lim["requests_per_hour"] == 3
    assert [a["id"] for a in cat["notify_actions"]] == ["yes_1h", "yes_window", "no"]


async def test_suggestions_switch_the_rule_on(db):
    for suggestion in ("babysitter", "house_sitter", "guest"):
        pid = await _sitter_profile(db, suggestion)
        body = pstore.body_of(await pstore.profile_row(db, pid))
        assert body["rules"]["access_requests"] is True
        assert "access_requests" not in body["fixed"]
    adult = await add_client(db, name="iPhone", platform="ios", profile="adult")
    assert await pstore.effective_rule(db, adult, "access_requests") is False


async def test_recipients_set_up_by_setup_kinds_and_family_managers(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    cfg = await _get_client_config(db, h.sitter_two, ha)
    req = cfg["access"]["request"]
    assert cfg["access"]["hours"]["open"] is False
    assert cfg["access"]["hours"]["reason"] == "schedule"
    assert req["enabled"] is True and req["available"] is True
    assert req["open"] is None and req["next_allowed_at"] is None
    # Papa set her up (first), Mama may set up babysitters, Oma is a family
    # manager (babysitter only); Onkel and the child never.
    assert req["recipients"] == [
        {"name": "Papa", "role": "set_up_by"},
        {"name": "Mama", "role": "setup"},
        {"name": "Oma", "role": "family_manager"},
    ]
    # Without the rule: no access.request at all.
    await _set_rule(db, h.sitter_two, False)
    cfg = await _get_client_config(db, h.sitter_two, ha)
    assert "request" not in cfg["access"]
    # A guest (kind guest): Papa and Mama (setup guest), no family manager.
    gpid = await _sitter_profile(db, "guest")
    await _with_hours(db, gpid)
    guest = await add_client(
        db, name="iPhone Gast", guest=True, platform="ios", profile_id=gpid
    )
    cfg = await _get_client_config(db, guest, ha)
    assert [r["role"] for r in cfg["access"]["request"]["recipients"]] == [
        "setup",
        "setup",
    ]


async def test_tvs_ask_too(db, clock, pushed):
    """Apple TV and Google TV ask with the remote (pair rule)."""
    h = await _house(db)
    ha = FakeHA()
    for platform in ("tvos", "android_tv"):
        tv = await add_client(
            db,
            name=f"TV {platform}",
            platform=platform,
            guest=True,
            expires_at="2099-01-01T00:00:00+00:00",
            profile_id=h.pid,
            set_up_by_client_id=h.papa,
            set_up_by_name="iPhone Papa",
            set_up_method="onboard",
        )
        assert await pstore.effective_rule(db, tv, "access_requests") is True
        cfg = await _get_client_config(db, tv, ha)
        req = cfg["access"]["request"]
        assert req["enabled"] is True and req["available"] is True
        assert req["recipients"][0] == {"name": "Papa", "role": "set_up_by"}
        (state,) = await _ask(db, ha, tv)
        assert state["request"]["status"] == "open"
        (upd,) = await _msg(
            db,
            ha,
            h.papa,
            {
                "type": "access_request_answer",
                "request_id": state["request"]["request_id"],
                "answer": "no",
                "message": "not_today",
            },
        )
        assert upd["request"]["answer"]["message"] == "not_today"


async def _set_rule(db, client_id, value):
    now = pstore.now_iso()
    async with db.transaction():
        await db.execute(
            "INSERT OR REPLACE INTO device_overrides (client_id, rule_key, value_json, "
            "user_choice, expires_at, note, created_by, created_at) "
            "VALUES (?, 'access_requests', ?, 0, NULL, NULL, 'test', ?)",
            (client_id, json.dumps(value), now),
        )


async def _with_hours(db, profile_id, start="17:00", end="01:00"):
    row = await pstore.profile_row(db, profile_id)
    body = json.loads(row["body_json"])
    body["rules"]["access_hours"] = {
        "enabled": True,
        "windows": [{"days": EVERY, "start": start, "end": end}],
    }
    async with db.transaction():
        await db.execute(
            "UPDATE profiles SET body_json = ? WHERE id = ?",
            (json.dumps(body), profile_id),
        )


# ── asking and answering ────────────────────────────────────────────────────


async def test_request_yes_one_hour_opens_and_closes_again(
    db, clock, pushed, connected, monkeypatch
):
    monkeypatch.setattr(enforcement, "notify_change", lambda *a, **k: None)
    h = await _house(db)
    ha = FakeHA()
    connected.update({h.papa, h.mama})
    await access.check_transitions(db)  # closed, logged once
    (state,) = await _ask(db, ha, h.sitter_two, note="  Kinder  schon wach ")
    assert state["type"] == "access_request_state"
    req = state["request"]
    rid = req["request_id"]
    assert req["status"] == "open" and req["note"] == "Kinder schon wach"
    assert req["opens_at"] == "2026-11-20T16:00:00Z"
    assert req["durations"] == ["until_window_start", "1h", "3h", "today"]
    assert req["expires_at"] == "2026-11-20T14:30:00Z"
    # Papa and Mama (connected) get the request; Oma is not connected and has
    # no remote access: nothing.
    updates = [
        (cid, m) for cid, m in pushed["to"] if m["type"] == "access_request_update"
    ]
    assert {cid for cid, _ in updates} == {h.papa, h.mama}
    assert all(m["request"]["can_answer"] for _, m in updates)
    # One open request per device.
    (err,) = await _ask(db, ha, h.sitter_two)
    assert err["reason"] == "request_limit"
    cfg = await _get_client_config(db, h.sitter_two, ha)
    assert cfg["access"]["request"]["open"]["request_id"] == rid
    assert cfg["access"]["request"]["available"] is False
    # Onkel is no recipient; the child neither.
    (err,) = await _msg(
        db,
        ha,
        h.onkel,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "yes",
            "duration": "1h",
        },
    )
    assert err["reason"] == "request_not_found"
    (err,) = await _msg(
        db,
        ha,
        h.child_one,
        {"type": "access_request_answer", "request_id": rid, "answer": "no"},
    )
    assert err["reason"] == "not_permitted"
    # Mama reads the open requests, Papa answers Yes 1 h.
    (lst,) = await _msg(db, ha, h.mama, {"type": "access_requests_get"})
    assert [r["request_id"] for r in lst["requests"]] == [rid]
    (lst,) = await _msg(db, ha, h.onkel, {"type": "access_requests_get"})
    assert lst["requests"] == []
    pushed["to"].clear()
    (upd,) = await _msg(
        db,
        ha,
        h.papa,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "yes",
            "duration": "1h",
        },
    )
    assert upd["type"] == "access_request_update"
    ans = upd["request"]["answer"]
    assert (
        upd["request"]["status"] == "approved" and upd["request"]["can_answer"] is False
    )
    assert ans["until"] == "2026-11-20T15:20:00Z" and ans["duration"] == "1h"
    assert ans["by"] == {
        "kind": "device",
        "client_id": h.papa,
        "name": "Papa",
        "via": "home",
        "owner_label": "Papa",
    }
    # First answer wins.
    (err,) = await _msg(
        db,
        ha,
        h.mama,
        {"type": "access_request_answer", "request_id": rid, "answer": "no"},
    )
    assert err["reason"] == "already_answered"
    assert err["request"]["status"] == "approved"
    # The device is open now with the early access, the key comes back.
    cfg = await _get_client_config(db, h.sitter_two, ha)
    hours = cfg["access"]["hours"]
    assert hours["open"] is True and hours["until"] == "2026-11-20T15:20:00Z"
    assert hours["early"]["by"]["name"] == "Papa"
    assert hours["early"]["request_id"] == rid
    assert "request" in cfg["access"] and cfg["access"]["request"]["available"] is False
    assert (h.sitter_two, {"type": "client_config_updated", "data": {}}) in pushed["to"]
    states = [
        m
        for cid, m in pushed["to"]
        if cid == h.sitter_two and m["type"] == "access_request_state"
    ]
    assert states and states[-1]["request"]["status"] == "approved"
    # Every recipient hears the answer.
    told = {cid for cid, m in pushed["to"] if m["type"] == "access_request_update"}
    assert {h.papa, h.mama} <= told
    # Rule snapshot: the integration lets it through until then.
    entry = (await pstore.resolve_all(db, [h.sitter_two]))[h.sitter_two]
    snap = await enforcement.build_snapshot(
        db,
        ha,
        entry,
        "a" * 32,
        states={},
        stored_qa={},
        tz_name="Europe/Zurich",
        now=clock["now"],
    )
    assert snap["access"]["early_until"] == "2026-11-20T15:20:00Z"
    # Admin list view
    view = await clients_api.client_view(db, h.sitter_two)
    assert view.access_now.open is True and view.access_now.early.by["name"] == "Papa"
    # Log: requested, answered (with the author), opened with early.
    rows = await db.fetchall(
        "SELECT kind, event_kind, actor, action, until, detail_json FROM event_log "
        "WHERE client_id = ? ORDER BY id",
        (h.sitter_two,),
    )
    kinds = [r["kind"] for r in rows]
    assert kinds == [
        "access_closed",
        "access_requested",
        "access_request_answered",
        "access_opened",
    ]
    answered = rows[2]
    assert answered["event_kind"] == "access_request_answered"
    assert answered["actor"] == "Papa" and answered["action"] == "yes"
    assert json.loads(rows[3]["detail_json"])["early"]["by"] == "Papa"
    data = event_log.ha_event_data(
        await event_log.view(
            db,
            await db.fetchone(
                "SELECT * FROM event_log WHERE kind = 'access_request_answered'"
            ),
        )
    )
    assert data["kind"] == "access_request_answered" and data["action"] == "yes"

    # After the hour: closed again, the early access is gone.
    clock["now"] = FRI_AFTERNOON + timedelta(hours=1, minutes=1)
    await access.check_transitions(db, now=clock["now"])
    cfg = await _get_client_config(db, h.sitter_two, ha)
    assert cfg["access"]["hours"]["open"] is False
    assert "early" not in cfg["access"]["hours"]
    assert await db.fetchone(
        "SELECT 1 FROM paired_clients WHERE id = ? AND access_early_json IS NULL",
        (h.sitter_two,),
    )
    last = await db.fetchone(
        "SELECT kind FROM event_log WHERE client_id = ? ORDER BY id DESC LIMIT 1",
        (h.sitter_two,),
    )
    assert last["kind"] == "access_closed"


async def test_until_window_start_continues_into_the_window(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    (upd,) = await _msg(
        db,
        ha,
        h.mama,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "yes",
            "duration": "until_window_start",
        },
    )
    assert upd["request"]["answer"]["until"] == "2026-11-20T16:00:00Z"
    hours = (await _get_client_config(db, h.sitter_two, ha))["access"]["hours"]
    # 17:00-01:00 follows directly: open until 01:00 local.
    assert hours["open"] is True and hours["until"] == "2026-11-21T00:00:00Z"


async def test_durations_today_and_until_start_only_when_close(db, clock):
    now = FRI_AFTERNOON
    assert access_requests.durations_for(None, now) == ["1h", "3h", "today"]
    far = now + timedelta(hours=30)
    assert access_requests.durations_for(far, now) == ["1h", "3h", "today"]
    today = access_requests.until_for("today", now, None, "Europe/Zurich")
    assert today == datetime(2026, 11, 20, 23, 0, tzinfo=timezone.utc)
    assert access_requests.until_for("3h", now, None, None) == now + timedelta(hours=3)


async def test_no_with_message_cooldown_and_hourly_limit(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    (err,) = await _msg(
        db,
        ha,
        h.papa,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "no",
            "message": "nope",
        },
    )
    assert err["reason"] == "invalid_request"
    (upd,) = await _msg(
        db,
        ha,
        h.papa,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "no",
            "message": "later",
        },
    )
    assert upd["request"]["status"] == "declined"
    assert upd["request"]["answer"]["message"] == "later"
    (err,) = await _ask(db, ha, h.sitter_two)
    assert err["reason"] == "cooldown" and 0 < err["retry_after"] <= 601
    cfg = await _get_client_config(db, h.sitter_two, ha)
    assert cfg["access"]["request"]["available"] is False
    assert cfg["access"]["request"]["next_allowed_at"] == "2026-11-20T14:30:00Z"
    # After the cooldown: two more (cancelled) requests, then the hourly limit.
    clock["now"] = FRI_AFTERNOON + timedelta(minutes=11)
    for _ in range(2):
        (state,) = await _ask(db, ha, h.sitter_two)
        assert state["request"]["status"] == "open"
        (state,) = await _msg(
            db,
            ha,
            h.sitter_two,
            {
                "type": "access_request_cancel",
                "request_id": state["request"]["request_id"],
            },
        )
        assert state["request"]["status"] == "cancelled"
    (err,) = await _ask(db, ha, h.sitter_two)
    assert err["reason"] == "request_limit" and err["retry_after"] > 0


async def test_message_rate_limit(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    for _ in range(access_requests.limits()["messages_per_device_per_minute"]):
        await _msg(db, ha, h.mama, {"type": "access_requests_get"})
    (err,) = await _msg(db, ha, h.mama, {"type": "access_requests_get"})
    assert err["reason"] == "rate_limited" and err["retry_after"] > 0


async def test_refusals_open_switch_off_rule_off_and_relay(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    # Away (integration relay): only at home.
    (err,) = await _msg(db, ha, h.sitter_two, {"type": "access_request"}, route="away")
    assert err["reason"] == "home_only"
    # Switch off: a request never overrides Home Assistant.
    await access.set_switch(db, h.sitter_two, False, by="admin", actor="t")
    (err,) = await _ask(db, ha, h.sitter_two)
    assert err["reason"] == "switch_off"
    await access.set_switch(db, h.sitter_two, True, by="admin", actor="t")
    # Open: nothing to ask.
    clock["now"] = WINDOW_START + timedelta(minutes=5)
    (err,) = await _ask(db, ha, h.sitter_two)
    assert err["reason"] == "not_closed"
    clock["now"] = FRI_AFTERNOON
    # Too long note
    (err,) = await _ask(db, ha, h.sitter_two, note="x" * 81)
    assert err["reason"] == "invalid_request"
    # Rule off / an adult without access hours
    await _set_rule(db, h.sitter_two, False)
    (err,) = await _ask(db, ha, h.sitter_two)
    assert err["reason"] == "not_permitted"
    (err,) = await _ask(db, ha, h.papa)
    assert err["reason"] == "not_permitted"


async def test_nobody_to_answer(db, clock, pushed):
    pid = await _sitter_profile(db)
    sitter_two = await add_client(
        db,
        name="iPhone Sitter Two",
        platform="ios",
        guest=True,
        expires_at="2099-01-01T00:00:00+00:00",
        profile_id=pid,
    )
    ha = FakeHA()
    (err,) = await _ask(db, ha, sitter_two)
    assert err["reason"] == "no_recipients"
    cfg = await _get_client_config(db, sitter_two, ha)
    assert cfg["access"]["request"]["available"] is False
    assert cfg["access"]["request"]["recipients"] == []


async def test_expiry_and_window_start_close_open_requests(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    nxt = await access_requests.tick(db, ha)
    assert nxt == datetime(2026, 11, 20, 14, 30, tzinfo=timezone.utc)
    clock["now"] = FRI_AFTERNOON + timedelta(minutes=10)
    await access.check_transitions(db, now=clock["now"])
    row = await access_requests.request_row(db, rid)
    assert row["status"] == "expired"
    ev = await db.fetchone(
        "SELECT event_kind FROM event_log WHERE kind = 'access_request_expired'"
    )
    assert ev["event_kind"] == "access_request_expired"
    states = [
        m
        for cid, m in pushed["to"]
        if cid == h.sitter_two and m["type"] == "access_request_state"
    ]
    assert states[-1]["request"]["status"] == "expired"
    # An answer after the expiry
    (err,) = await _msg(
        db,
        ha,
        h.papa,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "yes",
            "duration": "1h",
        },
    )
    assert err["reason"] == "already_answered"
    # A new request is cancelled when the window starts by itself.
    clock["now"] = WINDOW_START - timedelta(minutes=2)
    (state,) = await _ask(db, ha, h.sitter_two)
    rid2 = state["request"]["request_id"]
    clock["now"] = WINDOW_START + timedelta(seconds=1)
    await access.check_transitions(db, now=clock["now"])
    assert (await access_requests.request_row(db, rid2))["status"] == "cancelled"


async def test_recipient_answers_away_through_the_relay(db, clock, pushed, connected):
    h = await _house(db)
    ha = FakeHA()
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    # Papa has remote access and is not connected: the request goes through
    # the integration relay.
    relays = [m for m in pushed["integration"] if m["type"] == "family_relay"]
    assert relays and h.papa in relays[-1]["client_ids"]
    assert relays[-1]["message"]["type"] == "access_request_update"
    out = await family_api.integration_family(
        _int_request(db),
        family_api.IntegrationFamily(
            client_id=h.papa,
            message={
                "type": "access_request_answer",
                "request_id": rid,
                "answer": "no",
            },
        ),
    )
    (upd,) = out["messages"]
    assert upd["request"]["answer"]["by"]["via"] == "away"
    # Mama has no remote access: away she is not a recipient.
    out = await family_api.integration_family(
        _int_request(db),
        family_api.IntegrationFamily(
            client_id=h.mama, message={"type": "access_requests_get"}
        ),
    )
    assert out["messages"] == [{"type": "access_requests", "requests": []}]
    out = await family_api.integration_family(
        _int_request(db),
        family_api.IntegrationFamily(
            client_id=h.sitter_two, message={"type": "access_request"}
        ),
    )
    assert out["messages"][0]["reason"] == "home_only"


async def test_ws_dispatch(db, clock, pushed):
    h = await _house(db)
    ha = FakeHA()
    ws = fam._WS(db, ha, h.sitter_two)
    await _handle_client_message(
        ws, db, {"type": "access_request"}, "access_request", h.sitter_two
    )
    assert ws.sent[-1]["type"] == "access_request_state"
    ws = fam._WS(db, ha, None)
    await _handle_client_message(
        ws, db, {"type": "access_requests_get"}, "access_requests_get", "admin"
    )
    assert ws.sent[-1]["reason"] == "not_permitted"


async def test_admin_overview_answer_and_end(db, clock, pushed, monkeypatch):
    monkeypatch.setattr(enforcement, "notify_change", lambda *a, **k: None)
    h = await _house(db)
    ha = FakeHA()
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    req = _request(db, ha)
    out = await access_api.get_access_requests(req)
    assert [r["request_id"] for r in out["requests"]] == [rid]
    assert out["requests"][0]["can_answer"] is True and out["early"] == []
    with pytest.raises(HTTPException) as e:
        await access_api.answer_access_request(
            req, rid, access_api.AccessRequestAnswer(answer="yes")
        )
    assert e.value.status_code == 422
    view = await access_api.answer_access_request(
        req, rid, access_api.AccessRequestAnswer(answer="yes", duration="today")
    )
    assert view["status"] == "approved"
    assert view["answer"]["by"] == {
        "kind": "admin",
        "client_id": None,
        "name": "Admin One",
        "via": "admin_ui",
    }
    assert view["answer"]["until"] == "2026-11-20T23:00:00Z"
    with pytest.raises(HTTPException) as e:
        await access_api.answer_access_request(
            req, rid, access_api.AccessRequestAnswer(answer="no")
        )
    assert e.value.status_code == 409 and e.value.detail["code"] == "already_answered"
    out = await access_api.get_access_requests(req)
    assert out["early"][0]["client_id"] == h.sitter_two
    assert {b["type"] for b in pushed["broadcast"]} >= {"access_requests_changed"}
    # End it early: closed again, logged.
    assert (await access_api.end_early_access(req, h.sitter_two)).ended is True
    assert (await _get_client_config(db, h.sitter_two, ha))["access"]["hours"][
        "open"
    ] is False
    assert await db.fetchone(
        "SELECT 1 FROM event_log WHERE kind = 'access_early_ended'"
    )
    assert (await access_api.end_early_access(req, h.sitter_two)).ended is False
    with pytest.raises(HTTPException) as e:
        await access_api.end_early_access(req, "00000000-0000-4000-8000-000000000000")
    assert e.value.status_code == 404


async def test_ha_notification_buttons_answer(db, clock, pushed, monkeypatch):
    monkeypatch.setattr(enforcement, "notify_change", lambda *a, **k: None)
    h = await _house(db)
    ha = FakeHA()
    async with db.transaction():
        await fstore.put_scope(
            db,
            h.papa,
            notify_targets=["notify.mobile_app_iphone_papa", "notify.telegram"],
            actor="t",
        )
    spawned = []
    monkeypatch.setattr(tasks, "spawn", lambda coro, name="": spawned.append(coro))
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    assert len(spawned) == 1
    await spawned.pop()
    mobile = [s for s in ha.services if s[1] == "mobile_app_iphone_papa"]
    telegram = [s for s in ha.services if s[1] == "telegram"]
    assert len(mobile) == 1 and len(telegram) == 1
    assert "data" not in telegram[0][2]
    payload = mobile[0][2]
    assert payload["title"] == "Früher Zugang?"
    assert (
        "iPhone Sitter Two" not in payload["message"]
        and "Sitter Two" in payload["message"]
    )
    assert "17:00" in payload["message"]
    titles = [a["title"] for a in payload["data"]["actions"]]
    assert titles == ["Ja, 1 Std.", "Ja, bis zum Beginn", "Nein"]
    tokens = await db.fetchall(
        "SELECT * FROM access_answer_tokens WHERE request_id = ?", (rid,)
    )
    assert len(tokens) == 3
    action = payload["data"]["actions"][0]["action"]
    assert action.startswith(access_requests.ACTION_PREFIX)
    token = action[len(access_requests.ACTION_PREFIX) :]
    assert all(token not in json.dumps(t) for t in tokens)
    access_requests.on_ha_event(db, ha)(
        "mobile_app_notification_action", {"action": action}
    )
    assert len(spawned) == 1
    await spawned.pop()
    row = await access_requests.request_row(db, rid)
    assert row["status"] == "approved"
    answer = json.loads(row["answer_json"])
    assert answer["duration"] == "1h" and answer["by"]["via"] == "ha_notification"
    assert answer["by"]["name"] == "Papa"
    assert not await db.fetchall(
        "SELECT 1 FROM access_answer_tokens WHERE request_id = ?", (rid,)
    )
    assert ha.services[-1][2] == {
        "message": "clear_notification",
        "data": {"tag": payload["data"]["tag"]},
    }
    assert await access_requests.answer_by_token(db, ha, token) == "invalid"
    assert await access_requests.answer_by_token(db, ha, "short") == "invalid"


async def test_client_config_and_views_match_the_contract(
    db, clock, pushed, monkeypatch
):
    from .test_profiles_api import _contract_validator

    import os

    if not os.environ.get("CONTRACTS_DIR"):
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    monkeypatch.setattr(enforcement, "notify_change", lambda *a, **k: None)
    validator = _contract_validator()
    if validator is None:
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    h = await _house(db)
    ha = FakeHA()
    for cid in (h.sitter_two, h.papa):
        cfg = await _get_client_config(db, cid, ha)
        assert sorted(validator.iter_errors(cfg), key=str) == []
    (state,) = await _ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    _contract_ok(_contract_validator("ws/server/access_request_state.json"), state)
    (lst,) = await _msg(db, ha, h.papa, {"type": "access_requests_get"})
    _contract_ok(_contract_validator("ws/server/access_requests.json"), lst)
    (upd,) = await _msg(
        db,
        ha,
        h.papa,
        {
            "type": "access_request_answer",
            "request_id": rid,
            "answer": "yes",
            "duration": "3h",
        },
    )
    _contract_ok(_contract_validator("ws/server/access_request_update.json"), upd)
    (err,) = await _msg(
        db,
        ha,
        h.papa,
        {"type": "access_request_answer", "request_id": rid, "answer": "no"},
    )
    _contract_ok(_contract_validator("ws/server/access_request_error.json"), err)
    cfg = await _get_client_config(db, h.sitter_two, ha)
    assert sorted(validator.iter_errors(cfg), key=str) == []
    out = await access_api.get_access_requests(_request(db, ha))
    _contract_ok(_contract_validator("rest/access-requests.response.json"), out)
    view = (await clients_api.client_view(db, h.sitter_two)).model_dump()
    _contract_ok(_contract_validator("rest/paired-client.json"), view)
    entry = (await pstore.resolve_all(db, [h.sitter_two]))[h.sitter_two]
    snap = await enforcement.build_snapshot(
        db,
        ha,
        entry,
        "a" * 32,
        states={},
        stored_qa={},
        tz_name="Europe/Zurich",
        now=clock["now"],
    )
    _contract_ok(_contract_validator("common/enforcement-snapshot.json"), snap)


def _contract_ok(validator, data):
    errors = sorted(validator.iter_errors(data), key=str)
    assert errors == [], [e.message for e in errors[:3]]
