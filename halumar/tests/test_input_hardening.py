"""Input limits and malformed input from unauthenticated peers.

The body limit applies to chunked bodies as well as to ``Content-Length``;
WebSocket messages are capped below uvicorn's default. A JSON frame that is
no object, an ``auth`` whose secret is no string and a ``pair`` with a
non-ASCII PIN get an error answer instead of raising. start-pairing reads
chunked bodies, so the requested kind (member or guest) is always honoured.
"""

import json
import types

import httpx
import pytest
import uvicorn

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
import main


@pytest.fixture
def app(db, monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_SIZE", 1024)
    main.app.state.db = db
    main.app.state.ha_client = None
    main.app.state.startup_error = None
    return main.app


def _client(app):
    transport = httpx.ASGITransport(app=app, client=("192.0.2.10", 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://hub")


# ── Body limit ─────────────────────────────────────────────────────────────
#
# main.SMALL_BODY_LIMIT without credentials, AUTHENTICATED_BODY_LIMIT with
# them, MAX_REQUEST_BODY_SIZE only for an admin on LARGE_BODY_ROUTES; the
# whole body within SMALL_/LARGE_BODY_SECONDS. Patched small here.

INGRESS = {"X-Ingress-Path": "/x"}


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(main, "SMALL_BODY_LIMIT", 1024)
    monkeypatch.setattr(main, "AUTHENTICATED_BODY_LIMIT", 8 * 1024)
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_SIZE", 64 * 1024)
    monkeypatch.setattr(main, "IMPORT_BODY_LIMIT", 64 * 1024)


def _ingress_client(app):
    transport = httpx.ASGITransport(app=app, client=("172.30.32.2", 40000))
    return httpx.AsyncClient(transport=transport, base_url="http://hub")


def _chunks(total, size=256, sent=None):
    async def gen():
        for _ in range(total // size):
            if sent is not None:
                sent.append(1)
            yield b"x" * size

    return gen()


async def test_unauthenticated_chunked_body_stops_at_the_small_limit(app, limits):
    sent = []
    async with _client(app) as c:
        r = await c.post(
            "/api/auth/login",
            content=_chunks(64 * 1024, sent=sent),
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 413
    assert "content-length" not in r.request.headers  # really chunked
    assert len(sent) < 10  # stopped near 1 KiB, not read to the end


async def test_unauthenticated_large_route_gets_the_small_limit(app, limits):
    """Before the auth check the route alone opens no large body."""
    async with _client(app) as c:
        r = await c.post(
            "/api/import/areas",
            content=_chunks(32 * 1024),
            headers={"content-type": "application/json"},
        )
    assert r.status_code in (401, 413)
    assert r.status_code != 200


async def test_declared_length_over_the_limit_is_refused(app, limits):
    async with _client(app) as c:
        r = await c.post(
            "/api/auth/login",
            content=b"{" + b" " * 2000 + b"}",
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 413


async def test_small_chunked_body_is_still_read(app, limits):
    async def chunks():
        yield b'{"username": "x", '
        yield b'"password": "y"}'

    async with _client(app) as c:
        r = await c.post(
            "/api/auth/login",
            content=chunks(),
            headers={"content-type": "application/json"},
        )
    assert r.status_code not in (408, 413, 500)


async def test_admin_large_route_takes_a_large_body(app, limits, monkeypatch):
    """An import of 40 KiB (chunked) passes for the admin; the same body on
    a plain JSON route is over the authenticated limit."""
    import api.export as export

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(export, "sync_all_sensors", _noop)
    areas = [{"name": f"Raum {i}", "description": "d" * 300} for i in range(100)]
    body = json.dumps({"areas": areas}).encode()
    assert 32 * 1024 < len(body) < 64 * 1024

    async def gen():
        for i in range(0, len(body), 4096):
            yield body[i : i + 4096]

    async with _ingress_client(app) as c:
        r = await c.post(
            "/api/import/areas",
            content=gen(),
            headers={"content-type": "application/json", **INGRESS},
        )
        assert r.status_code == 200, r.text
        assert r.json()["imported"] == 100
        r = await c.put(
            "/api/areas/order",
            content=b'{"area_ids": [' + b'"x", ' * 3000 + b'"x"]}',
            headers={"content-type": "application/json", **INGRESS},
        )
        assert r.status_code == 413


async def test_slow_body_is_cut_off(app, limits, monkeypatch):
    import asyncio

    monkeypatch.setattr(main, "SMALL_BODY_SECONDS", 0.3)

    async def drip():
        for _ in range(20):
            await asyncio.sleep(0.1)
            yield b" "

    async with _client(app) as c:
        r = await c.post(
            "/api/auth/login",
            content=drip(),
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 408


def test_body_limit_by_route_and_credentials():
    scope = {"path": "/api/import/areas", "state": {}}
    assert main.body_limit(scope)[0] == main.SMALL_BODY_LIMIT
    scope["state"]["auth_type"] = "client"
    assert main.body_limit(scope)[0] == main.AUTHENTICATED_BODY_LIMIT
    for auth in ("ingress", "admin", "session"):
        scope["state"]["auth_type"] = auth
        assert main.body_limit(scope) == (
            main.IMPORT_BODY_LIMIT,
            main.LARGE_BODY_SECONDS,
        )
    for path in (
        "/api/areas/a1/image/card",
        "/api/areas/a1/image/bg-light",
        "/api/settings/home/image/bg_dark",
    ):
        assert main.body_limit({"path": path, "state": {"auth_type": "ingress"}})[
            0
        ] == (main.MAX_REQUEST_BODY_SIZE)
    assert (
        main.body_limit({"path": "/api/areas", "state": {"auth_type": "ingress"}})[0]
        == main.AUTHENTICATED_BODY_LIMIT
    )


def test_websocket_frames_are_capped_at_the_server():
    from api.ws.handshake import MAX_WS_MESSAGE_SIZE

    # log_config=None: leave the logging of the other tests alone.
    config = uvicorn.Config(main.app, log_config=None, **main.UVICORN_OPTIONS)
    assert config.ws_max_size == 4 * MAX_WS_MESSAGE_SIZE
    assert config.ws_max_size < 16 * 1024 * 1024


# ── Malformed WebSocket frames ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '"x"',
        "5",
        "null",
        '{"type": 5}',
        '{"type": ["auth"]}',
        '{"type": "auth", "secret": 123}',
        '{"type": "auth", "secret": {"a": 1}}',
        '{"type": "auth", "secret": null}',
    ],
)
def test_malformed_frame_gets_an_error_and_the_socket_stays(db, payload):
    from starlette.testclient import TestClient

    main.app.state.db = db
    main.app.state.ha_client = None
    main.app.state.startup_error = None
    with TestClient(main.app).websocket_connect("/api/ws") as sock:
        assert sock.receive_json()["type"] == "auth_required"
        sock.send_text(payload)
        reply = sock.receive_json()
        assert reply["type"] in ("error", "auth_error"), reply
        # Still served: a ping gets its pong.
        sock.send_text(json.dumps({"type": "ping"}))
        assert sock.receive_json()["type"] in ("pong", "auth_required")


def _ws(db):
    from starlette.testclient import TestClient

    main.app.state.db = db
    main.app.state.ha_client = None
    main.app.state.startup_error = None
    return TestClient(main.app).websocket_connect("/api/ws")


@pytest.mark.parametrize(
    "send",
    [
        lambda s: s.send_bytes(b"\x00\x01\x02"),
        lambda s: s.send_text("[" * 60000),  # RecursionError in json.loads
        lambda s: s.send_text('{"a":' * 5000 + "1" + "}" * 5000),
    ],
    ids=["binary", "deep-list", "deep-object"],
)
def test_binary_and_deeply_nested_frames_get_an_error(db, send):
    """These raised out of the handler (KeyError
    'text', RecursionError) and the server dropped the connection."""
    with _ws(db) as sock:
        assert sock.receive_json()["type"] == "auth_required"
        send(sock)
        # error; a nested object the parser still reads is just no command
        assert sock.receive_json()["type"] in ("error", "auth_required")
        sock.send_text(json.dumps({"type": "ping"}))
        assert sock.receive_json()["type"] in ("pong", "auth_required")


def test_invalid_frames_count_against_a_budget_before_auth(db):
    from starlette.websockets import WebSocketDisconnect

    from api.ws.handshake import INVALID_FRAMES_CLOSE_CODE, MAX_INVALID_FRAMES

    with _ws(db) as sock:
        sock.receive_json()
        for i in range(MAX_INVALID_FRAMES):
            (sock.send_bytes if i % 2 else sock.send_text)(b"x" if i % 2 else "[")
            assert sock.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            sock.receive_json()
        assert closed.value.code == INVALID_FRAMES_CLOSE_CODE


# ── start-pairing reads a chunked body ────────────────────────────────────


@pytest.fixture
def clean_pins(monkeypatch):
    pairing.clear_active_pins()

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(ws, "broadcast", _noop)
    yield
    pairing.clear_active_pins()


async def test_start_pairing_reads_a_body_without_content_length(db, clean_pins):
    raw = json.dumps({"is_guest": True, "guest_hours": 2}).encode()

    async def _body():
        return raw

    request = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url="")
        ),
        headers={"transfer-encoding": "chunked"},
        body=_body,
    )
    await apiintg.integration_start_pairing(request)
    _pin, data = pairing.get_active_pin()
    assert data.get("is_guest") is True


async def test_start_pairing_over_http_with_a_chunked_guest_body(app, clean_pins):
    from api import auth

    async def chunks():
        yield b'{"is_guest": true, '
        yield b'"guest_hours": 2}'

    token = await auth.rotate_integration_token(app.state.db)
    async with _client(app) as c:
        r = await c.post(
            "/api/integration/start-pairing",
            content=chunks(),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
            },
        )
    assert r.status_code == 200, r.text
    _pin, data = pairing.get_active_pin()
    assert data.get("is_guest") is True


async def test_start_pairing_deeply_nested_body_is_400(db, clean_pins):
    from fastapi import HTTPException

    raw = b"[" * 60000

    async def _body():
        return raw

    request = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url="")
        ),
        headers={},
        body=_body,
    )
    with pytest.raises(HTTPException) as exc:
        await apiintg.integration_start_pairing(request)
    assert exc.value.status_code == 400


# ── non-ASCII PIN ─────────────────────────────────────────────────────


def test_non_ascii_pin_is_no_match_and_raises_nothing():
    pairing.set_active_pin("1234-5678", {"created_at": 0, "expires_at": 9e12})
    try:
        assert pairing.match_pin("١٢٣٤-٥٦٧٨") is None
        assert pairing.match_pin("1234-5678") is not None
        assert pairing.retired_pin_reason("١٢٣٤-٥٦٧٨") is None
    finally:
        pairing.clear_active_pins()


def test_non_ascii_header_values_do_not_raise(direct_admin_on):
    """Like the PIN: session cookie, User-Agent and Origin are compared
    with compare_digest too; a header decoded as Latin-1 can hold any
    character up to U+00FF."""
    from api import auth
    from api.ws import handshake

    token = auth._create_session("admin", user_agent="Browser")
    try:
        assert auth._validate_session("äöü") is None
        assert auth._validate_session(token, user_agent="Bröwser") is None
    finally:
        auth._destroy_session(token)
    sock = types.SimpleNamespace(
        headers={"origin": "http://hüb", "host": "hub"},
        url=types.SimpleNamespace(scheme="ws"),
    )
    assert handshake._same_origin(sock) is False
