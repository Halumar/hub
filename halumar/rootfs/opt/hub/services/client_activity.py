"""Activity feed per device (Admin UI).

A small ring buffer in the database: at most ``MAX_EVENTS_PER_CLIENT`` rows
per device, older ones are dropped on insert, all rows go with the device
(``ON DELETE CASCADE``). Every new entry is also broadcast to admin sessions
as ``device_activity``.

Recording is best effort: a failing write is logged and never breaks the
connection or request that caused it.

Actions of a device (service calls) are not recorded: apps call Home
Assistant directly with the device's own HA user, the hub never sees them
(they show up in the HA logbook under the device's user). Kids mode:
child devices control through the hub; actions a rule blocked are recorded
(``rule_blocked``, throttled), allowed ones only in the audit log.
"""

from __future__ import annotations

import json
import logging

from models.database import Database

logger = logging.getLogger("hub.services.client_activity")

MAX_EVENTS_PER_CLIENT = 100

PAIRED = "paired"
CONNECTED = "connected"
DISCONNECTED = "disconnected"
APP_UPDATED = "app_updated"
UPGRADE_REQUIRED = "upgrade_required"
TOKEN_ISSUED = "token_issued"
TOKEN_ROTATED = "token_rotated"
# This device confirmed the onboarding of another device.
DEVICE_ADDED = "device_added"
# A guest paired with an invitation of this device.
GUEST_INVITED = "guest_invited"
# Kids mode: a rule blocked an action of this device (throttled), the
# admin changed its profile, released it or lifted its pauses.
RULE_BLOCKED = "rule_blocked"
PROFILE_CHANGED = "profile_changed"
KIDS_OVERRIDE = "kids_override"

EVENTS = (
    PAIRED,
    CONNECTED,
    DISCONNECTED,
    APP_UPDATED,
    UPGRADE_REQUIRED,
    TOKEN_ISSUED,
    TOKEN_ROTATED,
    DEVICE_ADDED,
    GUEST_INVITED,
    RULE_BLOCKED,
    PROFILE_CHANGED,
    KIDS_OVERRIDE,
)


def _row_to_event(row: dict) -> dict:
    detail = None
    if row.get("detail"):
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            detail = None
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "ts": row["ts"],
        "event": row["event"],
        "detail": detail if isinstance(detail, dict) else None,
    }


async def insert(
    db: Database, client_id: str, event: str, detail: dict | None = None
) -> dict | None:
    """Store one event and trim the device's buffer (inside a transaction).

    Returns the stored event, or None when the device does not exist.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown activity event {event!r}")
    clean = {k: v for k, v in (detail or {}).items() if v is not None} or None
    ts = Database.now()
    async with db.transaction():
        exists = await db.fetchone(
            "SELECT 1 AS x FROM paired_clients WHERE id = ?", (client_id,)
        )
        if not exists:
            return None
        cursor = await db.execute(
            "INSERT INTO client_activity (client_id, ts, event, detail) "
            "VALUES (?, ?, ?, ?)",
            (client_id, ts, event, json.dumps(clean) if clean else None),
        )
        event_id = cursor.lastrowid
        await db.execute(
            "DELETE FROM client_activity WHERE client_id = ? AND id <= ("
            "SELECT id FROM client_activity WHERE client_id = ? "
            "ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (client_id, client_id, MAX_EVENTS_PER_CLIENT),
        )
    return {
        "id": event_id,
        "client_id": client_id,
        "ts": ts,
        "event": event,
        "detail": clean,
    }


async def record(
    db: Database, client_id: str, event: str, detail: dict | None = None
) -> dict | None:
    """Store an event and tell admin sessions; never raises."""
    try:
        entry = await insert(db, client_id, event, detail)
    except Exception as e:  # noqa: BLE001 - activity is best effort
        logger.warning("Activity %s for %s not recorded: %s", event, client_id, e)
        return None
    if entry is None:
        return None
    try:
        from api import ws as api_ws
        from api.ws import EventAudience

        await api_ws.broadcast(
            {"type": "device_activity", "data": entry}, audience=EventAudience.ADMIN
        )
    except Exception as e:  # noqa: BLE001 - socket gone / shutting down
        logger.debug("device_activity broadcast failed: %s", e)
    return entry


async def list_events(
    db: Database, client_id: str, limit: int = MAX_EVENTS_PER_CLIENT
) -> list[dict]:
    """Newest first, at most ``limit`` (capped to the buffer size)."""
    limit = max(1, min(int(limit), MAX_EVENTS_PER_CLIENT))
    rows = await db.fetchall(
        "SELECT id, client_id, ts, event, detail FROM client_activity "
        "WHERE client_id = ? ORDER BY id DESC LIMIT ?",
        (client_id, limit),
    )
    events = [_row_to_event(r) for r in rows]
    await _current_names(db, events)
    return events


# detail key of another device's id -> key of its name (device_added, guest_invited).
_NAMED_DEVICES = {
    "added_client_id": "added_client_name",
    "guest_client_id": "guest_client_name",
}


async def _current_names(db: Database, events: list[dict]) -> None:
    """Show the current name of a device an entry names (renamed later).

    The stored name stays when that device no longer exists.
    """
    ids = {
        d[key]
        for e in events
        if (d := e.get("detail"))
        for key in _NAMED_DEVICES
        if isinstance(d.get(key), str)
    }
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    rows = await db.fetchall(
        f"SELECT id, name FROM paired_clients WHERE id IN ({marks})",  # noqa: S608 - placeholders only
        tuple(ids),
    )
    names = {r["id"]: r["name"] for r in rows if r.get("name")}
    for event in events:
        detail = event.get("detail")
        if not detail:
            continue
        for id_key, name_key in _NAMED_DEVICES.items():
            name = names.get(detail.get(id_key))
            if name:
                detail[name_key] = name
