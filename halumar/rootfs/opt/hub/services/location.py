"""Location of the devices (contracts docs/location.md).

Two ways reach the device_tracker of a device in Home Assistant:

* devices with a Home Assistant key call the integration action
  ``<ns>.report_location`` (home and away); the integration checks the rule
  itself and tells the hub only the metadata (``location-status``);
* devices on the hub path (parental controls or Hub only; never a watch)
  may hold no key and report here, at home only
  (WS ``location_report``, REST ``POST /api/client/location``): the hub checks
  the effective rule ``location`` and the rate limit and hands the fix to the
  integration (``client_location``).

The hub keeps **no position**: neither in the database nor in memory, and the
log carries only metadata. It keeps per device what the app shares
(``location_shared``) and when the last fix was accepted (``location_at``),
in memory, for the admin UI.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.services.location")

MIN_INTERVAL_SECONDS = 5.0
STALE_AFTER = timedelta(minutes=15)
FUTURE_SKEW = timedelta(minutes=5)

SHARED = ("none", "foreground", "background")
RULE_VALUES = ("off", "foreground", "background")

# Reasons ($defs/location_report_reason) with the REST status of the hub way.
INVALID = "invalid_request"
NOT_HUB = "not_hub_controlled"
STALE = "location_stale"
NOT_ALLOWED = "location_not_allowed"
BACKGROUND_NOT_ALLOWED = "location_background_not_allowed"
RATE_LIMITED = "rate_limited"
STATUS = {
    INVALID: 422,
    STALE: 422,
    NOT_HUB: 403,
    NOT_ALLOWED: 403,
    BACKGROUND_NOT_ALLOWED: 403,
    RATE_LIMITED: 429,
}

_FIELDS = {
    # name: (required, low, high, nullable)
    "latitude": (True, -90.0, 90.0, False),
    "longitude": (True, -180.0, 180.0, False),
    "accuracy": (True, 0.0, 100000.0, False),
    "altitude": (False, -1000.0, 100000.0, True),
    "vertical_accuracy": (False, 0.0, 100000.0, True),
    "speed": (False, 0.0, 1000.0, True),
    "course": (False, 0.0, 360.0, True),
}
_ALLOWED_KEYS = frozenset(_FIELDS) | {
    "battery_level",
    "background",
    "ts",
    "location_shared",
}

# client_id -> {"location_shared": str | None, "location_at": str | None}
_status: dict[str, dict[str, str | None]] = {}
# client_id -> monotonic time of the last accepted report (hub way)
_accepted_at: dict[str, float] = {}
# client_id -> time of the last accepted fix (hub way, ordering)
_last_fix: dict[str, datetime] = {}


class LocationError(Exception):
    """A refused report; ``reason`` from $defs/location_report_reason."""

    def __init__(self, reason: str, retry_after: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after

    def answer(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "location_report_error", "reason": self.reason}
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


def reset() -> None:
    """Forget everything (tests)."""
    _status.clear()
    _accepted_at.clear()
    _last_fix.clear()


def forget(client_id: str) -> None:
    """A device was removed."""
    _status.pop(client_id, None)
    _accepted_at.pop(client_id, None)
    _last_fix.pop(client_id, None)


# ── rule ──────────────────────────────────────────────────────────────────


def rule_value(effective: dict[str, dict]) -> str:
    """Effective rule location; ``off`` when it does not apply."""
    value = (effective.get("location") or {}).get("value")
    return value if value in RULE_VALUES else "off"


def snapshot_value(effective: dict[str, dict]) -> str:
    """Field ``location`` of the rule snapshot (enforcement-snapshot.json)."""
    return rule_value(effective)


def required(effective: dict[str, dict]) -> bool:
    """Effective rule location_required (false when it does not apply)."""
    return (effective.get("location_required") or {}).get("value") is True


# ── status (admin UI) ──────────────────────────────────────────────────────


def status_of(client_id: str) -> dict[str, str | None]:
    entry = _status.get(client_id) or {}
    return {
        "location_shared": entry.get("location_shared"),
        "location_at": entry.get("location_at"),
    }


def _shared_value(value: Any) -> str | None:
    if value is None:
        return None
    if value not in SHARED:
        raise ValueError("invalid location_shared")
    return value


def set_shared(client_id: str, value: Any) -> bool:
    """``client_status.location_shared``; True when it changed. Raises ValueError."""
    shared = _shared_value(value)
    entry = _status.setdefault(
        client_id, {"location_shared": None, "location_at": None}
    )
    if entry["location_shared"] == shared:
        return False
    entry["location_shared"] = shared
    return True


def note_fix(
    client_id: str, ts: datetime, background: bool, shared: str | None = None
) -> bool:
    """An accepted fix (either way); True when the shown status changed.

    A reported ``location_shared`` wins; without one a background fix proves
    at least background, a foreground fix at least foreground.
    """
    entry = _status.setdefault(
        client_id, {"location_shared": None, "location_at": None}
    )
    before = dict(entry)
    if shared is None:
        current = entry["location_shared"]
        if background:
            shared = "background"
        elif current in (None, "none"):
            shared = "foreground"
        else:
            shared = current
    entry["location_shared"] = shared
    at = _iso(ts)
    if entry["location_at"] is None or entry["location_at"] < at:
        entry["location_at"] = at
    return entry != before


# ── parsing ────────────────────────────────────────────────────────────────


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise LocationError(INVALID)
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise LocationError(INVALID) from err
    if ts.tzinfo is None:
        raise LocationError(INVALID)
    return ts


def _number(value: Any, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocationError(INVALID)
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise LocationError(INVALID)
    return number


def parse_report(body: Any) -> dict[str, Any]:
    """Validated report (fields of location_report without ``type``)."""
    if not isinstance(body, dict) or set(body) - _ALLOWED_KEYS:
        raise LocationError(INVALID)
    fix: dict[str, Any] = {}
    for name, (needed, low, high, nullable) in _FIELDS.items():
        value = body.get(name)
        if value is None:
            if needed or (name in body and not nullable):
                raise LocationError(INVALID)
            fix[name] = None
            continue
        fix[name] = _number(value, low, high)
    background = body.get("background")
    if not isinstance(background, bool):
        raise LocationError(INVALID)
    battery = body.get("battery_level")
    if battery is not None:
        if isinstance(battery, bool) or not isinstance(battery, int):
            raise LocationError(INVALID)
        if not 0 <= battery <= 100:
            raise LocationError(INVALID)
    try:
        shared = _shared_value(body.get("location_shared"))
    except ValueError as err:
        raise LocationError(INVALID) from err
    ts = parse_ts(body.get("ts"))
    return {
        "fix": fix,
        "ts": ts,
        "background": background,
        "battery_level": battery,
        "location_shared": shared,
    }


def check_age(ts: datetime, now: datetime) -> None:
    if ts < now - STALE_AFTER or ts > now + FUTURE_SKEW:
        raise LocationError(STALE)


def check_rule(rule: str, background: bool) -> None:
    if rule == "off":
        raise LocationError(NOT_ALLOWED)
    if background and rule != "background":
        raise LocationError(BACKGROUND_NOT_ALLOWED)


# ── hub way (devices on the hub path) ─────────────────────────────


async def report(
    db: Database, client_id: str, body: Any, *, via: str
) -> dict[str, Any]:
    """Check and forward one report; returns location_report_ok, raises LocationError."""
    from services.profiles import catalog as cat
    from services.profiles import resolver as res
    from services.profiles import store as profiles

    try:
        report_ = parse_report(body)
        resolved = await profiles.resolve_client(db, client_id)
        if resolved is None:
            raise LocationError(NOT_ALLOWED)
        client, prof, effective = resolved
        if not res.uses_hub_path(
            effective,
            child=bool(prof.get("child_protection")),
            platform=cat.device_platform(client.get("platform")),
        ):
            raise LocationError(NOT_HUB)
        now = datetime.now(timezone.utc)
        check_age(report_["ts"], now)
        rule = rule_value(effective)
        check_rule(rule, report_["background"])
        last = _accepted_at.get(client_id)
        mono = time.monotonic()
        if last is not None and mono - last < MIN_INTERVAL_SECONDS:
            raise LocationError(
                RATE_LIMITED, max(1, math.ceil(MIN_INTERVAL_SECONDS - (mono - last)))
            )
    except LocationError as err:
        # Metadata only, never coordinates.
        logger.info(
            "location report refused: client=%s via=%s reason=%s",
            client_id,
            via,
            err.reason,
        )
        raise
    _accepted_at[client_id] = mono
    newest = _last_fix.get(client_id)
    if newest is not None and report_["ts"] <= newest:
        logger.debug("location report older than the last one: client=%s", client_id)
        return {"type": "location_report_ok", "applied": False}
    from api.ws.broadcast import send_to_integration

    fix = {**report_["fix"], "ts": _iso(report_["ts"])}
    sent = await send_to_integration(
        {"type": "client_location", "data": {"client_id": client_id, "location": fix}}
    )
    if not sent:
        # Nobody took the fix (integration not connected): not applied, and
        # the same fix is still new when the app sends it again.
        logger.info(
            "location report not delivered (no integration): client=%s via=%s",
            client_id,
            via,
        )
        return {"type": "location_report_ok", "applied": False}
    _last_fix[client_id] = report_["ts"]
    logger.info(
        "location report accepted: client=%s via=%s background=%s rule=%s to_integration=%s",
        client_id,
        via,
        report_["background"],
        rule,
        sent,
    )
    await _record(
        db,
        client_id,
        report_["ts"],
        report_["background"],
        report_["location_shared"],
        report_["battery_level"],
    )
    return {"type": "location_report_ok", "applied": True}


# ── action way (metadata from the integration) ───────────────────────────


async def integration_status(
    db: Database,
    client_id: str,
    *,
    background: bool,
    ts: datetime,
    shared: str | None,
    battery_level: int | None,
) -> dict[str, Any]:
    """POST /api/integration/location-status: an accepted <ns>.report_location."""
    logger.info(
        "location reported through Home Assistant: client=%s background=%s",
        client_id,
        background,
    )
    await _record(db, client_id, ts, background, shared, battery_level)
    return {"client_id": client_id, **status_of(client_id)}


async def _record(
    db: Database,
    client_id: str,
    ts: datetime,
    background: bool,
    shared: str | None,
    battery_level: int | None,
) -> None:
    changed = note_fix(client_id, ts, background, shared)
    if battery_level is not None:
        row = await db.fetchone(
            "SELECT battery_level FROM paired_clients WHERE id = ?", (client_id,)
        )
        if row is not None and row.get("battery_level") != battery_level:
            async with db.transaction():
                await db.execute(
                    "UPDATE paired_clients SET battery_level = ? WHERE id = ?",
                    (battery_level, client_id),
                )
            changed = True
    if changed:
        from api.ws.client import broadcast_client_status

        await broadcast_client_status(db, client_id)
