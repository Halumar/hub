"""Tests for Ingress source-IP trust.

Both api/auth.py::_is_trusted_ingress (REST) and api/websocket.py::_is_ingress
(WebSocket) delegate to services/ingress.py and must:
- trust ONLY the Supervisor Ingress proxy 172.30.32.2, and only when the
  X-Ingress-Path header is present,
- reject every other hassio address (other add-ons: 172.30.32.1, 172.30.33.x),
  loopback (host processes under host_network), LAN IPs, unparseable hosts
  and a missing header,
- trust loopback and the hassio network again only with the explicit
  HUB_DEV flag (default off).

A spoofed header must also not reach the HTML rewrite of IngressMiddleware.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from api.auth import AuthMiddleware, _is_trusted_ingress, _ip_is_trusted
from api.websocket import _is_ingress
from api.websocket import _ip_is_trusted as _ws_ip_is_trusted
from services import ingress


@pytest.fixture(autouse=True)
def _no_dev_flag(monkeypatch):
    monkeypatch.delenv("HUB_DEV", raising=False)


def _conn(host, ingress_hdr=True):
    """Build a Request/WebSocket-shaped object (both read the same attrs)."""
    headers = {"x-ingress-path": "/abc"} if ingress_hdr else {}
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=host) if host is not None else None,
    )


SUPERVISOR = "172.30.32.2"
# Every other hassio / loopback address: trusted only with HUB_DEV.
DEV_ONLY_IPS = [
    "172.30.32.1",
    "172.30.32.5",
    "172.30.33.1",
    "172.30.33.254",
    "127.0.0.1",
    "127.5.4.3",
    "::1",
]
# Never trusted, not even with the dev flag.
UNTRUSTED_IPS = [
    "172.30.34.1",
    "172.30.31.255",
    "::10",
    "::1a",
    "192.168.1.5",
    "198.51.100.178",
    "8.8.8.8",
    "fe80::1",
    "not-an-ip",
    "",
]


class TestIpIsTrusted:
    def test_only_supervisor_proxy_trusted_by_default(self):
        assert _ip_is_trusted(SUPERVISOR) is True
        assert _ws_ip_is_trusted(SUPERVISOR) is True
        assert _ip_is_trusted("::ffff:172.30.32.2") is True

    def test_other_hassio_and_loopback_rejected_by_default(self):
        for ip in DEV_ONLY_IPS + UNTRUSTED_IPS:
            assert _ip_is_trusted(ip) is False, ip
            assert _ws_ip_is_trusted(ip) is False, ip

    def test_dev_flag_trusts_loopback_and_hassio_network(self, monkeypatch):
        monkeypatch.setenv("HUB_DEV", "1")
        for ip in DEV_ONLY_IPS + [SUPERVISOR]:
            assert _ip_is_trusted(ip) is True, ip
            assert _ws_ip_is_trusted(ip) is True, ip
        for ip in UNTRUSTED_IPS:
            assert _ip_is_trusted(ip) is False, ip

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    def test_dev_flag_false_values_stay_off(self, monkeypatch, value):
        monkeypatch.setenv("HUB_DEV", value)
        assert ingress.dev_mode() is False
        assert _ip_is_trusted("127.0.0.1") is False

    def test_ipv6_prefix_is_not_a_match(self, monkeypatch):
        monkeypatch.setenv("HUB_DEV", "1")
        assert _ip_is_trusted("::10") is False
        assert _ip_is_trusted("::1a") is False
        assert _ip_is_trusted("::1") is True


class TestAuthIsTrustedIngress:
    """api.auth._is_trusted_ingress (HTTP)."""

    def test_supervisor_with_header_trusted(self):
        assert _is_trusted_ingress(_conn(SUPERVISOR)) is True

    def test_spoofed_header_from_addons_and_host_rejected(self):
        # An SSH app over loopback and a bridge add-on (via
        # 172.30.32.1 / 172.30.33.x / host IP).
        for ip in ("127.0.0.1", "172.30.32.1", "172.30.33.1", "198.51.100.178"):
            assert _is_trusted_ingress(_conn(ip)) is False, ip

    def test_missing_header_not_trusted(self):
        assert _is_trusted_ingress(_conn(SUPERVISOR, ingress_hdr=False)) is False

    def test_unparseable_and_missing_client_not_trusted(self):
        assert _is_trusted_ingress(_conn("garbage")) is False
        assert _is_trusted_ingress(_conn(None)) is False

    def test_rejection_is_logged(self, caplog):
        with caplog.at_level("WARNING", logger="hub.ingress"):
            assert _is_trusted_ingress(_conn("127.0.0.1")) is False
        assert "Rejected Ingress request from untrusted IP: 127.0.0.1" in caplog.text


class TestWebSocketIsIngress:
    """api.websocket._is_ingress (WebSocket)."""

    def test_supervisor_with_header_trusted(self):
        assert _is_ingress(_conn(SUPERVISOR)) is True

    def test_spoofed_header_rejected(self):
        for ip in ("127.0.0.1", "::1", "172.30.32.1", "172.30.33.9"):
            assert _is_ingress(_conn(ip)) is False, ip

    def test_dev_flag_allows_loopback(self, monkeypatch):
        monkeypatch.setenv("HUB_DEV", "1")
        assert _is_ingress(_conn("127.0.0.1")) is True

    def test_missing_header_not_trusted(self):
        assert _is_ingress(_conn(SUPERVISOR, ingress_hdr=False)) is False


def _app_with_middleware():
    """Minimal app: AuthMiddleware + main.IngressMiddleware, like production."""
    from main import IngressMiddleware

    app = FastAPI()

    @app.get("/api/auth/session")
    async def check(request: Request):
        return {"auth_type": request.state.auth_type}

    @app.get("/page")
    async def page():
        return HTMLResponse("<html><head></head><body>x</body></html>")

    app.add_middleware(IngressMiddleware)
    app.add_middleware(AuthMiddleware)
    return app


class TestMiddlewareEndToEnd:
    def test_spoofed_header_from_loopback_is_not_ingress(self):
        app = _app_with_middleware()
        with TestClient(app, client=("127.0.0.1", 5000)) as c:
            r = c.get("/api/auth/session", headers={"X-Ingress-Path": "/x"})
        assert r.status_code == 200
        assert r.json()["auth_type"] is None

    def test_supervisor_proxy_is_ingress(self):
        app = _app_with_middleware()
        with TestClient(app, client=(SUPERVISOR, 5000)) as c:
            r = c.get("/api/auth/session", headers={"X-Ingress-Path": "/x"})
        assert r.json()["auth_type"] == "ingress"

    def test_html_rewrite_ignores_spoofed_header(self):
        app = _app_with_middleware()
        app.state.db = None
        evil = '/x"><script>alert(1)</script>'
        with TestClient(app, client=("172.30.33.1", 5000)) as c:
            r = c.get("/page", headers={"X-Ingress-Path": evil}, follow_redirects=False)
        assert "__INGRESS_PATH" not in r.text
        assert "<script>alert(1)" not in r.text

    def test_html_rewrite_for_genuine_ingress(self):
        app = _app_with_middleware()
        with TestClient(app, client=(SUPERVISOR, 5000)) as c:
            r = c.get("/page", headers={"X-Ingress-Path": "/api/hassio_ingress/t"})
        assert 'window.__INGRESS_PATH = "/api/hassio_ingress/t"' in r.text
