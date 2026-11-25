"""Live entity states for devices on the hub path.

A device on the hub path (``services.hub_control``) may hold no HA key, so
it cannot subscribe to Home Assistant itself. After ``state_subscribe`` the
hub forwards ``entity_state`` for the entities the device can see (visible
placements in readable areas and tile run targets), sanitized like every
state that leaves the hub. A new ``state_subscribe`` is accepted at most
every 10 s per device (``hub_control.subscribe_retry_after``).

One HA WebSocket subscription (``subscribe_events`` / ``state_changed``,
Supervisor token) runs only while at least one device is subscribed. The
set of visible entities per device is refreshed at most every
:data:`REFRESH_SECONDS` (area or rule changes reach the device within that
time; ``client_config_updated`` makes the app re-subscribe anyway). Right
after ``state_subscribed`` the device gets the current state of every
visible entity once (:func:`send_snapshot`, all of them: the scope is
bounded by :data:`SNAPSHOT_MAX`, the same bound as the rule snapshot) – read
only after the HA subscription is active, so no change between snapshot and
relay is lost. Each state of the snapshot is awaited on the connection
(after a reconnect: through its queue, waiting while it is half full), so
a large scope never overflows the outgoing queue.
With a visible weather entity ``sun.sun`` is relayed too (day and night of
the weather symbol); it stays read only (not in the control scope).

A change is relayed only when the sanitized state or attributes differ from
what the device got last for that entity (:func:`_fingerprint`): Home
Assistant renews the token in the picture link of media players and
cameras every few minutes, which changes nothing a device gets (the
``hub_picture_id`` marker stays the same), so it is not relayed. A removal
(state null) is always relayed.

Relayed states go through the outgoing queue of each connection (``push``):
a stalled device never holds up the others. When the relay's HA
socket reconnects, every subscriber gets a fresh snapshot, since changes
made while it was down were not relayed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("hub.services.state_relay")

REFRESH_SECONDS = 30.0
SUN_ENTITY_ID = "sun.sun"
# Every visible entity; the same bound as the rule snapshot
# (enforcement.MAX_ENTITIES). A larger scope is cut and logged.
SNAPSHOT_MAX = 10000
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0
# How long the snapshot waits for the HA subscription of a starting relay.
READY_TIMEOUT = 5.0
# A queued snapshot waits while this many messages are still waiting to go
# out on the connection (its queue holds 256), at most SEND_TIMEOUT seconds.
SNAPSHOT_BACKLOG = 128
SEND_TIMEOUT = 10.0


@dataclass
class Subscriber:
    client_id: str
    send: Callable[[dict[str, Any]], Awaitable[None]]
    entities: set[str] = field(default_factory=set)
    refreshed_at: float = 0.0
    # Non-blocking send through the connection's queue; False when the
    # connection is gone. Without it ``send`` is awaited with a time limit.
    push: Callable[[dict[str, Any]], bool] | None = None
    # Messages still waiting in that queue.
    backlog: Callable[[], int] | None = None
    # Live states handed over per entity: a snapshot state read before a
    # live one is never sent after it.
    live: dict[str, int] = field(default_factory=dict)
    # Fingerprint of the state each entity was sent with last.
    sent: dict[str, bytes] = field(default_factory=dict)


_subscribers: dict[Any, Subscriber] = {}
_task: asyncio.Task | None = None
_db = None
_ha_client = None
_ready: asyncio.Event | None = None


def _fingerprint(state: dict | None) -> bytes | None:
    """Digest of what a device sees of a sanitized state (state and
    attributes; ``last_updated`` and ``context`` change without them)."""
    if not isinstance(state, dict):
        return None
    seen = [state.get("state"), state.get("attributes")]
    raw = json.dumps(seen, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).digest()


def _ready_event() -> asyncio.Event:
    global _ready
    if _ready is None:
        _ready = asyncio.Event()
    return _ready


async def wait_ready(timeout: float = READY_TIMEOUT) -> bool:
    """Wait until the running relay is subscribed to Home Assistant (no-op without a relay)."""
    if _task is None or _task.done():
        return False
    try:
        await asyncio.wait_for(_ready_event().wait(), timeout)
        return True
    except asyncio.TimeoutError:
        logger.info("State relay not subscribed after %.0fs; snapshot anyway", timeout)
        return False


def reset() -> None:
    global _task, _db, _ha_client, _ready
    if _task and not _task.done():
        _task.cancel()
    _task = None
    _ready = None
    _subscribers.clear()
    _db = None
    _ha_client = None


def rescope(client_id: str | None = None) -> None:
    """The scope of ``client_id`` (None: of every device) may have changed
    (profile, adjustment, access, areas): it is read again before the next
    state goes out, not only after :data:`REFRESH_SECONDS`."""
    for sub in _subscribers.values():
        if client_id is None or sub.client_id == client_id:
            sub.refreshed_at = 0.0


def subscriber_count() -> int:
    return len(_subscribers)


async def _visible(db, client_id: str) -> set[str]:
    from services import hub_control

    ids = set(await hub_control.visible_entities(db, client_id))
    if any(e.startswith("weather.") for e in ids):
        ids.add(SUN_ENTITY_ID)
    return ids


async def subscribe(
    key: Any,
    db,
    ha_client,
    client_id: str,
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    start: bool = True,
    push: Callable[[dict[str, Any]], bool] | None = None,
    backlog: Callable[[], int] | None = None,
) -> int:
    """Register ``key`` (the WebSocket); returns the number of entities."""
    global _db, _ha_client
    _db, _ha_client = db, ha_client
    sub = Subscriber(client_id, send, push=push, backlog=backlog)
    sub.entities = await _visible(db, client_id)
    sub.refreshed_at = time.monotonic()
    _subscribers[key] = sub
    if start:
        _ensure_running()
    return len(sub.entities)


async def _deliver(key: Any, sub: Subscriber, message: dict[str, Any]) -> bool:
    """Hand ``message`` to ``sub``; a gone or stuck connection is dropped."""
    if sub.push is not None:
        ok = sub.push(message)
    else:
        try:
            await asyncio.wait_for(sub.send(message), SEND_TIMEOUT)
            ok = True
        except Exception:  # noqa: BLE001 - socket gone or stuck
            ok = False
    if not ok:
        _subscribers.pop(key, None)
    return ok


async def _drained(sub: Subscriber) -> None:
    """Wait (at most SEND_TIMEOUT) until the connection's queue has room."""
    if sub.backlog is None:
        return
    deadline = time.monotonic() + SEND_TIMEOUT
    while sub.backlog() >= SNAPSHOT_BACKLOG and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def send_snapshot(key: Any, ha_client, *, queued: bool = False) -> int:
    """Send the current state of every entity ``key`` sees (after ``state_subscribed``).

    A device without a key has no HA socket: without this it would know only
    the states of placements it loaded with ``get_area`` (no tile run
    targets, and a change between ``get_area`` and ``state_subscribe`` would
    be lost). Unknown or unavailable entities are skipped. The first
    snapshot is sent in order on the connection (each send awaited, so a
    large scope never fills the queue); ``queued`` (after a reconnect of the
    relay): through the connection's queue, behind the states relayed
    before, waiting while it is half full.
    """
    from api import authz

    sub = _subscribers.get(key)
    if sub is None or ha_client is None:
        return 0
    await wait_ready()
    entities = sorted(sub.entities)
    if len(entities) > SNAPSHOT_MAX:
        logger.warning(
            "Device %s sees %d entities; its snapshot holds the first %d",
            sub.client_id,
            len(entities),
            SNAPSHOT_MAX,
        )
    sent = 0
    for entity_id in entities[:SNAPSHOT_MAX]:
        if _subscribers.get(key) is not sub:
            return sent
        if queued and sub.push is not None:
            await _drained(sub)
        seen = sub.live.get(entity_id, 0)
        try:
            state = await ha_client.get_state(entity_id)
        except Exception as e:  # noqa: BLE001 - skip this entity
            logger.debug("Snapshot state of %s failed: %s", entity_id, e)
            continue
        if not state or sub.live.get(entity_id, 0) != seen:
            # gone, or a newer live state went out while this one was read
            continue
        message = {
            "type": "entity_state",
            "entity_id": entity_id,
            "state": authz.sanitize_state(entity_id, state),
        }
        sub.sent[entity_id] = _fingerprint(message["state"])
        if queued and sub.push is not None:
            if not await _deliver(key, sub, message):
                return sent
        else:
            try:
                await sub.send(message)
            except Exception:  # noqa: BLE001 - socket gone
                _subscribers.pop(key, None)
                return sent
        sent += 1
    return sent


def _resend_snapshots() -> None:
    """After a reconnect: what changed meanwhile was not relayed."""
    from services import tasks

    for key in list(_subscribers):
        try:
            tasks.spawn(
                send_snapshot(key, _ha_client, queued=True), name="state-relay-snapshot"
            )
        except RuntimeError:  # shutting down
            return


def unsubscribe(key: Any) -> None:
    global _task
    _subscribers.pop(key, None)
    if not _subscribers and _task and not _task.done():
        _task.cancel()
        _task = None
        _ready_event().clear()


async def dispatch_event(entity_id: str, new_state: dict | None) -> int:
    """Send one state change to every subscriber that sees the entity."""
    from api import authz

    if not isinstance(entity_id, str):
        return 0
    sent = 0
    now = time.monotonic()
    for key, sub in list(_subscribers.items()):
        if _db is not None and (
            not sub.refreshed_at or now - sub.refreshed_at > REFRESH_SECONDS
        ):
            try:
                sub.entities = await _visible(_db, sub.client_id)
            except Exception as e:  # noqa: BLE001 - keep the old set
                logger.debug("Refresh of visible entities failed: %s", e)
            sub.refreshed_at = now
        if entity_id not in sub.entities:
            # out of scope: relayed in full again once it is back
            sub.sent.pop(entity_id, None)
            continue
        clean = authz.sanitize_state(entity_id, new_state)
        fingerprint = _fingerprint(clean)
        if fingerprint is None:
            sub.sent.pop(entity_id, None)
        elif sub.sent.get(entity_id) == fingerprint:
            continue  # nothing the device sees changed (e.g. a token renewal)
        else:
            sub.sent[entity_id] = fingerprint
        message = {"type": "entity_state", "entity_id": entity_id, "state": clean}
        sub.live[entity_id] = sub.live.get(entity_id, 0) + 1
        if await _deliver(key, sub, message):
            sent += 1
    if sent:
        await asyncio.sleep(0)  # let the queues send right away
    return sent


def _ensure_running() -> None:
    global _task
    if _task is None or _task.done():
        try:
            _task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            _task = None


async def _run() -> None:
    from services.ha_client import HA_WS

    import websockets

    delay = RECONNECT_MIN
    subscribed_before = False
    while _subscribers:
        try:
            token = getattr(_ha_client, "_token", None)
            if not token:
                return
            async with websockets.connect(HA_WS, max_size=16 * 1024 * 1024) as ws:
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_required":
                    raise RuntimeError("unexpected greeting")
                await ws.send(json.dumps({"type": "auth", "access_token": token}))
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_ok":
                    raise RuntimeError("auth failed")
                await ws.send(
                    json.dumps(
                        {
                            "id": 1,
                            "type": "subscribe_events",
                            "event_type": "state_changed",
                        }
                    )
                )
                msg = json.loads(await ws.recv())
                if msg.get("id") != 1 or not msg.get("success"):
                    raise RuntimeError("subscribe_events refused")
                delay = RECONNECT_MIN
                _ready_event().set()
                logger.info("State relay subscribed (%d devices)", len(_subscribers))
                if subscribed_before:
                    _resend_snapshots()
                subscribed_before = True
                while _subscribers:
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "event":
                        continue
                    data = (msg.get("event") or {}).get("data") or {}
                    await dispatch_event(data.get("entity_id"), data.get("new_state"))
        except asyncio.CancelledError:
            _ready_event().clear()
            raise
        except Exception as e:  # noqa: BLE001 - reconnect with backoff
            _ready_event().clear()
            logger.warning("State relay disconnected (%s); retry in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
