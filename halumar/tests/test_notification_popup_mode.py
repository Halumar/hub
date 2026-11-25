"""Tests for the optional ``message`` and the ``popup_mode`` field.

Covers the shared addon<->app contract changes:
- message is OPTIONAL (only title is required); None/"" both accepted.
- popup_mode: "none" | "popup" | "both", DEFAULT "none".
    none  => inline banner only
    popup => centered modal only
    both  => inline banner + centered modal
- The legacy ``popup`` bool is kept for back-compat:
    * emitted as (popup_mode != "none")
    * accepted on input (true -> "popup") when popup_mode is unset.

And that popup_mode is surfaced in:
- create_notification + normalize_notification_row
- the notifications list / per-client readers
- the display_notification ws_message (REST + integration)
- the per-client notification sensor items (full content)
"""

import types

import pytest

from models.schemas import SendNotificationRequest
from services.notifications import (
    canonical_popup_mode,
    create_notification,
    get_client_notifications,
    get_notification,
    normalize_notification_row,
    resolve_popup_mode,
)


# ── pure helpers ─────────────────────────────────────────────────────────────


def test_canonical_popup_mode():
    for m in ("none", "popup", "both"):
        assert canonical_popup_mode(m) == m
    assert canonical_popup_mode("POPUP") == "popup"
    assert canonical_popup_mode("Both") == "both"
    assert canonical_popup_mode("bogus") == "none"
    assert canonical_popup_mode(None) == "none"
    assert canonical_popup_mode("") == "none"


def test_resolve_popup_mode_legacy_bool_maps():
    # Explicit popup_mode wins.
    assert resolve_popup_mode("both", popup=False) == "both"
    assert resolve_popup_mode("popup", popup=False) == "popup"
    # Unset popup_mode -> derive from legacy bool.
    assert resolve_popup_mode(None, popup=True) == "popup"
    assert resolve_popup_mode(None, popup=False) == "none"
    # Default "none" + legacy popup=true is still upgraded to "popup".
    assert resolve_popup_mode("none", popup=True) == "popup"


# ── create + persistence + normalization ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_defaults_popup_mode_none(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(db, client_id=client_id, title="hi")
    assert n["popup_mode"] == "none"
    assert n["popup"] is False

    fetched = await get_notification(db, n["id"])
    assert fetched["popup_mode"] == "none"
    assert fetched["popup"] is False


@pytest.mark.asyncio
async def test_create_message_optional(db_with_client):
    db, client_id, _, _ = db_with_client
    # title only, no message -> message coerced to "".
    n = await create_notification(db, client_id=client_id, title="title only")
    assert n["message"] == ""
    # explicit None message also accepted.
    n2 = await create_notification(db, client_id=client_id, title="t", message=None)
    assert n2["message"] == ""

    fetched = await get_notification(db, n["id"])
    assert fetched["title"] == "title only"
    assert fetched["message"] == ""


@pytest.mark.asyncio
async def test_create_popup_mode_persists(db_with_client):
    db, client_id, _, _ = db_with_client
    for mode, expect_popup in (("none", False), ("popup", True), ("both", True)):
        n = await create_notification(
            db, client_id=client_id, title="x", popup_mode=mode
        )
        assert n["popup_mode"] == mode
        assert n["popup"] is expect_popup
        fetched = await get_notification(db, n["id"])
        assert fetched["popup_mode"] == mode
        assert fetched["popup"] is expect_popup


@pytest.mark.asyncio
async def test_create_legacy_popup_bool_maps(db_with_client):
    db, client_id, _, _ = db_with_client
    # Legacy caller: popup=True, no popup_mode -> "popup".
    n = await create_notification(db, client_id=client_id, title="x", popup=True)
    assert n["popup_mode"] == "popup"
    assert n["popup"] is True
    # Explicit popup_mode wins over the legacy bool.
    n2 = await create_notification(
        db, client_id=client_id, title="x", popup=True, popup_mode="both"
    )
    assert n2["popup_mode"] == "both"


@pytest.mark.asyncio
async def test_create_invalid_popup_mode_falls_back(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db, client_id=client_id, title="x", popup_mode="teleport"
    )
    assert n["popup_mode"] == "none"
    assert n["popup"] is False


def test_normalize_row_legacy_derives_popup_mode():
    # Legacy row predating the column: popup bool present, popup_mode absent.
    assert normalize_notification_row({"popup": 1})["popup_mode"] == "popup"
    assert normalize_notification_row({"popup": 0})["popup_mode"] == "none"
    # popup_mode present wins and re-derives the popup bool.
    out = normalize_notification_row({"popup_mode": "both", "popup": 0})
    assert out["popup_mode"] == "both"
    assert out["popup"] is True


@pytest.mark.asyncio
async def test_legacy_db_row_reads_back_popup_mode(db_with_client):
    """A row inserted without popup_mode reads back a derived value."""
    db, client_id, _, _ = db_with_client
    now = db.now()
    await db.execute(
        """INSERT INTO client_notifications
           (id, client_id, title, message, notification_type, popup,
            status, created_at)
           VALUES ('legacy-popup', ?, 't', '', 'default', 1, 'pending', ?)""",
        (client_id, now),
    )
    await db.commit()
    fetched = await get_notification(db, "legacy-popup")
    assert fetched["popup_mode"] == "popup"
    assert fetched["popup"] is True


@pytest.mark.asyncio
async def test_popup_mode_in_pending_payload(db_with_client):
    db, client_id, _, _ = db_with_client
    await create_notification(db, client_id=client_id, title="x", popup_mode="both")
    pending = await get_client_notifications(db, client_id, active_only=True)
    assert len(pending) == 1
    assert pending[0]["popup_mode"] == "both"
    assert "popup_mode" in pending[0]


# ── schema validation ───────────────────────────────────────────────────────


def test_send_schema_message_optional_and_popup_mode_default():
    req = SendNotificationRequest(client_id="c1", title="x")
    assert req.message is None
    assert req.popup_mode is None  # unset -> derive from legacy bool downstream


def test_schema_accepts_valid_popup_modes():
    for mode in ("none", "popup", "both"):
        assert (
            SendNotificationRequest(
                client_id="c1", title="x", popup_mode=mode
            ).popup_mode
            == mode
        )


def test_schema_rejects_bad_popup_mode():
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", title="x", popup_mode="modal")


def test_schema_accepts_legacy_popup_bool():
    req = SendNotificationRequest(client_id="c1", title="x", popup=True)
    assert req.popup is True
    assert req.popup_mode is None  # resolved to "popup" by create_notification


# ── surfaced in the display_notification ws_message (REST + integration) ─────


class _FakeWS:
    """Captures the single message sent to the connected client."""

    def __init__(self):
        self.sent = None

    async def __call__(self, client_id, message):  # send_to_client signature
        self.sent = message
        return True


@pytest.mark.asyncio
async def test_rest_ws_message_has_popup_mode(db_with_client, monkeypatch):
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

    # title only (no message), explicit popup_mode.
    body = SendNotificationRequest(
        client_id=client_id, title="hello", popup_mode="both"
    )
    await apinotif.send_notification(request, body)

    msg = fake_send.sent
    assert msg["command"] == "display_notification"
    assert msg["popup_mode"] == "both"
    assert msg["popup"] is True
    assert msg["title"] == "hello"
    assert msg["message"] == ""


@pytest.mark.asyncio
async def test_integration_ws_message_has_popup_mode(db_with_client, monkeypatch):
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

    # Legacy popup bool, no popup_mode -> resolved to "popup".
    body = SendNotificationRequest(client_id=client_id, title="hi", popup=True)
    await apiintg.integration_notify(request, body)

    msg = fake_send.sent
    assert msg["popup_mode"] == "popup"
    assert msg["popup"] is True


# ── surfaced in the per-client notification sensor (full content) ────────────
#
# The HA sensor carries the full push-shaped items (it is the remote
# delivery path), so popup_mode + the derived popup bool must
# survive the sensor projection exactly like in the WS payloads above.


@pytest.mark.asyncio
async def test_sensor_payload_carries_popup_mode(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(db, client_id=client_id, title="x", popup_mode="both")

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
    assert "notification_ids" not in attrs
    item = attrs["notifications"][0]
    assert item["notification_id"] == n["id"]
    assert item["popup_mode"] == "both"
    assert item["popup"] is True


# ── surfaced in the admin notifications list ─────────────────────────────────


@pytest.mark.asyncio
async def test_admin_list_has_popup_mode(db_with_client):
    db, client_id, _, _ = db_with_client
    await create_notification(db, client_id=client_id, title="x", popup_mode="popup")

    import api.notifications as apinotif

    state = types.SimpleNamespace(db=db, auth_type="admin")
    app = types.SimpleNamespace(state=state)
    request = types.SimpleNamespace(state=state, app=app)

    result = await apinotif.list_notifications(request, status="active")
    items = result["notifications"]
    assert len(items) == 1
    assert items[0]["popup_mode"] == "popup"
