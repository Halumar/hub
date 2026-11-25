"""Rule catalog and suggestions of the profile model.

``data/profile_rules.json`` and ``data/profile_suggestions.json`` are byte
copies of the contract's ``data/profile-rules.json`` and
``data/profile-suggestions.json`` (checked by ``scripts/ci/contracts-check.sh``).
"""

from __future__ import annotations

import copy
import json
import math
from functools import cache
from pathlib import Path
from typing import Any

from services.profiles import resolver
from services.profiles.schema import rule_value_errors

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# Version of the stored profile body (profiles.body_json.schema_version).
BODY_SCHEMA_VERSION = 1

# Device types with a default profile (house settings) and the guest slot.
# Every Android device class has its own slot, preset like its Apple
# counterpart (ANDROID_COUNTERPART).
PROFILE_KINDS = (
    "guest",
    "child",
    "panel",
    "tv",
    "house_sitter",
    "babysitter",
    "person",
)
# Kinds whose devices are set up as a guest access with an expiry.
GUEST_ACCESS_KINDS = ("guest", "house_sitter", "babysitter")

DEVICE_PLATFORMS = (
    "ios",
    "ipados",
    "tvos",
    "watchos",
    "android",
    "android_tablet",
    "android_tv",
    "wearos",
)
DEFAULT_SLOTS = (*DEVICE_PLATFORMS, "guest")
ANDROID_COUNTERPART = {
    "android": "ios",
    "android_tablet": "ipados",
    "android_tv": "tvos",
    "wearos": "watchos",
}
# Platform of a device that never reported one: treated like an iPhone.
FALLBACK_PLATFORM = "ios"


class ProfileError(Exception):
    """Admin API error: HTTP ``status`` and contract ``code`` (``detail.code``)."""

    def __init__(self, status: int, code: str, message: str | None = None, field=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or code
        self.field = field

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            out["field"] = self.field
        return out


@cache
def catalog() -> dict:
    return json.loads((DATA_DIR / "profile_rules.json").read_text("utf-8"))


@cache
def suggestions() -> dict:
    return json.loads((DATA_DIR / "profile_suggestions.json").read_text("utf-8"))


@cache
def rules_by_key() -> dict[str, dict]:
    return {r["key"]: r for r in catalog()["rules"]}


def rule(key: str) -> dict | None:
    return rules_by_key().get(key)


def suggestion(suggestion_id: str) -> dict | None:
    for s in suggestions()["suggestions"]:
        if s["id"] == suggestion_id:
            return s
    return None


def max_profiles() -> int:
    return int(catalog()["limits"]["max_profiles"])


def name_max_length() -> int:
    return int(catalog()["limits"]["profile_name_max_length"])


def note_max_length() -> int:
    return int(catalog()["limits"]["note_max_length"])


def profile_kinds() -> list[dict]:
    """Profile kinds with labels and platforms (catalog ``profile_kinds``)."""
    return catalog()["profile_kinds"]


def kind_platforms(kind: str) -> set[str]:
    for k in profile_kinds():
        if k["id"] == kind:
            return set(k["platforms"])
    return set()


def guest_access() -> dict:
    return catalog()["guest_access"]


def expiry_limits() -> dict:
    return catalog()["expiry_limits"]


def device_platform(platform: str | None) -> str:
    """Catalog platform of a device (unknown → iPhone, the strictest app set)."""
    return platform if platform in DEVICE_PLATFORMS else FALLBACK_PLATFORM


def materialize(suggestion_id: str | None = None) -> dict:
    """Complete body of a new profile (empty or from a suggestion)."""
    sug = suggestion(suggestion_id) if suggestion_id else None
    return resolver.materialize(catalog(), sug)


def always_fixed() -> set[str]:
    return {r["key"] for r in catalog()["rules"] if not r["fixed_checkbox"]}


def user_choice_allowed() -> set[str]:
    return {r["key"] for r in catalog()["rules"] if r["user_choice_allowed"]}


# ── validation ──────────────────────────────────────────────────────────────


def check_value(key: str, value: Any, *, code_prefix: str = "rules") -> Any:
    """Validated copy of one rule value; raises :class:`ProfileError` (422)."""
    if rule(key) is None:
        raise ProfileError(422, f"{code_prefix}.{key}", f"unknown rule {key}", key)
    errs = rule_value_errors(key, value)
    if errs:
        raise ProfileError(422, f"{code_prefix}.{key}", "; ".join(errs[:3]), key)
    return copy.deepcopy(value)


def check_rules(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        raise ProfileError(422, "rules", "rules must be an object")
    return {key: check_value(key, value) for key, value in values.items()}


def check_keys(keys: Any, field: str) -> list[str]:
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise ProfileError(422, field, f"{field} must be a list of rule keys")
    for key in keys:
        if rule(key) is None:
            raise ProfileError(422, f"{field}.{key}", f"unknown rule {key}", key)
    return sorted(set(keys))


def normalize_body(body: dict) -> dict:
    """Stored body: complete rule values, ``fixed`` incl. rules without the
    checkbox, ``user_choice`` only for rules that allow it."""
    rules = {k: copy.deepcopy(v) for k, v in body.get("rules", {}).items() if rule(k)}
    fixed = set(body.get("fixed", [])) | always_fixed()
    fixed = {k for k in fixed if rule(k)}
    uc = set(body.get("user_choice", [])) & user_choice_allowed()
    return {
        "schema_version": BODY_SCHEMA_VERSION,
        "rules": rules,
        "fixed": sorted(fixed),
        "user_choice": sorted(uc),
    }


def check_user_choice(keys: list[str]) -> list[str]:
    for key in keys:
        if key not in user_choice_allowed():
            raise ProfileError(
                422, f"user_choice.{key}", f"{key} cannot be the user's choice", key
            )
    return keys


def migrate_body(raw: Any) -> dict:
    """Body of a stored profile in the current schema version.

    Version 1 is the first one; later catalog changes add a step here
    (vN -> vN+1). Rules missing in a stored body are not filled in: the
    resolver uses the catalog default (source ``default``) until the admin
    saves the profile.
    """
    body = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    version = body.get("schema_version", 1)
    if version != BODY_SCHEMA_VERSION:
        raise ValueError(f"unknown profile body version {version}")
    return {
        "schema_version": BODY_SCHEMA_VERSION,
        "rules": {k: v for k, v in body.get("rules", {}).items() if rule(k)},
        "fixed": sorted(set(body.get("fixed", [])) | always_fixed()),
        "user_choice": sorted(set(body.get("user_choice", [])) & user_choice_allowed()),
    }


def catalog_defaults(body: dict) -> list[str]:
    """Catalog rules the stored profile has no value for (new after an update)."""
    return sorted(k for k in rules_by_key() if k not in body.get("rules", {}))


def order_index(key: str, value: Any) -> int | None:
    """Position of ``value`` in the rule's order (strict → loose), None = unordered."""
    r = rule(key)
    if not r or not r.get("order"):
        return None
    for i, v in enumerate(r["order"]):
        if resolver.json_equal(v, value):
            return i
    return None


def loosens(key: str, before: Any, after: Any) -> bool:
    """``after`` is looser than ``before`` for a critical/sensitive rule."""
    r = rule(key)
    if not r or r.get("security") not in ("critical", "sensitive"):
        return False
    # null switches the rate limit off.
    if key == "kids_rate_limit":
        return isinstance(before, dict) and _rate_looser(before, after)
    if key == "kids_rate_limit_all":
        if not isinstance(before, dict) or not before.get("enabled"):
            return False
        if not isinstance(after, dict) or not after.get("enabled"):
            return True
        return _rate_looser(before, after)
    if before is None or after is None:
        return False
    if key == "kids_quiet_hours":
        return bool(_quiet_minutes(before) - _quiet_minutes(after))
    if key == "kids_exceptions":
        return _exceptions_looser(before, after)
    if key == "setup_devices":
        if not after.get("enabled"):
            return False
        if not before.get("enabled"):
            return True
        return bool(set(after.get("kinds", [])) - set(before.get("kinds", [])))
    if key == "areas":
        if after.get("mode") == "all":
            return before.get("mode") != "all"
        if before.get("mode") == "all":
            return False
        return bool(
            set(after.get("area_ids", [])) - set(before.get("area_ids", []))
            or set(after.get("tags", [])) - set(before.get("tags", []))
        )
    # Unordered values: compared by what they allow.
    if key == "access_hours":
        if not isinstance(before, dict) or not before.get("enabled"):
            return False
        if not isinstance(after, dict) or not after.get("enabled"):
            return True
        old = before.get("windows") or []
        return any(w not in old for w in after.get("windows") or [])
    if key == "kids_blocked_types":
        if not isinstance(before, list) or not isinstance(after, list):
            return False
        return bool(set(before) - set(after))
    if key == "remote_control":
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        return _remote_looser(before, after)
    i, j = order_index(key, before), order_index(key, after)
    if i is None or j is None:
        return False
    return j > i


_RATE_MORE = ("max_actions", "max_entities")  # higher = looser
_RATE_LESS = ("window_seconds", "cooldown_seconds")  # lower = looser
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _is_num(v: Any) -> bool:
    """A finite number (int or float, never a bool), compared by value."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _rate_looser(before: dict, after: Any) -> bool:
    """A rate limit (per entity or for everything) allows more than before."""
    if not isinstance(after, dict):
        return True  # switched off

    def num(value: dict, field: str) -> float | None:
        v = value.get(field)
        return v if _is_num(v) else None

    for field in _RATE_MORE + _RATE_LESS:
        b, a = num(before, field), num(after, field)
        if b is None:
            continue
        if a is None:
            return True
        if (a > b) if field in _RATE_MORE else (a < b):
            return True
    return False


def _quiet_minutes(windows: Any) -> set[int]:
    """Minutes of the week inside a bedtime window (defs quiet_hours_window:
    end <= start runs into the next day, start == end is the whole day)."""
    out: set[int] = set()
    if not isinstance(windows, list):
        return out
    week = 7 * 1440
    for w in windows:
        if not isinstance(w, dict):
            continue
        try:
            # HH:MM (contract); seconds, if any, are ignored like in the admin UI
            sh, sm = (int(x) for x in str(w.get("start")).split(":")[:2])
            eh, em = (int(x) for x in str(w.get("end")).split(":")[:2])
        except ValueError:
            continue
        start, end = sh * 60 + sm, eh * 60 + em
        length = end - start if end > start else end - start + 1440
        for day in w.get("days") or []:
            if day in _WEEKDAYS:
                base = _WEEKDAYS.index(day) * 1440 + start
                out.update((base + i) % week for i in range(length))
    return out


def _kids_rules_looser(before: dict, after: dict) -> bool:
    """One kids rules object (kids-rules.json) allows more than before.

    A field that is missing inherits from the profile, which is unknown
    here: setting or dropping it counts as looser unless the new value is
    the strict one (``blocked``/``color_lock`` true, dropping a false).
    """
    for field in set(before) | set(after):
        b, a = before.get(field), after.get(field)
        if resolver.json_equal(b, a):
            continue
        if field in ("blocked", "color_lock"):
            # false allows explicitly; dropping true inherits the unknown
            if a is False or (a is None and b is True):
                return True
        elif field == "blocked_types":
            if not isinstance(b, list) or not isinstance(a, list):
                return True
            if set(b) - set(a):
                return True
        elif field == "quiet_hours":
            if field not in before or field not in after:
                return True
            if _quiet_minutes(b) - _quiet_minutes(a):
                return True
        elif field == "rate_limit":
            if not isinstance(b, dict) or _rate_looser(b, a):
                return True
        elif field == "volume_max":
            if not _is_num(b) or not _is_num(a) or a > b:
                return True
        else:
            return True
    return False


def _exceptions_looser(before: Any, after: Any) -> bool:
    """An exception for an area or device was added or allows more."""

    def by_target(value: Any) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for item in value if isinstance(value, list) else []:
            if isinstance(item, dict) and isinstance(item.get("rules"), dict):
                out[json.dumps(item.get("target"), sort_keys=True)] = item["rules"]
        return out

    b, a = by_target(before), by_target(after)
    return any(_kids_rules_looser(b.get(t, {}), a.get(t, {})) for t in set(b) | set(a))


_REMOTE_LEVELS = ("hidden", "view", "control")


def _remote_looser(before: dict, after: dict) -> bool:
    """A level of ``remote_control`` (default, a domain or an entity, each
    falling back to the default) is looser after than before."""

    def level(value: dict, part: str | None, key: str | None) -> int:
        # Precedence entity > domain:device_class > domain > default.
        domains = value.get("domains") or {}
        v = None
        if part == "entities":
            v = (value.get("entities") or {}).get(key)
            key = key.split(".", 1)[0] if key else key
        elif part == "domains":
            v = domains.get(key)
        if v is None and key:
            v = domains.get(key.split(":", 1)[0])
        if v is None:
            v = value.get("default")
        return _REMOTE_LEVELS.index(v) if v in _REMOTE_LEVELS else len(_REMOTE_LEVELS)

    if level(after, None, None) > level(before, None, None):
        return True
    for part in ("domains", "entities"):
        keys = set(before.get(part) or {}) | set(after.get(part) or {})
        if any(level(after, part, k) > level(before, part, k) for k in keys):
            return True
    return False
