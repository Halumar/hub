"""Settings-PIN hashing for the remote-lock feature.

The admin sets a settings PIN in the web UI; the hub stores ONLY a salted
PBKDF2 hash and delivers (hash, salt) to the owning device, which validates the
entered PIN offline. The plaintext PIN never persists and is never returned to
the admin UI. PBKDF2-HMAC-SHA256 is used because both Apple platforms can
recompute it via CommonCrypto (CryptoKit has no PBKDF2).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

ITERATIONS = 100_000
_DKLEN = 32
_SALT_BYTES = 16


def hash_pin(pin: str) -> tuple[str, str]:
    """Return (hash_hex, salt_hex) for a freshly generated random salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt, ITERATIONS, _DKLEN
    )
    return digest.hex(), salt.hex()


def verify_pin(pin: str, hash_hex: str, salt_hex: str) -> bool:
    """Constant-time verify a PIN against a stored (hash, salt). Server-side helper."""
    try:
        salt = bytes.fromhex(salt_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt, ITERATIONS, _DKLEN
    )
    return hmac.compare_digest(digest.hex(), hash_hex)
