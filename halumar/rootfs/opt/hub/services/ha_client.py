"""Home Assistant API client service."""

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger("hub.services.ha_client")

# Supervisor proxy URLs (inside add-on container)
SUPERVISOR_API = "http://supervisor"
HA_API = f"{SUPERVISOR_API}/core/api"
HA_WS = "ws://supervisor/core/websocket"

# REST fallback cache TTL (seconds), only while the live mirror is down
STATE_CACHE_TTL = 5
# Maximum cached entity states (prevents unbounded memory growth)
STATE_CACHE_MAX_SIZE = 2000


# Values that get interpolated into HA/Supervisor API URL paths (domain,
# service, entity_id) MUST be validated first. HA domains, service names, and
# entity object-ids are slugs limited to [a-z0-9_]. Rejecting anything else
# prevents path traversal: httpx collapses "../" in the path client-side, so an
# unvalidated service like "../../../../addons/self/restart" would otherwise
# escape /core/api/ and hit the Supervisor API with the hub's privileged
# Supervisor token.
_HA_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_HA_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def is_valid_ha_name(name: object) -> bool:
    """True if ``name`` is a safe HA domain/service token (slug, no path chars)."""
    return isinstance(name, str) and _HA_NAME_RE.match(name) is not None


def is_valid_entity_id(entity_id: object) -> bool:
    """True if ``entity_id`` is a well-formed ``domain.object_id`` slug."""
    return isinstance(entity_id, str) and _HA_ENTITY_ID_RE.match(entity_id) is not None


class ImageTooLarge(Exception):
    """An image from Home Assistant is larger than the caller allows."""


class HAClient:
    """Async client for Home Assistant Core API via Supervisor proxy."""

    def __init__(self, token: str):
        self._token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=30.0,
        )
        self._ws_id = 0
        self._ws_id_lock = asyncio.Lock()

        # Persistent HA connection with live state mirror and registry cache
        # (services/ha_ws.py). Reads use it while it is up; without
        # it (HA restarting, tests) they fall back to REST/one-shot WS.
        self.ws = None

        # REST fallback cache: {entity_id: (timestamp, state_data)}
        # Bounded to STATE_CACHE_MAX_SIZE entries; oldest evicted on overflow.
        self._state_cache: dict[str, tuple[float, dict | None]] = {}
        self._state_cache_ttl = STATE_CACHE_TTL
        self._state_cache_max_size = STATE_CACHE_MAX_SIZE

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    def attach_ws(self, ws) -> None:
        """Use the persistent connection ``ws`` (services.ha_ws.HAWebSocket)."""
        self.ws = ws

    @property
    def mirror_ready(self) -> bool:
        """True while the live state mirror is complete and current."""
        return self.ws is not None and self.ws.states_ready

    def state_known(self, entity_id: str) -> bool | None:
        """Whether HA has ``entity_id`` right now; None if unknown (no mirror)."""
        if not self.mirror_ready:
            return None
        return self.ws.has_state(entity_id)

    async def entity_known(self, entity_id: str) -> bool | None:
        """Whether HA has ``entity_id`` as a state or in the entity registry.

        None if it cannot be decided right now: without a complete live
        mirror (HA not connected or restarting) a missing state proves
        nothing, and the registry may be unreadable.
        """
        present = self.state_known(entity_id)
        if present is None:
            return None
        if present:
            return True
        # Registered but without a state (disabled, not loaded yet).
        try:
            registry = await self._registry_list("entity")
        except Exception as e:  # noqa: BLE001 - any HA/network error
            logger.warning("entity check: registry not readable (%s)", e)
            return None
        return any(entry.get("entity_id") == entity_id for entry in registry)

    def _mirror_write(self, url: str, result: Any) -> None:
        """Take the answer of a states-API write into the mirror."""
        if self.ws is None or not url.startswith(f"{HA_API}/states/"):
            return
        entity_id = url[len(f"{HA_API}/states/") :]
        self._state_cache.pop(entity_id, None)
        if result is None:
            self.ws.remove_state(entity_id)
        elif isinstance(result, dict):
            self.ws.apply_state(result)

    # ── REST API Methods ──────────────────────────────────────────────────

    async def _get(self, url: str) -> Any:
        """Make a GET request to HA API."""
        response = await self._client.get(url)
        response.raise_for_status()
        return response.json()

    async def _post(self, url: str, data: dict | None = None) -> Any:
        """Make a POST request to HA API."""
        response = await self._client.post(url, json=data or {})
        response.raise_for_status()
        result = response.json()
        self._mirror_write(url, result)
        return result

    async def _delete(self, url: str) -> None:
        """Make a DELETE request to HA API. A 404 is treated as success.

        Used to remove states-API entities (e.g. stale Halumar sensors) via
        DELETE /core/api/states/<entity_id>; deleting an entity that is already
        gone must stay a no-op so cleanup can run idempotently on every sync.
        """
        response = await self._client.delete(url)
        if response.status_code != 404:
            response.raise_for_status()
        self._mirror_write(url, None)

    async def get_config(self) -> dict:
        """Get HA configuration."""
        return await self._get(f"{HA_API}/config")

    async def get_image(
        self,
        proxy: str,
        entity_id: str,
        *,
        max_bytes: int,
        timeout: float,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[str, bytes]:
        """Still image of a camera (``proxy`` ``camera_proxy``) or the artwork
        of a media player (``media_player_proxy``) with the hub's own access.

        Returns ``(content type, bytes)``, read in memory only. Raises
        :class:`ImageTooLarge` beyond ``max_bytes`` (the download stops
        there), ``httpx.HTTPStatusError`` for an error answer and
        ``httpx.HTTPError`` when Home Assistant does not answer in time. A
        redirect is not followed and counts as an error answer.
        """
        if proxy not in ("camera_proxy", "media_player_proxy"):
            raise ValueError(f"Invalid image proxy: {proxy!r}")
        if not is_valid_entity_id(entity_id):
            raise ValueError(f"Invalid entity_id: {entity_id!r}")
        params = {}
        if width:
            params["width"] = str(width)
        if height:
            params["height"] = str(height)
        async with self._client.stream(
            "GET",
            f"{HA_API}/{proxy}/{entity_id}",
            params=params or None,
            timeout=timeout,
            follow_redirects=False,
        ) as response:
            if not response.is_success:
                raise httpx.HTTPStatusError(
                    f"image answer {response.status_code}",
                    request=response.request,
                    response=response,
                )
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ImageTooLarge()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ImageTooLarge()
            return response.headers.get("content-type", ""), bytes(data)

    async def get_states(self) -> list[dict]:
        """Get all entity states (from the live mirror while it is up)."""
        if self.mirror_ready:
            return self.ws.all_states()
        return await self._get(f"{HA_API}/states")

    async def get_state(self, entity_id: str) -> dict | None:
        """Get a single entity state with TTL cache.

        Returns cached data if it's within the TTL window, otherwise
        fetches from HA API and updates the cache. This eliminates
        N+1 API calls when loading area detail (many entities at once).
        """
        # Guard the URL path against traversal (see is_valid_entity_id).
        if not is_valid_entity_id(entity_id):
            raise ValueError(f"Invalid entity_id: {entity_id!r}")
        if self.mirror_ready:
            return self.ws.get_state(entity_id)
        now = time.monotonic()
        cached = self._state_cache.get(entity_id)
        if cached and (now - cached[0]) < self._state_cache_ttl:
            return cached[1]

        try:
            state = await self._get(f"{HA_API}/states/{entity_id}")
            # Evict oldest entries if cache is full
            if len(self._state_cache) >= self._state_cache_max_size:
                # Remove expired entries first
                expired = [
                    k
                    for k, (ts, _) in self._state_cache.items()
                    if (now - ts) >= self._state_cache_ttl
                ]
                for k in expired:
                    del self._state_cache[k]
                # If still full, remove oldest 10%
                if len(self._state_cache) >= self._state_cache_max_size:
                    sorted_keys = sorted(
                        self._state_cache, key=lambda k: self._state_cache[k][0]
                    )
                    for k in sorted_keys[: max(1, len(sorted_keys) // 10)]:
                        del self._state_cache[k]
            self._state_cache[entity_id] = (now, state)
            return state
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._state_cache[entity_id] = (now, None)
                return None
            raise

    def invalidate_state_cache(self, entity_id: str | None = None) -> None:
        """Invalidate cached state for a specific entity or all entities."""
        if entity_id:
            self._state_cache.pop(entity_id, None)
        else:
            self._state_cache.clear()

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict | None = None,
        target: dict | None = None,
    ) -> list[dict]:
        """Call a HA service."""
        # Guard the URL path against traversal: domain/service are interpolated
        # into ".../services/{domain}/{service}" and must be plain slugs. Without
        # this, "service" could carry "../" and escape to arbitrary HA/Supervisor
        # endpoints (see is_valid_ha_name).
        if not is_valid_ha_name(domain) or not is_valid_ha_name(service):
            raise ValueError(f"Invalid service call: {domain!r}.{service!r}")
        payload = {}
        if data:
            payload.update(data)
        if target:
            payload.update(target)
        return await self._post(f"{HA_API}/services/{domain}/{service}", payload)

    async def get_services(self) -> list[dict]:
        """Get available services."""
        return await self._get(f"{HA_API}/services")

    async def get_history(
        self, entity_id: str, start_time: str, end_time: str
    ) -> list[dict]:
        """Recorder history of one entity (compressed rows ``{s, lu, lc?}``).

        Significant changes only, no attributes, minimal response – what the
        sensor charts of the apps need.
        """
        if not is_valid_entity_id(entity_id):
            raise ValueError(f"Invalid entity_id: {entity_id!r}")
        result = await self._ws_command(
            {
                "type": "history/history_during_period",
                "start_time": start_time,
                "end_time": end_time,
                "entity_ids": [entity_id],
                "minimal_response": True,
                "no_attributes": True,
                "significant_changes_only": True,
            }
        )
        rows = (result or {}).get(entity_id) if isinstance(result, dict) else None
        return rows if isinstance(rows, list) else []

    async def get_forecasts(self, entity_id: str, forecast_type: str) -> list[dict]:
        """``weather.get_forecasts`` of one weather entity (read only)."""
        if not is_valid_entity_id(entity_id) or not is_valid_ha_name(forecast_type):
            raise ValueError(f"Invalid forecast request: {entity_id!r}")
        result = await self._ws_command(
            {
                "type": "call_service",
                "domain": "weather",
                "service": "get_forecasts",
                "target": {"entity_id": entity_id},
                "service_data": {"type": forecast_type},
                "return_response": True,
            }
        )
        response = (result or {}).get("response") if isinstance(result, dict) else None
        entry = (response or {}).get(entity_id) if isinstance(response, dict) else None
        forecast = (entry or {}).get("forecast") if isinstance(entry, dict) else None
        return forecast if isinstance(forecast, list) else []

    # ── WebSocket API Methods ─────────────────────────────────────────────
    # These use the HA WebSocket API via supervisor proxy for registry access

    async def _ws_command(self, command: dict) -> Any:
        """Execute a WebSocket command against HA Core.

        Uses the persistent connection while it is up, otherwise a one-shot
        connection (startup before the first connect, tests).
        """
        if self.ws is not None and self.ws.connected:
            return await self.ws.command(command)
        return await self._ws_command_once(command)

    async def _registry_list(self, kind: str) -> list[dict]:
        """Registry list, from the persistent connection's cache if possible."""
        if self.ws is not None and self.ws.connected:
            return await self.ws.registry(kind)
        return await self._ws_command({"type": f"config/{kind}_registry/list"})

    def _registry_changed(self, kind: str) -> None:
        # The registry event follows asynchronously; a read right after an
        # own write must not see the old cache.
        if self.ws is not None:
            self.ws.invalidate_registry(kind)

    async def _ws_command_once(self, command: dict) -> Any:
        """Execute a command over a short-lived WebSocket connection."""
        import websockets

        async with self._ws_id_lock:
            self._ws_id += 1
            command["id"] = self._ws_id

        try:
            # Increase max_size for large responses (entity registry can be >1MB)
            async with websockets.connect(HA_WS, max_size=16 * 1024 * 1024) as ws:
                # Wait for auth_required
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_required":
                    raise RuntimeError(f"Unexpected message: {msg}")

                # Authenticate
                await ws.send(
                    json.dumps(
                        {
                            "type": "auth",
                            "access_token": self._token,
                        }
                    )
                )

                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_ok":
                    raise RuntimeError(f"Auth failed: {msg}")

                # Send command
                await ws.send(json.dumps(command))

                # Wait for response (timeout prevents infinite loop on unexpected messages)
                async with asyncio.timeout(30):
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == command["id"]:
                            if msg.get("success"):
                                return msg.get("result", [])
                            else:
                                raise RuntimeError(
                                    f"Command failed: {msg.get('error', {})}"
                                )

        except ImportError:
            logger.warning("websockets package not available, using REST fallback")
            return []
        except Exception as e:
            logger.error("WebSocket command failed: %s", e)
            raise

    async def get_cloud_remote_domain(self) -> str | None:
        """Host of this instance's Nabu Casa remote UI, None without one.

        ``cloud/status`` answers ``remote_domain`` once the instance is
        logged in to Home Assistant Cloud; without the cloud integration the
        command is unknown (None, a definite answer). Raises when Home
        Assistant cannot be asked (no connection, no answer).
        """
        try:
            status = await self._ws_command({"type": "cloud/status"})
        except Exception as e:
            if "unknown_command" in str(e):
                return None
            raise
        if not isinstance(status, dict) or not status.get("logged_in"):
            return None
        domain = status.get("remote_domain")
        return domain.lower() if isinstance(domain, str) and domain else None

    async def get_areas(self) -> list[dict]:
        """Get all areas from HA area registry."""
        return await self._registry_list("area")

    async def create_area(
        self,
        name: str,
        icon: str | None = None,
        picture: str | None = None,
    ) -> dict:
        """Create a new area in HA."""
        cmd: dict[str, Any] = {"type": "config/area_registry/create", "name": name}
        if icon:
            cmd["icon"] = icon
        if picture:
            cmd["picture"] = picture
        try:
            return await self._ws_command(cmd)
        finally:
            self._registry_changed("area")

    async def update_area(
        self,
        area_id: str,
        name: str | None = None,
        icon: str | None = None,
        picture: str | None = None,
    ) -> dict:
        """Update an existing area in HA."""
        cmd: dict[str, Any] = {
            "type": "config/area_registry/update",
            "area_id": area_id,
        }
        if name is not None:
            cmd["name"] = name
        if icon is not None:
            cmd["icon"] = icon
        if picture is not None:
            cmd["picture"] = picture
        try:
            return await self._ws_command(cmd)
        finally:
            self._registry_changed("area")

    async def delete_area(self, area_id: str) -> None:
        """Delete an area from HA."""
        try:
            await self._ws_command(
                {"type": "config/area_registry/delete", "area_id": area_id}
            )
        finally:
            self._registry_changed("area")

    async def get_devices(self) -> list[dict]:
        """Get all devices from HA device registry."""
        return await self._registry_list("device")

    async def get_entity_registry(self) -> list[dict]:
        """Get all entities from HA entity registry."""
        return await self._registry_list("entity")

    async def update_entity_area(self, entity_id: str, area_id: str | None) -> dict:
        """Update the area assignment of an entity in HA."""
        cmd = {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
            "area_id": area_id,
        }
        try:
            return await self._ws_command(cmd)
        finally:
            self._registry_changed("entity")

    async def get_floors(self) -> list[dict]:
        """Get all floors from HA floor registry."""
        return await self._ws_command({"type": "config/floor_registry/list"})

    async def get_labels(self) -> list[dict]:
        """Get all labels from HA label registry."""
        return await self._ws_command({"type": "config/label_registry/list"})

    async def get_script_config(self, entity_id: str) -> dict:
        """Raw configuration of a script entity (``script/config``)."""
        result = await self._ws_command(
            {"type": "script/config", "entity_id": entity_id}
        )
        return (result or {}).get("config") or {}

    # ── Supervisor API Methods ────────────────────────────────────────────

    async def get_addon_info(self) -> dict:
        """Get own add-on info from Supervisor."""
        return await self._get(f"{SUPERVISOR_API}/addons/self/info")

    async def send_discovery(self, service: str, config: dict) -> dict:
        """Announce a discovery payload to the Supervisor (POST /discovery).

        The Supervisor deduplicates per addon+service (a repeat announce
        updates the stored config in place), so calling this on every startup
        is safe. The service must be declared under ``discovery:`` in
        config.yaml, otherwise the Supervisor rejects the request.
        """
        return await self._post(
            f"{SUPERVISOR_API}/discovery",
            {"service": service, "config": config},
        )

    async def get_core_info(self) -> dict:
        """Get HA Core info from the Supervisor (``data``: port, ssl, version ...).

        GET /core/info is allowed for the Supervisor default role.
        """
        result = await self._get(f"{SUPERVISOR_API}/core/info")
        return (result or {}).get("data") or {}

    async def get_supervisor_info(self) -> dict:
        """Get Supervisor info."""
        return await self._get(f"{SUPERVISOR_API}/info")

    async def get_addon_logs(self, lines: int = 200) -> str:
        """Fetch recent hub log lines from Supervisor.

        Proxies GET /addons/self/logs which returns plain text (newest last).
        Without a ``Range`` header the Supervisor sends only its default 100
        entries, so the last ``lines`` journal entries are requested
        explicitly (same header the Supervisor builds for ``?lines=``), then
        the text is trimmed to ``lines`` lines. Raises on non-2xx responses.
        """
        headers = {}
        if lines and lines > 0:
            headers["Range"] = f"entries=:-{lines - 1}:{lines}"
        response = await self._client.get(
            f"{SUPERVISOR_API}/addons/self/logs", headers=headers
        )
        response.raise_for_status()
        text = response.text
        if lines and lines > 0:
            log_lines = text.splitlines()
            text = "\n".join(log_lines[-lines:])
        return text

    async def restart_addon(self) -> None:
        """Restart this hub via Supervisor (POST /addons/self/restart).

        Raises httpx.HTTPStatusError on non-2xx responses.
        """
        response = await self._client.post(f"{SUPERVISOR_API}/addons/self/restart")
        response.raise_for_status()
