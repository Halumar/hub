"""Guest invitations by member devices.

A member device whose effective rule ``setup_devices`` contains the kind
guest asks for an invitation (``guest_invite_create``) and shows its PIN as a
large QR code. It chooses within the same frame as when setting up a device
(``services.onboarding.grant_for`` with purpose invite): a guest profile, the
end of the access (at most 90 days) and remote access (only when the profile
does not fix it and the member has it). The guest pairs with the normal
``pair`` and gets exactly that; every later change of the guest stays with
the admin. Invitations live in memory only (a restart ends them, the member
creates a new one).

Rules (contract: invitations by members, AsyncAPI):

* 5 minutes, single use; one open invitation per device (a new one replaces
  it), 10 invitations per device and hour, 20 open in total.
* Invitation PINs work next to the admin's single pairing PIN. Wrong PINs
  count against the per-IP budget like the admin PIN; an invitation dies
  after 30 failures from at least 3 source IPs.
* Who may invite: a paired, active member with ``setup_devices`` and the kind
  guest (guests and parental controls never: boundaries). Checked again when
  the guest pairs (fail closed).
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.services.guest_invites")

INVITE_SECONDS = 300
MAX_INVITES_PER_DEVICE = 10
INVITES_WINDOW_SECONDS = 3600
MAX_OPEN_TOTAL = 20
MAX_PIN_FAILURES = 30
MIN_PIN_FAILURE_IPS = 3
_MAX_TRACKED_KEYS = 10000

MAX_DEFAULT_AREAS = 200

# guest_invite_error reasons (contracts $defs/guest_invite_error_reason).
INVALID_REQUEST = "invalid_request"
NOT_PERMITTED = "not_permitted"
RATE_LIMITED = "rate_limited"
NO_GUEST_AREAS = "no_guest_areas"
PROFILE_NOT_ALLOWED = "profile_not_allowed"
EXPIRY_NOT_ALLOWED = "expiry_not_allowed"
ACCESS_HOURS_NOT_ALLOWED = "access_hours_not_allowed"
INVITE_NOT_FOUND = "invite_not_found"

MESSAGES = {
    INVALID_REQUEST: "Invalid invitation request",
    NOT_PERMITTED: "This device may not invite guests",
    RATE_LIMITED: "Too many invitations. Please try again later.",
    NO_GUEST_AREAS: "The guest profile shows no area",
    PROFILE_NOT_ALLOWED: "This profile may not be given from this device",
    EXPIRY_NOT_ALLOWED: "Invalid end of the guest access",
    ACCESS_HOURS_NOT_ALLOWED: "The profile fixes the access hours",
    INVITE_NOT_FOUND: "Unknown or finished invitation",
}

# guest_invite_ended results (contracts $defs/guest_invite_result).
RESULT_PAIRED = "paired"
RESULT_EXPIRED = "expired"
RESULT_CANCELLED = "cancelled"
RESULT_REVOKED = "revoked"
RESULT_REPLACED = "replaced"
RESULT_INVALIDATED = "invalidated"


@dataclass
class Invite:
    """One open guest invitation (memory only)."""

    pin: str
    client_id: str
    inviter_name: str
    created_at: float
    expires_at: float
    profile_id: str | None = None
    guest_expires_at: str | None = None
    remote: bool | None = None
    owner_label: str | None = None
    access_windows: list | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    failed_attempts: int = 0
    failed_ips: set[str] = field(default_factory=set)
    task: Any = None

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def invited_by(self) -> dict:
        return {"client_id": self.client_id, "name": self.inviter_name}

    def admin_view(self) -> dict:
        """Admin list entry: never the PIN."""
        return {
            "id": self.id,
            "invited_by": self.invited_by(),
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
        }


_invites: dict[str, Invite] = {}
_created: OrderedDict[str, list[float]] = OrderedDict()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def reset() -> None:
    """Forget every invitation and counter (tests, shutdown)."""
    for invite in _invites.values():
        if invite.task is not None:
            with contextlib.suppress(RuntimeError):  # loop already closed
                invite.task.cancel()
    _invites.clear()
    _created.clear()


# ── Invitations ──────────────────────────────────────────────────────────────


def open_invites() -> list[Invite]:
    """Open invitations, oldest first (expired ones are not listed)."""
    now = time.time()
    return sorted(
        (i for i in _invites.values() if not i.expired(now)),
        key=lambda i: i.created_at,
    )


def invite_of(client_id: str) -> Invite | None:
    for invite in _invites.values():
        if invite.client_id == client_id:
            return invite
    return None


def by_id(invite_id: str) -> Invite | None:
    if not isinstance(invite_id, str):
        return None
    return _invites.get(invite_id)


def pin_in_use(pin: str) -> bool:
    return any(hmac.compare_digest(pin, i.pin) for i in _invites.values())


def match(pin: str) -> Invite | None:
    """Open invitation with this PIN (constant-time over all), else None."""
    if not isinstance(pin, str):
        return None
    now = time.time()
    found = None
    for invite in list(_invites.values()):
        if hmac.compare_digest(pin.encode(), invite.pin.encode()) and not (
            invite.expired(now)
        ):
            found = invite
    return found


def _window(key: str, now: float) -> list[float]:
    while len(_created) >= _MAX_TRACKED_KEYS and key not in _created:
        _created.popitem(last=False)
    hits = [t for t in _created.get(key, []) if t > now - INVITES_WINDOW_SECONDS]
    _created[key] = hits
    _created.move_to_end(key)
    return hits


def create_allowed(client_id: str) -> bool:
    """Limits of a new invitation of ``client_id`` (its own open one is replaced)."""
    now = time.time()
    others = [
        i for i in _invites.values() if not i.expired(now) and i.client_id != client_id
    ]
    if len(others) >= MAX_OPEN_TOTAL:
        return False
    return len(_window(client_id, now)) < MAX_INVITES_PER_DEVICE


def create(
    *,
    client_id: str,
    inviter_name: str,
    pin: str,
    profile_id: str | None = None,
    guest_expires_at: str | None = None,
    remote: bool | None = None,
    owner_label: str | None = None,
    access_windows: list | None = None,
) -> Invite:
    """Register a new invitation (caller checked :func:`create_allowed`)."""
    now = time.time()
    _window(client_id, now).append(now)
    invite = Invite(
        pin=pin,
        client_id=client_id,
        inviter_name=inviter_name,
        created_at=now,
        expires_at=now + INVITE_SECONDS,
        profile_id=profile_id,
        guest_expires_at=guest_expires_at,
        remote=remote,
        owner_label=owner_label,
        access_windows=access_windows,
    )
    _invites[invite.id] = invite
    return invite


def remove(invite: Invite) -> bool:
    """Take an invitation out of the store; False when it was already gone."""
    if _invites.pop(invite.id, None) is None:
        return False
    if invite.task is not None:
        invite.task.cancel()
        invite.task = None
    return True


def register_failure(ip: str) -> list[Invite]:
    """Count a wrong PIN against every open invitation.

    Returns the invitations that reached ``MAX_PIN_FAILURES`` from at least
    ``MIN_PIN_FAILURE_IPS`` source IPs (the caller ends them).
    """
    dead = []
    for invite in open_invites():
        invite.failed_attempts += 1
        if len(invite.failed_ips) < 64:
            invite.failed_ips.add(ip)
        if (
            invite.failed_attempts >= MAX_PIN_FAILURES
            and len(invite.failed_ips) >= MIN_PIN_FAILURE_IPS
        ):
            dead.append(invite)
    return dead


# ── Permission ──────────────────────────────────────────────────────────────


def is_member(row: dict | None) -> bool:
    """Active, non-guest device."""
    return bool(
        row
        and row.get("is_active")
        and not row.get("pending_deletion_at")
        and row.get("access") != "guest"
    )


async def device_row(db: Database, client_id: str) -> dict | None:
    return await db.fetchone(
        "SELECT name, is_active, access, pending_deletion_at, profile_id "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )


async def may_invite(db: Database, client_id: str) -> bool:
    """Rule ``setup_devices`` with the kind guest."""
    from services import onboarding

    return await onboarding.may_invite(db, client_id)


# ── What a guest of an invitation gets ──────────────────────────────────────


@dataclass
class Choice:
    """Checked choice of a guest_invite_create."""

    profile: dict
    guest_expires_at: str | None
    remote: bool | None
    owner_label: str | None
    access_hours: dict | None = None  # device adjustment of access_hours
    access_windows: list | None = None

    def overrides(self) -> dict:
        out: dict = {}
        if self.remote is not None:
            out["remote_access"] = self.remote
        if self.access_hours is not None:
            out["access_hours"] = self.access_hours
        return out


async def choose(
    db: Database, client_id: str, message: dict, platform: str = "ios"
) -> Choice:
    """Check the member's choice against its grant (raises SetupError).

    ``platform``: the invited device's; at the invitation a phone, at the
    pairing the platform the device reports (no escalation on what it is)."""
    from services import onboarding
    from services.profiles import store

    grant = await onboarding.grant_for(db, client_id, platform, "invite")
    profiles = grant["kinds"][0]["profiles"] if grant["kinds"] else []
    if not profiles:
        raise onboarding.SetupError(PROFILE_NOT_ALLOWED)
    wanted = message.get("profile_id")
    if wanted is None:
        default_id = await store.default_profile_for(db, None, True)
        entry = next((p for p in profiles if p["id"] == default_id), profiles[0])
    else:
        entry = next((p for p in profiles if p["id"] == wanted), None)
        if entry is None:
            raise onboarding.SetupError(PROFILE_NOT_ALLOWED)
    try:
        expires = onboarding.parse_expiry(message.get("expires_at"), required=False)
    except onboarding.SetupError:
        raise onboarding.SetupError(EXPIRY_NOT_ALLOWED) from None
    remote = message.get("remote")
    if remote is not None:
        if not isinstance(remote, bool):
            raise onboarding.SetupError(INVALID_REQUEST)
        if remote == entry["remote_access"]:
            remote = None
        elif not entry["remote_choice"]:
            raise onboarding.SetupError(EXPIRY_NOT_ALLOWED)
    try:
        owner = onboarding._clean_label(message.get("owner_label"), 50)
    except onboarding.SetupError:
        raise onboarding.SetupError(INVALID_REQUEST) from None
    windows = message.get("access_windows")
    found = await onboarding.confirmer(db, client_id)
    hours = onboarding.parse_access_windows(windows, entry, found[2] if found else None)
    prof = await store.profile_row(db, entry["id"])
    if prof is None:
        raise onboarding.SetupError(PROFILE_NOT_ALLOWED)
    return Choice(prof, expires, remote, owner, hours, windows if hours else None)


async def visible_area_names(
    db: Database, profile_row: dict, overrides: dict | None = None
) -> tuple[bool, list[str]]:
    """(all areas, names of the active areas) a new guest with this profile sees."""
    from api import authz
    from services import onboarding

    rules = onboarding.new_device_rules(
        profile_row, None, guest=True, overrides=overrides
    )
    scope = authz.scope_from_rule(rules["areas"]["value"])
    areas = await db.fetchall(
        "SELECT id, name FROM areas WHERE is_active = 1 ORDER BY sort_order, name"
    )
    by_area: dict[str, list[str]] = {}
    for r in await db.fetchall("SELECT area_id, tag FROM area_tags"):
        by_area.setdefault(r["area_id"], []).append(r["tag"])
    names = [
        a["name"]
        for a in areas
        if authz.area_in_scope(scope, by_area.get(a["id"], []), a["id"])
    ][:MAX_DEFAULT_AREAS]
    return scope.all, names


async def grant(db: Database, choice: Choice) -> dict:
    """What a guest of an invitation gets (guest_invite.grant, display only)."""
    from services import onboarding
    from services.profiles import store

    overrides = choice.overrides()
    all_areas, names = await visible_area_names(db, choice.profile)
    rules = onboarding.new_device_rules(
        choice.profile, None, guest=True, overrides=overrides
    )
    return {
        "all_areas": all_areas,
        "area_names": names,
        "expires_hours": await store.guest_default_hours(db),
        "expires_at": choice.guest_expires_at,
        "profile": {
            "id": choice.profile["id"],
            "name": choice.profile["name"],
            "kind": choice.profile.get("kind") or "guest",
        },
        "remote_allowed": rules["remote_access"]["value"] is True,
        "owner_label": choice.owner_label,
        "access_hours": rules["access_hours"]["value"],
    }
