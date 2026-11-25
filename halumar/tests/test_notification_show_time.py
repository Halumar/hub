"""Tests for the PER-NOTIFICATION show_time flag.

"Show send time" is a per-notification field
(client_notifications.show_time, default 1/True), not a client-config setting. It
flows through create_notification, normalize_notification_row, the schemas,
and the display_notification WS payload + sensor payload.

There is no global show-time setting: no /settings/notification-show-time
route and no value in the client config.

Covers:
- show_time defaults to True on the schemas and create_notification.
- A per-notification False is persisted and surfaced.
- show_time is absent from _get_client_config.
"""

import pytest

from api.websocket import _get_client_config
from models.schemas import SendNotificationRequest
from services.notifications import (
    create_notification,
    get_notification,
    normalize_notification_row,
)


# ── schema defaults ──────────────────────────────────────────────────────────


def test_send_schema_defaults_show_time_true():
    body = SendNotificationRequest(client_id="c1", title="hi")
    assert body.show_time is True


def test_schema_accepts_show_time_false():
    body = SendNotificationRequest(client_id="c1", title="hi", show_time=False)
    assert body.show_time is False


# ── create_notification + normalize ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_default_show_time_true(db_with_client):
    db, client_id, _secret, _hash = db_with_client
    n = await create_notification(db, client_id=client_id, title="hi")
    assert n["show_time"] is True
    # Persisted and re-read.
    stored = await get_notification(db, n["id"])
    assert stored["show_time"] is True


@pytest.mark.asyncio
async def test_create_show_time_false_persists(db_with_client):
    db, client_id, _secret, _hash = db_with_client
    n = await create_notification(db, client_id=client_id, title="hi", show_time=False)
    assert n["show_time"] is False
    stored = await get_notification(db, n["id"])
    assert stored["show_time"] is False


def test_normalize_legacy_row_defaults_true():
    """A row predating the column (no show_time key) defaults to True."""
    row = {"id": "x", "notification_type": "default"}
    d = normalize_notification_row(row)
    assert d["show_time"] is True


def test_normalize_zero_is_false():
    row = {"id": "x", "notification_type": "default", "show_time": 0}
    d = normalize_notification_row(row)
    assert d["show_time"] is False


# ── WS display payload includes show_time ────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_ws_message_includes_show_time(db_with_client, monkeypatch):
    import api.notifications as notifications_api

    captured = {}

    async def _fake_send(client_id, message):
        captured["msg"] = message
        return True

    async def _noop_broadcast(*a, **k):
        return None

    import api.websocket as ws

    monkeypatch.setattr(ws, "send_to_client", _fake_send)
    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)

    db, client_id, _secret, _hash = db_with_client
    client = {"id": client_id, "name": "Test Client"}
    await notifications_api._deliver_notification(
        db,
        client,
        title="hi",
        message="",
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
        show_time=False,
    )
    assert "show_time" in captured["msg"]
    assert captured["msg"]["show_time"] is False


# ── no global flag in the client config ──────────────────────────────────────


@pytest.mark.asyncio
async def test_show_time_absent_from_ingress_config(db):
    config = await _get_client_config(db, "ingress")
    assert "notification_show_time" not in config


@pytest.mark.asyncio
async def test_show_time_absent_from_paired_config(db_with_client):
    db, client_id, _secret, _hash = db_with_client
    config = await _get_client_config(db, client_id)
    assert "notification_show_time" not in config


# ── no global route ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
def test_no_global_route():
    """No global show-time setting (per notification only)."""
    import api.pairing as pairing

    assert not hasattr(pairing, "get_notification_show_time_setting")
