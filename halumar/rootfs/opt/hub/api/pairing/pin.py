"""Pairing PIN: generate, status, cancel; PIN store, rate limit, failures.

Apps pair only over the WebSocket (``api/ws/handshake._handle_pair``); there
is no REST pairing endpoint.
"""

import hashlib
import hmac
import logging
import secrets
import time
from typing import Literal
from collections import OrderedDict

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from api import pairing as api_pairing
from api import ws as api_ws
from api.auth import _is_trusted_ingress, require_admin
from api.ws import EventAudience
from models.database import Database
from services.audit_log import log_admin_action, log_pairing_failure
from services.expiry import MAX_GUEST_HOURS, expiry_from_hours, normalize_expiry_input
from models.schemas import normalize_owner_label

logger = logging.getLogger("hub.api.pairing")


# In-memory storage for active pairing PINs
# {pin: {"created_at": timestamp, "expires_at": timestamp, "failed_attempts": int}}
_active_pins: dict[str, dict] = {}

# Default pairing window: 5 minutes
PAIRING_TIMEOUT_SECONDS = 300

# Rate limiting with LRU bound
_pair_rate_limits: OrderedDict[str, list[float]] = OrderedDict()
PAIR_RATE_LIMIT_MAX = 5
PAIR_RATE_LIMIT_WINDOW = 60  # seconds
_PAIR_RATE_MAX_KEYS = 10000

# Failed pairing attempts per source IP across all WS connections, so
# reconnecting does not reset the per-connection limit.
# Successful pairings do not count; a successful
# pairing clears the budget of its IP and a new PIN (admin intent: a fresh
# window) starts with a fresh budget. Since one address cannot invalidate a
# PIN (below), a host that burns its budget only locks itself out.
PAIR_IP_LIMIT_MAX = 10
PAIR_IP_LIMIT_WINDOW = 600  # seconds

# Global failure counter of the active PIN. A PIN is invalidated
# only after MAX_PIN_FAILURES failures from at least MIN_PIN_FAILURE_IPS
# different source IPs: one LAN host (e.g. a guest that sees
# "pairing active" in HA) cannot end every pairing, and the PIN allows at
# most 30 failed attempts at 10^8 possible values.
MAX_PIN_FAILURES = 30
MIN_PIN_FAILURE_IPS = 3

# Dead PINs are remembered for a while so a late device gets a precise
# pair_error reason (pin_expired / too_many_failures) instead of pin_invalid.
RETIRED_PIN_MEMORY_SECONDS = 600
REASON_PIN_EXPIRED = "pin_expired"
REASON_TOO_MANY_FAILURES = "too_many_failures"
_retired_pins: dict[str, tuple[str, float]] = {}


def _pair_rate_check(
    key: str,
    limit: int = PAIR_RATE_LIMIT_MAX,
    window: float = PAIR_RATE_LIMIT_WINDOW,
) -> bool:
    """Check if a pairing rate limit key is within limits. LRU-bounded."""
    now = time.time()
    cutoff = now - window

    # LRU eviction
    while len(_pair_rate_limits) >= _PAIR_RATE_MAX_KEYS and _pair_rate_limits:
        _pair_rate_limits.popitem(last=False)

    if key in _pair_rate_limits:
        _pair_rate_limits[key] = [t for t in _pair_rate_limits[key] if t > cutoff]
        _pair_rate_limits.move_to_end(key)
    else:
        _pair_rate_limits[key] = []

    if len(_pair_rate_limits[key]) >= limit:
        return False

    _pair_rate_limits[key].append(now)
    return True


def _generate_pin() -> str:
    """Generate a numeric pairing PIN in format XXXX-XXXX."""
    part1 = "".join(secrets.choice("0123456789") for _ in range(4))
    part2 = "".join(secrets.choice("0123456789") for _ in range(4))
    return f"{part1}-{part2}"


def _generate_secret() -> str:
    """Generate a 64-char hex secret token for a paired client."""
    return secrets.token_hex(32)


def _hash_secret(secret: str) -> str:
    """Hash a secret using SHA-256 for storage."""
    return hashlib.sha256(secret.encode()).hexdigest()


def _retire_pin(pin: str, reason: str) -> None:
    now = time.time()
    for old in [p for p, (_r, until) in _retired_pins.items() if until < now]:
        del _retired_pins[old]
    _retired_pins[pin] = (reason, now + RETIRED_PIN_MEMORY_SECONDS)


def _cleanup_expired_pins():
    """Remove expired PINs from memory (remembered as expired)."""
    now = time.time()
    expired = [pin for pin, data in _active_pins.items() if data["expires_at"] < now]
    for pin in expired:
        del _active_pins[pin]
        _retire_pin(pin, REASON_PIN_EXPIRED)


# ── Pin access helpers (encapsulate _active_pins for external modules) ───────


def get_active_pin() -> tuple[str, dict] | None:
    """Return the currently active (non-expired) PIN and its data, or None.

    Cleans up expired PINs before checking.
    """
    _cleanup_expired_pins()
    if not _active_pins:
        return None
    pin, data = next(iter(_active_pins.items()))
    remaining = max(0, int(data["expires_at"] - time.time()))
    if remaining <= 0:
        return None
    return pin, data


def _replace_active_pin(pin: str, data: dict) -> None:
    """A new PIN replaces the old one; old dead PINs are no longer reported."""
    _active_pins.clear()
    _retired_pins.clear()
    _reset_ip_budgets()
    _active_pins[pin] = data


def set_active_pin(pin: str, data: dict) -> None:
    """Set a new active PIN, replacing any existing one."""
    _replace_active_pin(pin, data)


def clear_active_pins() -> None:
    """Clear all active PINs."""
    _active_pins.clear()
    _retired_pins.clear()


def remove_pin(pin: str) -> None:
    """Remove a specific PIN (e.g. after successful pairing)."""
    _active_pins.pop(pin, None)


def match_pin(pin: str) -> tuple[str, dict] | None:
    """Find a PIN using constant-time comparison. Returns (pin, data) or None.

    Cleans up expired PINs before matching. Does NOT remove the PIN.
    """
    _cleanup_expired_pins()
    for stored_pin, stored_data in _active_pins.items():
        if hmac.compare_digest(pin.encode(), stored_pin.encode()):
            return stored_pin, stored_data
    return None


def retired_pin_reason(pin: str) -> str | None:
    """Why a recently dead PIN no longer works (constant-time), else None."""
    now = time.time()
    found = None
    for stored_pin, (reason, until) in list(_retired_pins.items()):
        if until >= now and hmac.compare_digest(pin.encode(), stored_pin.encode()):
            found = reason
    return found


_IP_KEY = "pair-ip:"


def pair_ip_allowed(ip: str) -> bool:
    """Whether ``ip`` still has failed-attempt budget (does not consume it)."""
    cutoff = time.time() - PAIR_IP_LIMIT_WINDOW
    failures = [t for t in _pair_rate_limits.get(_IP_KEY + ip, []) if t > cutoff]
    return len(failures) < PAIR_IP_LIMIT_MAX


def _record_ip_failure(ip: str) -> None:
    _pair_rate_check(_IP_KEY + ip, limit=1 << 30, window=PAIR_IP_LIMIT_WINDOW)


def _reset_ip_budgets() -> None:
    """A new PIN starts with a fresh per-IP budget."""
    for key in [k for k in _pair_rate_limits if k.startswith(_IP_KEY)]:
        del _pair_rate_limits[key]


def clear_ip_failures(ip: str) -> None:
    """A successful pairing clears the failed-attempt budget of its IP."""
    _pair_rate_limits.pop(_IP_KEY + ip, None)


async def register_pin_failure(ip: str, reason: str = "invalid_pin") -> bool:
    """Count a failed or rate-limited pairing attempt against the active PIN.

    Returns True when the PIN was invalidated because it reached
    ``MAX_PIN_FAILURES`` from at least ``MIN_PIN_FAILURE_IPS`` source IPs.
    Without an active PIN nothing is counted.
    """
    log_pairing_failure(reason, ip)
    _record_ip_failure(ip)
    # Open guest invitations of members die by the same rule.
    from api.ws import guest_invites as guest_invites_ws
    from services import guest_invites

    await guest_invites_ws.end_invalidated(guest_invites.register_failure(ip))
    active = get_active_pin()
    if not active:
        return False
    pin, data = active
    data["failed_attempts"] = data.get("failed_attempts", 0) + 1
    failed_ips = data.setdefault("failed_ips", set())
    if len(failed_ips) < 64:
        failed_ips.add(ip)
    if (
        data["failed_attempts"] < MAX_PIN_FAILURES
        or len(failed_ips) < MIN_PIN_FAILURE_IPS
    ):
        return False
    _active_pins.pop(pin, None)
    _retire_pin(pin, REASON_TOO_MANY_FAILURES)
    logger.warning(
        "Pairing PIN invalidated after %d failed attempts from %d addresses "
        "(last from %s)",
        data["failed_attempts"],
        len(failed_ips),
        ip,
    )
    log_admin_action("pairing_pin_invalidated", "too_many_failures", ip)
    await api_ws.broadcast(
        {"type": "pairing_cancelled", "data": {"reason": "too_many_failures"}},
        audience=EventAudience.ADMIN,
    )
    return True


def _get_client_ip(request: Request) -> str:
    """Get client IP from the actual TCP connection (not headers).

    Uses request.client.host directly to prevent XFF spoofing.
    """
    if request.client:
        return request.client.host
    return "unknown"


# ── Schemas ──────────────────────────────────────────────────────────────────


class GeneratePinRequest(BaseModel):
    """Optional body of the PIN generation (contracts pairing-generate.request)."""

    model_config = {"extra": "forbid"}

    access: Literal["member", "guest"] = "member"
    profile_id: str | None = Field(
        None,
        max_length=64,
        description="Profile of the new device; null = the default profile of the "
        "device type that pairs (guests: the guest default profile)",
    )
    own_area_id: str | None = Field(None, max_length=64, description="Own area")
    owner_label: str | None = Field(
        None,
        max_length=200,
        description="Owner of the paired device (label only, max 50 characters)",
    )
    guest_hours: int | None = Field(
        None,
        ge=1,
        le=MAX_GUEST_HOURS,
        description="Guests: duration in hours (default: house setting)",
    )
    guest_expires_at: str | None = Field(
        None,
        max_length=100,
        description="Guests: own end, ISO 8601 WITH offset; not with guest_hours",
    )

    @field_validator("guest_expires_at")
    @classmethod
    def validate_guest_expires_at(cls, v):
        # naive/garbage/past -> 422 BEFORE the active PIN is replaced.
        return normalize_expiry_input(v) if v else v

    @field_validator("owner_label")
    @classmethod
    def validate_owner_label(cls, v):
        return normalize_owner_label(v)

    @model_validator(mode="after")
    def guest_fields(self):
        if self.access == "member" and (self.guest_hours or self.guest_expires_at):
            raise ValueError("guest_hours and guest_expires_at are only for guests")
        if self.guest_hours and self.guest_expires_at:
            raise ValueError("guest.expires: guest_hours and guest_expires_at together")
        return self


class GeneratePinResponse(BaseModel):
    """Response when generating a new pairing PIN."""

    pin: str
    expires_in: int = Field(description="Seconds until PIN expires")
    expires_at: str = Field(description="ISO timestamp when PIN expires")
    url: str = Field(description="Base URL encoded in the pairing deep link")
    deep_link: str = Field(
        description="Canonical pairing deep link (QR + copy-link source)"
    )


async def generate_pairing_pin(
    request: Request, data: GeneratePinRequest | None = None
):
    """Generate a new pairing PIN. Admin-only.

    Only accessible via Ingress (admin) or with API token.
    PIN is valid for 5 minutes. Only one PIN can be active at a time.
    """
    require_admin(request)
    _cleanup_expired_pins()

    from services import guest_invites

    pin = api_pairing._generate_pin()
    while guest_invites.pin_in_use(pin):
        pin = api_pairing._generate_pin()
    now = time.time()
    expires_at = now + PAIRING_TIMEOUT_SECONDS

    pin_data = {
        "created_at": now,
        "expires_at": expires_at,
    }

    db: Database = request.app.state.db
    data = data or GeneratePinRequest()
    is_guest = data.access == "guest"
    if data.profile_id is not None:
        prof = await db.fetchone(
            "SELECT id, child_protection FROM profiles WHERE id = ?", (data.profile_id,)
        )
        if not prof:
            raise HTTPException(status_code=404, detail="Profile not found")
        if is_guest and prof["child_protection"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "profile.guest_child_protection",
                    "message": "a guest cannot have parental controls",
                    "field": "profile_id",
                },
            )
        pin_data["profile_id"] = data.profile_id
    elif is_guest:
        from services.profiles import store as profiles

        guest_default = await profiles.default_profile_for(db, None, True)
        prof = await db.fetchone(
            "SELECT child_protection FROM profiles WHERE id = ?", (guest_default,)
        )
        if prof and prof["child_protection"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "profile.guest_child_protection",
                    "message": "the guest default profile has parental controls",
                },
            )
        # Fixed when the window opens (as a chosen profile).
        pin_data["profile_id"] = guest_default
    if data.own_area_id is not None:
        if not await db.fetchone(
            "SELECT id FROM areas WHERE id = ?", (data.own_area_id,)
        ):
            raise HTTPException(status_code=404, detail="Area not found")
        pin_data["own_area_id"] = data.own_area_id

    if is_guest:
        # guest_expires_at is already validated + normalized to UTC.
        from services.profiles import store as profiles

        guest_expiry = data.guest_expires_at or expiry_from_hours(
            data.guest_hours or await profiles.guest_default_hours(db)
        )
        pin_data["is_guest"] = True
        pin_data["guest_expires_at"] = guest_expiry

    # Owner label of the device paired with this PIN.
    if data.owner_label:
        pin_data["owner_label"] = data.owner_label

    # Replaces (invalidates) any existing active PIN.
    _replace_active_pin(pin, pin_data)

    expires_iso = Database.now()  # Approximate, good enough for display

    # No part of the PIN in the log (half of it halves the guessing work).
    pin_type = "guest " if is_guest else ""
    logger.info(
        "Generated %spairing PIN (expires in %ds)",
        pin_type,
        PAIRING_TIMEOUT_SECONDS,
    )

    if is_guest:
        from services import event_log

        await event_log.guest_invited(
            db,
            by_client_id=None,
            actor=getattr(request.state, "actor", None) or "admin",
            until=pin_data.get("guest_expires_at"),
            profile_id=pin_data.get("profile_id"),
        )

    # Tell admin UIs (and the integration push session) a window opened —
    # the payload stays secret-free (never the PIN).
    await api_ws.broadcast(
        {
            "type": "pairing_started",
            "data": {
                "is_guest": is_guest,
                "expires_in": PAIRING_TIMEOUT_SECONDS,
            },
        },
        audience=EventAudience.ADMIN,
    )

    # Canonical deep link: web-UI QR, copy-link and the integration's QR PNG
    # all emit this exact string (single source, services/pairing_link.py).
    from services.pairing_link import build_pairing_deep_link, resolve_base_url

    from services import home_networks

    db = request.app.state.db
    base_url = await resolve_base_url(request, db)

    return GeneratePinResponse(
        pin=pin,
        expires_in=PAIRING_TIMEOUT_SECONDS,
        expires_at=expires_iso,
        url=base_url,
        deep_link=build_pairing_deep_link(base_url, pin, await home_networks.cidrs(db)),
    )


async def pairing_status(request: Request):
    """Get current pairing status. Admin-only.

    Returns whether a PIN is active and how many paired clients exist.
    The PIN itself is only returned for Ingress (admin) requests.
    """
    require_admin(request)
    _cleanup_expired_pins()

    is_admin = _is_trusted_ingress(request)

    active_pin = None
    remaining = 0
    if _active_pins:
        pin, data = next(iter(_active_pins.items()))
        remaining = max(0, int(data["expires_at"] - time.time()))
        if remaining > 0:
            # Only show PIN to admin (Ingress) requests
            active_pin = pin if is_admin else "****-****"

    db: Database = request.app.state.db
    client_count = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM paired_clients WHERE is_active = 1"
    )

    # Get guest info from active PIN
    is_guest_pin = False
    guest_expires_at = None
    if _active_pins and remaining > 0:
        pin_meta = next(iter(_active_pins.values()))
        is_guest_pin = pin_meta.get("is_guest", False)
        guest_expires_at = pin_meta.get("guest_expires_at")

    # url/deep_link only for admin (Ingress) requests while pairing is active:
    # the deep link carries the PIN in cleartext (same gate as the PIN masking).
    url: str | None = None
    deep_link: str | None = None
    if is_admin and remaining > 0 and _active_pins:
        from services import home_networks
        from services.pairing_link import build_pairing_deep_link, resolve_base_url

        pin, _pin_meta = next(iter(_active_pins.items()))
        url = await resolve_base_url(request, db)
        deep_link = build_pairing_deep_link(url, pin, await home_networks.cidrs(db))

    return {
        "pairing_active": remaining > 0 and bool(_active_pins),
        "pin": active_pin,
        "remaining_seconds": remaining,
        "active_clients": client_count["cnt"] if client_count else 0,
        "is_guest_pin": is_guest_pin,
        "guest_expires_at": guest_expires_at,
        "url": url,
        "deep_link": deep_link,
    }


async def cancel_pairing(request: Request):
    """Cancel the currently active pairing PIN. Admin-only."""
    require_admin(request)
    _cleanup_expired_pins()
    if _active_pins:
        clear_active_pins()
        logger.info("Active pairing PIN cancelled by admin")

        await api_ws.broadcast(
            {"type": "pairing_cancelled", "data": {}},
            audience=EventAudience.ADMIN,
        )
    else:
        logger.info("No active pairing PIN to cancel")
