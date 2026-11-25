"""Integration REST surface: PII-free status, notify, kick, cancel, client
tags, guest extend.

Contract under test:

(a) GET /api/integration/status is PII-free: notifications appear as
    ids+counts per client only (no title/message/action fields), guests as
    client_id/is_connected/expires_at only, pairing_active: bool is present
    and the pairing PIN is NOT in the payload.
(b) POST /api/integration/notify targets one client AND "all" (broadcast),
    fields matching the hub notification model.
(c) POST /api/integration/clients/{client_id}/kick disconnects the WS
    session; the client row stays active.
(d) POST /api/integration/pairing/cancel cancels an open pairing window and
    returns 200 also when none is open (idempotent).
(e) POST /api/integration/clients/{client_id}/tags replaces the FULL tag
    list (body {"tags": [...]}) — shares set_client_tags with the admin
    PATCH /pairing/clients/{id}, including the client_config_updated
    broadcast.
(f) POST /api/integration/clients/{client_id}/extend extends a guest's
    expiry (body {"hours": N} or {"expires_at": "ISO"}) — shares
    extend_guest_expiry with the admin PATCH /clients/{id}/extend and
    returns {"client_id", "expires_at"} (UTC, to the second).

All endpoints reject non-integration auth (require_integration).
"""

import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from .profile_helpers import add_client

import api.integration as apiintg
import api.websocket as ws
from models.database import Database
from models.schemas import (
    ClientTagsRequest,
    ExtendGuestRequest,
    SendNotificationRequest,
)
from services.notifications import create_notification

# Guest grants are at most 90 days ahead.
_BASE = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0)
SOON_A = _BASE.replace(hour=12, minute=30, second=45).isoformat()
SOON_B = _BASE.replace(hour=12, minute=30, second=0).isoformat()
SOON_C = (_BASE + timedelta(days=1)).replace(hour=9, minute=15, second=30).isoformat()


def _integration_request(db):
    """Minimal request with integration auth carrying the test db."""
    state = types.SimpleNamespace(db=db, ha_client=None)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=state),
    )


def _admin_request(db):
    """Minimal request with admin auth (for admin-parity checks)."""
    request = _integration_request(db)
    request.state.auth_type = "admin"
    return request


async def _make_client(db, name, *, is_guest=False, expires_at=None):
    client_id = str(uuid.uuid4())
    now = Database.now()
    await add_client(
        db,
        client_id=client_id,
        name=name,
        secret_hash="h" * 64,
        guest=is_guest,
        expires_at=expires_at,
        last_seen=now,
        device_name="iPad Pro",
    )
    return client_id


@pytest.fixture
def clean_pairing():
    """Isolate the module-level active-pin state around each test."""
    from api.pairing import clear_active_pins

    clear_active_pins()
    yield
    clear_active_pins()


@pytest.fixture
def _quiet_ws(monkeypatch):
    """No-op the WS fan-out used by the notification delivery path."""

    async def _noop_send(client_id, message):
        return False

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "send_to_client", _noop_send)
    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)


# ── (a) status: PII-free payload ─────────────────────────────────────────────


class TestStatusPayload:
    async def test_notifications_are_ids_and_counts_only(self, db, clean_pairing):
        client_id = await _make_client(db, "Wall Panel")
        n1 = await create_notification(
            db, client_id=client_id, title="Secret door open", message="hush"
        )
        n2 = await create_notification(
            db, client_id=client_id, title="Second", message="also secret"
        )

        result = await apiintg.integration_status(_integration_request(db))

        me = next(c for c in result["clients"] if c["id"] == client_id)
        # Newest first, ids only.
        assert me["notification_ids"] == [n2["id"], n1["id"]]
        assert me["notification_count"] == 2
        assert "notifications" not in me
        # No content anywhere in the whole payload.
        assert "Secret door open" not in str(result)
        assert "hush" not in str(result)

    async def test_guests_are_stripped_to_triple(self, db, clean_pairing):
        guest_id = await _make_client(
            db,
            "Manager One's iPhone",
            is_guest=True,
            expires_at="2026-07-04T10:00:00+00:00",
        )
        await _make_client(db, "Wall Panel")  # non-guest

        result = await apiintg.integration_status(_integration_request(db))

        assert result["guest_count"] == 1
        entry = result["guests"][0]
        assert set(entry) == {"client_id", "is_connected", "expires_at"}
        assert entry["client_id"] == guest_id
        assert entry["is_connected"] is False
        assert entry["expires_at"] == "2026-07-04T10:00:00+00:00"
        # The guest name must not leak through the guests list (it is still a
        # paired client, so the clients list carries it for device naming).
        assert "Manager One" not in str(result["guests"])

    async def test_pairing_active_bool_and_no_pin(self, db, clean_pairing):
        from api.pairing import set_active_pin

        result = await apiintg.integration_status(_integration_request(db))
        assert result["pairing_active"] is False

        set_active_pin(
            "1234-5678",
            {
                "created_at": time.time(),
                "expires_at": time.time() + 300,
                "failed_attempts": 0,
            },
        )
        result = await apiintg.integration_status(_integration_request(db))
        assert result["pairing_active"] is True
        assert result["pairing_remaining"] > 0
        # The PIN itself must NOT be in this payload (QR endpoint carries it).
        assert "pairing_pin" not in result
        assert "1234-5678" not in str(result)

    async def test_rejects_non_integration_auth(self, db):
        import fastapi

        request = _integration_request(db)
        request.state.auth_type = "admin"
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_status(request)
        assert exc.value.status_code == 403


# ── (b) notify: one client AND "all" ─────────────────────────────────────────


class TestNotify:
    async def test_single_client(self, db, _quiet_ws):
        client_id = await _make_client(db, "Panel")

        body = SendNotificationRequest(
            client_id=client_id, title="Hello", message="World"
        )
        result = await apiintg.integration_notify(_integration_request(db), body)

        assert result["notification"]["title"] == "Hello"
        assert result["ws_sent"] is False  # not connected in tests

        rows = await db.fetchall(
            "SELECT * FROM client_notifications WHERE client_id = ?", (client_id,)
        )
        assert len(rows) == 1

    async def test_broadcast_all(self, db, _quiet_ws):
        c1 = await _make_client(db, "Panel 1")
        c2 = await _make_client(db, "Panel 2")

        body = SendNotificationRequest(client_id="all", title="Everyone")
        result = await apiintg.integration_notify(_integration_request(db), body)
        assert result == {"sent": 2}

        for cid in (c1, c2):
            rows = await db.fetchall(
                "SELECT * FROM client_notifications WHERE client_id = ?", (cid,)
            )
            assert len(rows) == 1

    async def test_unknown_client_404(self, db, _quiet_ws):
        import fastapi

        body = SendNotificationRequest(client_id="nope", title="x")
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_notify(_integration_request(db), body)
        assert exc.value.status_code == 404

    async def test_title_or_message_required(self, db, _quiet_ws):
        import fastapi

        body = SendNotificationRequest(client_id="all")
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_notify(_integration_request(db), body)
        assert exc.value.status_code == 400


# ── (c) kick ─────────────────────────────────────────────────────────────────


class TestKick:
    async def test_kick_disconnects_and_keeps_row_active(self, db, monkeypatch):
        client_id = await _make_client(db, "Panel")

        calls = []

        async def _fake_disconnect(cid, code=4003, reason=""):
            calls.append((cid, code, reason))
            return 1

        monkeypatch.setattr(ws, "disconnect_client", _fake_disconnect)

        result = await apiintg.integration_kick_client(
            _integration_request(db), client_id
        )
        assert result == {"success": True, "client_id": client_id, "closed": 1}
        # 4004 = plain kick: NOT a credential-reset code (4001/4003) and not
        # the auth-timeout code (4002).
        assert calls == [(client_id, 4004, "Kicked")]

        # The client row stays active (kick != deactivate/delete).
        row = await db.fetchone(
            "SELECT is_active FROM paired_clients WHERE id = ?", (client_id,)
        )
        assert row["is_active"] == 1

    async def test_kick_offline_client_returns_zero(self, db):
        client_id = await _make_client(db, "Panel")
        result = await apiintg.integration_kick_client(
            _integration_request(db), client_id
        )
        assert result["success"] is True
        assert result["closed"] == 0

    async def test_kick_unknown_client_404(self, db):
        import fastapi

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_kick_client(_integration_request(db), "nope")
        assert exc.value.status_code == 404


# ── (d) pairing/cancel ───────────────────────────────────────────────────────


class TestCancelPairing:
    async def test_cancels_open_window(self, db, clean_pairing):
        from api.pairing import get_active_pin, set_active_pin

        set_active_pin(
            "1234-5678",
            {
                "created_at": time.time(),
                "expires_at": time.time() + 300,
                "failed_attempts": 0,
            },
        )
        result = await apiintg.integration_cancel_pairing(_integration_request(db))
        assert result == {"success": True, "cancelled": True}
        assert get_active_pin() is None

    async def test_idempotent_when_none_open(self, db, clean_pairing):
        result = await apiintg.integration_cancel_pairing(_integration_request(db))
        assert result == {"success": True, "cancelled": False}
        # Calling again is still fine.
        result = await apiintg.integration_cancel_pairing(_integration_request(db))
        assert result == {"success": True, "cancelled": False}


# ── (e) clients/{id}/tags ────────────────────────────────────────────────────


class TestSetClientTags:
    """The integration's tags become a device adjustment of the rule areas."""

    async def _areas(self, db, client_id):
        from services.profiles import store

        return (await store.resolve_client(db, client_id))[2]["areas"]

    async def test_sets_tags_and_broadcasts(self, db, monkeypatch):
        client_id = await _make_client(db, "Panel")

        sent = []

        async def _capture(cid, message):
            sent.append((cid, message))
            return True

        async def _quiet(message, *a, **k):
            return None

        monkeypatch.setattr(ws, "send_to_client", _capture)
        monkeypatch.setattr(ws, "broadcast", _quiet)

        body = ClientTagsRequest(tags=["kitchen", "guests"])
        result = await apiintg.integration_set_client_tags(
            _integration_request(db), client_id, body
        )
        assert result == {
            "success": True,
            "client_id": client_id,
            "tags": ["kitchen", "guests"],
        }
        areas = await self._areas(db, client_id)
        assert areas["source"] == "device"
        assert areas["value"]["mode"] == "selected"
        assert areas["value"]["tags"] == ["kitchen", "guests"]
        # Area visibility changed -> the device re-fetches its config live.
        assert (client_id, {"type": "client_config_updated", "data": {}}) in sent

    async def test_empty_list_means_all_for_members(self, db, _quiet_ws):
        client_id = await _make_client(db, "Panel")
        await apiintg.integration_set_client_tags(
            _integration_request(db), client_id, ClientTagsRequest(tags=["a"])
        )
        result = await apiintg.integration_set_client_tags(
            _integration_request(db), client_id, ClientTagsRequest(tags=[])
        )
        assert result["tags"] == ["*"]
        areas = await self._areas(db, client_id)
        assert areas["value"]["mode"] == "all"
        # Equal to the profile value: the adjustment is gone again.
        assert areas["source"] == "profile"

    async def test_empty_list_means_none_for_guests(self, db, _quiet_ws):
        client_id = await _make_client(db, "Gast", is_guest=True, expires_at=SOON_A)
        await apiintg.integration_set_client_tags(
            _integration_request(db), client_id, ClientTagsRequest(tags=["*"])
        )
        result = await apiintg.integration_set_client_tags(
            _integration_request(db), client_id, ClientTagsRequest(tags=[])
        )
        assert result["tags"] == []

    async def test_fixed_areas_answer_409(self, db, _quiet_ws):
        import fastapi

        client_id = await _make_client(db, "Panel")
        await db.execute(
            "UPDATE profiles SET body_json = json_set(body_json, '$.fixed', "
            "json('[\"areas\"]')) WHERE id = (SELECT profile_id FROM paired_clients "
            "WHERE id = ?)",
            (client_id,),
        )
        await db.commit()
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_set_client_tags(
                _integration_request(db), client_id, ClientTagsRequest(tags=["x"])
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "rule_fixed"

    async def test_unknown_client_404(self, db, _quiet_ws):
        import fastapi

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_set_client_tags(
                _integration_request(db), "nope", ClientTagsRequest(tags=["x"])
            )
        assert exc.value.status_code == 404

    async def test_rejects_non_integration_auth(self, db):
        import fastapi

        client_id = await _make_client(db, "Panel")
        for auth_type in ("admin", "client", "session", "ingress"):
            request = _integration_request(db)
            request.state.auth_type = auth_type
            with pytest.raises(fastapi.HTTPException) as exc:
                await apiintg.integration_set_client_tags(
                    request, client_id, ClientTagsRequest(tags=["x"])
                )
            assert exc.value.status_code == 403


# ── (f) clients/{id}/extend ──────────────────────────────────────────────────


class TestExtendGuest:
    async def test_explicit_expires_at_keeps_seconds(self, db, _quiet_ws):
        guest_id = await _make_client(
            db, "Guest", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )

        body = ExtendGuestRequest(expires_at=SOON_A)
        result = await apiintg.integration_extend_guest(
            _integration_request(db), guest_id, body
        )
        # Same normalization as the admin PATCH: UTC, to the second.
        assert result == {
            "client_id": guest_id,
            "expires_at": SOON_A,
            "hub_remote_now": False,
            "hub_remote_at": None,
        }

        row = await db.fetchone(
            "SELECT expires_at FROM paired_clients WHERE id = ?", (guest_id,)
        )
        assert row["expires_at"] == SOON_A

    async def test_hours_adds_from_now(self, db, _quiet_ws):
        guest_id = await _make_client(
            db, "Guest", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )

        result = await apiintg.integration_extend_guest(
            _integration_request(db), guest_id, ExtendGuestRequest(hours=2)
        )
        new_exp = datetime.fromisoformat(result["expires_at"])
        expected = datetime.now(timezone.utc) + timedelta(hours=2)
        # To the second, so it lags "now + 2h" by less than a second.
        assert new_exp.microsecond == 0
        assert timedelta(0) <= (expected - new_exp) < timedelta(seconds=2)

    async def test_notifies_client_and_admins(self, db, monkeypatch):
        """Admin parity: guest_extended goes to the client and to admins."""
        guest_id = await _make_client(
            db, "Guest", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )

        sent, events = [], []

        async def _capture_send(client_id, message):
            sent.append((client_id, message))
            return True

        async def _capture_broadcast(message, *a, **k):
            events.append(message)

        monkeypatch.setattr(ws, "send_to_client", _capture_send)
        monkeypatch.setattr(ws, "broadcast", _capture_broadcast)

        body = ExtendGuestRequest(expires_at=SOON_B)
        await apiintg.integration_extend_guest(_integration_request(db), guest_id, body)

        assert (
            guest_id,
            {"type": "guest_extended", "expires_at": SOON_B},
        ) in sent
        assert {
            "type": "guest_extended",
            "data": {
                "client_id": guest_id,
                "expires_at": SOON_B,
            },
        } in events

    async def test_matches_admin_extend_behavior(self, db, _quiet_ws):
        """Integration extend and admin PATCH extend share extend_guest_expiry."""
        import api.pairing as pairing

        g1 = await _make_client(
            db, "Guest 1", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )
        g2 = await _make_client(
            db, "Guest 2", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )
        target = SOON_C

        admin_result = await pairing.extend_guest(
            _admin_request(db), g1, ExtendGuestRequest(expires_at=target)
        )
        intg_result = await apiintg.integration_extend_guest(
            _integration_request(db), g2, ExtendGuestRequest(expires_at=target)
        )
        assert admin_result["expires_at"] == intg_result["expires_at"] == SOON_C

    async def test_non_guest_400(self, db, _quiet_ws):
        import fastapi

        client_id = await _make_client(db, "Panel")
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_extend_guest(
                _integration_request(db), client_id, ExtendGuestRequest(hours=1)
            )
        assert exc.value.status_code == 400

    async def test_unknown_client_404(self, db, _quiet_ws):
        import fastapi

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_extend_guest(
                _integration_request(db), "nope", ExtendGuestRequest(hours=1)
            )
        assert exc.value.status_code == 404

    async def test_neither_hours_nor_expires_at_400(self, db, _quiet_ws):
        import fastapi

        guest_id = await _make_client(
            db, "Guest", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_extend_guest(
                _integration_request(db), guest_id, ExtendGuestRequest()
            )
        assert exc.value.status_code == 400

    async def test_rejects_non_integration_auth(self, db):
        import fastapi

        guest_id = await _make_client(
            db, "Guest", is_guest=True, expires_at="2026-07-04T10:00:00+00:00"
        )
        for auth_type in ("admin", "client", "session", "ingress"):
            request = _integration_request(db)
            request.state.auth_type = auth_type
            with pytest.raises(fastapi.HTTPException) as exc:
                await apiintg.integration_extend_guest(
                    request, guest_id, ExtendGuestRequest(hours=1)
                )
            assert exc.value.status_code == 403
