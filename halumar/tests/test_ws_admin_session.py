"""Admin web UI over the WebSocket outside Ingress.

* A valid session cookie on a TLS handshake whose ``Origin`` matches the
  ``Host`` authenticates the socket as admin session ``"admin"`` (no
  Cross-Site WebSocket Hijacking with the SameSite=Lax cookie).
* Admin sessions are no clients: not counted, no client_connected events, but
  they receive ADMIN broadcasts.
* When the session ends (expiry, eviction, logout) the socket gets
  ``session_expired`` and close code 4401; the UI returns to its login.
* Pairing base URL derived for Ingress is the TLS URL.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

import api.auth as auth
import api.websocket as ws
from api.ws import handshake
from services import protocol, tls
from services.pairing_link import resolve_base_url

# Direct admin access is off by default; these tests are about the direct login.
pytestmark = pytest.mark.usefixtures("direct_admin_on")

UA = "Mozilla/5.0 test"
HOST = "10.0.0.5:8100"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HUB_DEV", raising=False)
    ws._clients.clear()
    auth._sessions.clear()
    yield
    ws._clients.clear()
    auth._sessions.clear()


class _WS:
    def __init__(
        self,
        db,
        *,
        cookie=None,
        origin=f"https://{HOST}",
        scheme="wss",
        messages=(),
        block=False,
    ):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.headers = {"host": HOST, "user-agent": UA}
        if origin:
            self.headers["origin"] = origin
        self.cookies = {auth.SESSION_COOKIE_NAME: cookie} if cookie else {}
        self.client = SimpleNamespace(host="10.0.0.9")
        self.url = SimpleNamespace(hostname="10.0.0.5", port=8100, scheme=scheme)
        self.sent = []
        self.closed = None
        self.application_state = WebSocketState.CONNECTING
        self._messages = list(messages)
        self._block = block

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
        if self._messages:
            return json.dumps(self._messages.pop(0))
        if self._block:
            await asyncio.sleep(3600)
        raise WebSocketDisconnect()


def _session():
    return auth._create_session("admin-user", UA)


class TestHandshake:
    async def test_valid_cookie_same_origin_is_admin_session(self, db):
        fake = _WS(db, cookie=_session(), messages=[{"type": "ping"}])
        await ws.websocket_endpoint(fake)
        assert fake.sent[0] == {
            "type": "auth_ok",
            "client_id": "admin",
            **protocol.protocol_fields(),
        }
        assert fake.sent[1] == {"type": "pong"}

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"origin": "https://evil.example"},
            {"origin": None},
            {"origin": "http://10.0.0.5:8099", "scheme": "ws"},
            {"scheme": "ws", "origin": f"http://{HOST}"},
        ],
    )
    async def test_refused_without_tls_or_same_origin(self, db, kwargs):
        fake = _WS(db, cookie=_session(), **kwargs)
        await ws.websocket_endpoint(fake)
        assert fake.sent[0]["type"] == "auth_required"

    async def test_invalid_cookie_needs_auth(self, db):
        fake = _WS(db, cookie="nope")
        await ws.websocket_endpoint(fake)
        assert fake.sent[0]["type"] == "auth_required"

    async def test_admin_session_is_no_client_but_gets_admin_events(self, db):
        token = _session()
        fake = _WS(db, cookie=token, block=True)
        task = asyncio.create_task(ws.websocket_endpoint(fake))
        await asyncio.sleep(0.05)
        try:
            assert ws.get_connected_client_ids() == set()
            assert ws.get_connected_count() == 0
            await ws.broadcast(
                {"type": "pairing_cancelled", "data": {}},
                audience=ws.EventAudience.ADMIN,
            )
            assert {"type": "pairing_cancelled", "data": {}} in fake.sent
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


class TestSessionEnd:
    async def test_expired_session_closes_4401(self, db, monkeypatch):
        monkeypatch.setattr(handshake, "SESSION_RECHECK_SECONDS", 0.05)
        token = _session()
        fake = _WS(db, cookie=token, block=True)
        task = asyncio.create_task(ws.websocket_endpoint(fake))
        await asyncio.sleep(0.05)
        auth._sessions[token]["expires_at"] = time.time() - 1
        await asyncio.wait_for(task, 2)
        assert fake.closed == (4401, "Session expired")
        assert {"type": "session_expired"} in fake.sent
        assert fake not in ws._clients

    async def test_logout_closes_live_sockets(self, db):
        token = _session()
        fake = _WS(db, cookie=token, block=True)
        task = asyncio.create_task(ws.websocket_endpoint(fake))
        await asyncio.sleep(0.05)
        req = SimpleNamespace(
            cookies={auth.SESSION_COOKIE_NAME: token},
            url=SimpleNamespace(scheme="https"),
            headers={
                "x-csrf-token": auth._sessions[token]["csrf_token"],
                "user-agent": UA,
            },
        )
        await auth.logout(req)
        assert fake.closed == (4401, "Session expired")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert token not in auth._sessions


class TestPairingUrlTls:
    def _ingress_request(self, db):
        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(app_url="")),
            headers={"x-ingress-path": "/api/hassio_ingress/abc", "host": "ha:8123"},
            client=SimpleNamespace(host="172.30.32.2"),
            url=SimpleNamespace(scheme="http"),
        )

    async def test_ingress_gets_tls_url(self, db, monkeypatch):
        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "ab" * 32)
        await db.set_setting("ha_internal_url", "http://10.0.0.5:8123")
        url = await resolve_base_url(self._ingress_request(db), db)
        assert url == f"https://10.0.0.5:{tls.TLS_PORT}"

    async def test_plain_only_without_certificate(self, db, monkeypatch):
        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: None)
        await db.set_setting("ha_internal_url", "http://10.0.0.5:8123")
        url = await resolve_base_url(self._ingress_request(db), db)
        assert url == "http://10.0.0.5:8099"
