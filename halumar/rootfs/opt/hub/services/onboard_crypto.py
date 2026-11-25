"""Seal of the onboarding credentials: HPKE base mode, RFC 9180.

Suite DHKEM(X25519, HKDF-SHA256) / HKDF-SHA256 / ChaCha20Poly1305 (kem 0x0020,
kdf 0x0001, aead 0x0003), single shot (sequence number 0). The TV app opens it
with CryptoKit (``HPKE.Ciphersuite.Curve25519_SHA256_ChachaPoly``). Known-answer
vectors (RFC 9180 A.2.1 and the onboarding case): contracts
``fixtures/crypto/onboard-seal-vectors/v1.json``.

The ``cryptography`` package has no HPKE API, so the key schedule is built from
its primitives (X25519, ChaCha20Poly1305) and the standard library (HMAC).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

SUITE = "X25519-SHA256-ChaCha20Poly1305"
INFO = b"hub/onboard/v1"

_KEM_ID, _KDF_ID, _AEAD_ID = 0x0020, 0x0001, 0x0003
_KEM_SUITE = b"KEM" + _KEM_ID.to_bytes(2, "big")
_HPKE_SUITE = (
    b"HPKE"
    + _KEM_ID.to_bytes(2, "big")
    + _KDF_ID.to_bytes(2, "big")
    + _AEAD_ID.to_bytes(2, "big")
)
_NK, _NN, _NH = 32, 12, 32


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes | None:
    """Strict base64url without padding; None when malformed."""
    if not isinstance(text, str) or not text or "=" in text:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def parse_public_key(text: str) -> bytes | None:
    """Raw X25519 public key (32 bytes) from base64url, or None."""
    raw = b64url_decode(text)
    if raw is None or len(raw) != 32 or b64url(raw) != text:
        return None
    try:
        # A trial exchange rejects the low-order points (all-zero shared
        # secret), which nothing can be sealed to.
        X25519PrivateKey.generate().exchange(X25519PublicKey.from_public_bytes(raw))
    except ValueError:
        return None
    return raw


def _extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt or bytes(_NH), ikm, hashlib.sha256).digest()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _labeled_extract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _extract(salt, b"HPKE-v1" + suite + label + ikm)


def _labeled_expand(
    suite: bytes, prk: bytes, label: bytes, info: bytes, length: int
) -> bytes:
    labeled = length.to_bytes(2, "big") + b"HPKE-v1" + suite + label + info
    return _expand(prk, labeled, length)


def _raw(public: X25519PublicKey) -> bytes:
    return public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _shared_secret(dh: bytes, enc: bytes, pk_r: bytes) -> bytes:
    eae_prk = _labeled_extract(_KEM_SUITE, b"", b"eae_prk", dh)
    return _labeled_expand(_KEM_SUITE, eae_prk, b"shared_secret", enc + pk_r, _NH)


def _key_schedule(shared_secret: bytes, info: bytes) -> tuple[bytes, bytes]:
    psk_id_hash = _labeled_extract(_HPKE_SUITE, b"", b"psk_id_hash", b"")
    info_hash = _labeled_extract(_HPKE_SUITE, b"", b"info_hash", info)
    context = b"\x00" + psk_id_hash + info_hash
    secret = _labeled_extract(_HPKE_SUITE, shared_secret, b"secret", b"")
    key = _labeled_expand(_HPKE_SUITE, secret, b"key", context, _NK)
    nonce = _labeled_expand(_HPKE_SUITE, secret, b"base_nonce", context, _NN)
    return key, nonce


def seal(
    pk_r: bytes,
    plaintext: bytes,
    *,
    info: bytes = INFO,
    aad: bytes = b"",
    sk_e: bytes | None = None,
) -> tuple[bytes, bytes]:
    """HPKE SealBase: ``(enc, ciphertext)``. ``sk_e`` only for known-answer tests."""
    eph = (
        X25519PrivateKey.from_private_bytes(sk_e)
        if sk_e
        else X25519PrivateKey.generate()
    )
    enc = _raw(eph.public_key())
    dh = eph.exchange(X25519PublicKey.from_public_bytes(pk_r))
    key, nonce = _key_schedule(_shared_secret(dh, enc, pk_r), info)
    return enc, ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)


def open_sealed(
    sk_r: bytes, enc: bytes, ciphertext: bytes, *, info: bytes = INFO, aad: bytes = b""
) -> bytes:
    """HPKE OpenBase (tests and tools; the hub itself only seals)."""
    priv = X25519PrivateKey.from_private_bytes(sk_r)
    dh = priv.exchange(X25519PublicKey.from_public_bytes(enc))
    key, nonce = _key_schedule(_shared_secret(dh, enc, _raw(priv.public_key())), info)
    return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)


def seal_credentials(
    public_key: bytes, session_id: str, client_id: str, secret: str
) -> dict:
    """``onboard_ok.sealed`` for ``{client_id, secret}`` (aad = session id)."""
    plaintext = json.dumps(
        {"client_id": client_id, "secret": secret}, separators=(",", ":")
    ).encode()
    enc, ciphertext = seal(public_key, plaintext, aad=session_id.encode())
    return {"suite": SUITE, "enc": b64url(enc), "ciphertext": b64url(ciphertext)}
