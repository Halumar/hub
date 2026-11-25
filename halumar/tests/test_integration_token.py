"""Dedicated integration token: rotation, scoped auth, telemetry.

Contract under test:

1. /api/integration/* accepts EXACTLY the dedicated integration token
   (settings key ``integration_token_hash``): the admin API token, sessions,
   Ingress and paired-client secrets are rejected by require_integration, and
   the integration token itself opens nothing outside /api/integration/*.
2. rotate_integration_token mints a 64-hex secret, stores ONLY its SHA-256
   hash and invalidates the previous token.
3. Every authenticated integration request updates the in-memory telemetry
   (last_request_ts/request_count + X-Hub-Integration-Version header);
   401s on integration paths increment auth_failure_count.
4. GET /api/admin/integration-status returns the exact admin payload shape.
"""

import asyncio
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.auth import (
    AuthMiddleware,
    hash_secret,
    require_integration,
    rotate_integration_token,
)
from services import integration_status


@pytest.fixture(autouse=True)
def _reset_telemetry():
    """Isolate the module-level telemetry counters around each test."""
    integration_status.reset()
    yield
    integration_status.reset()


def _make_request(db, path, headers, scheme="http"):
    """Build a real starlette Request whose app.state.db is the test db."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        # Untrusted public IP so the Ingress trust does not apply.
        "client": ("203.0.113.7", 54321),
        "server": ("testserver", 80),
        "scheme": scheme,
        "app": SimpleNamespace(state=SimpleNamespace(db=db)),
    }
    return Request(scope)


async def _passed(request):
    return "PASSED_THROUGH"


async def _dispatch(db, path, token=None, scheme="http"):
    headers = {}
    if token:
        headers["authorization"] = f"Bearer {token}"
    mw = AuthMiddleware(app=None)
    return await mw.dispatch(_make_request(db, path, headers, scheme), _passed)


async def _dispatch_with_headers(db, path, headers):
    mw = AuthMiddleware(app=None)
    return await mw.dispatch(_make_request(db, path, headers), _passed)


# ── rotation ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_returns_64_hex_and_stores_only_hash(db):
    token = await rotate_integration_token(db)
    assert len(token) == 64
    int(token, 16)  # 64-hex secret

    stored = await db.get_setting("integration_token_hash")
    assert stored == hash_secret(token)
    # The plaintext is never persisted anywhere in settings.
    row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM settings WHERE value = ?", (token,)
    )
    assert row["cnt"] == 0


@pytest.mark.asyncio
async def test_rotate_invalidates_previous_token(db):
    old = await rotate_integration_token(db)
    new = await rotate_integration_token(db)
    assert old != new

    result = await _dispatch(db, "/api/integration/status", token=old)
    assert isinstance(result, Response) and result.status_code == 401
    passed = await _dispatch(db, "/api/integration/status", token=new)
    assert passed == "PASSED_THROUGH"


# ── middleware auth + scoping ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_integration_token_authenticates_integration_paths(db):
    token = await rotate_integration_token(db)
    passed = await _dispatch(db, "/api/integration/status", token=token)
    assert passed == "PASSED_THROUGH"


@pytest.mark.asyncio
async def test_integration_token_is_scoped_to_integration_paths(db):
    """The scoped token opens NOTHING outside /api/integration/*."""
    token = await rotate_integration_token(db)
    result = await _dispatch(db, "/api/areas", token=token)
    assert isinstance(result, Response)
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_admin_token_is_distinct_from_integration_token(db):
    """The admin API token passes the middleware but require_integration
    rejects it at the endpoint (auth_type 'admin' != 'integration')."""
    admin_token = "a" * 64
    await db.set_setting("api_token_hash", hash_secret(admin_token))
    await rotate_integration_token(db)

    # Middleware lets the admin token through over TLS (it is valid) ...
    passed = await _dispatch(
        db, "/api/integration/status", token=admin_token, scheme="https"
    )
    assert passed == "PASSED_THROUGH"
    # ... never over plain HTTP ...
    refused = await _dispatch(db, "/api/integration/status", token=admin_token)
    assert isinstance(refused, Response) and refused.status_code == 403
    # ... but the endpoint-level gate rejects every non-integration auth type.
    import fastapi

    for auth_type in ("admin", "ingress", "session", "client", None):
        request = SimpleNamespace(state=SimpleNamespace(auth_type=auth_type))
        with pytest.raises(fastapi.HTTPException) as exc:
            require_integration(request)
        assert exc.value.status_code == 403

    require_integration(SimpleNamespace(state=SimpleNamespace(auth_type="integration")))


# ── telemetry ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_requests_update_telemetry_and_version(db):
    token = await rotate_integration_token(db)

    await _dispatch_with_headers(
        db,
        "/api/integration/status",
        {
            "authorization": f"Bearer {token}",
            "x-hub-integration-version": "1.2.3",
        },
    )
    await _dispatch(db, "/api/integration/status", token=token)

    status = integration_status.get_status()
    assert status["connected"] is True
    assert status["request_count"] == 2
    assert status["auth_failure_count"] == 0
    # Header parsed; the last seen value is kept when the header is absent.
    assert status["integration_version"] == "1.2.3"
    assert status["last_request_ts"] is not None


@pytest.mark.asyncio
async def test_auth_failures_on_integration_paths_are_counted(db):
    await rotate_integration_token(db)

    r1 = await _dispatch(db, "/api/integration/status", token="wrong-token")
    r2 = await _dispatch(db, "/api/integration/status")  # no auth at all
    assert r1.status_code == 401 and r2.status_code == 401

    status = integration_status.get_status()
    assert status["auth_failure_count"] == 2
    assert status["request_count"] == 0
    assert status["connected"] is False

    # Failures elsewhere do NOT count as integration auth failures.
    await _dispatch(db, "/api/areas", token="wrong-token")
    assert integration_status.get_status()["auth_failure_count"] == 2


def test_connected_window_is_90_seconds(monkeypatch):
    integration_status.record_request("1.0.0")
    assert integration_status.get_status()["connected"] is True

    real_time = integration_status.time.time
    monkeypatch.setattr(integration_status.time, "time", lambda: real_time() + 91)
    assert integration_status.get_status()["connected"] is False


# ── admin endpoint ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_integration_status_shape(db):
    from api.admin import get_integration_status
    from version import VERSION

    integration_status.record_request("9.9.9")
    integration_status.record_auth_failure()

    request = SimpleNamespace(state=SimpleNamespace(auth_type="ingress"))
    result = await get_integration_status(request)

    assert set(result) == {
        "connected",
        "last_request_ts",
        "request_count",
        "auth_failure_count",
        "integration_version",
        "push_connected",
        "addon_version",
        "ha_version",
        "ha_domain",
    }
    assert result["connected"] is True
    # No HA client in this test: the HA version is unknown.
    assert result["ha_version"] is None
    # No WS push session in this test — telemetry-connected but not push.
    assert result["push_connected"] is False
    assert result["request_count"] == 1
    assert result["auth_failure_count"] == 1
    assert result["integration_version"] == "9.9.9"
    assert result["addon_version"] == VERSION
    assert result["ha_domain"] == "halumar"


async def test_admin_integration_status_reports_the_ha_version():
    """The HA version comes from GET /api/config (Supervisor proxy)."""
    from api.admin import get_integration_status

    class _HA:
        async def get_config(self):
            return {"version": "2026.11.2", "location_name": "Home"}

    request = SimpleNamespace(
        state=SimpleNamespace(auth_type="ingress"),
        app=SimpleNamespace(state=SimpleNamespace(ha_client=_HA())),
    )
    assert (await get_integration_status(request))["ha_version"] == "2026.11.2"


def test_admin_integration_status_requires_admin():
    import fastapi

    from api.admin import get_integration_status

    request = SimpleNamespace(state=SimpleNamespace(auth_type="client"))
    with pytest.raises(fastapi.HTTPException) as exc:
        asyncio.run(get_integration_status(request))
    assert exc.value.status_code == 403


# ── integration-token management endpoints ──────────────────────────────────


@pytest.mark.asyncio
async def test_token_status_and_regenerate_endpoints(db, monkeypatch):
    from api.auth import get_integration_token_status, regenerate_integration_token

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)

    def _admin_request():
        return SimpleNamespace(
            state=SimpleNamespace(auth_type="ingress"),
            app=SimpleNamespace(state=SimpleNamespace(db=db)),
            client=None,
        )

    status = await get_integration_token_status(_admin_request())
    assert status == {"has_token": False, "supervisor_managed": False}

    result = await regenerate_integration_token(_admin_request())
    token = result["integration_token"]
    assert len(token) == 64
    assert await db.get_setting("integration_token_hash") == hash_secret(token)

    status = await get_integration_token_status(_admin_request())
    assert status["has_token"] is True

    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token")
    status = await get_integration_token_status(_admin_request())
    assert status["supervisor_managed"] is True
