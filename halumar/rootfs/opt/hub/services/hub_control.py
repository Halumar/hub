"""The hub path: devices that read and switch through the hub.

A device is on the hub path when its profile has parental controls or its
effective rule ``control_path`` is ``hub`` ("Hub only"); a watch never is
(it has no hub socket). Such a device holds no Home Assistant key unless the
profile gives it remote access (``resolver.holds_key``), so at home it reads
states, history, forecasts and still images and switches through the hub
(WS ``control``, REST ``POST /api/client/control``).

What the hub checks for every request of the hub path:

1. The request's shape (entity id, action, data up to 4 KB; target keys in
   ``data`` are dropped: the hub targets exactly one entity).
2. The device is paired, active, not being deleted, not a watch and on the
   hub path; else ``not_hub_controlled``.
3. Its access is open (hours, switch, guest expiry); else ``rule_blocked
   {access_closed}``.
4. The entity is in its scope (:func:`visible_entities`); else
   ``not_visible``.
5. With parental controls the kids rules, counters, pauses and releases
   (``services.kids_mode``) and a single-use grant for the integration. Other
   devices get no grant and no kids state.

Then the hub calls the integration action ``<ns>.control`` with the device's
own (read-only) HA user. The integration checks the allowlist, the area
scope, ``run`` releases, group members, scene and script targets, access
hours and its rate limit again; a refusal there is the answer. When Home
Assistant or the integration does not answer the result is ``ha_unavailable``
or ``not_ready``; there is no other path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from models.database import Database
from namespace import HA_DOMAIN
from services.audit_log import log_service_call
from services.ha_client import is_valid_entity_id, is_valid_ha_name

logger = logging.getLogger("hub.services.hub_control")

CONTROL_DATA_MAX_BYTES = 4096
REQUEST_ID_MAX = 64

# Target keys a device may not send: the hub targets exactly one entity.
TARGET_KEYS = ("entity_id", "device_id", "area_id", "label_id", "floor_id")

# control_error reasons (contract $defs/control_error_reason).
ERR_NOT_HUB_CONTROLLED = "not_hub_controlled"
ERR_NOT_VISIBLE = "not_visible"
ERR_INVALID_REQUEST = "invalid_request"
ERR_NOT_READY = "not_ready"
ERR_HA_ERROR = "ha_error"
ERR_HA_UNAVAILABLE = "ha_unavailable"
ERR_RATE_LIMITED = "rate_limited"

# The entity's state was read and is not available (decide reads no more).
STATE_UNKNOWN = object()
# Bound of the state read on the control path (the HA client waits up to 30 s).
STATE_READ_SECONDS = 5.0
# How long a request waits for the previous action of the same device; the
# HA call itself may take 10 s. Longer: answered ha_unavailable instead of
# a queue that runs late.
CONTROL_LOCK_WAIT_SECONDS = 3.0
# How long a call waits for the rules of a device that just got its HA user.
RULES_WAIT_SECONDS = 3.0

# Read limits per device (contract AsyncAPI x-limits hub_*).
STATE_SUBSCRIBE_MIN_SECONDS = 10.0
READS_PER_WINDOW = 10
READS_WINDOW_SECONDS = 10.0
IMAGE_MIN_SECONDS = 1.0

_client_locks: dict[str, asyncio.Lock] = {}
_core_url_cache: str | None = None
_subscribed_at: dict[str, float] = {}
_reads: dict[str, list[float]] = {}
_images: dict[tuple[str, str], float] = {}
# Bound of the limiter tables (devices x entities); cleared when exceeded.
LIMITER_MAX = 5000


def reset() -> None:
    """Forget locks, limits and caches, also of the images (tests)."""
    global _core_url_cache
    from services import hub_images

    hub_images.reset()
    _client_locks.clear()
    _core_url_cache = None
    _subscribed_at.clear()
    _reads.clear()
    _images.clear()


def _client_lock(key: str) -> asyncio.Lock:
    lock = _client_locks.get(key)
    if lock is None:
        lock = _client_locks[key] = asyncio.Lock()
    return lock


def error_message(
    request_id: str | None, reason: str, retry_after: int | None = None
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "control_error",
        "request_id": request_id,
        "reason": reason,
    }
    if reason == ERR_RATE_LIMITED:
        msg["retry_after"] = retry_after
    return msg


# Answers of the Supervisor's proxy while Home Assistant is not reachable.
UNREACHABLE_STATUS = (502, 503, 504)


def unreachable(e: BaseException) -> bool:
    """Whether an error means Home Assistant is not reachable (answered
    ``ha_unavailable``, not ``ha_error``): no connection, a timeout, or the
    proxy's 502/503/504, also as a refused WebSocket upgrade."""
    if isinstance(e, (ConnectionError, OSError, TimeoutError, httpx.TransportError)):
        return True
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code in UNREACHABLE_STATUS
    response = getattr(e, "response", None)  # websockets InvalidStatus
    status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(e, "status_code", None)
    if status in UNREACHABLE_STATUS:
        return True
    try:
        from websockets.exceptions import InvalidHandshake

        return isinstance(e, InvalidHandshake) and status is None
    except ImportError:  # pragma: no cover - websockets is a dependency
        return False


def request_id_of(message: dict[str, Any]) -> str | None:
    rid = message.get("request_id")
    if isinstance(rid, str) and 0 < len(rid) <= REQUEST_ID_MAX:
        return rid
    return None


# ── read limits ─────────────────────────────────────────────────────────────


def _retry(seconds: float) -> int:
    return max(1, int(seconds + 0.999))


def _bound(table: dict) -> None:
    if len(table) > LIMITER_MAX:
        table.clear()


def subscribe_retry_after(client_id: str, now: float | None = None) -> int | None:
    """A new ``state_subscribe`` of the device: None when allowed (and
    counted), else the seconds until the next one is accepted."""
    now = time.monotonic() if now is None else now
    last = _subscribed_at.get(client_id)
    if last is not None and now - last < STATE_SUBSCRIBE_MIN_SECONDS:
        return _retry(STATE_SUBSCRIBE_MIN_SECONDS - (now - last))
    _bound(_subscribed_at)
    _subscribed_at[client_id] = now
    return None


def read_retry_after(client_id: str, now: float | None = None) -> int | None:
    """``history_get`` and ``forecast_get`` together: at most
    :data:`READS_PER_WINDOW` per :data:`READS_WINDOW_SECONDS` and device."""
    now = time.monotonic() if now is None else now
    recent = [t for t in _reads.get(client_id, ()) if now - t < READS_WINDOW_SECONDS]
    if len(recent) >= READS_PER_WINDOW:
        _reads[client_id] = recent
        return _retry(READS_WINDOW_SECONDS - (now - recent[0]))
    _bound(_reads)
    _reads[client_id] = [*recent, now]
    return None


def image_retry_after(
    client_id: str, entity_id: str, now: float | None = None
) -> int | None:
    """Still images: at most one request per second per entity and device."""
    now = time.monotonic() if now is None else now
    key = (client_id, entity_id)
    last = _images.get(key)
    if last is not None and now - last < IMAGE_MIN_SECONDS:
        return _retry(IMAGE_MIN_SECONDS - (now - last))
    _bound(_images)
    _images[key] = now
    return None


# ── the device ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HubDevice:
    """A paired device on the hub path, resolved (``store.resolve_client``)."""

    row: dict
    profile: dict
    effective: dict
    child: bool


async def hub_device(db: Database, client_id: str | None) -> HubDevice | None:
    """The device when it is paired, active, not being deleted, not a watch
    and on the hub path (``resolver.uses_hub_path``); None otherwise (fail
    closed: also when it cannot be resolved)."""
    from services.profiles import catalog as cat
    from services.profiles import resolver as res
    from services.profiles import store

    if not client_id:
        return None
    try:
        resolved = await store.resolve_client(db, client_id)
    except Exception as e:  # noqa: BLE001 - unresolvable: not on the hub path
        logger.warning("Device %s could not be resolved: %s", client_id, e)
        return None
    if not resolved:
        return None
    row, prof, effective = resolved
    child = bool(prof["child_protection"])
    if (
        not row.get("is_active")
        or row.get("pending_deletion_at")
        # A watch has no hub socket and its app never uses the hub path: it
        # switches only through <ns>.control. Its secret is refused here.
        or row.get("platform") in store.WATCH_PLATFORMS
        or not res.uses_hub_path(
            effective, child=child, platform=cat.device_platform(row.get("platform"))
        )
    ):
        return None
    return HubDevice(row, prof, effective, child)


async def uses_hub_path(db: Database, client_id: str | None) -> bool:
    return await hub_device(db, client_id) is not None


async def visible_entities(db: Database, client_id: str) -> dict[str, list[str]]:
    """entity_id -> listed area ids it is placed in (``[]`` = run target).

    The scope of a device on the hub path (effective rule ``areas``, listed
    areas only: an area a notification merely links to is readable, not
    switchable; nothing while the access is closed). Run targets of tiles in
    those placements are allowed without an area (only profile and entity
    rules apply).
    """
    from api import authz

    area_ids = await authz.listed_area_ids(db, client_id)
    if not area_ids:
        return {}
    placeholders = ",".join("?" for _ in area_ids)
    rows = await db.fetchall(
        f"SELECT area_id, entity_id, tile FROM area_entities WHERE area_id IN "
        f"({placeholders}) AND (is_visible IS NULL OR is_visible = 1)",
        tuple(area_ids),
    )
    result: dict[str, list[str]] = {}
    run_targets: set[str] = set()
    for r in rows:
        result.setdefault(r["entity_id"], [])
        if r["area_id"] not in result[r["entity_id"]]:
            result[r["entity_id"]].append(r["area_id"])
        tile = _loads(r.get("tile"))
        for key in ("tap_action", "hold_action"):
            action = tile.get(key)
            if isinstance(action, dict) and action.get("action") == "run":
                target = action.get("target_entity_id")
                if isinstance(target, str) and is_valid_entity_id(target):
                    run_targets.add(target)
    for target in run_targets:
        result.setdefault(target, [])
    return result


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def read_state(ha_client, entity_id: str, timeout: float | None = None):
    """The entity's state, None when not available (rules fall back)."""
    if ha_client is None:
        return None
    try:
        if timeout is None:
            return await ha_client.get_state(entity_id)
        return await asyncio.wait_for(ha_client.get_state(entity_id), timeout)
    except Exception as e:  # noqa: BLE001 - rules fall back to no state
        logger.debug("State of %s not available: %s", entity_id, e)
        return None


# ── the call to Home Assistant ──────────────────────────────────────────────


async def core_url(ha_client) -> str:
    """Base URL of HA Core inside the app network (device-token calls).

    The Supervisor proxy accepts only the app's own token, so calls with a
    device token go to Core directly (``HUB_HA_CORE_URL`` overrides).
    """
    global _core_url_cache
    override = os.environ.get("HUB_HA_CORE_URL")
    if override:
        return override.rstrip("/")
    if _core_url_cache:
        return _core_url_cache
    port, ssl = 8123, False
    try:
        info = await ha_client.get_core_info()
        port = int(info.get("port") or 8123)
        ssl = bool(info.get("ssl"))
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read HA port from the Supervisor (%s)", e)
    _core_url_cache = f"{'https' if ssl else 'http'}://homeassistant:{port}"
    return _core_url_cache


# Reasons of <ns>.control (contract $defs/control_denial_reason) mapped to
# control_error reasons (the kids reasons are rule_blocked: kids_mode).
DENIAL_TO_ERROR = {
    "blocked_area": ERR_NOT_VISIBLE,
    "blocked_remote": ERR_NOT_VISIBLE,
    "blocked_view_only": ERR_NOT_VISIBLE,
    "blocked_quick_access": ERR_NOT_VISIBLE,
    "blocked_group_member": ERR_NOT_VISIBLE,
    "scene_not_released": ERR_NOT_VISIBLE,
    "rules_missing": ERR_NOT_READY,
    "not_a_device": ERR_NOT_READY,
    "kids_hub_unreachable": ERR_HA_UNAVAILABLE,
    "rate_limited": ERR_HA_ERROR,
    "action_failed": ERR_HA_ERROR,
}


class ControlDenied(Exception):
    """``<ns>.control`` refused the action (reason, until, as answered)."""

    def __init__(self, reason: str, until: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.until = until


class ControlFailure(Exception):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.reason = reason
        # The HTTP status of Home Assistant's answer, when there was one.
        self.status = status


async def call_control_as_device(
    ha_client,
    token: str,
    entity_id: str,
    action: str,
    data: dict[str, Any],
    grant: str | None,
) -> dict[str, Any]:
    """``<ns>.control`` with the device's own token; the entity's result.

    ``POST /api/services/<ns>/control?return_response`` with ``entity_ids``
    (a batch of one answers with the reason instead of raising, also over
    REST). Raises :class:`ControlDenied` when the integration refused and
    :class:`ControlFailure` (control_error reason) when HA or the
    integration did not answer (missing action = integration not loaded).
    """
    base = await core_url(ha_client)
    url = f"{base}/api/services/{HA_DOMAIN}/control?return_response"
    body: dict[str, Any] = {"entity_ids": [entity_id], "action": action, "data": data}
    if grant:
        body["grant"] = grant
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:  # noqa: S501 - internal app network, HA's cert names the public host
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as e:
        logger.warning("HA not reachable for a device call: %s", e)
        raise ControlFailure(ERR_HA_UNAVAILABLE) from e
    if resp.status_code in (400, 404):
        # No <ns>.control (integration not loaded) or a malformed call.
        logger.warning(
            "%s.control answered %s for a hub action: integration not loaded?",
            HA_DOMAIN,
            resp.status_code,
        )
        raise ControlFailure(ERR_HA_UNAVAILABLE, resp.status_code)
    if resp.status_code >= 300:
        logger.warning("HA answered %s to a device call", resp.status_code)
        raise ControlFailure(ERR_HA_ERROR)
    try:
        results = resp.json()["service_response"]["results"]
        result = next(r for r in results if r.get("entity_id") == entity_id)
    except (ValueError, KeyError, TypeError, StopIteration) as e:
        raise ControlFailure(ERR_HA_ERROR) from e
    if not result.get("ok"):
        raise ControlDenied(
            str(result.get("reason") or "action_failed"), result.get("until")
        )
    return result


# Injection point for tests:
# async fn(ha_client, token, entity_id, action, data, grant) -> result.
ha_caller = call_control_as_device


# ── control ─────────────────────────────────────────────────────────────────


def _validate_request(message: dict[str, Any]) -> tuple[str | None, str, str, dict]:
    request_id = message.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not 0 < len(request_id) <= REQUEST_ID_MAX
    ):
        raise ControlFailure(ERR_INVALID_REQUEST)
    entity_id = message.get("entity_id")
    service = message.get("service")
    data = message.get("data")
    if not is_valid_entity_id(entity_id) or not is_valid_ha_name(service):
        raise ControlFailure(ERR_INVALID_REQUEST)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ControlFailure(ERR_INVALID_REQUEST)
    try:
        size = len(json.dumps(data))
    except (TypeError, ValueError) as e:
        raise ControlFailure(ERR_INVALID_REQUEST) from e
    if size > CONTROL_DATA_MAX_BYTES:
        raise ControlFailure(ERR_INVALID_REQUEST)
    clean = {k: v for k, v in data.items() if k not in TARGET_KEYS}
    return request_id, entity_id, service, clean


def access_closed_message(
    request_id: str | None, entity_id: str, hours: dict
) -> dict[str, Any]:
    from services import kids_mode

    now = kids_mode.utcnow()
    opens = kids_mode.parse_iso(hours.get("opens_at"))
    return {
        "type": "rule_blocked",
        "request_id": request_id,
        "entity_id": entity_id,
        "reason": "access_closed",
        "level": "profile",
        "until": kids_mode.iso(opens),
        "retry_after": (max(0, int((opens - now).total_seconds())) if opens else None),
    }


async def device_token(db: Database, client_id: str) -> str | None:
    """The device's own HA token (decrypted), None while there is none."""
    row = await db.fetchone(
        "SELECT ha_token_encrypted FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not (row or {}).get("ha_token_encrypted"):
        return None
    from services.crypto import decrypt_token_with_status

    token, _err = await decrypt_token_with_status(db, row["ha_token_encrypted"])
    return token or None


async def control(
    db: Database, ha_client, client_id: str, message: dict[str, Any]
) -> dict[str, Any]:
    """Handle one ``control`` request of a paired device; returns the answer."""
    from services import access, kids_mode

    request_id = (
        message.get("request_id")
        if isinstance(message.get("request_id"), str)
        else None
    )
    try:
        request_id, entity_id, service, data = _validate_request(message)
    except ControlFailure as e:
        return error_message(request_id, e.reason)

    device = await hub_device(db, client_id)
    if device is None:
        return error_message(request_id, ERR_NOT_HUB_CONTROLLED)
    # Outside its access the device switches nothing.
    await kids_mode.hub_time_zone(ha_client)
    hours = access.state_of(device.row, device.profile, device.effective)
    if not hours["open"]:
        return access_closed_message(request_id, entity_id, hours)
    layers = kids_mode.layers_of(device.effective) if device.child else None
    if device.child and layers is None:
        # Parental controls whose kids rules cannot be read: fail closed.
        return error_message(request_id, ERR_NOT_HUB_CONTROLLED)

    visible = await visible_entities(db, client_id)
    if entity_id not in visible:
        return error_message(request_id, ERR_NOT_VISIBLE)

    # Reads with an HA round trip before the lock (bounded): the entity's
    # state for the kids rules.
    state = (
        await read_state(ha_client, entity_id, STATE_READ_SECONDS)
        if layers is not None
        else None
    )
    # One action of a device at a time (a child's watch shares its phone's):
    # parallel requests are decided like sequential ones. The wait is short:
    # a request behind a slow HA call is answered instead of queued.
    subject = kids_mode.kids_subject(device.row) if layers is not None else client_id
    lock = _client_lock(subject)
    try:
        await asyncio.wait_for(lock.acquire(), CONTROL_LOCK_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return error_message(request_id, ERR_HA_UNAVAILABLE)
    try:
        if layers is not None:
            return await kids_mode.decide_and_run(
                db,
                ha_client,
                client_id,
                {
                    "kids_override_until": device.row.get("kids_override_until"),
                    "kids_subject": subject,
                },
                layers,
                visible[entity_id],
                request_id,
                entity_id,
                service,
                data,
                STATE_UNKNOWN if state is None else state,
            )
        return await _run(
            db, ha_client, client_id, request_id, entity_id, service, data
        )
    finally:
        lock.release()


async def run_as_device(
    db: Database,
    ha_client,
    client_id: str,
    entity_id: str,
    action: str,
    data: dict[str, Any],
    grant: str | None,
) -> dict[str, Any]:
    """``<ns>.control`` with the device's token, logged once: the hub's own
    entry stands for the call, the integration's report of it is dropped
    (``event_log.expect_hub_call``). Raises like :func:`call_control_as_device`;
    :class:`ControlFailure` ``not_ready`` without a token."""
    from services import enforcement, event_log

    token = await device_token(db, client_id)
    if not token:
        raise ControlFailure(ERR_NOT_READY)
    if enforcement.is_token_held(client_id) or not enforcement.rules_delivered(
        client_id
    ):
        # A device that just got its HA user: its rules go to the integration
        # first (bounded), so the call does not meet a device without rules.
        await enforcement.push_now("hub_control", RULES_WAIT_SECONDS)
        if enforcement.is_token_held(client_id):
            raise ControlFailure(ERR_NOT_READY)
    mark = event_log.expect_hub_call(client_id, entity_id, action)
    try:
        return await ha_caller(ha_client, token, entity_id, action, data, grant)
    except ControlFailure as e:
        # Nothing reached the integration: nothing of it to drop.
        event_log.forget_hub_call(mark)
        if e.status == 400 and not enforcement.rules_delivered(client_id):
            # The integration refused the call before the device's rules
            # arrived: not ready yet, the app tries again.
            raise ControlFailure(ERR_NOT_READY) from e
        raise


async def _run(
    db: Database,
    ha_client,
    client_id: str,
    request_id: str | None,
    entity_id: str,
    service: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    """A device on the hub path without parental controls: no kids state,
    no grant; the integration checks the call again with the device's
    rule snapshot."""
    from services import event_log

    try:
        result = await run_as_device(
            db, ha_client, client_id, entity_id, service, data, None
        )
    except ControlFailure as e:
        return error_message(request_id, e.reason)
    except ControlDenied as e:
        await event_log.hub_control(
            db,
            client_id,
            entity_id=entity_id,
            action=service,
            ok=False,
            reason=e.reason,
            until=e.until,
            child=False,
        )
        return error_message(
            request_id, DENIAL_TO_ERROR.get(e.reason, ERR_INVALID_REQUEST)
        )
    if ha_client is not None:
        try:
            ha_client.invalidate_state_cache(entity_id)
        except Exception:  # noqa: BLE001
            pass
    log_service_call(entity_id.split(".", 1)[0], service, client_id, entity_id)
    await event_log.hub_control(
        db, client_id, entity_id=entity_id, action=service, ok=True, child=False
    )
    answer: dict[str, Any] = {
        "type": "control_ok",
        "request_id": request_id,
        "entity_id": entity_id,
    }
    adjusted = (result or {}).get("adjusted")
    if adjusted:
        answer["adjusted"] = adjusted
    return answer
