"""Low-level profile rows on a raw aiosqlite connection.

Used by the schema code (fresh install, migration v9) and by the store.
Callers run these inside a transaction.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import aiosqlite

from services.profiles import catalog as cat

# Hub language of generated names (suggestion names, "Kopie von …").
DEFAULT_LANGUAGE = "de"
SETTING_LANGUAGE = "hub_language"
SETTING_GUEST_HOURS = "guest_default_hours"


def default_setting_key(slot: str) -> str:
    return f"default_profile_{slot}"


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def body_json(body: dict) -> str:
    """Stored form of a profile body (``child_protection`` lives in its column)."""
    return dumps(cat.normalize_body(body))


async def insert_profile(
    conn: aiosqlite.Connection,
    *,
    name: str,
    body: dict,
    now: str,
    actor: str | None,
    description: str | None = None,
    icon: str | None = None,
    color: str | None = None,
    origin: tuple[str, int] | None = None,
    profile_id: str | None = None,
    kind: str | None = None,
) -> str:
    profile_id = profile_id or str(uuid.uuid4())
    stored = body_json(body)
    child = 1 if body.get("child_protection") else 0
    kind = kind or body.get("kind") or "person"
    await conn.execute(
        "INSERT INTO profiles (id, name, description, icon, color, kind, "
        "child_protection, body_json, origin_suggestion, origin_version, rev, "
        "created_at, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (
            profile_id,
            name,
            description,
            icon,
            color,
            kind,
            child,
            stored,
            origin[0] if origin else None,
            origin[1] if origin else None,
            now,
            now,
            actor,
        ),
    )
    await conn.execute(
        "INSERT INTO profile_revisions (profile_id, rev, name, kind, child_protection, "
        "body_json, created_at, created_by) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (profile_id, name, kind, child, stored, now, actor),
    )
    return profile_id


async def set_setting(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


async def audit(
    conn: aiosqlite.Connection,
    action: str,
    *,
    now: str,
    actor: str | None,
    profile_id: str | None = None,
    client_id: str | None = None,
    rule_key: str | None = None,
    before: Any = None,
    after: Any = None,
    note: str | None = None,
) -> None:
    await conn.execute(
        "INSERT INTO profile_audit (ts, action, actor, profile_id, client_id, rule_key, "
        "before_json, after_json, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            action,
            actor,
            profile_id,
            client_id,
            rule_key,
            None if before is None else dumps(before),
            None if after is None else dumps(after),
            note,
        ),
    )


def suggestion_name(suggestion: dict, language: str = DEFAULT_LANGUAGE) -> str:
    names = suggestion["name"]
    return names.get(language) or names.get("en") or suggestion["id"]


async def create_starters(
    conn: aiosqlite.Connection, *, now: str, actor: str | None
) -> dict[str, str]:
    """The starter suggestions as profiles and the default profiles (fresh hub)."""
    ids: dict[str, str] = {}
    for sug in cat.suggestions()["suggestions"]:
        if not sug.get("starter"):
            continue
        ids[sug["id"]] = await insert_profile(
            conn,
            name=suggestion_name(sug),
            body=cat.materialize(sug["id"]),
            now=now,
            actor=actor,
            description=sug["description"].get(DEFAULT_LANGUAGE),
            icon=sug.get("icon"),
            color=sug.get("color"),
            origin=(sug["id"], sug["version"]),
            kind=sug["kind"],
        )
    for sug in cat.suggestions()["suggestions"]:
        if sug["id"] not in ids:
            continue
        for slot in sug.get("default_for", []):
            await set_setting(conn, default_setting_key(slot), ids[sug["id"]])
    await set_setting(
        conn, SETTING_GUEST_HOURS, str(cat.guest_access()["default_hours"])
    )
    missing = [
        s
        for s in cat.DEFAULT_SLOTS
        if not any(
            slot == s
            for sug in cat.suggestions()["suggestions"]
            if sug["id"] in ids
            for slot in sug.get("default_for", [])
        )
    ]
    if missing:
        raise RuntimeError(f"no starter suggestion is the default for {missing}")
    return ids
