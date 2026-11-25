"""Per-client compliance status derivation + HMAC signing.

The hub derives a small, PII-free compliance status per paired client from
EXISTING state (no new source of truth) and signs the RESTRICTING statuses so
a remote app can verify them without an HA token. The integration serves the
opaque blob unauthenticated; the iOS app verifies the signature with the same
key material before acting.

Crypto contract (byte-identical on the hub and iOS, v1):

- The pairing secret is ``secrets.token_hex(32)`` (a 64-hex string). The
  add-on stores only ``secret_hash = sha256(secret.encode()).hexdigest()``;
  the app holds the secret string itself.
- HMAC key material = the RAW 32-byte SHA-256 digest of the secret string:
  add-on ``bytes.fromhex(secret_hash)``, iOS
  ``Data(SHA256.hash(data: Data(hubSecret.utf8)))``.
- ``k_sign = HMAC_SHA256(key=digest32, message=utf8("hub/compliance/v1"))``
- ``sig = hexlower(HMAC_SHA256(key=k_sign,
  message=utf8("v1|" + client_id + "|" + status + "|" + ts)))`` with ``ts``
  the unix time in seconds as a base-10 integer string.

Signed statuses: ``need_token_update``, ``deleted``. Unsigned neutral:
``compliant``, ``unknown``. The app performs a restricting action ONLY on a
verified signature; unsigned or failed-sig statuses are treated as compliant.
"""

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

from services.expiry import is_expired

logger = logging.getLogger("hub.compliance")

# UTF-8 label for the k_sign derivation (v1 of the compliance contract).
DERIVATION_LABEL = b"hub/compliance/v1"

STATUS_COMPLIANT = "compliant"
STATUS_NEED_TOKEN_UPDATE = "need_token_update"
STATUS_DELETED = "deleted"
STATUS_UNKNOWN = "unknown"

# Only these statuses carry a signature (they authorize restricting actions).
SIGNED_STATUSES = frozenset({STATUS_NEED_TOKEN_UPDATE, STATUS_DELETED})


def derive_signing_key(digest32: bytes) -> bytes:
    """Derive k_sign from the raw 32-byte SHA-256 digest of the secret."""
    return hmac.new(digest32, DERIVATION_LABEL, hashlib.sha256).digest()


def sign_status(secret_hash: str, client_id: str, status: str, ts: int) -> str:
    """Sign ``v1|client_id|status|ts`` per the v1 compliance contract.

    ``secret_hash`` is the stored hex SHA-256 of the client secret; its raw
    bytes are the HMAC key material. Returns the lowercase-hex signature.
    """
    digest32 = bytes.fromhex(secret_hash)
    k_sign = derive_signing_key(digest32)
    message = f"v1|{client_id}|{status}|{ts}".encode()
    return hmac.new(k_sign, message, hashlib.sha256).hexdigest()


def _guest_expired(client: dict) -> bool:
    """Whether a guest client is past its expires_at.

    Aware datetime compare (a naive stored value is read as UTC);
    an unparseable value counts as expired (fail closed).
    """
    if not client.get("is_guest"):
        return False
    return is_expired(client.get("expires_at"))


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _token_newer_than_last_contact(client: dict) -> bool:
    """The HA token changed after the app last talked to the hub.

    The app gets a new token only over the hub (in the home network). A
    device that was away when its token was rotated, or when its remote
    access was switched off, still holds the old, now invalid token: it must
    stop signing in from outside (failed logins, HA IP ban) until it is home
    again.
    """
    if client.get("ha_token_status") != "active":
        return False
    minted = _parse_ts(client.get("ha_token_minted_at"))
    if minted is None:
        return False
    seen = _parse_ts(client.get("last_seen"))
    return seen is None or minted > seen


def derive_status(client: dict) -> str:
    """Derive a client's compliance status from its existing row state.

    - pending-deletion tombstone -> ``deleted``
    - deactivated, a guest past its expiry, or an HA token the app has not
      fetched yet (issued after its last hub contact) -> ``need_token_update``
    - otherwise -> ``compliant`` (a device still waiting for its HA token,
      ``ha_token_status`` pending, is compliant)
    """
    if client.get("pending_deletion_at"):
        return STATUS_DELETED
    if (
        not client.get("is_active")
        or _guest_expired(client)
        or _token_newer_than_last_contact(client)
    ):
        return STATUS_NEED_TOKEN_UPDATE
    return STATUS_COMPLIANT


def build_compliance(client: dict, now: int | None = None) -> dict:
    """Build a client's compliance blob: {status, ts[, sig]}.

    ``sig`` is present ONLY for the signed (restricting) statuses. The blob is
    PII-free and safe to serve unauthenticated via the integration.
    """
    status = derive_status(client)
    ts = int(time.time()) if now is None else int(now)
    blob = {"status": status, "ts": ts}
    if status in SIGNED_STATUSES:
        try:
            blob["sig"] = sign_status(client["secret_hash"], client["id"], status, ts)
        except ValueError:
            # Unusable key material (corrupt secret_hash). Fail NEUTRAL: an
            # unsigned restricting status is treated as compliant by the app,
            # so this can never enable a restricting action.
            logger.error(
                "Cannot sign compliance status for client %s: bad secret_hash",
                client.get("id"),
            )
    return blob
