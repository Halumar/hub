"""NFC and QR tags registered in the hub (contract docs/links.md).

A physical tag carries only ``https://<website>/t/<tag_id>``. What it does is
stored here (table ``scan_tags``): a name and an action (``show`` an entity
or area, ``toggle`` / ``turn_on`` / ``turn_off`` an entity, ``run`` a scene,
script or button). Admins manage the tags (``/api/scan-tags``); a device asks
with the WebSocket message ``tag_resolve`` and gets ``tag_result``. The hub
never executes a tag: the app switches afterwards like a widget
(``control_origin`` ``quick_access``), so quick access levels, the profile
and ``<ns>.control`` apply. Every resolve is logged as ``tag_scanned``
(activity, ``<ns>_event`` and ``<ns>_tag_scanned`` in Home Assistant).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.services.scan_tags")

MAX_TAGS = 100
NAME_MAX = 60
TAG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
KINDS = ("show", "toggle", "turn_on", "turn_off", "run")
# Domains each switching kind may target (run: scenes, scripts, buttons).
RUN_DOMAINS = frozenset({"scene", "script", "button", "input_button"})
SWITCH_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "input_boolean",
        "fan",
        "cover",
        "lock",
        "media_player",
        "climate",
        "humidifier",
        "valve",
        "siren",
        "vacuum",
        "lawn_mower",
        "automation",
        "water_heater",
    }
)
RESULTS = ("ok", "unknown_tag", "tags_off", "not_visible", "rate_limited")
RATE_LIMIT = 10
RATE_WINDOW = 60.0
VIA = ("nfc", "qr", "link")

_scans: dict[str, deque[float]] = defaultdict(deque)


class ScanTagError(Exception):
    """Admin API error: HTTP ``status`` and contract ``code``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def reset() -> None:
    """Forget the rate limit counters (tests)."""
    _scans.clear()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_tag_id() -> str:
    """22 characters base64url, 128 random bits."""
    return secrets.token_urlsafe(16)


def tag_url(tag_id: str) -> str:
    from brand import BRAND_WEBSITE_URL

    return f"{BRAND_WEBSITE_URL.rstrip('/')}/t/{tag_id}"


def _view(row: dict) -> dict[str, Any]:
    return {
        "tag_id": row["tag_id"],
        "name": row["name"],
        "action": {
            "kind": row["action_kind"],
            "entity_id": row.get("entity_id"),
            "area_id": row.get("area_id"),
        },
        "url": tag_url(row["tag_id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_scanned_at": row.get("last_scanned_at"),
    }


async def list_tags(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM scan_tags ORDER BY created_at DESC, tag_id LIMIT ?",
        (MAX_TAGS,),
    )
    return [_view(r) for r in rows]


async def get_tag(db: Database, tag_id: str) -> dict[str, Any] | None:
    if not isinstance(tag_id, str) or not TAG_ID_RE.match(tag_id):
        return None
    row = await db.fetchone("SELECT * FROM scan_tags WHERE tag_id = ?", (tag_id,))
    return _view(row) if row else None


async def _check_action(db: Database, action: dict[str, Any]) -> dict[str, Any]:
    kind = action.get("kind")
    if kind not in KINDS:
        raise ScanTagError(422, "invalid_target", "unknown action kind")
    entity_id = action.get("entity_id")
    area_id = action.get("area_id")
    if entity_id is not None and (
        not isinstance(entity_id, str) or not ENTITY_RE.match(entity_id)
    ):
        raise ScanTagError(422, "invalid_target", "invalid entity_id")
    if area_id is not None and (
        not isinstance(area_id, str) or not UUID_RE.match(area_id)
    ):
        raise ScanTagError(422, "invalid_target", "invalid area_id")
    if kind == "show":
        if (entity_id is None) == (area_id is None):
            raise ScanTagError(
                422, "invalid_target", "show needs either an entity or an area"
            )
        if area_id is not None:
            area = await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,))
            if area is None:
                raise ScanTagError(422, "invalid_target", "unknown area")
        return {"kind": kind, "entity_id": entity_id, "area_id": area_id}
    if entity_id is None or area_id is not None:
        raise ScanTagError(422, "invalid_target", f"{kind} needs an entity")
    domain = entity_id.split(".", 1)[0]
    allowed = RUN_DOMAINS if kind == "run" else SWITCH_DOMAINS
    if domain not in allowed:
        raise ScanTagError(
            422, "invalid_target", f"{kind} does not fit an entity of {domain}"
        )
    return {"kind": kind, "entity_id": entity_id, "area_id": None}


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
        raise ScanTagError(422, "invalid_name", f"name 1–{NAME_MAX} characters")
    return name.strip()


async def create(
    db: Database, *, name: Any, action: dict[str, Any], tag_id: Any = None
) -> dict[str, Any]:
    clean = _clean_name(name)
    target = await _check_action(db, action)
    if tag_id is None:
        tag_id = new_tag_id()
    elif not isinstance(tag_id, str) or not TAG_ID_RE.match(tag_id):
        raise ScanTagError(422, "invalid_tag_id", "tag_id is 22 base64url characters")
    count = await db.fetchone("SELECT COUNT(*) AS n FROM scan_tags")
    if count and count["n"] >= MAX_TAGS:
        raise ScanTagError(422, "tag_limit", f"at most {MAX_TAGS} tags")
    if await db.fetchone("SELECT tag_id FROM scan_tags WHERE tag_id = ?", (tag_id,)):
        raise ScanTagError(409, "tag_exists", "the tag is already registered")
    now = _now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO scan_tags (tag_id, name, action_kind, entity_id, area_id, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tag_id,
                clean,
                target["kind"],
                target["entity_id"],
                target["area_id"],
                now,
                now,
            ),
        )
    tag = await get_tag(db, tag_id)
    assert tag is not None
    return tag


async def update(
    db: Database, tag_id: str, *, name: Any, action: dict[str, Any]
) -> dict[str, Any]:
    if await get_tag(db, tag_id) is None:
        raise ScanTagError(404, "not_found", "unknown tag")
    clean = _clean_name(name)
    target = await _check_action(db, action)
    async with db.transaction():
        await db.execute(
            "UPDATE scan_tags SET name = ?, action_kind = ?, entity_id = ?, "
            "area_id = ?, updated_at = ? WHERE tag_id = ?",
            (
                clean,
                target["kind"],
                target["entity_id"],
                target["area_id"],
                _now(),
                tag_id,
            ),
        )
    tag = await get_tag(db, tag_id)
    assert tag is not None
    return tag


async def delete(db: Database, tag_id: str) -> bool:
    if await get_tag(db, tag_id) is None:
        return False
    async with db.transaction():
        await db.execute("DELETE FROM scan_tags WHERE tag_id = ?", (tag_id,))
    return True


# ── resolve (device) ─────────────────────────────────────────────────────────


def _rate_limited(client_id: str) -> int | None:
    """Seconds until the next scan is accepted, or None."""
    now = time.monotonic()
    hits = _scans[client_id]
    while hits and now - hits[0] >= RATE_WINDOW:
        hits.popleft()
    if len(hits) >= RATE_LIMIT:
        return max(1, int(RATE_WINDOW - (now - hits[0])) + 1)
    hits.append(now)
    return None


async def _visible(db: Database, client_id: str, action: dict[str, Any]) -> bool:
    from api import authz
    from services import hub_control

    if action.get("area_id"):
        return action["area_id"] in await authz.listed_area_ids(db, client_id)
    return action.get("entity_id") in await hub_control.visible_entities(db, client_id)


async def _scan_allowed(db: Database, client_id: str) -> bool:
    from services.profiles import store as profiles

    resolved = await profiles.resolve_client(db, client_id)
    if not resolved:
        return False
    client, _profile, rules = resolved
    if client.get("access") == "guest" or not client.get("is_active", 1):
        return False
    entry = rules.get("scan_tags") or {}
    return entry.get("source") != "not_applicable" and entry.get("value") is True


async def resolve(
    db: Database, client_id: str, tag_id: Any, via: Any = None
) -> dict[str, Any]:
    """Answer ``tag_result`` for one scan and log it (never raises for input)."""
    if not isinstance(tag_id, str) or not TAG_ID_RE.match(tag_id):
        return {"type": "error", "message": "tag_id must be 22 base64url characters"}
    wait = _rate_limited(client_id)
    if wait is not None:
        return {
            "type": "tag_result",
            "tag_id": tag_id,
            "result": "rate_limited",
            "retry_after": wait,
        }
    tag = await get_tag(db, tag_id)
    if tag is None:
        result = "unknown_tag"
    elif not await _scan_allowed(db, client_id):
        result = "tags_off"
    elif not await _visible(db, client_id, tag["action"]):
        result = "not_visible"
    else:
        result = "ok"
    if tag is not None and result == "ok":
        async with db.transaction():
            await db.execute(
                "UPDATE scan_tags SET last_scanned_at = ? WHERE tag_id = ?",
                (_now(), tag_id),
            )
    await _log(db, client_id, tag_id, tag, result, via)
    answer: dict[str, Any] = {"type": "tag_result", "tag_id": tag_id, "result": result}
    if result == "ok" and tag is not None:
        answer["tag"] = {"name": tag["name"], "action": tag["action"]}
    return answer


async def _log(
    db: Database,
    client_id: str,
    tag_id: str,
    tag: dict[str, Any] | None,
    result: str,
    via: Any,
) -> None:
    from services import event_log

    action = (tag or {}).get("action") or {}
    await event_log.record(
        db,
        category=event_log.ACTIVITY,
        kind="tag_scanned",
        event_kind="tag_scanned",
        source="hub",
        client_id=client_id,
        entity_id=action.get("entity_id") if result == "ok" else None,
        action=action.get("kind") if result == "ok" else None,
        result="ok" if result == "ok" else "denied",
        reason=None if result == "ok" else result,
        detail={
            "tag_id": tag_id,
            "tag_name": (tag or {}).get("name"),
            "tag_result": result,
            "via": via if via in VIA else "link",
        },
    )
    logger.info("Tag %s… resolved for %s: %s", tag_id[:4], client_id, result)
