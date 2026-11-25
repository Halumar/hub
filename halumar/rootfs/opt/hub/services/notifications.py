"""Persistent notification service for Halumar clients.

Stores notifications in SQLite (client_notifications table) so they
survive hub restarts and can be polled by the HA integration.

Notification lifecycle:
1. Created via REST API or integration endpoint -> status='pending'
2. Sent to client via WebSocket -> still 'pending' (WS is fire-and-forget)
3. Client sends notification_ack -> status='delivered', delivered_at set
4. Client dismisses or notification expires -> status='dismissed', dismissed_at set
5. Admin clears all -> status='dismissed' for all active notifications

Status values:
- pending: Created but not yet confirmed by client
- delivered: Client acknowledged receipt (notification_ack)
- dismissed: Cleared, expired, or dismissed by user

delivered_via (set alongside status='delivered') records which path confirmed
delivery: 'ws' (hub WebSocket ack) or 'remote' (HA-service fallback ack via
the integration). NULL for undelivered rows and rows stored before the column
existed.

Notification types (canonical, shared addon<->app contract):
- default: user can dismiss.
- sticky: user cannot dismiss; stays until the hub clears it.

Alternative values are accepted on input and normalized on read/write:
  normal -> default, permanent -> sticky, sticky -> sticky.

Extra fields (shared contract):
- popup (bool): True => classic centered in-app popup; False => native banner.
- scheduled_at (ISO-8601 UTC | None): when set, the app schedules a LOCAL
  device notification for that time. The hub still DELIVERS the notification
  immediately (online -> now, offline -> pending); it does NOT hold delivery.
"""

import logging
import uuid
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.services.notifications")

# Canonical notification types.
NOTIFICATION_TYPE_DEFAULT = "default"
NOTIFICATION_TYPE_STICKY = "sticky"

# Alternative -> canonical mapping. Unknown/empty values fall back to "default".
_LEGACY_TYPE_MAP = {
    "normal": NOTIFICATION_TYPE_DEFAULT,
    "default": NOTIFICATION_TYPE_DEFAULT,
    "permanent": NOTIFICATION_TYPE_STICKY,
    "sticky": NOTIFICATION_TYPE_STICKY,
}

# App-side presentation extension (shared addon<->app contract).
PRIORITY_MIN = 0
PRIORITY_MAX = 100
PRIORITY_DEFAULT = 50

# Bounds of the active notifications of one device, the same for the HA
# sensor and the hub socket and for the integration's queue
# while the hub is down (protocol.md): 50 (which ones: keep_rank); one that is
# not sticky ends 72 h after it was created or became due (scheduled_at). A
# sticky one stays until it is cleared.
ACTIVE_MAX_PER_CLIENT = 50
ACTIVE_MAX_AGE_HOURS = 72

# Allowed accent colors. "none" => no chip/accent.
NOTIFICATION_COLORS = ("none", "red", "blue", "green", "yellow", "orange")
NOTIFICATION_COLOR_DEFAULT = "none"

# Tap action kinds. "none" => not tappable. "area"/"entity" => in-app deep
# link (action_target is an area_id/entity_id); "url" => external link.
NOTIFICATION_ACTION_TYPES = ("none", "area", "entity", "url")
NOTIFICATION_ACTION_TYPE_DEFAULT = "none"

# Popup presentation modes (shared addon<->app contract):
#   none   => inline banner only (no modal).
#   popup  => centered modal only.
#   both   => inline banner + centered modal (extra attention).
#   direct => the app immediately OPENS the linked area/entity/url when the
#             notification is displayed and consumes it (one-time, no card or
#             popup). REQUIRES an action link (action_type != "none").
# The ``popup`` bool is emitted and accepted alongside, as
# (popup_mode != "none").
NOTIFICATION_POPUP_MODES = ("none", "popup", "both", "direct")
NOTIFICATION_POPUP_MODE_DEFAULT = "none"


def broadcast_notification_id(broadcast_id: str, client_id: str) -> str:
    """Id of one device's notification of a broadcast with a stable id.

    UUIDv5 with the broadcast id as namespace and the client id (lowercase
    text) as name; the integration derives the same ids for its fallback, so a
    broadcast that timed out but reached the hub is handed over as duplicates
    (docs/protocol.md of the contract).
    """
    return str(uuid.uuid5(uuid.UUID(broadcast_id), client_id.lower()))


def clamp_priority(value: Any) -> int:
    """Clamp an arbitrary priority value into the 0..100 range.

    Non-integer / missing values fall back to the default (50).
    """
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return PRIORITY_DEFAULT
    return max(PRIORITY_MIN, min(PRIORITY_MAX, ivalue))


def canonical_color(value: Any) -> str:
    """Map an arbitrary color value to a valid color, default "none"."""
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in NOTIFICATION_COLORS else NOTIFICATION_COLOR_DEFAULT


def canonical_action_type(value: Any) -> str:
    """Map an arbitrary action_type to a valid value, default "none"."""
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in NOTIFICATION_ACTION_TYPES else NOTIFICATION_ACTION_TYPE_DEFAULT


def canonical_popup_mode(value: Any) -> str:
    """Map an arbitrary popup_mode to a valid value, default "none".

    Accepts "none"/"popup"/"both"/"direct" (case-insensitive). Unknown/missing
    values fall back to "none".
    """
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if v in NOTIFICATION_POPUP_MODES else NOTIFICATION_POPUP_MODE_DEFAULT


def resolve_popup_mode(popup_mode: Any, popup: Any = False) -> str:
    """Resolve the canonical popup_mode from a value plus the popup bool.

    ``popup_mode`` is canonicalized to "none"/"popup"/"both"/"direct". The
    ``popup`` bool is then folded in: when the resolved
    mode is "none" but ``popup`` is truthy, the result is upgraded to "popup".
    This lets
    a caller that sends only ``popup=true`` (and the default
    ``popup_mode="none"``) still get a modal.
    """
    mode = canonical_popup_mode(popup_mode)
    if mode == "none" and popup:
        return "popup"
    return mode


def canonical_notification_type(value: str | None) -> str:
    """Map any accepted notification_type to its canonical value.

    normal -> default, permanent -> sticky, sticky -> sticky, default -> default.
    Unknown or missing values fall back to "default".
    """
    return _LEGACY_TYPE_MAP.get(
        (value or "").strip().lower(), NOTIFICATION_TYPE_DEFAULT
    )


def normalize_notification_row(row: Any) -> dict[str, Any]:
    """Convert a DB row (dict/sqlite Row) into a normalized notification dict.

    Normalizes ``notification_type`` to its canonical value, coerces ``popup``
    to a bool, and ensures ``scheduled_at`` is present (None when unset).
    Leaves any extra keys (e.g. joined ``client_name``) untouched.
    """
    d = dict(row)
    d["notification_type"] = canonical_notification_type(d.get("notification_type"))
    # popup_mode is authoritative. A row without popup_mode (None) or with the
    # default "none" but popup=1 gets the popup bool folded in
    # (true -> "popup"). The popup bool is then re-derived as
    # (popup_mode != "none") so the two always agree on read.
    d["popup_mode"] = resolve_popup_mode(d.get("popup_mode"), bool(d.get("popup") or 0))
    d["popup"] = d["popup_mode"] != NOTIFICATION_POPUP_MODE_DEFAULT
    d["scheduled_at"] = d.get("scheduled_at")
    d["priority"] = clamp_priority(
        d["priority"] if d.get("priority") is not None else PRIORITY_DEFAULT
    )
    d["color"] = canonical_color(d.get("color"))
    d["action_type"] = canonical_action_type(d.get("action_type"))
    d["action_target"] = d.get("action_target")
    # Per-notification "show send time" flag (default on). Stored as 0/1; a row
    # without a value (None) defaults to True.
    raw_show_time = d.get("show_time")
    d["show_time"] = True if raw_show_time is None else bool(raw_show_time)
    return d


def notification_to_push_payload(notification: dict[str, Any]) -> dict[str, Any]:
    """Convert a normalized notification dict into the display_notification
    push payload shape (the same fields the real-time WS push carries, minus
    the type/command wrapper).

    This is the item shape of BOTH the WS content channel response
    ({"type": "notifications", "notifications": [...]}) and the
    ``notifications`` attribute of the per-client HA notification sensor
    (services/ha_sensors.py) — one serializer, so the WS and sensor shapes
    can never drift and clients reuse their push parsing on either path.
    ``icon``/``icon_svg`` are included only when set (matching the push);
    ``status`` is added so clients can tell pending from delivered.
    """
    payload: dict[str, Any] = {
        "notification_id": notification["id"],
        "title": notification.get("title", ""),
        "message": notification.get("message", ""),
        "notification_type": notification["notification_type"],
        "popup": notification["popup"],
        "popup_mode": notification["popup_mode"],
        "scheduled_at": notification.get("scheduled_at"),
        "priority": notification["priority"],
        "color": notification["color"],
        "action_type": notification["action_type"],
        "action_target": notification.get("action_target"),
        "show_time": notification["show_time"],
        "created_at": notification.get("created_at"),
        "status": notification.get("status"),
    }
    if notification.get("icon"):
        payload["icon"] = notification["icon"]
    if notification.get("icon_svg"):
        payload["icon_svg"] = notification["icon_svg"]
    return payload


async def create_notification(
    db: Database,
    client_id: str,
    title: str = "",
    message: str | None = "",
    notification_type: str = "default",
    icon: str | None = None,
    icon_svg: str | None = None,
    popup: bool = False,
    popup_mode: str | None = None,
    scheduled_at: str | None = None,
    priority: int = PRIORITY_DEFAULT,
    color: str = NOTIFICATION_COLOR_DEFAULT,
    action_type: str = NOTIFICATION_ACTION_TYPE_DEFAULT,
    action_target: str | None = None,
    show_time: bool = True,
    notification_id: str | None = None,
    created_at: str | None = None,
    delivered: bool = False,
) -> dict[str, Any]:
    """Create a notification and store it in the database.

    ``notification_type`` also accepts normal and permanent and is
    persisted as its canonical form (default/sticky). ``scheduled_at`` is
    stored as-is and delivered immediately like any notification — there is no
    server-side hold-until-time scheduler.

    ``notification_id``/``created_at`` are given by the integration (stable id
    of a notify call, or a notification it queued while the hub was
    unreachable); ``delivered`` stores one the app already confirmed through
    Home Assistant (delivered_via='remote').

    Returns the created notification as a normalized dict.
    """
    notification_id = notification_id or str(uuid.uuid4())
    now = created_at or Database.now()
    status = "delivered" if delivered else "pending"
    delivered_at = Database.now() if delivered else None
    delivered_via = "remote" if delivered else None

    # message is optional (only title is required); coerce None to "" so the
    # NOT NULL column is satisfied.
    message = message or ""

    ntype = canonical_notification_type(notification_type)
    # popup_mode is authoritative; the popup bool is derived from it so the
    # two never disagree. If popup_mode is absent, derive it from the popup
    # bool (True -> "popup").
    pmode = resolve_popup_mode(popup_mode, popup)
    popup_bool = pmode != NOTIFICATION_POPUP_MODE_DEFAULT
    popup_int = 1 if popup_bool else 0
    prio = clamp_priority(priority)
    col = canonical_color(color)
    atype = canonical_action_type(action_type)
    atarget = action_target or None
    show_time_bool = bool(show_time)
    show_time_int = 1 if show_time_bool else 0

    async with db.transaction():
        await db.execute(
            """INSERT INTO client_notifications
               (id, client_id, title, message, notification_type, icon, icon_svg,
                popup, popup_mode, scheduled_at, priority, color, action_type,
                action_target, show_time, status, created_at, delivered_at,
                delivered_via)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                notification_id,
                client_id,
                title,
                message,
                ntype,
                icon,
                icon_svg,
                popup_int,
                pmode,
                scheduled_at,
                prio,
                col,
                atype,
                atarget,
                show_time_int,
                status,
                now,
                delivered_at,
                delivered_via,
            ),
        )

    notification = {
        "id": notification_id,
        "client_id": client_id,
        "title": title,
        "message": message,
        "notification_type": ntype,
        "icon": icon,
        "icon_svg": icon_svg,
        "popup": popup_bool,
        "popup_mode": pmode,
        "scheduled_at": scheduled_at,
        "priority": prio,
        "color": col,
        "action_type": atype,
        "action_target": atarget,
        "show_time": show_time_bool,
        "status": status,
        "created_at": now,
        "delivered_at": delivered_at,
        "delivered_via": delivered_via,
        "dismissed_at": None,
    }

    logger.info(
        "Created %s notification %s for client %s "
        "(popup_mode=%s, popup=%s, scheduled_at=%s)",
        ntype,
        notification_id,
        client_id,
        pmode,
        popup_bool,
        scheduled_at,
    )
    return notification


async def acknowledge_notification(
    db: Database, notification_id: str, delivered_via: str = "ws"
) -> bool:
    """Mark a notification as delivered (client acknowledged receipt).

    ``delivered_via`` records WHICH path confirmed delivery: "ws" (the hub
    WebSocket notification_ack — default) or "remote" (the HA-service fallback
    ack forwarded by the integration). Only pending rows flip, so the first
    confirmation wins and a duplicate/racing ack is a no-op (idempotent).

    Returns True if the notification was found and updated.
    """
    now = Database.now()
    async with db.transaction():
        result = await db.execute(
            "UPDATE client_notifications "
            "SET status = 'delivered', delivered_at = ?, delivered_via = ? "
            "WHERE id = ? AND status = 'pending'",
            (now, delivered_via, notification_id),
        )
    updated = result.rowcount > 0 if hasattr(result, "rowcount") else True
    if updated:
        logger.debug(
            "Notification %s acknowledged (via=%s)", notification_id, delivered_via
        )
    return updated


async def dismiss_notification(db: Database, notification_id: str) -> bool:
    """Mark a single notification as dismissed.

    Returns True if the notification was found and updated.
    """
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "UPDATE client_notifications SET status = 'dismissed', dismissed_at = ? "
            "WHERE id = ? AND status != 'dismissed'",
            (now, notification_id),
        )
    return True


async def clear_client_notifications(db: Database, client_id: str) -> int:
    """Dismiss all active notifications for a client.

    Returns the number of notifications dismissed.
    """
    now = Database.now()
    async with db.transaction():
        cursor = await db.execute(
            "UPDATE client_notifications SET status = 'dismissed', dismissed_at = ? "
            "WHERE client_id = ? AND status != 'dismissed'",
            (now, client_id),
        )
    count = cursor.rowcount if hasattr(cursor, "rowcount") else 0
    logger.info("Cleared %d notifications for client %s", count, client_id)
    return count


# Which active notifications a device keeps when there are more than
# ACTIVE_MAX_PER_CLIENT: sticky before not sticky, higher priority first, then
# the newest. A sticky or high-priority notice is never pushed out by lower ones.
_KEEP_ORDER_SQL = (
    "(notification_type = 'sticky') DESC, priority DESC, created_at DESC, id DESC"
)


def keep_rank(item: dict[str, Any]) -> tuple:
    """Sort key (descending) of :data:`_KEEP_ORDER_SQL` for a row or a push
    item (sensor)."""
    try:
        priority = int(item.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    return (
        item.get("notification_type") == NOTIFICATION_TYPE_STICKY,
        priority,
        str(item.get("created_at") or ""),
        str(item.get("id") or item.get("notification_id") or ""),
    )


def bounded(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most ACTIVE_MAX_PER_CLIENT items, the ones kept by :func:`keep_rank`,
    newest first."""
    if len(items) > ACTIVE_MAX_PER_CLIENT:
        items = sorted(items, key=keep_rank, reverse=True)[:ACTIVE_MAX_PER_CLIENT]
    return sorted(
        items,
        key=lambda n: (
            str(n.get("created_at") or ""),
            str(n.get("id") or n.get("notification_id") or ""),
        ),
        reverse=True,
    )


async def trim_active_notifications(db: Database, client_id: str) -> list[str]:
    """Dismiss the active notifications of ``client_id`` beyond
    :data:`ACTIVE_MAX_PER_CLIENT`: not sticky, lowest priority and oldest go
    first. Returns their ids."""
    rows = await db.fetchall(
        "SELECT id FROM client_notifications "
        "WHERE client_id = ? AND status != 'dismissed' "
        f"ORDER BY {_KEEP_ORDER_SQL} LIMIT -1 OFFSET ?",
        (client_id, ACTIVE_MAX_PER_CLIENT),
    )
    ids = [r["id"] for r in rows]
    if ids:
        await _dismiss_ids(db, ids)
        logger.info(
            "Dismissed %d notification(s) of client %s beyond %d",
            len(ids),
            client_id,
            ACTIVE_MAX_PER_CLIENT,
        )
    return ids


async def expire_active_notifications(
    db: Database, hours: int = ACTIVE_MAX_AGE_HOURS
) -> dict[str, list[str]]:
    """Dismiss active notifications (not sticky) older than ``hours``, counted
    from ``scheduled_at`` when that is later than ``created_at`` (a reminder
    scheduled days ahead stays until it is due and then ``hours`` more).

    Returns the dismissed ids per client.
    """
    rows = await db.fetchall(
        "SELECT id, client_id FROM client_notifications "
        "WHERE status != 'dismissed' AND notification_type != ? "
        "AND datetime(created_at) < datetime('now', ?) "
        "AND (datetime(scheduled_at) IS NULL "
        "OR datetime(scheduled_at) < datetime('now', ?))",
        (NOTIFICATION_TYPE_STICKY, f"-{hours} hours", f"-{hours} hours"),
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["client_id"], []).append(r["id"])
    if rows:
        await _dismiss_ids(db, [r["id"] for r in rows])
        logger.info("Expired %d notification(s) older than %d h", len(rows), hours)
    return out


async def _dismiss_ids(db: Database, ids: list[str]) -> None:
    now = Database.now()
    async with db.transaction():
        for nid in ids:
            await db.execute(
                "UPDATE client_notifications SET status = 'dismissed', "
                "dismissed_at = ? WHERE id = ? AND status != 'dismissed'",
                (now, nid),
            )


async def get_client_notifications(
    db: Database,
    client_id: str,
    active_only: bool = True,
    limit: int = ACTIVE_MAX_PER_CLIENT,
) -> list[dict[str, Any]]:
    """Get notifications for a client.

    Args:
        client_id: The paired client UUID.
        active_only: If True, only return pending/delivered (not dismissed).
        limit: Maximum number of notifications to return.

    Returns list of notification dicts, newest first.
    """
    if active_only:
        rows = await db.fetchall(
            "SELECT * FROM client_notifications "
            "WHERE client_id = ? AND status != 'dismissed' "
            f"ORDER BY {_KEEP_ORDER_SQL} LIMIT ?",
            (client_id, limit),
        )
        return bounded([normalize_notification_row(r) for r in rows])
    else:
        rows = await db.fetchall(
            "SELECT * FROM client_notifications "
            "WHERE client_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (client_id, limit),
        )
    return [normalize_notification_row(r) for r in rows]


async def get_active_notifications_for_all_clients(
    db: Database,
) -> dict[str, list[dict[str, Any]]]:
    """Get active notifications grouped by client_id.

    Used by /integration/status and the HA sensors; the same at most
    :data:`ACTIVE_MAX_PER_CLIENT` per client as the hub socket, newest first.
    Returns: {client_id: [notification, ...], ...}
    """
    rows = await db.fetchall(
        "SELECT * FROM client_notifications WHERE status != 'dismissed'"
    )
    result: dict[str, list[dict]] = {}
    for r in rows:
        result.setdefault(r["client_id"], []).append(normalize_notification_row(r))
    return {cid: bounded(items) for cid, items in result.items()}


def is_watch(client: dict[str, Any] | None) -> bool:
    """A watch (``watchos``, ``wearos``): no hub socket, no notification view."""
    from services.onboarding import WATCH_PLATFORMS

    return bool(client) and client.get("platform") in WATCH_PLATFORMS


async def notification_recipient(
    db: Database, client: dict[str, Any]
) -> dict[str, Any] | None:
    """The device that shows notifications addressed to ``client``.

    A watch never connects to the hub, so a notification stored for it would
    wait forever. Its notifications go to the phone that set it up; the
    phone's operating system shows them on the watch while the phone is
    locked (notification forwarding). ``None`` when that phone is no longer
    paired and active.
    """
    if not is_watch(client):
        return client
    phone_id = client.get("set_up_by_client_id")
    if not phone_id:
        return None
    phone = await db.fetchone(
        "SELECT id, name, is_active, platform, pending_deletion_at "
        "FROM paired_clients WHERE id = ?",
        (phone_id,),
    )
    if (
        not phone
        or not phone.get("is_active")
        or phone.get("pending_deletion_at")
        or is_watch(phone)
    ):
        return None
    return phone


async def get_notification(db: Database, notification_id: str) -> dict[str, Any] | None:
    """Get a single notification by ID."""
    row = await db.fetchone(
        "SELECT * FROM client_notifications WHERE id = ?",
        (notification_id,),
    )
    return normalize_notification_row(row) if row else None


async def delete_notification(db: Database, notification_id: str) -> bool:
    """Permanently delete a notification from the database."""
    async with db.transaction():
        await db.execute(
            "DELETE FROM client_notifications WHERE id = ?",
            (notification_id,),
        )
    return True


async def cleanup_old_notifications(db: Database, hours: int = 72) -> int:
    """Delete dismissed notifications older than `hours` hours.

    Called periodically to prevent database bloat.
    Returns the number of deleted rows.
    """
    async with db.transaction():
        cursor = await db.execute(
            "DELETE FROM client_notifications "
            "WHERE status = 'dismissed' "
            "AND datetime(dismissed_at) < datetime('now', ?)",
            (f"-{hours} hours",),
        )
    count = cursor.rowcount if hasattr(cursor, "rowcount") else 0
    if count > 0:
        logger.info("Cleaned up %d old notifications", count)
    return count
