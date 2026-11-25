"""Integration WebSocket push, add-on side.

Contract under test:

(a) Handshake auth BEFORE accept on GET /api/ws: a valid
    "Authorization: Bearer <integration token>" header authenticates the
    session as client_id="integration" and the server immediately sends
    {"type": "auth_ok", "client_id": "integration"}; a present-but-invalid
    header closes 4003 WITHOUT accept and counts an auth failure; without the
    header the session authenticates with the auth message as usual.
(b) The integration session is excluded from get_connected_client_ids and
    get_connected_count (it is an observer, not a client) and never triggers
    client_connected/client_disconnected broadcasts.
(c) The session is READ-ONLY: only ping/pong inbound; everything else is
    rejected with {"type":"error","message":"integration session is
    read-only"}.
(d) broadcast() delivers ALL and ADMIN events to the integration session,
    projected through _integration_view: notification_created is redacted to
    {"client_id","notification_id","ws_sent"} (never title/message/action);
    every other event passes unchanged. Admin sessions keep the full payload.
(e) ADMIN broadcasts pairing_started {"is_guest","expires_in"} (admin
    generate + integration start-pairing) and pairing_cancelled {} (admin
    cancel + integration cancel).
(f) client_connected fires on the auth-message success path for paired
    clients, not when the connection opens.
(g) Telemetry: record_request on WS auth success and on every integration
    pong (keeps the 90s connected window warm from the 30s ping cadence).
"""

import json
import time
import types
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
from api.auth import hash_secret
from services import integration_status, protocol

# Client info every app sends with auth/pair.
CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}

INTEGRATION_TOKEN = "tok-" + "a" * 60
VERSION_HEADER = "x-hub-integration-version"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate global WS/telemetry/pairing state and dev-mode env per test."""
    monkeypatch.delenv("HUB_DEV", raising=False)
    ws._clients.clear()
    integration_status.reset()
    pairing.clear_active_pins()
    yield
    ws._clients.clear()
    integration_status.reset()
    pairing.clear_active_pins()


async def _seed_integration_token(db, token=INTEGRATION_TOKEN):
    await db.set_setting("integration_token_hash", hash_secret(token))


class _DrivableWS:
    """Drives websocket_endpoint: yields queued messages then disconnects.

    Snapshots the connected-clients view right before disconnecting so tests
    can assert what the world looked like WHILE the session was open.
    """

    def __init__(self, db, messages=None, headers=None, host="203.0.113.7"):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.headers: dict[str, str] = dict(headers or {})
        self.client = SimpleNamespace(host=host)
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self.accepted = False
        self.snapshot: tuple[set, int] | None = None
        self._messages = list(messages or [])

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive_text(self):
        if self._messages:
            return self._messages.pop(0)
        self.snapshot = (ws.get_connected_client_ids(), ws.get_connected_count())
        raise WebSocketDisconnect()


class _RecordingWS:
    """Passive fake registered directly in ws._clients for broadcast tests."""

    def __init__(self):
        self._sent: list[str] = []

    async def send_text(self, data: str):
        self._sent.append(data)

    @property
    def messages(self) -> list[dict]:
        return [json.loads(s) for s in self._sent]


def _register(fake, client_id, authenticated=True, **extra):
    ws._clients[fake] = {
        "authenticated": authenticated,
        "client_id": client_id,
        "pair_attempts": 0,
        "auth_attempts": 0,
        **extra,
    }


def _integration_request(db):
    state = types.SimpleNamespace(db=db, ha_client=None)

    async def _body():
        return b""

    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=state),
        headers={},
        body=_body,
    )


def _admin_request(db):
    request = _integration_request(db)
    request.state.auth_type = "admin"
    return request


# ── (a) handshake auth before accept ─────────────────────────────────────────


class TestIntegrationHandshake:
    async def test_valid_bearer_authenticates_as_integration(self, db):
        await _seed_integration_token(db)
        fake = _DrivableWS(
            db,
            headers={
                "authorization": f"Bearer {INTEGRATION_TOKEN}",
                VERSION_HEADER: "2026.7.5",
            },
        )
        await ws.websocket_endpoint(fake)

        assert fake.accepted is True
        assert fake.sent[0] == {
            "type": "auth_ok",
            "client_id": "integration",
            **protocol.protocol_fields(),
        }
        # Telemetry recorded at auth success, with the handshake version.
        status = integration_status.get_status()
        assert status["connected"] is True
        assert status["request_count"] == 1
        assert status["integration_version"] == "2026.7.5"
        # Session cleaned up after disconnect.
        assert ws._clients == {}

    async def test_invalid_bearer_closes_4003_without_accept(self, db):
        await _seed_integration_token(db)
        fake = _DrivableWS(db, headers={"authorization": "Bearer wrong-token"})
        await ws.websocket_endpoint(fake)

        assert fake.accepted is False
        assert fake.closed is not None
        assert fake.closed[0] == 4003
        assert fake.sent == []
        assert ws._clients == {}
        assert integration_status.get_status()["auth_failure_count"] == 1

    async def test_bearer_without_configured_token_closes_4003(self, db):
        # No integration_token_hash stored (fresh install) — a Bearer header
        # can never authenticate and must not fall through to anonymous.
        fake = _DrivableWS(db, headers={"authorization": f"Bearer {INTEGRATION_TOKEN}"})
        await ws.websocket_endpoint(fake)

        assert fake.accepted is False
        assert fake.closed[0] == 4003
        assert integration_status.get_status()["auth_failure_count"] == 1

    async def test_without_header_the_connection_is_asked_to_auth(self, db):
        # db fixture seeds an api token, so auth stays required: the fresh
        # external connection is told to auth.
        fake = _DrivableWS(db)
        await ws.websocket_endpoint(fake)

        assert fake.accepted is True
        assert fake.sent[0]["type"] == "auth_required"
        assert all(m.get("type") != "auth_ok" for m in fake.sent)


# ── (b) excluded from connected clients ──────────────────────────────────────


class TestIntegrationExclusion:
    async def test_live_session_not_in_connected_ids_or_count(self, db):
        await _seed_integration_token(db)
        fake = _DrivableWS(
            db,
            messages=[json.dumps({"type": "ping"})],
            headers={"authorization": f"Bearer {INTEGRATION_TOKEN}"},
        )
        await ws.websocket_endpoint(fake)

        # Snapshot taken while the integration session was still registered.
        connected_ids, connected_count = fake.snapshot
        assert connected_ids == set()
        assert connected_count == 0

    def test_helpers_exclude_integration_pseudo_client(self):
        _register(_RecordingWS(), "integration")
        _register(_RecordingWS(), "ingress")
        _register(_RecordingWS(), "abc-paired")

        assert ws.get_connected_client_ids() == {"abc-paired"}
        # Only paired clients count; admin sessions and the integration are
        # observers, not clients.
        assert ws.get_connected_count() == 1

    async def test_disconnect_does_not_broadcast_client_disconnected(self, db):
        await _seed_integration_token(db)
        admin = _RecordingWS()
        _register(admin, "ingress")

        fake = _DrivableWS(db, headers={"authorization": f"Bearer {INTEGRATION_TOKEN}"})
        await ws.websocket_endpoint(fake)

        assert all(m["type"] != "client_disconnected" for m in admin.messages), (
            "integration session must never surface as a client"
        )


# ── (c) read-only session ────────────────────────────────────────────────────


class TestReadOnlySession:
    async def test_only_ping_pong_accepted_end_to_end(self, db):
        await _seed_integration_token(db)
        fake = _DrivableWS(
            db,
            messages=[
                json.dumps({"type": "get_areas"}),
                json.dumps({"type": "auth", "secret": "x"}),
                json.dumps({"type": "notification_ack", "notification_id": "n1"}),
                json.dumps({"type": "ping"}),
            ],
            headers={"authorization": f"Bearer {INTEGRATION_TOKEN}"},
        )
        await ws.websocket_endpoint(fake)

        assert fake.sent[0] == {
            "type": "auth_ok",
            "client_id": "integration",
            **protocol.protocol_fields(),
        }
        read_only_error = {
            "type": "error",
            "message": "integration session is read-only",
        }
        assert fake.sent[1] == read_only_error
        assert fake.sent[2] == read_only_error
        assert fake.sent[3] == read_only_error
        assert fake.sent[4] == {"type": "pong"}

    async def test_pong_marks_alive_and_records_telemetry(self, db):
        fake = _RecordingWS()
        fake.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        _register(fake, "integration", integration_version="9.9.9")

        before = time.time()
        await ws._handle_message(
            fake, {"type": "pong"}, authenticated=True, client_id="integration"
        )

        assert ws._clients[fake]["last_pong"] >= before
        status = integration_status.get_status()
        assert status["request_count"] == 1
        assert status["connected"] is True
        assert status["integration_version"] == "9.9.9"


# ── (d) broadcast projection / redaction ─────────────────────────────────────


class TestIntegrationBroadcastView:
    def test_view_redacts_notification_created_only(self):
        full = {
            "type": "notification_created",
            "data": {
                "notification": {
                    "id": "n-1",
                    "title": "Secret door open",
                    "message": "hush",
                    "action_target": "area-1",
                },
                "client_id": "c-1",
                "client_name": "Wall Panel",
                "ws_sent": True,
            },
        }
        redacted = ws._integration_view(full)
        assert redacted == {
            "type": "notification_created",
            "data": {"client_id": "c-1", "notification_id": "n-1", "ws_sent": True},
        }
        # Any other event passes through unchanged (identity).
        other = {"type": "client_status_updated", "data": {"battery_level": 42}}
        assert ws._integration_view(other) is other

    async def test_admin_event_reaches_integration_redacted(self):
        admin = _RecordingWS()
        integration = _RecordingWS()
        paired = _RecordingWS()
        _register(admin, "ingress")
        _register(integration, "integration")
        _register(paired, "abc-paired")

        await ws.broadcast(
            {
                "type": "notification_created",
                "data": {
                    "notification": {"id": "n-1", "title": "Secret", "message": "s"},
                    "client_id": "c-1",
                    "client_name": "Panel",
                    "ws_sent": False,
                },
            },
            audience=ws.EventAudience.ADMIN,
        )

        # Admin session: full payload.
        assert admin.messages[0]["data"]["notification"]["title"] == "Secret"
        # Integration session: ids only — no content anywhere.
        assert integration.messages[0] == {
            "type": "notification_created",
            "data": {"client_id": "c-1", "notification_id": "n-1", "ws_sent": False},
        }
        assert "Secret" not in str(integration.messages)
        # External paired client: nothing (ADMIN audience).
        assert paired.messages == []

    async def test_other_admin_event_unredacted_for_integration(self):
        integration = _RecordingWS()
        paired = _RecordingWS()
        _register(integration, "integration")
        _register(paired, "abc-paired")

        message = {
            "type": "client_status_updated",
            "data": {"id": "abc", "battery_level": 42},
        }
        await ws.broadcast(message, audience=ws.EventAudience.ADMIN)

        assert integration.messages == [message]
        assert paired.messages == []

    async def test_all_event_reaches_integration_and_clients(self):
        integration = _RecordingWS()
        paired = _RecordingWS()
        _register(integration, "integration")
        _register(paired, "abc-paired")

        message = {"type": "area_updated", "data": {"area_id": "a1"}}
        await ws.broadcast(message, audience=ws.EventAudience.ALL)

        assert integration.messages == [message]
        assert paired.messages == [message]

    async def test_log_tab_events_never_reach_integration(self):
        integration = _RecordingWS()
        _register(integration, "integration")

        for message in (
            {
                "type": "log_entry",
                "data": {"id": 1, "device_name": "iPhone Child Three"},
            },
            {"type": "log_read", "data": {"ids": [1], "all": False}},
        ):
            await ws.broadcast(message, audience=ws.EventAudience.ADMIN)

        assert integration.messages == []


# ── (e) pairing_started / pairing_cancelled broadcasts ───────────────────────


class TestPairingBroadcasts:
    @pytest.fixture
    def captured(self, monkeypatch):
        events = []

        async def _capture(message, *a, **k):
            audience = k.get("audience") or (a[0] if a else None)
            events.append((message, audience))

        monkeypatch.setattr(ws, "broadcast", _capture)
        return events

    async def test_admin_generate_broadcasts_pairing_started(self, db, captured):
        await pairing.generate_pairing_pin(_admin_request(db), None)

        assert (
            {
                "type": "pairing_started",
                "data": {
                    "is_guest": False,
                    "expires_in": pairing.PAIRING_TIMEOUT_SECONDS,
                },
            },
            ws.EventAudience.ADMIN,
        ) in captured

    async def test_admin_guest_generate_sets_is_guest(self, db, captured):
        data = pairing.GeneratePinRequest(access="guest", guest_hours=2)
        await pairing.generate_pairing_pin(_admin_request(db), data)

        started = [e for e, _ in captured if e["type"] == "pairing_started"]
        assert started == [
            {
                "data": {
                    "is_guest": True,
                    "expires_in": pairing.PAIRING_TIMEOUT_SECONDS,
                },
                "type": "pairing_started",
            }
        ]

    async def test_integration_start_pairing_broadcasts_pairing_started(
        self, db, captured
    ):
        await apiintg.integration_start_pairing(_integration_request(db))

        assert (
            {
                "type": "pairing_started",
                "data": {
                    "is_guest": False,
                    "expires_in": pairing.PAIRING_TIMEOUT_SECONDS,
                },
            },
            ws.EventAudience.ADMIN,
        ) in captured

    async def test_admin_cancel_broadcasts_pairing_cancelled(self, db, captured):
        pairing.set_active_pin(
            "1234-5678",
            {"created_at": time.time(), "expires_at": time.time() + 300},
        )
        await pairing.cancel_pairing(_admin_request(db))

        assert (
            {"type": "pairing_cancelled", "data": {}},
            ws.EventAudience.ADMIN,
        ) in captured

    async def test_admin_cancel_without_window_is_silent(self, db, captured):
        await pairing.cancel_pairing(_admin_request(db))
        assert all(e["type"] != "pairing_cancelled" for e, _ in captured)

    async def test_integration_cancel_broadcasts_pairing_cancelled(self, db, captured):
        pairing.set_active_pin(
            "1234-5678",
            {"created_at": time.time(), "expires_at": time.time() + 300},
        )
        result = await apiintg.integration_cancel_pairing(_integration_request(db))
        assert result["cancelled"] is True

        assert (
            {"type": "pairing_cancelled", "data": {}},
            ws.EventAudience.ADMIN,
        ) in captured

    async def test_integration_cancel_without_window_is_silent(self, db, captured):
        result = await apiintg.integration_cancel_pairing(_integration_request(db))
        assert result["cancelled"] is False
        assert all(e["type"] != "pairing_cancelled" for e, _ in captured)


# ── (f) client_connected fires on auth-message success ───────────────────────


class TestClientConnectedFix:
    async def test_auth_success_broadcasts_client_connected(
        self, db_with_client, monkeypatch
    ):
        db, client_id, client_secret, _ = db_with_client

        events = []

        async def _capture(message, *a, **k):
            audience = k.get("audience") or (a[0] if a else None)
            events.append((message, audience))

        monkeypatch.setattr(ws, "broadcast", _capture)

        fake = _RecordingWS()
        fake.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        fake.sent = []

        async def _send_json(payload):
            fake.sent.append(payload)

        fake.send_json = _send_json
        _register(fake, None, authenticated=False)

        authenticated, new_client_id = await ws._handle_message(
            fake,
            {"type": "auth", "secret": client_secret, **CLIENT_INFO},
            authenticated=False,
            client_id=None,
        )

        assert authenticated is True
        assert new_client_id == client_id
        assert fake.sent[0]["type"] == "auth_ok"
        assert (
            {"type": "client_connected", "data": {"client_id": client_id}},
            ws.EventAudience.ADMIN,
        ) in events

    async def test_failed_auth_does_not_broadcast(self, db_with_client, monkeypatch):
        db, _client_id, _secret, _ = db_with_client

        events = []

        async def _capture(message, *a, **k):
            events.append(message)

        monkeypatch.setattr(ws, "broadcast", _capture)

        fake = _RecordingWS()
        fake.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        fake.sent = []

        async def _send_json(payload):
            fake.sent.append(payload)

        fake.send_json = _send_json
        _register(fake, None, authenticated=False)

        await ws._handle_message(
            fake,
            {"type": "auth", "secret": "wrong-secret", **CLIENT_INFO},
            authenticated=False,
            client_id=None,
        )
        assert all(e["type"] != "client_connected" for e in events)

    async def test_connection_open_does_not_broadcast_for_ingress(self, db):
        """Opening a connection broadcasts nothing: an ingress session (the
        only kind authenticated at open besides dev and integration) never
        triggers client_connected."""
        admin = _RecordingWS()
        _register(admin, "ingress")

        fake = _DrivableWS(
            db, headers={"x-ingress-path": "/api/hassio_ingress/x"}, host="172.30.32.2"
        )
        await ws.websocket_endpoint(fake)

        assert fake.sent[0] == {
            "type": "auth_ok",
            "client_id": "ingress",
            **protocol.protocol_fields(),
        }
        assert all(m["type"] != "client_connected" for m in admin.messages)
