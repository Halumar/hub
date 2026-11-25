"""Tests for popup_mode "direct" and client_id="all" broadcast.

Covers the shared addon<->app contract additions:
- popup_mode gains a 4th value "direct": the app immediately OPENS the linked
  area/entity/url when the notification is displayed and consumes it (one-time,
  no card/popup). It REQUIRES an action -> popup_mode="direct" with
  action_type=="none" (or no action_target) is rejected with HTTP 400.
- send_notification accepts client_id="all" to deliver the notification to
  EVERY active paired client (one notification row per active client), returning
  a summary {"sent": N}.
"""

import types

import pytest

from .profile_helpers import add_client
from fastapi import HTTPException

import api.notifications as apinotif
import api.websocket as ws
from models.database import Database
from models.schemas import SendNotificationRequest
from services.notifications import (
    canonical_popup_mode,
    create_notification,
    get_client_notifications,
)


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_canonical_popup_mode_accepts_direct():
    assert canonical_popup_mode("direct") == "direct"
    assert canonical_popup_mode("DIRECT") == "direct"


def test_schema_accepts_direct_popup_mode():
    assert (
        SendNotificationRequest(
            client_id="c1", title="x", popup_mode="direct"
        ).popup_mode
        == "direct"
    )


@pytest.mark.asyncio
async def test_create_notification_persists_direct(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db,
        client_id=client_id,
        title="x",
        popup_mode="direct",
        action_type="area",
        action_target="living_room",
    )
    assert n["popup_mode"] == "direct"
    # direct is not "none", so the legacy popup bool is True.
    assert n["popup"] is True


# ── helpers for endpoint tests ───────────────────────────────────────────────


def _request(db):
    state = types.SimpleNamespace(db=db, ha_client=None, auth_type="admin")
    app = types.SimpleNamespace(state=state)
    return types.SimpleNamespace(state=state, app=app)


@pytest.fixture
def patch_ws(monkeypatch):
    """Patch the websocket send/broadcast used by the notifications API."""
    sent: list = []

    async def _send_to_client(client_id, message):
        sent.append((client_id, message))
        return True

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "send_to_client", _send_to_client)
    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)
    return sent


# ── direct requires an action ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_direct_without_action_rejected(db_with_client, patch_ws):
    db, client_id, _, _ = db_with_client
    body = SendNotificationRequest(
        client_id=client_id, title="open it", popup_mode="direct"
    )
    with pytest.raises(HTTPException) as exc:
        await apinotif.send_notification(_request(db), body)
    assert exc.value.status_code == 400
    assert "direct" in exc.value.detail
    # Nothing was created/delivered.
    assert await get_client_notifications(db, client_id) == []


@pytest.mark.asyncio
async def test_direct_with_action_type_but_no_target_rejected(db_with_client, patch_ws):
    db, client_id, _, _ = db_with_client
    body = SendNotificationRequest(
        client_id=client_id,
        title="open it",
        popup_mode="direct",
        action_type="area",
        action_target=None,
    )
    with pytest.raises(HTTPException) as exc:
        await apinotif.send_notification(_request(db), body)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_direct_with_action_ok(db_with_client, patch_ws):
    db, client_id, _, _ = db_with_client
    body = SendNotificationRequest(
        client_id=client_id,
        title="open it",
        popup_mode="direct",
        action_type="area",
        action_target="living_room",
    )
    result = await apinotif.send_notification(_request(db), body)
    assert result["notification"]["popup_mode"] == "direct"
    assert result["notification"]["action_type"] == "area"
    assert result["notification"]["action_target"] == "living_room"
    # Delivered via WS with popup_mode "direct".
    assert patch_ws[0][1]["popup_mode"] == "direct"


# ── client_id="all" broadcast ────────────────────────────────────────────────


async def _add_client(db, name, is_active=1):
    return await add_client(
        db, name=name, is_active=is_active, last_seen=Database.now()
    )


@pytest.mark.asyncio
async def test_broadcast_all_creates_one_per_active_client(db_with_client, patch_ws):
    db, client_id, _, _ = db_with_client
    cid2 = await _add_client(db, "Second")
    cid3 = await _add_client(db, "Third")
    inactive = await _add_client(db, "Inactive", is_active=0)

    body = SendNotificationRequest(client_id="all", title="hello all")
    result = await apinotif.send_notification(_request(db), body)

    # Three active clients -> three deliveries.
    assert result == {"sent": 3}
    for cid in (client_id, cid2, cid3):
        notifs = await get_client_notifications(db, cid)
        assert len(notifs) == 1
        assert notifs[0]["title"] == "hello all"
    # Inactive client got nothing.
    assert await get_client_notifications(db, inactive) == []
    # WS push happened for each active client.
    assert {c for c, _ in patch_ws} == {client_id, cid2, cid3}


@pytest.mark.asyncio
async def test_broadcast_all_with_direct_requires_action(db_with_client, patch_ws):
    db, client_id, _, _ = db_with_client
    await _add_client(db, "Second")
    body = SendNotificationRequest(client_id="all", title="x", popup_mode="direct")
    with pytest.raises(HTTPException) as exc:
        await apinotif.send_notification(_request(db), body)
    assert exc.value.status_code == 400
    # No notifications created for anyone.
    assert await get_client_notifications(db, client_id) == []
