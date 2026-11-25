"""WS hardening: per-IP limits, global PIN failure counter, revoke.

* Unauthenticated WS slots are limited per source IP, so one host cannot
  lock out every app.
* Pairing attempts are limited per IP across connections and REST; every
  wrong or rate-limited attempt counts against the active PIN, which dies after
  ``MAX_PIN_FAILURES``.
* Non-string ``pin``/``client_name`` are rejected.
* Revoke closes the live WS at once; a deleted client in its reset grace
  period gets no reads; after the wipe-ack purge the receive loop ends without
  touching the closed socket.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

import api.pairing as pairing
import api.websocket as ws
from api.ws import handshake
from models.database import Database

# Client info every app sends with auth/pair.
CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("HUB_DEV", raising=False)
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()
    events = []

    async def _capture(message, *a, **k):
        events.append(message)

    monkeypatch.setattr(ws, "broadcast", _capture)
    yield events
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()


class _WS:
    """Fake WebSocket that can drive websocket_endpoint."""

    def __init__(self, db, messages=(), host="192.0.2.10"):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.headers = {}
        self.client = SimpleNamespace(host=host)
        self.url = SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []
        self.closed = None
        self.application_state = WebSocketState.CONNECTING
        self._messages = list(messages)
        self.receives_after_close = 0

    async def accept(self):
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED

    async def receive_text(self):
        if self.application_state != WebSocketState.CONNECTED:
            self.receives_after_close += 1
            raise RuntimeError(
                'WebSocket is not connected. Need to call "accept" first.'
            )
        if self._messages:
            return json.dumps(self._messages.pop(0))
        raise WebSocketDisconnect()


class _Pending:
    """Hashable placeholder for a pending (unauthenticated) connection."""


def _pending(host):
    fake = _Pending()
    ws._clients[fake] = {"authenticated": False, "client_id": None, "ip": host}
    return fake


async def _client(db, *, is_guest=False):
    secret = uuid.uuid4().hex
    client_id = await add_client(
        db, name="C", secret=secret, last_seen=Database.now(), guest=is_guest
    )
    return client_id, secret


def _activate_pin(pin="1234-5678"):
    pairing.set_active_pin(pin, {"created_at": 0, "expires_at": 9e12})
    return pin


class TestUnauthSlotsPerIp:
    async def test_sixth_pending_connection_from_one_ip_rejected(self, db):
        for _ in range(ws.MAX_WS_UNAUTH_PER_IP):
            _pending("192.0.2.10")
        fake = _WS(db, host="192.0.2.10")
        await ws.websocket_endpoint(fake)
        assert fake.closed == (4029, "Too many connections")
        assert fake.application_state == WebSocketState.DISCONNECTED

    async def test_other_ip_still_gets_a_slot(self, db):
        for _ in range(ws.MAX_WS_UNAUTH_PER_IP):
            _pending("192.0.2.10")
        fake = _WS(db, host="192.0.2.99")
        await ws.websocket_endpoint(fake)
        assert fake.sent[0]["type"] == "auth_required"
        assert fake.closed is None


class TestPinFailures:
    async def _pair(self, db, fake, pin):
        ws._clients.setdefault(
            fake, {"authenticated": False, "client_id": None, "pair_attempts": 0}
        )
        await ws._handle_message(
            fake,
            {"type": "pair", "pin": pin, "client_name": "X", **CLIENT_INFO},
            False,
            None,
        )
        return fake.sent[-1]

    async def _burn(self, db, host, attempts):
        replies = []
        fake = _WS(db, host=host)
        for i in range(attempts):
            if i and i % ws.MAX_PAIR_ATTEMPTS == 0:
                fake = _WS(db, host=host)
            replies.append(await self._pair(db, fake, f"{i % 10}{i % 10}00-0000"))
        return replies

    async def test_pin_dies_after_failures_from_several_addresses(
        self, db, _clean_state
    ):
        """30 failures from at least 3 addresses kill the PIN."""
        real = _activate_pin()
        hosts = [f"192.0.2.{i}" for i in range(1, pairing.MIN_PIN_FAILURE_IPS + 1)]
        per_host = pairing.MAX_PIN_FAILURES // len(hosts)
        for host in hosts:
            await self._burn(db, host, per_host)
        assert pairing.get_active_pin() is None
        assert {
            "type": "pairing_cancelled",
            "data": {"reason": "too_many_failures"},
        } in (_clean_state)
        # the real PIN no longer pairs and the device learns why
        reply = await self._pair(db, _WS(db, host="192.0.2.200"), real)
        assert reply["type"] == "pair_error"
        assert reply["reason"] == "too_many_failures"

    async def test_one_address_cannot_kill_the_pin(self, db, _clean_state):
        """A single LAN host only exhausts its own budget."""
        real = _activate_pin()
        replies = await self._burn(db, "192.0.2.66", 40)
        assert pairing.get_active_pin() is not None
        assert {r["reason"] for r in replies} == {"pin_invalid", "rate_limited"}
        assert [r["reason"] for r in replies].count("pin_invalid") == (
            pairing.PAIR_IP_LIMIT_MAX
        )
        reply = await self._pair(db, _WS(db, host="192.0.2.10"), real)
        assert reply["type"] == "pair_ok"

    async def test_per_ip_budget_spans_connections(self, db):
        _activate_pin()
        replies = []
        for _ in range(3):  # three connections, 5 attempts each, same IP
            fake = _WS(db, host="192.0.2.50")
            for _ in range(5):
                replies.append(await self._pair(db, fake, "0000-0001"))
        reasons = [r["reason"] for r in replies]
        assert reasons.count("pin_invalid") == pairing.PAIR_IP_LIMIT_MAX
        assert "rate_limited" in reasons

    async def test_new_pin_resets_ip_budget(self, db):
        """A host that burnt its budget only locked itself out; a new PIN
        (admin intent) gives it a fresh window, a success clears it too."""
        _activate_pin()
        await self._burn(db, "192.0.2.60", pairing.PAIR_IP_LIMIT_MAX)
        assert not pairing.pair_ip_allowed("192.0.2.60")
        real = _activate_pin("2468-1357")
        assert pairing.pair_ip_allowed("192.0.2.60")
        other = _WS(db, host="192.0.2.61")
        await self._pair(db, other, "0000-0001")
        reply = await self._pair(db, other, real)
        assert reply["type"] == "pair_ok"
        assert pairing._pair_rate_limits.get("pair-ip:192.0.2.61") is None

    async def test_expired_pin_has_its_own_reason(self, db):
        pin = "1357-2468"
        pairing.set_active_pin(pin, {"created_at": 0, "expires_at": 1})
        reply = await self._pair(db, _WS(db, host="192.0.2.70"), pin)
        assert reply == {
            "type": "pair_error",
            "reason": "pin_expired",
            "message": "Pairing PIN expired",
        }
        # not counted as a failure
        assert pairing.pair_ip_allowed("192.0.2.70")
        assert pairing._pair_rate_limits.get("pair-ip:192.0.2.70") is None

    async def test_wrong_pin_reason(self, db):
        _activate_pin()
        reply = await self._pair(db, _WS(db), "0000-0001")
        assert reply["reason"] == "pin_invalid"

    async def test_correct_pin_still_pairs_below_limit(self, db):
        real = _activate_pin()
        fake = _WS(db)
        for _ in range(3):
            await self._pair(db, fake, "0000-0001")
        reply = await self._pair(db, fake, real)
        assert reply["type"] == "pair_ok"

    async def test_non_string_pin_rejected_without_crash(self, db):
        real = _activate_pin()
        fake = _WS(db)
        reply = await self._pair(db, fake, 12345678)
        assert reply["type"] == "pair_error"
        ws._clients[fake]["pair_attempts"] = 0
        await ws._handle_message(
            fake,
            {"type": "pair", "pin": real, "client_name": ["x"], **CLIENT_INFO},
            False,
            None,
        )
        assert fake.sent[-1]["type"] == "pair_error"
        assert pairing.get_active_pin() is not None, "PIN not burned by a bad name"

    async def test_rest_pair_endpoint_removed(self):
        from main import app

        paths = set(app.openapi()["paths"])
        assert "/api/pairing/pair" not in paths
        assert "/api/pairing/generate" in paths


class TestRevokeAndRemoval:
    async def test_soft_revoke_closes_live_ws(self, db):
        client_id, _ = await _client(db)
        guest_id, _ = await _client(db, is_guest=True)
        live, guest_ws = _WS(db), _WS(db)
        ws._clients[live] = {"authenticated": True, "client_id": client_id}
        ws._clients[guest_ws] = {"authenticated": True, "client_id": guest_id}
        req = SimpleNamespace(
            state=SimpleNamespace(auth_type="ingress"),
            app=SimpleNamespace(state=SimpleNamespace(db=db)),
        )
        await pairing.revoke_client(req, client_id)
        await pairing.revoke_client(req, guest_id)
        assert live.closed == (4001, "Client revoked")
        assert guest_ws.closed == (4003, "Guest session revoked")
        assert live not in ws._clients and guest_ws not in ws._clients

    async def test_reset_signaled_session_gets_no_reads(self, db):
        client_id, _ = await _client(db)
        fake = _WS(db)
        ws._clients[fake] = {
            "authenticated": True,
            "client_id": client_id,
            "reset_signaled": True,
        }
        for msg_type in ("get_areas", "get_entity_state", "get_client_config"):
            await ws._handle_message(
                fake, {"type": msg_type, "entity_id": "light.x"}, True, client_id
            )
            assert fake.sent[-1] == {"type": "error", "message": "Client removed"}
        await ws._handle_message(fake, {"type": "ping"}, True, client_id)
        assert fake.sent[-1] == {"type": "pong"}

    async def test_loop_stops_after_wipe_ack_purge(self, db):
        client_id, secret = await _client(db)
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )
        await db.commit()
        fake = _WS(
            db,
            messages=[
                {"type": "auth", "secret": secret, **CLIENT_INFO},
                {"type": "client_reset_ack", "client_id": client_id},
                {"type": "get_areas"},
            ],
        )
        await ws.websocket_endpoint(fake)
        types = [m["type"] for m in fake.sent]
        assert types == ["auth_required", "client_removed", "client_reset_ack_ok"]
        assert fake.closed == (1000, "Client removed")
        assert fake.receives_after_close == 0
        assert fake not in ws._clients

    async def test_loop_stops_when_session_was_evicted(self, db, monkeypatch):
        client_id, secret = await _client(db)
        fake = _WS(db, messages=[{"type": "auth", "secret": secret, **CLIENT_INFO}])
        real_handle = handshake._handle_message

        async def _handle_then_revoke(websocket, message, authenticated, cid):
            result = await real_handle(websocket, message, authenticated, cid)
            # Admin revokes while the next frame is already queued.
            await ws.disconnect_client(client_id, code=4001, reason="Client revoked")
            websocket._messages.append({"type": "get_areas"})
            websocket.application_state = WebSocketState.CONNECTED
            return result

        monkeypatch.setattr(handshake, "_handle_message", _handle_then_revoke)
        await ws.websocket_endpoint(fake)
        assert [m["type"] for m in fake.sent] == ["auth_required", "auth_ok"]
