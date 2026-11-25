"""Migration v20: "Hub only" (rule ``control_path`` value ``hub``).

* ``paired_clients.key_lost_at``: when a device last stopped holding a Home
  Assistant key (``resolver.holds_key``); the column ``remote_lost_at`` it
  replaces is renamed, so its moments stay.
* Profiles with parental controls get ``control_path = hub``: their devices
  had no key without remote access before as well.
* Profiles of the kinds guest, babysitter and house sitter get
  ``control_path = hub`` and the rule fixed. Their ``remote_access`` stays as
  stored: with it a device keeps a read-only key.
* Device adjustments of ``control_path`` on the devices of these profiles are
  deleted (audit ``override_removed``).
* Every changed profile gets a new revision (``rev`` + 1, so the devices
  fetch their configuration again) and an audit entry ``profile_updated``.
* Active devices of the guest kinds that hold no key afterwards get
  ``key_lost_at``: ``device_provisioning.secure_keyless_tokens`` rotates
  their tokens once the integration is connected; until then the hub no
  longer hands them out.

A watch whose phone may no longer carry one is removed with its HA user at
the start that runs this migration (``watch_access.remove_disallowed``: the
removal reaches the integration, which a migration step cannot).

Every other profile, device and adjustment stays as it is (members on
Checked or Direct, panels and TVs). Idempotent: a profile that already has
the values is left alone, and the column step checks the table first.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger("hub.migrations")

ACTOR = "Migration v20"
HUB = "hub"
RULE = "control_path"
GUEST_KINDS = ("guest", "babysitter", "house_sitter")


def _dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def _fetchall(conn: aiosqlite.Connection, sql: str, params=()) -> list[dict]:
    async with conn.execute(sql, params) as cur:
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in await cur.fetchall()]


async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return {row[1] for row in await cur.fetchall()}


async def key_lost_column(conn: aiosqlite.Connection) -> None:
    """``key_lost_at`` from ``remote_lost_at`` (renamed), else added. A
    table rebuilt by an earlier step with the current schema has both: the
    moments move over and ``remote_lost_at`` goes."""
    cols = await _columns(conn, "paired_clients")
    if "key_lost_at" in cols:
        if "remote_lost_at" in cols:
            await conn.execute(
                "UPDATE paired_clients SET key_lost_at = remote_lost_at "
                "WHERE key_lost_at IS NULL"
            )
            await conn.execute("ALTER TABLE paired_clients DROP COLUMN remote_lost_at")
        return
    if "remote_lost_at" in cols:
        await conn.execute(
            "ALTER TABLE paired_clients RENAME COLUMN remote_lost_at TO key_lost_at"
        )
    else:
        await conn.execute("ALTER TABLE paired_clients ADD COLUMN key_lost_at TEXT")


def convert_body(body: dict, *, guest_kind: bool) -> tuple[dict, bool]:
    """Body with ``control_path = hub`` (and fixed for the guest kinds); changed?"""
    rules = dict(body.get("rules") or {})
    fixed = list(body.get("fixed") or [])
    changed = False
    if rules.get(RULE) != HUB:
        rules[RULE] = HUB
        changed = True
    if guest_kind and RULE not in fixed:
        fixed = sorted([*fixed, RULE])
        changed = True
    if not changed:
        return body, False
    return {**body, "rules": rules, "fixed": fixed}, True


def _holds_key(client: dict, profile: dict, overrides: list[dict], now: str) -> bool:
    """``resolver.holds_key`` of one device with the migrated rows."""
    from services.profiles import resolver as res
    from services.profiles import store

    effective = store.resolve_inputs(
        store.device_inputs(client, profile, overrides, now)
    )
    return res.holds_key(effective)


async def run(conn: aiosqlite.Connection, *, now: str | None = None) -> dict:
    """Carry a v19 database over to v20 (inside the caller's transaction)."""
    from services.profiles import rows

    now = now or datetime.now(timezone.utc).isoformat()
    report: dict = {"profiles": [], "overrides_removed": 0, "keys_lost": []}
    await key_lost_column(conn)

    profiles = await _fetchall(
        conn,
        "SELECT id, name, kind, child_protection, body_json, rev, origin_suggestion "
        "FROM profiles ORDER BY id",
    )
    changed: dict[str, dict] = {}
    for p in profiles:
        guest_kind = p.get("kind") in GUEST_KINDS
        if not (p["child_protection"] or guest_kind):
            continue
        body = json.loads(p["body_json"] or "{}")
        new, did = convert_body(body, guest_kind=guest_kind)
        if not did:
            continue
        rev = int(p["rev"] or 1) + 1
        stored = _dumps(new)
        await conn.execute(
            "UPDATE profiles SET body_json = ?, rev = ?, updated_at = ?, updated_by = ? "
            "WHERE id = ?",
            (stored, rev, now, ACTOR, p["id"]),
        )
        await conn.execute(
            "INSERT INTO profile_revisions (profile_id, rev, name, kind, "
            "child_protection, body_json, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                p["id"],
                rev,
                p["name"],
                p.get("kind") or "person",
                1 if p["child_protection"] else 0,
                stored,
                now,
                ACTOR,
            ),
        )
        await rows.audit(
            conn,
            "profile_updated",
            now=now,
            actor=ACTOR,
            profile_id=p["id"],
            rule_key=RULE,
            before={
                "value": (body.get("rules") or {}).get(RULE),
                "fixed": RULE in (body.get("fixed") or []),
            },
            after={"value": HUB, "fixed": RULE in new["fixed"]},
        )
        changed[p["id"]] = {**p, "body_json": stored, "rev": rev}
        report["profiles"].append(p["id"])

    # Adjustments of control_path on the devices of these profiles.
    targets = [
        p["id"]
        for p in profiles
        if p["child_protection"] or p.get("kind") in GUEST_KINDS
    ]
    if targets:
        marks = ", ".join("?" for _ in targets)
        for o in await _fetchall(
            conn,
            "SELECT o.client_id, o.value_json, o.expires_at FROM device_overrides o "
            "JOIN paired_clients c ON c.id = o.client_id "
            f"WHERE o.rule_key = ? AND c.profile_id IN ({marks})",
            (RULE, *targets),
        ):
            await conn.execute(
                "DELETE FROM device_overrides WHERE client_id = ? AND rule_key = ?",
                (o["client_id"], RULE),
            )
            await rows.audit(
                conn,
                "override_removed",
                now=now,
                actor=ACTOR,
                client_id=o["client_id"],
                rule_key=RULE,
                before={
                    "value": json.loads(o["value_json"]),
                    "expires_at": o["expires_at"],
                },
            )
            report["overrides_removed"] += 1

    # Devices of the guest kinds that hold no key now.
    guest_profiles = {p["id"]: p for p in profiles if p.get("kind") in GUEST_KINDS}
    for pid in list(guest_profiles):
        if pid in changed:
            guest_profiles[pid] = changed[pid]
    if guest_profiles:
        marks = ", ".join("?" for _ in guest_profiles)
        clients = await _fetchall(
            conn,
            f"SELECT * FROM paired_clients WHERE profile_id IN ({marks}) "
            "AND is_active = 1 AND pending_deletion_at IS NULL ORDER BY id",
            tuple(guest_profiles),
        )
        resolve_now = now.replace("+00:00", "Z")
        for c in clients:
            overrides = await _fetchall(
                conn,
                "SELECT client_id, rule_key, value_json, user_choice, expires_at "
                "FROM device_overrides WHERE client_id = ? ORDER BY rule_key",
                (c["id"],),
            )
            if _holds_key(c, guest_profiles[c["profile_id"]], overrides, resolve_now):
                continue
            await conn.execute(
                "UPDATE paired_clients SET key_lost_at = ? WHERE id = ?",
                (now, c["id"]),
            )
            report["keys_lost"].append(c["id"])
    logger.warning(
        "Migration v20: %d profile(s) set to Hub only, %d device adjustment(s) of "
        "control_path removed, %d device(s) lose their Home Assistant key",
        len(report["profiles"]),
        report["overrides_removed"],
        len(report["keys_lost"]),
    )
    return report
