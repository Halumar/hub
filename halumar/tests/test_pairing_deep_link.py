"""Canonical pairing deep link: the services/pairing_link.py contract.

One format, built at exactly ONE place and emitted identically by every
admin surface (web-UI QR, copy link); the integration camera never shows it:

    halumar://pair?url=<percent-encoded base URL, no trailing slash>
                   &pin=<XXXX-XXXX>[&tls_port=<int>&fp=<64 lowercase hex>]

Under test:
(a) build_pairing_deep_link: exact string incl. percent-encoding, trailing
    slash stripped, fp/tls_port only when a cert exists.
(b) POST /api/pairing/generate returns url + deep_link (exact string, no
    double encoding).
(c) GET /api/integration/pairing-qr is a status image WITHOUT PIN/link.
(d) GET /api/pairing/status: url/deep_link only for admin (Ingress) requests
    while pairing is active, null otherwise (the link carries the PIN).
(e) resolve_base_url order: app_url > forwarded headers (non-Ingress only) >
    routable Host (non-Ingress only) > ha_internal_url host + :8099.
"""

import time
import types

import pytest

import api.pairing as pairing
import services.tls as tls
from services.pairing_link import build_pairing_deep_link, resolve_base_url


@pytest.fixture
def clean_pairing():
    """Isolate the module-level active-pin state around each test."""
    pairing.clear_active_pins()
    yield
    pairing.clear_active_pins()


@pytest.fixture
def no_cert(monkeypatch):
    """No TLS cert: the deep link must NOT carry fp/tls_port."""
    monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: None)


def _request(db, *, auth_type="admin", app_url="", headers=None, host="203.0.113.7"):
    """Minimal request namespace covering everything the endpoints touch."""
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth_type),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url=app_url)
        ),
        headers=dict(headers or {}),
        client=types.SimpleNamespace(host=host),
        url=types.SimpleNamespace(scheme="http"),
    )


def _ingress_headers():
    return {"x-ingress-path": "/api/hassio_ingress/abc"}


# ── (a) build_pairing_deep_link ──────────────────────────────────────────────


class TestBuildPairingDeepLink:
    def test_exact_string_without_cert(self, no_cert):
        link = build_pairing_deep_link("http://192.168.1.50:8099", "3477-3800")
        assert link == (
            "halumar://pair?url=http%3A%2F%2F192.168.1.50%3A8099&pin=3477-3800"
        )

    def test_trailing_slash_stripped(self, no_cert):
        link = build_pairing_deep_link("http://192.168.1.50:8099/", "3477-3800")
        assert link == (
            "halumar://pair?url=http%3A%2F%2F192.168.1.50%3A8099&pin=3477-3800"
        )

    def test_fp_and_tls_port_appended_when_cert_exists(self, monkeypatch):
        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "ab" * 32)
        link = build_pairing_deep_link("http://home.local:8099", "1234-5678")
        assert link == (
            "halumar://pair?url=http%3A%2F%2Fhome.local%3A8099&pin=1234-5678"
            f"&tls_port={tls.TLS_PORT}&fp={'ab' * 32}"
        )


# ── (b) /api/pairing/generate ────────────────────────────────────────────────


class TestGenerateReturnsDeepLink:
    async def test_exact_deep_link_and_url(
        self, db, clean_pairing, no_cert, monkeypatch
    ):
        monkeypatch.setattr(pairing, "_generate_pin", lambda: "3477-3800")

        request = _request(db, app_url="http://192.168.1.50:8099/")
        result = await pairing.generate_pairing_pin(request, None)

        assert result.url == "http://192.168.1.50:8099"
        assert result.deep_link == (
            "halumar://pair?url=http%3A%2F%2F192.168.1.50%3A8099&pin=3477-3800"
        )
        # Double-encoding protection: the encoded URL is encoded exactly once.
        assert "%25" not in result.deep_link

    async def test_deep_link_carries_fp_when_cert_exists(
        self, db, clean_pairing, monkeypatch
    ):
        monkeypatch.setattr(pairing, "_generate_pin", lambda: "3477-3800")
        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "cd" * 32)

        request = _request(db, app_url="http://192.168.1.50:8099")
        result = await pairing.generate_pairing_pin(request, None)

        assert result.deep_link == (
            "halumar://pair?url=http%3A%2F%2F192.168.1.50%3A8099&pin=3477-3800"
            f"&tls_port={tls.TLS_PORT}&fp={'cd' * 32}"
        )


# ── (c) /api/integration/pairing-qr never carries the PIN ─────────


class TestIntegrationQrHasNoPin:
    async def test_status_image_without_pin_or_link(
        self, db, clean_pairing, no_cert, monkeypatch
    ):
        from api.integration import integration_pairing_qr

        monkeypatch.setattr(pairing, "_generate_pin", lambda: "3477-3800")
        admin = _request(db, app_url="http://192.168.1.50:8099")
        await pairing.generate_pairing_pin(admin, None)

        intg = _request(db, auth_type="integration", app_url="http://192.168.1.50:8099")
        resp = await integration_pairing_qr(intg)

        assert resp.headers["X-Pairing-Active"] == "true"
        assert resp.headers["X-Pairing-Pin"] == "omitted"
        assert resp.body.startswith(b"\x89PNG")
        for secret in (b"3477", b"3800", b"halumar://", b"pin="):
            assert secret not in resp.body

    async def test_standby_when_no_pin(self, db, clean_pairing, no_cert):
        from api.integration import integration_pairing_qr

        resp = await integration_pairing_qr(_request(db, auth_type="integration"))
        assert resp.headers["X-Pairing-Active"] == "false"
        assert resp.body.startswith(b"\x89PNG")


# ── (d) /api/pairing/status gates url/deep_link on admin + active ────────────


class TestStatusDeepLinkGate:
    async def test_null_without_ingress_admin(self, db, clean_pairing, no_cert):
        pairing.set_active_pin(
            "1234-5678",
            {"created_at": time.time(), "expires_at": time.time() + 300},
        )
        # Token admin (no Ingress headers): pairing is active but the link
        # (which carries the PIN in cleartext) stays hidden, like the PIN.
        result = await pairing.pairing_status(_request(db))

        assert result["pairing_active"] is True
        assert result["pin"] == "****-****"
        assert result["url"] is None
        assert result["deep_link"] is None

    async def test_null_when_no_active_pairing(self, db, clean_pairing, no_cert):
        result = await pairing.pairing_status(
            _request(db, headers=_ingress_headers(), host="172.30.32.2")
        )
        assert result["pairing_active"] is False
        assert result["url"] is None
        assert result["deep_link"] is None

    async def test_present_for_ingress_admin_with_active_pairing(
        self, db, clean_pairing, no_cert
    ):
        await db.set_setting("ha_internal_url", "http://192.168.1.10:8123")
        pairing.set_active_pin(
            "1234-5678",
            {"created_at": time.time(), "expires_at": time.time() + 300},
        )

        result = await pairing.pairing_status(
            _request(db, headers=_ingress_headers(), host="172.30.32.2")
        )

        assert result["pin"] == "1234-5678"
        assert result["url"] == "http://192.168.1.10:8099"
        assert result["deep_link"] == (
            "halumar://pair?url=http%3A%2F%2F192.168.1.10%3A8099&pin=1234-5678"
        )


# ── (e) resolve_base_url resolution order ────────────────────────────────────


class TestResolveBaseUrl:
    async def test_app_url_wins(self, db):
        request = _request(
            db,
            app_url="https://halumar.example.com/",
            headers={"x-forwarded-host": "ignored.example.com"},
        )
        assert await resolve_base_url(request, db) == "https://halumar.example.com"

    async def test_direct_access_uses_host_header(self, db):
        request = _request(db, headers={"host": "192.168.1.50:8099"})
        assert await resolve_base_url(request, db) == "http://192.168.1.50:8099"

    async def test_forwarded_headers_win_over_host(self, db):
        request = _request(
            db,
            headers={
                "x-forwarded-host": "halumar.lan:8099",
                "x-forwarded-proto": "https",
                "host": "192.168.1.50:8099",
            },
        )
        assert await resolve_base_url(request, db) == "https://halumar.lan:8099"

    async def test_ingress_skips_headers_and_uses_ha_internal_url(self, db):
        """Ingress requests carry HA's :8123 headers — encoding those would
        produce an unreachable URL, so they must be skipped."""
        await db.set_setting("ha_internal_url", "http://192.168.1.10:8123")
        request = _request(
            db,
            headers={
                **_ingress_headers(),
                "x-forwarded-host": "homeassistant.local:8123",
                "host": "homeassistant.local:8123",
            },
            host="172.30.32.2",
        )
        assert await resolve_base_url(request, db) == "http://192.168.1.10:8099"

    async def test_non_routable_host_falls_back(self, db):
        await db.set_setting("ha_internal_url", "http://192.168.1.10:8123")
        request = _request(db, headers={"host": "localhost:8099"})
        assert await resolve_base_url(request, db) == "http://192.168.1.10:8099"
