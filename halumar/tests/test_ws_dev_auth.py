"""Dev auto-auth only with HUB_DEV.

A missing api_token never grants admin. Dev auto-auth (which admits a fresh
connection as admin client_id="dev" with no credential) requires the explicit
HUB_DEV env var. Without it, a fresh external WS connection stays
UNAUTHENTICATED and must pair/auth normally.

``websocket_endpoint`` is driven end-to-end with a fake socket (same technique
as test_client_delete_reset.py); ``broadcast`` is mocked.
"""

import json
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

import api.websocket as ws


async def _async_noop(*_a, **_k):
    return None


async def _clear_api_token(db):
    """Remove the api_token_hash the db fixture seeds via _ensure_api_token,
    so the 'no token configured' path can be exercised."""
    await db.execute(
        "DELETE FROM settings WHERE key IN ('api_token', 'api_token_hash')"
    )
    await db.commit()


class _DrivableWS:
    """Drives websocket_endpoint: yields queued messages then disconnects.

    Uses a non-ingress source IP and no X-Ingress-Path header so the only path
    to authentication is dev auto-auth (or an explicit auth/pair message).
    """

    def __init__(self, db, messages=None, host="203.0.113.7"):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.headers: dict[str, str] = {}
        self.client = SimpleNamespace(host=host)
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self._messages = list(messages or [])

    async def accept(self):
        pass

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive_text(self):
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect()


class TestAuthDisabledEnvGate:
    """_auth_disabled must fail closed unless HUB_DEV is set."""

    async def test_no_env_no_token_is_not_disabled(self, db, monkeypatch):
        await _clear_api_token(db)
        monkeypatch.delenv("HUB_DEV", raising=False)
        assert await ws._auth_disabled(db) is False

    async def test_env_set_no_token_is_disabled(self, db, monkeypatch):
        await _clear_api_token(db)
        monkeypatch.setenv("HUB_DEV", "1")
        assert await ws._auth_disabled(db) is True

    async def test_env_set_but_token_present_is_not_disabled(self, db, monkeypatch):
        monkeypatch.setenv("HUB_DEV", "1")
        await db.set_setting("api_token_hash", "deadbeef")
        assert await ws._auth_disabled(db) is False

    async def test_no_env_even_with_token_is_not_disabled(self, db, monkeypatch):
        monkeypatch.delenv("HUB_DEV", raising=False)
        await db.set_setting("api_token_hash", "deadbeef")
        assert await ws._auth_disabled(db) is False


class TestFreshConnectionAutoAuth:
    """End-to-end: the fresh-connection auto-auth decision."""

    async def test_fresh_connection_not_auto_authenticated(self, db, monkeypatch):
        """No env + no token → connection is told to auth, never auto-authed."""
        await _clear_api_token(db)
        monkeypatch.delenv("HUB_DEV", raising=False)
        monkeypatch.setattr(ws, "broadcast", _async_noop)
        ws._clients.clear()

        fake = _DrivableWS(db)
        await ws.websocket_endpoint(fake)

        assert fake.sent, "expected at least one server message"
        assert fake.sent[0]["type"] == "auth_required"
        # Critically: never silently authenticated.
        assert all(m.get("type") != "auth_ok" for m in fake.sent)
        assert ws._clients == {}  # cleaned up on disconnect

    async def test_env_opt_in_enables_dev_auto_auth(self, db, monkeypatch):
        """With HUB_DEV set and no token, dev auto-auth still works."""
        await _clear_api_token(db)
        monkeypatch.setenv("HUB_DEV", "1")
        monkeypatch.setattr(ws, "broadcast", _async_noop)
        ws._clients.clear()

        fake = _DrivableWS(db)
        await ws.websocket_endpoint(fake)

        assert fake.sent[0]["type"] == "auth_ok"
        assert fake.sent[0]["client_id"] == "dev"
