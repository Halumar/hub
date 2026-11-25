"""Bounds of a device's active notifications.

The HA sensor (the app's way away from home) and the hub socket show the same
set: the newest 50 active notifications; one that is not sticky ends after
72 h. Dismissed ones are announced to the device and the admin UIs.
"""

from datetime import datetime, timedelta, timezone

import pytest

import api.notifications as apinotif
import api.websocket as ws
import services.ha_sensors as hs
from services.notifications import (
    ACTIVE_MAX_PER_CLIENT,
    expire_active_notifications,
    get_client_notifications,
    get_notification,
)

from .profile_helpers import add_client


class _HA:
    """Keeps posted states like HA does."""

    def __init__(self):
        self.states = {}

    async def _post(self, url, data=None):
        eid = url.rsplit("/", 1)[-1]
        self.states[eid] = dict(data, entity_id=eid)
        return data

    async def _delete(self, url):
        self.states.pop(url.rsplit("/", 1)[-1], None)


@pytest.fixture
def pushes(monkeypatch):
    log = []

    async def _send(client_id, message):
        log.append((client_id, message))
        return True

    async def _broadcast(message, *a, **k):
        log.append(("*", message))

    monkeypatch.setattr(ws, "send_to_client", _send)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return log


async def _send(db, client, i, **kw):
    fields = dict(
        title=f"Door {i}",
        message="opened " * 10,
        notification_type="default",
        icon=None,
        icon_svg=None,
        popup=False,
        popup_mode=None,
        scheduled_at=None,
        priority=50,
        color="none",
        action_type="none",
        action_target=None,
    )
    fields.update(kw)
    n, _ = await apinotif._deliver_notification(db, client, **fields)
    return n


async def test_sensor_and_socket_show_the_same_newest_50(db, pushes):
    cid = await add_client(db, name="Tablet")
    client = {"id": cid, "name": "Tablet"}
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    made = []
    for i in range(ACTIVE_MAX_PER_CLIENT + 10):
        at = (base + timedelta(seconds=i)).isoformat()
        made.append((await _send(db, client, i, created_at=at))["id"])
    ha = _HA()
    await hs.sync_client_notification_sensors(db, ha)
    sensor = ha.states[hs.client_notification_sensor_id("Tablet", cid)]
    in_sensor = [n["notification_id"] for n in sensor["attributes"]["notifications"]]
    on_socket = [n["id"] for n in await get_client_notifications(db, cid)]
    newest = list(reversed(made))[:ACTIVE_MAX_PER_CLIENT]
    assert in_sensor == on_socket == newest
    assert sensor["state"] == str(ACTIVE_MAX_PER_CLIENT)
    # the ten oldest are dismissed, and the device and admin UIs are told
    for nid in made[:10]:
        assert (await get_notification(db, nid))["status"] == "dismissed"
    told = {
        m["notification_id"]
        for c, m in pushes
        if c == cid and m.get("command") == "dismiss_notification"
    }
    assert told == set(made[:10])
    admin = {
        m["data"]["notification_id"]
        for c, m in pushes
        if c == "*" and m.get("type") == "notification_dismissed"
    }
    assert admin == set(made[:10])


async def test_old_notifications_end_after_72_hours_sticky_stays(db, pushes):
    cid = await add_client(db, name="Phone")
    client = {"id": cid, "name": "Phone"}
    old = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=71)).isoformat()
    gone = await _send(db, client, 1, created_at=old)
    sticky = await _send(db, client, 2, created_at=old, notification_type="sticky")
    kept = await _send(db, client, 3, created_at=fresh)
    expired = await expire_active_notifications(db)
    assert expired == {cid: [gone["id"]]}
    active = {n["id"] for n in await get_client_notifications(db, cid)}
    assert active == {sticky["id"], kept["id"]}
    assert await expire_active_notifications(db) == {}


async def test_cleanup_loop_tells_the_device_and_syncs_the_sensor(
    db, pushes, monkeypatch
):
    import asyncio

    import main

    cid = await add_client(db, name="Phone")
    old = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    n = await _send(db, {"id": cid, "name": "Phone"}, 1, created_at=old)
    ha = _HA()
    await hs.sync_client_notification_sensors(db, ha)
    sensor = hs.client_notification_sensor_id("Phone", cid)
    assert ha.states[sensor]["state"] == "1"
    pushes.clear()

    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._notification_cleanup_loop(db, ha)
    assert ha.states[sensor]["state"] == "0"
    assert (
        cid,
        {
            "type": "command",
            "command": "dismiss_notification",
            "notification_id": n["id"],
        },
    ) in pushes


async def test_a_scheduled_reminder_is_not_expired_before_it_is_due(db, pushes):
    """The 72 h count from scheduled_at when that is later."""
    cid = await add_client(db, name="Phone")
    client = {"id": cid, "name": "Phone"}
    now = datetime.now(timezone.utc)
    created = (now - timedelta(hours=73)).isoformat()
    later = await _send(
        db,
        client,
        1,
        created_at=created,
        scheduled_at=(now + timedelta(days=1)).isoformat(),
    )
    due_long_ago = await _send(
        db,
        client,
        2,
        created_at=created,
        scheduled_at=(now - timedelta(hours=73)).isoformat(),
    )
    due_lately = await _send(
        db,
        client,
        3,
        created_at=created,
        scheduled_at=(now - timedelta(hours=10)).isoformat(),
    )
    assert await expire_active_notifications(db) == {cid: [due_long_ago["id"]]}
    active = {n["id"] for n in await get_client_notifications(db, cid)}
    assert active == {later["id"], due_lately["id"]}


async def test_the_cap_keeps_sticky_and_high_priority(db, pushes):
    """A sticky priority-100 alarm is not pushed out by 50 low ones;
    a priority-90 notice outlives newer priority-0 ones."""
    cid = await add_client(db, name="Tablet")
    client = {"id": cid, "name": "Tablet"}
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    alarm = await _send(
        db,
        client,
        0,
        created_at=base.isoformat(),
        notification_type="sticky",
        priority=100,
    )
    high = await _send(
        db, client, 1, created_at=(base + timedelta(seconds=1)).isoformat(), priority=90
    )
    low = []
    for i in range(2, 52):
        at = (base + timedelta(seconds=i)).isoformat()
        low.append((await _send(db, client, i, created_at=at, priority=0))["id"])
    for keep in (alarm, high):
        assert (await get_notification(db, keep["id"]))["status"] != "dismissed"
    # the two oldest low ones went
    assert [(await get_notification(db, n))["status"] for n in low[:2]] == [
        "dismissed",
        "dismissed",
    ]
    listed = [n["id"] for n in await get_client_notifications(db, cid)]
    assert len(listed) == ACTIVE_MAX_PER_CLIENT
    assert listed[0] == low[-1] and listed[-1] == alarm["id"]  # newest first
    ha = _HA()
    await hs.sync_client_notification_sensors(db, ha)
    sensor = ha.states[hs.client_notification_sensor_id("Tablet", cid)]
    assert [
        n["notification_id"] for n in sensor["attributes"]["notifications"]
    ] == listed


async def test_items_waiting_for_handover_keep_the_sensor_bounded(
    db, pushes, monkeypatch
):
    """Carried items of the integration count towards the 50."""
    cid = await add_client(db, name="Tablet")
    client = {"id": cid, "name": "Tablet"}
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(ACTIVE_MAX_PER_CLIENT):
        await _send(db, client, i, created_at=(base + timedelta(seconds=i)).isoformat())
    carried = [
        {
            "notification_id": f"00000000-0000-4000-8000-{i:012d}",
            "title": f"queued {i}",
            "notification_type": "default",
            "priority": 50,
            "created_at": (base + timedelta(hours=1, seconds=i)).isoformat(),
        }
        for i in range(5)
    ]

    async def _carried(db, ha_client, sensor_id):
        return carried

    monkeypatch.setattr(hs, "_pending_handover_items", _carried)
    ha = _HA()
    await hs.sync_client_notification_sensors(db, ha)
    attrs = ha.states[hs.client_notification_sensor_id("Tablet", cid)]["attributes"]
    assert attrs["count"] == len(attrs["notifications"]) == ACTIVE_MAX_PER_CLIENT
    ids = [n["notification_id"] for n in attrs["notifications"]]
    assert ids[:5] == [c["notification_id"] for c in reversed(carried)]
    assert attrs["pending_handover"] == [c["notification_id"] for c in carried]


async def test_first_cleanup_runs_shortly_after_the_start(db, monkeypatch):
    import asyncio

    import main

    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(main.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await main._notification_cleanup_loop(db, None)
    assert sleeps[0] <= 60 and sleeps[1] == 3600


async def test_get_notifications_is_the_whole_active_set(db, pushes):
    """A device that missed the dismiss (offline, no HA sensor) can
    reconcile with get_notifications: the answer is every active
    notification of the device, so a card missing from it is gone."""
    from api.websocket import _handle_message

    from .test_sensor_privacy import FakeWebSocket

    cid = await add_client(db, name="Kid")
    client = {"id": cid, "name": "Kid"}
    now = datetime.now(timezone.utc)
    old = await _send(db, client, 0, created_at=(now - timedelta(hours=80)).isoformat())
    kept = []
    for i in range(1, ACTIVE_MAX_PER_CLIENT + 3):
        at = (now - timedelta(minutes=60 - i)).isoformat()
        kept.append((await _send(db, client, i, created_at=at))["id"])
    await expire_active_notifications(db)
    active = await db.fetchall(
        "SELECT id FROM client_notifications WHERE client_id = ? AND status != 'dismissed'",
        (cid,),
    )
    sock = FakeWebSocket(db)
    await _handle_message(
        sock, {"type": "get_notifications"}, authenticated=True, client_id=cid
    )
    listed = [n["notification_id"] for n in sock.sent[-1]["notifications"]]
    assert set(listed) == {r["id"] for r in active}
    assert len(listed) == ACTIVE_MAX_PER_CLIENT
    assert old["id"] not in listed and kept[:3][0] not in listed
