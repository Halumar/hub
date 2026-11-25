"""Self-signed TLS for the hub's client-facing port.

On a LAN there is no CA-signed certificate for ``https://<host>:8100``, so the
trust anchor is TOFU PINNING, not CA validation: the hub generates a
long-lived self-signed cert on first use and publishes its SHA-256 fingerprint
(``/api/health``, the Bonjour TXT record and the pairing deep link). A client pins
that fingerprint at pairing and rejects any cert change afterwards, which
defeats a LAN MITM on the channel that carries the ``client_config`` (HA token)
and the pairing secret.

The private key lives in ``/data/tls/key.pem`` with ``0600`` perms (same model
as the Fernet keyfile): a leaked DB alone never exposes it, and it never leaves
``/data``.
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import os
import ssl
import stat

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from brand import BRAND_NAME
from services.atomic_io import atomic_write_bytes

logger = logging.getLogger("hub.services.tls")

# External TLS port for wss/https clients. The plain-HTTP :8099 stays for HA
# Ingress (internal Docker) and is unchanged.
TLS_PORT = 8100

# Persistent location inside the hub /data volume.
TLS_DIR = "/data/tls"

# Self-signed + pinned, so a long validity avoids expiry surprises (rotation is
# a deliberate admin action, not a CA cadence).
_VALIDITY_DAYS = 3650
_KEY_SIZE = 2048

# fingerprint cache keyed by cert path (reading + hashing on every /health call
# is wasteful; the file only changes on regenerate, which clears the entry).
_fingerprint_cache: dict[str, str] = {}

# Fingerprint of the cert the running TLS server presents, keyed by cert path
# (set by mark_served when the server loads it). After regenerate the file
# holds a new cert, but the server keeps the old one until the restart.
_served: dict[str, str] = {}


def _paths(tls_dir: str) -> tuple[str, str]:
    return os.path.join(tls_dir, "cert.pem"), os.path.join(tls_dir, "key.pem")


def enforce_tls13_minimum(ctx: ssl.SSLContext) -> None:
    """Raise the serving context's protocol floor to TLS 1.3.

    uvicorn builds its SSLContext with ``PROTOCOL_TLS_SERVER``, which still
    negotiates TLS 1.2. Every pinning client (iOS/tvOS via URLSession) speaks
    TLS 1.3, so allowing 1.2 only widens the downgrade/legacy-cipher surface.
    Called by ``main._run_dual_servers`` on the loaded uvicorn config.
    """
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3


def _build_san(
    extra_hosts: list[str] | None, extra_ips: list[str] | None
) -> x509.SubjectAlternativeName:
    """Best-effort SAN. Under fingerprint pinning the SAN is not the trust
    anchor, but a sensible SAN keeps standard validation working for anyone who
    later fronts the hub with a CA-valid cert / reverse proxy."""
    entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.DNSName("homeassistant.local"),
    ]
    for host in extra_hosts or []:
        host = (host or "").strip()
        if host:
            entries.append(x509.DNSName(host))
    seen: set[str] = set()
    for ip_str in ["127.0.0.1", *(extra_ips or [])]:
        ip_str = (ip_str or "").strip()
        if not ip_str or ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
        except ValueError:
            # Not an IP (e.g. a hostname slipped into extra_ips) — treat as DNS.
            entries.append(x509.DNSName(ip_str))
    return x509.SubjectAlternativeName(entries)


def _write_atomic(path: str, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` atomically with the given file mode, fsynced
    (file and directory), so a power loss never leaves an empty file."""
    atomic_write_bytes(path, data, mode)


def _pair_usable(cert_path: str, key_path: str) -> bool:
    """Both files hold a certificate and its own private key."""
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        with open(key_path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        pub = serialization.PublicFormat.SubjectPublicKeyInfo
        enc = serialization.Encoding.DER
        return cert.public_key().public_bytes(
            enc, pub
        ) == key.public_key().public_bytes(enc, pub)
    except Exception:  # noqa: BLE001 - any read or parse error: not usable
        return False


def ensure_cert(
    tls_dir: str = TLS_DIR,
    extra_hosts: list[str] | None = None,
    extra_ips: list[str] | None = None,
) -> tuple[str, str]:
    """Return ``(cert_path, key_path)``, generating a self-signed pair on first
    use. Idempotent: an existing usable pair is returned untouched (so the
    pinned fingerprint stays stable across restarts); an empty or damaged
    one is replaced."""
    cert_path, key_path = _paths(tls_dir)
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        if _pair_usable(cert_path, key_path):
            return cert_path, key_path
        # Empty or damaged (power loss): the TLS server could never start.
        # A new pair means the devices pair again, but they could not
        # connect with this one either.
        logger.error("TLS cert or key at %s unusable, generating a new pair", tls_dir)

    os.makedirs(tls_dir, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{BRAND_NAME} Add-on")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .add_extension(_build_san(extra_hosts, extra_ips), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    # Key first (0600), then cert (0644 — the cert is public).
    _write_atomic(key_path, key_pem, stat.S_IRUSR | stat.S_IWUSR)
    _write_atomic(
        cert_path,
        cert_pem,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
    )
    _fingerprint_cache.pop(cert_path, None)
    logger.info(
        "Generated self-signed TLS cert at %s (fingerprint %s)",
        cert_path,
        _sha256_der_hex(cert_pem),
    )
    return cert_path, key_path


def _sha256_der_hex(cert_pem: bytes) -> str:
    cert = x509.load_pem_x509_certificate(cert_pem)
    der = cert.public_bytes(serialization.Encoding.DER)
    return hashlib.sha256(der).hexdigest()


def get_fingerprint(tls_dir: str = TLS_DIR) -> str | None:
    """SHA-256 fingerprint (lowercase hex of the DER cert) of the cert the
    running TLS server presents, or ``None`` when no cert exists yet. This is
    the value a client pins (health, QR, Bonjour, client_config)."""
    cert_path, _ = _paths(tls_dir)
    return _served.get(cert_path) or file_fingerprint(tls_dir)


def mark_served(tls_dir: str = TLS_DIR) -> str | None:
    """Record the cert in ``tls_dir`` as the one the TLS server now presents
    (call right after the server loaded it); returns its fingerprint."""
    cert_path, _ = _paths(tls_dir)
    fp = file_fingerprint(tls_dir)
    if fp:
        _served[cert_path] = fp
    return fp


def pending_fingerprint(tls_dir: str = TLS_DIR) -> str | None:
    """Fingerprint of a regenerated cert the server does not present yet
    (restart pending), else ``None``."""
    fp = file_fingerprint(tls_dir)
    return fp if fp and fp != get_fingerprint(tls_dir) else None


def file_fingerprint(tls_dir: str = TLS_DIR) -> str | None:
    """Fingerprint of ``cert.pem`` on disk, or ``None`` when there is none."""
    cert_path, _ = _paths(tls_dir)
    if not os.path.isfile(cert_path):
        return None
    cached = _fingerprint_cache.get(cert_path)
    if cached:
        return cached
    try:
        with open(cert_path, "rb") as f:
            fp = _sha256_der_hex(f.read())
        _fingerprint_cache[cert_path] = fp
        return fp
    except Exception as e:  # noqa: BLE001 - never break /health on a bad cert
        logger.error("Failed to read TLS cert fingerprint: %s", type(e).__name__)
        return None


def regenerate(
    tls_dir: str = TLS_DIR,
    extra_hosts: list[str] | None = None,
    extra_ips: list[str] | None = None,
) -> str:
    """Rotate the self-signed cert and return the NEW fingerprint.

    Until the restart :func:`get_fingerprint` keeps answering the fingerprint
    the running server presents (when :func:`mark_served` ran); the new one is
    :func:`pending_fingerprint`.

    Deletes the existing ``cert.pem``/``key.pem`` (if any), clears the cached
    fingerprint for this path, then calls :func:`ensure_cert` to mint a fresh
    pair. This is a DELIBERATE admin action, not a restart-safe operation: the
    fingerprint changes, so every client that pinned the old one must re-pair,
    and the hub must be restarted for the running TLS server to pick up the
    new cert (uvicorn holds the old ``ssl_certfile`` for the process lifetime).
    """
    cert_path, key_path = _paths(tls_dir)
    for path in (cert_path, key_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _fingerprint_cache.pop(cert_path, None)

    ensure_cert(tls_dir=tls_dir, extra_hosts=extra_hosts, extra_ips=extra_ips)

    fp = file_fingerprint(tls_dir=tls_dir)
    if fp is None:
        # ensure_cert just wrote a valid pair, so this is unreachable in
        # practice; guard it so the caller always gets a real fingerprint.
        raise RuntimeError("cert missing immediately after regeneration")
    return fp


def cert_info(tls_dir: str = TLS_DIR) -> dict | None:
    """Public summary of the current leaf cert for the admin UI, or ``None``
    when no cert exists.

    Returns the pinned fingerprint, the validity window (ISO-8601, UTC), the
    subject CN, and whether the cert is self-signed (issuer == subject). Only
    reads the PUBLIC cert.pem — never touches or exposes the private key.
    """
    cert_path, _ = _paths(tls_dir)
    if not os.path.isfile(cert_path):
        return None
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
    except Exception as e:  # noqa: BLE001 - a bad cert must not 500 the UI
        logger.error("Failed to load TLS cert for cert_info: %s", type(e).__name__)
        return None

    der = cert.public_bytes(serialization.Encoding.DER)

    def _cn(name: x509.Name) -> str:
        attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(attrs[0].value) if attrs else ""

    return {
        "fingerprint": hashlib.sha256(der).hexdigest(),
        "not_valid_before": cert.not_valid_before_utc.isoformat(),
        "not_valid_after": cert.not_valid_after_utc.isoformat(),
        "subject": _cn(cert.subject),
        "self_signed": cert.issuer == cert.subject,
    }
