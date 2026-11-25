"""Family tables. Plain database functions; writes run
inside the caller's transaction unless they open their own."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from models.database import Database
from services.family import data

SETTING_KEY = "family_settings"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ── house settings ──────────────────────────────────────────────────────────


async def get_settings(db: Database) -> dict[str, Any]:
    out = data.settings_defaults()
    stored = _loads(await db.get_setting(SETTING_KEY), {})
    for key in out:
        if key in stored:
            out[key] = stored[key]
    return out


def check_settings(value: dict[str, Any]) -> dict[str, Any]:
    """Validated copy (raises ValueError with the field name)."""
    defaults = data.settings_defaults()
    if set(value) != set(defaults):
        raise ValueError("settings")
    if not isinstance(value["requests_enabled"], bool):
        raise ValueError("requests_enabled")
    cats = value["categories"]
    if (
        not isinstance(cats, list)
        or len(set(cats)) != len(cats)
        or set(cats) - set(data.categories())
    ):
        raise ValueError("categories")
    lim = data.limits()
    for key in (
        "request_timeout_minutes",
        "child_requests_per_hour",
        "decline_cooldown_minutes",
        "activity_days",
    ):
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(key)
        if not lim[key]["min"] <= v <= lim[key]["max"]:
            raise ValueError(key)
    return {k: value[k] for k in defaults}


async def put_settings(db: Database, value: dict[str, Any]) -> dict[str, Any]:
    clean = check_settings(value)
    await db.set_setting(SETTING_KEY, dumps(clean))
    return clean


# ── scope of a family manager ───────────────────────────────────────────────


async def scope_row(db: Database, client_id: str) -> dict[str, Any]:
    row = await db.fetchone(
        "SELECT * FROM family_scopes WHERE client_id = ?", (client_id,)
    )
    if not row:
        return {
            "scope": {"all": True, "client_ids": []},
            "notify_targets": [],
            "notify": {},
            "updated_at": None,
            "updated_by": None,
        }
    return {
        "scope": {
            "all": bool(row["all_children"]),
            "client_ids": _loads(row["child_ids_json"], []),
        },
        "notify_targets": _loads(row["notify_targets_json"], []),
        "notify": _loads(row["notify_json"], {}),
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


async def put_scope(
    db: Database,
    client_id: str,
    *,
    scope: dict[str, Any] | None = None,
    notify_targets: list[str] | None = None,
    notify: dict[str, bool] | None = None,
    actor: str | None,
) -> None:
    current = await scope_row(db, client_id)
    scope = scope if scope is not None else current["scope"]
    targets = (
        notify_targets if notify_targets is not None else current["notify_targets"]
    )
    prefs = notify if notify is not None else current["notify"]
    await db.execute(
        "INSERT INTO family_scopes (client_id, all_children, child_ids_json, "
        "notify_targets_json, notify_json, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(client_id) DO UPDATE SET "
        "all_children = excluded.all_children, child_ids_json = excluded.child_ids_json, "
        "notify_targets_json = excluded.notify_targets_json, "
        "notify_json = excluded.notify_json, updated_at = excluded.updated_at, "
        "updated_by = excluded.updated_by",
        (
            client_id,
            1 if scope["all"] else 0,
            dumps(list(scope["client_ids"])),
            dumps(list(targets)),
            dumps(dict(prefs)),
            iso(utcnow()),
            actor,
        ),
    )


async def all_scopes(db: Database) -> dict[str, dict[str, Any]]:
    out = {}
    for row in await db.fetchall("SELECT client_id FROM family_scopes"):
        out[row["client_id"]] = await scope_row(db, row["client_id"])
    return out


# ── releases ────────────────────────────────────────────────────────────────


def child_actor(by: Any) -> Any:
    """What the child device learns about who acted: kind and display name –
    never that device's id or whether it was away (family-request.json)."""
    if not isinstance(by, dict):
        return by
    return {"kind": by.get("kind"), "name": by.get("name")}


def release_view(row: dict, *, for_child: bool = False) -> dict[str, Any]:
    by = _loads(row["by_json"], {})
    out: dict[str, Any] = {
        "kind": row["kind"],
        "until": iso(parse(row["until"])),
        "by": child_actor(by) if for_child else by,
        "request_id": row.get("request_id"),
        "created_at": iso(parse(row["created_at"])),
    }
    if row["kind"] == "entity":
        out["entity_id"] = row["entity_id"]
    return out


async def releases_of(
    db: Database, client_id: str, now: datetime | None = None
) -> list[dict]:
    now = now or utcnow()
    rows = await db.fetchall(
        "SELECT * FROM kids_releases WHERE client_id = ? ORDER BY until",
        (client_id,),
    )
    return [r for r in rows if (parse(r["until"]) or now) > now]


async def put_release(
    db: Database,
    client_id: str,
    kind: str,
    entity_id: str | None,
    until: datetime,
    by: dict[str, Any],
    request_id: str | None = None,
) -> dict:
    """A new release replaces the running one of the same kind (and entity)."""
    row = {
        "id": str(uuid.uuid4()),
        "client_id": client_id,
        "kind": kind,
        "entity_id": entity_id or "",
        "until": iso(until),
        "by_json": dumps(by),
        "request_id": request_id,
        "created_at": iso(utcnow()),
    }
    await db.execute(
        "INSERT INTO kids_releases (id, client_id, kind, entity_id, until, by_json, "
        "request_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(client_id, kind, entity_id) DO UPDATE SET id = excluded.id, "
        "until = excluded.until, by_json = excluded.by_json, "
        "request_id = excluded.request_id, created_at = excluded.created_at",
        tuple(row.values()),
    )
    return row


async def end_releases(
    db: Database, client_id: str, kind: str | None = None, entity_id: str | None = None
) -> list[dict]:
    where, params = ["client_id = ?"], [client_id]
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if entity_id:
        where.append("entity_id = ?")
        params.append(entity_id)
    clause = " AND ".join(where)
    rows = await db.fetchall(
        f"SELECT * FROM kids_releases WHERE {clause}", tuple(params)
    )
    if rows:
        await db.execute(f"DELETE FROM kids_releases WHERE {clause}", tuple(params))
    return rows


# ── requests ────────────────────────────────────────────────────────────────


async def request_row(db: Database, request_id: str) -> dict | None:
    return await db.fetchone("SELECT * FROM kids_requests WHERE id = ?", (request_id,))


async def open_request_of(db: Database, client_id: str) -> dict | None:
    return await db.fetchone(
        "SELECT * FROM kids_requests WHERE client_id = ? AND status = 'open' "
        "ORDER BY created_at DESC LIMIT 1",
        (client_id,),
    )


async def requests_since(db: Database, client_id: str, since: datetime) -> list[dict]:
    return await db.fetchall(
        "SELECT * FROM kids_requests WHERE client_id = ? AND created_at >= ? "
        "ORDER BY created_at DESC",
        (client_id, iso(since)),
    )


async def insert_request(
    db: Database,
    client_id: str,
    category: str,
    entity_id: str | None,
    note: str | None,
    expires_at: datetime,
) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "client_id": client_id,
        "category": category,
        "entity_id": entity_id,
        "note": note,
        "status": "open",
        "created_at": iso(utcnow()),
        "expires_at": iso(expires_at),
        "answer_json": None,
        "answered_at": None,
    }
    await db.execute(
        "INSERT INTO kids_requests (id, client_id, category, entity_id, note, status, "
        "created_at, expires_at, answer_json, answered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(row.values()),
    )
    return row


async def close_request(
    db: Database, request_id: str, status: str, answer: dict | None
) -> bool:
    """Close an open request; False when someone else was faster (first wins)."""
    cur = await db.execute(
        "UPDATE kids_requests SET status = ?, answer_json = ?, answered_at = ? "
        "WHERE id = ? AND status = 'open'",
        (
            status,
            dumps(answer) if answer is not None else None,
            iso(utcnow()),
            request_id,
        ),
    )
    return cur.rowcount == 1


async def requests_for(
    db: Database, client_ids: list[str], *, include_closed: bool, since: datetime
) -> list[dict]:
    if not client_ids:
        return []
    marks = ",".join("?" for _ in client_ids)
    status = "" if include_closed else " AND status = 'open'"
    return await db.fetchall(
        f"SELECT * FROM kids_requests WHERE client_id IN ({marks}){status} "
        "AND (status = 'open' OR created_at >= ?) ORDER BY created_at DESC LIMIT 100",
        (*client_ids, iso(since)),
    )


def answer_of(row: dict) -> dict | None:
    return _loads(row.get("answer_json"), {}) or None


# ── answer tokens (HA notifications) ────────────────────────────────────────


async def insert_token(
    db: Database,
    token_hash: str,
    request_id: str,
    action: str,
    manager_client_id: str | None,
    notify_service: str | None,
    expires_at: str,
) -> None:
    await db.execute(
        "INSERT INTO family_answer_tokens (token_hash, request_id, action, "
        "manager_client_id, notify_service, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (token_hash, request_id, action, manager_client_id, notify_service, expires_at),
    )


async def take_token(db: Database, token_hash: str) -> dict | None:
    """Single use: the row is deleted when it is read."""
    row = await db.fetchone(
        "SELECT * FROM family_answer_tokens WHERE token_hash = ?", (token_hash,)
    )
    if row:
        await db.execute(
            "DELETE FROM family_answer_tokens WHERE token_hash = ?", (token_hash,)
        )
    return row


async def drop_tokens(db: Database, request_id: str) -> None:
    await db.execute(
        "DELETE FROM family_answer_tokens WHERE request_id = ?", (request_id,)
    )


async def prune(db: Database, now: datetime | None = None) -> None:
    """Drop closed requests after the longest activity view and stale tokens
    (the activity itself lives in the log tab)."""
    now = now or utcnow()
    keep = now - timedelta(days=data.limits()["activity_days"]["max"])
    await db.execute(
        "DELETE FROM kids_requests WHERE status != 'open' AND created_at < ?",
        (iso(keep),),
    )
    await db.execute(
        "DELETE FROM family_answer_tokens WHERE expires_at < ?", (iso(now),)
    )
