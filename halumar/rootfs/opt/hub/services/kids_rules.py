"""Rule engine of the kids mode (contract docs/profiles.md).

Pure: no database, no I/O, no clock of its own. The caller passes the rule
layers, the entity state, the device's recent actions and ``now``; the
engine answers with a :class:`Decision`. The same rules go to the apps in
``client_config.profile.rules`` (display only); this module is the
authority.

Rule object (``kids_rules``, contract ``common/kids-rules.json``), on three
levels profile -> area -> entity. A missing field is inherited from the
more general level; ``null`` for ``rate_limit``/``volume_max`` switches the
rule off on that level.

* ``blocked`` (bool): everything in this scope blocked / explicitly allowed.
* ``blocked_types`` (profile, area): HA domains or ``domain:device_class``.
* ``quiet_hours``: windows ``{days, start, end}`` in the hub time zone;
  ``end <= start`` crosses midnight and belongs to the start day,
  ``start == end`` is the whole day, ``[]`` = none.
* ``rate_limit``: ``{max_actions, window_seconds, cooldown_seconds}`` per
  entity and device.
* ``color_lock`` (bool): no colors/effects on lights.
* ``volume_max`` (0-100): volume cap for media players.

Pause everything (profile rule ``kids_rate_limit_all``, profile level
only): more than ``max_entities`` different entities or more than
``max_actions`` actions of the device within ``window_seconds`` pause every
entity for ``cooldown_seconds`` (:func:`evaluate_all`).
"""

from __future__ import annotations

import math
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LEVEL_PROFILE = "profile"
LEVEL_AREA = "area"
LEVEL_ENTITY = "entity"

REASON_BLOCKED = "blocked"
REASON_QUIET_HOURS = "quiet_hours"
REASON_RATE_LIMITED = "rate_limited"
REASON_COOLDOWN = "cooldown"
REASON_COLOR_LOCKED = "color_locked"
REASON_VOLUME_LIMIT = "volume_limit"
REASON_PAUSED_ALL = "paused_all"

REASONS = (
    REASON_BLOCKED,
    REASON_QUIET_HOURS,
    REASON_RATE_LIMITED,
    REASON_COOLDOWN,
    REASON_COLOR_LOCKED,
    REASON_VOLUME_LIMIT,
    REASON_PAUSED_ALL,
)

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

RULE_KEYS = (
    "blocked",
    "blocked_types",
    "quiet_hours",
    "rate_limit",
    "color_lock",
    "volume_max",
)
# blocked_types only makes sense for a group of entities.
LEVEL_KEYS = {
    LEVEL_PROFILE: RULE_KEYS,
    LEVEL_AREA: RULE_KEYS,
    LEVEL_ENTITY: tuple(k for k in RULE_KEYS if k != "blocked_types"),
}

MAX_WINDOWS = 14
MAX_TYPES = 30
RATE_MAX_ACTIONS = (1, 100)
RATE_WINDOW_SECONDS = (5, 3600)
RATE_COOLDOWN_SECONDS = (0, 86400)

TYPE_RE = re.compile(r"^[a-z0-9_]{1,64}(:[a-z0-9_]{1,64})?$")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Light attributes that change color or effect (color_lock). Brightness and
# the white tone (color temperature) stay allowed.
COLOR_KEYS = frozenset(
    {
        "rgb_color",
        "rgbw_color",
        "rgbww_color",
        "hs_color",
        "xy_color",
        "color_name",
        "effect",
        "flash",
    }
)

# Actions that make things quieter/darker: allowed during quiet hours.
REDUCING_SERVICES = frozenset(
    {
        "turn_off",
        "media_pause",
        "media_stop",
        "volume_down",
        "close_cover",
        "stop_cover",
    }
)
ON_STATES = frozenset(
    {"on", "playing", "open", "opening", "paused", "idle", "buffering"}
)

VOLUME_STEP = 0.1  # HA's default media_player volume step

DEFAULT_CHILD_RULES: dict[str, Any] = {
    "blocked_types": [
        "lock",
        "alarm_control_panel",
        "cover:garage",
        "cover:gate",
        "camera",
    ],
    "rate_limit": {"max_actions": 6, "window_seconds": 60, "cooldown_seconds": 300},
    "quiet_hours": [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "20:00", "end": "06:30"},
        {"days": ["sat", "sun"], "start": "21:00", "end": "07:00"},
    ],
    "color_lock": False,
    "volume_max": 40,
}


class RuleError(ValueError):
    """Invalid rule object; ``reason`` is ``rules.<field>`` (422 detail)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── validation ──────────────────────────────────────────────────────────────


def _int_in(value: Any, bounds: tuple[int, int], reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuleError(reason)
    if not bounds[0] <= value <= bounds[1]:
        raise RuleError(reason)
    return value


def validate_rules(rules: Any, level: str) -> dict[str, Any]:
    """Canonical copy of a rule object for ``level``; raises :class:`RuleError`."""
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        raise RuleError("rules")
    allowed = LEVEL_KEYS[level]
    unknown = set(rules) - set(allowed)
    if unknown:
        raise RuleError(f"rules.{sorted(unknown)[0]}")
    out: dict[str, Any] = {}
    for key in allowed:
        if key not in rules:
            continue
        value = rules[key]
        if key in ("blocked", "color_lock"):
            if not isinstance(value, bool):
                raise RuleError(f"rules.{key}")
            out[key] = value
        elif key == "blocked_types":
            if not isinstance(value, list) or len(value) > MAX_TYPES:
                raise RuleError("rules.blocked_types")
            seen: list[str] = []
            for item in value:
                if not isinstance(item, str) or not TYPE_RE.match(item):
                    raise RuleError("rules.blocked_types")
                if item not in seen:
                    seen.append(item)
            out[key] = seen
        elif key == "quiet_hours":
            out[key] = _validate_windows(value)
        elif key == "rate_limit":
            if value is None:
                out[key] = None
                continue
            if not isinstance(value, dict) or set(value) - {
                "max_actions",
                "window_seconds",
                "cooldown_seconds",
            }:
                raise RuleError("rules.rate_limit")
            out[key] = {
                "max_actions": _int_in(
                    value.get("max_actions"), RATE_MAX_ACTIONS, "rules.rate_limit"
                ),
                "window_seconds": _int_in(
                    value.get("window_seconds"), RATE_WINDOW_SECONDS, "rules.rate_limit"
                ),
                "cooldown_seconds": _int_in(
                    value.get("cooldown_seconds"),
                    RATE_COOLDOWN_SECONDS,
                    "rules.rate_limit",
                ),
            }
        elif key == "volume_max":
            out[key] = (
                None if value is None else _int_in(value, (0, 100), "rules.volume_max")
            )
    return out


def _validate_windows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_WINDOWS:
        raise RuleError("rules.quiet_hours")
    windows = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"days", "start", "end"}:
            raise RuleError("rules.quiet_hours")
        days = item["days"]
        if (
            not isinstance(days, list)
            or not days
            or any(d not in DAYS for d in days)
            or len(set(days)) != len(days)
        ):
            raise RuleError("rules.quiet_hours")
        for k in ("start", "end"):
            if not isinstance(item[k], str) or not HHMM_RE.match(item[k]):
                raise RuleError("rules.quiet_hours")
        windows.append(
            {
                "days": [d for d in DAYS if d in days],
                "start": item["start"],
                "end": item["end"],
            }
        )
    return windows


# ── time zone and quiet hours ───────────────────────────────────────────────


def zone(name: str | None) -> ZoneInfo | timezone:
    """``ZoneInfo(name)``; UTC for an unknown or empty name."""
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _window_spans(
    windows: Iterable[dict[str, Any]], day: datetime, tz
) -> list[tuple[datetime, datetime]]:
    """UTC spans of the windows starting on local calendar ``day``."""
    weekday = DAYS[day.weekday()]
    spans = []
    for w in windows:
        if weekday not in w["days"]:
            continue
        start_t, end_t = _hhmm(w["start"]), _hhmm(w["end"])
        start_local = datetime.combine(day.date(), start_t, tzinfo=tz)
        end_day = day.date() if end_t > start_t else day.date() + timedelta(days=1)
        end_local = datetime.combine(end_day, end_t, tzinfo=tz)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        if end_utc > start_utc:
            spans.append((start_utc, end_utc))
    return spans


def quiet_until(
    windows: list[dict[str, Any]] | None, now: datetime, tz_name: str | None
) -> datetime | None:
    """End (UTC) of the quiet time ``now`` is in, ``None`` outside.

    Chained or overlapping windows are followed to their common end.
    """
    if not windows:
        return None
    tz = zone(tz_name)
    now_utc = now.astimezone(timezone.utc)
    local = now_utc.astimezone(tz)
    spans: list[tuple[datetime, datetime]] = []
    # A window can start up to one day before (crossing midnight); look a
    # bit further for chains (two weeks at most).
    for offset in range(-1, 15):
        spans.extend(_window_spans(windows, local + timedelta(days=offset), tz))
    spans.sort()
    until = None
    point = now_utc
    for _ in range(len(spans) + 1):
        covering = [end for start, end in spans if start <= point < end]
        if not covering:
            break
        point = max(covering)
        until = point
    return until


def bedtime_end(
    windows: list[dict[str, Any]] | None, now: datetime, tz_name: str | None
) -> datetime | None:
    """End (UTC) of the quiet time ``now`` is in, else of the next one.

    "Until tomorrow morning" of a parent release: the end of the bedtime.
    ``None`` without quiet hours (or none in the coming week).
    """
    if not windows:
        return None
    until = quiet_until(windows, now, tz_name)
    if until is not None:
        return until
    start = next_quiet_start(windows, now, tz_name)
    return quiet_until(windows, start, tz_name) if start else None


def next_quiet_start(
    windows: list[dict[str, Any]] | None, now: datetime, tz_name: str | None
) -> datetime | None:
    """Start (UTC) of the next quiet time; ``None`` inside one, without quiet
    hours or without one in the coming week.

    Exact to the minute of the window (weekdays, windows across midnight, the
    zone's daylight saving time), not a sampled point.
    """
    if not windows or quiet_until(windows, now, tz_name) is not None:
        return None
    tz = zone(tz_name)
    now_utc = now.astimezone(timezone.utc)
    local = now_utc.astimezone(tz)
    starts = [
        start
        for offset in range(0, 8)
        for start, _end in _window_spans(windows, local + timedelta(days=offset), tz)
        if start > now_utc
    ]
    return min(starts) if starts else None


# ── layers ──────────────────────────────────────────────────────────────────


@dataclass
class Layers:
    """Rule objects of one profile for one entity (in one area)."""

    profile: dict[str, Any] = field(default_factory=dict)
    area: dict[str, Any] | None = None
    entity: dict[str, Any] | None = None

    def pick(self, key: str) -> tuple[Any, str | None]:
        """``(value, level)`` of the most specific level that sets ``key``."""
        for level, rules in (
            (LEVEL_ENTITY, self.entity),
            (LEVEL_AREA, self.area),
            (LEVEL_PROFILE, self.profile),
        ):
            if rules and key in rules:
                return rules[key], level
        return None, None


def entity_types(entity_id: str, state: dict | None) -> set[str]:
    """Type keys of an entity: its domain and ``domain:device_class``."""
    domain = entity_id.split(".", 1)[0]
    types = {domain}
    attrs = (state or {}).get("attributes") or {}
    device_class = attrs.get("device_class")
    if isinstance(device_class, str) and device_class:
        types.add(f"{domain}:{device_class}")
    return types


@dataclass(frozen=True)
class Releases:
    """Running releases of a child device for one action.

    ``quiet``: bedtime does not apply (bedtime release, or an entity release
    of this entity); ``rate``: no pause and no rate limit (pause release);
    ``types``: the blocked device types do not block this entity (entity
    release). Explicit blocks of the admin (exceptions) always apply.
    """

    quiet: bool = False
    rate: bool = False
    types: bool = False


NO_RELEASES = Releases()


def access_blocked(
    layers: Layers, entity_id: str, state: dict | None, *, skip_types: bool = False
) -> str | None:
    """Level that blocks the entity, ``None`` when it is allowed."""
    if layers.entity and "blocked" in layers.entity:
        return LEVEL_ENTITY if layers.entity["blocked"] else None
    if layers.area and "blocked" in layers.area:
        return LEVEL_AREA if layers.area["blocked"] else None
    types, level = layers.pick("blocked_types")
    if not skip_types and types:
        if entity_types(entity_id, state) & set(types):
            return level
        # State unknown: the device class may be a blocked one.
        prefix = entity_id.split(".", 1)[0] + ":"
        if state is None and any(
            isinstance(t, str) and t.startswith(prefix) for t in types
        ):
            return level
    if layers.profile.get("blocked"):
        return LEVEL_PROFILE
    return None


# ── decision ────────────────────────────────────────────────────────────────


@dataclass
class Decision:
    allowed: bool
    reason: str | None = None
    level: str | None = None
    until: datetime | None = None
    # Rewritten call (volume cap): service and data to send instead.
    service: str | None = None
    data: dict[str, Any] | None = None
    adjusted: dict[str, Any] | None = None
    limit: dict[str, Any] | None = None
    # A cooldown that this decision starts (rate limit just exceeded).
    start_cooldown_until: datetime | None = None
    # A pause of everything that this decision starts and why:
    # {"trigger": "actions"|"entities", "actions": n, "entities": [...]}.
    start_pause_all_until: datetime | None = None
    pause_all: dict[str, Any] | None = None

    def retry_after(self, now: datetime) -> int | None:
        if self.until is None:
            return None
        return max(0, math.ceil((self.until - now).total_seconds()))


def _is_reducing(service: str, data: dict, state: dict | None) -> bool:
    if service in REDUCING_SERVICES:
        return True
    if service == "volume_mute":
        return data.get("is_volume_muted") is True
    if service == "toggle":
        return (state or {}).get("state") in ON_STATES
    return False


def _color_change(entity_id: str, service: str, data: dict) -> bool:
    if not entity_id.startswith("light."):
        return False
    if service not in ("turn_on", "toggle"):
        return False
    return bool(COLOR_KEYS & set(data))


def evaluate_one(
    layers: Layers,
    *,
    entity_id: str,
    service: str,
    data: dict[str, Any] | None,
    state: dict | None,
    now: datetime,
    tz_name: str | None,
    recent: Iterable[datetime] = (),
    cooldown_until: datetime | None = None,
    override_until: datetime | None = None,
    releases: Releases = NO_RELEASES,
) -> Decision:
    """Decide one action against one set of layers (one area)."""
    data = dict(data or {})
    level = access_blocked(layers, entity_id, state, skip_types=releases.types)
    if level:
        return Decision(False, REASON_BLOCKED, level)

    overridden = override_until is not None and override_until > now

    if not overridden:
        windows, wlevel = layers.pick("quiet_hours")
        if windows and not releases.quiet and not _is_reducing(service, data, state):
            until = quiet_until(windows, now, tz_name)
            if until:
                return Decision(False, REASON_QUIET_HOURS, wlevel, until=until)

        if not releases.rate and cooldown_until is not None and cooldown_until > now:
            _, rlevel = layers.pick("rate_limit")
            return Decision(False, REASON_COOLDOWN, rlevel, until=cooldown_until)

    color_lock, clevel = layers.pick("color_lock")
    if color_lock and _color_change(entity_id, service, data):
        return Decision(False, REASON_COLOR_LOCKED, clevel)

    decision = Decision(True, service=service, data=data)
    volume_max, vlevel = layers.pick("volume_max")
    if volume_max is not None and entity_id.startswith("media_player."):
        cap = volume_max / 100
        limit = {"volume_max": volume_max}
        if service == "volume_set":
            level_value = data.get("volume_level")
            if isinstance(level_value, (int, float)) and not isinstance(
                level_value, bool
            ):
                if level_value > cap:
                    data["volume_level"] = cap
                    decision = Decision(
                        True,
                        service=service,
                        data=data,
                        adjusted={"volume_level": cap},
                        level=vlevel,
                        limit=limit,
                    )
        elif service == "volume_up":
            current = ((state or {}).get("attributes") or {}).get("volume_level")
            if not isinstance(current, (int, float)) or current >= cap - 1e-6:
                return Decision(False, REASON_VOLUME_LIMIT, vlevel, limit=limit)
            if current + VOLUME_STEP > cap + 1e-6:
                decision = Decision(
                    True,
                    service="volume_set",
                    data={"volume_level": cap},
                    adjusted={"volume_level": cap},
                    level=vlevel,
                    limit=limit,
                )

    if not overridden and not releases.rate:
        rate, rlevel = layers.pick("rate_limit")
        if rate:
            window_start = now - timedelta(seconds=rate["window_seconds"])
            in_window = sorted(t for t in recent if t > window_start)
            if len(in_window) >= rate["max_actions"]:
                if rate["cooldown_seconds"]:
                    until = now + timedelta(seconds=rate["cooldown_seconds"])
                    cooldown = until
                else:
                    # No pause: free again when the oldest action leaves the window.
                    oldest = in_window[len(in_window) - rate["max_actions"]]
                    until = oldest + timedelta(seconds=rate["window_seconds"])
                    cooldown = None
                return Decision(
                    False,
                    REASON_RATE_LIMITED,
                    rlevel,
                    until=until,
                    start_cooldown_until=cooldown,
                    limit={
                        "max_actions": rate["max_actions"],
                        "window_seconds": rate["window_seconds"],
                    },
                )
    return decision


def evaluate(
    profile_rules: dict[str, Any],
    area_rules: list[dict[str, Any] | None],
    entity_rules: dict[str, Any] | None,
    **kwargs: Any,
) -> Decision:
    """Decide an action for an entity placed in ``area_rules`` areas.

    ``area_rules`` holds one entry per area the entity is visible in (``None``
    = the area has no rule object); an empty list means "no area" (only
    profile and entity apply). Any blocking area blocks: the decision with
    the latest ``until`` (``None`` = no time = strongest) wins.
    """
    areas = area_rules or [None]
    allowed: Decision | None = None
    blocked: list[Decision] = []
    for area in areas:
        d = evaluate_one(
            Layers(profile=profile_rules or {}, area=area, entity=entity_rules),
            **kwargs,
        )
        if d.allowed:
            # Several areas: keep the strictest rewrite (lowest volume).
            if allowed is None or (d.adjusted and not allowed.adjusted):
                allowed = d
            elif d.adjusted and allowed.adjusted:
                if d.adjusted.get("volume_level", 1) < allowed.adjusted.get(
                    "volume_level", 1
                ):
                    allowed = d
        else:
            blocked.append(d)
    if blocked:

        def strength(d: Decision) -> tuple[int, float]:
            if d.until is None:
                return (1, 0.0)
            return (0, d.until.timestamp())

        return max(blocked, key=strength)
    assert allowed is not None
    return allowed


# ── pause everything ─────────────────────────────────────────────────


def rate_limit_all(value: Any) -> dict[str, int] | None:
    """The switched-on value of ``kids_rate_limit_all``, else None."""
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    try:
        return {
            k: int(value[k])
            for k in (
                "max_actions",
                "max_entities",
                "window_seconds",
                "cooldown_seconds",
            )
        }
    except (KeyError, TypeError, ValueError):
        return None


def evaluate_all(
    limit: dict[str, int] | None,
    *,
    entity_id: str,
    now: datetime,
    recent: Iterable[tuple[datetime, str]] = (),
    paused_until: datetime | None = None,
) -> Decision | None:
    """Pause everything of a device; None = no objection.

    ``recent`` = the device's successful actions ``(time, entity_id)``. A
    running pause rejects every action; otherwise the action that would make
    it more than ``max_actions`` actions or more than ``max_entities``
    different entities within the window is rejected and starts the pause.
    The caller skips this during a parent release or a pause release.
    """
    if limit is None:
        return None
    info = {
        "max_actions": limit["max_actions"],
        "max_entities": limit["max_entities"],
        "window_seconds": limit["window_seconds"],
    }
    if paused_until is not None and paused_until > now:
        return Decision(
            False, REASON_PAUSED_ALL, LEVEL_PROFILE, until=paused_until, limit=info
        )
    window_start = now - timedelta(seconds=limit["window_seconds"])
    in_window = [(t, e) for t, e in recent if t > window_start]
    entities: list[str] = []
    for _t, e in in_window:
        if e not in entities:
            entities.append(e)
    trigger = None
    if len(in_window) >= limit["max_actions"]:
        trigger = "actions"
    elif entity_id not in entities and len(entities) >= limit["max_entities"]:
        trigger = "entities"
    if trigger is None:
        return None
    until = now + timedelta(seconds=limit["cooldown_seconds"])
    return Decision(
        False,
        REASON_PAUSED_ALL,
        LEVEL_PROFILE,
        until=until,
        limit=info,
        start_pause_all_until=until,
        pause_all={
            "trigger": trigger,
            "actions": len(in_window),
            "entities": entities + [entity_id] if trigger == "entities" else entities,
        },
    )


# ── in-memory counters ──────────────────────────────────────────────────────


class ActionCounters:
    """Recent successful actions and cooldowns per (device, entity), and
    per device for the pause of everything.

    In memory on purpose: a hub restart resets them.
    """

    MAX_HISTORY = 128
    MAX_DEVICE_HISTORY = 512

    def __init__(self) -> None:
        self._recent: dict[tuple[str, str], deque[datetime]] = {}
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._device: dict[str, deque[tuple[datetime, str]]] = {}
        self._paused_all: dict[str, datetime] = {}

    def recent(self, client_id: str, entity_id: str) -> list[datetime]:
        return list(self._recent.get((client_id, entity_id), ()))

    def cooldown(
        self, client_id: str, entity_id: str, now: datetime
    ) -> datetime | None:
        until = self._cooldowns.get((client_id, entity_id))
        if until is not None and until <= now:
            self._cooldowns.pop((client_id, entity_id), None)
            return None
        return until

    def record(self, client_id: str, entity_id: str, at: datetime) -> None:
        q = self._recent.setdefault(
            (client_id, entity_id), deque(maxlen=self.MAX_HISTORY)
        )
        q.append(at)
        d = self._device.setdefault(client_id, deque(maxlen=self.MAX_DEVICE_HISTORY))
        d.append((at, entity_id))

    def forget(self, client_id: str, entity_id: str, at: datetime) -> None:
        """Take back an action recorded before its call failed."""
        q = self._recent.get((client_id, entity_id))
        if q is not None and at in q:
            q.remove(at)
        d = self._device.get(client_id)
        if d is not None and (at, entity_id) in d:
            d.remove((at, entity_id))

    def device_recent(self, client_id: str) -> list[tuple[datetime, str]]:
        return list(self._device.get(client_id, ()))

    def paused_all(self, client_id: str, now: datetime) -> datetime | None:
        until = self._paused_all.get(client_id)
        if until is not None and until <= now:
            self._paused_all.pop(client_id, None)
            return None
        return until

    def start_pause_all(self, client_id: str, until: datetime) -> None:
        self._paused_all[client_id] = until
        self._device.pop(client_id, None)

    def start_cooldown(self, client_id: str, entity_id: str, until: datetime) -> None:
        self._cooldowns[(client_id, entity_id)] = until
        self._recent.pop((client_id, entity_id), None)

    def cooldowns_of(self, client_id: str, now: datetime) -> dict[str, datetime]:
        out = {}
        for (cid, eid), until in list(self._cooldowns.items()):
            if cid != client_id:
                continue
            if until <= now:
                self._cooldowns.pop((cid, eid), None)
            else:
                out[eid] = until
        return out

    def clear_client(self, client_id: str) -> int:
        """End every pause of the device (per entity and of everything)."""
        keys = [k for k in self._cooldowns if k[0] == client_id]
        for k in keys:
            self._cooldowns.pop(k, None)
        for k in [k for k in self._recent if k[0] == client_id]:
            self._recent.pop(k, None)
        self._device.pop(client_id, None)
        paused = self._paused_all.pop(client_id, None)
        return len(keys) + (1 if paused is not None else 0)

    def reset(self) -> None:
        self._recent.clear()
        self._cooldowns.clear()
        self._device.clear()
        self._paused_all.clear()


counters = ActionCounters()
