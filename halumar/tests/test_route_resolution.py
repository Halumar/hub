"""Every route is reachable with an example path.

Routes are matched in registration order, so a literal path must be
registered before a parameterized one of the same shape (for example
PUT /areas/{id}/sections/order before PUT /areas/{id}/sections/{section_id}).
This test builds an example path for every HTTP route of the real app
and asserts that, for every method, the app's router dispatches it to that
very route's endpoint.

The probe calls the router directly with a bare scope: routing records the
matched endpoint in the scope, and FastAPI stops before the endpoint runs
(no request stack in the scope), so no handler is executed.
"""

import asyncio
import re

import pytest
from starlette.routing import Mount, Route

from main import app

_PARAM = re.compile(r"\{([^}:]+)(?::[^}]+)?\}")


def _example_path(path: str) -> str:
    return _PARAM.sub(lambda m: f"ex-{m.group(1)}", path)


def _all_http_routes():
    """(full path, route) for every HTTP route, in registration order."""
    for route in app.router.routes:
        original = getattr(route, "original_router", None)
        if original is not None:  # FastAPI >= 0.13x: included router
            prefix = route.include_context.prefix or ""
            for sub in original.routes:
                if isinstance(sub, Route):
                    yield prefix + sub.path, sub
        elif isinstance(route, Route) and not isinstance(route, Mount):
            yield route.path, route


def _dispatch(path: str, method: str):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("192.0.2.1", 1),
        "server": ("test", 80),
        "app": app,
        "state": {},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        pass

    try:
        asyncio.run(app.router(scope, receive, send))
    except Exception:  # noqa: BLE001 - only the routing decision matters
        pass
    return scope.get("endpoint")


def _cases():
    for full_path, route in _all_http_routes():
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            yield pytest.param(full_path, route, method, id=f"{method} {full_path}")


CASES = list(_cases())


def test_enumerates_the_api():
    assert len(CASES) > 100


@pytest.mark.parametrize("full_path, route, method", CASES)
def test_route_resolves_to_itself(full_path, route, method):
    path = _example_path(full_path)
    endpoint = _dispatch(path, method)
    assert endpoint is route.endpoint, (
        f"{method} {path} is dispatched to "
        f"{getattr(endpoint, '__name__', endpoint)!r} instead of "
        f"{route.endpoint.__name__!r} ({full_path})"
    )


def test_section_order_route_is_reachable():
    from api.areas import reorder_sections

    assert _dispatch("/api/areas/a1/sections/order", "PUT") is reorder_sections
