"""Notifications addressed to a watch.

A watch never connects to the hub (watchOS allows no WebSocket, TN3135), so
a notification stored for it would wait forever. It goes to the phone that
set the watch up instead; the phone's system shows it on the watch while the
phone is locked. The answer names that phone (``forwarded_to``); without the
phone the call fails with 422. "all" leaves watches out, the handover of a
queued notification follows the same way.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone

import fastapi
import pytest

import api.integration as apiintg
import api.notifications as apinotif
import api.websocket as ws
from models.database import Database
from models.schemas import (
    HandoverNotification,
    IntegrationNotifyRequest,
    NotificationHandoverRequest,
    SendNotificationRequest,
)
from services.notifications import get_notification, notification_recipient

from .profile_helpers import add_client


def _request(db, auth="admin"):
    state = types.SimpleNamespace(db=db, ha_client=None, auth_type=auth)
    return types.SimpleNamespace(state=state, app=types.SimpleNamespace(state=state))


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def _send(client_id, message):
        sent.append((client_id, message))
        return True

    async def _broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "send_to_client", _send)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return sent


async def _phone_and_watch(db, **phone_columns):
    phone = await add_client(
        db, name="iPhone Air", platform="ios", last_seen=Database.now(), **phone_columns
    )
    watch = await add_client(
        db,
        name="Apple Watch",
        platform="watchos",
        set_up_by_client_id=phone,
        set_up_method="onboard",
    )
    return phone, watch


async def _stored(db):
    rows = await db.fetchall("SELECT id, client_id FROM client_notifications")
    return [(r["client_id"], r["id"]) for r in rows]


async def test_admin_notification_for_the_watch_goes_to_its_phone(db, pushes):
    phone, watch = await _phone_and_watch(db)
    result = await apinotif.send_notification(
        _request(db), SendNotificationRequest(client_id=watch, title="Tür")
    )
    assert result["forwarded_to"] == {"client_id": phone, "name": "iPhone Air"}
    assert result["notification"]["client_id"] == phone
    assert result["ws_sent"] is True
    assert [p[0] for p in pushes] == [phone]
    assert pushes[0][1]["command"] == "display_notification"
    assert await _stored(db) == [(phone, result["notification"]["id"])]


async def test_other_devices_are_not_forwarded(db, pushes):
    phone, _watch = await _phone_and_watch(db)
    result = await apinotif.send_notification(
        _request(db), SendNotificationRequest(client_id=phone, title="Hallo")
    )
    assert "forwarded_to" not in result
    assert result["notification"]["client_id"] == phone


@pytest.mark.parametrize("phone_state", ["deleted", "inactive", "pending_deletion"])
async def test_watch_without_its_phone_is_refused(db, pushes, phone_state):
    phone, watch = await _phone_and_watch(db)
    if phone_state == "deleted":
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (phone,))
    elif phone_state == "inactive":
        await db.execute(
            "UPDATE paired_clients SET is_active = 0 WHERE id = ?", (phone,)
        )
    else:
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ? WHERE id = ?",
            (Database.now(), phone),
        )
    with pytest.raises(fastapi.HTTPException) as exc:
        await apinotif.send_notification(
            _request(db), SendNotificationRequest(client_id=watch, title="Tür")
        )
    assert exc.value.status_code == 422
    assert "phone" in exc.value.detail
    assert pushes == []
    assert await _stored(db) == []


async def test_watch_set_up_by_nobody_is_refused(db):
    watch = await add_client(db, name="Watch", platform="wearos")
    row = await db.fetchone(
        "SELECT id, platform, set_up_by_client_id FROM paired_clients WHERE id = ?",
        (watch,),
    )
    assert await notification_recipient(db, dict(row)) is None


async def test_integration_retry_for_the_watch_is_a_duplicate(db, pushes):
    phone, watch = await _phone_and_watch(db)
    nid = str(uuid.uuid4())
    body = IntegrationNotifyRequest(client_id=watch, title="Hi", notification_id=nid)
    first = await apiintg.integration_notify(_request(db, "integration"), body)
    assert first["notification"]["id"] == nid
    assert first["forwarded_to"]["client_id"] == phone
    second = await apiintg.integration_notify(_request(db, "integration"), body)
    assert second["duplicate"] is True
    assert second["forwarded_to"]["client_id"] == phone
    assert len(pushes) == 1
    assert await _stored(db) == [(phone, nid)]


async def test_broadcast_leaves_watches_out(db, pushes):
    phone, _watch = await _phone_and_watch(db)
    tv = await add_client(db, name="Apple TV", platform="tvos")
    result = await apinotif.send_notification(
        _request(db), SendNotificationRequest(client_id="all", title="Alle")
    )
    assert result == {"sent": 2}
    assert {p[0] for p in pushes} == {phone, tv}


async def test_handover_for_the_watch_goes_to_its_phone(db, pushes):
    phone, watch = await _phone_and_watch(db)
    nid = str(uuid.uuid4())
    created_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    body = NotificationHandoverRequest(
        notifications=[
            HandoverNotification(
                notification_id=nid, client_id=watch, created_at=created_at, title="x"
            )
        ]
    )
    result = await apiintg.integration_notifications_handover(
        _request(db, "integration"), body
    )
    assert result["created"] == [nid] and result["skipped"] == []
    assert (await get_notification(db, nid))["client_id"] == phone
    # The same handover again is a duplicate, not an id conflict.
    again = await apiintg.integration_notifications_handover(
        _request(db, "integration"), body
    )
    assert again["duplicates"] == [nid] and again["skipped"] == []


async def test_handover_for_a_watch_without_phone_is_skipped(db, pushes):
    phone, watch = await _phone_and_watch(db)
    await db.execute("DELETE FROM paired_clients WHERE id = ?", (phone,))
    nid = str(uuid.uuid4())
    body = NotificationHandoverRequest(
        notifications=[
            HandoverNotification(
                notification_id=nid,
                client_id=watch,
                created_at=datetime.now(timezone.utc).isoformat(),
                title="x",
            )
        ]
    )
    result = await apiintg.integration_notifications_handover(
        _request(db, "integration"), body
    )
    assert result["skipped"] == [{"notification_id": nid, "reason": "client_not_found"}]
    assert await _stored(db) == []


async def test_status_names_the_platform_and_the_phone_of_a_watch(db):
    """The integration needs both while the hub is down: "all" leaves the
    watch out, a notification for it goes into the phone's sensor."""
    from api.pairing import clear_active_pins

    clear_active_pins()
    phone, watch = await _phone_and_watch(db)
    status = await apiintg.integration_status(_request(db, "integration"))
    clients = {c["id"]: c for c in status["clients"]}
    assert clients[watch]["platform"] == "watchos"
    assert clients[watch]["set_up_by"] == {
        "client_id": phone,
        "name": "",
        "method": "onboard",
    }
    assert clients[phone]["platform"] == "ios"
    assert clients[phone]["set_up_by"] is None
