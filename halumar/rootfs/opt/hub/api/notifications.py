"""Notification API endpoints for the Halumar admin frontend.

Provides CRUD operations for client notifications:
- GET /notifications — list all active notifications (across all clients)
- GET /notifications/client/{client_id} — list notifications for a specific client
- POST /notifications — send a notification to a client (stores + sends via WS)
- DELETE /notifications/{notification_id} — permanently delete a notification
- POST /notifications/client/{client_id}/clear — dismiss all notifications for a client
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from api import ws as api_ws
from api.auth import require_admin
from api.ws import EventAudience
from data.notification_icons import CURATED_NOTIFICATION_ICONS
from models.database import Database
from models.schemas import SendNotificationRequest
from services.notifications import (
    broadcast_notification_id,
    create_notification,
    clear_client_notifications,
    delete_notification,
    get_client_notifications,
    get_notification,
    is_watch,
    normalize_notification_row,
    notification_recipient,
    resolve_popup_mode,
    trim_active_notifications,
)

logger = logging.getLogger("hub.api.notifications")
router = APIRouter(tags=["notifications"])

WATCH_WITHOUT_PHONE = (
    "A watch shows the notifications of the phone that set it up, "
    "and that phone is no longer paired."
)


@router.get("/notifications")
async def list_notifications(request: Request, status: str = "active"):
    """List notifications across all clients.

    Query params:
      - status: "active" (default, pending+delivered), "all" (include dismissed)

    Admin-only.
    """
    require_admin(request)
    db: Database = request.app.state.db

    if status == "all":
        rows = await db.fetchall(
            "SELECT cn.*, pc.name as client_name "
            "FROM client_notifications cn "
            "JOIN paired_clients pc ON cn.client_id = pc.id "
            "ORDER BY cn.created_at DESC LIMIT 200"
        )
    else:
        rows = await db.fetchall(
            "SELECT cn.*, pc.name as client_name "
            "FROM client_notifications cn "
            "JOIN paired_clients pc ON cn.client_id = pc.id "
            "WHERE cn.status != 'dismissed' "
            "ORDER BY cn.created_at DESC LIMIT 200"
        )

    return {"notifications": [normalize_notification_row(r) for r in rows]}


@router.get("/notifications/icons")
async def list_notification_icons(request: Request):
    """Return the curated Material Design Icon list for the picker.

    These are suggestions only; the create API still accepts any manually
    typed ``mdi:*`` name. Admin-only.
    """
    require_admin(request)
    return {"icons": CURATED_NOTIFICATION_ICONS}


@router.get("/notifications/client/{client_id}")
async def list_client_notifications(
    request: Request, client_id: str, status: str = "active", limit: int = 50
):
    """List notifications for a specific client.

    Query params:
      - status: "active" (default), "all"
      - limit: max results (default 50)

    Admin-only.
    """
    require_admin(request)
    db: Database = request.app.state.db

    active_only = status != "all"
    notifications = await get_client_notifications(db, client_id, active_only, limit)
    return {"notifications": notifications}


async def _deliver_notification(
    db: Database,
    client: dict,
    *,
    title: str,
    message: str,
    notification_type: str,
    icon: str | None,
    icon_svg: str | None,
    popup: bool,
    popup_mode: str | None,
    scheduled_at: str | None,
    priority: int,
    color: str,
    action_type: str,
    action_target: str | None,
    show_time: bool = True,
    notification_id: str | None = None,
    created_at: str | None = None,
    delivered: bool = False,
) -> tuple[dict, bool]:
    """Create + persist a notification for one client and push it via WS.

    Returns (notification, ws_sent). Also broadcasts notification_created to
    admin UIs. Does NOT sync HA sensors (callers do that once at the end).
    A ``delivered`` notification (handed over by the integration, already
    confirmed by the app) is not pushed again. The device keeps at most
    ACTIVE_MAX_PER_CLIENT active ones (keep_rank); the others are dismissed.
    """
    client_id = client["id"]

    notification = await create_notification(
        db,
        client_id=client_id,
        title=title,
        message=message,
        notification_type=notification_type,
        icon=icon,
        icon_svg=icon_svg,
        popup=popup,
        popup_mode=popup_mode,
        scheduled_at=scheduled_at,
        priority=priority,
        color=color,
        action_type=action_type,
        action_target=action_target,
        show_time=show_time,
        notification_id=notification_id,
        created_at=created_at,
        delivered=delivered,
    )

    # Send to client via WebSocket (if connected)
    ws_message = {
        "type": "command",
        "command": "display_notification",
        "notification_id": notification["id"],
        "title": title,
        "message": message,
        "notification_type": notification["notification_type"],
        "popup": notification["popup"],
        "popup_mode": notification["popup_mode"],
        "scheduled_at": notification["scheduled_at"],
        "priority": notification["priority"],
        "color": notification["color"],
        "action_type": notification["action_type"],
        "action_target": notification["action_target"],
        "show_time": notification["show_time"],
        "created_at": notification["created_at"],
    }
    if icon:
        ws_message["icon"] = icon
    if icon_svg:
        ws_message["icon_svg"] = icon_svg

    ws_sent = False if delivered else await api_ws.send_to_client(client_id, ws_message)
    await announce_dismissed(client_id, await trim_active_notifications(db, client_id))

    # Broadcast to admin UIs
    await api_ws.broadcast(
        {
            "type": "notification_created",
            "data": {
                "notification": notification,
                "client_id": client_id,
                "client_name": client["name"],
                "ws_sent": ws_sent,
            },
        },
        audience=EventAudience.ADMIN,
    )

    logger.info(
        "Sent %s notification to client '%s' (ws=%s)",
        notification_type,
        client["name"],
        ws_sent,
    )
    return notification, ws_sent


async def announce_dismissed(client_id: str, notification_ids: list[str]) -> None:
    """The hub dismissed these notifications itself (cap, expiry): tell the
    device and the admin UIs. The caller syncs the HA sensors."""
    for nid in notification_ids:
        await api_ws.send_to_client(
            client_id,
            {
                "type": "command",
                "command": "dismiss_notification",
                "notification_id": nid,
            },
        )
        await api_ws.broadcast(
            {
                "type": "notification_dismissed",
                "data": {"notification_id": nid, "client_id": client_id},
            },
            audience=EventAudience.ADMIN,
        )


def notification_fields(body: SendNotificationRequest) -> dict:
    """Validated create arguments of a notification request (400 if invalid)."""
    title = body.title
    # message is optional (only title is required); coerce None to "".
    message = body.message or ""
    if not title and not message:
        raise HTTPException(status_code=400, detail="title or message is required")

    # popup_mode "direct" consumes an action immediately, so it must have one.
    resolved_mode = resolve_popup_mode(body.popup_mode, body.popup)
    if resolved_mode == "direct" and (
        body.action_type == "none" or not body.action_target
    ):
        raise HTTPException(
            status_code=400,
            detail="direct requires an area/entity/url link",
        )

    icon = body.icon
    icon_svg = body.icon_svg

    # Auto-resolve MDI icons to SVG path data
    if icon and icon.startswith("mdi:") and not icon_svg:
        from data.mdi_paths import get_mdi_path

        icon_svg = get_mdi_path(icon[4:])

    return dict(
        title=title,
        message=message,
        notification_type=body.notification_type,
        icon=icon,
        icon_svg=icon_svg,
        popup=body.popup,
        popup_mode=body.popup_mode,
        scheduled_at=body.scheduled_at,
        priority=body.priority,
        color=body.color,
        action_type=body.action_type,
        action_target=body.action_target,
        show_time=body.show_time,
    )


async def deliver_notification_request(
    request: Request, body: SendNotificationRequest
) -> dict:
    """Validate and deliver a SendNotificationRequest (one client or "all").

    Shared implementation behind POST /notifications (admin) and
    POST /integration/notify (integration token) — callers enforce auth.
    Returns {"sent": N} for the "all" broadcast, otherwise
    {"notification": ..., "ws_sent": ...}.

    With a ``notification_id`` (integration only) the call is idempotent: an
    existing notification of the same client is returned with
    ``duplicate: true`` and not pushed again; an id of another client is 409.

    A watch never gets a notification of its own: it goes to the phone that
    set the watch up (``forwarded_to`` in the answer), 422 when that phone is
    gone; "all" leaves watches out.
    """
    db: Database = request.app.state.db

    client_id = body.client_id
    deliver_kwargs = notification_fields(body)
    notification_id = getattr(body, "notification_id", None)

    # Broadcast path: deliver to every active paired client. A broadcast id
    # (integration) gives each device a derived, stable id: devices that
    # already have theirs are skipped (retry after a timeout).
    # Watches are left out: they show the notifications of their phone.
    if client_id == "all":
        clients = await db.fetchall(
            "SELECT id, name, platform FROM paired_clients WHERE is_active = 1"
        )
        sent = 0
        duplicates = 0
        for client in clients:
            if is_watch(client):
                continue
            own_id = None
            if notification_id:
                own_id = broadcast_notification_id(notification_id, client["id"])
                if await get_notification(db, own_id):
                    duplicates += 1
                    continue
            await _deliver_notification(
                db, dict(client), notification_id=own_id, **deliver_kwargs
            )
            sent += 1

        if sent:
            await _sync_notification_sensors(request, db)

        logger.info(
            "Broadcast notification to %d active client(s), %d duplicate(s)",
            sent,
            duplicates,
        )
        if notification_id:
            return {"sent": sent, "duplicates": duplicates}
        return {"sent": sent}

    # Single-client path.
    client = await db.fetchone(
        "SELECT id, name, is_active, platform, set_up_by_client_id "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    # A watch has no hub channel: its notification goes to its phone, and the
    # answer says so (``forwarded_to``) instead of a notification that waits
    # forever.
    recipient = await notification_recipient(db, dict(client))
    if recipient is None:
        raise HTTPException(status_code=422, detail=WATCH_WITHOUT_PHONE)
    forwarded = recipient["id"] != client_id

    if notification_id:
        existing = await get_notification(db, notification_id)
        if existing:
            if existing.get("client_id") != recipient["id"]:
                raise HTTPException(
                    status_code=409,
                    detail="notification_id belongs to another client",
                )
            result = {"notification": existing, "ws_sent": False, "duplicate": True}
            if forwarded:
                result["forwarded_to"] = _forward_info(recipient)
            return result

    notification, ws_sent = await _deliver_notification(
        db, dict(recipient), notification_id=notification_id, **deliver_kwargs
    )

    await _sync_notification_sensors(request, db)

    result = {
        "notification": notification,
        "ws_sent": ws_sent,
    }
    if forwarded:
        logger.info(
            "Notification for watch '%s' forwarded to its phone '%s'",
            client["name"],
            recipient["name"],
        )
        result["forwarded_to"] = _forward_info(recipient)
    return result


def _forward_info(phone: dict) -> dict:
    return {"client_id": phone["id"], "name": phone["name"]}


@router.post("/notifications")
async def send_notification(request: Request, body: SendNotificationRequest):
    """Create and send a notification to a client.

    Body: {
        "client_id": "uuid",            // or "all" (every device except watches)
        "title": "Alert",
        "message": "Something happened",
        "notification_type": "normal",  // "normal", "sticky", "permanent"
        "icon": "mdi:bell"              // optional
    }

    Creates the notification in the DB, then sends it to the client via
    WebSocket if connected. Broadcasts notification_created to admin UIs.

    A watch has no hub connection: a notification for a watch goes to the
    phone that set it up (its system shows it on the watch while the phone is
    locked); the response names that phone in ``forwarded_to`` and is 422
    when that phone is no longer paired.

    When ``client_id`` is "all", the notification is created and delivered to
    every active paired client except watches (they get it through their
    phone); the response is a summary ({"sent": N}).

    popup_mode "direct" immediately opens the linked area/entity/url on the
    client and therefore REQUIRES an action (action_type != "none" with an
    action_target).

    Admin-only.
    """
    require_admin(request)
    return await deliver_notification_request(request, body)


async def _sync_notification_sensors(request: Request, db: Database) -> None:
    """Best-effort sync of the HA notification sensors."""
    try:
        from services.ha_sensors import sync_notification_sensors_safe

        ha_client = request.app.state.ha_client
        await sync_notification_sensors_safe(db, ha_client)
    except Exception:
        pass


@router.delete("/notifications/{notification_id}", status_code=204)
async def remove_notification(request: Request, notification_id: str):
    """Permanently delete a notification and dismiss it on the client.

    Sends dismiss_notification command to the client via WebSocket (if connected).
    Admin-only.
    """
    require_admin(request)
    db: Database = request.app.state.db

    notification = await get_notification(db, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    client_id = notification["client_id"]

    await delete_notification(db, notification_id)

    # Send dismiss command to client (best-effort, client may be offline)
    await api_ws.send_to_client(
        client_id,
        {
            "type": "command",
            "command": "dismiss_notification",
            "notification_id": notification_id,
        },
    )

    # Broadcast to admin UIs
    await api_ws.broadcast(
        {
            "type": "notification_deleted",
            "data": {
                "notification_id": notification_id,
                "client_id": client_id,
            },
        },
        audience=EventAudience.ADMIN,
    )

    # Sync HA notification sensors
    try:
        from services.ha_sensors import sync_notification_sensors_safe

        ha_client = request.app.state.ha_client
        await sync_notification_sensors_safe(db, ha_client)
    except Exception:
        pass


@router.post("/notifications/client/{client_id}/clear", status_code=200)
async def clear_notifications(request: Request, client_id: str):
    """Dismiss all active notifications for a client.

    Also sends clear_notifications command to the client via WebSocket.
    Admin-only.
    """
    require_admin(request)
    db: Database = request.app.state.db

    # Verify client exists
    client = await db.fetchone(
        "SELECT id, name FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    count = await clear_client_notifications(db, client_id)

    # Send clear command to client via WebSocket
    ws_sent = await api_ws.send_to_client(
        client_id,
        {
            "type": "command",
            "command": "clear_notifications",
        },
    )

    # Broadcast to admin UIs
    await api_ws.broadcast(
        {
            "type": "notifications_cleared",
            "data": {
                "client_id": client_id,
                "client_name": client["name"],
                "count": count,
            },
        },
        audience=EventAudience.ADMIN,
    )

    # Sync HA notification sensors
    try:
        from services.ha_sensors import sync_notification_sensors_safe

        ha_client = request.app.state.ha_client
        await sync_notification_sensors_safe(db, ha_client)
    except Exception:
        pass

    return {"cleared": count, "ws_sent": ws_sent}
