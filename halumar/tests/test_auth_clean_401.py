"""AuthMiddleware.dispatch RETURNS a clean 401, it does not raise.

`AuthMiddleware` is a Starlette `BaseHTTPMiddleware`. An `HTTPException`
raised inside `dispatch` would not be routed through FastAPI's exception
handlers: it would propagate as an unhandled ASGI error (a 500 with a
traceback). So a rejected request, for example the HA integration polling
/api/integration/status with a stale token, gets a
`JSONResponse(status_code=401, ...)`.

This test drives `AuthMiddleware.dispatch` directly (the suite has no full-app
TestClient) with an invalid Bearer token to a protected /api/ path and asserts
that it RETURNS a 401 JSONResponse and does NOT raise.
"""

import asyncio

import pytest
from starlette.responses import Response

from api.auth import AuthMiddleware


class _State:
    # Truthy db so dispatch proceeds to the auth decision instead of the
    # startup 503 branch. _validate_token will return None for any token
    # because this stub exposes no matching settings/clients.
    class _DB:
        async def get_setting(self, key):
            return None

        async def fetchone(self, *args, **kwargs):
            return None

        async def fetchall(self, *args, **kwargs):
            return []

    db = _DB()


class _App:
    state = _State()


def _make_request(path, headers):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",  # HTTP middleware path (NOT "websocket")
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        # Untrusted public IP so the Ingress trust does not apply.
        "client": ("203.0.113.7", 54321),
        "server": ("testserver", 80),
        "scheme": "http",
        "app": _App(),
    }
    from starlette.requests import Request

    return Request(scope)


async def _call_next(request):
    # Sentinel "allowed through" response, returned only if auth let it pass.
    return "PASSED_THROUGH_200"


def _dispatch(path, headers):
    mw = AuthMiddleware(app=None)
    request = _make_request(path, headers)
    return asyncio.run(mw.dispatch(request, _call_next))


def test_invalid_bearer_returns_clean_401_without_raising():
    """Invalid Bearer token on a protected /api/ path returns a 401 response."""
    result = _dispatch("/api/areas", {"authorization": "Bearer totally-invalid-token"})
    assert isinstance(result, Response)
    assert result.status_code == 401
    # Not passed through to the app.
    assert result != "PASSED_THROUGH_200"


def test_invalid_bearer_on_integration_status_returns_clean_401():
    """The exact noisy case: integration polling with a stale token -> clean 401."""
    result = _dispatch(
        "/api/integration/status",
        {"authorization": "Bearer stale-integration-token"},
    )
    assert isinstance(result, Response)
    assert result.status_code == 401


def test_invalid_bearer_dispatch_does_not_raise():
    """dispatch must not raise HTTPException (which would become a 500 + traceback)."""
    try:
        _dispatch("/api/areas", {"authorization": "Bearer nope"})
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"dispatch raised instead of returning a response: {exc!r}")
