"""Tests for the self-signed TLS service of the client-facing port.

Security-critical: the hub generates a long-lived self-signed cert whose
SHA-256 fingerprint clients pin at pairing (TOFU). These tests verify that:
- ensure_cert() creates a usable cert+key pair with a 0600-protected key,
- ensure_cert() is idempotent (the pinned fingerprint stays stable on restart),
- a passed extra IP lands in the cert SAN,
- get_fingerprint() equals an independent SHA-256 over the DER leaf cert and is
  None when no cert exists,
- the emitted cert+key actually load into an ssl.SSLContext (i.e. uvicorn can
  serve TLS with them).

Everything runs against a per-test tmp dir (tls_dir param) — never /data.
"""

import hashlib
import ipaddress
import os
import ssl
import stat

import pytest

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from brand import BRAND_NAME
import services.tls as tls


@pytest.fixture(autouse=True)
def clear_fp_cache():
    """Isolate the module-level fingerprint cache between tests."""
    tls._fingerprint_cache.clear()
    tls._served.clear()
    yield
    tls._fingerprint_cache.clear()
    tls._served.clear()


@pytest.fixture
def tls_dir(tmp_path):
    """A fresh, empty TLS directory for each test (never /data)."""
    return str(tmp_path / "tls")


def _load_cert(cert_path: str) -> x509.Certificate:
    with open(cert_path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


class TestEnsureCert:
    """ensure_cert() generation, permissions, idempotency, and SAN."""

    def test_creates_cert_and_key(self, tls_dir):
        cert_path, key_path = tls.ensure_cert(tls_dir=tls_dir)
        assert os.path.isfile(cert_path)
        assert os.path.isfile(key_path)
        assert cert_path == os.path.join(tls_dir, "cert.pem")
        assert key_path == os.path.join(tls_dir, "key.pem")

    def test_key_has_0600_permissions(self, tls_dir):
        _, key_path = tls.ensure_cert(tls_dir=tls_dir)
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        assert mode == 0o600

    def test_idempotent_reuses_existing(self, tls_dir):
        """A second call must return the same pair untouched, so the pinned
        fingerprint stays stable across restarts."""
        cert_path, key_path = tls.ensure_cert(tls_dir=tls_dir)
        cert_bytes = open(cert_path, "rb").read()
        key_bytes = open(key_path, "rb").read()

        cert_path2, key_path2 = tls.ensure_cert(tls_dir=tls_dir)
        assert (cert_path2, key_path2) == (cert_path, key_path)
        # Not regenerated: byte-for-byte identical, so the fingerprint is stable.
        assert open(cert_path2, "rb").read() == cert_bytes
        assert open(key_path2, "rb").read() == key_bytes

    def test_san_includes_extra_ip(self, tls_dir):
        cert_path, _ = tls.ensure_cert(tls_dir=tls_dir, extra_ips=["192.0.2.50"])
        san = (
            _load_cert(cert_path)
            .extensions.get_extension_for_class(x509.SubjectAlternativeName)
            .value
        )
        ips = san.get_values_for_type(x509.IPAddress)
        assert ipaddress.ip_address("192.0.2.50") in ips
        # Loopback is always present regardless of extra_ips.
        assert ipaddress.ip_address("127.0.0.1") in ips

    def test_san_includes_default_dns_names(self, tls_dir):
        cert_path, _ = tls.ensure_cert(tls_dir=tls_dir)
        san = (
            _load_cert(cert_path)
            .extensions.get_extension_for_class(x509.SubjectAlternativeName)
            .value
        )
        dns = san.get_values_for_type(x509.DNSName)
        assert "localhost" in dns
        assert "homeassistant.local" in dns

    def test_cert_and_key_load_into_ssl_context(self, tls_dir):
        """The emitted pair must be usable by an ssl server context (this is
        exactly what uvicorn's ssl_certfile/ssl_keyfile do)."""
        cert_path, key_path = tls.ensure_cert(tls_dir=tls_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Raises on a mismatched or malformed cert/key — must not raise.
        ctx.load_cert_chain(cert_path, key_path)


class TestEnforceTls13Minimum:
    """enforce_tls13_minimum(): the TLS 1.3 floor applied by _run_dual_servers."""

    def test_raises_floor_on_plain_server_context(self, tls_dir):
        cert_path, key_path = tls.ensure_cert(tls_dir=tls_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_path, key_path)
        # PROTOCOL_TLS_SERVER would still negotiate TLS 1.2 by default.
        assert ctx.minimum_version < ssl.TLSVersion.TLSv1_3

        tls.enforce_tls13_minimum(ctx)
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3

    def test_applies_to_uvicorn_built_context(self, tls_dir):
        """End-to-end against the exact context uvicorn serves with: main.py
        loads the uvicorn Config and hardens config.ssl — replicate that."""
        import uvicorn

        async def _dummy_app(scope, receive, send):  # pragma: no cover
            pass

        cert_path, key_path = tls.ensure_cert(tls_dir=tls_dir)
        config = uvicorn.Config(
            _dummy_app,
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
        )
        config.load()
        assert config.ssl is not None
        assert config.ssl.minimum_version < ssl.TLSVersion.TLSv1_3

        tls.enforce_tls13_minimum(config.ssl)
        assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_3


class TestGetFingerprint:
    """get_fingerprint() correctness against an independent hash."""

    def test_matches_independent_sha256_of_der(self, tls_dir):
        cert_path, _ = tls.ensure_cert(tls_dir=tls_dir)
        der = _load_cert(cert_path).public_bytes(serialization.Encoding.DER)
        expected = hashlib.sha256(der).hexdigest()

        assert tls.get_fingerprint(tls_dir=tls_dir) == expected

    def test_is_lowercase_hex(self, tls_dir):
        tls.ensure_cert(tls_dir=tls_dir)
        fp = tls.get_fingerprint(tls_dir=tls_dir)
        assert fp is not None
        assert len(fp) == 64  # SHA-256 = 32 bytes = 64 hex chars
        assert fp == fp.lower()
        assert all(c in "0123456789abcdef" for c in fp)

    def test_none_when_no_cert(self, tls_dir):
        # No ensure_cert() call ⇒ no cert file ⇒ None (never a fake value).
        assert tls.get_fingerprint(tls_dir=tls_dir) is None

    def test_stable_across_calls(self, tls_dir):
        """Repeated calls (served from cache) return the same value."""
        tls.ensure_cert(tls_dir=tls_dir)
        assert tls.get_fingerprint(tls_dir=tls_dir) == tls.get_fingerprint(
            tls_dir=tls_dir
        )


class TestRegenerate:
    """regenerate() rotates the cert and returns the NEW fingerprint."""

    def test_changes_fingerprint(self, tls_dir):
        tls.ensure_cert(tls_dir=tls_dir)
        old = tls.get_fingerprint(tls_dir=tls_dir)
        new = tls.regenerate(tls_dir=tls_dir)
        assert new != old
        # The rotation is durable: a fresh read (not from the cache) agrees.
        tls._fingerprint_cache.clear()
        assert tls.get_fingerprint(tls_dir=tls_dir) == new

    def test_returns_lowercase_hex(self, tls_dir):
        fp = tls.regenerate(tls_dir=tls_dir)
        assert len(fp) == 64
        assert fp == fp.lower()
        assert all(c in "0123456789abcdef" for c in fp)

    def test_works_when_no_prior_cert(self, tls_dir):
        # No ensure_cert() first — regenerate() mints the initial pair.
        fp = tls.regenerate(tls_dir=tls_dir)
        assert fp == tls.get_fingerprint(tls_dir=tls_dir)

    def test_clears_stale_cache(self, tls_dir):
        # Prime the cache, then regenerate: the stale entry must not survive.
        tls.ensure_cert(tls_dir=tls_dir)
        old = tls.get_fingerprint(tls_dir=tls_dir)
        cert_path, _ = tls._paths(tls_dir)
        assert tls._fingerprint_cache.get(cert_path) == old
        new = tls.regenerate(tls_dir=tls_dir)
        assert tls._fingerprint_cache.get(cert_path) == new != old

    def test_extra_ip_lands_in_new_san(self, tls_dir):
        tls.regenerate(tls_dir=tls_dir, extra_ips=["198.51.100.9"])
        cert_path, _ = tls._paths(tls_dir)
        san = (
            _load_cert(cert_path)
            .extensions.get_extension_for_class(x509.SubjectAlternativeName)
            .value
        )
        assert ipaddress.ip_address("198.51.100.9") in san.get_values_for_type(
            x509.IPAddress
        )


class TestServedFingerprint:
    """After regenerate the hub keeps advertising the cert the running server
    presents until the restart."""

    def test_regenerate_does_not_change_the_advertised_fingerprint(self, tls_dir):
        tls.ensure_cert(tls_dir=tls_dir)
        served = tls.mark_served(tls_dir=tls_dir)
        assert tls.pending_fingerprint(tls_dir=tls_dir) is None
        new = tls.regenerate(tls_dir=tls_dir)
        assert new != served
        assert tls.get_fingerprint(tls_dir=tls_dir) == served
        assert tls.pending_fingerprint(tls_dir=tls_dir) == new
        assert tls.file_fingerprint(tls_dir=tls_dir) == new
        # The restart loads the new cert.
        assert tls.mark_served(tls_dir=tls_dir) == new
        assert tls.get_fingerprint(tls_dir=tls_dir) == new
        assert tls.pending_fingerprint(tls_dir=tls_dir) is None

    async def test_settings_endpoint_shows_the_served_one(self, monkeypatch, tmp_path):
        from api.admin import get_tls_settings, regenerate_tls_cert

        import services.mdns as mdns

        real_paths = tls._paths
        tmp_dir = str(tmp_path / "tls")
        monkeypatch.setattr(tls, "_paths", lambda _dir: real_paths(tmp_dir))
        monkeypatch.setattr(mdns, "_get_local_ip", lambda: "127.0.0.1")
        real_regen = tls.regenerate
        monkeypatch.setattr(
            tls, "regenerate", lambda **kw: real_regen(tls_dir=tmp_dir, **kw)
        )
        tls.ensure_cert(tls_dir=tmp_dir)
        served = tls.mark_served()
        result = await regenerate_tls_cert(_admin_request())
        settings = await get_tls_settings(_admin_request())
        assert settings["fingerprint"] == served
        assert settings["pending_fingerprint"] == result["fingerprint"] != served


class TestCertInfo:
    """cert_info() summary shape, self-signed flag, and None when absent."""

    def test_none_when_no_cert(self, tls_dir):
        assert tls.cert_info(tls_dir=tls_dir) is None

    def test_keys_and_types(self, tls_dir):
        tls.ensure_cert(tls_dir=tls_dir)
        info = tls.cert_info(tls_dir=tls_dir)
        assert set(info) == {
            "fingerprint",
            "not_valid_before",
            "not_valid_after",
            "subject",
            "self_signed",
        }
        assert isinstance(info["fingerprint"], str)
        assert isinstance(info["not_valid_before"], str)
        assert isinstance(info["not_valid_after"], str)
        assert isinstance(info["subject"], str)
        assert isinstance(info["self_signed"], bool)

    def test_fingerprint_matches_get_fingerprint(self, tls_dir):
        tls.ensure_cert(tls_dir=tls_dir)
        assert tls.cert_info(tls_dir=tls_dir)["fingerprint"] == tls.get_fingerprint(
            tls_dir=tls_dir
        )

    def test_self_signed_true_and_subject_cn(self, tls_dir):
        tls.ensure_cert(tls_dir=tls_dir)
        info = tls.cert_info(tls_dir=tls_dir)
        assert info["self_signed"] is True
        assert info["subject"] == f"{BRAND_NAME} Add-on"

    def test_validity_is_iso8601_utc(self, tls_dir):
        import datetime as _dt

        tls.ensure_cert(tls_dir=tls_dir)
        info = tls.cert_info(tls_dir=tls_dir)
        # Parseable ISO-8601 with a tz offset, and after < before-window ordering.
        before = _dt.datetime.fromisoformat(info["not_valid_before"])
        after = _dt.datetime.fromisoformat(info["not_valid_after"])
        assert before.tzinfo is not None and after.tzinfo is not None
        assert after > before


# ── Admin TLS settings endpoints (api/admin.py) ──────────────────────────────
#
# GET /api/settings/tls surfaces the state; POST /api/settings/tls/regenerate
# rotates the cert. Both are admin-only. Endpoint functions are called directly
# with a fake request (matching the direct-call style above); the tls service
# is monkeypatched so these never touch a real /data cert.


def _admin_request(auth_type: str = "admin"):
    import types

    # require_admin only reads request.state.auth_type.
    return types.SimpleNamespace(state=types.SimpleNamespace(auth_type=auth_type))


class TestTlsSettingsEndpoints:
    async def test_get_returns_expected_keys_and_types(self, monkeypatch):
        from api.admin import get_tls_settings

        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "ab" * 32)
        monkeypatch.setattr(
            tls,
            "cert_info",
            lambda *a, **k: {
                "fingerprint": "ab" * 32,
                "not_valid_before": "2024-01-01T00:00:00+00:00",
                "not_valid_after": "2034-01-01T00:00:00+00:00",
                "subject": f"{BRAND_NAME} Add-on",
                "self_signed": True,
            },
        )

        result = await get_tls_settings(_admin_request())

        assert set(result) == {
            "tls_enabled",
            "tls_port",
            "fingerprint",
            "pending_fingerprint",
            "cert",
        }
        assert isinstance(result["tls_enabled"], bool)
        assert result["tls_port"] == tls.TLS_PORT
        assert result["fingerprint"] == "ab" * 32
        assert result["cert"]["self_signed"] is True
        # Never leak the private key or its path.
        assert "key" not in result and "key_path" not in result

    async def test_get_handles_no_cert(self, monkeypatch):
        from api.admin import get_tls_settings

        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: None)
        monkeypatch.setattr(tls, "cert_info", lambda *a, **k: None)

        result = await get_tls_settings(_admin_request())
        assert result["fingerprint"] is None
        assert result["cert"] is None

    async def test_get_requires_admin(self):
        from fastapi import HTTPException

        from api.admin import get_tls_settings

        with pytest.raises(HTTPException) as exc:
            await get_tls_settings(_admin_request(auth_type="client"))
        assert exc.value.status_code == 403

    async def test_regenerate_returns_new_fingerprint_and_restart_flag(
        self, monkeypatch
    ):
        import services.mdns as mdns

        from api.admin import regenerate_tls_cert

        captured: dict = {}

        def _fake_regen(*args, **kwargs):
            captured.update(kwargs)
            return "ff" * 32

        monkeypatch.setattr(tls, "regenerate", _fake_regen)
        monkeypatch.setattr(mdns, "_get_local_ip", lambda: "192.168.1.5")

        result = await regenerate_tls_cert(_admin_request())

        assert result["fingerprint"] == "ff" * 32
        assert result["restart_required"] is True
        assert "re-pair" in result["message"]
        # Detected LAN IP is added to the SAN; loopback is skipped by the service.
        assert captured["extra_ips"] == ["192.168.1.5"]

    async def test_regenerate_skips_loopback_ip(self, monkeypatch):
        import services.mdns as mdns

        from api.admin import regenerate_tls_cert

        captured: dict = {}
        monkeypatch.setattr(
            tls, "regenerate", lambda *a, **k: captured.update(k) or "ff" * 32
        )
        monkeypatch.setattr(mdns, "_get_local_ip", lambda: "127.0.0.1")

        await regenerate_tls_cert(_admin_request())
        assert captured["extra_ips"] == []

    async def test_regenerate_requires_admin(self):
        from fastapi import HTTPException

        from api.admin import regenerate_tls_cert

        with pytest.raises(HTTPException) as exc:
            await regenerate_tls_cert(_admin_request(auth_type="client"))
        assert exc.value.status_code == 403


# ── Fingerprint-publishing contract (health / QR / Bonjour / client_config) ──
#
# The SAME lowercase-hex SHA-256 the iOS/tvOS apps pin must surface in every
# channel, and be null/absent (never a fake value) when no cert exists. The
# pure service is covered above; these cover the publish WIRING in each channel.
# get_fingerprint is monkeypatched (each consumer imports it at call time), so
# these don't depend on a real /data cert.


class TestClientConfigPublishesFingerprint:
    """WS client_config carries tls_fingerprint + tls_port — contract (d)."""

    async def test_fields_present_when_cert_exists(self, db_with_client, monkeypatch):
        from api.websocket import _get_client_config

        db, client_id, _, _ = db_with_client
        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "cd" * 32)
        cfg = await _get_client_config(db, client_id)
        assert cfg["tls_fingerprint"] == "cd" * 32
        assert cfg["tls_port"] == tls.TLS_PORT

    async def test_null_when_no_cert(self, db_with_client, monkeypatch):
        from api.websocket import _get_client_config

        db, client_id, _, _ = db_with_client
        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: None)
        cfg = await _get_client_config(db, client_id)
        assert cfg["tls_fingerprint"] is None
        assert cfg["tls_port"] is None


class TestMdnsPublishesFingerprint:
    """Bonjour _halumar._tcp TXT carries tls/tls_port/fp — contract (c).

    The registration flow itself is covered in test_mdns.py.
    """

    def test_txt_records_when_cert_exists(self, monkeypatch):
        import services.mdns as mdns

        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "ab" * 32)
        props = mdns.build_properties("iid-1", "1.0")
        assert props["tls"] == "1"
        assert props["tls_port"] == str(tls.TLS_PORT)
        assert props["fp"] == "ab" * 32

    def test_no_txt_records_when_no_cert(self, monkeypatch):
        import services.mdns as mdns

        monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: None)
        props = mdns.build_properties("iid-1", "1.0")
        assert "tls" not in props
        assert "tls_port" not in props
        assert "fp" not in props


def test_empty_or_damaged_pair_is_replaced(tmp_path):
    """A power loss right after the first start left empty files;
    the TLS server could never load them. They are replaced."""
    import ssl as _ssl

    from services import tls

    d = tmp_path / "tls"
    d.mkdir()
    (d / "cert.pem").write_bytes(b"")
    (d / "key.pem").write_bytes(b"")
    cert, key = tls.ensure_cert(tls_dir=str(d))
    assert (d / "cert.pem").read_bytes().startswith(b"-----BEGIN CERTIFICATE")
    assert tls.get_fingerprint(tls_dir=str(d))
    _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    # A usable pair stays untouched (pinned fingerprint).
    before = (d / "cert.pem").read_bytes()
    tls.ensure_cert(tls_dir=str(d))
    assert (d / "cert.pem").read_bytes() == before


def test_cert_files_are_fsynced(tmp_path, monkeypatch):
    import os as _os

    from services import tls

    synced = []
    real = _os.fsync

    def _fsync(fd):
        synced.append(fd)
        return real(fd)

    monkeypatch.setattr(_os, "fsync", _fsync)
    tls.ensure_cert(tls_dir=str(tmp_path / "tls"))
    assert len(synced) >= 4  # each file and its directory
