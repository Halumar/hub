"""Persistent WebSocket connection to Home Assistant Core.

One long-lived, authenticated connection through the Supervisor proxy:

- reconnects on its own after an HA Core restart, update or network loss,
  with exponential backoff and jitter (``services.tasks.backoff_delay``);
- subscribes to ``state_changed``, ``homeassistant_started``, the area,
  device and entity registry events and ``call_service`` (the log tab maps
  direct actions of device users by their HA user);
- keeps a live mirror of all states (``get_states`` on every connect, then
  ``state_changed``), so reads of many entities cost no HTTP call;
- caches the area/device/entity registries until a registry event (or a
  reconnect) invalidates them;
- forwards events to listeners (sensor resync, area switches) and tells
  connect listeners when a fresh connection with a complete mirror is up.

Listeners run inside the reader and must not block: they schedule their work
(``services.tasks.spawn``).
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from services.tasks import backoff_delay

logger = logging.getLogger("hub.services.ha_ws")

HA_WS_URL = "ws://supervisor/core/websocket"

# An HA Core restart takes ~30 s; a cap of 5 s (2.5-5 s with jitter) brings
# the hub back within a few seconds after HA, at one cheap attempt per few
# seconds through the Supervisor proxy while HA is down.
RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 5.0
COMMAND_TIMEOUT_SECONDS = 30.0
AUTH_TIMEOUT_SECONDS = 10.0
# Entity registry and get_states can be several MB on large installations.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

SUBSCRIBED_EVENTS = (
    "state_changed",
    "homeassistant_started",
    "area_registry_updated",
    "device_registry_updated",
    "entity_registry_updated",
    "call_service",
    # Answer buttons of family notifications (services/family/notify.py).
    "mobile_app_notification_action",
)
REGISTRY_EVENTS = {
    "area_registry_updated": "area",
    "device_registry_updated": "device",
    "entity_registry_updated": "entity",
}
REGISTRY_KINDS = ("area", "device", "entity")

EventListener = Callable[[str, dict], None]
# (event_type, data, context) – for listeners that need the caller (user_id).
ContextEventListener = Callable[[str, dict, dict], None]
ConnectListener = Callable[[], Awaitable[None]]


class HAUnavailable(ConnectionError):
    """No live connection to Home Assistant (restart, network)."""


class HACommandError(RuntimeError):
    """Home Assistant answered a command with ``success: false``."""


class HAAuthError(RuntimeError):
    """The token was rejected (``auth_invalid``)."""


def _timestamp(state: dict | None) -> datetime | None:
    value = (state or {}).get("last_updated")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _default_connect(url: str):
    import websockets

    return websockets.connect(
        url,
        max_size=MAX_MESSAGE_BYTES,
        open_timeout=AUTH_TIMEOUT_SECONDS,
        # Library keepalive pings detect a dead link within ~40 s.
        ping_interval=20,
        ping_timeout=20,
    )


class HAWebSocket:
    """The hub's single live connection to Home Assistant."""

    def __init__(
        self,
        token: str,
        url: str = HA_WS_URL,
        *,
        connect: Callable[[str], Any] | None = None,
        reconnect_initial: float = RECONNECT_INITIAL_SECONDS,
        reconnect_max: float = RECONNECT_MAX_SECONDS,
    ) -> None:
        self._token = token
        self._url = url
        self._connect = connect or _default_connect
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max

        self._ws: Any = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subscriptions: dict[int, str] = {}
        self._connected = False
        self._connected_event: asyncio.Event | None = None
        self._stopping = False

        self._states: dict[str, dict] = {}
        self._states_ready = False

        self._registry: dict[str, list[dict] | None] = dict.fromkeys(REGISTRY_KINDS)
        self._registry_gen: dict[str, int] = dict.fromkeys(REGISTRY_KINDS, 0)

        self._event_listeners: list[EventListener] = []
        self._context_listeners: list[ContextEventListener] = []
        self._connect_listeners: list[ConnectListener] = []

        # Diagnostics
        self.connects = 0
        self.attempts = 0
        self.last_error: str | None = None
        self.ha_version: str | None = None

    # ── Public state ────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def states_ready(self) -> bool:
        """True while the state mirror is complete and kept current."""
        return self._connected and self._states_ready

    def get_state(self, entity_id: str) -> dict | None:
        return self._states.get(entity_id)

    def has_state(self, entity_id: str) -> bool:
        return entity_id in self._states

    def all_states(self) -> list[dict]:
        return list(self._states.values())

    def entity_ids(self) -> list[str]:
        return list(self._states)

    def add_event_listener(self, listener: EventListener) -> None:
        self._event_listeners.append(listener)

    def add_context_listener(self, listener: ContextEventListener) -> None:
        """Like add_event_listener, plus the event's context (user_id, parent_id)."""
        self._context_listeners.append(listener)

    def add_connect_listener(self, listener: ConnectListener) -> None:
        self._connect_listeners.append(listener)

    async def wait_connected(self, timeout: float) -> bool:
        if self._connected:
            return True
        if self._connected_event is None:
            self._connected_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return self._connected

    # ── Mirror updates from REST writes ─────────────────────────────────

    def apply_state(self, state: dict | None) -> None:
        """Take a state object (event or REST answer) into the mirror.

        An older ``last_updated`` never overwrites a newer one, so the order
        of ``get_states``, events and REST answers does not matter.
        """
        if not isinstance(state, dict):
            return
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str):
            return
        current = self._states.get(entity_id)
        if current is not None:
            new_ts, cur_ts = _timestamp(state), _timestamp(current)
            if new_ts is not None and cur_ts is not None and new_ts < cur_ts:
                return
        self._states[entity_id] = state

    def remove_state(self, entity_id: str) -> None:
        self._states.pop(entity_id, None)

    # ── Registry cache ──────────────────────────────────────────────────

    def invalidate_registry(self, kind: str | None = None) -> None:
        for k in (kind,) if kind else REGISTRY_KINDS:
            self._registry[k] = None
            self._registry_gen[k] += 1

    async def registry(self, kind: str) -> list[dict]:
        """``config/<kind>_registry/list``, cached until a registry event."""
        cached = self._registry[kind]
        if cached is not None:
            return list(cached)
        gen = self._registry_gen[kind]
        result = await self.command({"type": f"config/{kind}_registry/list"})
        result = list(result or [])
        # An event during the fetch may already have made the answer stale.
        if gen == self._registry_gen[kind] and self._connected:
            self._registry[kind] = result
        return list(result)

    # ── Commands ────────────────────────────────────────────────────────

    async def command(
        self, message: dict, timeout: float = COMMAND_TIMEOUT_SECONDS
    ) -> Any:
        """Send a command and return its ``result`` (raises on failure)."""
        ws = self._ws
        if ws is None or not self._connected:
            raise HAUnavailable("not connected to Home Assistant")
        return await self._command(ws, message, timeout)

    async def _command(self, ws: Any, message: dict, timeout: float) -> Any:
        self._next_id += 1
        msg_id = self._next_id
        payload = {**message, "id": msg_id}
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise HAUnavailable(
                f"Home Assistant did not answer {message.get('type')} in {timeout:.0f}s"
            ) from None
        finally:
            self._pending.pop(msg_id, None)

    # ── Connection loop ─────────────────────────────────────────────────

    async def run(self) -> None:
        """Connect, keep the connection, reconnect with backoff. Forever."""
        if self._connected_event is None:
            self._connected_event = asyncio.Event()
        failures = 0
        while True:
            self.attempts += 1
            try:
                async with self._connect(self._url) as ws:
                    await self._authenticate(ws)
                    failures = 0
                    await self._serve(ws)
                self.last_error = "connection closed"
            except asyncio.CancelledError:
                self._stopping = True
                raise
            except HAAuthError as e:
                self.last_error = str(e)
                logger.error("Home Assistant rejected the hub's token: %s", e)
            except Exception as e:  # noqa: BLE001 - reconnect below
                self.last_error = f"{type(e).__name__}: {e}"
                log = logger.warning if failures == 0 else logger.debug
                log("Home Assistant connection lost or refused: %s", self.last_error)
            finally:
                self._closed()
            delay = backoff_delay(
                failures, self._reconnect_initial, self._reconnect_max
            )
            failures += 1
            logger.debug("Reconnecting to Home Assistant in %.1fs", delay)
            await asyncio.sleep(delay)

    async def _authenticate(self, ws: Any) -> None:
        async with asyncio.timeout(AUTH_TIMEOUT_SECONDS):
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise RuntimeError(f"unexpected first message {msg.get('type')!r}")
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            msg = json.loads(await ws.recv())
        if msg.get("type") == "auth_invalid":
            raise HAAuthError(msg.get("message") or "auth_invalid")
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"unexpected auth answer {msg.get('type')!r}")
        self.ha_version = msg.get("ha_version")

    async def _serve(self, ws: Any) -> None:
        """Run one authenticated connection until it ends."""
        self._ws = ws
        self._states = {}
        self._states_ready = False
        self.invalidate_registry()
        reader = asyncio.create_task(self._read(ws), name="ha-ws-reader")
        try:
            # Subscribe first, then load the snapshot: no change gets lost in
            # between (apply_state keeps the newer of event and snapshot).
            for event_type in SUBSCRIBED_EVENTS:
                await self._subscribe(ws, event_type)
            states = await self._command(
                ws, {"type": "get_states"}, COMMAND_TIMEOUT_SECONDS
            )
            for state in states or []:
                self.apply_state(state)
            self._states_ready = True
            self._connected = True
            self.connects += 1
            if self._connected_event is not None:
                self._connected_event.set()
            logger.info(
                "Connected to Home Assistant %s (%d states, connection #%d)",
                self.ha_version or "",
                len(self._states),
                self.connects,
            )
            for listener in list(self._connect_listeners):
                try:
                    await listener()
                except Exception as e:  # noqa: BLE001 - one listener must not break the link
                    logger.error("HA connect listener failed: %s", e, exc_info=True)
            await reader
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _subscribe(self, ws: Any, event_type: str) -> None:
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        self._subscriptions[msg_id] = event_type
        try:
            await ws.send(
                json.dumps(
                    {"id": msg_id, "type": "subscribe_events", "event_type": event_type}
                )
            )
            await asyncio.wait_for(future, COMMAND_TIMEOUT_SECONDS)
        except BaseException:
            self._subscriptions.pop(msg_id, None)
            raise
        finally:
            self._pending.pop(msg_id, None)

    async def _read(self, ws: Any) -> None:
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - connection ended
            self.last_error = f"{type(e).__name__}: {e}"

    def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type")
        if msg_type == "result":
            future = self._pending.get(msg.get("id"))
            if future is None or future.done():
                return
            if msg.get("success"):
                future.set_result(msg.get("result"))
            else:
                error = msg.get("error") or {}
                future.set_exception(
                    HACommandError(
                        f"{error.get('code', 'error')}: {error.get('message', '')}"
                    )
                )
            return
        if msg_type != "event":
            return
        event = msg.get("event") or {}
        event_type = event.get("event_type") or self._subscriptions.get(msg.get("id"))
        data = event.get("data") or {}
        if event_type == "state_changed":
            entity_id = data.get("entity_id")
            new_state = data.get("new_state")
            if new_state is None:
                if isinstance(entity_id, str):
                    self.remove_state(entity_id)
            else:
                self.apply_state(new_state)
        elif event_type in REGISTRY_EVENTS:
            self.invalidate_registry(REGISTRY_EVENTS[event_type])
        for listener in list(self._event_listeners):
            try:
                listener(event_type, data)
            except Exception as e:  # noqa: BLE001 - listeners never break the reader
                logger.error("HA event listener failed: %s", e, exc_info=True)
        context = event.get("context") or {}
        for listener in list(self._context_listeners):
            try:
                listener(event_type, data, context)
            except Exception as e:  # noqa: BLE001 - listeners never break the reader
                logger.error("HA event listener failed: %s", e, exc_info=True)

    def _closed(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._states_ready = False
        self._ws = None
        self._subscriptions.clear()
        self.invalidate_registry()
        if self._connected_event is not None:
            self._connected_event.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HAUnavailable("connection to Home Assistant lost"))
        self._pending.clear()
        if was_connected and not self._stopping:
            logger.warning("Disconnected from Home Assistant; reconnecting")
