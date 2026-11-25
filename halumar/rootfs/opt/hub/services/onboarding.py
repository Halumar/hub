"""Setting up a device from an already paired device.

An unpaired device of any app platform opens a one-time session
(``onboard_start``) with an ephemeral X25519 public key and shows a QR code
(hub id, short TLS fingerprint, session id, public key) and a 6-digit code.
A paired device with the rule ``setup_devices`` scans the QR code (and checks
the fingerprint against its own pin) or types the code, reads the session
(``onboard_lookup`` → ``onboard_info`` with the grant) and confirms
(``onboard_confirm``) with a kind and a profile. The hub creates the device
and seals its client secret to the waiting device's key (HPKE,
``services/onboard_crypto.py``). Sessions live in memory only: a restart ends
them (the device shows a new code). Contract: docs/setup.md.

Rules:

* 5 minutes, single use; one session per connection (a new start replaces
  it); TLS port only.
* Limits: per source IP 3 open sessions and 20 starts per 10 minutes, 20
  open sessions in total; per confirming device 10 failures per 10 minutes;
  wrong codes 5 per confirming device and 20 on the hub per 10 minutes.
* A lookup/confirm with another public key or hub id than the session ends
  the session (``session_invalidated``).
* Who may confirm: an active member whose effective rule ``setup_devices`` is
  on (only profiles of kind person; guests and parental controls never).
* What it may give (:func:`grant_for`): the kinds of ``setup_devices.kinds``
  that fit the waiting device's platform, and per kind the profiles that are
  not stronger than the confirming device (:func:`stronger_rules`). A watch
  is the phone's own watch: its default profile, needs the rule ``watch``.
* A child's watch (contract docs/setup.md): a phone with
  parental controls whose effective ``watch`` is on (``kids_watch``) binds
  its watch's session with ``onboard_lookup`` (:func:`child_watch_phone`,
  ``Session.child_phone``) but never confirms; an adult with
  ``setup_devices`` kind child confirms it (kind child, the phone's profile),
  the watch is the phone's (``set_up_by``) and follows it.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from models.database import Database
from services.protocol import ClientInfo

logger = logging.getLogger("hub.services.onboarding")

SESSION_SECONDS = 300
MAX_OPEN_PER_IP = 3
MAX_OPEN_TOTAL = 20
MAX_STARTS_PER_IP = 20
STARTS_WINDOW_SECONDS = 600
MAX_FAILURES_PER_DEVICE = 10
FAILURES_WINDOW_SECONDS = 600
_MAX_TRACKED_KEYS = 10000


# Platforms that may wait for onboarding (contracts onboard_start): every app
# platform; the web pairs with the PIN.
ONBOARD_PLATFORMS = frozenset(
    {
        "ios",
        "ipados",
        "tvos",
        "watchos",
        "android",
        "android_tablet",
        "android_tv",
        "wearos",
    }
)
CODE_DIGITS = 6
MAX_CODE_FAILURES_PER_DEVICE = 5
MAX_CODE_FAILURES_TOTAL = 20
CODE_FAILURES_WINDOW_SECONDS = 600
# Watches: added only by a phone whose rule ``watch`` is on.
WATCH_PLATFORMS = frozenset({"watchos", "wearos"})
MAX_AREA_CHOICE = 200

# onboard_error reasons (contracts $defs/onboard_error_reason).
INVALID_REQUEST = "invalid_request"
INVALID_CLIENT_INFO = "invalid_client_info"
TLS_REQUIRED = "tls_required"
RATE_LIMITED = "rate_limited"
ALREADY_AUTHENTICATED = "already_authenticated"
CONNECTION_EXPIRED = "connection_expired"
SESSION_NOT_FOUND = "session_not_found"
SESSION_EXPIRED = "session_expired"
SESSION_INVALIDATED = "session_invalidated"
HUB_MISMATCH = "hub_mismatch"
NOT_PERMITTED = "not_permitted"
DECLINED = "declined"
CANCELLED = "cancelled"
PLATFORM_NOT_SUPPORTED = "platform_not_supported"
AREAS_NOT_ALLOWED = "areas_not_allowed"
TOO_MANY_ATTEMPTS = "too_many_attempts"
KIND_NOT_ALLOWED = "kind_not_allowed"
PROFILE_NOT_ALLOWED = "profile_not_allowed"
EXPIRY_NOT_ALLOWED = "expiry_not_allowed"
MANAGERS_NOT_ALLOWED = "managers_not_allowed"
ACCESS_HOURS_NOT_ALLOWED = "access_hours_not_allowed"
DEVICE_NOT_FOUND = "device_not_found"

MESSAGES = {
    INVALID_REQUEST: "Invalid onboarding request",
    INVALID_CLIENT_INFO: "Invalid client info",
    TLS_REQUIRED: "Onboarding is only available over TLS (port 8100)",
    RATE_LIMITED: "Too many onboarding attempts. Please try again later.",
    ALREADY_AUTHENTICATED: "Already authenticated - onboarding not needed",
    CONNECTION_EXPIRED: "Connection expired. Reconnect to try again.",
    SESSION_NOT_FOUND: "Unknown or finished onboarding session",
    SESSION_EXPIRED: "Onboarding session expired",
    SESSION_INVALIDATED: "Onboarding session invalidated (key or hub mismatch)",
    HUB_MISMATCH: "This code belongs to another hub",
    NOT_PERMITTED: "This device may not set up devices",
    DECLINED: "Onboarding declined",
    CANCELLED: "Onboarding cancelled by an admin",
    PLATFORM_NOT_SUPPORTED: "This device pairs with the PIN",
    AREAS_NOT_ALLOWED: "Area choice not allowed",
    TOO_MANY_ATTEMPTS: "Too many wrong codes. Scan the QR code or try again later.",
    KIND_NOT_ALLOWED: "This kind may not be set up from this device",
    PROFILE_NOT_ALLOWED: "This profile may not be given from this device",
    EXPIRY_NOT_ALLOWED: "Invalid end of the guest access",
    MANAGERS_NOT_ALLOWED: "Family managers may not be chosen",
    ACCESS_HOURS_NOT_ALLOWED: "The profile fixes the access hours",
    DEVICE_NOT_FOUND: "Not a device set up by this device",
}

# onboarding_ended results (contracts $defs/onboarding_result).
RESULT_CONFIRMED = "confirmed"
RESULT_DECLINED = "declined"
RESULT_EXPIRED = "expired"
RESULT_CANCELLED = "cancelled"
RESULT_INVALIDATED = "invalidated"
RESULT_REPLACED = "replaced"
RESULT_DISCONNECTED = "disconnected"


@dataclass
class Session:
    """One running onboarding session (memory only)."""

    session_id: str
    public_key: bytes
    public_key_b64: str
    client_name: str
    info: ClientInfo
    ip: str
    websocket: Any
    created_at: float
    expires_at: float
    code: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    claimed_by: dict | None = None
    claimed_ids: set[str] = field(default_factory=set)
    # A child's watch: {client_id, name} of the child's phone that bound it.
    child_phone: dict | None = None

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def admin_view(self) -> dict:
        """Admin list entry: never the session id or the key."""
        return {
            "id": self.id,
            "client_name": self.client_name,
            "platform": self.info.platform,
            "app_version": self.info.app_version,
            "device_model": self.info.device_model,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
            "source_ip": self.ip,
            "claimed_by": dict(self.claimed_by) if self.claimed_by else None,
        }


_sessions: dict[str, Session] = {}
_starts: OrderedDict[str, list[float]] = OrderedDict()
_failures: OrderedDict[str, list[float]] = OrderedDict()
_code_failures: OrderedDict[str, list[float]] = OrderedDict()
_CODE_TOTAL_KEY = "*"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def reset() -> None:
    """Forget every session and counter (tests, shutdown)."""
    _sessions.clear()
    _starts.clear()
    _failures.clear()
    _code_failures.clear()


def _window(store: OrderedDict, key: str, window: float, now: float) -> list[float]:
    while len(store) >= _MAX_TRACKED_KEYS and key not in store:
        store.popitem(last=False)
    hits = [t for t in store.get(key, []) if t > now - window]
    store[key] = hits
    store.move_to_end(key)
    return hits


# ── Sessions ─────────────────────────────────────────────────────────────────


def sessions() -> list[Session]:
    """Running sessions, oldest first (expired ones are not listed)."""
    now = time.time()
    return sorted(
        (s for s in _sessions.values() if not s.expired(now)),
        key=lambda s: s.created_at,
    )


def session_of(websocket) -> Session | None:
    for session in _sessions.values():
        if session.websocket is websocket:
            return session
    return None


def by_admin_id(ref: str) -> Session | None:
    for session in _sessions.values():
        if session.id == ref:
            return session
    return None


def find(session_id: str) -> Session | None:
    """Session by id (constant-time compare), including expired ones."""
    if not isinstance(session_id, str):
        return None
    found = None
    for stored_id, session in list(_sessions.items()):
        if hmac.compare_digest(stored_id.encode(), session_id.encode()):
            found = session
    return found


def start_allowed(ip: str, websocket=None) -> bool:
    """Limits of a new start from ``ip`` (the session it replaces does not count)."""
    now = time.time()
    open_sessions = [
        s
        for s in _sessions.values()
        if not s.expired(now) and s.websocket is not websocket
    ]
    if len(open_sessions) >= MAX_OPEN_TOTAL:
        return False
    if sum(1 for s in open_sessions if s.ip == ip) >= MAX_OPEN_PER_IP:
        return False
    return len(_window(_starts, ip, STARTS_WINDOW_SECONDS, now)) < MAX_STARTS_PER_IP


def create(
    *,
    websocket,
    ip: str,
    public_key: bytes,
    public_key_b64: str,
    client_name: str,
    info: ClientInfo,
) -> Session:
    """Register a new session (caller checked :func:`start_allowed`)."""
    now = time.time()
    _window(_starts, ip, STARTS_WINDOW_SECONDS, now).append(now)
    in_use = {s.code for s in _sessions.values() if s.code}
    while True:
        code = f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"
        if code not in in_use:
            break
    session = Session(
        code=code,
        session_id=secrets.token_urlsafe(16),
        public_key=public_key,
        public_key_b64=public_key_b64,
        client_name=client_name,
        info=info,
        ip=ip,
        websocket=websocket,
        created_at=now,
        expires_at=now + SESSION_SECONDS,
    )
    _sessions[session.session_id] = session
    return session


def find_by_code(code: str) -> Session | None:
    """Open session with this unused code (constant-time over all)."""
    if not isinstance(code, str) or len(code) != CODE_DIGITS or not code.isdigit():
        return None
    now = time.time()
    found = None
    for session in list(_sessions.values()):
        stored = session.code or "x" * CODE_DIGITS
        if hmac.compare_digest(stored.encode(), code.encode()) and not session.expired(
            now
        ):
            found = session
    return found


def code_attempt_allowed(client_id: str) -> bool:
    """Wrong codes: per confirming device and on the hub (x-limits onboard_code_*)."""
    now = time.time()
    mine = _window(_code_failures, client_id, CODE_FAILURES_WINDOW_SECONDS, now)
    total = _window(_code_failures, _CODE_TOTAL_KEY, CODE_FAILURES_WINDOW_SECONDS, now)
    return (
        len(mine) < MAX_CODE_FAILURES_PER_DEVICE
        and len(total) < MAX_CODE_FAILURES_TOTAL
    )


def record_code_failure(client_id: str) -> None:
    now = time.time()
    _window(_code_failures, client_id, CODE_FAILURES_WINDOW_SECONDS, now).append(now)
    _window(_code_failures, _CODE_TOTAL_KEY, CODE_FAILURES_WINDOW_SECONDS, now).append(
        now
    )


def remove(session: Session) -> bool:
    """Take a session out of the store; False when it was already gone."""
    return _sessions.pop(session.session_id, None) is not None


def matches(
    session: Session, public_key: str, hub_id: str, own_hub_id: str
) -> str | None:
    """None when key and hub id match the session, else the error reason."""
    if not isinstance(hub_id, str) or not hmac.compare_digest(
        hub_id.lower().encode(), own_hub_id.lower().encode()
    ):
        return HUB_MISMATCH
    if not isinstance(public_key, str) or not hmac.compare_digest(
        public_key.encode(), session.public_key_b64.encode()
    ):
        return SESSION_INVALIDATED
    return None


# ── Failures of confirming devices ──────────────────────────────────────────


def failures_allowed(client_id: str) -> bool:
    hits = _window(_failures, client_id, FAILURES_WINDOW_SECONDS, time.time())
    return len(hits) < MAX_FAILURES_PER_DEVICE


def record_failure(client_id: str) -> None:
    now = time.time()
    _window(_failures, client_id, FAILURES_WINDOW_SECONDS, now).append(now)


# ── Permission ──────────────────────────────────────────────────────────────


class SetupError(Exception):
    """A request of a setting-up device was refused (``onboard_error`` reason)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_member(row: dict | None) -> bool:
    return bool(
        row
        and row.get("is_active")
        and not row.get("pending_deletion_at")
        and row.get("access") != "guest"
    )


async def confirmer(db: Database, client_id: str) -> tuple[dict, dict, dict] | None:
    """(client row, profile row, effective rules) of a device that may set up
    devices (effective ``setup_devices`` on), else None."""
    from services.profiles import store

    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        return None
    row, prof, rules = resolved
    if not _is_member(row):
        return None
    from services import access

    if not access.is_open(row, prof, rules):
        return None  # outside its access a device sets up nothing
    value = rules["setup_devices"]["value"]
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    return row, prof, rules


async def child_watch_phone(
    db: Database, client_id: str
) -> tuple[dict, dict, dict] | None:
    """(row, profile row, effective rules) of a phone with parental controls
    that may add its own watch (effective rule ``watch`` on, i.e. the kids
    rule ``kids_watch``), else None. It never confirms a setup itself."""
    from services.profiles import store

    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        return None
    row, prof, rules = resolved
    if not _is_member(row) or not prof["child_protection"]:
        return None
    if row.get("platform") in WATCH_PLATFORMS or store.carrier_of(row):
        return None
    from services import access

    if not access.is_open(row, prof, rules):
        return None
    if (rules.get("watch") or {}).get("value") is not True:
        return None
    return row, prof, rules


def child_watch_grant(phone_name: str) -> dict:
    """``onboard_info.grant`` for the child's phone that bound its watch's
    session: nothing to choose, it shows the code to an adult."""
    from services.profiles import catalog as cat

    ga = cat.guest_access()
    return {
        "tags": [],
        "all_areas": False,
        "areas": [],
        "kinds": [],
        "managers_choice": False,
        "managers": [],
        "guest_access": {
            "default_hours": ga["default_hours"],
            "max_hours": ga["max_hours"],
            "presets_hours": ga["presets_hours"],
        },
        "own_watch": True,
        "child_watch": {"role": "child", "phone_name": phone_name or "-"},
    }


async def may_set_up(db: Database, client_id: str) -> bool:
    return await confirmer(db, client_id) is not None


async def may_invite(db: Database, client_id: str) -> bool:
    """``setup_devices`` with the kind guest."""
    found = await confirmer(db, client_id)
    return bool(found) and "guest" in found[2]["setup_devices"]["value"]["kinds"]


async def is_member(db: Database, client_id: str) -> bool:
    row = await db.fetchone(
        "SELECT is_active, access, pending_deletion_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    return _is_member(row)


# ── No escalation (docs/setup.md) ───────────────────────────────────────────


def _remote_stronger(new: dict, mine: dict) -> bool:
    from services.profiles import resolver

    return resolver.remote_wider(new, mine)


def hours_wider(new: Any, mine: Any) -> bool:
    """Whether access hours ``new`` give access at a time ``mine`` does not:
    a device with access hours sets up nothing that has access
    outside them (``resolver.hours_wider``, also used to limit a watch)."""
    from services.profiles import resolver

    return resolver.hours_wider(new, mine)


def stronger_rules(new: dict[str, dict], mine: dict[str, dict]) -> list[str]:
    """Rules in which ``new`` gives more rights than ``mine`` (empty = not
    stronger). Compared: security critical/sensitive outside kids and
    location, both applicable; areas are checked separately."""
    from services.profiles import catalog as cat

    out = []
    for rule in cat.catalog()["rules"]:
        key = rule["key"]
        if rule["security"] not in ("critical", "sensitive") or rule["group"] in (
            "kids",
            "location",
        ):
            continue
        if key == "areas":
            continue
        n, m = new.get(key) or {}, mine.get(key) or {}
        if "not_applicable" in (n.get("source"), m.get("source")):
            continue
        nv, mv = n.get("value"), m.get("value")
        if key == "control_path":
            # By what each device holds of Home Assistant (none < read-only <
            # full), the new one resolved on its platform: a phone on Hub only
            # with remote access may set up a TV or a watch (Checked there).
            from services.profiles import resolver

            if resolver.key_looser(new, mine):
                out.append(key)
        elif key == "setup_devices":
            if (nv or {}).get("enabled") and (
                not (mv or {}).get("enabled")
                or set(nv.get("kinds", [])) - set((mv or {}).get("kinds", []))
            ):
                out.append(key)
        elif key == "access_hours":
            if hours_wider(nv, mv):
                out.append(key)
        elif key == "remote_control":
            if (
                isinstance(nv, dict)
                and isinstance(mv, dict)
                and _remote_stronger(nv, mv)
            ):
                out.append(key)
        elif rule.get("order"):
            i, j = cat.order_index(key, nv), cat.order_index(key, mv)
            if i is None or j is None or i > j:
                out.append(key)
    return out


# ── Grant ───────────────────────────────────────────────────────────────────


async def _confirmer_scope(db: Database, confirming_client_id: str):
    """``(scope, visible areas in display order)`` of a confirming device."""
    from api import authz

    scope = await authz.client_area_scope(db, confirming_client_id)
    areas = await db.fetchall(
        "SELECT id, name, icon FROM areas WHERE is_active = 1 ORDER BY sort_order, name"
    )
    tag_rows = await db.fetchall("SELECT area_id, tag FROM area_tags")
    by_area: dict[str, list[str]] = {}
    for r in tag_rows:
        by_area.setdefault(r["area_id"], []).append(r["tag"])
    visible = [
        a
        for a in areas
        if authz.area_in_scope(scope, by_area.get(a["id"], []), a["id"])
    ]
    return scope, visible, by_area


def _far_future() -> str:
    return "9999-12-31T00:00:00Z"


def new_device_rules(
    prof: dict,
    platform: str | None,
    *,
    guest: bool,
    overrides: dict | None = None,
    own_area_id: str | None = None,
) -> dict[str, dict]:
    """Effective rules of a device about to be set up with ``prof``."""
    from services.profiles import store

    client = {
        "platform": platform,
        "access": "guest" if guest else "member",
        "expires_at": _far_future() if guest else None,
        "own_area_id": own_area_id,
    }
    overs = [
        {
            "rule_key": k,
            "value_json": json.dumps(v),
            "user_choice": 0,
            "expires_at": None,
        }
        for k, v in (overrides or {}).items()
    ]
    return store.resolve_inputs(store.device_inputs(client, prof, overs))


def _visible_ids(scope, areas: list[dict], by_area: dict) -> list[str]:
    from api import authz

    return [
        a["id"]
        for a in areas
        if authz.area_in_scope(scope, by_area.get(a["id"], []), a["id"])
    ]


async def _all_active_areas(db: Database) -> list[dict]:
    return await db.fetchall(
        "SELECT id, name, icon FROM areas WHERE is_active = 1 ORDER BY sort_order, name"
    )


async def _profile_entry(
    db: Database,
    prof: dict,
    *,
    kind: str,
    platform: str | None,
    mine: dict,
    my_visible: list[dict],
    all_areas: list[dict],
    by_area: dict,
    purpose: str,
    fixed_areas: bool = False,
) -> dict | None:
    """Grant entry of one profile, or None when it would give more rights.

    ``fixed_areas``: no area choice (a child's watch follows its phone)."""
    from api import authz
    from services.profiles import catalog as cat
    from services.profiles import store

    guest = kind in cat.GUEST_ACCESS_KINDS
    if guest and prof["child_protection"]:
        return None
    body = store.body_of(prof)
    rules = new_device_rules(prof, platform, guest=guest)
    if stronger_rules(rules, mine):
        return None
    area_choice = (
        purpose == "onboard" and not fixed_areas and "areas" not in body["fixed"]
    )
    entry: dict[str, Any] = {
        "id": prof["id"],
        "name": prof["name"],
        "kind": kind,
        "description": prof.get("description"),
        "icon": prof.get("icon"),
        "color": prof.get("color"),
        "child_protection": bool(prof["child_protection"]),
        "area_choice": area_choice,
    }
    if not area_choice:
        scope = authz.scope_from_rule(rules["areas"]["value"])
        ids = _visible_ids(scope, all_areas, by_area)
        mine_ids = {a["id"] for a in my_visible}
        if not set(ids) <= mine_ids:
            return None
        entry["profile_areas"] = {
            "all_areas": scope.all,
            "area_names": [a["name"] for a in all_areas if a["id"] in ids][
                :MAX_AREA_CHOICE
            ],
        }
    remote = rules["remote_access"]
    entry["remote_access"] = remote["value"] is True
    entry["remote_choice"] = bool(
        guest
        and remote["source"] != "not_applicable"
        and "remote_access" not in body["fixed"]
        and mine["remote_access"]["value"] is True
    )
    entry["start_area_choice"] = bool(
        purpose == "onboard"
        and kind == "panel"
        and rules["panel_start"]["source"] != "not_applicable"
        and "panel_start" not in body["fixed"]
    )
    # Access hours may be chosen unless the profile fixes them.
    hours = rules["access_hours"]
    applies = hours["source"] != "not_applicable"
    entry["access_hours_choice"] = applies and "access_hours" not in body["fixed"]
    entry["access_hours"] = (
        hours["value"] if applies else {"enabled": False, "windows": []}
    )
    return entry


def parse_access_windows(
    value: Any, entry: dict, mine: dict | None = None
) -> dict | None:
    """``access_windows`` of onboard_confirm / guest_invite_create.

    Returns the device adjustment of ``access_hours`` (None = keep the
    profile value); raises SetupError. Checked like the rule editor; never
    outside the setting-up device's own access hours (``mine``, its
    effective rules)."""
    from services.profiles import catalog as cat
    from services.profiles.catalog import ProfileError

    if value is None:
        return None
    if not entry.get("access_hours_choice"):
        raise SetupError(ACCESS_HOURS_NOT_ALLOWED)
    if not isinstance(value, list) or not 1 <= len(value) <= 14:
        raise SetupError(INVALID_REQUEST)
    try:
        hours = cat.check_value("access_hours", {"enabled": True, "windows": value})
    except ProfileError:
        raise SetupError(INVALID_REQUEST) from None
    if mine is not None and hours_wider(
        hours, (mine.get("access_hours") or {}).get("value")
    ):
        raise SetupError(ACCESS_HOURS_NOT_ALLOWED)
    return None if hours == entry.get("access_hours") else hours


async def managers_for(db: Database, confirming_client_id: str) -> list[dict]:
    """Family manager devices (grant.managers)."""
    from services.family import service as family

    out = []
    for m in await family.all_managers(db):
        out.append(
            {
                "client_id": m.client_id,
                "name": m.name,
                "owner_label": m.owner_label,
                "all_children": bool(m.scope["all"]),
                "is_self": m.client_id == confirming_client_id,
            }
        )
    return out[:100]


async def grant_for(
    db: Database,
    confirming_client_id: str,
    platform: str | None = None,
    purpose: str = "onboard",
    session: Session | None = None,
) -> dict:
    """What ``confirming_client_id`` may give to a device of ``platform``
    (common/setup-grant.json). Raises :class:`SetupError` without the right.

    ``session``: the looked-up session; one a child's phone bound is the
    watch of that child (:func:`_child_watch_kinds`)."""
    from services.profiles import catalog as cat
    from services.profiles import store

    found = await confirmer(db, confirming_client_id)
    if not found:
        raise SetupError(NOT_PERMITTED)
    _row, _prof, mine = found
    scope, visible, by_area = await _confirmer_scope(db, confirming_client_id)
    all_areas = await _all_active_areas(db)
    allowed = set(mine["setup_devices"]["value"]["kinds"])
    if purpose == "invite":
        allowed &= {"guest"}
    profiles = await store.profile_rows(db)
    kinds: list[dict] = []
    own_watch = purpose == "onboard" and platform in WATCH_PLATFORMS
    child_phone = session.child_phone if own_watch and session is not None else None
    if child_phone is not None:
        own_watch = False
    common = {
        "platform": platform,
        "mine": mine,
        "my_visible": visible,
        "all_areas": all_areas,
        "by_area": by_area,
        "purpose": purpose,
    }
    if child_phone is not None:
        kinds = await _child_watch_kinds(
            db, confirming_client_id, child_phone, allowed, common
        )
    elif own_watch:
        if mine.get("watch", {}).get("value") is True:
            pid = await store.default_profile_for(db, platform, False)
            prof = await store.profile_row(db, pid)
            kind = prof.get("kind") or "person"
            entry = await _profile_entry(db, prof, kind=kind, **common)
            if entry:
                kinds.append({"kind": kind, "profiles": [entry]})
    else:
        for k in cat.profile_kinds():
            if k["id"] not in allowed:
                continue
            if purpose == "onboard" and platform not in k["platforms"]:
                continue
            entries = []
            for prof in profiles:
                if (prof.get("kind") or "person") != k["id"]:
                    continue
                entry = await _profile_entry(db, prof, kind=k["id"], **common)
                if entry:
                    entries.append(entry)
            if entries:
                kinds.append({"kind": k["id"], "profiles": entries[:50]})
    ga = cat.guest_access()
    grant: dict[str, Any] = {
        "tags": scope.as_tags(),
        "all_areas": scope.all,
        "areas": [
            {"id": a["id"], "name": a["name"], "icon": a.get("icon")}
            for a in visible[:MAX_AREA_CHOICE]
        ],
        "kinds": kinds,
        "managers_choice": mine["family_manager"]["value"] is True,
        "managers": await managers_for(db, confirming_client_id),
        "guest_access": {
            "default_hours": await store.guest_default_hours(db),
            "max_hours": ga["max_hours"],
            "presets_hours": ga["presets_hours"],
        },
        "own_watch": own_watch,
    }
    if child_phone is not None:
        grant["managers_choice"] = False
        grant["managers"] = []
        grant["child_watch"] = {
            "role": "adult",
            "phone_name": child_phone.get("name") or "-",
        }
    return grant


async def _child_watch_kinds(
    db: Database,
    confirming_client_id: str,
    child_phone: dict,
    allowed: set[str],
    common: dict,
) -> list[dict]:
    """Kinds an adult may give a child's watch: exactly kind child with the
    phone's profile, when the adult may set up children (``setup_devices``
    kind child), is not the phone itself, the profile passes "No escalation"
    on the watch and the phone still may add a watch. Nothing to choose: the
    watch follows the phone's areas, adjustments and kids state."""
    phone_id = child_phone["client_id"]
    if confirming_client_id == phone_id or "child" not in allowed:
        return []
    found = await child_watch_phone(db, phone_id)
    if found is None:
        raise SetupError(NOT_PERMITTED)
    _row, phone_prof, _rules = found
    entry = await _profile_entry(
        db, phone_prof, kind="child", fixed_areas=True, **common
    )
    if not entry:
        return []
    entry.update(
        remote_choice=False,
        start_area_choice=False,
        access_hours_choice=False,
    )
    return [{"kind": "child", "profiles": [entry]}]


def offered(grant: dict, kind: Any, profile_id: Any) -> dict | None:
    """The grant entry of ``profile_id`` under ``kind`` (None = not offered)."""
    for k in grant["kinds"]:
        if k["kind"] == kind:
            for p in k["profiles"]:
                if p["id"] == profile_id:
                    return p
    return None


# ── Confirm ─────────────────────────────────────────────────────────────────


@dataclass
class AreaChoice:
    """Resolved area scope of a new device."""

    areas: dict | None  # value of the rule areas (adjustment), None = profile
    all_areas: bool
    area_names: list[str]
    mode: str  # "inherit" | "all" | "subset" | "profile"


def resolve_area_choice(
    scope, visible: list[dict], message: dict, area_choice: bool
) -> AreaChoice | None:
    """Areas of the new device from ``onboard_confirm`` (None = not allowed).

    * no ``all_areas``/``area_ids``: the confirming device's own areas;
    * ``all_areas: true``: all, only if the confirming device sees all;
    * ``area_ids``: non-empty, unique, each an active area it sees.

    Without ``area_choice`` (the profile fixes ``areas``) the choice is
    ignored.
    """
    all_flag = message.get("all_areas")
    ids = message.get("area_ids")
    if all_flag is not None and not isinstance(all_flag, bool):
        return None
    names = [a["name"] for a in visible][:MAX_AREA_CHOICE]
    if not area_choice:
        return AreaChoice(None, False, names, "profile")
    if all_flag:
        if not scope.all:
            return None
        return AreaChoice(
            {"mode": "all", "area_ids": [], "tags": [], "own_area": False},
            True,
            names,
            "all",
        )
    own = (
        {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
        if scope.all
        else {
            "mode": "selected",
            "area_ids": sorted(scope.area_ids),
            "tags": sorted(scope.tags),
            "own_area": False,
        }
    )
    if ids is None:
        return AreaChoice(own, scope.all, names, "inherit")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > MAX_AREA_CHOICE
        or not all(isinstance(i, str) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        return None
    visible_ids = [a["id"] for a in visible]
    chosen = set(ids)
    if not chosen <= set(visible_ids):
        return None
    if chosen == set(visible_ids) and not scope.all:
        return AreaChoice(own, False, names, "inherit")
    ordered = [a for a in visible if a["id"] in chosen]
    return AreaChoice(
        {
            "mode": "selected",
            "area_ids": [a["id"] for a in ordered],
            "tags": [],
            "own_area": False,
        },
        False,
        [a["name"] for a in ordered],
        "subset",
    )


@dataclass
class Plan:
    """A checked onboard_confirm: what the hub creates."""

    kind: str
    profile: dict
    guest: bool
    expires_at: str | None
    client_name: str
    owner_label: str | None
    own_area_id: str | None
    overrides: dict
    area: AreaChoice
    managers: list[str]
    remote_allowed: bool
    access_hours: dict | None = None
    # A child's watch: the phone it belongs to ({client_id, name}; its
    # set_up_by) and the watch profile it is stored with (it resolves with
    # the phone's profile while the phone has parental controls).
    child_phone: dict | None = None
    stored_profile_id: str | None = None


def _clean_label(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise SetupError(INVALID_REQUEST)
    cleaned = " ".join(value.split())
    if any(ord(ch) < 32 for ch in cleaned):
        raise SetupError(INVALID_REQUEST)
    return cleaned or None


def parse_expiry(value: Any, *, required: bool) -> str | None:
    """Checked end of a guest access (in the future, at most max_hours)."""
    from services.profiles import catalog as cat

    if value is None:
        if required:
            raise SetupError(EXPIRY_NOT_ALLOWED)
        return None
    if not isinstance(value, str):
        raise SetupError(EXPIRY_NOT_ALLOWED)
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SetupError(EXPIRY_NOT_ALLOWED) from None
    if ts.tzinfo is None:
        raise SetupError(EXPIRY_NOT_ALLOWED)
    now = datetime.now(timezone.utc)
    limit = now + timedelta(hours=cat.guest_access()["max_hours"], minutes=10)
    if ts <= now or ts > limit:
        raise SetupError(EXPIRY_NOT_ALLOWED)
    return ts.astimezone(timezone.utc).isoformat()


async def plan_confirm(
    db: Database, client_id: str, session: Session, message: dict
) -> Plan:
    """Check an ``onboard_confirm`` against the grant (raises SetupError)."""
    from services.profiles import catalog as cat
    from services.profiles import store

    platform = session.info.platform
    grant = await grant_for(db, client_id, platform, session=session)
    kind = message.get("kind")
    if kind not in cat.PROFILE_KINDS or not any(
        k["kind"] == kind for k in grant["kinds"]
    ):
        raise SetupError(KIND_NOT_ALLOWED)
    entry = offered(grant, kind, message.get("profile_id"))
    if entry is None:
        raise SetupError(PROFILE_NOT_ALLOWED)
    prof = await store.profile_row(db, entry["id"])
    if prof is None:
        raise SetupError(PROFILE_NOT_ALLOWED)
    guest = kind in cat.GUEST_ACCESS_KINDS and not grant["own_watch"]

    name = message.get("client_name")
    if name is not None and (
        not isinstance(name, str) or not name.strip() or len(name) > 100
    ):
        raise SetupError(INVALID_REQUEST)
    client_name = (
        " ".join(name.split()) if isinstance(name, str) else session.client_name
    )
    owner_label = _clean_label(message.get("owner_label"), 50)

    found = await confirmer(db, client_id)
    if not found:
        raise SetupError(NOT_PERMITTED)
    mine = found[2]
    scope, visible, by_area = await _confirmer_scope(db, client_id)
    visible_ids = {a["id"] for a in visible}

    area = resolve_area_choice(scope, visible, message, entry["area_choice"])
    if area is None:
        raise SetupError(AREAS_NOT_ALLOWED)
    overrides: dict[str, Any] = {}
    if area.areas is not None:
        overrides["areas"] = area.areas

    own_area_id = message.get("own_area_id")
    if grant.get("child_watch") and (
        own_area_id is not None or message.get("start_area") is not None
    ):
        # A child's watch follows its phone's own area and areas.
        raise SetupError(AREAS_NOT_ALLOWED)
    if own_area_id is not None and (
        not isinstance(own_area_id, str) or own_area_id not in visible_ids
    ):
        raise SetupError(AREAS_NOT_ALLOWED)

    start_area = message.get("start_area")
    if start_area is not None:
        if not entry["start_area_choice"] or not isinstance(start_area, str):
            raise SetupError(AREAS_NOT_ALLOWED)
        if start_area not in visible_ids:
            raise SetupError(AREAS_NOT_ALLOWED)
        if area.areas is not None and area.areas["mode"] == "selected":
            chosen = set(area.areas["area_ids"])
            if area.mode == "subset" and start_area not in chosen:
                raise SetupError(AREAS_NOT_ALLOWED)
        overrides["panel_start"] = {"mode": "area", "area_id": start_area}

    expires_at = parse_expiry(message.get("expires_at"), required=guest)
    if not guest and expires_at is not None:
        raise SetupError(EXPIRY_NOT_ALLOWED)

    remote = message.get("remote")
    if remote is not None:
        if not isinstance(remote, bool) or not guest:
            raise SetupError(INVALID_REQUEST)
        if remote != entry["remote_access"]:
            if not entry["remote_choice"]:
                raise SetupError(PROFILE_NOT_ALLOWED)
            overrides["remote_access"] = remote

    hours = parse_access_windows(message.get("access_windows"), entry, mine)
    if hours is not None:
        overrides["access_hours"] = hours

    managers = message.get("managers")
    chosen_managers: list[str] = []
    if managers is not None:
        if not isinstance(managers, list) or not all(
            isinstance(m, str) for m in managers
        ):
            raise SetupError(INVALID_REQUEST)
        if managers:
            if not grant["managers_choice"] or not prof["child_protection"]:
                raise SetupError(MANAGERS_NOT_ALLOWED)
            known = {m["client_id"]: m for m in grant["managers"]}
            for m in managers:
                if m not in known:
                    raise SetupError(MANAGERS_NOT_ALLOWED)
                if not known[m]["all_children"]:
                    chosen_managers.append(m)

    # Final check with every choice applied: never more than the confirming
    # device (also its areas).
    rules = new_device_rules(
        prof, platform, guest=guest, overrides=overrides, own_area_id=own_area_id
    )
    if stronger_rules(rules, mine):
        raise SetupError(PROFILE_NOT_ALLOWED)
    from api import authz

    all_areas = await _all_active_areas(db)
    new_scope = authz.scope_from_rule(rules["areas"]["value"])
    new_ids = set(_visible_ids(new_scope, all_areas, by_area))
    if not new_ids <= visible_ids:
        raise SetupError(AREAS_NOT_ALLOWED)
    if area.mode == "profile":
        area = AreaChoice(
            None,
            new_scope.all,
            [a["name"] for a in all_areas if a["id"] in new_ids][:MAX_AREA_CHOICE],
            "profile",
        )
    child_phone = session.child_phone if grant.get("child_watch") else None
    stored_profile_id = None
    if child_phone is not None:
        try:
            stored_profile_id = await store.default_profile_for(db, platform, False)
        except Exception:  # noqa: BLE001 - no watch profile: nothing is created
            raise SetupError(PROFILE_NOT_ALLOWED) from None
    return Plan(
        kind=kind,
        profile=prof,
        guest=guest,
        expires_at=expires_at,
        client_name=client_name,
        owner_label=owner_label,
        own_area_id=own_area_id,
        overrides=overrides,
        area=area,
        managers=chosen_managers,
        remote_allowed=rules["remote_access"]["value"] is True,
        access_hours=rules["access_hours"]["value"]
        if rules["access_hours"]["source"] != "not_applicable"
        else None,
        child_phone=child_phone,
        stored_profile_id=stored_profile_id,
    )


async def add_to_managers(
    db: Database, child_id: str, manager_ids: list[str], actor: str | None
) -> None:
    """A new child joins the chosen family managers' lists."""
    from services.family import store as fstore

    for mid in manager_ids:
        current = await fstore.scope_row(db, mid)
        scope = current["scope"]
        if scope["all"] or child_id in scope["client_ids"]:
            continue
        await fstore.put_scope(
            db,
            mid,
            scope={"all": False, "client_ids": [*scope["client_ids"], child_id]},
            actor=actor,
        )
