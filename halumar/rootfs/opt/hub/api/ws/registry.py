"""Connection registry of /api/ws: who is connected, and closing sessions."""

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger("hub.api.websocket")

# Connected clients: ws -> {"authenticated": bool, "client_id": str|None, "pair_attempts": int, "auth_attempts": int}
_clients: dict[WebSocket, dict] = {}

# Pseudo client_id of the companion integration's read-only push session
# (header-Bearer-authenticated on the handshake). Excluded from connected
# client sets/counts and from the paired-client message handlers.
INTEGRATION_CLIENT_ID = "integration"

# Pseudo client_id of an admin web-UI session authenticated by its session
# cookie (external admin UI over TLS, outside Ingress).
ADMIN_SESSION_CLIENT_ID = "admin"

# Admin sessions: Ingress, dev auto-auth, cookie-authenticated web UI.
ADMIN_SESSION_IDS = frozenset({"ingress", "dev", ADMIN_SESSION_CLIENT_ID})

# Sessions that are not paired clients (never counted, never broadcast as
# client_connected/client_disconnected, no client-only messages).
PSEUDO_CLIENT_IDS = ADMIN_SESSION_IDS | {INTEGRATION_CLIENT_ID}


def _is_admin_client(client_id: str | None) -> bool:
    """Check if a client_id represents an admin session."""
    return client_id in ADMIN_SESSION_IDS


def get_connected_client_ids() -> set[str]:
    """Return the set of authenticated paired client IDs currently connected.

    Excludes the 'ingress', 'dev' and 'integration' pseudo-clients. Used by
    the integration API to report which clients are online.
    """
    ids: set[str] = set()
    for _ws, info in _clients.items():
        if info.get("authenticated") and info.get("client_id"):
            cid = info["client_id"]
            if cid not in PSEUDO_CLIENT_IDS:
                ids.add(cid)
    return ids


def get_connected_count() -> int:
    """Return the total number of authenticated WebSocket connections.

    Admin sessions and the integration's read-only push session are excluded:
    they observe the client fleet, they are not part of it.
    """
    return sum(
        1
        for info in _clients.values()
        if info.get("authenticated") and info.get("client_id") not in PSEUDO_CLIENT_IDS
    )


def get_integration_push_connected() -> bool:
    """Return True while the integration's WS push session is connected.

    Live truth comes from ``_clients`` here — deliberately NOT from
    services/integration_status.py, which this module imports (cycle).
    """
    return any(
        info.get("authenticated") and info.get("client_id") == INTEGRATION_CLIENT_ID
        for info in _clients.values()
    )


async def disconnect_client(
    client_id: str, code: int = 4003, reason: str = "Session expired"
) -> int:
    """Disconnect all WebSocket connections for a given client_id.

    Closes the connection with the specified close code and removes
    the client from the _clients dict.

    Returns the number of connections closed.
    """
    closed = 0
    for ws, info in list(_clients.items()):
        if info.get("client_id") == client_id:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                pass
            _clients.pop(ws, None)
            discard_sender(ws)
            closed += 1
    if closed:
        logger.info(
            "Disconnected client %s (%d connections, code=%d)",
            client_id,
            closed,
            code,
        )
    return closed


# ── Outgoing queues ─────────────────────────────────────
#
# Pushes (broadcasts, targeted sends, pings) go through a bounded queue per
# connection, drained by a short-lived sender task. A slow client does not
# hold up the sender of a broadcast or the other clients, and a connection
# whose queue overflows or whose send fails is CLOSED (close 1000, the app
# reconnects and reloads) rather than dropped from the registry while its
# socket stays open.
# Direct answers to a request are still sent inline by the handler.

SEND_QUEUE_MAX = 256
SEND_TIMEOUT_SECONDS = 10.0
CLOSE_TIMEOUT_SECONDS = 5.0
DROP_CLOSE_CODE = 1000


class _Sender:
    __slots__ = ("queue", "task", "dropped")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(SEND_QUEUE_MAX)
        self.task: asyncio.Task | None = None
        self.dropped = False


_senders: dict[WebSocket, _Sender] = {}


def queue_send(ws: WebSocket, payload: str) -> bool:
    """Queue ``payload`` for ``ws``. False if the connection was dropped.

    A full queue closes the connection (``drop_connection``). The caller
    should ``await flush()`` once after queueing to a batch of connections.
    """
    sender = _senders.get(ws)
    if sender is None:
        sender = _senders[ws] = _Sender()
    if sender.dropped:
        return False
    try:
        sender.queue.put_nowait(payload)
    except asyncio.QueueFull:
        info = _clients.get(ws) or {}
        logger.warning(
            "Send queue of client_id=%s full (%d messages); closing the connection",
            info.get("client_id"),
            SEND_QUEUE_MAX,
        )
        drop_connection(ws, "Send queue overflow")
        return False
    if sender.task is None or sender.task.done():
        from services import tasks

        try:
            sender.task = tasks.spawn(_drain(ws, sender), name="ws-sender")
        except RuntimeError:  # shutting down
            return False
    return True


async def flush() -> None:
    """Let the sender tasks run once (fast clients are served right away)."""
    await asyncio.sleep(0)


async def deliver(targets: list[tuple[WebSocket, str]]) -> int:
    """Queue each ``(ws, payload)``; returns how many connections took it.

    A connection whose send failed right away (closed socket) is dropped
    and not counted.
    """
    queued = [_senders[ws] for ws, payload in targets if queue_send(ws, payload)]
    await flush()
    return sum(1 for sender in queued if not sender.dropped)


def pending_sends(ws: WebSocket) -> int:
    sender = _senders.get(ws)
    return sender.queue.qsize() if sender else 0


async def _drain(ws: WebSocket, sender: _Sender) -> None:
    while not sender.dropped:
        try:
            payload = sender.queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            await asyncio.wait_for(ws.send_text(payload), SEND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - broken or stuck connection
            info = _clients.get(ws) or {}
            logger.warning(
                "Send to client_id=%s failed (%s); closing the connection",
                info.get("client_id"),
                type(e).__name__,
            )
            drop_connection(ws, "Send failed")
            return


def drop_connection(ws: WebSocket, reason: str) -> None:
    """Remove ``ws`` from the registry and close it in the background."""
    sender = _senders.pop(ws, None)
    if sender is not None:
        sender.dropped = True
    _clients.pop(ws, None)
    from services import tasks

    try:
        tasks.spawn(_close_quietly(ws, DROP_CLOSE_CODE, reason), name="ws-close")
    except RuntimeError:  # shutting down: uvicorn closes the socket
        pass


def discard_sender(ws: WebSocket) -> None:
    """Forget the queue of a connection that ended."""
    sender = _senders.pop(ws, None)
    if sender is not None:
        sender.dropped = True
        if sender.task is not None and not sender.task.done():
            sender.task.cancel()


async def _close_quietly(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await asyncio.wait_for(
            ws.close(code=code, reason=reason), CLOSE_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - already closed or dead
        pass
