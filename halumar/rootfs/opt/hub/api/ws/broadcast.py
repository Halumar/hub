"""Fan-out of /api/ws: audiences, broadcast, targeted sends, ping loop."""

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any

from fastapi import WebSocket

from api import ws as api_ws
from api.ws.registry import (
    INTEGRATION_CLIENT_ID,
    PSEUDO_CLIENT_IDS,
    _clients,
    _is_admin_client,
    deliver,
    drop_connection,
    flush,
    get_connected_client_ids,
    queue_send,
)

logger = logging.getLogger("hub.api.websocket")


# ── Broadcast audience ───────────────────────────────────────────────────────


class EventAudience(str, Enum):
    """Target audience for broadcast events.

    ALL:   Every authenticated client (wall panels, iOS apps, admin UI).
           Used for data changes that all clients need (area_updated, layout_updated,
           client_config_updated).
    ADMIN: Only admin sessions (ingress, dev-mode). External paired clients
           are excluded. Used for management events that only the admin WebUI
           cares about (area_created, client_paired, notification_created, etc.).
    """

    ALL = "all"
    ADMIN = "admin"


# Server-initiated ping interval and timeout for dead connection detection
WS_PING_INTERVAL = 30  # seconds between pings
WS_PING_TIMEOUT = 10  # seconds to wait for pong before considering dead

# Grace period after signaling client_removed before force-closing (4001) a
# live connection that never sent client_reset_ack (e.g. an older app).
RESET_ACK_GRACE_SECONDS = 10


# Admin-UI-only broadcasts: never to the integration (the log tab's entries
# carry names, and every entry would make it re-poll the status).
_NOT_FOR_INTEGRATION = frozenset({"log_entry", "log_read"})


def _integration_view(message: dict[str, Any]) -> dict[str, Any]:
    """Project a broadcast message onto the integration's PII contract.

    The integration push session receives every ALL/ADMIN event UNCHANGED
    with exactly one redaction: ``notification_created`` is reduced to
    ids+counts (client_id / notification_id / ws_sent — never title, message
    or action content), matching the REST snapshot contract of
    /api/integration/* (see api/integration.py). Events are pure refresh
    triggers for the integration — it re-polls the status snapshot.
    """
    if message.get("type") != "notification_created":
        return message
    data = message.get("data") or {}
    notification = data.get("notification") or {}
    return {
        "type": "notification_created",
        "data": {
            "client_id": data.get("client_id"),
            "notification_id": notification.get("id"),
            "ws_sent": data.get("ws_sent"),
        },
    }


async def broadcast(
    message: dict[str, Any],
    audience: EventAudience = EventAudience.ALL,
) -> None:
    """Broadcast a message to authenticated WebSocket clients.

    Uses list() snapshot of _clients to avoid RuntimeError when a client
    disconnects (and is removed from _clients) during iteration.

    The integration push session is special-cased: it receives BOTH ALL and
    ADMIN events (it is a read-only observer, not an admin session), each
    projected through _integration_view (notification_created redacted to
    ids — the only PII-carrying event).

    Args:
        message: The message dict to broadcast.
        audience: Who should receive this message.
            ALL   — every authenticated client (default, backward-compatible).
            ADMIN — only admin sessions (ingress / dev-mode connections).
                    External paired clients are skipped.
    """
    # Quick access check sensors follow every layout/config change.
    from services import enforcement, quick_access, state_relay, watch_access

    quick_access.notify_change(message.get("type"))
    enforcement.notify_change(message.get("type"))
    # Areas, placements, profiles or access may have changed: the relay
    # checks every scope again before it sends the next state.
    state_relay.rescope()
    if message.get("type") == "client_config_updated":
        # Watches have no socket: their phones get the sealed access.
        watch_access.notify(None)
    if not _clients:
        logger.debug(
            "[broadcast] No clients connected, skipping: %s", message.get("type")
        )
        return

    data = json.dumps(message)
    # Lazily-serialized redacted view for integration session(s).
    integration_data: str | None = None
    targets = []
    for client, info in list(_clients.items()):
        if not info.get("authenticated"):
            continue
        if info.get("client_id") == INTEGRATION_CLIENT_ID:
            # Integration session: gets ALL + ADMIN events, redacted.
            if message.get("type") in _NOT_FOR_INTEGRATION:
                continue
            if integration_data is None:
                integration_data = json.dumps(_integration_view(message))
            targets.append((client, info, integration_data))
            continue
        # Apply audience filter
        if audience == EventAudience.ADMIN:
            if not _is_admin_client(info.get("client_id")):
                continue
        targets.append((client, info, data))

    if not targets:
        logger.debug(
            "[broadcast] No %s clients, skipping: %s",
            audience.value,
            message.get("type"),
        )
        return

    sent_count = await deliver([(ws, payload) for ws, _info, payload in targets])

    logger.info(
        "[broadcast] type=%s audience=%s sent to %d client(s)",
        message.get("type"),
        audience.value,
        sent_count,
    )


async def send_to_client(client_id: str, message: dict[str, Any]) -> bool:
    """Send a message to a specific paired client by client_id.

    Returns True if at least one WebSocket received the message.
    Used by the integration API to send commands to specific clients
    (e.g. navigate_to_area, show_entity, display_notification).
    """
    if message.get("type") == "client_config_updated" and client_id:
        # A watch has no socket: its phone gets the sealed access. The
        # device's live states follow its scope from the next state on.
        from services import state_relay, watch_access

        watch_access.notify(client_id)
        state_relay.rescope(client_id)
    if not _clients or not client_id:
        return False

    data = json.dumps(message)
    targets = [
        (ws, data)
        for ws, info in list(_clients.items())
        if info.get("authenticated") and info.get("client_id") == client_id
    ]
    return await deliver(targets) > 0


async def notify_areas_changed(db, area: dict | None = None) -> int:
    """Tell paired apps that their area list may have changed.

    ``areas_changed`` (no payload) goes to every authenticated app session
    (not admin, not integration, not in its reset grace period) whose list
    can contain ``area`` (``{"id": …, "is_active": …, "tags": [...]}``, ``id``
    for area pins) by the rule
    of ``get_areas``; without ``area`` (bulk delete) to every app. Admin
    sessions keep area_created / area_deleted / areas_deleted. Returns the
    number of sessions notified.
    """
    from api import authz
    from services import state_relay

    # The area list changed: every live-state subscription follows its
    # scope from the next state on (also when the area was switched off).
    state_relay.rescope()
    if area is not None and not area.get("is_active", True):
        return 0
    data = json.dumps({"type": "areas_changed", "data": {}})
    scopes: dict[str, bool] = {}
    targets: list[tuple[WebSocket, str]] = []
    for ws, info in list(_clients.items()):
        client_id = info.get("client_id")
        if (
            not info.get("authenticated")
            or not client_id
            or client_id in PSEUDO_CLIENT_IDS
            or _is_admin_client(client_id)
            or info.get("reset_signaled")
        ):
            continue
        if area is not None:
            if client_id not in scopes:
                scope = await authz.client_area_scope(db, client_id)
                scopes[client_id] = authz.area_in_scope(
                    scope, list(area.get("tags") or []), area.get("id")
                )
            if not scopes[client_id]:
                continue
        targets.append((ws, data))
    sent = await deliver(targets)
    if sent:
        logger.info("[broadcast] type=areas_changed sent to %d app session(s)", sent)
    return sent


async def broadcast_area_scoped(db, area_id: str, message: dict[str, Any]) -> int:
    """Send ``message`` to the sessions affected by a change in ``area_id``.

    Admin sessions and the integration (unchanged, like ADMIN/ALL events)
    always; a paired app only if it may read the area (``authz.can_read_area``:
    listed or deep-linked by its notifications) and is not in its reset grace
    period. Used for ``entity_updated`` (tile settings). Returns the
    number of sessions reached.
    """
    from api import authz
    from services import enforcement, quick_access, state_relay

    # A placement may have been hidden or the layout replaced.
    state_relay.rescope()

    quick_access.notify_change(message.get("type"))
    enforcement.notify_change(message.get("type"))
    data = json.dumps(message)
    integration_data: str | None = None
    readable: dict[str, bool] = {}
    targets = []
    for ws, info in list(_clients.items()):
        client_id = info.get("client_id")
        if not info.get("authenticated") or not client_id:
            continue
        if client_id == INTEGRATION_CLIENT_ID:
            if integration_data is None:
                integration_data = json.dumps(_integration_view(message))
            targets.append((ws, integration_data))
            continue
        if _is_admin_client(client_id):
            targets.append((ws, data))
            continue
        if info.get("reset_signaled"):
            continue
        if client_id not in readable:
            readable[client_id] = await authz.can_read_area(db, client_id, area_id)
        if readable[client_id]:
            targets.append((ws, data))
    sent = await deliver(targets)
    logger.info(
        "[broadcast] type=%s area-scoped sent to %d session(s)",
        message.get("type"),
        sent,
    )
    return sent


async def announce_area_active(db, area_id: str, is_active: bool) -> None:
    """An area was switched on or off (integration switch, HA input):
    ``area_updated`` to the sessions that may read it now
    (:func:`broadcast_area_scoped`) and the data-less ``areas_changed`` to
    the apps whose list holds it when active (it just came or went)."""
    await broadcast_area_scoped(
        db,
        area_id,
        {"type": "area_updated", "data": {"area_id": area_id, "is_active": is_active}},
    )
    rows = await db.fetchall("SELECT tag FROM area_tags WHERE area_id = ?", (area_id,))
    await notify_areas_changed(
        db, {"id": area_id, "is_active": True, "tags": [r["tag"] for r in rows]}
    )


async def send_to_integration(message: dict[str, Any]) -> bool:
    """Send a message to the integration push session only.

    Used for provision_client / update_client_identity / deprovision_client;
    these never go to apps or admin UIs. Returns True if the session got it.
    """
    return await api_ws.send_to_client(INTEGRATION_CLIENT_ID, message)


async def wait_until_disconnected(client_id: str, timeout: float) -> bool:
    """Wait until no live connection of ``client_id`` is left (max ``timeout``)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while client_id in get_connected_client_ids():
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.2)
    return True


# Clients that were just told about a rotated token, waiting for them to
# re-fetch their config (get_client_config) before the old token is revoked.
_config_fetch_waiters: dict[str, list[asyncio.Event]] = {}

# How long a token rotation waits for a connected client to fetch its new
# config before the old token is revoked anyway.
CONFIG_FETCH_GRACE_SECONDS = 5.0
# After the fetch, give the app time to switch its HA connection to the new
# token before the old one is revoked. Revoking right after the fetch would
# make HA close the old session while the app opens the new one, and the app
# would take the late close of the old socket for a failure of the new one.
# Apps switch within ~20 ms.
CONFIG_SWITCH_GRACE_SECONDS = 3.0


def _notify_config_fetched(client_id: str) -> None:
    for event in _config_fetch_waiters.get(client_id, ()):
        event.set()


async def deliver_rotated_config(
    client_id: str, grace_seconds: float | None = None
) -> bool:
    """Push client_config_updated to a rotated client and wait for its fetch.

    Called by token rotation between "new token stored" and "old token
    revoked": the connected app learns about the new token
    immediately instead of only on its next reconnect, and the old token is
    revoked only after the app fetched its new config plus
    CONFIG_SWITCH_GRACE_SECONDS to switch over (or after ``grace_seconds``
    without a fetch). Returns True if a live connection got the message.
    """
    grace = (
        api_ws.CONFIG_FETCH_GRACE_SECONDS if grace_seconds is None else grace_seconds
    )
    event = asyncio.Event()
    _config_fetch_waiters.setdefault(client_id, []).append(event)
    try:
        delivered = await api_ws.send_to_client(
            client_id, {"type": "client_config_updated", "data": {}}
        )
        if delivered:
            try:
                await asyncio.wait_for(event.wait(), grace)
                logger.info(
                    "Client %s fetched its config after token rotation", client_id
                )
                await asyncio.sleep(api_ws.CONFIG_SWITCH_GRACE_SECONDS)
            except asyncio.TimeoutError:
                logger.info(
                    "Client %s did not fetch its config within %.0fs after token "
                    "rotation",
                    client_id,
                    grace,
                )
        else:
            logger.info(
                "Client %s is not connected; it gets the rotated token on its "
                "next connect",
                client_id,
            )
        return delivered
    finally:
        waiters = _config_fetch_waiters.get(client_id, [])
        if event in waiters:
            waiters.remove(event)
        if not waiters:
            _config_fetch_waiters.pop(client_id, None)


async def signal_client_removed(client_id: str) -> int:
    """Tell every live connection of ``client_id`` to reset itself.

    Sends ``{"type":"client_removed","client_id":...,"reason":"deleted"}`` over
    each live authenticated WebSocket of this client and marks that connection's
    state ``reset_signaled=True``. A well-behaved app replies IMMEDIATELY with
    ``client_reset_ack`` (handled by ``_handle_message``), which hard-purges the
    tombstone row and closes the socket. As a FALLBACK for connections that never
    ack, a force-disconnect (close 4001 "Client removed") is scheduled after
    ``RESET_ACK_GRACE_SECONDS`` WITHOUT purging; when such a device reconnects
    with its old secret, the handshake is replayed via ``_match_pending_deletion``
    and only its wipe-ack purges the row.

    This NEVER closes a socket inline and NEVER deletes a row. Returns the number
    of live connections signaled (0 if the client was offline at delete time —
    in which case its signed tombstone simply persists until it returns or an
    admin force-purges it).
    """
    if not client_id:
        return 0

    data = json.dumps(
        {"type": "client_removed", "client_id": client_id, "reason": "deleted"}
    )
    signaled = 0
    for ws, info in list(_clients.items()):
        if not info.get("authenticated"):
            continue
        if info.get("client_id") != client_id:
            continue
        if not queue_send(ws, data):
            continue
        info["reset_signaled"] = True
        signaled += 1
    await flush()

    if signaled:

        async def _fallback_disconnect() -> None:
            try:
                await asyncio.sleep(api_ws.RESET_ACK_GRACE_SECONDS)
                await api_ws.disconnect_client(
                    client_id, code=4001, reason="Client removed"
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - best-effort fallback, never raise
                logger.debug(
                    "fallback disconnect for %s failed", client_id, exc_info=True
                )

        from services import tasks

        tasks.spawn(_fallback_disconnect(), name="ws-reset-fallback")
        logger.info(
            "Signaled client_removed to %d connection(s) for %s", signaled, client_id
        )

    return signaled


async def ws_ping_loop() -> None:
    """Background task: periodically ping all connected WebSocket clients.

    Detects dead/stale connections that didn't properly close (e.g. network
    disconnect, sleep mode) and closes them. Application-level ``ping``
    through the connection's send queue; a cycle that fails is logged and
    the loop goes on, so one exception never ends dead-connection
    detection.
    """
    while True:
        await asyncio.sleep(WS_PING_INTERVAL)
        try:
            await ping_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - next cycle tries again
            logger.error("WebSocket ping cycle failed: %s", e, exc_info=True)


async def ping_once(now: float | None = None) -> int:
    """One ping cycle; returns the number of connections closed as dead."""
    if not _clients:
        return 0
    now = time.time() if now is None else now
    ping = json.dumps({"type": "ping"})
    dead: list[tuple[WebSocket, dict]] = []
    for ws, info in list(_clients.items()):
        # Only ping authenticated clients - unauthenticated ones have their own timeout
        if not info.get("authenticated"):
            continue
        last_pong = info.get("last_pong", 0)
        last_ping = info.get("last_ping", 0)
        if last_ping > 0 and last_pong < last_ping - WS_PING_TIMEOUT:
            # No answer to the previous ping within the timeout
            dead.append((ws, info))
            continue
        if queue_send(ws, ping):
            info["last_ping"] = now
    await flush()

    for ws, info in dead:
        drop_connection(ws, "Ping timeout")
        cid = info.get("client_id")
        logger.info(
            "Removed dead WebSocket connection (client=%s, total: %d)",
            cid,
            len(_clients),
        )
        # Broadcast disconnect for paired clients
        if cid and cid not in PSEUDO_CLIENT_IDS:
            if cid not in get_connected_client_ids():
                try:
                    await api_ws.broadcast(
                        {"type": "client_disconnected", "data": {"client_id": cid}},
                        audience=EventAudience.ADMIN,
                    )
                except Exception:  # noqa: BLE001 - best effort
                    pass
                # A babysitter that left no longer gets the children's requests.
                from services import access

                access.wake()
    return len(dead)
