"""Tests for the client->server notification_dismiss WS handler.

The app's X-button sends {"type":"notification_dismiss","notification_id":...}
so the notification is removed server-side (stays gone across reconnects),
like a web-UI delete. The handler:
- verifies the notification belongs to the authed client,
- calls services.notifications.dismiss_notification,
- replies notification_dismiss_ok,
- broadcasts the change to admin.
"""

import types

import pytest

from .profile_helpers import add_client

from api.websocket import _handle_message
from services.notifications import create_notification, get_notification


class _FakeWS:
    """Minimal WebSocket double: captures send_json + exposes app.state."""

    def __init__(self, db):
        self.sent = []
        state = types.SimpleNamespace(db=db, ha_client=None)
        self.app = types.SimpleNamespace(state=state)

    async def send_json(self, message):
        self.sent.append(message)


@pytest.mark.asyncio
async def test_dismiss_marks_notification_dismissed(db_with_client, monkeypatch):
    db, client_id, _, _ = db_with_client

    captured = []

    import api.websocket as ws

    async def _capture_broadcast(message, *a, **k):
        captured.append(message)

    monkeypatch.setattr(ws, "broadcast", _capture_broadcast)

    n = await create_notification(db, client_id=client_id, message="bye")
    wsk = _FakeWS(db)

    auth, cid = await _handle_message(
        wsk,
        {"type": "notification_dismiss", "notification_id": n["id"]},
        authenticated=True,
        client_id=client_id,
    )
    assert (auth, cid) == (True, client_id)

    # Reply confirms dismissal.
    assert wsk.sent[-1] == {
        "type": "notification_dismiss_ok",
        "notification_id": n["id"],
    }

    # Persisted as dismissed.
    fetched = await get_notification(db, n["id"])
    assert fetched["status"] == "dismissed"
    assert fetched["dismissed_at"] is not None

    # Admin broadcast emitted.
    assert any(m.get("type") == "notification_dismissed" for m in captured)
    dm = next(m for m in captured if m.get("type") == "notification_dismissed")
    assert dm["data"]["notification_id"] == n["id"]
    assert dm["data"]["client_id"] == client_id


@pytest.mark.asyncio
async def test_dismiss_requires_notification_id(db_with_client):
    db, client_id, _, _ = db_with_client
    wsk = _FakeWS(db)
    await _handle_message(
        wsk,
        {"type": "notification_dismiss"},
        authenticated=True,
        client_id=client_id,
    )
    assert wsk.sent[-1]["type"] == "error"
    assert "notification_id" in wsk.sent[-1]["message"]


@pytest.mark.asyncio
async def test_dismiss_rejects_ingress(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(db, client_id=client_id, message="x")
    wsk = _FakeWS(db)
    await _handle_message(
        wsk,
        {"type": "notification_dismiss", "notification_id": n["id"]},
        authenticated=True,
        client_id="ingress",
    )
    assert wsk.sent[-1]["type"] == "error"
    # Still active (not dismissed).
    fetched = await get_notification(db, n["id"])
    assert fetched["status"] == "pending"


@pytest.mark.asyncio
async def test_dismiss_ownership_check(db_with_client, monkeypatch):
    """A client cannot dismiss another client's notification."""
    db, client_id, _, _ = db_with_client

    # Second client owns the notification.
    import hashlib

    other_id = await add_client(
        db, name="Other", secret_hash=hashlib.sha256(b"s").hexdigest()
    )
    n = await create_notification(db, client_id=other_id, message="not yours")

    wsk = _FakeWS(db)
    await _handle_message(
        wsk,
        {"type": "notification_dismiss", "notification_id": n["id"]},
        authenticated=True,
        client_id=client_id,  # different client
    )
    assert wsk.sent[-1]["type"] == "error"
    assert "not found" in wsk.sent[-1]["message"].lower()

    # Still active.
    fetched = await get_notification(db, n["id"])
    assert fetched["status"] == "pending"


@pytest.mark.asyncio
async def test_dismiss_unknown_notification(db_with_client):
    db, client_id, _, _ = db_with_client
    wsk = _FakeWS(db)
    await _handle_message(
        wsk,
        {"type": "notification_dismiss", "notification_id": "does-not-exist"},
        authenticated=True,
        client_id=client_id,
    )
    assert wsk.sent[-1]["type"] == "error"
