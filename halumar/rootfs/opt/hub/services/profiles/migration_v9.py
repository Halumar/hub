"""Schema v9: the profile model with a self check.

Existing devices and settings are carried over. The step runs in the
migration transaction with foreign keys off (the paired_clients table is
rebuilt):

1. Snapshot before: for every device the effective values of the old model,
   mapped onto rule keys. Keys the old model left to the device are marked
   ``DEVICE`` and not compared.
2. Profiles: the starter suggestions; one profile per template; one per kids
   profile (parental controls); one per kids profile + template combination
   actually used. Guest defaults go into the profile "Gast".
3. Devices: access, profile, own area NULL; a permanent adjustment
   ("Migration v9") for every rule whose old value differs from the profile.
4. Self check with the resolver: every compared key must have the same value
   and lock as before; HA key, external URL and local_only unchanged. Any
   difference raises :class:`MigrationCheckError` with the report: the
   transaction rolls back, nothing is changed.
5. Old tables, columns and settings are removed.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from services.profiles import catalog as cat
from services.profiles import migration_v13
from services.profiles import resolver as res
from services.profiles import rows

ACTOR = "Migration v9"
# Fixed log line of the self check (contracts docs/profiles.md):
# "Migration v9 self-check passed: N devices, 0 deviations, …" or
# "Migration v9 self-check failed: K deviations: …".
SELF_CHECK_PREFIX = "Migration v9 self-check"
NOTE = "Migration v9"
DEVICE = object()  # the old model let the device decide


class MigrationCheckError(RuntimeError):
    """The self check found a difference; ``report`` lists all of them."""

    def __init__(self, report: dict):
        self.report = report
        diffs = report.get("differences", [])
        lines = "; ".join(diffs[:10])
        super().__init__(
            f"{SELF_CHECK_PREFIX} failed: {len(diffs)} deviations: {lines}"
        )


# ── the old model (frozen copy of the v8 semantics) ─────────────────────────

_IOS, _TVOS = "ios", "tvos"
# Old managed keys and the platform tags that received them (managed_config.py).
_OLD_MANAGED = {
    "show_filter_button": {_IOS},
    "show_search_bar": {_IOS},
    "show_header": {_IOS, _TVOS},
    "kiosk_mode": {_IOS, _TVOS},
    "panel_night_mode": {_IOS},
    "panel_night_start": {_IOS},
    "panel_night_end": {_IOS},
    "panel_night_brightness": {_IOS},
    "panel_dim_after": {_IOS},
    "panel_screensaver_after": {_IOS, _TVOS},
    "panel_auto_home_after": {_IOS, _TVOS},
    "panel_home_area": {_IOS},
    "area_card_columns": {_IOS},
    "camera_live_preview": {_IOS},
    "power_button_enabled": {_IOS},
    "power_button_domains": {_IOS},
    "location_tracking_enabled": {_IOS},
    "location_background_enabled": {_IOS},
    "location_distance_filter": {_IOS},
    "quick_access_enabled": {_IOS},
    "quick_access_max": {_IOS},
}
_ACCESS_KEYS = (
    "settings_hidden",
    "settings_lock_enabled",
    "settings_require_biometrics",
)


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _old_platform_tag(platform: str | None) -> str | None:
    if not platform:
        return None
    return {"ios": _IOS, "ipados": _IOS, "tvos": _TVOS}.get(platform, platform)


def _managed(settings: Any, tag: str | None) -> dict[str, Any]:
    """Valid managed values of one settings dict (platform filter like v8)."""
    raw = _loads(settings) or {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in raw.items():
        tags = _OLD_MANAGED.get(key)
        if tags is None or value is None:
            continue
        if tag is not None and tag not in tags:
            continue
        out[key] = value
    return out


def _presets(settings: Any) -> dict[str, Any]:
    raw = _loads(settings) or {}
    out = {}
    if isinstance(raw, dict):
        if raw.get("card_style") in ("calm", "color"):
            out["card_style"] = raw["card_style"]
        if isinstance(raw.get("card_style_locked"), bool):
            out["card_style_locked"] = raw["card_style_locked"]
    return out


def _bool(v: Any) -> bool | None:
    return v if isinstance(v, bool) else None


def managed_to_rules(m: dict[str, Any]) -> dict[str, Any]:
    """Old managed values -> rule values (only the rules they decide)."""
    out: dict[str, Any] = {}
    sb, fb = _bool(m.get("show_search_bar")), _bool(m.get("show_filter_button"))
    if sb is not None or fb is not None:
        out["search_filter"] = all(v for v in (sb, fb) if v is not None)
    if _bool(m.get("show_header")) is not None:
        out["header"] = m["show_header"]
    if _bool(m.get("kiosk_mode")) is not None:
        out["kiosk"] = m["kiosk_mode"]
    for old, new in (
        ("panel_dim_after", "panel_dim"),
        ("panel_screensaver_after", "panel_screensaver"),
        ("panel_auto_home_after", "panel_auto_home"),
        ("area_card_columns", "columns"),
    ):
        v = m.get(old)
        if isinstance(v, int) and not isinstance(v, bool):
            out[new] = v
    if isinstance(m.get("panel_home_area"), str) and m["panel_home_area"].strip():
        out["panel_start"] = {"mode": "area", "area_id": m["panel_home_area"].strip()}
    night_keys = (
        "panel_night_mode",
        "panel_night_start",
        "panel_night_end",
        "panel_night_brightness",
    )
    if any(k in m for k in night_keys):
        night = copy.deepcopy(cat.rule("panel_night")["default"])
        for old, field in zip(night_keys, ("mode", "start", "end", "brightness")):
            if old in m:
                night[field] = m[old]
        out["panel_night"] = night
    if _bool(m.get("camera_live_preview")) is not None:
        out["camera_live"] = m["camera_live_preview"]
    pe, pd = _bool(m.get("power_button_enabled")), m.get("power_button_domains")
    if pe is not None or isinstance(pd, list):
        default = cat.rule("power_button")["default"]
        allowed = set(
            json.loads((cat.DATA_DIR / "schemas" / "defs.json").read_text())["$defs"][
                "power_button_domain"
            ]["enum"]
        )
        out["power_button"] = {
            "enabled": pe if pe is not None else default["enabled"],
            "domains": [d for d in dict.fromkeys(pd) if d in allowed]
            if isinstance(pd, list)
            else list(default["domains"]),
        }
    lt, lb = (
        _bool(m.get("location_tracking_enabled")),
        _bool(m.get("location_background_enabled")),
    )
    if lt is not None or lb is not None:
        out["location"] = (
            "off" if lt is False else ("foreground" if lb is False else "background")
        )
    qe, qm = _bool(m.get("quick_access_enabled")), m.get("quick_access_max")
    if qe is not None or qm in ("status", "control"):
        out["quick_access"] = (
            "off" if qe is False else (qm if qm in ("status", "control") else "control")
        )
    return out


def _areas_from_tags(tags: list[str], guest: bool) -> dict:
    tags = [t for t in tags if isinstance(t, str)]
    if "*" in tags or (not tags and not guest):
        return {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
    pins = [t[5:] for t in tags if t.startswith("area:")]
    plain = [t for t in tags if not t.startswith("area:")]
    return {"mode": "selected", "area_ids": pins, "tags": plain, "own_area": False}


def _kids_rules(rules: dict) -> dict[str, Any]:
    if rules.get("blocked"):
        raise ValueError("kids profile blocks everything (profile level 'blocked')")
    return {
        "kids_blocked_types": list(rules.get("blocked_types", [])),
        "kids_quiet_hours": list(rules.get("quiet_hours", [])),
        "kids_rate_limit": rules.get("rate_limit"),
        "kids_volume_max": rules.get("volume_max"),
        "kids_color_lock": bool(rules.get("color_lock", False)),
    }


def before_snapshot(
    client: dict, template: dict | None, kids: dict | None, settings: dict[str, str]
) -> dict[str, tuple[Any, bool] | object]:
    """Effective old values of one device: key -> (value, locked) or DEVICE."""
    guest = bool(client.get("is_guest"))
    child = bool(client.get("profile_id")) and kids is not None
    tag = _old_platform_tag(client.get("platform"))
    tsettings = (template or {}).get("settings_json")
    managed = {
        **_managed(tsettings, tag),
        **_managed(client.get("app_config_overrides"), tag),
    }
    snap: dict[str, Any] = {key: DEVICE for key in cat.rules_by_key()}
    for key, value in managed_to_rules(managed).items():
        snap[key] = (value, True)
    # quick access: hub-decided even without a managed value (v8 default control).
    if "quick_access" not in managed_to_rules(managed):
        snap["quick_access"] = ("control", True)
    if guest:
        snap["quick_access"] = ("off", True)
    # card style preset: device over template, per key.
    tp, dp = _presets(tsettings), _presets(client.get("app_config_overrides"))
    style = dp.get("card_style", tp.get("card_style"))
    if style is not None:
        locked = dp.get("card_style_locked", tp.get("card_style_locked", False))
        snap["card_style"] = (style, bool(locked))
    # stacked notifications: global switch, else the device column.
    if settings.get("notification_stacking") == "false":
        snap["notification_stack"] = ("off", True)
    elif client.get("notification_stacked_style") in ("banner", "carousel"):
        snap["notification_stack"] = (client["notification_stacked_style"], True)
    tacc = _loads(tsettings) or {}
    tacc = tacc if isinstance(tacc, dict) else {}
    hidden = bool(client.get("settings_hidden")) or tacc.get("settings_hidden") is True
    pin = bool(client.get("settings_pin_hash") and client.get("settings_pin_salt"))
    snap["app_settings"] = ("hidden" if hidden else ("pin" if pin else "open"), True)
    snap["app_settings_biometrics"] = (
        bool(client.get("settings_require_biometrics"))
        or tacc.get("settings_require_biometrics") is True,
        True,
    )
    snap["remote_access"] = (bool(client.get("remote_allowed")) and not guest, True)
    # v13: the former rights are the rule setup_devices; carried only
    # when there is something to carry (off is the default of every kind).
    setup = migration_v13.setup_value(
        (
            bool(client.get("can_add_devices"))
            or settings.get("members_can_add_devices") == "true"
        )
        and not guest
        and not child,
        bool(client.get("can_invite_guests")) and not guest and not child,
    )
    if setup["enabled"]:
        snap["setup_devices"] = (setup, True)
    snap["areas"] = (_areas_from_tags(_loads(client.get("tags")) or [], guest), True)
    if child:
        for key, value in _kids_rules(_loads(kids["rules"]) or {}).items():
            snap[key] = (value, True)
        snap["kids_exceptions"] = (kids_exceptions(kids), True)
    return snap


def kids_exceptions(kids: dict) -> list[dict]:
    return [
        {
            "target": {"area_id" if r["scope"] == "area" else "entity_id": r["target"]},
            "rules": _loads(r["rules"]) or {},
        }
        for r in sorted(
            kids.get("targets", []), key=lambda r: (r["scope"], r["target"])
        )
    ]


# Rules the new model changes on purpose.
NEW_RULES = {"control_mode_max", "watch", "remote_control", "notification_level"}


def compared_keys(snap: dict, platform: str, child: bool) -> list[str]:
    out = []
    for key, value in snap.items():
        if value is DEVICE or key in NEW_RULES:
            continue
        if platform not in cat.rule(key)["platforms"]:
            continue
        if cat.rule(key)["requires_child_protection"] and not child:
            continue
        out.append(key)
    return out


def boundary_tightened(key: str, before, after: dict) -> bool:
    """A boundary of the new model (guest access, parental controls)
    decides the rule and is stricter than the old value: a change on purpose
    (e.g. guests no longer see the settings). Boundaries never loosen."""
    # A prerequisite (settings with PIN, but no PIN) only follows from the
    # parental-controls cap here: the old model delivered pin only with a PIN.
    if after["source"] not in ("boundary", "prerequisite"):
        return False
    i, j = cat.order_index(key, before), cat.order_index(key, after["value"])
    return i is not None and j is not None and j <= i


# ── profiles of the migration ───────────────────────────────────────────────


def _member_body(base_id: str, members_add: bool) -> dict:
    body = cat.materialize(base_id)
    body["fixed"] = sorted(
        cat.always_fixed() | {k for k in body["fixed"] if k.startswith("kids_")}
    )
    if members_add:
        body["rules"]["setup_devices"] = migration_v13.setup_value(True, False)
    return body


def _apply_template(body: dict, template: dict) -> dict:
    """Template values into a body: managed -> value (not the user's choice),
    preset -> user's choice with starting value, preset with lock -> value."""
    body = copy.deepcopy(body)
    tsettings = template.get("settings_json")
    managed = _managed(tsettings, None)
    uc = set(body["user_choice"])
    for key, value in managed_to_rules(managed).items():
        body["rules"][key] = value
        uc.discard(key)
    presets = _presets(tsettings)
    if "card_style" in presets:
        body["rules"]["card_style"] = presets["card_style"]
        if presets.get("card_style_locked"):
            uc.discard("card_style")
        else:
            uc.add("card_style")
    acc = _loads(tsettings) or {}
    acc = acc if isinstance(acc, dict) else {}
    if acc.get("settings_hidden") is True:
        body["rules"]["app_settings"] = "hidden"
    elif acc.get("settings_lock_enabled") is True:
        body["rules"]["app_settings"] = "pin"
    if acc.get("settings_require_biometrics") is True:
        body["rules"]["app_settings_biometrics"] = True
    body["user_choice"] = sorted(uc & cat.user_choice_allowed())
    return body


def _apply_kids(body: dict, kids: dict) -> dict:
    body = copy.deepcopy(body)
    body["child_protection"] = True
    body["rules"].update(_kids_rules(_loads(kids["rules"]) or {}))
    body["rules"]["kids_exceptions"] = kids_exceptions(kids)
    return body


def _unique_name(name: str, taken: set[str]) -> str:
    limit = cat.name_max_length()
    candidate = name[:limit]
    n = 2
    while candidate.casefold() in taken:
        suffix = f" ({n})"
        candidate = name[: limit - len(suffix)] + suffix
        n += 1
    taken.add(candidate.casefold())
    return candidate


async def _fetchall(conn: aiosqlite.Connection, sql: str, params=()) -> list[dict]:
    async with conn.execute(sql, params) as cur:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in await cur.fetchall()]


NEW_PAIRED_CLIENTS_COLUMNS = (
    "id, name, secret_hash, ha_token_encrypted, is_active, created_at, last_seen, "
    "device_name, app_version, app_build, platform, device_model, protocol_version, "
    "battery_level, connection_type, current_area_id, latitude, longitude, access, "
    "expires_at, ha_user_id, ha_refresh_token_id, ha_token_minted_at, ha_token_status, "
    "ha_token_pending_since, owner_label, set_up_by_client_id, set_up_by_name, "
    "set_up_method, "
    "pending_deletion_at, pending_hidden_at, ha_user_released_at, settings_pin_hash, "
    "settings_pin_salt, profile_id, own_area_id, kids_override_until"
)
OLD_SETTINGS = (
    "members_can_add_devices",
    "guest_invite_defaults",
    "guest_tags_predefined",
    "notification_stacking",
    "notification_stacked_style",
    "notification_show_time",
)


async def run(conn: aiosqlite.Connection, *, now: str | None = None) -> dict:
    """Carry a v8 database over to v9 (inside the caller's transaction)."""
    from services.db_migrations import (
        PAIRED_CLIENTS_SQL,
        PROFILE_INDEXES_SQL,
        PROFILE_TABLES_SQL,
        PROFILE_TRIGGERS_SQL,
        split_sql,
    )

    now = now or datetime.now(timezone.utc).isoformat()
    settings = {
        r["key"]: r["value"]
        for r in await _fetchall(conn, "SELECT key, value FROM settings")
    }
    templates = {
        r["id"]: r for r in await _fetchall(conn, "SELECT * FROM config_templates")
    }
    kids = {
        r["id"]: dict(r, targets=[])
        for r in await _fetchall(conn, "SELECT * FROM kids_profiles")
    }
    for r in await _fetchall(conn, "SELECT * FROM kids_profile_rules"):
        if r["profile_id"] in kids:
            kids[r["profile_id"]]["targets"].append(r)
    clients = await _fetchall(
        conn, "SELECT * FROM paired_clients ORDER BY created_at, id"
    )
    report: dict[str, Any] = {
        "profiles": [],
        "devices": [],
        "differences": [],
        "boundary_changes": [],
    }

    for stmt in split_sql(PROFILE_TABLES_SQL):
        await conn.execute(stmt)

    # 1. before
    snaps = {}
    for c in clients:
        try:
            snaps[c["id"]] = before_snapshot(
                c,
                templates.get(c.get("config_template_id")),
                kids.get(c.get("profile_id")),
                settings,
            )
        except ValueError as e:
            report["differences"].append(f"{c['id']}: {e}")
    if report["differences"]:
        raise MigrationCheckError(report)

    # 2. profiles
    members_add = settings.get("members_can_add_devices") == "true"
    taken: set[str] = set()
    for sug in cat.suggestions()["suggestions"]:
        if sug.get("starter"):
            _unique_name(rows.suggestion_name(sug), taken)
    starter_ids = await rows.create_starters(conn, now=now, actor=ACTOR)
    adult_body = _member_body("adult", members_add)
    await _replace_body(conn, starter_ids["adult"], adult_body)
    bodies: dict[str, dict] = {starter_ids["adult"]: adult_body}
    for sid in starter_ids.values():
        if sid not in bodies:
            bodies[sid] = await _body_of(conn, sid)

    guest_hours = cat.guest_access()["default_hours"]
    gdefaults = _loads(settings.get("guest_invite_defaults")) or {}
    guest_body = _member_body("guest", False)
    if isinstance(gdefaults, dict):
        if gdefaults.get("all_areas") is True:
            guest_body["rules"]["areas"] = {
                "mode": "all",
                "area_ids": [],
                "tags": [],
                "own_area": False,
            }
        elif gdefaults.get("area_ids"):
            guest_body["rules"]["areas"] = {
                "mode": "selected",
                "area_ids": list(gdefaults["area_ids"]),
                "tags": [],
                "own_area": True,
            }
        if gdefaults.get("template_id") in templates:
            guest_body = _apply_template(
                guest_body, templates[gdefaults["template_id"]]
            )
        hours = gdefaults.get("expires_hours")
        if isinstance(hours, int) and not isinstance(hours, bool):
            guest_hours = max(1, min(hours, cat.guest_access()["max_hours"]))
    await _replace_body(conn, starter_ids["guest"], guest_body)
    bodies[starter_ids["guest"]] = guest_body
    await rows.set_setting(conn, rows.SETTING_GUEST_HOURS, str(guest_hours))

    template_profiles = {}
    for tid, tpl in templates.items():
        body = _apply_template(_member_body("adult", members_add), tpl)
        pid = await rows.insert_profile(
            conn,
            name=_unique_name(tpl["name"] or "Vorlage", taken),
            body=body,
            now=now,
            actor=ACTOR,
            description=tpl.get("description") or None,
        )
        template_profiles[tid], bodies[pid] = pid, body
    kids_profiles = {}
    for kid, k in kids.items():
        body = _apply_kids(_member_body("child", False), k)
        pid = await rows.insert_profile(
            conn,
            name=_unique_name(k["name"], taken),
            body=body,
            now=now,
            actor=ACTOR,
        )
        kids_profiles[kid], bodies[pid] = pid, body
    combos = {}
    for c in clients:
        kid, tid = c.get("profile_id"), c.get("config_template_id")
        if kid in kids and tid in templates and (kid, tid) not in combos:
            body = _apply_kids(bodies[template_profiles[tid]], kids[kid])
            name = f"{kids[kid]['name']} + {templates[tid]['name']}"
            pid = await rows.insert_profile(
                conn,
                name=_unique_name(name, taken),
                body=body,
                now=now,
                actor=ACTOR,
            )
            combos[(kid, tid)], bodies[pid] = pid, body
    child_flags = {pid: bool(b.get("child_protection")) for pid, b in bodies.items()}

    # 3. devices
    assignments: dict[str, tuple[str, list[dict]]] = {}
    for c in clients:
        guest = bool(c.get("is_guest"))
        kid, tid = c.get("profile_id"), c.get("config_template_id")
        if guest:
            pid = template_profiles.get(tid, starter_ids["guest"])
        elif kid in kids:
            pid = combos.get((kid, tid), kids_profiles[kid])
        else:
            pid = template_profiles.get(tid, starter_ids["adult"])
        body = bodies[pid]
        platform = cat.device_platform(c.get("platform"))
        access = {
            "kind": "guest" if guest else "member",
            "expires_at": c.get("expires_at") if guest else None,
        }
        if guest and not access["expires_at"]:
            report["differences"].append(f"{c['id']}: guest without expiry")
            continue
        device = {
            "own_area_id": None,
            "has_remote_pin": bool(
                c.get("settings_pin_hash") and c.get("settings_pin_salt")
            ),
        }
        snap = snaps[c["id"]]
        keys = compared_keys(snap, platform, child_flags[pid])
        profile_only = res.resolve(
            cat.catalog(),
            platform=platform,
            access=_unexpired(access),
            profile=body,
            overrides=[],
            device=device,
            now=now,
        )
        overs = []
        for key in keys:
            value, locked = snap[key]
            got = profile_only[key]
            if res.json_equal(got["value"], value) and got["locked"] == locked:
                continue
            if boundary_tightened(key, value, got):
                continue  # an adjustment could not loosen a boundary anyway
            if key in body["fixed"]:
                report["differences"].append(
                    f"{c['id']}/{key}: fixed in the profile, cannot adjust"
                )
                continue
            overs.append(
                {
                    "rule": key,
                    "value": copy.deepcopy(value),
                    "user_choice": not locked,
                    "expires_at": None,
                }
            )
        after = res.resolve(
            cat.catalog(),
            platform=platform,
            access=_unexpired(access),
            profile=body,
            overrides=overs,
            device=device,
            now=now,
        )
        # 4. self check
        for key in keys:
            value, locked = snap[key]
            if not (
                res.json_equal(after[key]["value"], value)
                and after[key]["locked"] == locked
            ):
                if boundary_tightened(key, value, after[key]):
                    report["boundary_changes"].append(
                        f"{c['id']}/{key}: {value!r} -> {after[key]['value']!r} "
                        f"({after[key]['reason']})"
                    )
                    continue
                report["differences"].append(
                    f"{c['id']}/{key}: before {value!r} (locked {locked}), after "
                    f"{after[key]['value']!r} (locked {after[key]['locked']})"
                )
        child_before = c.get("profile_id") in kids
        if child_flags[pid] != child_before:
            report["differences"].append(f"{c['id']}: HA key would change")
        remote_before = bool(c.get("remote_allowed")) and not guest
        if (
            after["remote_access"]["value"] is not None
            and bool(after["remote_access"]["value"]) != remote_before
        ):
            report["differences"].append(
                f"{c['id']}: remote access / local_only would change"
            )
        assignments[c["id"]] = (pid, overs)
        report["devices"].append(
            {
                "client_id": c["id"],
                "name": c["name"],
                "profile_id": pid,
                "overrides": sorted(o["rule"] for o in overs),
                "compared": len(keys),
            }
        )
    if report["differences"]:
        raise MigrationCheckError(report)

    # 5. rebuild paired_clients, drop the old model
    new_sql = PAIRED_CLIENTS_SQL.replace(
        "CREATE TABLE IF NOT EXISTS paired_clients (",
        "CREATE TABLE paired_clients_v9 (",
        1,
    )
    await conn.execute(new_sql)
    for c in clients:
        pid, _ = assignments[c["id"]]
        guest = bool(c.get("is_guest"))
        values = dict(c)
        values.update(
            access="guest" if guest else "member",
            expires_at=c.get("expires_at") if guest else None,
            profile_id=pid,
            own_area_id=None,
            set_up_by_client_id=c.get("invited_by_client_id"),
            set_up_by_name=c.get("invited_by_name"),
            set_up_method="invite" if c.get("invited_by_client_id") else None,
        )
        cols = [x.strip() for x in NEW_PAIRED_CLIENTS_COLUMNS.split(",")]
        await conn.execute(
            f"INSERT INTO paired_clients_v9 ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(values.get(col) for col in cols),
        )
    await conn.execute("DROP TABLE paired_clients")
    await conn.execute("ALTER TABLE paired_clients_v9 RENAME TO paired_clients")
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_paired_clients_pending "
        "ON paired_clients(ha_token_status) WHERE ha_token_status = 'pending'",
        "CREATE INDEX IF NOT EXISTS idx_paired_clients_secret_hash ON paired_clients(secret_hash)",
        "CREATE INDEX IF NOT EXISTS idx_paired_clients_active ON paired_clients(is_active)",
    ):
        await conn.execute(stmt)
    for stmt in split_sql(PROFILE_INDEXES_SQL) + split_sql(PROFILE_TRIGGERS_SQL):
        await conn.execute(stmt)
    for cid, (pid, overs) in assignments.items():
        for o in overs:
            await conn.execute(
                "INSERT INTO device_overrides (client_id, rule_key, value_json, user_choice, "
                "expires_at, note, created_by, created_at) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    cid,
                    o["rule"],
                    rows.dumps(o["value"]),
                    1 if o["user_choice"] else 0,
                    NOTE,
                    ACTOR,
                    now,
                ),
            )
    await conn.execute("DROP TABLE IF EXISTS kids_profile_rules")
    await conn.execute("DROP TABLE IF EXISTS kids_profiles")
    await conn.execute("DROP INDEX IF EXISTS idx_config_templates_platform")
    await conn.execute("DROP TABLE IF EXISTS config_templates")
    for key in OLD_SETTINGS:
        await conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    async with conn.execute("PRAGMA foreign_key_check") as cur:
        problems = await cur.fetchall()
    if problems:
        report["differences"].append(
            f"foreign key check: {[tuple(p) for p in problems][:5]}"
        )
        raise MigrationCheckError(report)
    async with conn.execute("SELECT id, name FROM profiles ORDER BY name") as cur:
        report["profiles"] = [{"id": r[0], "name": r[1]} for r in await cur.fetchall()]
    await rows.audit(
        conn,
        "migration_v9",
        now=now,
        actor=ACTOR,
        after={
            "profiles": len(report["profiles"]),
            "devices": len(report["devices"]),
            "overrides": sum(len(d["overrides"]) for d in report["devices"]),
        },
    )
    return report


def _unexpired(access: dict) -> dict:
    """Guests are compared as if not expired (an expired guest is removed by the hub)."""
    if access["kind"] == "guest":
        return {"kind": "guest", "expires_at": "9999-12-31T00:00:00Z"}
    return access


async def _body_of(conn: aiosqlite.Connection, profile_id: str) -> dict:
    async with conn.execute(
        "SELECT body_json, child_protection FROM profiles WHERE id = ?", (profile_id,)
    ) as cur:
        row = await cur.fetchone()
    body = json.loads(row[0])
    body["child_protection"] = bool(row[1])
    return body


async def _replace_body(
    conn: aiosqlite.Connection, profile_id: str, body: dict
) -> None:
    stored = rows.body_json(body)
    await conn.execute(
        "UPDATE profiles SET body_json = ? WHERE id = ?", (stored, profile_id)
    )
    await conn.execute(
        "UPDATE profile_revisions SET body_json = ? WHERE profile_id = ? AND rev = 1",
        (stored, profile_id),
    )
