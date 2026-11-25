"""The hub never reads ``X-Forwarded-*`` for the client address or the scheme.

uvicorn's default (``proxy_headers`` on, ``forwarded_allow_ips`` 127.0.0.1)
would take the client address and the scheme from these headers for every
loopback peer, and with ``host_network`` every process on the host is one;
a forwarded address would pass for the Supervisor Ingress proxy. The hub
therefore runs uvicorn with ``proxy_headers`` off. The tests
load the app the way the server does (``uvicorn.Config(...).load()``), so the
middleware uvicorn itself adds is part of what is checked.
"""

import ast
import os

import httpx
import pytest
import uvicorn

import api.auth as auth
import main

SUPERVISOR = "172.30.32.2"
SPOOF = {"X-Ingress-Path": "/x", "X-Forwarded-For": SUPERVISOR}


@pytest.fixture
def served(db, monkeypatch):
    """The ASGI app exactly as a uvicorn server of the hub runs it."""
    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setattr(main.app.state, "db", db, raising=False)
    monkeypatch.setattr(main.app.state, "ha_client", None, raising=False)
    monkeypatch.setattr(main.app.state, "startup_error", None, raising=False)
    config = uvicorn.Config(
        main.app, host="0.0.0.0", port=8099, lifespan="off", **main.UVICORN_OPTIONS
    )
    config.load()
    return config.loaded_app


async def _call(served, peer, method, path, headers, **kwargs):
    transport = httpx.ASGITransport(app=served, client=(peer, 40000))
    async with httpx.AsyncClient(transport=transport, base_url="http://hub:8099") as c:
        return await c.request(method, path, headers=headers, **kwargs)


async def test_loopback_peer_does_not_become_ingress(served):
    r = await _call(served, "127.0.0.1", "GET", "/api/pairing/clients", SPOOF)
    assert r.status_code == 401
    r = await _call(served, "127.0.0.1", "POST", "/api/auth/token/regenerate", SPOOF)
    assert r.status_code == 401
    assert "api_token" not in r.text


async def test_lan_peer_does_not_become_ingress(served):
    r = await _call(served, "192.168.1.50", "GET", "/api/pairing/clients", SPOOF)
    assert r.status_code == 401


async def test_supervisor_proxy_itself_is_still_ingress(served):
    """Real Ingress traffic comes from 172.30.32.2 directly, no header needed."""
    r = await _call(
        served, SUPERVISOR, "GET", "/api/pairing/clients", {"X-Ingress-Path": "/x"}
    )
    assert r.status_code == 200


async def test_forwarded_proto_does_not_count_as_tls(served, direct_admin_on):
    """A plain connection stays plain: the HA login is refused before the
    password is looked at."""
    r = await _call(
        served,
        "127.0.0.1",
        "POST",
        "/api/auth/login",
        {"X-Forwarded-Proto": "https"},
        json={"username": "admin", "password": "secret"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == auth.TLS_REQUIRED_DETAIL


def test_every_uvicorn_server_gets_the_options():
    """main.py starts three servers (plain, and HTTP + TLS in dual mode);
    none of them may fall back to uvicorn's default."""
    assert main.UVICORN_OPTIONS["proxy_headers"] is False
    with open(os.path.abspath(main.__file__)) as f:
        tree = ast.parse(f.read())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "uvicorn"
        and node.func.attr in ("run", "Config")
    ]
    assert len(calls) == 3
    for call in calls:
        spread = [
            kw.value.id
            for kw in call.keywords
            if kw.arg is None and isinstance(kw.value, ast.Name)
        ]
        assert "UVICORN_OPTIONS" in spread, f"main.py:{call.lineno}"
