"""Admin page only through Home Assistant unless direct access is switched on.

* Default off: ``POST /api/auth/login`` answers 403 ``admin_ingress_only``
  before the password reaches Home Assistant, no session counts, an
  unauthenticated admin request names the reason, ``/api/auth/session``
  reports ``direct_access: false`` with the Home Assistant address.
* ``PUT /api/settings/admin-direct-access`` only through Ingress (not with a
  session or the admin API token); switching off ends every direct session
  and closes its admin sockets.
* Ingress, the admin API token and the paths of apps stay as they are.
"""

import httpx
import pytest

import api.auth as auth
from api import ws as api_ws
from api.auth import hash_secret
from api.ws.registry import _clients

# auth.httpx is the httpx module: tests that fake the Supervisor call patch it.
_AsyncClient = httpx.AsyncClient
_ASGITransport = httpx.ASGITransport
INGRESS_IP = ("172.30.32.2", 40000)
LAN_IP = ("192.0.2.7", 50000)
UA = {"user-agent": "pytest-ua"}
INGRESS = {"x-ingress-path": "/api/hassio_ingress/tok"}


@pytest.fixture
async def app(db, monkeypatch):
    import main

    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.setattr(main.app.state, "db", db, raising=False)
    monkeypatch.setattr(main.app.state, "ha_client", None, raising=False)
    monkeypatch.setattr(main.app.state, "startup_error", None, raising=False)
    monkeypatch.setattr(auth, "_direct_admin_enabled", False)
    auth._sessions.clear()
    auth._rate_limits.clear()
    yield main.app
    auth._sessions.clear()
    auth._rate_limits.clear()


def _client(app, ip=LAN_IP, base="https://hub.local:8100"):
    return _AsyncClient(transport=_ASGITransport(app=app, client=ip), base_url=base)


def _session_cookie(token):
    return {"cookie": f"{auth.SESSION_COOKIE_NAME}={token}", **UA}


async def test_default_is_off(db):
    assert await auth.load_direct_admin_setting(db) is False
    assert auth.direct_admin_enabled() is False


async def test_login_refused_before_the_password_is_checked(app, monkeypatch):
    called = []

    class _NoHTTPX:
        def __init__(self, *a, **k):
            called.append(True)

    monkeypatch.setattr(auth.httpx, "AsyncClient", _NoHTTPX)
    async with _client(app) as c:
        r = await c.post("/api/auth/login", json={"username": "a", "password": "b"})
    assert r.status_code == 403
    assert r.json()["reason"] == auth.DIRECT_ADMIN_OFF_REASON
    assert called == [] and auth._sessions == {}


async def test_session_endpoint_points_to_home_assistant(app, db):
    await db.set_setting("ha_internal_url", "http://192.0.2.10:8123")
    await db.set_setting("ha_url", "https://remote.example")
    async with _client(app) as c:
        body = (await c.get("/api/auth/session")).json()
    assert body == {
        "authenticated": False,
        "direct_access": False,
        "ha_url": "http://192.0.2.10:8123",
    }


async def test_session_endpoint_without_known_address(app):
    async with _client(app) as c:
        body = (await c.get("/api/auth/session")).json()
    assert body["direct_access"] is False and body["ha_url"] is None


async def test_no_direct_session_counts_while_off(app):
    auth._direct_admin_enabled = True
    token = auth._create_session("admin", "pytest-ua")
    auth._direct_admin_enabled = False
    async with _client(app) as c:
        r = await c.get("/api/areas", headers=_session_cookie(token))
        bearer = await c.get(
            "/api/areas", headers={"authorization": f"Bearer {token}", **UA}
        )
        session = (
            await c.get("/api/auth/session", headers=_session_cookie(token))
        ).json()
    assert r.status_code == 401 and r.json()["reason"] == "admin_ingress_only"
    assert bearer.status_code == 401
    assert session["authenticated"] is False


async def test_admin_api_token_and_ingress_unaffected(app, db):
    await db.set_setting("api_token_hash", hash_secret("tok-123"))
    async with _client(app) as c:
        r = await c.get("/api/areas", headers={"authorization": "Bearer tok-123"})
    assert r.status_code == 200
    async with _client(app, ip=INGRESS_IP, base="http://hub.local:8099") as c:
        r = await c.get("/api/areas", headers=INGRESS)
        session = (await c.get("/api/auth/session", headers=INGRESS)).json()
    assert r.status_code == 200
    assert session == {"authenticated": True, "method": "ingress"}


async def test_health_stays_public(app):
    async with _client(app) as c:
        assert (await c.get("/api/health")).status_code in (200, 503)


async def test_switch_only_through_ingress(app, db):
    await db.set_setting("api_token_hash", hash_secret("tok-123"))
    async with _client(app) as c:
        r = await c.put(
            "/api/settings/admin-direct-access",
            json={"admin_direct_access": True},
            headers={"authorization": "Bearer tok-123"},
        )
        read = await c.get(
            "/api/settings/admin-direct-access",
            headers={"authorization": "Bearer tok-123"},
        )
    assert r.status_code == 403
    assert read.status_code == 200 and read.json() == {"admin_direct_access": False}
    assert await db.get_setting(auth.DIRECT_ADMIN_SETTING) is None

    async with _client(app, ip=INGRESS_IP, base="http://hub.local:8099") as c:
        r = await c.put(
            "/api/settings/admin-direct-access",
            json={"admin_direct_access": True},
            headers=INGRESS,
        )
    assert r.status_code == 200 and r.json() == {"admin_direct_access": True}
    assert await db.get_setting(auth.DIRECT_ADMIN_SETTING) == "true"
    assert auth.direct_admin_enabled() is True
    auth._direct_admin_enabled = False
    assert await auth.load_direct_admin_setting(db) is True

    async with _client(app) as c:
        body = (await c.get("/api/auth/session")).json()
    assert body == {"authenticated": False, "direct_access": True}


async def test_a_direct_session_cannot_switch(app, db):
    auth._direct_admin_enabled = True
    token = auth._create_session("admin", "pytest-ua")
    csrf = auth._sessions[token]["csrf_token"]
    async with _client(app) as c:
        r = await c.put(
            "/api/settings/admin-direct-access",
            json={"admin_direct_access": False},
            headers={**_session_cookie(token), "x-csrf-token": csrf},
        )
    assert r.status_code == 403
    assert auth.direct_admin_enabled() is True and token in auth._sessions


class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed = code


async def test_switching_off_ends_sessions_and_sockets(app, db):
    auth._direct_admin_enabled = True
    token = auth._create_session("admin", "pytest-ua")
    other = auth._create_session("admin2", "pytest-ua")
    session_ws, ingress_ws = _FakeSocket(), _FakeSocket()
    _clients[session_ws] = {"client_id": "admin", "session_token": token}
    _clients[ingress_ws] = {"client_id": "admin"}
    try:
        async with _client(app, ip=INGRESS_IP, base="http://hub.local:8099") as c:
            r = await c.put(
                "/api/settings/admin-direct-access",
                json={"admin_direct_access": False},
                headers=INGRESS,
            )
        assert r.status_code == 200 and r.json() == {"admin_direct_access": False}
        assert auth._sessions == {}
        assert auth._validate_session(other, "pytest-ua") is None
        assert session_ws.closed == api_ws.SESSION_EXPIRED_CLOSE_CODE
        assert session_ws.sent == [{"type": "session_expired"}]
        assert session_ws not in _clients
        assert ingress_ws.closed is None and ingress_ws in _clients
        assert await db.get_setting(auth.DIRECT_ADMIN_SETTING) == "false"
    finally:
        _clients.pop(session_ws, None)
        _clients.pop(ingress_ws, None)


async def test_login_works_again_when_switched_on(app, monkeypatch):
    class _Resp:
        status_code = 200

        def json(self):
            return {"result": "ok", "data": {"group_ids": ["system-admin"]}}

    class _HTTPX:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            return _Resp()

    monkeypatch.setattr(auth.httpx, "AsyncClient", _HTTPX)
    auth._direct_admin_enabled = True
    async with _client(app) as c:
        r = await c.post(
            "/api/auth/login", json={"username": "admin", "password": "pw"}, headers=UA
        )
        assert r.status_code == 200 and r.json()["success"] is True
        areas = await c.get("/api/areas", headers=UA)
    assert areas.status_code == 200


@pytest.mark.parametrize("path", auth.HEAD_ASSET_PATHS)
async def test_head_icons_are_public(app, path, monkeypatch, tmp_path):
    """Favicons, touch icon and manifest load without a session."""
    import main

    (tmp_path / path.lstrip("/")).write_bytes(b"x")
    (tmp_path / "index.html").write_text("<html></html>")
    monkeypatch.setattr(main, "static_dir", str(tmp_path))
    monkeypatch.setattr(main, "_spa_fallback", str(tmp_path / "index.html"))
    async with _client(app) as c:
        r = await c.get(path)
    assert r.status_code == 200 and r.content == b"x"
    if path.endswith(".webmanifest"):
        assert r.headers["content-type"].startswith("application/manifest+json")


async def test_head_icons_follow_the_ingress_path(app, monkeypatch, tmp_path):
    import main

    head = "".join(f'<link href="{p}">' for p in auth.HEAD_ASSET_PATHS)
    (tmp_path / "index.html").write_text(f"<html><head>{head}</head></html>")
    monkeypatch.setattr(main, "static_dir", str(tmp_path))
    monkeypatch.setattr(main, "_spa_fallback", str(tmp_path / "index.html"))
    async with _client(app, ip=INGRESS_IP, base="http://hub:8099") as c:
        html = (await c.get("/", headers=INGRESS)).text
    for p in auth.HEAD_ASSET_PATHS:
        assert f'href="/api/hassio_ingress/tok{p}"' in html
