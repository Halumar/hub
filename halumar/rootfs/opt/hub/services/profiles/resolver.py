"""Profile resolver: port of the contract's ``tools/profiles_ref.py``.

Pure functions, no database, no network, no clock (``now`` is a parameter).
The shared vectors of the contract (``tests/fixtures/profiles``) and the
Hypothesis properties in ``tests/test_profile_resolver.py`` keep this file
and the reference resolver identical in behavior.

  materialize(catalog, suggestion=None)          -> profile body
  resolve(catalog, platform=, access=, profile=, overrides=, device=, now=)
                                                  -> {rule key: effective}
  expiry(preset, now, time_zone)                 -> aware datetime
  client_rules(effective)                        -> client_config.rules
  remote_level(effective, entity_id, device_class)    -> level of remote_control
  entity_access(effective, entity_id=, device_class=, route=, quick_access=, catalog=)
  notification_shown(catalog, effective, priority, entity_id=, device_class=)
  remote_pin_source(profile, device)             -> "profile" | "device" | None
  ha_user_group(effective)                       -> "read_only" | "users"
  holds_key(effective)                           -> bool (own Home Assistant key)
  ha_access(effective, platform=, child=, access_open=, phone=)
                                                  -> Home Assistant access fields of client_config
  key_level(effective) / key_looser(new, mine)   -> no escalation of the key
  snapshot_control_path(effective)               -> "checked" | "direct"
"""

from __future__ import annotations

import copy
import functools
import datetime as dt
import json
from zoneinfo import ZoneInfo

# Order of the boundaries when several apply; on equal caps the first one is reported.
BOUNDARY_ORDER = ("guest", "child_protection", "hub_only")
# Platforms whose apps cannot run without their own Home Assistant key (a
# watch has no hub socket, the TV apps read and switch through Home
# Assistant): "Hub only" resolves to "checked" there (platform_needs_key).
KEY_PLATFORMS = ("tvos", "android_tv", "watchos", "wearos")
WATCH_PLATFORMS = ("watchos", "wearos")
CONTROL_PATHS = ("hub", "checked", "direct")
# Profile kinds whose devices are guest accesses (catalog profile_kinds with guest_access).
GUEST_KINDS = ("guest", "house_sitter", "babysitter")
MORNING_HOUR = 7
NIGHT_UNTIL_HOUR = (
    5  # from 00:00 to 04:59 "tomorrow morning" means 07:00 of the same day
)


def json_equal(a, b) -> bool:
    """JSON equality that keeps true and 1 apart."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def parse_ts(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def fmt_ts(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


# ── profiles ──────────────────────────────────────────────────────────────────


def materialize(catalog: dict, suggestion: dict | None = None) -> dict:
    """Profile body of a new profile: empty (catalog defaults) or a copy of a suggestion."""
    rules = {r["key"]: copy.deepcopy(r["default"]) for r in catalog["rules"]}
    always_fixed = {r["key"] for r in catalog["rules"] if not r["fixed_checkbox"]}
    if suggestion is None:
        fixed = {r["key"] for r in catalog["rules"] if r["default_fixed"]}
        user_choice = {r["key"] for r in catalog["rules"] if r["default_user_choice"]}
        child = False
        kind = "person"
    else:
        rules.update(copy.deepcopy(suggestion["rules"]))
        fixed = set(suggestion["fixed"])
        user_choice = set(suggestion["user_choice"])
        child = suggestion["child_protection"]
        kind = suggestion["kind"]
    allowed_uc = {r["key"] for r in catalog["rules"] if r["user_choice_allowed"]}
    return {
        "kind": kind,
        "child_protection": child,
        "rules": rules,
        "fixed": sorted(fixed | always_fixed),
        "user_choice": sorted(user_choice & allowed_uc),
    }


def starter_for_kind(suggestions: list[dict], kind: str) -> dict | None:
    """Starter suggestion of a profile kind (person: Adult, child: Child, ...)."""
    return next((s for s in suggestions if s["starter"] and s["kind"] == kind), None)


def fill_missing(
    catalog: dict, suggestions: list[dict], body: dict, origin: str | None = None
) -> dict:
    """Body with the comfort rules a stored profile does not hold yet.

    A rule new after a hub update that the person may choose on the device
    gets the value of the origin suggestion, else of the starter suggestion of
    the profile kind, else the catalog default, and is the user's choice as
    the catalog says (``default_user_choice``): it is never locked before the
    admin decides. Filled keys are listed in ``filled`` and resolve with
    source default; saving the profile stores them.
    """
    out = {
        **body,
        "rules": dict(body.get("rules", {})),
        "user_choice": list(body.get("user_choice", [])),
        "filled": list(body.get("filled", [])),
    }
    by_id = {s["id"]: s for s in suggestions}
    sources = [
        by_id.get(origin) if origin else None,
        starter_for_kind(suggestions, body.get("kind", "person")),
    ]
    for rule in catalog["rules"]:
        key = rule["key"]
        if key in out["rules"] or not rule["user_choice_allowed"]:
            continue
        value = next(
            (s["rules"][key] for s in sources if s and key in s["rules"]),
            rule["default"],
        )
        out["rules"][key] = copy.deepcopy(value)
        out["filled"].append(key)
        if rule["default_user_choice"] and key not in out["user_choice"]:
            out["user_choice"].append(key)
    out["user_choice"] = sorted(out["user_choice"])
    return out


# ── resolver ──────────────────────────────────────────────────────────────────


def _normalize(key: str, value):
    """Canonical form of object values (missing optional fields filled in)."""
    if key == "areas" and isinstance(value, dict):
        return {
            "mode": value["mode"],
            "area_ids": list(value.get("area_ids", [])),
            "tags": list(value.get("tags", [])),
            "own_area": bool(value.get("own_area", False)),
        }
    if key == "panel_start" and isinstance(value, dict):
        return {
            "mode": value["mode"],
            "area_id": value.get("area_id") if value["mode"] == "area" else None,
        }
    if key == "control_path" and value not in CONTROL_PATHS:
        return "hub"  # an unknown value is the strictest one (fail closed)
    return value


def _resolve_own_area(key: str, value, own_area_id: str | None):
    """Replace the symbolic own area by the device's area (or drop it: fail closed)."""
    value = _normalize(key, copy.deepcopy(value))
    if key == "areas":
        if value["own_area"]:
            if own_area_id and own_area_id not in value["area_ids"]:
                value["area_ids"].append(own_area_id)
            value["own_area"] = False
        if value["mode"] == "all":
            value["area_ids"], value["tags"] = [], []
    elif key == "panel_start":
        if value["mode"] == "own_area":
            value = (
                {"mode": "area", "area_id": own_area_id}
                if own_area_id
                else {"mode": "overview", "area_id": None}
            )
    elif key == "kids_exceptions":
        out = []
        for exc in value:
            target = exc["target"]
            if target.get("own_area"):
                if not own_area_id:
                    continue
                exc = {"target": {"area_id": own_area_id}, "rules": exc["rules"]}
            out.append(exc)
        value = out
    return value


def _index(rule: dict, value) -> int:
    for i, v in enumerate(rule["order"]):
        if json_equal(v, value):
            return i
    raise ValueError(f"{rule['key']}: value {value!r} not in order")


def _entry(
    value,
    source,
    reason,
    *,
    user_choice=False,
    fixed=False,
    expires_at=None,
    then=None,
) -> dict:
    return {
        "value": value,
        "source": source,
        "reason": reason,
        "locked": not user_choice,
        "fixed": fixed,
        "user_choice": user_choice,
        "expires_at": expires_at,
        "then": then,
    }


def remote_pin_source(profile: dict, device: dict) -> str | None:
    """Effective remote PIN: a device PIN wins, unless the profile PIN is fixed
    (then the device PIN is ignored and the API rejects setting one)."""
    has_profile = bool(profile.get("has_remote_pin"))
    if has_profile and profile.get("remote_pin_fixed"):
        return "profile"
    if device.get("has_remote_pin"):
        return "device"
    return "profile" if has_profile else None


def _clamp(
    rule: dict,
    entry: dict,
    *,
    access: dict,
    profile: dict,
    device: dict,
    expired: bool,
    resolved: dict | None = None,
) -> dict:
    """Boundaries, prerequisites and guest expiry, in this order. Never loosens a value.

    ``resolved``: the rules resolved so far (catalog order). ``hub_only`` is
    active unless ``control_path`` resolved to checked or direct (it comes
    first in the catalog; unknown counts as hub, fail closed). A boundary with
    ``lifted_by`` does not apply while that rule resolved to true (parental
    controls: ``watch`` only with ``kids_watch``; hub_only: ``quick_access``
    and ``watch`` only with ``remote_access``)."""
    e = dict(entry)
    path = ((resolved or {}).get("control_path") or {}).get("value")
    active = [
        b
        for b in BOUNDARY_ORDER
        if (b == "guest" and access["kind"] == "guest")
        or (b == "child_protection" and profile["child_protection"])
        or (b == "hub_only" and path not in ("checked", "direct"))
    ]
    by_boundary = {b["boundary"]: b for b in rule["boundaries"]}
    for name in active:
        b = by_boundary.get(name)
        if b is None:
            continue
        lifted = b.get("lifted_by")
        if lifted and ((resolved or {}).get(lifted) or {}).get("value") is True:
            continue
        if rule["order"] is None:
            # A rule without order (object value, e.g. setup_devices): the
            # boundary always decides.
            capped, decides = copy.deepcopy(b["cap"]), True
        else:
            capped = (
                e["value"]
                if _index(rule, e["value"]) <= _index(rule, b["cap"])
                else b["cap"]
            )
            decides = json_equal(b["cap"], rule["order"][0])
        if not json_equal(capped, e["value"]) or (
            decides and e["source"] != "boundary"
        ):
            e = _entry(capped, "boundary", b["reason"], fixed=True)
    p = rule.get("prerequisite")
    if (
        p
        and json_equal(e["value"], p["when"])
        and p["requires"] == "remote_pin"
        and remote_pin_source(profile, device) is None
    ):
        e = _entry(
            p["otherwise"], "prerequisite", "prerequisite_missing", fixed=e["fixed"]
        )
    if expired and rule.get("strictest") is not None:
        e = _entry(
            copy.deepcopy(rule["strictest"]),
            "access_expired",
            "access_expired",
            fixed=True,
        )
    return e


def _platform_needs_key(platform: str, entry: dict) -> dict:
    """control_path on a platform that needs its own Home Assistant key: hub
    becomes checked (source boundary, reason platform_needs_key), also as the
    value after a temporary adjustment (``then``). The one documented case
    where a boundary raises a value, and never to direct. An expired guest
    keeps hub (access_expired: it gets no key at all)."""
    if platform not in KEY_PLATFORMS or entry["source"] == "access_expired":
        return entry
    e = dict(entry)
    if e["value"] == "hub":
        e.update(
            value="checked",
            source="boundary",
            reason="platform_needs_key",
            locked=True,
            fixed=True,
            user_choice=False,
        )
    if e.get("then") == "hub":
        e["then"] = "checked"
    if (
        e["source"] == "boundary"
        and e.get("expires_at") is not None
        and json_equal(e["then"], e["value"])
    ):
        e["expires_at"], e["then"] = None, None  # nothing changes at the end
    return e


def _depends_on(rule: dict) -> list[str]:
    """Earlier rules the boundaries of ``rule`` read: every ``lifted_by``
    rule, and control_path for hub_only."""
    deps = []
    for b in rule["boundaries"]:
        if b.get("lifted_by") and b["lifted_by"] not in deps:
            deps.append(b["lifted_by"])
        if b["boundary"] == "hub_only" and "control_path" not in deps:
            deps.append("control_path")
    return deps


def _dependency_ends(
    rule: dict,
    pre: dict,
    eff: dict,
    *,
    access: dict,
    profile: dict,
    device: dict,
    expired: bool,
    resolved: dict,
) -> dict:
    """A rule the boundaries read can change at its ``expires_at`` (a
    temporary remote access ends, a temporary checked ends and hub_only
    applies again, a lift ends). Walking those ends in time order, before the
    entry's own ``expires_at``: at the first end that changes the value, the
    entry carries that ``expires_at`` and as ``then`` the value from then on,
    so an offline app stops in time."""
    ends = sorted(
        (parse_ts(resolved[d]["expires_at"]), d)
        for d in _depends_on(rule)
        if resolved.get(d) and resolved[d].get("expires_at")
    )
    own = parse_ts(eff.get("expires_at"))
    after = dict(resolved)
    for t_end, dep in ends:
        if own is not None and own <= t_end:
            break
        d = resolved[dep]
        after[dep] = {**d, "value": d.get("then"), "expires_at": None, "then": None}
        then = _clamp(
            rule,
            pre,
            access=access,
            profile=profile,
            device=device,
            expired=expired,
            resolved=after,
        )
        if not json_equal(then["value"], eff["value"]):
            return {**eff, "expires_at": d["expires_at"], "then": then["value"]}
    return eff


def resolve(
    catalog: dict,
    *,
    platform: str,
    access: dict,
    profile: dict,
    overrides: list,
    device: dict,
    now: str,
) -> dict:
    """Effective value of every catalog rule for one device.

    access   {"kind": "member"|"guest", "expires_at": ts|None}
    profile  {"kind": profile kind (missing = person), "child_protection": bool, "rules": {key: value} (missing = catalog default),
              "fixed": [key], "user_choice": [key], "filled": [key] (comfort rules filled in
              by fill_missing: value as given, source default)}
    overrides [{"rule": key, "value": v, "user_choice": bool?, "expires_at": ts|None}]
    device   {"own_area_id": uuid|None, "has_remote_pin": bool}
    """
    t_now = parse_ts(now)
    t_exp = parse_ts(access.get("expires_at"))
    expired = access["kind"] == "guest" and t_exp is not None and t_now >= t_exp
    by_rule = {}
    for o in overrides:
        by_rule[o["rule"]] = o  # one adjustment per rule; the hub stores no duplicates
    fixed_keys = set(profile.get("fixed", []))
    uc_keys = set(profile.get("user_choice", []))
    out: dict[str, dict] = {}
    for rule in catalog["rules"]:
        key = rule["key"]
        if platform not in rule["platforms"]:
            out[key] = _entry(None, "not_applicable", "not_applicable", fixed=True)
            continue
        if rule["requires_child_protection"] and not profile["child_protection"]:
            out[key] = _entry(
                None, "not_applicable", "requires_child_protection", fixed=True
            )
            continue
        kinds = rule.get("profile_kinds")
        if kinds is not None and profile.get("kind", "person") not in kinds:
            out[key] = _entry(
                None, "not_applicable", "requires_profile_kind", fixed=True
            )
            continue
        fixed = (not rule["fixed_checkbox"]) or key in fixed_keys
        if key in profile.get("rules", {}):
            value = profile["rules"][key]
            source = "default" if key in profile.get("filled", ()) else "profile"
        elif key == "control_path" and (
            profile["child_protection"]
            or access["kind"] == "guest"
            or profile.get("kind", "person") in GUEST_KINDS
        ):
            # Parental controls and guests without a stored value: the hub
            # path, no key by default (fail closed).
            value, source = "hub", "default"
        else:
            value, source = rule["default"], "default"
        uc = rule["user_choice_allowed"] and key in uc_keys
        reason = (
            "user_choice"
            if uc
            else ("profile" if source == "profile" else "catalog_default")
        )
        base = _entry(
            _resolve_own_area(key, value, device.get("own_area_id")),
            source,
            reason,
            user_choice=uc,
            fixed=fixed,
        )
        eff = _clamp(
            rule,
            base,
            access=access,
            profile=profile,
            device=device,
            expired=expired,
            resolved=out,
        )
        pre = base
        o = by_rule.get(key)
        o_end = parse_ts(o.get("expires_at")) if o else None
        if o and not fixed and (o_end is None or t_now < o_end):
            o_uc = rule["user_choice_allowed"] and bool(o.get("user_choice", False))
            o_reason = (
                "user_choice" if o_uc else ("device_temporary" if o_end else "device")
            )
            dev = _entry(
                _resolve_own_area(key, o["value"], device.get("own_area_id")),
                "device",
                o_reason,
                user_choice=o_uc,
                fixed=False,
            )
            adj = _clamp(
                rule,
                dev,
                access=access,
                profile=profile,
                device=device,
                expired=expired,
                resolved=out,
            )
            if o_end is not None and (
                adj["source"] == "device" or not json_equal(adj["value"], eff["value"])
            ):
                # A temporary adjustment, also one a boundary caps, ends with
                # its value from then on.
                adj["expires_at"] = o["expires_at"]
                adj["then"] = eff["value"]
            eff = adj
            pre = dev
        eff = _dependency_ends(
            rule,
            pre,
            eff,
            access=access,
            profile=profile,
            device=device,
            expired=expired,
            resolved=out,
        )
        if key == "control_path":
            eff = _platform_needs_key(platform, eff)
        # A child's watch switches only through Home Assistant: "Allow a
        # watch" needs remote access (resolved before, catalog order), else
        # it does not apply and the phone's rule watch stays capped.
        if key == "kids_watch" and out["remote_access"]["value"] is not True:
            eff = _entry(None, "not_applicable", "requires_remote_access", fixed=True)
        elif (
            key == "kids_watch"
            and eff["source"] != "not_applicable"
            and eff.get("expires_at") is None
            and out["remote_access"].get("expires_at")
        ):
            # It applies only as long as a temporary remote access runs.
            eff = {
                **eff,
                "expires_at": out["remote_access"]["expires_at"],
                "then": None,
            }
        out[key] = eff
    # "When away" only exists with remote access.
    if (
        "remote_control" in out
        and out["remote_control"]["source"] != "not_applicable"
        and out["remote_access"]["value"] is not True
    ):
        out["remote_control"] = _entry(
            None, "not_applicable", "requires_remote_access", fixed=True
        )
    # "Location required" only exists while the location may be shared.
    if (
        "location_required" in out
        and out["location_required"]["source"] != "not_applicable"
        and out["location"]["value"] in (None, "off")
    ):
        out["location_required"] = _entry(
            None, "not_applicable", "requires_location", fixed=True
        )
    _mark_enforcement(catalog, out)
    return out


def _mark_enforcement(catalog: dict, out: dict) -> None:
    """``enforcement`` of every entry from the effective control path: on the
    hub path the hub enforces (``enforcement_with_hub_path``); on the hub and
    the checked path Home Assistant enforces operating (device user read-only,
    switching only through the integration action:
    ``enforcement_with_checked_path``)."""
    path = (out.get("control_path") or {}).get("value")
    for rule in catalog["rules"]:
        e = out[rule["key"]]
        e["enforcement"] = (
            (rule.get("enforcement_with_hub_path") if path == "hub" else None)
            or (
                rule.get("enforcement_with_checked_path")
                if path in ("hub", "checked")
                else None
            )
            or rule["enforcement"]
        )


def ha_user_group(effective: dict) -> str:
    """Home Assistant group of the device user: users with the direct control
    path, else read_only (hub and checked; a rule that does not apply keeps
    the strict default)."""
    cp = effective.get("control_path") or {}
    return "users" if cp.get("value") == "direct" else "read_only"


def holds_key(effective: dict) -> bool:
    """The device gets its own Home Assistant key: on the checked or direct
    path, or on the hub path with effective remote access (a read-only key).
    An unknown or missing control path counts as hub (fail closed). Whether
    the key is delivered now also depends on the access (closed: ha_token
    null)."""
    path = (effective.get("control_path") or {}).get("value")
    return (
        path in ("checked", "direct")
        or (effective.get("remote_access") or {}).get("value") is True
    )


def snapshot_control_path(effective: dict) -> str:
    """Control path of the device in Home Assistant (rule snapshot,
    ha_enforcement, control_paths): direct stays direct, everything else
    (hub, checked, unknown) is checked."""
    cp = effective.get("control_path") or {}
    return "direct" if cp.get("value") == "direct" else "checked"


def uses_hub_path(effective: dict, *, child: bool, platform: str) -> bool:
    """The device reads and switches through the hub where it is reachable:
    parental controls or control path hub (unknown counts as hub). A watch
    or a TV never: they always use their own key."""
    path = (effective.get("control_path") or {}).get("value")
    return platform not in KEY_PLATFORMS and (
        child or path not in ("checked", "direct")
    )


# What a device holds of Home Assistant, weakest first (no-escalation
# compares control_path by this).
KEY_LEVELS = ("none", "read_only", "full")


def key_level(effective: dict) -> str:
    """full on the direct path, read_only when the device holds a key
    otherwise (checked, or hub with remote access), else none."""
    if (effective.get("control_path") or {}).get("value") == "direct":
        return "full"
    return "read_only" if holds_key(effective) else "none"


def key_looser(new: dict, mine: dict) -> bool:
    """No escalation for control_path: the new device (resolved on its
    platform) holds more of Home Assistant than the setting-up device
    (none < read_only < full)."""
    return KEY_LEVELS.index(key_level(new)) > KEY_LEVELS.index(key_level(mine))


def ha_access(
    effective: dict,
    *,
    platform: str,
    child: bool,
    access_open: bool,
    phone: dict | None = None,
) -> dict:
    """The Home Assistant access fields of client_config for one device.

    holds_key       holds_key(effective); a watch only while its phone's
                    effective rule watch is on (``phone``: the phone's
                    effective rules at the same moment; missing = no key)
    hub_path        uses_hub_path(effective, child=, platform=)
    token           ha_token: "clear", "sealed" (a watch: only inside
                    watch_access) or None; only while the access is open
    ha_enforcement  snapshot_control_path(effective) with the token, else None
    ha_url          may be sent: remote access and the access open
    ha_instance_id  may be sent: always on the checked and direct path, on the
                    hub path only with the token
    kids_away       with parental controls "ha" exactly when the device holds
                    a key, else "none"; None without
    """
    key = holds_key(effective)
    if (
        platform in WATCH_PLATFORMS
        and ((phone or {}).get("watch") or {}).get("value") is not True
    ):
        key = False
    hub_path = uses_hub_path(effective, child=child, platform=platform)
    token = key and access_open
    remote = (effective.get("remote_access") or {}).get("value") is True
    return {
        "holds_key": key,
        "hub_path": hub_path,
        "token": ("sealed" if platform in WATCH_PLATFORMS else "clear")
        if token
        else None,
        "ha_enforcement": snapshot_control_path(effective) if token else None,
        "ha_url": remote and access_open,
        "ha_instance_id": token if (hub_path or child) else True,
        "kids_away": ("ha" if key else "none") if child else None,
    }


def client_rules(effective: dict) -> dict:
    """client_config.rules: the effective rules without the not applicable ones."""
    return {k: v for k, v in effective.items() if v["source"] != "not_applicable"}


# ── when away ──────────────────────────────────────────────────────────

REMOTE_ORDER = ("hidden", "view", "control")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEK_MINUTES = 7 * 1440
# How a rule is limited to the phone's (catalog watch_limit, profiles_ref).
WATCH_LIMIT_CLASSES = ("none", "ordered", "areas", "schedule", "remote_levels", "kinds")


def _remote_level_of(value: dict, key: str) -> str:
    if "." in key and key in value.get("entities", {}):
        return value["entities"][key]
    domain = key.split(".", 1)[0] if "." in key else key
    if key in value.get("domains", {}):
        return value["domains"][key]
    if ":" in domain and domain.split(":", 1)[0] in value.get("domains", {}):
        return value["domains"][domain.split(":", 1)[0]]
    if domain in value.get("domains", {}):
        return value["domains"][domain]
    return value.get("default", "hidden")


def remote_wider(new: dict, mine: dict) -> bool:
    """``remote_control`` ``new`` reaches something further than ``mine``."""
    keys = {"*"}
    for v in (new, mine):
        keys.update(v.get("domains", {}))
        keys.update(v.get("entities", {}))
    for key in keys:
        n = new.get("default") if key == "*" else _remote_level_of(new, key)
        m = mine.get("default") if key == "*" else _remote_level_of(mine, key)
        if REMOTE_ORDER.index(n) > REMOTE_ORDER.index(m):
            return True
    return False


def open_minutes(windows: list) -> set[int]:
    """Minutes of the week (local time, Monday 00:00 = 0) the windows open."""
    out: set[int] = set()
    for w in windows:
        try:
            sh, sm = (int(x) for x in w["start"].split(":"))
            eh, em = (int(x) for x in w["end"].split(":"))
            days = [DAYS.index(d) for d in w["days"]]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        start, end = sh * 60 + sm, eh * 60 + em
        length = end - start if end > start else end + 1440 - start
        for day in days:
            first = day * 1440 + start
            out.update((first + i) % WEEK_MINUTES for i in range(length))
    return out


def hours_wider(new, mine) -> bool:
    """Access hours ``new`` open at a time ``mine`` is closed (off = open)."""
    if not isinstance(mine, dict) or not mine.get("enabled"):
        return False
    if not isinstance(new, dict) or not new.get("enabled"):
        return True
    return not open_minutes(new.get("windows") or []) <= open_minutes(
        mine.get("windows") or []
    )


def limited_keys(catalog: dict) -> list[str]:
    """Rules a watch never has looser than its phone (``watch_limit`` other than ``none``): the
    No-escalation rules of setting up (critical/sensitive outside kids and location) and the areas."""
    return [
        r["key"] for r in catalog["rules"] if r.get("watch_limit", "none") != "none"
    ]


def _class(rule: dict) -> str:
    """The rule's ``watch_limit`` class; an unknown one is refused (never a silent replace)."""
    kind = rule.get("watch_limit")
    if kind not in WATCH_LIMIT_CLASSES:
        raise ValueError(f"rule {rule['key']}: no watch_limit class ({kind!r})")
    return kind


def _area_set(value) -> tuple[bool, set, set]:
    """(all, area ids, tags) of a resolved ``areas`` value (own area already an id)."""
    if not isinstance(value, dict) or value.get("mode") == "all":
        return (isinstance(value, dict), set(), set())
    return (False, set(value.get("area_ids", [])), set(value.get("tags", [])))


def _hours_minutes(value) -> set | None:
    """Open minutes of the week of an ``access_hours`` value; ``None`` = always open."""
    if not isinstance(value, dict) or not value.get("enabled"):
        return None
    return open_minutes(value.get("windows") or [])


def _hours_value(minutes: set | None) -> dict:
    """``access_hours`` that open exactly at ``minutes`` (one window per run, a run at most a day;
    windows of equal times on several days share one entry). More than the allowed 14 entries: the
    rest is closed (never more open)."""
    if minutes is None:
        return {"enabled": False, "windows": []}
    if len(minutes) == WEEK_MINUTES:
        return {"enabled": False, "windows": []}
    runs: list[tuple[int, int]] = []
    starts = sorted(m for m in minutes if (m - 1) % WEEK_MINUTES not in minutes)
    for start in starts:
        length = 0
        while (start + length) % WEEK_MINUTES in minutes and length < WEEK_MINUTES:
            length += 1
        while length > 0:
            part = min(length, 1440)
            runs.append((start, part))
            start, length = (start + part) % WEEK_MINUTES, length - part
    by_time: dict[tuple[str, str], list[str]] = {}
    for start, length in runs:
        day, minute = divmod(start, 1440)
        end = (minute + length) % 1440
        key = (
            f"{minute // 60:02d}:{minute % 60:02d}",
            f"{end // 60:02d}:{end % 60:02d}",
        )
        by_time.setdefault(key, []).append(DAYS[day])
    windows = [
        {"days": sorted(set(days), key=DAYS.index), "start": s, "end": e}
        for (s, e), days in sorted(
            by_time.items(), key=lambda kv: (DAYS.index(kv[1][0]), kv[0])
        )
    ]
    return {"enabled": True, "windows": windows[:14]}


def _remote_at(
    value: dict, entity: str | None, domain: str, device_class: str | None
) -> str:
    """Runtime level of ``remote_control`` (entity > domain:device_class > domain > default)."""
    if entity and entity in value.get("entities", {}):
        return value["entities"][entity]
    domains = value.get("domains", {})
    if device_class and f"{domain}:{device_class}" in domains:
        return domains[f"{domain}:{device_class}"]
    if domain in domains:
        return domains[domain]
    return value.get("default", "hidden")


def _remote_space(*values: dict) -> tuple[set, dict, set]:
    """(domains, classes per domain, entities) any side names."""
    domains: set = set()
    classes: dict = {}
    entities: set = set()
    for v in values:
        for key in v.get("domains", {}):
            domain, _, cls = key.partition(":")
            domains.add(domain)
            if cls:
                classes.setdefault(domain, set()).add(cls)
        for e in v.get("entities", {}):
            entities.add(e)
            domains.add(e.split(".", 1)[0])
    return domains, classes, entities


def _remote_lowest(value: dict, entity: str, classes: set) -> str:
    """The lowest level ``entity`` can have in ``value`` whatever its device class (no state known
    for it): its own key, else the lowest of every class key of its domain, the domain and default."""
    domain = entity.split(".", 1)[0]
    if entity in value.get("entities", {}):
        return value["entities"][entity]
    levels = [_remote_at(value, None, domain, c) for c in [None, *sorted(classes)]]
    return min(levels, key=REMOTE_ORDER.index)


def _remote_entity(
    value: dict, entity: str, classes: set, entity_classes: dict | None
) -> str:
    """Level of a named entity in ``value``: with its device class known (``entity_classes``: entity id
    -> device class or null, the hub's states) its real runtime level, else the lowest it can have."""
    if entity_classes is not None and entity in entity_classes:
        return _remote_at(
            value, entity, entity.split(".", 1)[0], entity_classes[entity]
        )
    return _remote_lowest(value, entity, classes)


def _remote_cases(a: dict, b: dict, entity_classes: dict | None = None):
    """Every distinct (entity, domain, device class) the levels of ``a`` and ``b`` can tell apart:
    named entities (with their known class only, else every class) and domains with every class either
    side names, an unnamed class and none, and an entity and domain nobody names."""
    domains, classes, entities = _remote_space(a, b)
    known = entity_classes or {}
    for domain in sorted(domains) + ["~other"]:
        named = [e for e in sorted(entities) if e.split(".", 1)[0] == domain]
        for entity in [*named, None]:
            if entity in known:
                yield entity, domain, known[entity]
                continue
            for cls in [*sorted(classes.get(domain, ())), None, "~other"]:
                yield entity, domain, cls


def _remote_normal(value: dict, entity_classes: dict | None) -> dict:
    """The same levels without redundant keys: a class key equal to its domain, a domain key equal to
    the default, an entity key equal to what it gets without it (for its known class, else for every
    class)."""
    out = {
        "default": value["default"],
        "domains": dict(value["domains"]),
        "entities": dict(value["entities"]),
    }
    doms = out["domains"]
    for key in [k for k in doms if ":" in k]:
        if doms[key] == doms.get(key.split(":", 1)[0], out["default"]):
            del doms[key]
    for key in [k for k in doms if ":" not in k]:
        if doms[key] == out["default"]:
            del doms[key]
    known = entity_classes or {}
    for entity in list(out["entities"]):
        level = out["entities"].pop(entity)
        domain = entity.split(".", 1)[0]
        if entity in known:
            cases = [known[entity]]
        else:
            cases = [
                None,
                "~other",
                *(k.split(":", 1)[1] for k in doms if k.startswith(domain + ":")),
            ]
        if any(_remote_at(out, None, domain, c) != level for c in cases):
            out["entities"][entity] = level
    return out


def leq(rule: dict, a, b, ctx: dict | None = None) -> bool:
    """``a`` gives at most what ``b`` gives (the partial order of the rule's ``watch_limit`` class,
    on what the values mean at runtime: concrete entities with device classes, areas with their tags
    when ``ctx`` has ``area_tags``, named entities with their class when ``ctx`` has ``entity_classes``)."""
    kind = _class(rule)
    ctx = ctx or {}
    if kind == "none":
        return True
    if kind == "ordered":
        return _index(rule, a) <= _index(rule, b)
    if kind == "areas":
        tags = ctx.get("area_tags")
        if tags is not None:
            ra, rb = _area_reach(a, tags), _area_reach(b, tags)
            return rb is None or (ra is not None and ra <= rb)
        a_all, a_ids, a_tags = _area_set(a)
        b_all, b_ids, b_tags = _area_set(b)
        return b_all or (not a_all and a_ids <= b_ids and a_tags <= b_tags)
    if kind == "schedule":
        am, bm = _hours_minutes(a), _hours_minutes(b)
        return bm is None or (am is not None and am <= bm)
    if kind == "remote_levels":
        if not isinstance(a, dict) or not isinstance(b, dict):
            return not isinstance(a, dict)
        return all(
            REMOTE_ORDER.index(_remote_at(a, *c))
            <= REMOTE_ORDER.index(_remote_at(b, *c))
            for c in _remote_cases(a, b, ctx.get("entity_classes"))
        )
    if kind == "kinds":
        if not (a or {}).get("enabled"):
            return True
        return bool((b or {}).get("enabled")) and set(a.get("kinds", [])) <= set(
            (b or {}).get("kinds", [])
        )
    raise ValueError(kind)


def looser(rule: dict, new, mine, ctx: dict | None = None) -> bool:
    """``new`` gives more than ``mine`` somewhere."""
    return not leq(rule, new, mine, ctx)


def _area_reach(value, area_tags: dict) -> set | None:
    """Area ids a value reaches with the known area tags (``None`` = all areas)."""
    if not isinstance(value, dict) or value.get("mode") == "all":
        return None
    tags = set(value.get("tags", []))
    return set(value.get("area_ids", [])) | {
        a for a, t in area_tags.items() if tags & set(t)
    }


def spring_gaps(time_zone: str | None, around: str | None) -> tuple:
    """How the clocks of the hub's zone jump forward within a year around ``around``: (weekday, first
    missing local minute, first minute after the gap). Zone not known: every jump of every IANA zone
    (fail closed until the hub knows its zone)."""
    if not around:
        return ()
    if not time_zone:
        return _any_zone_gaps(parse_ts(around).date().isoformat())
    return _spring_gaps(
        time_zone, parse_ts(around).astimezone(ZoneInfo(time_zone)).date().isoformat()
    )


@functools.lru_cache(maxsize=32)
def _spring_gaps(time_zone: str, day: str) -> tuple:
    tz = ZoneInfo(time_zone)
    start = dt.date.fromisoformat(day) - dt.timedelta(days=400)
    out = set()
    for n in range(800):
        day = start + dt.timedelta(days=n)
        first = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tz).utcoffset()
        last = dt.datetime.combine(day, dt.time(23, 59), tzinfo=tz).utcoffset()
        if last <= first:
            continue
        gap = []
        for minute in range(1440):
            local = dt.datetime.combine(
                day, dt.time(minute // 60, minute % 60), tzinfo=tz
            )
            back = local.astimezone(dt.timezone.utc).astimezone(tz)
            if (back.hour, back.minute) != (local.hour, local.minute):
                gap.append(minute)
        if gap:
            out.add((day.weekday(), gap[0], gap[-1] + 1))
    return tuple(sorted(out))


@functools.lru_cache(maxsize=4)
def _any_zone_gaps(day: str) -> tuple:
    import zoneinfo  # noqa: PLC0415

    out: set = set()
    for zone in sorted(zoneinfo.available_timezones()):
        try:
            out.update(_spring_gaps(zone, day))
        except (ValueError, OSError, zoneinfo.ZoneInfoNotFoundError):
            continue
    return tuple(sorted(out))


def _wall_spans(value: dict, gap: tuple) -> list[tuple[int, int]]:
    """Spans of ``value`` on a spring-forward night in local wall minutes from the start of that day
    (the day before negative), as the access windows behave (access_ref.spans): a local time inside the
    gap does not exist and means as much later; a span that ends before it starts is none."""
    wd, g0, g1 = gap
    out = []

    def real(day_off: int, minute: int) -> int:
        return (
            day_off * 1440
            + minute
            + (g1 - g0 if day_off == 0 and g0 <= minute < g1 else 0)
        )

    for w in value.get("windows") or []:
        s, e = (int(x[:2]) * 60 + int(x[3:]) for x in (w["start"], w["end"]))
        for day_off in (-1, 0):
            if DAYS[(wd + day_off) % 7] not in w["days"]:
                continue
            start, end = real(day_off, s), real(day_off if e > s else day_off + 1, e)
            if end > start:
                out.append((start, end))
    return out


def _gap_excess(value: dict, sides: tuple, gap: tuple) -> list[int]:
    """Local minutes after ``gap`` at which ``value`` is open while a side is closed. Only there can the
    real instants disagree with the weekly minutes (an edge inside the gap moves past its end)."""
    wd, g0, g1 = gap
    spans = [
        _wall_spans(v, gap) if isinstance(v, dict) and v.get("enabled") else None
        for v in (value, *sides)
    ]

    def is_open(sp, v):
        return sp is None or any(s <= v < e for s, e in sp)

    return [
        v
        for v in range(g1, g1 + (g1 - g0))
        if is_open(spans[0], v) and not all(is_open(sp, v) for sp in spans[1:])
    ]


def _fix_gaps(value: dict, sides: tuple, gaps: tuple) -> dict:
    """Spring forward: where the intersection would be open on the night while a side is closed (an edge
    inside the gap moves later on one side only), close just those minutes of that weekday; an end
    inside the gap moves to the gap's start. Nothing changes where every instant agrees (edges outside
    the gap, the same edges on both sides)."""
    if not gaps or not isinstance(value, dict) or not value.get("enabled"):
        return value
    for _ in range(WEEK_MINUTES):
        for gap in gaps:
            excess = _gap_excess(value, sides, gap)
            if excess:
                break
        else:
            return value
        wd, g0, g1 = gap
        base = wd * 1440
        minutes = open_minutes(value.get("windows") or [])
        before = set(minutes)
        for v in excess:
            minutes.discard((base + v) % WEEK_MINUTES)
        if minutes == before:
            # Open only through an end inside the gap: that run ends at the gap's start.
            for m in range(g0, g1):
                end = m
                while end < g1 and (base + end) % WEEK_MINUTES in before:
                    end += 1
                if (base + m) % WEEK_MINUTES in before and end < g1:
                    minutes.discard((base + m) % WEEK_MINUTES)
        if minutes == before:
            minutes.difference_update(
                (base + m) % WEEK_MINUTES for m in range(g0, g1 + (g1 - g0))
            )
        if minutes == before:
            break
        value = _hours_value(minutes)
    return {"enabled": True, "windows": []}


def _same_windows(a: dict, b: dict) -> bool:
    """Both access hours have the same windows (in any order)."""
    return sorted(
        json.dumps(w, sort_keys=True) for w in a.get("windows") or []
    ) == sorted(json.dumps(w, sort_keys=True) for w in b.get("windows") or [])


def meet(rule: dict, a, b, ctx: dict | None = None):
    """The most both give: never more than ``a`` and never more than ``b`` (rule class). ``ctx``:
    ``area_tags`` (area id -> tags, the hub's areas), ``entity_classes`` (entity id -> device class or
    null, the hub's states) and ``gaps`` (``spring_gaps`` of the hub's zone, or of every zone while it
    is not known)."""
    kind = _class(rule)
    ctx = ctx or {}
    if kind == "ordered":
        return copy.deepcopy(a if _index(rule, a) <= _index(rule, b) else b)
    if kind == "areas":
        a_all, a_ids, a_tags = _area_set(a)
        b_all, b_ids, b_tags = _area_set(b)
        if a_all and b_all:
            return {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
        if a_all:
            return copy.deepcopy(b)
        if b_all:
            return copy.deepcopy(a)
        tags = ctx.get("area_tags")
        if tags is not None:
            # The hub knows the areas' tags: the common areas themselves, pinned by id.
            common = _area_reach(a, tags) & _area_reach(b, tags)
            ordered = [i for i in a.get("area_ids", []) if i in common] + sorted(
                common - set(a.get("area_ids", []))
            )
            return {
                "mode": "selected",
                "area_ids": ordered,
                "tags": [],
                "own_area": False,
            }
        # Without the tags: common ids and common tags only (an area one side pins and the other
        # reaches by a tag is left out: never more; empty = no areas).
        return {
            "mode": "selected",
            "area_ids": [i for i in a.get("area_ids", []) if i in b_ids],
            "tags": [t for t in a.get("tags", []) if t in b_tags],
            "own_area": False,
        }
    if kind == "schedule":
        am, bm = _hours_minutes(a), _hours_minutes(b)
        if am is None:
            out = copy.deepcopy(b)
        elif bm is None or am <= bm:
            out = copy.deepcopy(a)
        elif bm <= am:
            out = copy.deepcopy(b)
        else:
            out = _hours_value(am & bm)
        if am is None or bm is None or _same_windows(a, b):
            return out  # one side always open or both the same: exactly that schedule
        return _fix_gaps(out, (a, b), ctx.get("gaps") or ())
    if kind == "remote_levels":
        domains, classes, entities = _remote_space(a, b)
        known = ctx.get("entity_classes")
        low = lambda x, y: x if REMOTE_ORDER.index(x) <= REMOTE_ORDER.index(y) else y  # noqa: E731
        out_domains = {}
        for domain in sorted(domains):
            out_domains[domain] = low(
                _remote_at(a, None, domain, None), _remote_at(b, None, domain, None)
            )
            for cls in sorted(classes.get(domain, ())):
                out_domains[f"{domain}:{cls}"] = low(
                    _remote_at(a, None, domain, cls), _remote_at(b, None, domain, cls)
                )
        return _remote_normal(
            {
                "default": low(a.get("default", "hidden"), b.get("default", "hidden")),
                "domains": out_domains,
                "entities": {
                    e: low(
                        _remote_entity(
                            a, e, classes.get(e.split(".", 1)[0], set()), known
                        ),
                        _remote_entity(
                            b, e, classes.get(e.split(".", 1)[0], set()), known
                        ),
                    )
                    for e in sorted(entities)
                },
            },
            known,
        )
    if kind == "kinds":
        if not (a or {}).get("enabled") or not (b or {}).get("enabled"):
            return {"enabled": False, "kinds": []}
        return {
            "enabled": True,
            "kinds": [k for k in a.get("kinds", []) if k in set(b.get("kinds", []))],
        }
    raise ValueError(kind)


def _same(rule: dict, a, b, ctx: dict) -> bool:
    """``a`` and ``b`` give the same at runtime (each at most the other); ``None`` only equals ``None``."""
    if a is None or b is None:
        return a is None and b is None
    return leq(rule, a, b, ctx) and leq(rule, b, a, ctx)


def _after(rule: dict, entry: dict):
    """The value of an entry once its ``expires_at`` passed (strictest when unknown: fail closed)."""
    then = entry.get("then")
    return copy.deepcopy(then if then is not None else _strictest(rule))


def _strictest(rule: dict):
    """The strictest value of a limited rule (``strictest``, else the first of ``order``)."""
    return rule["strictest"] if rule.get("strictest") is not None else rule["order"][0]


def limit_to(
    catalog: dict,
    effective: dict,
    limit: dict,
    *,
    platform: str = "watchos",
    area_tags: dict | None = None,
    entity_classes: dict | None = None,
    time_zone: str | None = None,
    now: str | None = None,
) -> dict:
    """A watch follows its phone: for every ``limited_keys`` rule the
    watch's value is the ``meet`` of its own and its phone's current value (source boundary, reason
    ``limited_by_phone`` only when that is stricter than its own somewhere). ``area_tags``: the hub's
    areas with their tags (the areas meet on the real areas); ``entity_classes``: entity id -> device
    class or null from the hub's states (an entity one side names gets the other side's level for its
    real class; without a state the lowest it can have); ``time_zone``/``now``: the hub's zone (access
    hours stay within both on the real instants of a spring-forward night; zone unknown: fail closed). Over time: with one end on either side the entry carries the earliest
    ``expires_at``; ``then`` is the meet of every later state (one side ended, both ended), so the
    value is never more than either side at any moment; what is lost is a loosening after the second
    end, which the hub sends when it happens. The watch's own profile and adjustments stay as they are.
    A phone on "Hub only" limits the watch's control_path to hub; a watch needs its own key, so it is
    checked again (platform_needs_key, ``platform`` = the watch's). Whether the watch may exist at all
    is decided on the phone (rule watch, boundary hub_only)."""
    out = dict(effective)
    by_key = {r["key"]: r for r in catalog["rules"]}
    ctx = {
        "area_tags": area_tags,
        "entity_classes": entity_classes,
        "gaps": spring_gaps(time_zone, now),
    }
    for key in limited_keys(catalog):
        rule, w, p = by_key[key], effective.get(key), limit.get(key)
        if not w or not p or "not_applicable" in (w["source"], p["source"]):
            continue
        value = meet(rule, w["value"], p["value"], ctx)
        ends = sorted({e for e in (w.get("expires_at"), p.get("expires_at")) if e})
        end, then = None, None
        if ends:
            end = ends[0]
            w2 = _after(rule, w) if w.get("expires_at") else w["value"]
            p2 = _after(rule, p) if p.get("expires_at") else p["value"]
            w1 = w2 if w.get("expires_at") == end else w["value"]
            p1 = p2 if p.get("expires_at") == end else p["value"]
            then = meet(rule, meet(rule, w1, p1, ctx), meet(rule, w2, p2, ctx), ctx)
            if json_equal(then, value) and not w.get("expires_at"):
                end, then = None, None
        if (
            _same(rule, value, w["value"], ctx)
            and end == w.get("expires_at")
            and _same(rule, then, w.get("then"), ctx)
        ):
            continue  # not stricter than the watch's own anywhere: its own entry stays
        e = _entry(
            value, "boundary", "limited_by_phone", fixed=True, expires_at=end, then=then
        )
        e["enforcement"] = w.get("enforcement")
        out[key] = e
    if (
        "remote_control" in out
        and out["remote_control"]["source"] != "not_applicable"
        and out["remote_access"]["value"] is not True
    ):
        out["remote_control"] = _entry(
            None, "not_applicable", "requires_remote_access", fixed=True
        )
    if "control_path" in out and out["control_path"]["source"] != "not_applicable":
        out["control_path"] = _platform_needs_key(platform, out["control_path"])
    for rule in catalog["rules"]:
        out[rule["key"]] = dict(out[rule["key"]])
    _mark_enforcement(catalog, out)
    return out


def strictest_limit(catalog: dict) -> dict:
    """The limit of a watch whose phone cannot be read: every limited rule at its strictest value
    (fail closed: no remote access, the checked path, nothing released)."""
    return {
        r["key"]: _entry(
            copy.deepcopy(_strictest(r)), "boundary", "limited_by_phone", fixed=True
        )
        for r in catalog["rules"]
        if r.get("watch_limit", "none") != "none"
    }


QUICK_ORDER = ("off", "status", "control")
REMOTE_TO_QUICK = {"hidden": "off", "view": "status", "control": "control"}


def remote_level(
    effective: dict, entity_id: str, device_class: str | None = None
) -> str:
    """Level of remote_control for one entity: entity > domain:device_class >
    domain > default. Without remote access nothing is reachable from away."""
    rc = effective.get("remote_control") or {}
    value = rc.get("value")
    if rc.get("source") == "not_applicable" or value is None:
        return "hidden"
    domain = entity_id.split(".", 1)[0]
    if entity_id in value["entities"]:
        return value["entities"][entity_id]
    if device_class and f"{domain}:{device_class}" in value["domains"]:
        return value["domains"][f"{domain}:{device_class}"]
    if domain in value["domains"]:
        return value["domains"][domain]
    return value["default"]


def is_safety_entity(catalog: dict, entity_id: str, device_class: str | None) -> bool:
    """Alarm panels and smoke/CO/water/gas sensors (catalog safety_entities)."""
    domain = entity_id.split(".", 1)[0]
    safety = catalog["safety_entities"]
    return domain in safety["domains"] or (
        domain == "binary_sensor"
        and device_class in safety["binary_sensor_device_classes"]
    )


def notification_shown(
    catalog: dict,
    effective: dict,
    priority: int,
    entity_id: str | None = None,
    device_class: str | None = None,
) -> bool:
    """Rule notification_level: critical notifications and notifications
    about a safety entity are never suppressed. Says nothing about delivery."""
    limits = catalog["notifications"]
    if priority >= limits["critical_min_priority"] or (
        entity_id and is_safety_entity(catalog, entity_id, device_class)
    ):
        return True
    rule = effective.get("notification_level") or {}
    level = rule.get("value") if rule.get("source") != "not_applicable" else None
    if level == "none":
        return False
    if level == "important":
        return priority >= limits["important_min_priority"]
    return True


def entity_access(
    effective: dict,
    *,
    entity_id: str,
    device_class: str | None,
    route: str,
    quick_access: str | None,
    catalog: dict,
) -> dict:
    """What the device may do with one entity on a route (local | external).

    app: control | view | hidden; quick: the strictest of the hub's
    per-entity quick access level and the remote level (hidden→off,
    view→status, control→control); None stays None.
    """
    safety = is_safety_entity(catalog, entity_id, device_class)
    level = (
        "control"
        if route == "local"
        else remote_level(effective, entity_id, device_class)
    )
    rc = effective.get("remote_control") or {}
    if (
        safety
        and route == "external"
        and level == "hidden"
        and rc.get("source") != "not_applicable"
    ):
        level = "view"  # never hidden from away
    quick = None
    if quick_access is not None:
        mapped = REMOTE_TO_QUICK[level]
        quick = QUICK_ORDER[
            min(QUICK_ORDER.index(quick_access), QUICK_ORDER.index(mapped))
        ]
    return {"app": level, "quick": quick, "always_visible": safety}


# ── temporary adjustments ─────────────────────────────────────────────────────


def expiry(preset: str, now: str, time_zone: str) -> dt.datetime:
    """End of a temporary device adjustment (hub time zone, DST aware)."""
    t_now = parse_ts(now)
    tz = ZoneInfo(time_zone)
    local = t_now.astimezone(tz)
    if preset == "1h":
        return t_now + dt.timedelta(hours=1)
    if preset == "7d":
        return t_now + dt.timedelta(days=7)
    if preset == "today":
        day = local.date() + dt.timedelta(days=1)
        return dt.datetime.combine(day, dt.time(0, 0), tz)
    if preset == "tomorrow_morning":
        day = (
            local.date()
            if local.hour < NIGHT_UNTIL_HOUR
            else local.date() + dt.timedelta(days=1)
        )
        return dt.datetime.combine(day, dt.time(MORNING_HOUR, 0), tz)
    raise ValueError(f"unknown preset {preset}")
