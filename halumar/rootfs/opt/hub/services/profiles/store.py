"""Profiles, device adjustments and effective rules on the hub database.

Reads build the resolver inputs (``device_inputs``) and the admin views; writes
are plain database functions that run inside the caller's transaction (see
``services.profiles.changes`` for preview, audit and side effects).
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from models.database import Database
from services.profiles import catalog as cat
from services.profiles import resolver as res
from services.profiles import rows
from services.profiles.catalog import ProfileError

PROFILE_COLUMNS = (
    "id, name, description, icon, color, kind, child_protection, body_json, "
    "origin_suggestion, origin_version, remote_pin_hash, remote_pin_salt, "
    "remote_pin_fixed, rev, created_at, updated_at, updated_by"
)
UNSET = object()

CLIENT_COLUMNS = (
    "id, name, is_active, pending_deletion_at, platform, access, expires_at, "
    "profile_id, own_area_id, settings_pin_hash, settings_pin_salt, "
    "kids_override_until, set_up_by_client_id, set_up_by_name, set_up_method, "
    "owner_label, access_switch, access_state, access_early_json, seal_key"
)

# A child's watch (contract protocol.md "A child's watch"):
# a watch whose phone (set_up_by) has parental controls resolves with the
# phone's profile, adjustments and these fields of the phone's row, and shares
# its kids state (counters, pause everything, releases, early access, kids
# override: ``kids_subject``).
WATCH_PLATFORMS = frozenset({"watchos", "wearos"})
CHILD_WATCH_SHARED = (
    "profile_id",
    "access",
    "expires_at",
    "own_area_id",
    "settings_pin_hash",
    "settings_pin_salt",
    "kids_override_until",
    "access_switch",
    "access_state",
    "access_early_json",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return res.fmt_ts(utcnow().replace(microsecond=0))


def body_of(row: dict) -> dict:
    """Resolver profile of a ``profiles`` row (stored body + flags).

    Comfort rules the stored body does not hold yet (new after an update)
    are filled in as the user's choice (``resolver.fill_missing``), so a new
    rule is never locked on the devices before the admin decides.
    """
    body = cat.migrate_body(row["body_json"])
    kind = row.get("kind") or "person"
    body = res.fill_missing(
        cat.catalog(),
        cat.suggestions()["suggestions"],
        {**body, "kind": kind},
        row.get("origin_suggestion"),
    )
    return {
        "kind": kind,
        "child_protection": bool(row["child_protection"]),
        "rules": body["rules"],
        "fixed": body["fixed"],
        "user_choice": body["user_choice"],
        "filled": body["filled"],
        "has_remote_pin": bool(
            row.get("remote_pin_hash") and row.get("remote_pin_salt")
        ),
        "remote_pin_fixed": bool(row.get("remote_pin_fixed")),
    }


def device_has_pin(client: dict) -> bool:
    return bool(client.get("settings_pin_hash") and client.get("settings_pin_salt"))


def effective_pin(
    client: dict, profile_row: dict | None
) -> tuple[str, str, str] | None:
    """(hash, salt, source) of the PIN that unlocks the settings.

    A device PIN wins unless the profile PIN is fixed.
    """
    prof = profile_row or {}
    has_profile = bool(prof.get("remote_pin_hash") and prof.get("remote_pin_salt"))
    if has_profile and (prof.get("remote_pin_fixed") or not device_has_pin(client)):
        return prof["remote_pin_hash"], prof["remote_pin_salt"], "profile"
    if device_has_pin(client):
        return client["settings_pin_hash"], client["settings_pin_salt"], "device"
    return None


def access_of(client: dict) -> dict:
    guest = client.get("access") == "guest"
    return {
        "kind": "guest" if guest else "member",
        "expires_at": _ts(client.get("expires_at")) if guest else None,
    }


def _ts(value: str | None) -> str | None:
    if not value:
        return None
    t = res.parse_ts(value)
    return res.fmt_ts(t) if t else None


def override_input(row: dict) -> dict:
    return {
        "rule": row["rule_key"],
        "value": json.loads(row["value_json"]),
        "user_choice": bool(row["user_choice"]),
        "expires_at": _ts(row.get("expires_at")),
    }


def device_inputs(
    client: dict, profile_row: dict, overrides: list[dict], now: str | None = None
) -> dict[str, Any]:
    """Keyword arguments of :func:`resolver.resolve` for one device."""
    return {
        "platform": cat.device_platform(client.get("platform")),
        "access": access_of(client),
        "profile": body_of(profile_row),
        "overrides": [override_input(o) for o in overrides],
        "device": {
            "own_area_id": client.get("own_area_id"),
            "has_remote_pin": device_has_pin(client),
        },
        "now": now or now_iso(),
    }


def resolve_inputs(inputs: dict[str, Any]) -> dict[str, dict]:
    return res.resolve(cat.catalog(), **inputs)


def carrier_of(client: dict) -> str | None:
    """The phone that carries a watch's access (``set_up_by`` of a watch
    paired through its phone), None for every other device."""
    phone = client.get("set_up_by_client_id")
    if (
        client.get("platform") in WATCH_PLATFORMS
        and client.get("set_up_method") == "onboard"
        and client.get("seal_key")
        and phone
        and phone != client.get("id")
    ):
        return phone
    return None


def as_child_watch(watch: dict, phone: dict) -> dict:
    """The row a child's watch resolves with: its own identity, the phone's
    profile, access and kids fields (:data:`CHILD_WATCH_SHARED`)."""
    merged = {**watch, **{k: phone.get(k) for k in CHILD_WATCH_SHARED}}
    merged["own_profile_id"] = watch.get("profile_id")
    merged["kids_subject"] = phone["id"]
    return merged


def kids_subject(client: dict) -> str:
    """Whose kids state a device uses: a child's watch uses its phone's."""
    return client.get("kids_subject") or client["id"]


def is_child_watch(client: dict) -> bool:
    return bool(client.get("kids_subject"))


async def phone_of_child_watch(db: Database, row: dict) -> dict | None:
    """The phone of a child's watch (its row with ``profile_name``) when
    ``row`` (with platform, set_up_by_client_id, set_up_method, seal_key) is
    one, else None. Cheap: one query only for watches."""
    phone_id = carrier_of(row)
    if not phone_id:
        return None
    return await db.fetchone(
        "SELECT c.id, c.name, c.owner_label, c.profile_id, p.name AS profile_name "
        "FROM paired_clients c JOIN profiles p ON p.id = c.profile_id "
        "WHERE c.id = ? AND p.child_protection = 1",
        (phone_id,),
    )


async def area_tags(db: Database) -> dict[str, list[str]]:
    """Area id -> tags of every area (the areas meet of a watch on the real areas)."""
    out: dict[str, list[str]] = {
        r["id"]: [] for r in await db.fetchall("SELECT id FROM areas")
    }
    for r in await db.fetchall("SELECT area_id, tag FROM area_tags"):
        out.setdefault(r["area_id"], []).append(r["tag"])
    return out


# HA client of the running app: its live state mirror gives the device classes
# a watch's remote levels are limited with (``register_states``).
_states_client = None


def register_states(ha_client) -> None:
    """The HA client whose live states give ``entity_classes`` (startup)."""
    global _states_client
    _states_client = ha_client


def entity_classes() -> dict[str, str | None] | None:
    """Entity id -> device class (None = a state without one) of every state HA
    has right now; None without a complete live mirror (every entity unknown:
    the lowest level it can have). Read on every resolve, so a new entity or a
    changed class counts at the next one."""
    client = _states_client
    if client is None or not getattr(client, "mirror_ready", False):
        return None
    return classes_of(client.ws.all_states())


def classes_of(states) -> dict[str, str | None]:
    """Entity id -> device class of HA states (a list or entity id -> state;
    an empty or non-text class is none, as the rule snapshot reads it)."""
    out: dict[str, str | None] = {}
    for state in (states.values() if isinstance(states, dict) else states) or []:
        entity_id = state.get("entity_id")
        cls = (state.get("attributes") or {}).get("device_class")
        if isinstance(entity_id, str):
            out[entity_id] = cls if isinstance(cls, str) and cls else None
    return out


def _hub_zone() -> str | None:
    from services import access

    return access.time_zone()


async def follow_phone(
    db: Database, client: dict, now: str | None = None
) -> tuple[dict, dict | None, list[dict], dict | None, dict | None]:
    """(row, profile row, adjustments, limit, area tags) a device resolves with.

    Its own, or for a watch whose phone has parental controls the phone's
    (:func:`as_child_watch`): one rule set and one kids state for both. An
    adult's watch keeps its own and gets as ``limit`` its phone's effective
    rules: it never has more than its phone (:func:`resolve_device`).
    """
    phone_id = carrier_of(client)
    limit = None
    if phone_id:
        phone = await client_row(db, phone_id)
        phone_prof = await profile_row(db, phone["profile_id"]) if phone else None
        if phone_prof and phone_prof["child_protection"]:
            return (
                as_child_watch(client, phone),
                phone_prof,
                await overrides_of(db, phone_id),
                None,
                None,
            )
        if phone_prof and _phone_usable(phone):
            limit = resolve_inputs(
                device_inputs(phone, phone_prof, await overrides_of(db, phone_id), now)
            )
            _unreadable_logged.discard(client.get("id"))
        elif not (phone_prof and phone_prof["child_protection"]):
            limit = _unreadable_phone(client, phone_id)
    prof = await profile_row(db, client["profile_id"])
    tags = await area_tags(db) if limit is not None else None
    return client, prof, await overrides_of(db, client["id"]), limit, tags


async def phone_rules(
    db: Database, watch: dict, now: str | None = None
) -> dict[str, dict] | None:
    """Effective rules of the phone that carries a watch's access, None when
    the device is not such a watch or its phone cannot be read (a watch then
    holds no key: ``resolver.ha_access``)."""
    phone_id = carrier_of(watch)
    if not phone_id:
        return None
    phone = await client_row(db, phone_id)
    if not _phone_usable(phone):
        return None
    phone_prof = await profile_row(db, phone["profile_id"])
    if phone_prof is None:
        return None
    return resolve_inputs(
        device_inputs(phone, phone_prof, await overrides_of(db, phone_id), now)
    )


def is_hub_remote(effective: dict[str, dict]) -> bool:
    """Hub only with remote access: the device holds the read-only key
    because of remote access (a TV or a watch resolves to checked)."""
    return (effective.get("control_path") or {}).get("value") == "hub" and (
        effective.get("remote_access") or {}
    ).get("value") is True


def hub_remote_schedule(
    client: dict, prof: dict, overrides: list[dict], now: str | None = None
) -> tuple[bool, str | None]:
    """``(hub_remote_now, hub_remote_at)`` of one device.

    ``hub_remote_now``: :func:`is_hub_remote` right now. ``hub_remote_at``:
    the earliest later end (of a device adjustment, of the guest access) after
    which the device is on Hub only with remote access while it is not now;
    None if no end leads there. Every end is evaluated one second after it,
    in time order, with the resolver."""
    now = now or now_iso()
    t_now = res.parse_ts(now)
    inputs = device_inputs(client, prof, overrides, now)
    current = is_hub_remote(resolve_inputs(inputs))
    if current:
        return True, None
    ends = set()
    for o in inputs["overrides"]:
        if o.get("expires_at"):
            ends.add(o["expires_at"])
    if inputs["access"].get("expires_at"):
        ends.add(inputs["access"]["expires_at"])
    for end in sorted(ends, key=res.parse_ts):
        t_end = res.parse_ts(end)
        if t_end <= t_now:
            continue
        later = res.fmt_ts(t_end + timedelta(seconds=1))
        if is_hub_remote(resolve_inputs({**inputs, "now": later})):
            return False, res.fmt_ts(t_end)
    return False, None


def _phone_usable(phone: dict | None) -> bool:
    return (
        bool(phone)
        and bool(phone.get("is_active"))
        and not phone.get("pending_deletion_at")
    )


# Watches whose unreadable phone was logged (once per watch until it reads again).
_unreadable_logged: set[str] = set()


def _unreadable_phone(watch: dict, phone_id: str) -> dict:
    """Fail closed: a watch whose phone row or profile cannot be read (or is
    no longer active) resolves with every limited rule at its strictest
    value - no remote access, the strictest control path (checked on a
    watch), nothing released. Logged once
    per watch until its phone can be read again."""
    import logging

    if watch.get("id") not in _unreadable_logged:
        _unreadable_logged.add(watch.get("id"))
        logging.getLogger("hub.profiles").warning(
            "Watch %s: its phone %s cannot be read; limited to the strictest rules",
            watch.get("id"),
            phone_id,
        )
    return res.strictest_limit(cat.catalog())


def resolve_device(
    client: dict,
    prof: dict,
    overrides: list[dict],
    limit: dict | None,
    now: str | None = None,
    tags: dict | None = None,
    classes: dict | None = None,
) -> dict[str, dict]:
    """Effective rules of a device; an adult's watch limited to its phone's
    (``resolver.limit_to``, reason ``limited_by_phone``): areas on the real
    areas (``tags``), remote levels with HA's device classes, access hours in
    the hub's time zone."""
    now = now or now_iso()
    inputs = device_inputs(client, prof, overrides, now)
    rules = resolve_inputs(inputs)
    if limit is not None:
        rules = res.limit_to(
            cat.catalog(),
            rules,
            limit,
            platform=inputs["platform"],
            area_tags=tags,
            entity_classes=classes if classes is not None else entity_classes(),
            time_zone=_hub_zone(),
            now=now,
        )
    return rules


# ── reads ────────────────────────────────────────────────────────────────────


async def profile_row(db: Database, profile_id: str) -> dict | None:
    return await db.fetchone(
        f"SELECT {PROFILE_COLUMNS} FROM profiles WHERE id = ?", (profile_id,)
    )


async def profile_rows(db: Database) -> list[dict]:
    return await db.fetchall(
        f"SELECT {PROFILE_COLUMNS} FROM profiles ORDER BY name COLLATE NOCASE"
    )


async def client_row(db: Database, client_id: str) -> dict | None:
    return await db.fetchone(
        f"SELECT {CLIENT_COLUMNS} FROM paired_clients WHERE id = ?", (client_id,)
    )


async def overrides_of(db: Database, client_id: str) -> list[dict]:
    return await db.fetchall(
        "SELECT client_id, rule_key, value_json, user_choice, expires_at, note, "
        "created_by, created_at, origin, created_by_client_id FROM device_overrides "
        "WHERE client_id = ? ORDER BY rule_key",
        (client_id,),
    )


async def resolve_client(
    db: Database, client_id: str, now: str | None = None
) -> tuple[dict, dict, dict[str, dict]] | None:
    """(client row, profile row, effective rules) of one device."""
    client = await client_row(db, client_id)
    if not client:
        return None
    client, prof, overrides, limit, tags = await follow_phone(db, client, now)
    if not prof:
        return None
    return client, prof, resolve_device(client, prof, overrides, limit, now, tags)


async def resolve_all(
    db: Database,
    client_ids: list[str] | None = None,
    now: str | None = None,
    classes: dict | None = None,
) -> dict[str, dict]:
    """client_id -> {"client", "profile", "overrides", "rules"} for many devices
    (``classes``: device classes of the states the caller enforces with, else
    the live mirror's)."""
    if client_ids is None:
        clients = await db.fetchall(f"SELECT {CLIENT_COLUMNS} FROM paired_clients")
    elif not client_ids:
        return {}
    else:
        marks = ",".join("?" for _ in client_ids)
        clients = await db.fetchall(
            f"SELECT {CLIENT_COLUMNS} FROM paired_clients WHERE id IN ({marks})",
            tuple(client_ids),
        )
    profiles = {p["id"]: p for p in await profile_rows(db)}
    rows_by_id = {c["id"]: c for c in clients}
    missing = sorted(
        {p for p in (carrier_of(c) for c in clients) if p and p not in rows_by_id}
    )
    if missing:
        marks = ",".join("?" for _ in missing)
        for c in await db.fetchall(
            f"SELECT {CLIENT_COLUMNS} FROM paired_clients WHERE id IN ({marks})",
            tuple(missing),
        ):
            rows_by_id[c["id"]] = c
    by_client: dict[str, list[dict]] = {}
    for o in await db.fetchall(
        "SELECT client_id, rule_key, value_json, user_choice, expires_at, note, "
        "created_by, created_at, origin, created_by_client_id FROM device_overrides"
    ):
        by_client.setdefault(o["client_id"], []).append(o)
    now = now or now_iso()
    tags = await area_tags(db) if any(carrier_of(c) for c in clients) else None
    out = {}
    for c in clients:
        phone = rows_by_id.get(carrier_of(c) or "")
        phone_prof = profiles.get(phone["profile_id"]) if phone else None
        limit = None
        if phone_prof is not None and not phone_prof["child_protection"]:
            # An adult's watch never has more than its phone has now.
            limit = resolve_inputs(
                device_inputs(phone, phone_prof, by_client.get(phone["id"], []), now)
            )
            if _phone_usable(phone):
                _unreadable_logged.discard(c.get("id"))
        child_phone = phone_prof is not None and phone_prof["child_protection"]
        if (
            carrier_of(c)
            and not child_phone
            and (phone_prof is None or not _phone_usable(phone))
        ):
            limit = _unreadable_phone(c, carrier_of(c))
        if phone_prof is not None and phone_prof["child_protection"]:
            # A child's watch: the phone's profile, adjustments and kids state.
            c = as_child_watch(c, phone)
            prof = phone_prof
            overs = by_client.get(phone["id"], [])
        else:
            prof = profiles.get(c["profile_id"])
            overs = by_client.get(c["id"], [])
        if prof is None:
            continue
        out[c["id"]] = {
            "client": c,
            "profile": prof,
            "overrides": overs,
            "rules": resolve_device(c, prof, overs, limit, now, tags, classes),
        }
    return out


async def effective_rule(db: Database, client_id: str, key: str) -> Any:
    """Effective value of one rule (None for unknown devices/not applicable)."""
    resolved = await resolve_client(db, client_id)
    if not resolved:
        return None
    return resolved[2][key]["value"]


async def remote_allowed_map(db: Database, client_ids: list[str]) -> dict[str, bool]:
    """Effective remote access per device (HA user local_only = not this)."""
    out = {}
    for cid, item in (await resolve_all(db, client_ids)).items():
        out[cid] = item["rules"]["remote_access"]["value"] is True
    return out


async def default_profiles(db: Database) -> dict[str, str]:
    out = {}
    for slot in cat.DEFAULT_SLOTS:
        out[slot] = await db.get_setting(rows.default_setting_key(slot))
    return out


async def guest_default_hours(db: Database) -> int:
    raw = await db.get_setting(rows.SETTING_GUEST_HOURS)
    try:
        return max(1, min(int(raw), cat.guest_access()["max_hours"]))
    except (TypeError, ValueError):
        return int(cat.guest_access()["default_hours"])


async def default_profile_for(db: Database, platform: str | None, guest: bool) -> str:
    slot = "guest" if guest else cat.device_platform(platform)
    pid = await db.get_setting(rows.default_setting_key(slot))
    if not pid or not await profile_row(db, pid):
        raise ProfileError(
            409, "default_profile_missing", f"no default profile for {slot}"
        )
    return pid


async def language(db: Database) -> str:
    return (await db.get_setting(rows.SETTING_LANGUAGE)) or rows.DEFAULT_LANGUAGE


# ── admin views ─────────────────────────────────────────────────────────────


def stored_or_default(body: dict, key: str, *, guest: bool = False):
    """A profile's value of ``key``, else what the resolver uses without one:
    the catalog default, but ``hub`` for control_path with parental controls,
    for a guest kind and for a guest device (fail closed)."""
    if key in body["rules"]:
        value = copy.deepcopy(body["rules"][key])
        if key == "control_path" and value not in res.CONTROL_PATHS:
            return "hub"  # an unknown value is the strictest (as the resolver)
        return value
    if key == "control_path" and (
        body.get("child_protection")
        or guest
        or body.get("kind", "person") in res.GUEST_KINDS
    ):
        return "hub"
    return copy.deepcopy(cat.rule(key)["default"])


def _full_rules(body: dict) -> dict:
    return {r["key"]: stored_or_default(body, r["key"]) for r in cat.catalog()["rules"]}


def pairing_hub_remote(prof: dict) -> dict[str, dict[str, bool]]:
    """Whether a device paired now with this profile is on Hub only with
    remote access (it holds the read-only key because of remote access), per
    access (member, guest) and platform. A new device has no adjustment, so
    no later end can lead there."""
    out: dict[str, dict[str, bool]] = {}
    for access in ("member", "guest"):
        out[access] = {}
        for platform in cat.DEVICE_PLATFORMS:
            client = {
                "platform": platform,
                "access": access,
                "expires_at": "9999-12-31T00:00:00Z" if access == "guest" else None,
                "own_area_id": None,
            }
            out[access][platform] = is_hub_remote(
                resolve_inputs(device_inputs(client, prof, []))
            )
    return out


async def profile_view(
    db: Database, row: dict, *, defaults: dict | None = None
) -> dict:
    body = body_of(row)
    if defaults is None:
        defaults = await default_profiles(db)
    counts = await db.fetchone(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN access = 'guest' THEN 1 ELSE 0 END) AS g, "
        "SUM(CASE WHEN EXISTS (SELECT 1 FROM device_overrides o WHERE o.client_id = "
        "c.id) THEN 1 ELSE 0 END) AS a FROM paired_clients c WHERE c.profile_id = ?",
        (row["id"],),
    )
    origin = None
    update = None
    if row.get("origin_suggestion"):
        origin = {
            "suggestion_id": row["origin_suggestion"],
            "version": row["origin_version"],
        }
        sug = cat.suggestion(row["origin_suggestion"])
        if sug and sug["version"] > (row["origin_version"] or 0):
            update = {"version": sug["version"]}
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description"),
        "icon": row.get("icon"),
        "color": row.get("color"),
        "kind": body["kind"],
        "child_protection": body["child_protection"],
        "rules": _full_rules(body),
        # A device paired now with this profile: Hub only with remote access.
        "hub_remote_pairing": pairing_hub_remote(row),
        "fixed": body["fixed"],
        "user_choice": body["user_choice"],
        "origin": origin,
        "suggestion_update": update,
        "catalog_defaults": cat.catalog_defaults(cat.migrate_body(row["body_json"])),
        "default_for": [slot for slot, pid in defaults.items() if pid == row["id"]],
        "device_count": int(counts["n"] or 0),
        "adjusted_device_count": int(counts["a"] or 0),
        "guest_device_count": int(counts["g"] or 0),
        "has_remote_pin": body["has_remote_pin"],
        "remote_pin_fixed": body["remote_pin_fixed"],
        "rev": row["rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "updated_by": row.get("updated_by"),
    }


def override_view(row: dict) -> dict:
    return {
        "rule": row["rule_key"],
        "value": json.loads(row["value_json"]),
        "user_choice": bool(row["user_choice"]),
        "expires_at": _ts(row.get("expires_at")),
        "note": row.get("note"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "origin": row.get("origin") or "admin",
        "created_by_client_id": row.get("created_by_client_id"),
    }


async def effective_view(db: Database, client_id: str) -> dict:
    resolved = await resolve_client(db, client_id)
    if not resolved:
        raise ProfileError(404, "client_not_found", "Client not found")
    client, prof, rules = resolved
    overrides = await overrides_of(db, client_id)
    body = body_of(prof)
    pin = effective_pin(client, prof)
    out_rules = {}
    for key, e in rules.items():
        item = dict(e)
        if e["source"] != "not_applicable":
            base = stored_or_default(body, key, guest=client.get("access") == "guest")
            item["profile_value"] = res._resolve_own_area(
                key, base, client.get("own_area_id")
            )
        out_rules[key] = item
    return {
        "client_id": client["id"],
        "platform": client.get("platform"),
        "access": {
            **access_of(client),
            "set_up_by": set_up_by_shown(client),
            "hours": _access_now(client, prof, rules),
            "time_zone": _time_zone(),
        },
        "profile": {
            "id": prof["id"],
            "name": prof["name"],
            "kind": prof.get("kind") or "person",
            "rev": prof["rev"],
            "child_protection": bool(prof["child_protection"]),
        },
        "own_area_id": client.get("own_area_id"),
        "has_remote_pin": device_has_pin(client),
        "remote_pin_source": pin[2] if pin else None,
        "rules": out_rules,
        "overrides": [override_view(o) for o in overrides],
        # On Hub only with remote access now, or the earliest end after which
        # it is (store.hub_remote_schedule).
        **dict(
            zip(
                ("hub_remote_now", "hub_remote_at"),
                hub_remote_schedule(client, prof, overrides),
            )
        ),
    }


def _access_now(client: dict, prof: dict, rules: dict) -> dict:
    from services import access

    return access.state_of(client, prof, rules)


def _time_zone() -> str:
    from services import access

    return access.client_time_zone()


def set_up_by_shown(client: dict) -> dict | None:
    """``set_up_by`` as the device itself may see it (client_config.access,
    contract $defs/set_up_by_shown): name and method, never the client_id of
    the setting-up device (a child must not learn its manager's id)."""
    by = set_up_by(client)
    if by is None:
        return None
    return {"name": by["name"], "method": by["method"]}


def set_up_by(client: dict) -> dict | None:
    """Member device that set this device up, else None (admin views)."""
    if not client.get("set_up_by_client_id"):
        return None
    return {
        "client_id": client["set_up_by_client_id"],
        "name": client.get("set_up_by_name") or "",
        "method": client.get("set_up_method") or "onboard",
    }


# ── validation helpers ──────────────────────────────────────────────────────


def clean_name(name: Any) -> str:
    if not isinstance(name, str):
        raise ProfileError(422, "name", "name must be a string", "name")
    cleaned = " ".join(name.split())
    if (
        not cleaned
        or len(cleaned) > cat.name_max_length()
        or any(ord(ch) < 32 for ch in cleaned)
    ):
        raise ProfileError(422, "name", "invalid name", "name")
    return cleaned


async def ensure_name_free(
    db: Database, name: str, except_id: str | None = None
) -> None:
    clash = await db.fetchone(
        "SELECT id FROM profiles WHERE name = ? COLLATE NOCASE AND id != ?",
        (name, except_id or ""),
    )
    if clash:
        raise ProfileError(
            422, "name.duplicate", "a profile with this name exists", "name"
        )


async def ensure_room(db: Database) -> None:
    row = await db.fetchone("SELECT COUNT(*) AS n FROM profiles")
    if row["n"] >= cat.max_profiles():
        raise ProfileError(
            422, "profile.limit", f"at most {cat.max_profiles()} profiles"
        )


async def free_name(db: Database, base: str) -> str:
    limit = cat.name_max_length()
    candidate = base[:limit]
    n = 2
    while await db.fetchone(
        "SELECT 1 FROM profiles WHERE name = ? COLLATE NOCASE", (candidate,)
    ):
        suffix = f" ({n})"
        candidate = base[: limit - len(suffix)] + suffix
        n += 1
    return candidate


# ── writes (inside the caller's transaction) ────────────────────────────────


async def insert_profile(
    db: Database,
    *,
    name: str,
    body: dict,
    actor: str | None,
    icon: str | None = None,
    color: str | None = None,
    description: str | None = None,
    origin: tuple[str, int] | None = None,
    kind: str | None = None,
) -> str:
    await ensure_room(db)
    await ensure_name_free(db, name)
    return await rows.insert_profile(
        db,
        kind=kind,
        name=name,
        body=body,
        now=now_iso(),
        actor=actor,
        icon=icon,
        color=color,
        description=description,
        origin=origin,
    )


async def save_profile(
    db: Database,
    row: dict,
    *,
    body: dict,
    actor: str | None,
    name: str | None = None,
    icon: Any = UNSET,
    color: Any = UNSET,
    description: Any = UNSET,
    origin_version: int | None = None,
    pin: tuple[str | None, str | None] | None = None,
    pin_fixed: bool | None = None,
) -> None:
    """New revision of a profile (icon/color/description ``UNSET`` = unchanged)."""
    if (
        body.get("child_protection")
        and (await default_profiles(db)).get("guest") == row["id"]
    ):
        # Every guest pairing would fail afterwards.
        raise ProfileError(
            422,
            "profile.guest_child_protection",
            "the guest default cannot have parental controls",
            "child_protection",
        )
    stored = rows.body_json(body)
    rev = row["rev"] + 1
    now = now_iso()
    sets = {
        "body_json": stored,
        "kind": body.get("kind") or row.get("kind") or "person",
        "child_protection": 1 if body.get("child_protection") else 0,
        "rev": rev,
        "updated_at": now,
        "updated_by": actor,
    }
    if name is not None:
        sets["name"] = name
    if icon is not UNSET:
        sets["icon"] = icon
    if color is not UNSET:
        sets["color"] = color
    if description is not UNSET:
        sets["description"] = description
    if origin_version is not None:
        sets["origin_version"] = origin_version
    if pin is not None:
        sets["remote_pin_hash"], sets["remote_pin_salt"] = pin
    if pin_fixed is not None:
        sets["remote_pin_fixed"] = 1 if pin_fixed else 0
    cols = ", ".join(f"{k} = ?" for k in sets)
    try:
        await db.execute(
            f"UPDATE profiles SET {cols} WHERE id = ?", (*sets.values(), row["id"])
        )
    except Exception as e:  # noqa: BLE001 - trigger of the guest boundary
        if "guest_child_protection" in str(e):
            raise ProfileError(
                422,
                "profile.guest_child_protection",
                "guest devices use this profile; parental controls are not possible",
                "child_protection",
            ) from e
        raise
    await db.execute(
        "INSERT INTO profile_revisions (profile_id, rev, name, kind, "
        "child_protection, body_json, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            rev,
            name or row["name"],
            sets["kind"],
            sets["child_protection"],
            stored,
            now,
            actor,
        ),
    )
    await prune_overrides(db, profile_id=row["id"])


async def prune_overrides(
    db: Database, *, profile_id: str | None = None, client_id: str | None = None
) -> list[tuple[str, str]]:
    """Delete adjustments the profile fixes or that equal the profile value."""
    where, params = (
        ("c.profile_id = ?", (profile_id,))
        if profile_id
        else ("c.id = ?", (client_id,))
    )
    items = await db.fetchall(
        "SELECT o.client_id, o.rule_key, o.value_json, o.user_choice, c.profile_id "
        f"FROM device_overrides o JOIN paired_clients c ON c.id = o.client_id WHERE {where}",
        params,
    )
    bodies: dict[str, dict] = {}
    removed = []
    for o in items:
        if o["profile_id"] not in bodies:
            bodies[o["profile_id"]] = body_of(await profile_row(db, o["profile_id"]))
        body = bodies[o["profile_id"]]
        r = cat.rule(o["rule_key"])
        base = body["rules"].get(o["rule_key"], r["default"] if r else None)
        same = (
            r is not None
            and res.json_equal(json.loads(o["value_json"]), base)
            and bool(o["user_choice"]) == (o["rule_key"] in body["user_choice"])
        )
        if r is None or o["rule_key"] in body["fixed"] or same:
            await db.execute(
                "DELETE FROM device_overrides WHERE client_id = ? AND rule_key = ?",
                (o["client_id"], o["rule_key"]),
            )
            removed.append((o["client_id"], o["rule_key"]))
    return removed


def expires_from_request(
    expires: str | None,
    expires_at: str | None,
    tz: str | None,
    now: datetime | None = None,
) -> str | None:
    """End of a temporary adjustment (preset in the hub time zone or own time)."""
    now = now or utcnow()
    if expires and expires_at:
        raise ProfileError(
            422, "override.expires_at", "expires and expires_at together"
        )
    if not expires and not expires_at:
        return None
    if expires:
        if expires not in {p["id"] for p in cat.catalog()["expiry_presets"]}:
            raise ProfileError(422, "override.expires_at", "unknown preset")
        end = res.expiry(expires, res.fmt_ts(now), tz or "UTC")
    else:
        try:
            end = res.parse_ts(expires_at) if isinstance(expires_at, str) else None
        except ValueError:
            end = None  # not a time: 422 below, never a 500
        if (
            end is None
            or "T" not in expires_at
            or (
                expires_at[-1] not in "Zz"
                and "+" not in expires_at[10:]
                and "-" not in expires_at[10:]
            )
        ):
            raise ProfileError(
                422, "override.expires_at", "expires_at needs a time zone"
            )
    limits = cat.expiry_limits()
    if end - now < timedelta(minutes=limits["min_minutes"]) or end - now > timedelta(
        days=limits["max_days"]
    ):
        raise ProfileError(
            422,
            "override.expires_at",
            f"the end must be {limits['min_minutes']} minutes to {limits['max_days']} days ahead",
        )
    return res.fmt_ts(end.replace(microsecond=0))


async def check_override(
    db: Database, client_id: str, key: str, value: Any, user_choice: bool
) -> tuple[dict, dict]:
    """Validate an adjustment for a device; returns (client, profile row)."""
    resolved = await resolve_client(db, client_id)
    if not resolved:
        raise ProfileError(404, "client_not_found", "Client not found")
    client, prof, rules = resolved
    if is_child_watch(client):
        # A child's watch follows its phone's adjustments (one rule set).
        raise ProfileError(
            409,
            "override.child_watch",
            "a child's watch follows its phone: adjust the phone",
            key,
        )
    r = cat.rule(key)
    if r is None:
        raise ProfileError(404, "rule_not_found", f"unknown rule {key}")
    e = rules[key]
    if e["source"] == "not_applicable":
        raise ProfileError(
            422, "override.not_applicable", f"{key} does not apply here", key
        )
    body = body_of(prof)
    if key in body["fixed"]:
        raise ProfileError(422, "override.fixed", f"{key} is fixed in the profile", key)
    if user_choice and not r["user_choice_allowed"]:
        raise ProfileError(
            422, "override.user_choice", f"{key} cannot be the user's choice", key
        )
    try:
        cat.check_value(key, value)
    except ProfileError as err:
        raise ProfileError(422, "override.value", err.message, key) from err
    active = set()
    if client.get("access") == "guest":
        active.add("guest")
    if body["child_protection"]:
        active.add("child_protection")
    for b in r["boundaries"]:
        if b["boundary"] not in active:
            continue
        lifted = b.get("lifted_by")
        if lifted and (rules.get(lifted) or {}).get("value") is True:
            continue  # e.g. watch while kids_watch is on
        if r["order"] is None:
            # Without order (setup_devices) the boundary always decides.
            decides, looser = True, True
        else:
            decides = res.json_equal(b["cap"], r["order"][0])
            looser = cat.order_index(key, value) > cat.order_index(key, b["cap"])
        if decides or looser:
            raise ProfileError(
                422,
                "override.boundary",
                f"a boundary decides {key} on this device",
                key,
            )
    return client, prof


async def put_override(
    db: Database,
    client_id: str,
    key: str,
    *,
    value: Any,
    user_choice: bool,
    expires_at: str | None,
    note: str | None,
    actor: str,
    origin: str = "admin",
    created_by_client_id: str | None = None,
) -> bool:
    """Store (or drop when equal to the profile) one adjustment; True if stored.

    ``origin``/``created_by_client_id``: who set it (a family manager
    device or the admin); the last writer wins.
    """
    client, prof = await check_override(db, client_id, key, value, user_choice)
    body = body_of(prof)
    base = body["rules"].get(key, cat.rule(key)["default"])
    if (
        expires_at is None
        and res.json_equal(value, base)
        and user_choice == (key in body["user_choice"])
    ):
        await db.execute(
            "DELETE FROM device_overrides WHERE client_id = ? AND rule_key = ?",
            (client_id, key),
        )
        return False
    await db.execute(
        "INSERT INTO device_overrides (client_id, rule_key, value_json, user_choice, "
        "expires_at, note, created_by, created_at, origin, created_by_client_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client_id, rule_key) DO UPDATE SET value_json = excluded.value_json, "
        "user_choice = excluded.user_choice, expires_at = excluded.expires_at, "
        "note = excluded.note, created_by = excluded.created_by, "
        "created_at = excluded.created_at, origin = excluded.origin, "
        "created_by_client_id = excluded.created_by_client_id",
        (
            client_id,
            key,
            rows.dumps(value),
            1 if user_choice else 0,
            expires_at,
            note,
            actor,
            now_iso(),
            origin,
            created_by_client_id,
        ),
    )
    return True


async def delete_override(db: Database, client_id: str, key: str) -> bool:
    cur = await db.execute(
        "DELETE FROM device_overrides WHERE client_id = ? AND rule_key = ?",
        (client_id, key),
    )
    return cur.rowcount > 0


async def assign(
    db: Database, client_id: str, profile_id: str, *, discard_overrides: bool
) -> None:
    client = await client_row(db, client_id)
    if not client:
        raise ProfileError(404, "client_not_found", "Client not found")
    prof = await profile_row(db, profile_id)
    if not prof:
        raise ProfileError(404, "profile_not_found", "Profile not found")
    if client.get("access") == "guest" and prof["child_protection"]:
        raise ProfileError(
            422,
            "profile.guest_child_protection",
            "a guest cannot have parental controls",
            "profile_id",
        )
    await db.execute(
        "UPDATE paired_clients SET profile_id = ? WHERE id = ?", (profile_id, client_id)
    )
    if client["profile_id"] != profile_id:
        await db.execute(
            "UPDATE paired_clients SET kids_override_until = NULL WHERE id = ?",
            (client_id,),
        )
    if discard_overrides:
        await db.execute(
            "DELETE FROM device_overrides WHERE client_id = ?", (client_id,)
        )
    else:
        await prune_overrides(db, client_id=client_id)


async def set_own_area(db: Database, client_id: str, area_id: str | None) -> None:
    if not await client_row(db, client_id):
        raise ProfileError(404, "client_not_found", "Client not found")
    if area_id is not None and not await db.fetchone(
        "SELECT id FROM areas WHERE id = ?", (area_id,)
    ):
        raise ProfileError(404, "area_not_found", "Area not found")
    await db.execute(
        "UPDATE paired_clients SET own_area_id = ? WHERE id = ?", (area_id, client_id)
    )


def areas_from_tags(tags: list[str], *, guest: bool) -> dict:
    """Value of the rule ``areas`` from client tags (integration).

    ``["*"]`` = all; ``[]`` = all for members, none for guests; ``area:<id>``
    = that area; other tags = areas with that tag.
    """
    tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
    if "*" in tags or (not tags and not guest):
        return {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
    pins = list(dict.fromkeys(t[5:] for t in tags if t.startswith("area:")))
    plain = list(dict.fromkeys(t for t in tags if not t.startswith("area:")))
    return {"mode": "selected", "area_ids": pins, "tags": plain, "own_area": False}


_WORD_RE = None


def area_matches(name: str, patterns: list[str]) -> bool:
    """A word of the area name starts with one of the prefixes."""
    import re

    global _WORD_RE
    if _WORD_RE is None:
        _WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
    words = [w.casefold() for w in _WORD_RE.findall(name or "")]
    return any(w.startswith(p.casefold()) for w in words for p in patterns)


async def preselect_areas(db: Database, body: dict, patterns: list[str] | None) -> dict:
    """``areas.area_ids`` of a new profile from a suggestion's ``area_patterns``:
    the active areas whose name matches; the admin checks the choice."""
    areas = (body.get("rules") or {}).get("areas")
    if not patterns or not isinstance(areas, dict) or areas.get("mode") != "selected":
        return body
    rows = await db.fetchall(
        "SELECT id, name FROM areas WHERE is_active = 1 ORDER BY sort_order, name"
    )
    ids = [r["id"] for r in rows if area_matches(r["name"], patterns)]
    if not ids:
        return body
    body = copy.deepcopy(body)
    current = list(body["rules"]["areas"].get("area_ids") or [])
    body["rules"]["areas"]["area_ids"] = current + [i for i in ids if i not in current]
    return body


def new_profile_id() -> str:
    return str(uuid.uuid4())
