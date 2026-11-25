"""Family manager and early access: hardening of the away route and of what
devices learn about each other.

* The relay (``POST /api/integration/family``) never speaks for a device that
  is connected to the hub; a family manager without remote access is refused
  away; malformed relayed messages get the error answer, not a 500.
* A child (and a device asking for early access) learns kind and name of who
  answered, not that device's id or route.
* An area-limited family manager cannot widen a child's areas by taking an
  adjustment away.
"""

import json

import pytest
from api import family as family_api
from api.ws.client import _get_client_config
from services import access, access_requests, enforcement
from services.family import service
from services.family import store as fstore
from services.profiles import store as pstore

from . import test_access_requests as early
from . import test_family as fam
from .test_family import FakeHA

_clean = fam._clean
_reset_requests = early._reset_requests
pushed = fam.pushed
clock = early.clock
connected = early.connected


async def _relay(db, ha, client_id, message):
    out = await family_api.integration_family(
        fam._request(db, ha, auth="integration"),
        family_api.IntegrationFamily(client_id=client_id, message=message),
    )
    return out["messages"]


async def _override(db, client_id, rule, value):
    async with db.transaction():
        await pstore.put_override(
            db,
            client_id,
            rule,
            value=value,
            user_choice=False,
            expires_at=None,
            note=None,
            actor="admin",
        )


def _selected(*area_ids, own_area=False):
    return {
        "mode": "selected",
        "area_ids": list(area_ids),
        "tags": [],
        "own_area": own_area,
    }


# ── no relay for a device that is at home ─────────────────────────────


async def test_relay_refuses_a_device_connected_to_the_hub(db, pushed, connected):
    """A child that got hold of the manager's id cannot answer its own
    request through the relay while the manager's device is at home."""
    ha = FakeHA()
    manager_one, child_one, _ = await fam._house(
        db, child_overrides={"remote_access": True}
    )
    (state,) = await _relay(
        db, ha, child_one, {"type": "kids_request", "category": "stay_up"}
    )
    rid = state["request"]["request_id"]
    connected.add(manager_one)
    for message in (
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "15m"},
        {
            "type": "family_release",
            "client_ids": [child_one],
            "kind": "pause",
            "preset": "2h",
        },
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "kids_quiet_hours",
            "value": [],
        },
        {"type": "family_get_children"},
        {"type": "family_notify_set", "notify": {"family_change": False}},
    ):
        (answer,) = await _relay(db, ha, manager_one, message)
        assert answer["type"] == "family_error", message["type"]
        assert answer["reason"] == "not_permitted", message["type"]
    assert (await fstore.request_row(db, rid))["status"] == "open"
    assert await fstore.releases_of(db, child_one) == []
    assert not [
        o
        for o in await pstore.overrides_of(db, child_one)
        if o["rule_key"] != "remote_access"
    ]
    # The child's own messages are not relayed while it is at home either.
    connected.add(child_one)
    (answer,) = await _relay(
        db, ha, child_one, {"type": "kids_request_cancel", "request_id": rid}
    )
    assert (answer["type"], answer["reason"]) == ("family_error", "not_permitted")
    assert (await fstore.request_row(db, rid))["status"] == "open"
    # Away (not connected) the relay is the route, as before.
    connected.clear()
    (answer,) = await _relay(
        db,
        ha,
        manager_one,
        {"type": "family_answer", "request_id": rid, "answer": "no"},
    )
    assert answer["request"]["status"] == "declined"


async def test_relay_refuses_early_access_answers_of_a_connected_device(
    db, clock, pushed, connected
):
    h = await early._house(db)
    ha = FakeHA()
    (state,) = await early._ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    connected.add(h.papa)
    (answer,) = await _relay(
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
    assert answer["type"] == "access_request_error"
    assert answer["reason"] == "not_permitted"
    assert (await access_requests.request_row(db, rid))["status"] == "open"


# ── away only with remote access ──────────────────────────────────────


async def test_manager_without_remote_access_is_refused_away(db, pushed, connected):
    ha = FakeHA()
    manager_one, child_one, _ = await fam._house(db)
    await _override(db, manager_one, "remote_access", False)
    cfg = await _get_client_config(db, manager_one, ha)
    assert cfg["family"]["remote"] == "home_only"
    message = {
        "type": "family_release",
        "client_ids": [child_one],
        "kind": "bedtime",
        "preset": "1h",
    }
    (answer,) = await _relay(db, ha, manager_one, message)
    assert (answer["type"], answer["reason"]) == ("family_error", "not_permitted")
    (answer,) = await _relay(db, ha, manager_one, {"type": "family_get_children"})
    assert (answer["type"], answer["reason"]) == ("family_error", "not_permitted")
    assert await fstore.releases_of(db, child_one) == []
    # At home (hub WebSocket) nothing changes.
    (answer,) = await fam._msg(db, ha, manager_one, message)
    assert answer["results"][0]["status"] == "ok"


# ── malformed relayed messages ────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        {"type": "family_get_activity", "client_id": ["x"]},
        {"type": "family_get_activity", "client_id": {"a": 1}},
        {"type": "family_get_activity", "cursor": "²"},
        {"type": "family_get_activity", "cursor": "9" * 40},
    ],
)
async def test_malformed_activity_message_is_invalid_request(db, pushed, message):
    ha = FakeHA()
    manager_one, _, _ = await fam._house(db)
    for answers in (
        await _relay(db, ha, manager_one, message),
        await fam._msg(db, ha, manager_one, message),
    ):
        (answer,) = answers
        assert (answer["type"], answer["reason"]) == ("family_error", "invalid_request")


@pytest.mark.parametrize(
    "value", [[1], "all", 7, {"mode": "selected", "area_ids": 5}, {"area_ids": [[1]]}]
)
async def test_malformed_areas_value_is_value_invalid(db, pushed, value):
    ha = FakeHA()
    kids = await fam._area(db, "Kinderzimmer", ["light.kids"])
    manager_one, child_one, _ = await fam._house(db)
    await _override(db, manager_one, "areas", _selected(kids))
    (answer,) = await _relay(
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
    assert answer["type"] == "family_result"
    assert answer["results"][0]["reason"] == "value_invalid"


async def test_an_error_in_a_relayed_message_is_answered_not_raised(
    db, pushed, monkeypatch
):
    ha = FakeHA()
    manager_one, _, _ = await fam._house(db)

    async def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "handle_manager_message", boom)
    monkeypatch.setattr(access_requests, "handle_message", boom)
    (answer,) = await _relay(db, ha, manager_one, {"type": "family_get_children"})
    assert (answer["type"], answer["reason"]) == ("family_error", "invalid_request")
    (answer,) = await _relay(db, ha, manager_one, {"type": "access_requests_get"})
    assert (answer["type"], answer["reason"]) == (
        "access_request_error",
        "invalid_request",
    )


# ── what the child and the asking device learn ────────────────────────


async def test_child_learns_kind_and_name_of_who_answered_only(db, pushed):
    ha = FakeHA()
    manager_one, child_one, _ = await fam._house(db)
    (state,) = await fam._ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    (answered,) = await fam._msg(
        db,
        ha,
        manager_one,
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "15m"},
        route="away",
    )
    only = {"kind": "family_manager", "name": "Manager One"}
    states = [
        m
        for cid, m in pushed["to"]
        if cid == child_one and m["type"] == "kids_request_state"
    ]
    assert states[-1]["request"]["answer"]["by"] == only
    cfg = await _get_client_config(db, child_one, ha)
    assert [r["by"] for r in cfg["kids"]["releases"]] == [only]
    assert manager_one not in json.dumps([states, cfg["kids"]])
    # Family managers keep the full actor (request and the child's releases).
    by = answered["request"]["answer"]["by"]
    assert (by["client_id"], by["via"]) == (manager_one, "away")
    (children,) = await fam._msg(db, ha, manager_one, {"type": "family_get_children"})
    mia_view = next(c for c in children["children"] if c["client_id"] == child_one)
    assert mia_view["releases"][0]["by"]["client_id"] == manager_one
    # The answer to the child's own next message is reduced as well.
    (state,) = await service.handle_child_message(
        db, ha, child_one, {"type": "kids_request_cancel", "request_id": rid}
    )
    assert state["request"]["answer"]["by"] == only


async def test_asking_device_learns_kind_and_name_of_who_answered_only(
    db, clock, pushed, connected, monkeypatch
):
    monkeypatch.setattr(enforcement, "notify_change", lambda *a, **k: None)
    h = await early._house(db)
    ha = FakeHA()
    connected.update({h.papa, h.sitter_two})
    await access.check_transitions(db)
    (state,) = await early._ask(db, ha, h.sitter_two)
    rid = state["request"]["request_id"]
    (update,) = await early._msg(
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
    only = {"kind": "device", "client_id": None, "name": "Papa"}
    states = [
        m
        for cid, m in pushed["to"]
        if cid == h.sitter_two and m["type"] == "access_request_state"
    ]
    assert states[-1]["request"]["answer"]["by"] == only
    cfg = await _get_client_config(db, h.sitter_two, ha)
    assert cfg["access"]["hours"]["early"]["by"] == only
    assert h.papa not in json.dumps(states)
    # The recipient who answered keeps the full actor.
    by = update["request"]["answer"]["by"]
    assert (by["client_id"], by["via"]) == (h.papa, "home")


# ── areas the child falls back to ─────────────────────────────────────


async def _areas_of(db, client_id):
    return (await pstore.resolve_client(db, client_id))[2]["areas"]["value"]


async def test_area_limited_manager_cannot_widen_areas_by_reset(db, pushed):
    ha = FakeHA()
    await fam._area(db, "Küche", ["light.kitchen"])
    kids = await fam._area(db, "Kinderzimmer", ["light.kids"])
    manager_one, child_one, child_two = await fam._house(db)
    for cid in (manager_one, child_one):
        await _override(db, cid, "areas", _selected(kids))
    reset = {
        "type": "family_rule_reset",
        "client_ids": [child_one, child_two],
        "rule": "areas",
    }
    (answer,) = await fam._msg(db, ha, manager_one, reset)
    by_id = {r["client_id"]: r for r in answer["results"]}
    # Child One would fall back to the profile value (all areas): refused.
    assert by_id[child_one]["reason"] == "areas_not_allowed"
    assert await _areas_of(db, child_one) == _selected(kids)
    # Child Two has no adjustment: nothing changes, nothing to refuse.
    assert by_id[child_two]["status"] == "ok"
    # A timed adjustment that replaces the admin's ends the same way: refused.
    (answer,) = await fam._msg(
        db,
        ha,
        manager_one,
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "areas",
            "value": _selected(kids),
            "expires": "1h",
        },
    )
    assert answer["results"][0]["reason"] == "areas_not_allowed"
    assert await _areas_of(db, child_one) == _selected(kids)
    # A manager who sees every area may reset.
    async with db.transaction():
        await pstore.delete_override(db, manager_one, "areas")
    (answer,) = await fam._msg(db, ha, manager_one, reset)
    assert {r["status"] for r in answer["results"]} == {"ok"}
    assert (await _areas_of(db, child_one))["mode"] == "all"


async def test_relay_refusal_says_why_and_echoes_the_request_id(db, pushed, connected):
    """The refusal for a connected device names the cause (not "not a family
    manager") and carries the request_id of the message."""
    ha = FakeHA()
    manager_one, _mia, _ = await fam._house(db)
    connected.add(manager_one)
    (answer,) = await _relay(
        db, ha, manager_one, {"type": "family_get_children", "request_id": "r-1"}
    )
    assert answer["type"] == "family_error" and answer["reason"] == "not_permitted"
    assert answer["message"] == "This device is connected to the hub at home."
    assert answer["request_id"] == "r-1"
