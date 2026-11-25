"""Web login only for HA admins, only over TLS; audit.

* ``POST /api/auth/login`` over plain HTTP (:8099 outside Ingress) is refused
  before the password is even checked.
* Valid HA credentials of a non-admin are refused (403). Admin status comes
  from the Supervisor ``/auth`` ``group_ids`` when reported, otherwise from
  HA's user list; failing to determine it fails closed (503).
* Sessions and the admin API token are only accepted over TLS.
* Admin requests (Ingress, session, admin token) are audited.
"""

import logging
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from fastapi import HTTPException
from starlette.requests import Request

import api.auth as auth
from api.auth import AuthMiddleware, LoginRequest, hash_secret

# Direct admin access is off by default; these tests are about the direct login.
pytestmark = pytest.mark.usefixtures("direct_admin_on")


class _Resp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeHTTPX:
    response = _Resp(200, {"result": "ok", "data": {}})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        return _FakeHTTPX.response


class _FakeHA:
    _token = "sup-token"

    def __init__(self, users=None, fail=False):
        self.users = users or []
        self.fail = fail
        self.commands = []

    async def _ws_command(self, command):
        self.commands.append(command)
        if self.fail:
            raise RuntimeError("HA unreachable")
        return self.users


USERS = [
    {
        "username": "admin",
        "is_owner": False,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-admin"],
    },
    {
        "username": "kiosk",
        "is_owner": False,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-users"],
    },
    {
        "username": "owner",
        "is_owner": True,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-users"],
    },
    {
        "username": "disabled",
        "is_owner": False,
        "is_active": False,
        "system_generated": False,
        "group_ids": ["system-admin"],
    },
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.setattr(auth.httpx, "AsyncClient", _FakeHTTPX)
    _FakeHTTPX.response = _Resp(200, {"result": "ok", "data": {}})
    auth._rate_limits.clear()
    auth._login_failures.clear()
    auth._sessions.clear()
    yield
    auth._rate_limits.clear()
    auth._login_failures.clear()
    auth._sessions.clear()


def _request(
    *,
    ha=None,
    db=None,
    scheme="https",
    method="POST",
    path="/api/auth/login",
    headers=None,
    client=("192.0.2.5", 5555),
):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw,
        "client": client,
        "server": ("testserver", 8100),
        "scheme": scheme,
        "app": SimpleNamespace(state=SimpleNamespace(ha_client=ha, db=db)),
    }
    return Request(scope)


async def _login(username, *, ha=None, scheme="https"):
    return await auth.login(
        _request(ha=ha or _FakeHA(USERS), scheme=scheme),
        LoginRequest(username=username, password="pw"),
    )


class TestLogin:
    async def test_plain_http_refused_before_password_check(self, monkeypatch):
        called = []

        class _Spy(_FakeHTTPX):
            async def post(self, *a, **k):
                called.append(True)
                return _Resp(200)

        monkeypatch.setattr(auth.httpx, "AsyncClient", _Spy)
        with pytest.raises(HTTPException) as exc:
            await _login("admin", scheme="http")
        assert exc.value.status_code == 403
        assert "8100" in exc.value.detail
        assert not called

    async def test_admin_from_user_list(self):
        resp = await _login("admin")
        assert resp.status_code == 200
        cookie = resp.headers["set-cookie"]
        assert "hub_session=" in cookie and "Secure" in cookie

    async def test_non_admin_refused(self):
        with pytest.raises(HTTPException) as exc:
            await _login("kiosk")
        assert exc.value.status_code == 403
        assert not auth._sessions

    async def test_owner_admitted_and_username_normalized(self):
        assert (await _login("  Owner ")).status_code == 200

    async def test_inactive_or_unknown_user_refused(self):
        for name in ("disabled", "ghost"):
            with pytest.raises(HTTPException) as exc:
                await _login(name)
            assert exc.value.status_code == 403

    async def test_group_ids_from_supervisor_win(self):
        ha = _FakeHA(fail=True)  # user list not needed
        _FakeHTTPX.response = _Resp(
            200, {"result": "ok", "data": {"group_ids": ["system-admin"]}}
        )
        assert (await _login("kiosk", ha=ha)).status_code == 200
        assert ha.commands == []
        _FakeHTTPX.response = _Resp(
            200, {"result": "ok", "data": {"group_ids": ["system-users"]}}
        )
        with pytest.raises(HTTPException) as exc:
            await _login("admin", ha=ha)
        assert exc.value.status_code == 403

    async def test_admin_check_failure_fails_closed(self):
        with pytest.raises(HTTPException) as exc:
            await _login("admin", ha=_FakeHA(fail=True))
        assert exc.value.status_code == 503
        assert not auth._sessions

    async def test_wrong_password_still_401(self):
        _FakeHTTPX.response = _Resp(401)
        with pytest.raises(HTTPException) as exc:
            await _login("admin")
        assert exc.value.status_code == 401

    async def test_one_address_cannot_lock_out_the_account(self):
        """Failures from one LAN host block only that host."""
        _FakeHTTPX.response = _Resp(401)
        attacker = ("192.0.2.66", 1)
        for _ in range(auth.LOGIN_FAILURES_PER_IP):
            with pytest.raises(HTTPException) as exc:
                await auth.login(
                    _request(ha=_FakeHA(USERS), client=attacker),
                    LoginRequest(username="admin", password="x"),
                )
            assert exc.value.status_code == 401
            auth._rate_limits.clear()  # only the failure counter matters here
        with pytest.raises(HTTPException) as exc:
            await auth.login(
                _request(ha=_FakeHA(USERS), client=attacker),
                LoginRequest(username="ADMIN", password="x"),
            )
        assert exc.value.status_code == 429
        # The real admin from another address still gets through to HA.
        with pytest.raises(HTTPException) as exc:
            await auth.login(
                _request(ha=_FakeHA(USERS), client=("192.0.2.10", 1)),
                LoginRequest(username="admin", password="x"),
            )
        assert exc.value.status_code == 401

    async def test_account_blocked_only_from_many_addresses(self):
        # 20 failures from 5 addresses: below the account limit.
        for i in range(5):
            for _ in range(4):
                auth._record_login_failure("admin", f"192.0.2.{i}")
        assert auth._login_blocked("admin", "192.0.2.200") is False
        # 30+ failures from many addresses: the account is blocked.
        for i in range(5):
            for _ in range(2):
                auth._record_login_failure("admin", f"198.51.100.{i}")
        assert auth._login_blocked("admin", "192.0.2.200") is True


async def _passed(request):
    return "PASSED"


class TestTransportAndAudit:
    async def test_session_cookie_only_over_tls(self, db):
        token = auth._create_session("admin", "ua")
        csrf = auth._sessions[token]["csrf_token"]
        headers = {
            "cookie": f"hub_session={token}",
            "user-agent": "ua",
            "x-csrf-token": csrf,
        }
        mw = AuthMiddleware(app=None)
        plain = await mw.dispatch(
            _request(
                db=db, scheme="http", method="GET", path="/api/areas", headers=headers
            ),
            _passed,
        )
        assert plain.status_code == 401
        ok = await mw.dispatch(
            _request(db=db, method="GET", path="/api/areas", headers=headers), _passed
        )
        assert ok == "PASSED"

    async def test_session_bearer_refused_over_plain(self, db):
        token = auth._create_session("admin", "")
        mw = AuthMiddleware(app=None)
        res = await mw.dispatch(
            _request(
                db=db,
                scheme="http",
                method="GET",
                path="/api/areas",
                headers={"authorization": f"Bearer {token}"},
            ),
            _passed,
        )
        assert res.status_code == 403

    async def test_client_secret_is_no_rest_credential(self, db):
        secret = "client-secret-value"
        await add_client(
            db, client_id="c1", name="C", secret_hash=hash_secret(secret), last_seen="x"
        )
        mw = AuthMiddleware(app=None)
        res = await mw.dispatch(
            _request(
                db=db,
                scheme="http",
                method="GET",
                path="/api/areas",
                headers={"authorization": f"Bearer {secret}"},
            ),
            _passed,
        )
        assert res.status_code == 401

    async def test_ingress_admin_requests_audited(self, db, caplog, monkeypatch):
        monkeypatch.setattr(auth, "_is_trusted_ingress", lambda request: True)
        mw = AuthMiddleware(app=None)
        caplog.set_level(logging.INFO, logger="hub.security")
        headers = {
            "x-ingress-path": "/api/hassio_ingress/x",
            "x-remote-user-name": "manager_one",
        }
        await mw.dispatch(
            _request(
                db=db,
                scheme="http",
                method="DELETE",
                path="/api/pairing/clients/c1",
                headers=headers,
                client=("172.30.32.2", 1),
            ),
            _passed,
        )
        await mw.dispatch(
            _request(
                db=db,
                scheme="http",
                method="GET",
                path="/api/areas",
                headers=headers,
                client=("172.30.32.2", 1),
            ),
            _passed,
        )
        await mw.dispatch(
            _request(
                db=db,
                scheme="http",
                method="GET",
                path="/api/auth/integration-token",
                headers=headers,
                client=("172.30.32.2", 1),
            ),
            _passed,
        )
        lines = [
            r.getMessage() for r in caplog.records if "ADMIN_ACCESS" in r.getMessage()
        ]
        assert len(lines) == 2
        assert (
            "method=DELETE path=/api/pairing/clients/c1 auth=ingress user=manager_one"
            in lines[0]
        )
        assert "ip=172.30.32.2" in lines[0]
        assert "path=/api/auth/integration-token" in lines[1]
