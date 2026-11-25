"""Remote fallback ack: POST /api/integration/notifications/ack.

Contract under test (remote ack through the integration service path):

(a) Unknown client → 404 (deactivated → 422, _get_active_client pattern).
(b) Per id, the WS ownership rule applies: unknown ids, ids belonging to
    another client, and ids that are no longer pending go silently to
    ``skipped`` — which also makes a duplicate ack idempotent (second call
    returns 200 with the id skipped).
(c) Matching pending ids are acknowledged with ``delivered_via='remote'``
    (+ delivered_at), while the WS ``notification_ack`` path records
    ``delivered_via='ws'`` — the DB status itself stays three-valued.
(d) Per acked id ONE ADMIN broadcast
    {"type": "notification_delivered", "data": {"notification_id",
    "client_id", "via": "remote"}}; the WS-ack broadcast carries the same
    shape with via="ws".
(e) The HA notification sensors are synced exactly ONCE per call (only when
    at least one id was acked) so the per-client sensor flips to delivered.
(f) Schema: 1-50 ids per call, each id 1-100 chars, client_id 1-100 chars.
(g) delivered_via is NOT part of the shared sensor/WS push item shape (the
    byte-equality drift test in test_sensor_privacy.py stays green).
"""

import types
import uuid

import pytest

from .profile_helpers import add_client
from pydantic import ValidationError

import api.integration as apiintg
import api.websocket as ws
import services.ha_sensors as ha_sensors
from models.database import Database
from models.schemas import AckNotificationsRequest
from services.notifications import (
    acknowledge_notification,
    create_notification,
    dismiss_notification,
    get_notification,
)


def _integration_request(db):
    """Minimal request with integration auth carrying the test db."""
    state = types.SimpleNamespace(db=db, ha_client=None)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=state),
    )


async def _make_client(db, name, *, is_active=True):
    client_id = str(uuid.uuid4())
    now = Database.now()
    await add_client(
        db,
        client_id=client_id,
        name=name,
        secret_hash="h" * 64,
        is_active=1 if is_active else 0,
        last_seen=now,
    )
    return client_id


def _ack_body(client_id, *ids):
    return AckNotificationsRequest(client_id=client_id, notification_ids=list(ids))


@pytest.fixture
def _quiet_ws(monkeypatch):
    """No-op the admin broadcast fan-out."""

    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)


@pytest.fixture
def _quiet_sensors(monkeypatch):
    """No-op the HA sensor sync (no Supervisor in tests)."""

    async def _noop_sync(*a, **k):
        return None

    monkeypatch.setattr(ha_sensors, "sync_notification_sensors_safe", _noop_sync)


# ── (a) client resolution ────────────────────────────────────────────────────


class TestClientResolution:
    async def test_unknown_client_404(self, db, _quiet_ws, _quiet_sensors):
        import fastapi

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ack_notifications(
                _integration_request(db), _ack_body("nope", "some-id")
            )
        assert exc.value.status_code == 404

    async def test_deactivated_client_422(self, db, _quiet_ws, _quiet_sensors):
        import fastapi

        client_id = await _make_client(db, "Panel", is_active=False)
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ack_notifications(
                _integration_request(db), _ack_body(client_id, "some-id")
            )
        assert exc.value.status_code == 422

    async def test_rejects_non_integration_auth(self, db):
        import fastapi

        client_id = await _make_client(db, "Panel")
        for auth_type in ("admin", "client", "session", "ingress"):
            request = _integration_request(db)
            request.state.auth_type = auth_type
            with pytest.raises(fastapi.HTTPException) as exc:
                await apiintg.integration_ack_notifications(
                    request, _ack_body(client_id, "some-id")
                )
            assert exc.value.status_code == 403


# ── (b)+(c) acked vs. skipped + delivered_via ────────────────────────────────


class TestAckedAndSkipped:
    async def test_valid_pending_id_is_acked_remote(
        self, db, _quiet_ws, _quiet_sensors
    ):
        client_id = await _make_client(db, "Panel")
        n = await create_notification(db, client_id=client_id, title="t", message="m")

        result = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, n["id"])
        )
        assert result == {
            "success": True,
            "client_id": client_id,
            "acked": [n["id"]],
            "skipped": [],
        }

        row = await db.fetchone(
            "SELECT status, delivered_at, delivered_via "
            "FROM client_notifications WHERE id = ?",
            (n["id"],),
        )
        assert row["status"] == "delivered"
        assert row["delivered_via"] == "remote"
        assert row["delivered_at"]

    async def test_unknown_id_is_skipped(self, db, _quiet_ws, _quiet_sensors):
        client_id = await _make_client(db, "Panel")
        result = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, "no-such-id")
        )
        assert result["acked"] == []
        assert result["skipped"] == ["no-such-id"]
        assert result["success"] is True

    async def test_foreign_id_is_skipped_and_untouched(
        self, db, _quiet_ws, _quiet_sensors
    ):
        mine = await _make_client(db, "Mine")
        other = await _make_client(db, "Other")
        n = await create_notification(db, client_id=other, title="t", message="m")

        result = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(mine, n["id"])
        )
        assert result["acked"] == []
        assert result["skipped"] == [n["id"]]

        # The other client's notification stays pending (no cross-client ack).
        notif = await get_notification(db, n["id"])
        assert notif["status"] == "pending"

    async def test_dismissed_id_is_skipped(self, db, _quiet_ws, _quiet_sensors):
        client_id = await _make_client(db, "Panel")
        n = await create_notification(db, client_id=client_id, title="t", message="m")
        await dismiss_notification(db, n["id"])

        result = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, n["id"])
        )
        assert result["acked"] == []
        assert result["skipped"] == [n["id"]]
        # Status untouched — a remote ack can never resurrect a dismissal.
        notif = await get_notification(db, n["id"])
        assert notif["status"] == "dismissed"

    async def test_double_ack_is_idempotent(self, db, _quiet_ws, _quiet_sensors):
        client_id = await _make_client(db, "Panel")
        n = await create_notification(db, client_id=client_id, title="t", message="m")

        first = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, n["id"])
        )
        second = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, n["id"])
        )
        assert first["acked"] == [n["id"]]
        assert second["acked"] == []
        assert second["skipped"] == [n["id"]]
        assert second["success"] is True

        # First winner counts — delivered_via stays 'remote'.
        row = await db.fetchone(
            "SELECT delivered_via FROM client_notifications WHERE id = ?",
            (n["id"],),
        )
        assert row["delivered_via"] == "remote"

    async def test_ws_ack_then_remote_ack_first_winner_counts(
        self, db, _quiet_ws, _quiet_sensors
    ):
        """Race WS-ack vs. remote-ack: whoever flips pending first wins."""
        client_id = await _make_client(db, "Panel")
        n = await create_notification(db, client_id=client_id, title="t", message="m")

        # WS path acked first (default delivered_via="ws").
        assert await acknowledge_notification(db, n["id"]) is True

        result = await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, n["id"])
        )
        assert result["acked"] == []
        assert result["skipped"] == [n["id"]]

        row = await db.fetchone(
            "SELECT delivered_via FROM client_notifications WHERE id = ?",
            (n["id"],),
        )
        assert row["delivered_via"] == "ws"

    async def test_mixed_batch(self, db, _quiet_ws, _quiet_sensors):
        mine = await _make_client(db, "Mine")
        other = await _make_client(db, "Other")
        ok1 = await create_notification(db, client_id=mine, title="a", message="")
        ok2 = await create_notification(db, client_id=mine, title="b", message="")
        foreign = await create_notification(db, client_id=other, title="x", message="")

        result = await apiintg.integration_ack_notifications(
            _integration_request(db),
            _ack_body(mine, ok1["id"], "unknown-id", foreign["id"], ok2["id"]),
        )
        assert result["acked"] == [ok1["id"], ok2["id"]]
        assert result["skipped"] == ["unknown-id", foreign["id"]]


# ── (d) admin broadcast shape ────────────────────────────────────────────────


class TestDeliveredBroadcast:
    async def test_one_admin_broadcast_per_acked_id_with_via_remote(
        self, db, monkeypatch, _quiet_sensors
    ):
        client_id = await _make_client(db, "Panel")
        n1 = await create_notification(db, client_id=client_id, title="a", message="")
        n2 = await create_notification(db, client_id=client_id, title="b", message="")

        events = []

        async def _capture_broadcast(message, *a, **k):
            audience = k.get("audience") or (a[0] if a else None)
            events.append((message, audience))

        monkeypatch.setattr(ws, "broadcast", _capture_broadcast)

        await apiintg.integration_ack_notifications(
            _integration_request(db),
            _ack_body(client_id, n1["id"], "unknown-id", n2["id"]),
        )

        delivered = [e for e in events if e[0]["type"] == "notification_delivered"]
        assert len(delivered) == 2  # one per ACKED id, none for skipped
        assert delivered[0][0] == {
            "type": "notification_delivered",
            "data": {
                "notification_id": n1["id"],
                "client_id": client_id,
                "via": "remote",
            },
        }
        assert delivered[1][0]["data"]["notification_id"] == n2["id"]
        for _msg, audience in delivered:
            assert audience == ws.EventAudience.ADMIN

    async def test_no_broadcast_when_everything_skipped(
        self, db, monkeypatch, _quiet_sensors
    ):
        client_id = await _make_client(db, "Panel")

        events = []

        async def _capture_broadcast(message, *a, **k):
            events.append(message)

        monkeypatch.setattr(ws, "broadcast", _capture_broadcast)

        await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, "unknown-id")
        )
        assert events == []

    async def test_ws_ack_broadcast_carries_via_ws(
        self, db, monkeypatch, _quiet_sensors
    ):
        """The WS notification_ack path emits the same shape with via='ws'."""
        client_id = await _make_client(db, "Panel")
        n = await create_notification(db, client_id=client_id, title="t", message="m")

        events = []

        async def _capture_broadcast(message, *a, **k):
            events.append(message)

        monkeypatch.setattr(ws, "broadcast", _capture_broadcast)

        fake = types.SimpleNamespace(
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(db=db, ha_client=None)
            ),
            sent=[],
        )

        async def _send_json(payload):
            fake.sent.append(payload)

        fake.send_json = _send_json

        await ws._handle_message(
            fake,
            {"type": "notification_ack", "notification_id": n["id"]},
            authenticated=True,
            client_id=client_id,
        )

        assert {"type": "notification_ack_ok", "notification_id": n["id"]} in fake.sent
        assert {
            "type": "notification_delivered",
            "data": {
                "notification_id": n["id"],
                "client_id": client_id,
                "via": "ws",
            },
        } in events

        row = await db.fetchone(
            "SELECT delivered_via FROM client_notifications WHERE id = ?",
            (n["id"],),
        )
        assert row["delivered_via"] == "ws"


# ── (e) exactly one sensor sync per call ─────────────────────────────────────


class TestSensorSync:
    async def test_single_sync_for_multi_id_batch(self, db, monkeypatch, _quiet_ws):
        client_id = await _make_client(db, "Panel")
        n1 = await create_notification(db, client_id=client_id, title="a", message="")
        n2 = await create_notification(db, client_id=client_id, title="b", message="")
        n3 = await create_notification(db, client_id=client_id, title="c", message="")

        sync_calls = []

        async def _capture_sync(*a, **k):
            sync_calls.append(a)

        monkeypatch.setattr(ha_sensors, "sync_notification_sensors_safe", _capture_sync)

        await apiintg.integration_ack_notifications(
            _integration_request(db),
            _ack_body(client_id, n1["id"], n2["id"], n3["id"]),
        )
        assert len(sync_calls) == 1

    async def test_no_sync_when_nothing_acked(self, db, monkeypatch, _quiet_ws):
        client_id = await _make_client(db, "Panel")

        sync_calls = []

        async def _capture_sync(*a, **k):
            sync_calls.append(a)

        monkeypatch.setattr(ha_sensors, "sync_notification_sensors_safe", _capture_sync)

        await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, "unknown-id")
        )
        assert sync_calls == []


# ── (f) schema validation ────────────────────────────────────────────────────


class TestAckSchema:
    def test_zero_ids_rejected(self):
        with pytest.raises(ValidationError):
            AckNotificationsRequest(client_id="c", notification_ids=[])

    def test_51_ids_rejected(self):
        with pytest.raises(ValidationError):
            AckNotificationsRequest(
                client_id="c", notification_ids=[f"id-{i}" for i in range(51)]
            )

    def test_50_ids_accepted(self):
        body = AckNotificationsRequest(
            client_id="c", notification_ids=[f"id-{i}" for i in range(50)]
        )
        assert len(body.notification_ids) == 50

    def test_overlong_id_rejected(self):
        with pytest.raises(ValidationError):
            AckNotificationsRequest(client_id="c", notification_ids=["x" * 101])

    def test_empty_id_rejected(self):
        with pytest.raises(ValidationError):
            AckNotificationsRequest(client_id="c", notification_ids=[""])

    def test_empty_client_id_rejected(self):
        with pytest.raises(ValidationError):
            AckNotificationsRequest(client_id="", notification_ids=["id-1"])

    def test_overlong_client_id_rejected(self):
        with pytest.raises(ValidationError):
            AckNotificationsRequest(client_id="c" * 101, notification_ids=["id-1"])


# ── (g) delivered_via is admin-only, not in the push item shape ──────────────


class TestDeliveredViaNotInPushShape:
    async def test_push_payload_has_no_delivered_via(
        self, db, _quiet_ws, _quiet_sensors
    ):
        from services.notifications import (
            get_client_notifications,
            notification_to_push_payload,
        )

        client_id = await _make_client(db, "Panel")
        n = await create_notification(db, client_id=client_id, title="t", message="m")
        await apiintg.integration_ack_notifications(
            _integration_request(db), _ack_body(client_id, n["id"])
        )

        items = await get_client_notifications(db, client_id, active_only=True)
        # The normalized admin dict carries delivered_via ...
        assert items[0]["delivered_via"] == "remote"
        # ... but the shared sensor/WS push item shape does NOT (drift guard).
        payload = notification_to_push_payload(items[0])
        assert "delivered_via" not in payload
