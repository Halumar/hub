"""Access hours, access switch, house sitter and babysitter.

Contract: ``docs/access.md``, vectors ``fixtures/profiles/access-hours`` and
``fixtures/profiles/family-check/babysitter.json``.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from api import authz
from api import integration as integration_api
from api import profiles as profiles_api
from api.pairing import clients as clients_api
from api.ws.client import _get_areas, _get_client_config
from fastapi import HTTPException
from models.database import SCHEMA_VERSION, Database
from services import access, enforcement, event_log, hub_control, kids_mode
from services.family import store as fstore
from services.profiles import store as pstore

from .profile_helpers import add_client
from . import test_family as fam
from .test_family import FakeHA, _area, _ask, _house, _msg, _request, _WS

# Fixtures of the family tests reused here.
_clean = fam._clean
calls = fam.calls
pushed = fam.pushed

VECTORS = Path(__file__).parent / "fixtures" / "profiles" / "access-hours"
EVERY = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Friday 2026-11-20 19:00 Europe/Zurich (18:00 UTC)
FRI_EVENING = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
FRI_NOON = datetime(2026, 11, 20, 11, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": FRI_EVENING}
    monkeypatch.setattr(access, "utcnow", lambda: state["now"])
    monkeypatch.setattr(kids_mode, "_tz_cache", (time.monotonic(), "Europe/Zurich"))
    return state


async def _sitter_profile(db, suggestion="babysitter"):
    view = await profiles_api.add_profile_suggestion(_request(db), suggestion)
    return view["id"]


async def _with_key(db, profile_id):
    """The profile on Checked: its devices hold a read-only key (the
    suggestions of the guest kinds are on Hub only)."""
    async with db.transaction():
        await db.execute(
            "UPDATE profiles SET body_json = json_set(body_json, "
            "'$.rules.control_path', 'checked') WHERE id = ?",
            (profile_id,),
        )
    return profile_id


async def _sitter(db, profile_id, name="iPhone Sitter One", **kw):
    return await add_client(
        db,
        name=name,
        owner_label="Sitter One",
        platform="ios",
        guest=True,
        expires_at="2099-01-01T00:00:00+00:00",
        profile_id=profile_id,
        **kw,
    )


# ── reference vectors ────────────────────────────────────────────────────────


def test_access_vectors_match_the_contract():
    files = sorted(
        p for p in VECTORS.glob("*.json") if not p.name.startswith("invalid")
    )
    assert files
    n = 0
    for path in files:
        vector = json.loads(path.read_text())
        for case in vector["cases"]:
            got = access.compute(
                schedule=bool(vector["access_hours"]["enabled"]),
                windows=vector["access_hours"]["windows"]
                if vector["access_hours"]["enabled"]
                else [],
                switch=case["switch"],
                now=datetime.fromisoformat(case["now"].replace("Z", "+00:00")),
                tz_name=vector["time_zone"],
                early=case.get("early"),
            )
            assert got == case["expected"], f"{path.name} {case['now']}"
            n += 1
    assert n >= 10


def test_profile_kinds_match_the_schema():
    from services import db_migrations
    from services.profiles import catalog as cat

    assert db_migrations.PROFILE_KINDS == cat.PROFILE_KINDS
    assert [k["id"] for k in cat.profile_kinds()] == list(cat.PROFILE_KINDS)
    assert "babysitter" in cat.GUEST_ACCESS_KINDS


def test_area_patterns_match_word_starts():
    pats = ["wohn", "kinder", "küche", "kitchen"]
    assert pstore.area_matches("Wohnzimmer", pats)
    assert pstore.area_matches("Kinderzimmer Child One", pats)
    assert pstore.area_matches("KÜCHE", pats)
    assert pstore.area_matches("Open kitchen", pats)
    assert not pstore.area_matches("Elternschlafzimmer", pats)
    assert not pstore.area_matches("Garage", pats)


# ── suggestions ─────────────────────────────────────────────────────────────


async def test_babysitter_suggestion_preselects_living_and_kids_rooms(db):
    living = await _area(db, "Wohnzimmer", ["light.living"])
    kids = await _area(db, "Kinderzimmer Child Three", ["light.kids"])
    await _area(db, "Garage", ["cover.garage"])
    pid = await _sitter_profile(db)
    row = await pstore.profile_row(db, pid)
    assert row["kind"] == "babysitter"
    body = pstore.body_of(row)
    assert set(body["rules"]["areas"]["area_ids"]) == {living, kids}
    assert body["rules"]["remote_access"] is False
    assert {"remote_access", "control_path"} <= set(body["fixed"])
    assert body["rules"]["access_hours"]["enabled"] is True
    assert body["rules"]["babysitter_requests"] is True

    hs = await _sitter_profile(db, "house_sitter")
    hrow = await pstore.profile_row(db, hs)
    hbody = pstore.body_of(hrow)
    assert hrow["kind"] == "house_sitter"
    assert hbody["rules"]["areas"]["mode"] == "selected"
    assert living in hbody["rules"]["areas"]["area_ids"]
    # House sitters get no remote access by default (no key on Hub only).
    assert hbody["rules"]["remote_access"] is False
    assert hbody["rules"]["control_path"] == "hub"
    assert "control_path" in hbody["fixed"]
    assert hbody["rules"]["remote_control"]["domains"]["cover:garage"] == "view"


# ── access state, client_config, read scope ────────────────────────────────


async def test_closed_access_shows_nothing_and_withholds_the_key(db, clock):
    from services import crypto

    ha = FakeHA()
    area = await _area(db, "Wohnzimmer", ["light.living"])
    pid = await _with_key(db, await _sitter_profile(db, "house_sitter"))
    token = await crypto.encrypt_token(db, "hs-token")
    hs = await _sitter(
        db,
        pid,
        name="iPhone Haushüter",
        ha_token_encrypted=token,
        ha_token_status="active",
    )
    # 07:00-21:00: Friday 19:00 local = open until 21:00
    cfg = await _get_client_config(db, hs, ha)
    hours = cfg["access"]["hours"]
    assert hours["open"] is True and hours["schedule"] is True
    assert hours["switch"] is True and hours["until"] == "2026-11-20T20:00:00Z"
    assert cfg["ha_token"] == "hs-token"
    assert [
        a["id"] for a in await _get_areas(db, await authz.client_area_scope(db, hs))
    ] == [area]

    clock["now"] = FRI_NOON.replace(hour=4)  # 05:00 local: closed until 07:00
    cfg = await _get_client_config(db, hs, ha)
    hours = cfg["access"]["hours"]
    assert hours == {
        "open": False,
        "reason": "schedule",
        "until": None,
        "opens_at": "2026-11-20T06:00:00Z",
        "schedule": True,
        "switch": True,
    }
    assert cfg["ha_token"] is None and cfg["ha_url"] is None
    assert cfg["ha_enforcement"] is None
    assert await authz.listed_area_ids(db, hs) == set()
    assert await authz.can_read_area(db, hs, area) is False
    # The configured scope stays for the rule snapshot.
    assert await authz.listed_area_ids(db, hs, ignore_access=True) == {area}


async def test_adult_without_hours_has_no_switch(db, clock):
    ha = FakeHA()
    phone = await add_client(db, name="iPhone", platform="ios", profile="adult")
    cfg = await _get_client_config(db, phone, ha)
    assert cfg["access"]["hours"] == {
        "open": True,
        "reason": None,
        "until": None,
        "opens_at": None,
        "schedule": False,
        "switch": None,
    }
    with pytest.raises(HTTPException) as e:
        await clients_api.set_access_switch(
            _request(db), phone, clients_api.AccessSwitchUpdate(on=False)
        )
    assert e.value.status_code == 409 and e.value.detail == "no_access_switch"
    # A plain guest has a switch (kind guest).
    guest = await add_client(db, name="Gast", guest=True)
    view = await clients_api.client_view(db, guest)
    assert view.access_now.switch is True and view.access_now.open is True


async def test_switch_closes_logs_and_reopens(db, clock, pushed):
    pid = await _sitter_profile(db)
    sitter_one = await _sitter(db, pid)
    await access.check_transitions(db)  # first evaluation: open, not logged
    view = await clients_api.set_access_switch(
        _request(db), sitter_one, clients_api.AccessSwitchUpdate(on=False)
    )
    assert view.access_now.open is False and view.access_now.reason == "switch"
    assert view.access_now.opens_at is None
    rows = await db.fetchall(
        "SELECT kind, detail_json FROM event_log WHERE client_id = ? "
        "AND kind IN ('access_opened', 'access_closed') ORDER BY id",
        (sitter_one,),
    )
    assert [r["kind"] for r in rows] == ["access_closed"]
    assert json.loads(rows[0]["detail_json"])["by"] == "admin"
    audit = await db.fetchall(
        "SELECT action, after_json FROM profile_audit WHERE client_id = ?",
        (sitter_one,),
    )
    assert [a["action"] for a in audit if a["action"] == "access_switch_set"]
    assert (sitter_one, {"type": "client_config_updated", "data": {}}) in pushed["to"]

    # Home Assistant turns it on again (integration endpoint)
    out = await integration_api.integration_access_switch(
        _int_request(db),
        integration_api.AccessSwitchRequest(client_id=sitter_one, on=True, user=None),
    )
    assert out["access"]["open"] is True
    rows = await db.fetchall(
        "SELECT kind, detail_json FROM event_log WHERE client_id = ? "
        "AND kind IN ('access_opened', 'access_closed') ORDER BY id",
        (sitter_one,),
    )
    assert [r["kind"] for r in rows] == ["access_closed", "access_opened"]
    assert json.loads(rows[1]["detail_json"])["by"] == "ha"
    entry = await event_log.view(
        db, await db.fetchone("SELECT * FROM event_log WHERE kind = 'access_opened'")
    )
    data = event_log.ha_event_data(entry)
    assert data["kind"] == "access_opened" and data["access_reason"] == "switch"

    with pytest.raises(HTTPException) as e:
        await integration_api.integration_access_switch(
            _int_request(db),
            integration_api.AccessSwitchRequest(
                client_id="00000000-0000-4000-8000-000000000000", on=True
            ),
        )
    assert e.value.status_code == 404


async def test_closing_access_withdraws_the_key_the_app_held(db, clock, pushed):
    """The token rotates when access closes (a modified app keeps
    no working key outside the window); parental controls and devices without
    an active token are left alone."""
    from services import crypto, device_provisioning

    pid = await _with_key(db, await _sitter_profile(db))
    token = await crypto.encrypt_token(db, "sitter-token")
    sitter_one = await _sitter(
        db, pid, ha_token_encrypted=token, ha_token_status="active"
    )
    pending = await _sitter(
        db, pid, name="iPhone Child Three", ha_token_status="pending"
    )
    await access.check_transitions(db)  # open, nothing happens
    assert not await device_provisioning.rotation_pending(db, sitter_one)

    clock["now"] = FRI_NOON  # closed until 17:00
    await access.check_transitions(db, now=FRI_NOON)
    assert await device_provisioning.rotation_pending(db, sitter_one)
    assert not await device_provisioning.rotation_pending(db, pending)
    # While closed the app gets no token; when access opens the hub hands out
    # the token it holds then (the rotated one).
    assert (await _get_client_config(db, sitter_one, FakeHA()))["ha_token"] is None
    clock["now"] = FRI_EVENING
    await access.check_transitions(db, now=FRI_EVENING)
    assert (await _get_client_config(db, sitter_one, FakeHA()))["ha_token"] == (
        "sitter-token"
    )


def _int_request(db):
    import types

    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration", client_id=None),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        headers={},
        client=types.SimpleNamespace(host="127.0.0.1"),
    )


async def test_schedule_transitions_are_logged_once(db, clock, pushed):
    pid = await _sitter_profile(db)
    sitter_one = await _sitter(db, pid)
    clock["now"] = FRI_NOON  # 12:00 local, babysitting from 17:00
    nxt = await access.check_transitions(db, now=FRI_NOON)
    assert nxt == datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
    assert await access.check_transitions(db, now=FRI_NOON) is not None
    clock["now"] = FRI_EVENING
    await access.check_transitions(db, now=FRI_EVENING)
    kinds = [
        r["kind"]
        for r in await db.fetchall(
            "SELECT kind FROM event_log WHERE client_id = ? ORDER BY id", (sitter_one,)
        )
    ]
    assert kinds == ["access_closed", "access_opened"]
    row = await db.fetchone(
        "SELECT until, detail_json FROM event_log WHERE kind = 'access_opened'"
    )
    assert row["until"] == "2026-11-21T00:00:00Z"
    assert json.loads(row["detail_json"])["reason"] == "schedule"


async def test_snapshot_carries_access_and_keeps_entities(db, clock):
    ha = FakeHA()
    await _area(db, "Wohnzimmer", ["light.living"])
    pid = await _sitter_profile(db)
    sitter_one = await _sitter(db, pid)
    clock["now"] = FRI_NOON  # closed by schedule
    entry = (await pstore.resolve_all(db, [sitter_one]))[sitter_one]
    snap = await enforcement.build_snapshot(
        db,
        ha,
        entry,
        "a" * 32,
        states={},
        stored_qa={},
        tz_name="Europe/Zurich",
        now=FRI_NOON,
    )
    assert snap["access"] == {
        "switch": True,
        "hours": True,
        "windows": [{"days": EVERY, "start": "17:00", "end": "01:00"}],
        "time_zone": "Europe/Zurich",
        "early_until": None,
    }
    assert "light.living" in snap["entities"]  # the integration decides access
    adult = await add_client(db, name="iPhone", platform="ios", profile="adult")
    entry = (await pstore.resolve_all(db, [adult]))[adult]
    snap = await enforcement.build_snapshot(
        db, ha, entry, "b" * 32, states={}, stored_qa={}, tz_name=None, now=FRI_NOON
    )
    assert snap["access"] is None


async def test_kids_control_refused_while_closed(db, clock, calls):
    ha = FakeHA({"light.kids": {"state": "off", "attributes": {}}})
    await _area(db, "Kinderzimmer", ["light.kids"])
    _alex, child_one, _tim = await _house(
        db,
        child_overrides={
            "access_hours": {
                "enabled": True,
                "windows": [{"days": ["sat", "sun"], "start": "08:00", "end": "20:00"}],
            }
        },
    )
    out = await hub_control.control(
        db,
        ha,
        child_one,
        {
            "type": "control",
            "entity_id": "light.kids",
            "service": "turn_on",
            "data": {},
        },
    )
    assert out["type"] == "rule_blocked" and out["reason"] == "access_closed"
    assert out["until"] == "2026-11-21T07:00:00+00:00"
    assert not calls


# ── babysitter answers requests ─────────────────────────────────────────────


async def test_babysitter_gets_and_answers_requests_within_limits(db, clock, pushed):
    ha = FakeHA()
    manager_one, child_one, _tim = await _house(db)
    pid = await _sitter_profile(db)
    sitter_one = await _sitter(db, pid)
    _WS(db, ha, sitter_one)
    _WS(db, ha, manager_one)
    cfg = await _get_client_config(db, sitter_one, ha)
    assert cfg["babysitter"] == {
        "active": True,
        "presets": ["15m", "30m", "access_end"],
        "access_until": "2026-11-21T00:00:00Z",
    }
    assert "family" not in cfg

    (state,) = await _ask(db, ha, child_one)
    rid = state["request"]["request_id"]
    to_lara = [
        m
        for cid, m in pushed["to"]
        if cid == sitter_one and m["type"] == "family_request"
    ]
    assert to_lara and to_lara[-1]["request"]["answer_presets"] == [
        "15m",
        "30m",
        "access_end",
    ]
    assert to_lara[-1]["request"]["can_approve"] is True

    # nothing but answering, and never 1 hour
    (err,) = await _msg(db, ha, sitter_one, {"type": "family_get_children"})
    assert err["reason"] == "not_permitted"
    (err,) = await _msg(
        db,
        ha,
        sitter_one,
        {
            "type": "family_rule_set",
            "client_ids": [child_one],
            "rule": "kids_volume_max",
            "value": 80,
        },
    )
    assert err["reason"] == "not_permitted"
    (err,) = await _msg(
        db,
        ha,
        sitter_one,
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "1h"},
    )
    assert err["reason"] == "preset_not_allowed"
    (err,) = await _msg(
        db,
        ha,
        sitter_one,
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "30m"},
        route="away",
    )
    assert err["reason"] == "not_permitted"
    (reqs,) = await _msg(db, ha, sitter_one, {"type": "family_get_requests"})
    assert [r["request_id"] for r in reqs["requests"]] == [rid]

    (out,) = await _msg(
        db,
        ha,
        sitter_one,
        {
            "type": "family_answer",
            "request_id": rid,
            "answer": "yes",
            "preset": "access_end",
        },
    )
    assert out["request"]["status"] == "approved"
    by = out["request"]["answer"]["by"]
    assert by["kind"] == "babysitter" and by["client_id"] == sitter_one
    (rel,) = await fstore.releases_of(db, child_one, now=FRI_EVENING)
    assert rel["kind"] == "bedtime"
    assert fstore.parse(rel["until"]) <= datetime(
        2026, 11, 21, 0, 0, tzinfo=timezone.utc
    )
    # the parents saw the answer with its author
    to_alex = [
        m
        for cid, m in pushed["to"]
        if cid == manager_one and m["type"] == "family_request"
    ]
    assert to_alex[-1]["request"]["answer"]["by"]["kind"] == "babysitter"

    # access closes: the babysitter's release ends, no more requests
    await access.check_transitions(db, now=FRI_EVENING)
    await clients_api.set_access_switch(
        _request(db), sitter_one, clients_api.AccessSwitchUpdate(on=False)
    )
    assert await fstore.releases_of(db, child_one, now=FRI_EVENING) == []
    (err,) = await _msg(db, ha, sitter_one, {"type": "family_get_requests"})
    assert err["reason"] == "not_permitted"
    cfg = await _get_client_config(db, sitter_one, ha)
    assert cfg["babysitter"]["active"] is False


async def test_child_names_the_active_babysitter_and_hears_of_changes(
    db, clock, pushed
):
    """The child sees who gets its question: the recipients
    include a babysitter only while it gets the requests, and follow live."""
    from api import ws as api_ws

    ha = FakeHA()
    _alex, child_one, _tim = await _house(db)
    sitter_one = await _sitter(db, await _sitter_profile(db))
    _WS(db, ha, child_one)
    updated = {"type": "client_config_updated", "data": {}}

    async def recipients():
        cfg = await _get_client_config(db, child_one, ha)
        return cfg["kids"]["requests"]["recipients"]

    def told_mia() -> bool:
        told = (child_one, updated) in pushed["to"]
        pushed["to"].clear()
        return told

    parents = [{"name": "Manager One", "role": "family_manager"}]
    with_lara = parents + [{"name": "Sitter One", "role": "babysitter"}]
    # not at home (no hub connection): not named
    assert await recipients() == parents
    await access.check_transitions(db, now=FRI_EVENING)
    told_mia()

    sitter_ws = _WS(db, ha, sitter_one)
    await access.check_transitions(db, now=FRI_EVENING)
    assert told_mia()
    assert await recipients() == with_lara
    await access.check_transitions(db, now=FRI_EVENING)
    assert not told_mia()

    # access switch off and on
    await clients_api.set_access_switch(
        _request(db), sitter_one, clients_api.AccessSwitchUpdate(on=False)
    )
    assert told_mia() and await recipients() == parents
    await clients_api.set_access_switch(
        _request(db), sitter_one, clients_api.AccessSwitchUpdate(on=True)
    )
    assert told_mia() and await recipients() == with_lara

    # leaves home
    del api_ws._clients[sitter_ws]
    await access.check_transitions(db, now=FRI_EVENING)
    assert told_mia() and await recipients() == parents

    # back home, then the access window ends (01:00 Zurich)
    _WS(db, ha, sitter_one)
    await access.check_transitions(db, now=FRI_EVENING)
    assert told_mia()
    clock["now"] = datetime(2026, 11, 21, 0, 30, tzinfo=timezone.utc)
    await access.check_transitions(db, now=clock["now"])
    assert told_mia() and await recipients() == parents


def test_recipients_view_orders_roles_and_names():
    from services.family.service import Manager, recipients_view

    def m(name, owner=None, sitter=False):
        return Manager(
            client_id=name,
            name=name,
            owner_label=owner,
            platform="ios",
            scope={"all": True, "client_ids": []},
            visible_area_ids=None,
            notify_targets=[],
            notify={},
            remote=False,
            babysitter=sitter,
        )

    out = recipients_view(
        [m("iPhone", "papa"), m("iPad", "Mama"), m("iPhone 2", "papa")],
        [m("Manager One", "Manager One", True)],
    )
    assert out == [
        {"name": "Mama", "role": "family_manager"},
        {"name": "papa", "role": "family_manager"},
        {"name": "Manager One", "role": "babysitter"},
    ]
    many = recipients_view([m(f"M{i:02}") for i in range(12)], [m("S", sitter=True)])
    assert len(many) == 10 and all(r["role"] == "family_manager" for r in many)


async def test_babysitter_never_unlocks_a_blocked_device(db, clock, pushed):
    ha = FakeHA({"lock.front_door": {"state": "locked", "attributes": {}}})
    await _area(db, "Eingang", ["lock.front_door", "light.hall"])
    _alex, child_one, _tim = await _house(db)
    pid = await _sitter_profile(db)
    sitter_one = await _sitter(db, pid)
    _WS(db, ha, sitter_one)
    (state,) = await _ask(
        db, ha, child_one, category="unlock_device", entity_id="lock.front_door"
    )
    if state["type"] == "family_error":
        pytest.skip("entity not visible for the child in this setup")
    rid = state["request"]["request_id"]
    (err,) = await _msg(
        db,
        ha,
        sitter_one,
        {"type": "family_answer", "request_id": rid, "answer": "yes", "preset": "15m"},
    )
    assert err["reason"] == "release_not_allowed"


async def test_babysitter_rule_only_for_kind_babysitter(db, clock):
    ha = FakeHA()
    phone = await add_client(
        db,
        name="iPhone",
        platform="ios",
        profile="adult",
        overrides={"babysitter_requests": True},
    )
    resolved = await pstore.resolve_client(db, phone)
    assert resolved[2]["babysitter_requests"]["reason"] == "requires_profile_kind"
    cfg = await _get_client_config(db, phone, ha)
    assert "babysitter" not in cfg


# ── migration ───────────────────────────────────────────────────────────────


async def test_v14_adds_switch_and_kind_babysitter(tmp_path):
    db_path = str(tmp_path / "hub.db")
    db = Database(db_path)
    await db.initialize()
    guest = await db.get_setting("default_profile_guest")
    await db.close()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    create = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'profiles'"
    ).fetchone()[0]
    old = create.replace("'babysitter', ", "").replace(
        "CREATE TABLE profiles", "CREATE TABLE profiles_old"
    )
    conn.execute(old)
    conn.execute("INSERT INTO profiles_old SELECT * FROM profiles")
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
    ).fetchall():
        conn.execute(f"DROP TRIGGER {name}")
    conn.execute("DROP TABLE profiles")
    conn.execute("ALTER TABLE profiles_old RENAME TO profiles")
    conn.execute("ALTER TABLE paired_clients DROP COLUMN access_switch")
    conn.execute("ALTER TABLE paired_clients DROP COLUMN access_state")
    conn.execute(
        "INSERT INTO profiles (id, name, kind, child_protection, body_json, "
        "origin_suggestion, origin_version, rev, created_at, updated_at) VALUES "
        "('bs', 'Babysitter', 'guest', 0, '{}', 'babysitter', 1, 1, 'x', 'x')"
    )
    conn.execute(
        "INSERT INTO paired_clients (id, name, secret_hash, created_at, access, "
        "expires_at, profile_id) VALUES ('g1', 'Gast', 'h', 'x', 'guest', "
        "'2099-01-01T00:00:00+00:00', ?)",
        (guest,),
    )
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()

    db = Database(db_path)
    await db.initialize()
    try:
        assert list((await db.fetchone("PRAGMA user_version")).values()) == [
            SCHEMA_VERSION
        ]
        row = await db.fetchone("SELECT kind FROM profiles WHERE id = 'bs'")
        assert row["kind"] == "babysitter"
        c = await db.fetchone("SELECT access_switch, access_state FROM paired_clients")
        assert c["access_switch"] == 1 and c["access_state"] is None
        triggers = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
        assert len(triggers) >= 3
        assert await db.fetchall("PRAGMA foreign_key_check") == []
    finally:
        await db.close()


async def test_client_configs_match_the_contract(db, clock):
    import os

    from .test_profiles_api import _contract_validator

    if not os.environ.get("CONTRACTS_DIR"):
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    validator = _contract_validator()
    if validator is None:
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    ha = FakeHA()
    _alex, child_one, _tim = await _house(db)
    pid = await _sitter_profile(db)
    sitter_one = await _sitter(db, pid)
    _WS(db, ha, sitter_one)
    hs = await _sitter(db, await _sitter_profile(db, "house_sitter"), name="Haushüter")
    cfg = await _get_client_config(db, child_one, ha)
    assert cfg["kids"]["requests"]["recipients"][-1]["role"] == "babysitter"
    errors = sorted(validator.iter_errors(cfg), key=str)
    assert errors == [], [e.message for e in errors[:3]]
    for now in (FRI_EVENING, FRI_NOON):
        clock["now"] = now
        for cid in (sitter_one, hs):
            cfg = await _get_client_config(db, cid, ha)
            errors = sorted(validator.iter_errors(cfg), key=str)
            assert errors == [], (cid, now, [e.message for e in errors[:3]])
    paired = _contract_validator("rest/paired-client.json")
    view = (await clients_api.client_view(db, sitter_one)).model_dump()
    assert sorted(paired.iter_errors(view), key=str) == []


# ── unknown time zone ───────────────────────────────────────────────


async def test_unknown_time_zone_closes_access_hours_and_the_last_zone_is_kept(
    db, monkeypatch
):
    """No zone ever read: a device with access hours is closed instead of
    counting its windows in UTC. Once HA reported a zone, a later failure
    keeps that zone (also after a restart, from the settings)."""
    kids_mode.reset()
    windows = [{"days": EVERY, "start": "08:00", "end": "20:00"}]
    cid = await add_client(
        db, overrides={"access_hours": {"enabled": True, "windows": windows}}
    )
    # 19:30 UTC = 20:30 in Zurich: closed there, open in UTC.
    monkeypatch.setattr(
        access, "utcnow", lambda: datetime(2026, 11, 23, 19, 30, tzinfo=timezone.utc)
    )

    class Down:
        async def get_config(self):
            raise RuntimeError("502 from the supervisor proxy")

    class Up:
        async def get_config(self):
            return {"time_zone": "Europe/Zurich"}

    assert await kids_mode.hub_time_zone(Down()) is None
    state = await access.client_state(db, cid)
    assert state["open"] is False and state["reason"] == "schedule"
    # A device without access hours is not affected.
    other = await add_client(db)
    assert (await access.client_state(db, other))["open"] is True

    assert await kids_mode.hub_time_zone(Up()) == "Europe/Zurich"
    kids_mode._tz_cache = None  # cache ran out, HA down again
    assert await kids_mode.hub_time_zone(Down()) == "Europe/Zurich"
    assert access.time_zone() == "Europe/Zurich"
    assert (await access.client_state(db, cid))["open"] is False

    # Restart: the planner stores the zone and takes it back first.
    stop = RuntimeError("stop")

    async def _once(*_a, **_k):
        raise stop

    import asyncio as _asyncio
    import types as _types

    app = _types.SimpleNamespace(state=_types.SimpleNamespace(db=db, ha_client=Up()))
    kids_mode.reset()
    monkeypatch.setattr(_asyncio, "wait_for", _once)
    with pytest.raises(RuntimeError):
        await access.access_loop(app)
    assert await db.get_setting(kids_mode.TZ_SETTING) == "Europe/Zurich"
    kids_mode.reset()
    app.state.ha_client = Down()
    with pytest.raises(RuntimeError):
        await access.access_loop(app)
    assert access.time_zone() == "Europe/Zurich"
    access.reset_state()
    kids_mode.reset()


async def test_a_wake_during_the_check_is_not_lost(db, monkeypatch):
    """wake() while check_transitions runs makes the loop check again
    right away instead of sleeping until the next edge."""
    import asyncio as _asyncio
    import types as _types

    kids_mode.reset()
    runs = []

    async def _check(_db, **_k):
        runs.append(1)
        if len(runs) == 1:
            access.wake()  # e.g. a profile change while the check runs
        return None

    monkeypatch.setattr(access, "check_transitions", _check)
    monkeypatch.setattr(access, "MAX_SLEEP_SECONDS", 30.0)
    app = _types.SimpleNamespace(state=_types.SimpleNamespace(db=db, ha_client=None))
    task = _asyncio.get_running_loop().create_task(access.access_loop(app))
    try:
        for _ in range(50):
            await _asyncio.sleep(0.01)
            if len(runs) >= 2:
                break
        assert len(runs) >= 2
    finally:
        task.cancel()
        with pytest.raises(_asyncio.CancelledError):
            await task
        access.reset_state()
