"""A plain HTTP request with an "Upgrade: websocket" header goes through normal
authentication; only a real ASGI websocket scope skips the HTTP middleware.

Genuine WebSocket connections arrive via the ASGI "websocket" scope and never
reach the BaseHTTPMiddleware HTTP path.

This test drives AuthMiddleware.dispatch directly (the suite has no full-app
TestClient) and asserts that an unauthenticated request to a protected /api/
path carrying `Upgrade: websocket` is rejected rather than passed through.
"""

import asyncio

from starlette.responses import Response

from api.auth import AuthMiddleware


class _State:
    # Truthy db so dispatch proceeds to the auth decision instead of the
    # startup 503 branch.
    db = object()


class _App:
    state = _State()


def _make_request(path, headers):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",  # NOT "websocket" — this is the HTTP middleware path
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
    # Starlette Request reads `app` from scope; import lazily to mirror usage.
    from starlette.requests import Request

    return Request(scope)


async def _call_next(request):
    # Sentinel "allowed through" response, returned only if auth let it pass.
    return "PASSED_THROUGH_200"


def _dispatch(path, headers):
    mw = AuthMiddleware(app=None)
    request = _make_request(path, headers)
    return asyncio.run(mw.dispatch(request, _call_next))


def test_upgrade_header_does_not_skip_auth():
    """Unauthenticated GET /api/areas with `Upgrade: websocket` is rejected.

    The rejection is a RETURNED 401 JSONResponse (raising inside
    BaseHTTPMiddleware would surface as a 500 with a traceback), and the
    request is not passed through.
    """
    result = _dispatch("/api/areas", {"upgrade": "websocket"})
    assert isinstance(result, Response)
    assert result.status_code == 401
    assert result != "PASSED_THROUGH_200"


def test_no_auth_without_header_still_rejected_control():
    """Control: same protected path without the header is also rejected (401)."""
    result = _dispatch("/api/areas", {})
    assert isinstance(result, Response)
    assert result.status_code == 401
    assert result != "PASSED_THROUGH_200"
