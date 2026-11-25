"""Persistent log tab: sources, alarms, filters, retention, notifications.

Covers services/event_log.py, services/event_notify.py and api/log.py:
entries from the profile audit, pairing/removal, the hub control path of
child devices, integration events (contract integration/events.request.json,
idempotent), HA call_service of devices that switch directly; alarms from the
rejection threshold; hidden persons (activity only); filters and paging;
nightly retention; read marks; notification rules with threshold, quiet
hours, bundling, targets (HA notify.* and member devices only) and the test
send; the hub's own outage detection; expiring guests.
"""

import json
import types
import uuid
from datetime import datetime, timedelta, timezone

import api.websocket as ws
import pytest
from api import integration as integration_api
from api import log as api
from fastapi import HTTPException
from pydantic import ValidationError
from models.schemas import IntegrationEvent, IntegrationEventsRequest
from services import crypto, event_log, event_notify, integration_events, kids_mode
from services import device_provisioning as dp
from services.profiles import changes

from .profile_helpers import add_client, starter_profile

UTC = timezone.utc


class FakeHA:
    def __init__(self, states=None, services=None):
        self.calls = []
        self.fail = set()
        self.ws = types.SimpleNamespace(get_state=lambda e: (states or {}).get(e))
        self._services = services or []

    async def call_service(self, domain, service, data=None, target=None):
        if service in self.fail:
            raise RuntimeError("boom")
        self.calls.append((domain, service, data))
        return []

    async def get_services(self):
        return self._services

    async def get_config(self):
        return {"time_zone": "Europe/Zurich"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    kids_mode.reset()
    integration_events.reset()
    event_log.reset()
    event_log.reset_outage()
    event_notify.reset()
    yield
    event_log.reset()
    event_notify.reset()
    integration_events.reset()
    crypto.reset_cache()


@pytest.fixture
def sent(monkeypatch):
    log = {"to": [], "broadcast": [], "integration": []}

    async def _send(client_id, message):
        log["to"].append((client_id, message))
        if client_id == "integration":
            log["integration"].append(message)
        return True

    async def _broadcast(message, *a, **k):
        log["broadcast"].append(message)

    monkeypatch.setattr(ws, "send_to_client", _send)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return log


@pytest.fixture
async def hub(db, sent, monkeypatch):
    ha = FakeHA(
        states={"light.kitchen": {"attributes": {"friendly_name": "Küchenlicht"}}},
        services=[
            {
                "domain": "notify",
                "services": {
                    "persistent_notification": {"name": "Persistent"},
                    "mobile_app_x": {},
                    "send_message": {},
                },
            },
            {"domain": "light", "services": {"turn_on": {}}},
        ],
    )
    event_log.register(db, ha)
    monkeypatch.setattr(kids_mode, "_tz_cache", None)
    return types.SimpleNamespace(db=db, ha=ha, sent=sent)


def _req(db, ha=None, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth, actor="Admin One"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
        client=None,
    )


def _ev(type_, client_id=None, at=None, **kw):
    return {
        "id": str(uuid.uuid4()),
        "type": type_,
        "at": at or datetime.now(UTC).isoformat(timespec="seconds"),
        "client_id": client_id,
        **kw,
    }


async def _post_events(db, events, dropped=0):
    body = IntegrationEventsRequest(events=events, dropped=dropped)
    return await integration_api.integration_events_intake(
        _req(db, auth="integration"), body
    )


async def _list(db, **kw):
    params = dict(
        category=None,
        range=None,
        client_id=None,
        owner=None,
        profile_id=None,
        area_id=None,
        kind=None,
        q=None,
        unread=False,
        important=False,
        before_id=None,
        limit=100,
    )
    params.update(kw)
    return await api.get_log(_req(db), **params)


def _rule(kinds, notify=("notify.persistent_notification",), devices=(), **kw):
    rule = {
        "id": str(uuid.uuid4()),
        "name": "Test",
        "enabled": True,
        "kinds": list(kinds),
        "targets": {"notify": list(notify), "devices": list(devices)},
        "filters": {k: [] for k in event_notify.empty_filters()},
        "threshold": {"count": 1, "window_minutes": 1},
        "bundle_minutes": 0,
        "quiet_hours": [],
    }
    for k, v in kw.items():
        if k in rule["filters"]:
            rule["filters"][k] = v
        else:
            rule[k] = v
    return rule


async def _save_rules(db, rules, retention=30):
    body = api.EventNotifications(retention_days=retention, rules=rules)
    return await api.put_event_notifications(_req(db), body)


# ── sources ─────────────────────────────────────────────────────────────────


async def test_audit_change_is_mirrored_with_taxonomy_kind(hub):
    db = hub.db
    cid = await add_client(
        db, name="iPad Child One", owner_label="Child One", profile="child"
    )
    pid = await starter_profile(db, "child")
    await changes.audit(
        db,
        "profile_assigned",
        actor="Admin One",
        profile_id=pid,
        client_id=cid,
        before={"profile_id": "x"},
        after={"profile_id": pid},
    )
    result = await _list(db, category="change")
    entry = result["entries"][0]
    assert entry["kind"] == "profile_assigned"
    assert entry["event_kind"] == "profile_changed"
    assert entry["actor"] == "Admin One"
    assert (
        entry["device_name"] == "iPad Child One" and entry["owner_label"] == "Child One"
    )
    assert entry["profile_id"] == pid and entry["profile_exists"]
    assert entry["detail"]["after"] == {"profile_id": pid}
    # hub kinds are handed to the integration (HA event <ns>_event)
    pushed = [m for m in hub.sent["integration"] if m["type"] == "ha_event"]
    assert pushed and pushed[-1]["data"]["kind"] == "profile_changed"
    assert pushed[-1]["data"]["device_name"] == "iPad Child One"
    assert any(m["type"] == "log_entry" for m in hub.sent["broadcast"])


async def test_override_set_has_no_taxonomy_kind_and_no_ha_event(hub):
    cid = await add_client(hub.db, name="iPhone")
    await changes.audit(
        hub.db, "override_set", actor="Admin One", client_id=cid, rule_key="columns"
    )
    entry = (await _list(hub.db))["entries"][0]
    assert entry["kind"] == "override_set" and entry["event_kind"] is None
    assert not [m for m in hub.sent["integration"] if m["type"] == "ha_event"]


async def test_pairing_and_removal(hub):
    db = hub.db
    cid = await add_client(db, name="iPad Küche")
    await event_log.device_paired(db, cid, guest=False, method="pin")
    await event_log.device_removed(db, cid, how="deleted", actor="Admin One")
    gid = await add_client(
        db, name="Guest One", guest=True, expires_at="2026-12-01T10:00:00+00:00"
    )
    await event_log.device_paired(db, gid, guest=True)
    await event_log.device_removed(db, gid, how="guest_expired")
    kinds = [e["event_kind"] for e in (await _list(db))["entries"]]
    assert kinds == ["guest_expired", "guest_paired", "device_removed", "device_paired"]
    guest_paired = (await _list(db, kind="guest_paired"))["entries"][0]
    assert guest_paired["until"] == "2026-12-01T10:00:00+00:00"


async def test_important_filter_needs_one_request(hub):
    """Alarms plus the kinds marked important, also behind many changes."""
    db = hub.db
    cid = await add_client(
        db, name="iPad Child One", owner_label="Child One", profile="child"
    )
    await event_log.device_paired(db, cid, guest=False, method="pin")
    await event_log.record(
        db,
        category="activity",
        kind="kids_release_requested",
        event_kind="kids_release_requested",
        client_id=cid,
        entity_id="light.kitchen",
    )
    for _ in range(3):
        await event_log.hub_control(
            db,
            cid,
            entity_id="light.kitchen",
            action="turn_on",
            ok=False,
            reason="quiet_hours",
        )
    # 250 unimportant changes after the important ones: the important filter
    # still finds every important entry, the pairing included.
    for _ in range(250):
        await changes.audit(
            db, "override_set", actor="Admin One", client_id=cid, rule_key="columns"
        )
    all_entries = (await _list(db, limit=500))["entries"]
    flagged = {e["id"] for e in all_entries if e["important"]}
    result = await _list(db, range="7d", important=True, limit=6)
    entries = result["entries"]
    assert {e["id"] for e in entries} == flagged
    kinds = {(e["category"], e["event_kind"] or e["kind"]) for e in entries}
    assert ("change", "device_paired") in kinds
    assert ("activity", "kids_release_requested") in kinds
    assert any(e["category"] == "alarm" for e in entries)
    # single rejections are activity and not important
    assert not any(e["kind"] == "kids_bedtime_attempt" for e in entries)
    assert all(e["important"] for e in entries)
    assert not result["has_more"]
    # combines with the other filters
    alarms = (await _list(db, important=True, category="alarm"))["entries"]
    assert alarms and all(e["category"] == "alarm" for e in alarms)


def test_important_kinds_follow_the_taxonomy():
    assert set(event_log.important_kinds()) == {
        "device_paired",
        "device_set_up",
        "access_requested",
        "access_request_answered",
        "guest_paired",
        "guest_expiring",
        "guest_expired",
        "profile_changed",
        "family_change",
        "kids_release_requested",
        "kids_request_answered",
        "kids_request_expired",
    }
    assert not any(event_log.is_alarm_kind(k) for k in event_log.important_kinds())
    assert event_log.is_important({"category": "alarm", "kind": "denied"})
    assert not event_log.is_important({"category": "activity", "kind": "denied"})
    assert event_log.is_important(
        {
            "category": "change",
            "kind": "profile_assigned",
            "event_kind": "profile_changed",
        }
    )


async def test_entry_keeps_names_after_the_device_is_gone(hub):
    db = hub.db
    cid = await add_client(db, name="Altes iPad", owner_label="Child Two")
    await event_log.device_paired(db, cid, guest=False)
    async with db.transaction():
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (cid,))
    entry = (await _list(db))["entries"][0]
    assert entry["device_name"] == "Altes iPad" and entry["owner_label"] == "Child Two"
    assert entry["device_exists"] is False


async def test_hub_control_path_of_children(hub):
    db = hub.db
    cid = await add_client(
        db, name="iPad Child Three", owner_label="Child Three", profile="child"
    )
    await event_log.hub_control(
        db, cid, entity_id="light.kitchen", action="turn_on", ok=True
    )
    await event_log.hub_control(
        db,
        cid,
        entity_id="light.kitchen",
        action="turn_on",
        ok=False,
        reason="quiet_hours",
        until="2026-11-20T06:00:00+00:00",
    )
    entries = (await _list(db, category="activity"))["entries"]
    assert [e["kind"] for e in entries] == ["kids_bedtime_attempt", "kids_control"]
    assert (
        entries[0]["result"] == "denied"
        and entries[0]["reason"] == "blocked_kids_bedtime"
    )
    assert (
        entries[1]["control_path"] == "hub"
        and entries[1]["entity_name"] == "Küchenlicht"
    )


# ── integration events ──────────────────────────────────────────────────────


async def test_integration_events_idempotent(hub):
    db = hub.db
    cid = await add_client(db, name="iPhone Air", owner_label="Admin One")
    ev = _ev(
        "control",
        cid,
        entity_id="light.kitchen",
        action="turn_on",
        route="external",
        origin="quick_access",
    )
    assert await _post_events(db, [ev]) == {"accepted": 1, "duplicates": 0}
    assert await _post_events(db, [ev]) == {"accepted": 0, "duplicates": 1}
    entries = (await _list(db))["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert (e["kind"], e["category"], e["route"], e["origin"], e["control_path"]) == (
        "control",
        "activity",
        "external",
        "quick_access",
        "checked",
    )
    assert e["source"] == "integration" and e["result"] == "ok"


async def test_rules_applied_is_accepted_not_stored(hub):
    cid = await add_client(hub.db, name="iPhone")
    r = await _post_events(
        hub.db, [_ev("rules_applied", cid, rules_hash="a" * 64, ha_group="read_only")]
    )
    assert r["accepted"] == 1
    assert (await _list(hub.db))["entries"] == []


async def test_integration_event_validation():
    with pytest.raises(ValidationError):
        IntegrationEvent(
            **_ev(
                "denied",
                str(uuid.uuid4()),
                entity_id="light.a",
                action="turn_on",
                route="local",
            )
        )
    with pytest.raises(ValidationError):
        IntegrationEvent(
            **_ev(
                "control",
                str(uuid.uuid4()),
                entity_id="light.a",
                action="turn_on",
                route="local",
                data={"x": 1},
            )
        )
    with pytest.raises(ValidationError):
        IntegrationEvent(**_ev("integration_outage"))
    with pytest.raises(ValidationError):
        IntegrationEventsRequest(events=[], dropped=0)


async def test_integration_events_need_the_integration_token(hub):
    body = IntegrationEventsRequest(
        events=[_ev("integration_started", version="1")], dropped=0
    )
    with pytest.raises(HTTPException) as e:
        await integration_api.integration_events_intake(_req(hub.db), body)
    assert e.value.status_code == 403


async def test_single_denial_is_activity_alarm_from_threshold(hub):
    db = hub.db
    cid = await add_client(db, name="iPhone Child Three", owner_label="Child Three")
    now = datetime.now(UTC)

    def deny(i):
        return _ev(
            "denied",
            cid,
            at=(now + timedelta(seconds=i)).isoformat(timespec="seconds"),
            entity_id="lock.front_door",
            action="unlock",
            route="local",
            reason="blocked_area",
        )

    await _post_events(db, [deny(0), deny(1)])
    assert (await _list(db, category="alarm"))["entries"] == []
    assert len((await _list(db, category="activity"))["entries"]) == 2
    await _post_events(db, [deny(2)])
    alarms = (await _list(db, category="alarm"))["entries"]
    assert len(alarms) == 1
    assert alarms[0]["kind"] == "denied" and alarms[0]["count"] == 3
    assert alarms[0]["reason"] == "blocked_area" and alarms[0]["unread"]
    # a 4th in the same window does not raise a second alarm
    await _post_events(db, [deny(3), deny(4), deny(5)])
    assert len((await _list(db, category="alarm"))["entries"]) == 1


async def test_access_alarms_and_outage_are_alarms(hub):
    db = hub.db
    cid = await add_client(
        db, name="Gast Guest One", guest=True, expires_at="2026-12-01T10:00:00+00:00"
    )
    await _post_events(
        db,
        [
            _ev("access_group_restored", cid, ha_group="read_only"),
            _ev(
                "direct_access_attempt",
                cid,
                count=6,
                reliability="best_effort",
                since="2026-11-19T10:00:00Z",
            ),
            _ev(
                "integration_outage",
                since="2026-11-19T09:00:00+00:00",
                reliability="best_effort",
            ),
            _ev("integration_started", version="2026.12.0"),
        ],
    )
    alarms = (await _list(db, category="alarm"))["entries"]
    assert {a["kind"] for a in alarms} == {
        "access_group_restored",
        "direct_access_attempt",
        "integration_outage",
    }
    started = (await _list(db, category="change"))["entries"][0]
    assert (
        started["kind"] == "integration_started"
        and started["detail"]["version"] == "2026.12.0"
    )


async def test_child_activity_of_the_integration_is_not_stored_twice(hub):
    """A child device switches through the hub, which records it itself; the
    integration's report of the same call (known only by its HA user) is
    accepted and mapped, but not stored a second time."""
    db = hub.db
    cid = await add_client(
        db, name="iPad Child Three", profile="child", ha_user_id="a" * 32
    )
    await event_log.hub_control(
        db, cid, entity_id="light.kitchen", action="turn_on", ok=True
    )
    ev = {
        **_ev(
            "kids_control",
            None,
            entity_id="light.kitchen",
            action="turn_on",
            route="local",
        ),
        "ha_user_id": "a" * 32,
    }
    assert await _post_events(db, [ev]) == {"accepted": 1, "duplicates": 0}
    entries = (await _list(db))["entries"]
    assert [(e["client_id"], e["source"]) for e in entries] == [(cid, "hub")]


async def test_hub_call_of_a_hub_only_device_is_logged_once(hub):
    """A device on Hub only without parental controls: the hub logs its call
    (kind control, control_path hub) and drops the integration's report of
    the same call; a later report of the device is stored again."""
    db = hub.db
    guest = await add_client(db, name="iPhone Gast", guest=True, ha_user_id="a" * 32)
    event_log.expect_hub_call(guest, "light.kitchen", "turn_on")
    await event_log.hub_control(
        db, guest, entity_id="light.kitchen", action="turn_on", ok=True, child=False
    )
    report = _ev(
        "control", guest, entity_id="light.kitchen", action="turn_on", route="local"
    )
    assert await _post_events(db, [report]) == {"accepted": 1, "duplicates": 0}
    entries = (await _list(db))["entries"]
    assert [(e["kind"], e["source"], e["control_path"]) for e in entries] == [
        ("control", "hub", "hub")
    ]
    # Nothing waits any more: the next report is the device's own.
    again = _ev(
        "control", guest, entity_id="light.kitchen", action="turn_on", route="local"
    )
    await _post_events(db, [again])
    assert len((await _list(db))["entries"]) == 2


async def test_a_report_of_another_action_is_never_dropped(hub):
    """The mark of a hub call (turn_on) drops only the report of that call:
    a direct call of another action on the same entity is stored."""
    db = hub.db
    guest = await add_client(db, name="iPhone Gast", guest=True, ha_user_id="a" * 32)
    event_log.expect_hub_call(guest, "lock.front", "lock")
    other = _ev(
        "control", guest, entity_id="lock.front", action="unlock", route="local"
    )
    await _post_events(db, [other])
    entries = (await _list(db))["entries"]
    assert [(e["action"], e["source"]) for e in entries] == [("unlock", "integration")]
    same = _ev("control", guest, entity_id="lock.front", action="lock", route="local")
    await _post_events(db, [same])
    assert len((await _list(db))["entries"]) == 1


async def test_hub_call_that_never_reached_the_integration_drops_nothing(hub):
    db = hub.db
    guest = await add_client(db, name="iPhone Gast", guest=True, ha_user_id="a" * 32)
    mark = event_log.expect_hub_call(guest, "light.kitchen", "turn_on")
    event_log.forget_hub_call(mark)
    await _post_events(
        db,
        [
            _ev(
                "control",
                guest,
                entity_id="light.kitchen",
                action="turn_on",
                route="local",
            )
        ],
    )
    assert [e["source"] for e in (await _list(db))["entries"]] == ["integration"]


async def test_hub_only_denial_is_logged_with_the_integration_reason(hub):
    db = hub.db
    guest = await add_client(db, name="iPhone Gast", guest=True, ha_user_id="a" * 32)
    await event_log.hub_control(
        db,
        guest,
        entity_id="light.kitchen",
        action="turn_on",
        ok=False,
        reason="blocked_area",
        child=False,
    )
    (entry,) = (await _list(db))["entries"]
    assert (entry["kind"], entry["result"], entry["reason"]) == (
        "denied",
        "denied",
        "blocked_area",
    )


# ── HA call_service (direct) ────────────────────────────────────────────────


async def test_direct_call_service_is_mapped_by_device_user(hub):
    db = hub.db
    cid = await add_client(
        db,
        name="iPhone Air",
        owner_label="Admin One",
        ha_user_id="a" * 32,
        overrides={"control_path": "direct"},
    )
    child = await add_client(
        db, name="iPad Child Three", profile="child", ha_user_id="b" * 32
    )
    await event_log.ha_call_service(
        db,
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.kitchen", "brightness": 10},
        },
        {"user_id": "a" * 32},
    )
    await event_log.ha_call_service(
        db,
        {"domain": "halumar", "service": "control", "service_data": {}},
        {"user_id": "a" * 32},
    )
    await event_log.ha_call_service(
        db,
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.kitchen"},
        },
        {"user_id": "b" * 32},
    )
    await event_log.ha_call_service(
        db,
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.kitchen"},
        },
        {"user_id": "c" * 32},
    )
    entries = (await _list(db))["entries"]
    assert len(entries) == 1
    e = entries[0]
    assert (e["client_id"], e["control_path"], e["source"], e["action"]) == (
        cid,
        "direct",
        "ha",
        "turn_on",
    )
    assert "brightness" not in json.dumps(e)
    assert child


# ── hidden persons, filters, paging ─────────────────────────────────────────


async def test_hidden_person_activity_not_stored_changes_are(hub):
    db = hub.db
    cid = await add_client(db, name="iPad Child One", owner_label="Child One")
    await api.put_log_settings(
        _req(db), api.LogSettings(retention_days=30, hidden_owners=["Child One"])
    )
    await event_log.hub_control(
        db, cid, entity_id="light.kitchen", action="turn_on", ok=True
    )
    r = await _post_events(
        db,
        [
            _ev(
                "control",
                cid,
                entity_id="light.kitchen",
                action="turn_on",
                route="local",
            )
        ],
    )
    assert r["accepted"] == 1
    await changes.audit(
        db, "override_set", actor="Admin One", client_id=cid, rule_key="columns"
    )
    entries = (await _list(db))["entries"]
    assert [e["category"] for e in entries] == ["change"]


async def test_filters_search_and_paging(hub):
    db = hub.db
    a = await add_client(db, name="iPhone Air", owner_label="Admin One")
    b = await add_client(db, name="iPad Child One", owner_label="Child One")
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
    await _post_events(
        db,
        [
            _ev(
                "control", a, entity_id="light.kitchen", action="turn_on", route="local"
            ),
            _ev(
                "control",
                b,
                entity_id="cover.garage_door",
                action="open_cover",
                route="external",
            ),
            _ev(
                "control",
                b,
                at=old,
                entity_id="light.kitchen",
                action="turn_off",
                route="local",
            ),
        ],
    )
    assert len((await _list(db, client_id=b))["entries"]) == 2
    assert len((await _list(db, owner="Admin One"))["entries"]) == 1
    assert len((await _list(db, range="7d"))["entries"]) == 2
    assert len((await _list(db, range="today"))["entries"]) >= 1
    assert [e["entity_id"] for e in (await _list(db, q="garage"))["entries"]] == [
        "cover.garage_door"
    ]
    assert len((await _list(db, q="küchenlicht"))["entries"]) == 2  # entity name
    assert len((await _list(db, q="garage_door"))["entries"]) == 1  # "_" is literal
    assert (await _list(db, q="garage%door"))["entries"] == []
    page = await _list(db, limit=2)
    assert len(page["entries"]) == 2 and page["has_more"]
    rest = await _list(db, before_id=page["entries"][-1]["id"], limit=2)
    assert len(rest["entries"]) == 1 and not rest["has_more"]
    with pytest.raises(HTTPException):
        await _list(db, range="year")


async def test_summary_read_marks_and_facets(hub):
    db = hub.db
    cid = await add_client(
        db,
        name="Gast Guest One",
        owner_label="Guest One",
        guest=True,
        expires_at="2026-12-01T10:00:00+00:00",
    )
    await _post_events(
        db, [_ev("access_group_restored", cid), _ev("access_group_restored", cid)]
    )
    s = await api.get_summary(_req(db, hub.ha))
    assert s["unread_alarms"] == 2 and s["counts"]["alarms_7d"] == 2
    assert {"id": cid, "name": "Gast Guest One"} in s["facets"]["devices"]
    assert "Guest One" in s["facets"]["owners"]
    first = s["unread"][0]["id"]
    assert (await api.mark_read(_req(db), api.ReadRequest(ids=[first])))["marked"] == 1
    assert (await api.get_summary(_req(db, hub.ha)))["unread_alarms"] == 1
    assert (await api.mark_read(_req(db), api.ReadRequest(all=True)))["marked"] == 1
    assert (await api.get_summary(_req(db, hub.ha)))["unread_alarms"] == 0
    with pytest.raises(ValidationError):
        api.ReadRequest()


async def test_admin_only(hub):
    with pytest.raises(HTTPException) as e:
        await api.get_summary(_req(hub.db, auth="client"))
    assert e.value.status_code == 403


# ── retention ───────────────────────────────────────────────────────────────


async def test_retention_choices_and_nightly_cleanup(hub):
    db = hub.db
    with pytest.raises(ValidationError):
        api.LogSettings(retention_days=14)
    cid = await add_client(db, name="iPhone")
    now = datetime.now(UTC)
    for days in (1, 8, 40):
        await _post_events(
            db,
            [
                _ev(
                    "control",
                    cid,
                    at=(now - timedelta(days=days)).isoformat(timespec="seconds"),
                    entity_id="light.kitchen",
                    action="turn_on",
                    route="local",
                )
            ],
        )
    assert await event_log.cleanup(db, now) == 1  # default 30 days
    await api.put_log_settings(_req(db), api.LogSettings(retention_days=7))
    assert await event_log.cleanup(db, now) == 1
    assert len((await _list(db))["entries"]) == 1
    # once per local night (from 03:00), at the latest after 24 h
    assert not await event_log.cleanup_due(db, now + timedelta(hours=1), "UTC")
    assert await event_log.cleanup_due(db, now + timedelta(hours=25), "UTC")
    next_night = (now + timedelta(days=1)).replace(hour=3, minute=5)
    assert await event_log.cleanup_due(db, next_night, "UTC")
    assert not await event_log.cleanup_due(db, next_night.replace(hour=2), "UTC") or (
        next_night.replace(hour=2) - now >= timedelta(hours=24)
    )


# ── notifications ───────────────────────────────────────────────────────────


async def test_rule_validation(hub):
    db = hub.db
    child = await add_client(db, name="iPad Child Three", profile="child")
    guest = await add_client(
        db, name="Guest One", guest=True, expires_at="2026-12-01T10:00:00+00:00"
    )
    member = await add_client(db, name="iPhone Air")
    with pytest.raises(HTTPException) as e:
        await _save_rules(db, [_rule(["denied"], notify=())])
    assert e.value.detail["code"] == "notifications.no_target"
    expired = await add_client(
        db, name="Alt", guest=True, expires_at="2026-01-01T10:00:00+00:00"
    )
    watch = await add_client(
        db, name="Apple Watch", platform="watchos", set_up_by_client_id=member
    )
    for bad in (str(uuid.uuid4()), expired, watch):
        with pytest.raises(HTTPException) as e:
            await _save_rules(db, [_rule(["denied"], notify=(), devices=(bad,))])
        assert e.value.detail["code"] == "notifications.device_unknown"
    # Every paired device may be a target, also children and guests
    saved = await _save_rules(
        db, [_rule(["denied"], notify=(), devices=(member, child, guest))], retention=90
    )
    assert saved["retention_days"] == 90 and len(saved["rules"]) == 1
    with pytest.raises(ValidationError):
        _ = api.Rule(**_rule(["no_such_kind"]))
    with pytest.raises(ValidationError):
        _ = api.Rule(**_rule(["denied"], notify=("light.kitchen",)))


async def test_notify_targets(hub):
    db = hub.db
    phone = await add_client(db, name="iPhone Air")
    await add_client(db, name="iPad Child Three", profile="child")
    end = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
    await add_client(db, name="Gast Guest One", guest=True, expires_at=end)
    await add_client(
        db, name="Gast Alt", guest=True, expires_at="2026-01-01T10:00:00+00:00"
    )
    # A watch has no hub channel: it shows its phone's notifications
    await add_client(
        db, name="Apple Watch", platform="watchos", set_up_by_client_id=phone
    )
    t = await api.get_targets(_req(db, hub.ha))
    assert [s["service"] for s in t["notify"]] == [
        "notify.mobile_app_x",
        "notify.persistent_notification",
    ]
    assert [(d["name"], d["kind"], d["expires_at"]) for d in t["devices"]] == [
        ("Gast Guest One", "guest", end),
        ("iPad Child Three", "child", None),
        ("iPhone Air", "member", None),
    ]


async def test_child_and_guest_devices_get_it_through_the_hub(hub):
    db = hub.db
    child = await add_client(db, name="iPad Child Three", profile="child")
    end = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
    guest = await add_client(db, name="Haushüter", guest=True, expires_at=end)
    await _save_rules(db, [_rule(["device_paired"], notify=(), devices=(child, guest))])
    new = await add_client(db, name="iPad Küche")
    await event_log.device_paired(db, new, guest=False)
    shown = {
        cid
        for cid, m in hub.sent["to"]
        if m.get("command") == "display_notification" and "iPad Küche" in m["message"]
    }
    assert shown == {child, guest}
    stored = await db.fetchall(
        "SELECT client_id FROM client_notifications WHERE client_id IN (?, ?)",
        (child, guest),
    )
    assert len(stored) == 2


async def test_device_notification_syncs_the_ha_sensor(hub, monkeypatch):
    """The per-device sensor is the app's way away from home."""
    from services import ha_sensors

    synced = []

    async def _sync(db, ha_client):
        synced.append(ha_client)

    monkeypatch.setattr(ha_sensors, "sync_notification_sensors_safe", _sync)
    db = hub.db
    phone = await add_client(db, name="iPhone Air")
    rule = _rule(["device_paired"], notify=(), devices=(phone,))
    await event_notify.send(db, rule, [], text="x")
    assert synced == [hub.ha]
    # nothing delivered to a device: no sync
    await event_notify.send(db, _rule(["device_paired"]), [], text="x")
    assert synced == [hub.ha]


async def test_removed_guest_is_pruned_from_the_rules(hub):
    db = hub.db
    end = (datetime.now(UTC) + timedelta(days=2)).isoformat(timespec="seconds")
    guest = await add_client(db, name="Guest One", guest=True, expires_at=end)
    member = await add_client(db, name="iPhone Air")
    only = _rule(["denied"], notify=(), devices=(guest,))
    both = _rule(["control"], notify=(), devices=(guest, member), client_ids=[guest])
    await _save_rules(db, [only, both])
    await event_log.device_removed(db, guest, how="guest_expired")
    rules = {r["id"]: r for r in (await event_notify.get_config(db))["rules"]}
    assert rules[only["id"]]["targets"]["devices"] == []
    assert rules[only["id"]]["enabled"] is False
    assert rules[both["id"]]["targets"]["devices"] == [member]
    assert rules[both["id"]]["filters"]["client_ids"] == []
    assert rules[both["id"]]["enabled"] is True


async def test_expired_guest_target_is_skipped(hub):
    db = hub.db
    guest = await add_client(
        db, name="Alt", guest=True, expires_at="2026-01-01T10:00:00+00:00"
    )
    rule = _rule(["denied"], devices=(guest,))
    results = await event_notify.send(db, rule, [], text="x", test=True)
    assert results[-1] == {
        "target": guest,
        "status": "failed",
        "error": "device_gone",
    }


async def test_threshold_rule_notifies_from_third_denial(hub):
    db = hub.db
    cid = await add_client(db, name="iPhone Child Three")
    await _save_rules(
        db, [_rule(["denied"], threshold={"count": 3, "window_minutes": 10})]
    )

    def deny():
        return _ev(
            "denied",
            cid,
            entity_id="lock.front_door",
            action="unlock",
            route="local",
            reason="blocked_area",
        )

    await _post_events(db, [deny(), deny()])
    assert hub.ha.calls == []
    await _post_events(db, [deny()])
    assert len(hub.ha.calls) == 1
    domain, service, data = hub.ha.calls[0]
    assert (domain, service) == ("notify", "persistent_notification")
    assert "3" in data["message"] and "iPhone Child Three" in data["message"]
    # the notified entry remembers where it went
    entry = (await _list(db, category="activity"))["entries"][0]
    assert entry["detail"]["sent"][0]["status"] == "handed_over"


async def test_filters_route_and_entities(hub):
    db = hub.db
    cid = await add_client(db, name="iPhone")
    await _save_rules(
        db, [_rule(["control"], routes=["external"], entity_ids=["cover.garage_door"])]
    )
    await _post_events(
        db,
        [
            _ev(
                "control",
                cid,
                entity_id="cover.garage_door",
                action="open_cover",
                route="local",
            ),
            _ev(
                "control",
                cid,
                entity_id="light.kitchen",
                action="turn_on",
                route="external",
            ),
        ],
    )
    assert hub.ha.calls == []
    await _post_events(
        db,
        [
            _ev(
                "control",
                cid,
                entity_id="cover.garage_door",
                action="open_cover",
                route="external",
            )
        ],
    )
    assert len(hub.ha.calls) == 1
    assert hub.ha.calls[0][2]["message"].endswith(("unterwegs", "away"))


async def test_device_target_appears_when_app_open(hub):
    db = hub.db
    member = await add_client(db, name="iPhone Air")
    await _save_rules(db, [_rule(["device_paired"], notify=(), devices=(member,))])
    new = await add_client(db, name="iPad Küche")
    await event_log.device_paired(db, new, guest=False)
    shown = [
        m
        for cid, m in hub.sent["to"]
        if cid == member and m.get("command") == "display_notification"
    ]
    assert shown and "iPad Küche" in shown[0]["message"]


async def test_bundling_collects_and_tick_sends_one(hub):
    db = hub.db
    cid = await add_client(db, name="iPad Child One", profile="child")
    await _save_rules(db, [_rule(["kids_control"], bundle_minutes=10)])
    for _ in range(4):
        await event_log.hub_control(
            db, cid, entity_id="light.kitchen", action="toggle", ok=True
        )
    assert hub.ha.calls == []
    now = datetime.now(UTC)
    await event_notify.tick(db, now + timedelta(minutes=5), "UTC")
    assert hub.ha.calls == []
    await event_notify.tick(db, now + timedelta(minutes=11), "UTC")
    assert len(hub.ha.calls) == 1 and "4×" in hub.ha.calls[0][2]["message"]


async def test_pause_everything_notifies_once_per_child_and_quarter_hour(hub):
    # Off unless the admin adds a rule; then at most one message per
    # child every 15 minutes, the rest bundled; log and ha_event always.
    db = hub.db
    child_one = await add_client(db, name="iPad Child One", profile="child")
    child_two = await add_client(db, name="iPad Child Two", profile="child")
    kids_mode._tz_cache = (10**12, "Europe/Zurich")

    async def pause(cid, trigger="entities"):
        await event_log.kids_paused_all(
            db,
            cid,
            entity_id="light.kitchen",
            action="toggle",
            until="2026-11-20T13:05:00+00:00",
            info={"trigger": trigger, "actions": 4, "entities": ["light.kitchen"]},
        )

    await pause(child_one)
    assert hub.ha.calls == []  # no rule: nothing sent
    events = [m for m in hub.sent["integration"] if m["type"] == "ha_event"]
    assert events and events[-1]["data"]["kind"] == "kids_paused_all"
    assert events[-1]["data"]["reason"] == "blocked_kids_paused_all"

    await _save_rules(db, [_rule(["kids_paused_all"])])
    await pause(child_one)
    assert len(hub.ha.calls) == 1
    assert hub.ha.calls[0][2]["message"] == (
        "iPad Child One: alles pausiert bis 14:05 (zu viele Geräte in kurzer Zeit)"
    )
    await pause(child_one, trigger="actions")
    await pause(child_one)
    await pause(child_two, trigger="actions")  # another child: its own quarter hour
    assert len(hub.ha.calls) == 2
    assert "iPad Child Two" in hub.ha.calls[1][2]["message"]
    assert "zu viele Aktionen" in hub.ha.calls[1][2]["message"]
    now = datetime.now(UTC)
    await event_notify.tick(db, now + timedelta(minutes=10), "UTC")
    assert len(hub.ha.calls) == 2
    await event_notify.tick(db, now + timedelta(minutes=16), "UTC")
    assert len(hub.ha.calls) == 3
    assert hub.ha.calls[2][2]["message"].startswith("iPad Child One: 2×")
    entries = (await _list(db, kind="kids_paused_all"))["entries"]
    assert len(entries) == 5


async def test_quiet_hours_queue_alarms_and_drop_others(hub):
    db = hub.db
    cid = await add_client(db, name="iPhone")
    all_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    quiet = [{"days": all_days, "start": "00:00", "end": "23:59"}]
    await _save_rules(
        db,
        [
            _rule(["access_group_restored"], quiet_hours=quiet),
            _rule(["device_paired"], quiet_hours=quiet),
        ],
    )
    # time zone UTC for the decision
    kids_mode._tz_cache = (10**12, "UTC")
    await _post_events(db, [_ev("access_group_restored", cid)])
    await event_log.device_paired(db, cid, guest=False)
    assert hub.ha.calls == []
    after = datetime.now(UTC).replace(hour=23, minute=59, second=30)
    await event_notify.tick(db, after, "UTC")
    assert len(hub.ha.calls) == 1
    assert hub.ha.calls[0][2]["message"].startswith(
        ("Zugriffsgruppe wiederhergestellt", "Access group restored")
    )


def test_quiet_window_over_midnight():
    rule = {"quiet_hours": [{"days": ["fri"], "start": "22:00", "end": "07:00"}]}
    fri_23 = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)  # Friday
    sat_06 = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
    sat_23 = datetime(2026, 11, 21, 23, 0, tzinfo=UTC)
    assert event_notify.in_quiet(rule, fri_23, "UTC") == datetime(
        2026, 11, 21, 7, 0, tzinfo=UTC
    )
    assert event_notify.in_quiet(rule, sat_06, "UTC") == datetime(
        2026, 11, 21, 7, 0, tzinfo=UTC
    )
    assert event_notify.in_quiet(rule, sat_23, "UTC") is None


async def test_test_send_reports_per_target(hub):
    db = hub.db
    member = await add_client(db, name="iPhone Air")
    rule = _rule(
        ["denied"],
        notify=("notify.persistent_notification", "notify.broken"),
        devices=(member,),
    )
    hub.ha.fail.add("broken")
    await _save_rules(db, [rule])
    result = await api.test_event_notification(
        _req(db), api.TestRequest(rule_id=rule["id"])
    )
    status = {r["target"]: r["status"] for r in result["results"]}
    assert status == {
        "notify.persistent_notification": "handed_over",
        "notify.broken": "failed",
        member: "shown",
    }
    assert result["ha_event"] == "handed_over"
    test_event = [m for m in hub.sent["integration"] if m["type"] == "ha_event"][-1]
    assert test_event["data"]["test"] is True and test_event["data"]["kind"] == "denied"
    for schema, answer in (
        ("rest/event-notifications-test.response.json", result),
        ("rest/event-notifications.json", await api.get_event_notifications(_req(db))),
        ("ws/integration/ha_event.json", test_event),
    ):
        check = _log_contract(schema)
        if check is not None:
            errors = [e.message for e in check.iter_errors(answer)]
            assert errors == [], (schema, errors[:3])
    # an unsaved draft can be tested too
    draft = api.TestRequest(rule=api.Rule(**_rule(["control"])))
    assert (await api.test_event_notification(_req(db), draft))["results"][0][
        "status"
    ] == "handed_over"
    with pytest.raises(HTTPException):
        await api.test_event_notification(
            _req(db), api.TestRequest(rule_id=str(uuid.uuid4()))
        )


async def test_guest_expiring_once_per_rule(hub):
    db = hub.db
    end = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    gid = await add_client(db, name="Guest One", guest=True, expires_at=end)
    await _save_rules(db, [_rule(["guest_expiring"], lead_minutes=180)])
    now = datetime.now(UTC)
    await event_notify.tick(db, now, "UTC")
    await event_notify.tick(db, now + timedelta(minutes=1), "UTC")
    assert len(hub.ha.calls) == 1 and "Guest One" in hub.ha.calls[0][2]["message"]
    entries = (await _list(db, kind="guest_expiring"))["entries"]
    assert len(entries) == 1 and entries[0]["client_id"] == gid


async def test_hub_detects_integration_outage(hub):
    db = hub.db
    await _save_rules(
        db, [_rule(["integration_outage", "integration_started"], delay_minutes=5)]
    )
    t0 = datetime.now(UTC)
    await event_log.watch_integration(db, t0, connected=True)
    await event_log.watch_integration(db, t0 + timedelta(seconds=30), connected=False)
    await event_log.watch_integration(db, t0 + timedelta(seconds=60), connected=False)
    assert (await _list(db, category="alarm"))["entries"] == []
    await event_log.watch_integration(db, t0 + timedelta(seconds=100), connected=False)
    alarms = (await _list(db, category="alarm"))["entries"]
    assert len(alarms) == 1 and alarms[0]["kind"] == "integration_outage"
    assert hub.ha.calls == []  # live outage: notified after the rule's delay
    await event_notify.tick(db, t0 + timedelta(minutes=6), "UTC")
    assert len(hub.ha.calls) == 1
    await event_notify.tick(db, t0 + timedelta(minutes=7), "UTC")
    assert len(hub.ha.calls) == 1
    await event_log.watch_integration(db, t0 + timedelta(minutes=8), connected=True)
    assert len(hub.ha.calls) == 2  # back again
    assert (await _list(db, category="alarm"))["entries"][0]["until"]


def test_taxonomy_matches_contract_draft():
    tax = event_log.taxonomy()
    # + family, + wall panel: panel_command, panel_command_rejected,
    # + kids_paused_all, + access_opened/access_closed, + tag_scanned
    # + access_requested/access_request_answered/access_request_expired
    assert len(tax) == 32
    assert tax["tag_scanned"]["source"] == "hub" and not tax["tag_scanned"]["alarm"]
    assert tax["kids_paused_all"]["source"] == "hub"
    assert tax["kids_paused_all"]["notify_min_interval_minutes"] == 15
    assert {k for k, v in tax.items() if v["alarm"]} == {
        "denied",
        "access_group_restored",
        "direct_access_attempt",
        "integration_outage",
    }
    assert set(event_log.INTEGRATION_CATEGORY) == {
        k for k, v in tax.items() if v["source"] == "integration"
    }


def test_message_texts_have_no_values():
    entry = {
        "event_kind": "control",
        "device_name": "iPhone Air",
        "owner_label": "Admin One",
        "entity_name": "Küchenlicht",
        "action": "turn_on",
        "route": "external",
    }
    assert (
        event_notify.message(entry, "de")
        == "iPhone Air (Admin One): Küchenlicht turn on · unterwegs"
    )
    assert (
        event_notify.message(
            {"event_kind": "guest_expired", "device_name": "Guest One"}, "en"
        )
        == "Guest access of Guest One ended"
    )


async def test_entries_beyond_retention_are_not_shown(hub):
    db = hub.db
    cid = await add_client(db, name="iPhone")
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    await _post_events(
        db,
        [
            _ev(
                "control",
                cid,
                at=old,
                entity_id="light.kitchen",
                action="turn_on",
                route="local",
            )
        ],
    )
    assert (await _list(db))["entries"] == []
    assert (await api.get_summary(_req(db, hub.ha)))["total"] == 0
    await api.put_log_settings(_req(db), api.LogSettings(retention_days=90))
    assert len((await _list(db))["entries"]) == 1
    assert (await api.get_summary(_req(db, hub.ha)))["total"] == 1


def _log_contract(schema: str):
    """Validator of the contract log schemas (CONTRACTS_LOG_DIR, CONTRACTS_DIR or .contracts)."""
    import os
    from pathlib import Path

    root = (
        os.environ.get("CONTRACTS_LOG_DIR")
        or os.environ.get("CONTRACTS_DIR")
        or str(Path(__file__).resolve().parents[2] / ".contracts")
    )
    path = Path(root) / "schemas" / schema
    if not path.exists():
        return None
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")

    def retrieve(uri):
        return referencing.Resource.from_contents(
            json.loads(Path(uri.replace("file://", "")).read_text())
        )

    body = json.loads(path.read_text())
    body["$id"] = path.as_uri()
    return jsonschema.Draft202012Validator(
        body, registry=referencing.Registry(retrieve=retrieve)
    )


async def test_answers_match_the_contract_proposal(hub):
    names = (
        "rest/log.response.json",
        "rest/log-summary.response.json",
        "rest/log-settings.json",
        "rest/notify-targets.response.json",
    )
    checks = {n: _log_contract(n) for n in names}
    if None in checks.values():
        pytest.skip("contract log schemas not found (CONTRACTS_LOG_DIR)")
    db = hub.db
    cid = await add_client(
        db, name="iPad Child One", owner_label="Child One", profile="child"
    )
    await event_log.hub_control(
        db, cid, entity_id="light.kitchen", action="turn_on", ok=True
    )
    for _ in range(3):
        await event_log.hub_control(
            db,
            cid,
            entity_id="light.kitchen",
            action="turn_on",
            ok=False,
            reason="quiet_hours",
        )
    await changes.audit(
        db,
        "override_set",
        actor="Admin One",
        client_id=cid,
        rule_key="columns",
        before=None,
        after={"value": 3},
    )
    answers = {
        "rest/log.response.json": await _list(db),
        "rest/log-summary.response.json": await api.get_summary(_req(db, hub.ha)),
        "rest/log-settings.json": await api.get_log_settings(_req(db)),
        "rest/notify-targets.response.json": await api.get_targets(_req(db, hub.ha)),
    }
    assert len(answers["rest/log.response.json"]["entries"]) == 6
    for name, answer in answers.items():
        errors = [e.message for e in checks[name].iter_errors(answer)]
        assert errors == [], (name, errors[:3])


# ── Payloads inside the contract enums ─────────────────────────


def _contract_errors(schema: str, instance: dict) -> list[str]:
    check = _log_contract(schema)
    if check is None:
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    return [
        f"{list(e.absolute_path)}: {e.message}" for e in check.iter_errors(instance)
    ]


@pytest.mark.parametrize("via,route", [("home", "local"), ("away", "external")])
async def test_family_and_access_entries_carry_home_away_as_route(hub, via, route):
    """Origin is app/quick_access; home/away went into it as well."""
    from services import access_requests
    from services.family import events as family_events

    db = hub.db
    cid = await add_client(
        db, name="iPad Child One", owner_label="Child One", profile="child"
    )
    by = {"kind": "device", "client_id": str(uuid.uuid4()), "name": "Papa", "via": via}
    await family_events.log(
        db,
        family_events.RELEASE,
        client_id=cid,
        category="change",
        event_kind=family_events.FAMILY_CHANGE,
        entity_id="light.kitchen",
        action="allow",
        by=by,
    )
    await access_requests._log(
        db,
        "access_request_answered",
        client_id=cid,
        category="activity",
        event_kind=None,
        by=by,
    )
    rows = await db.fetchall(
        "SELECT route, origin FROM event_log WHERE client_id = ?", (cid,)
    )
    assert [(r["route"], r["origin"]) for r in rows] == [(route, None), (route, None)]
    listed = await _list(db, client_id=cid)
    for entry in listed["entries"]:
        assert _contract_errors("common/log-entry.json", entry) == []


@pytest.mark.parametrize(
    "result", ["ok", "unknown_tag", "tags_off", "not_visible", "rate_limited"]
)
def test_tag_scanned_ha_event_reason_stays_in_the_enum(result):
    """The tag result went into reason, which is a control_denial_reason."""
    entry = {
        "event_kind": "tag_scanned",
        "ts": "2026-11-23T10:00:00+00:00",
        "client_id": str(uuid.uuid4()),
        "reason": None if result == "ok" else result,
        "detail": {"tag_id": "a" * 22, "tag_name": "Tür", "tag_result": result},
    }
    data = event_log.ha_event_data(entry)
    assert data["result"] == result
    assert data["reason"] is None
    errors = _contract_errors(
        "ws/integration/ha_event.json", {"type": "ha_event", "data": data}
    )
    assert errors == []


def test_watch_removed_with_its_phone_says_so():
    """A watch removed because its phone was removed names the phone."""
    entry = {
        "event_kind": "device_removed",
        "kind": "device_removed",
        "device_name": "Mum's Watch",
        "detail": {"how": "with_phone", "with_device": {"name": "Mum's iPhone"}},
    }
    assert event_notify.message(entry, "en") == (
        "Device removed: Mum's Watch (with Mum's iPhone)"
    )
    assert event_notify.message(entry, "de") == (
        "Gerät entfernt: Mum's Watch (mit Mum's iPhone)"
    )
