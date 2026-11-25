"""Tests for the notification presentation extension fields.

Covers the shared hub <-> app contract fields:
- priority (int 0..100, default 50, clamped)
- color (one of none/red/blue/green/yellow/orange, default none)
- action_type (none/area/entity/url, default none) + action_target
- created_at (server send-time) present in payloads

And that all of these are surfaced in:
- create_notification + normalize_notification_row
- the notifications list / per-client readers
- the display_notification ws_message (REST + integration)
- the per-client notification sensor items (full content)
"""

import types

import pytest

from models.schemas import SendNotificationRequest
from services.notifications import (
    canonical_action_type,
    canonical_color,
    clamp_priority,
    create_notification,
    get_client_notifications,
    get_notification,
    normalize_notification_row,
)


# ── helpers / pure functions ────────────────────────────────────────────────


def test_clamp_priority():
    assert clamp_priority(50) == 50
    assert clamp_priority(0) == 0
    assert clamp_priority(100) == 100
    assert clamp_priority(-5) == 0
    assert clamp_priority(9999) == 100
    assert clamp_priority(None) == 50
    assert clamp_priority("garbage") == 50
    assert clamp_priority("7") == 7


def test_canonical_color():
    for c in ("none", "red", "blue", "green", "yellow", "orange"):
        assert canonical_color(c) == c
    assert canonical_color("RED") == "red"
    assert canonical_color("purple") == "none"
    assert canonical_color(None) == "none"
    assert canonical_color("") == "none"


def test_canonical_action_type():
    for a in ("none", "area", "entity", "url"):
        assert canonical_action_type(a) == a
    assert canonical_action_type("AREA") == "area"
    assert canonical_action_type("bogus") == "none"
    assert canonical_action_type(None) == "none"


# ── create + persistence + normalization ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_defaults(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(db, client_id=client_id, message="hi")
    assert n["priority"] == 50
    assert n["color"] == "none"
    assert n["action_type"] == "none"
    assert n["action_target"] is None
    assert n["created_at"]  # send-time present

    fetched = await get_notification(db, n["id"])
    assert fetched["priority"] == 50
    assert fetched["color"] == "none"
    assert fetched["action_type"] == "none"
    assert fetched["action_target"] is None
    assert fetched["created_at"] == n["created_at"]


@pytest.mark.asyncio
async def test_create_custom_fields_persist(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db,
        client_id=client_id,
        message="tap me",
        priority=0,
        color="red",
        action_type="entity",
        action_target="light.kitchen",
    )
    assert n["priority"] == 0
    assert n["color"] == "red"
    assert n["action_type"] == "entity"
    assert n["action_target"] == "light.kitchen"

    fetched = await get_notification(db, n["id"])
    assert fetched["priority"] == 0
    assert fetched["color"] == "red"
    assert fetched["action_type"] == "entity"
    assert fetched["action_target"] == "light.kitchen"


@pytest.mark.asyncio
async def test_create_clamps_and_normalizes(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db,
        client_id=client_id,
        message="x",
        priority=500,
        color="chartreuse",
        action_type="teleport",
    )
    assert n["priority"] == 100
    assert n["color"] == "none"
    assert n["action_type"] == "none"


def test_normalize_row_supplies_defaults_for_legacy_rows():
    # A row stored without the presentation columns (keys absent).
    row = {"notification_type": "default"}
    out = normalize_notification_row(row)
    assert out["priority"] == 50
    assert out["color"] == "none"
    assert out["action_type"] == "none"
    assert out["action_target"] is None


@pytest.mark.asyncio
async def test_legacy_db_row_reads_back_defaults(db_with_client):
    """A row inserted without the presentation columns reads back with defaults."""
    db, client_id, _, _ = db_with_client
    now = db.now()
    await db.execute(
        """INSERT INTO client_notifications
           (id, client_id, title, message, notification_type, status, created_at)
           VALUES ('legacy-fields', ?, '', 'old', 'default', 'pending', ?)""",
        (client_id, now),
    )
    await db.commit()
    fetched = await get_notification(db, "legacy-fields")
    # Column defaults (priority=50, color='none', action_type='none') apply.
    assert fetched["priority"] == 50
    assert fetched["color"] == "none"
    assert fetched["action_type"] == "none"
    assert fetched["action_target"] is None


@pytest.mark.asyncio
async def test_fields_in_pending_payload(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db,
        client_id=client_id,
        message="later",
        priority=10,
        color="blue",
        action_type="url",
        action_target="https://example.com",
    )
    pending = await get_client_notifications(db, client_id, active_only=True)
    assert len(pending) == 1
    item = pending[0]
    assert item["priority"] == 10
    assert item["color"] == "blue"
    assert item["action_type"] == "url"
    assert item["action_target"] == "https://example.com"
    assert item["created_at"] == n["created_at"]
    assert {"priority", "color", "action_type", "action_target", "created_at"}.issubset(
        item.keys()
    )


# ── schema validation ───────────────────────────────────────────────────────


def test_send_schema_new_field_defaults():
    req = SendNotificationRequest(client_id="c1", message="x")
    assert req.priority == 50
    assert req.color == "none"
    assert req.action_type == "none"
    assert req.action_target is None


def test_send_schema_accepts_valid_values():
    req = SendNotificationRequest(
        client_id="c1",
        message="x",
        priority=0,
        color="orange",
        action_type="area",
        action_target="area-123",
    )
    assert req.priority == 0
    assert req.color == "orange"
    assert req.action_type == "area"
    assert req.action_target == "area-123"


def test_send_schema_rejects_out_of_range_priority():
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", message="x", priority=101)
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", message="x", priority=-1)


def test_send_schema_rejects_bad_color_and_action():
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", message="x", color="purple")
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", message="x", action_type="teleport")


# ── surfaced in the display_notification ws_message (REST + integration) ─────


class _FakeWS:
    """Captures the single message sent to the connected client."""

    def __init__(self):
        self.sent = None

    async def __call__(self, client_id, message):  # send_to_client signature
        self.sent = message
        return True


@pytest.mark.asyncio
async def test_rest_send_ws_message_has_fields(db_with_client, monkeypatch):
    db, client_id, _, _ = db_with_client
    import api.notifications as apinotif
    import api.websocket as ws

    fake_send = _FakeWS()
    monkeypatch.setattr(ws, "send_to_client", fake_send)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)

    state = types.SimpleNamespace(db=db, ha_client=None, auth_type="admin")
    app = types.SimpleNamespace(state=state)
    request = types.SimpleNamespace(state=state, app=app)

    body = SendNotificationRequest(
        client_id=client_id,
        message="hello",
        priority=3,
        color="yellow",
        action_type="url",
        action_target="https://x.test",
    )
    result = await apinotif.send_notification(request, body)

    msg = fake_send.sent
    assert msg["command"] == "display_notification"
    assert msg["priority"] == 3
    assert msg["color"] == "yellow"
    assert msg["action_type"] == "url"
    assert msg["action_target"] == "https://x.test"
    assert msg["created_at"] == result["notification"]["created_at"]


@pytest.mark.asyncio
async def test_integration_send_ws_message_has_fields(db_with_client, monkeypatch):
    db, client_id, _, _ = db_with_client
    import api.integration as apiintg
    import api.websocket as ws

    fake_send = _FakeWS()
    monkeypatch.setattr(ws, "send_to_client", fake_send)

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)

    state = types.SimpleNamespace(db=db, ha_client=None, auth_type="integration")
    app = types.SimpleNamespace(state=state)
    request = types.SimpleNamespace(state=state, app=app)

    body = SendNotificationRequest(
        client_id=client_id,
        message="hi",
        priority=7,
        color="green",
        action_type="area",
        action_target="area-9",
    )
    await apiintg.integration_notify(request, body)

    msg = fake_send.sent
    assert msg["priority"] == 7
    assert msg["color"] == "green"
    assert msg["action_type"] == "area"
    assert msg["action_target"] == "area-9"
    assert msg["created_at"]


# ── the per-client notification sensor carries the fields ────────────────────
#
# The HA sensor is the REMOTE delivery path and carries the full push-shaped
# items (readable by any HA token, by design); the presentation fields must
# survive the sensor projection.


@pytest.mark.asyncio
async def test_sensor_payload_carries_fields(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db,
        client_id=client_id,
        message="x",
        priority=2,
        color="red",
        action_type="entity",
        action_target="light.x",
    )

    captured = {}

    class _FakeHAClient:
        async def _post(self, url, payload):
            captured["payload"] = payload

        async def _delete(self, url):
            pass

    from services.ha_sensors import sync_client_notification_sensors

    await sync_client_notification_sensors(db, _FakeHAClient())

    attrs = captured["payload"]["attributes"]
    assert attrs["count"] == 1
    # Ids live inside the push-shaped items (no notification_ids attribute).
    assert "notification_ids" not in attrs
    item = attrs["notifications"][0]
    assert item["notification_id"] == n["id"]
    assert item["message"] == "x"
    assert item["priority"] == 2
    assert item["color"] == "red"
    assert item["action_type"] == "entity"
    assert item["action_target"] == "light.x"
    assert item["created_at"] == n["created_at"]


# ── surfaced in the admin notifications list ─────────────────────────────────


@pytest.mark.asyncio
async def test_admin_list_has_fields(db_with_client, monkeypatch):
    db, client_id, _, _ = db_with_client
    await create_notification(
        db,
        client_id=client_id,
        message="x",
        priority=4,
        color="blue",
        action_type="url",
        action_target="https://y.test",
    )

    import api.notifications as apinotif

    state = types.SimpleNamespace(db=db, auth_type="admin")
    app = types.SimpleNamespace(state=state)
    request = types.SimpleNamespace(state=state, app=app)

    result = await apinotif.list_notifications(request, status="active")
    items = result["notifications"]
    assert len(items) == 1
    item = items[0]
    assert item["priority"] == 4
    assert item["color"] == "blue"
    assert item["action_type"] == "url"
    assert item["action_target"] == "https://y.test"
    assert item["created_at"]
