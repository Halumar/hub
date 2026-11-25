"""Migration v13: setting up devices.

* ``profiles.kind``: from the origin suggestion; a profile without one is
  ``guest`` when it is the guest default profile, ``child`` with parental
  controls, else ``person``.
* The rules ``add_devices`` and ``invite_guests`` become ``setup_devices``
  ``{enabled, kinds}`` in profile bodies and device adjustments: inviting
  guests gives guest and house sitter, adding devices the member kinds
  (child, panel, tv, person); both together every kind. A device adjustment is
  built from the device's effective old values, so nothing loosens or
  tightens.
* ``paired_clients.invited_by_client_id`` / ``invited_by_name`` become
  ``set_up_by_client_id`` / ``set_up_by_name`` with ``set_up_method``
  ``invite``; devices confirmed by QR code get ``onboard`` and the confirming
  device from their ``paired`` activity.

Idempotent: a database whose v9 step already wrote the new names is left
as it is.
"""

from __future__ import annotations

import json
import logging

import aiosqlite

from services.profiles import catalog as cat

logger = logging.getLogger("hub.migrations")

OLD_KEYS = ("add_devices", "invite_guests")
ALL_KINDS = ("guest", "child", "panel", "tv", "house_sitter", "person")
GUEST_KINDS = ("guest", "house_sitter")
DEVICE_KINDS = ("child", "panel", "tv", "person")


def setup_value(add_devices: bool, invite_guests: bool) -> dict:
    """``setup_devices`` value of the former two rights."""
    kinds = set()
    if invite_guests:
        kinds.update(GUEST_KINDS)
    if add_devices:
        kinds.update(DEVICE_KINDS)
    return {
        "enabled": bool(kinds),
        "kinds": [k for k in ALL_KINDS if k in kinds],
    }


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _fetchall(conn: aiosqlite.Connection, sql: str, params=()) -> list[dict]:
    async with conn.execute(sql, params) as cur:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


def convert_body(body: dict) -> tuple[dict, bool]:
    """Body with ``setup_devices`` instead of the two old rules; changed?"""
    rules = body.get("rules") or {}
    fixed = list(body.get("fixed") or [])
    if not any(k in rules for k in OLD_KEYS) and not any(k in fixed for k in OLD_KEYS):
        return body, False
    a = rules.pop("add_devices", None) is True
    i = rules.pop("invite_guests", None) is True
    if "setup_devices" not in rules:
        rules["setup_devices"] = setup_value(a, i)
    was_fixed = [k for k in OLD_KEYS if k in fixed]
    fixed = [k for k in fixed if k not in OLD_KEYS]
    if was_fixed and "setup_devices" not in fixed:
        fixed.append("setup_devices")
    body = {**body, "rules": rules, "fixed": sorted(fixed)}
    return body, True


async def run(conn: aiosqlite.Connection) -> dict:
    report = {"kinds": {}, "bodies": 0, "overrides": 0, "set_up_by": 0}

    # 1. profile kind
    if "kind" not in await _columns(conn, "profiles"):
        kinds = ", ".join(f"'{k}'" for k in ALL_KINDS)
        await conn.execute(
            "ALTER TABLE profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'person' "
            f"CHECK (kind IN ({kinds}))"
        )
    if "kind" not in await _columns(conn, "profile_revisions"):
        await conn.execute(
            "ALTER TABLE profile_revisions ADD COLUMN kind TEXT NOT NULL DEFAULT 'person'"
        )
    guest_default = None
    for row in await _fetchall(
        conn, "SELECT value FROM settings WHERE key = 'default_profile_guest'"
    ):
        guest_default = row["value"]
    profiles = await _fetchall(
        conn,
        "SELECT id, origin_suggestion, child_protection, body_json FROM profiles",
    )
    for p in profiles:
        sug = cat.suggestion(p["origin_suggestion"]) if p["origin_suggestion"] else None
        if sug:
            kind = sug["kind"]
        elif p["id"] == guest_default:
            kind = "guest"
        elif p["child_protection"]:
            kind = "child"
        else:
            kind = "person"
        report["kinds"][p["id"]] = kind
        await conn.execute("UPDATE profiles SET kind = ? WHERE id = ?", (kind, p["id"]))
        await conn.execute(
            "UPDATE profile_revisions SET kind = ? WHERE profile_id = ?",
            (kind, p["id"]),
        )

    # 2. bodies (current and revisions)
    old_values: dict[str, tuple[bool, bool]] = {}
    for p in profiles:
        body = json.loads(p["body_json"] or "{}")
        rules = body.get("rules") or {}
        old_values[p["id"]] = (
            rules.get("add_devices") is True,
            rules.get("invite_guests") is True,
        )
        new, changed = convert_body(body)
        if changed:
            report["bodies"] += 1
            await conn.execute(
                "UPDATE profiles SET body_json = ? WHERE id = ?", (_dumps(new), p["id"])
            )
    for r in await _fetchall(
        conn, "SELECT profile_id, rev, body_json FROM profile_revisions"
    ):
        new, changed = convert_body(json.loads(r["body_json"] or "{}"))
        if changed:
            await conn.execute(
                "UPDATE profile_revisions SET body_json = ? WHERE profile_id = ? AND rev = ?",
                (_dumps(new), r["profile_id"], r["rev"]),
            )

    # 3. device adjustments: one setup_devices from the effective old values
    placeholders = ", ".join("?" for _ in OLD_KEYS)
    overrides = await _fetchall(
        conn,
        "SELECT o.*, c.profile_id FROM device_overrides o "
        "JOIN paired_clients c ON c.id = o.client_id "
        f"WHERE o.rule_key IN ({placeholders})",
        OLD_KEYS,
    )
    by_client: dict[str, list[dict]] = {}
    for o in overrides:
        by_client.setdefault(o["client_id"], []).append(o)
    for client_id, rows in by_client.items():
        prof_a, prof_i = old_values.get(rows[0]["profile_id"], (False, False))
        values = {"add_devices": prof_a, "invite_guests": prof_i}
        expiries = []
        for o in rows:
            values[o["rule_key"]] = json.loads(o["value_json"]) is True
            expiries.append(o["expires_at"])
        first = rows[0]
        expires = None if any(e is None for e in expiries) else max(expiries)
        await conn.execute(
            f"DELETE FROM device_overrides WHERE client_id = ? AND rule_key IN ({placeholders})",
            (client_id, *OLD_KEYS),
        )
        await conn.execute(
            "INSERT OR REPLACE INTO device_overrides (client_id, rule_key, value_json, "
            "user_choice, expires_at, note, created_by, created_at, origin, "
            "created_by_client_id) VALUES (?, 'setup_devices', ?, 0, ?, ?, ?, ?, ?, ?)",
            (
                client_id,
                _dumps(setup_value(values["add_devices"], values["invite_guests"])),
                expires,
                first.get("note"),
                first["created_by"],
                first["created_at"],
                first.get("origin") or "admin",
                first.get("created_by_client_id"),
            ),
        )
        report["overrides"] += 1

    # 4. who set a device up
    cols = await _columns(conn, "paired_clients")
    if "invited_by_client_id" in cols:
        await conn.execute(
            "ALTER TABLE paired_clients RENAME COLUMN invited_by_client_id TO set_up_by_client_id"
        )
        await conn.execute(
            "ALTER TABLE paired_clients RENAME COLUMN invited_by_name TO set_up_by_name"
        )
    if "set_up_method" not in await _columns(conn, "paired_clients"):
        await conn.execute(
            "ALTER TABLE paired_clients ADD COLUMN set_up_method TEXT "
            "CHECK (set_up_method IN ('onboard', 'invite'))"
        )
    await conn.execute(
        "UPDATE paired_clients SET set_up_method = 'invite' "
        "WHERE set_up_by_client_id IS NOT NULL AND set_up_method IS NULL"
    )
    names = {
        r["id"]: r["name"]
        for r in await _fetchall(conn, "SELECT id, name FROM paired_clients")
    }
    rows = await _fetchall(
        conn,
        "SELECT client_id, detail FROM client_activity WHERE event = 'paired' "
        "ORDER BY id",
    )
    for r in rows:
        try:
            detail = json.loads(r["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        by = detail.get("confirmed_by") if detail.get("method") == "onboard" else None
        if not by or r["client_id"] not in names:
            continue
        await conn.execute(
            "UPDATE paired_clients SET set_up_by_client_id = ?, set_up_by_name = ?, "
            "set_up_method = 'onboard' WHERE id = ? AND set_up_by_client_id IS NULL",
            (by, names.get(by) or "", r["client_id"]),
        )
        report["set_up_by"] += 1
    logger.warning(
        "Migration v13: %d profile kinds, %d bodies and %d device "
        "adjustments moved to setup_devices, %d devices with set_up_by from onboarding",
        len(report["kinds"]),
        report["bodies"],
        report["overrides"],
        report["set_up_by"],
    )
    return report
