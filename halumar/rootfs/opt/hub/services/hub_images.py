"""Still images for devices on the hub path.

A device on the hub path (``services.hub_control``) may hold no Home
Assistant key, and the states it gets carry no token-bearing picture URL
(``authz.sanitize_state``). So the hub serves the images itself:

* ``camera_image_get {request_id, entity_id, width?}`` → ``camera_image``
  (Home Assistant's ``/api/camera_proxy/<entity_id>``)
* ``media_image_get {request_id, entity_id}`` → ``media_image`` (the artwork
  of a media player, ``/api/media_player_proxy/<entity_id>``)

Each answer is ``{request_id, entity_id, content_type, data_b64}`` or a
``control_error`` / ``rule_blocked`` with the same ``request_id``. Checks, in
this order, so that nothing about an entity outside the scope leaks:

1. the request's shape: request id, entity id, ``width`` 64–3840, and no
   request of this device with the same request id still open (else
   ``invalid_request``; the open one continues);
2. the device is on the hub path (else ``not_hub_controlled``);
3. the entity is of the message's domain and in the device's scope, which is
   empty while its access is closed (else ``not_visible``, the same answer in
   every case; Home Assistant is not asked and no kids rule is read);
4. the limits (contract x-limits ``hub_image_*``): one open request and at
   most one per second per entity and device; per device at most
   :data:`DEVICE_OPEN_MAX` open requests and, per
   :data:`DEVICE_WINDOW_SECONDS`, :data:`DEVICE_IMAGES_PER_WINDOW` images and
   :data:`DEVICE_BYTES_PER_WINDOW` bytes; all devices together at most
   :data:`HUB_OPEN_MAX` open reads, of which one device holds at most
   :data:`DEVICE_HUB_SHARE` (lower than the contract's maximum, which the
   contract allows) (``rate_limited`` with ``retry_after``);
5. with parental controls the kids rule ``kids_blocked_types`` and the
   entity and area blocks (``rule_blocked {blocked}``);
6. the image, read with the hub's own access to Home Assistant within
   :data:`FETCH_TIMEOUT_SECONDS` for the whole read, without following a redirect, at most
   :data:`MAX_IMAGE_BYTES` and one of :data:`CONTENT_TYPES` (else
   ``ha_error``; Home Assistant not reachable: ``ha_unavailable``).

``width`` is a best-effort hint: the hub passes ``width`` and ``height =
ceil(width * 9 / 16)`` to the camera proxy, which scales only JPEG, only down
and by fixed factors to at least that size; the hub never scales itself.
An answer is at most :data:`MESSAGE_MAX_BYTES` long. The hub keeps no copy of
an image, writes none to disk and logs nothing about its content; the
device never gets a Home Assistant URL or token.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import time
from typing import Any

import httpx

from models.database import Database
from services import hub_control
from services.ha_client import ImageTooLarge, is_valid_entity_id

logger = logging.getLogger("hub.services.hub_images")

# Contract: x-limits hub_image_max_bytes (2 MiB of image data).
MAX_IMAGE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10.0
WIDTH_RANGE = (64, 3840)
# Image types a device may get; aliases Home Assistant or cameras send.
CONTENT_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")
CONTENT_TYPE_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
# Budget per device, all entities together (x-limits hub_image_*).
DEVICE_OPEN_MAX = 4
DEVICE_IMAGES_PER_WINDOW = 20
DEVICE_BYTES_PER_WINDOW = 16 * 1024 * 1024
DEVICE_WINDOW_SECONDS = 10.0
MESSAGE_MAX_BYTES = 2_800_000
# Image reads of all devices together, so slow cameras never take the HA
# connections the hub's other calls need.
HUB_OPEN_MAX = 8
# A device's share of them (lower than the contract's maximum DEVICE_OPEN_MAX,
# which a hub may undercut), so two slow cameras never block every device.
DEVICE_HUB_SHARE = 2

KINDS = {
    # message type: (domain, Home Assistant proxy, answer type, width hint)
    "camera_image_get": ("camera", "camera_proxy", "camera_image", True),
    "media_image_get": ("media_player", "media_player_proxy", "media_image", False),
}

# Open requests: (device, entity) and (device, request id).
_open: set[tuple[str, str]] = set()
_open_ids: set[tuple[str, str]] = set()
# Accepted requests per device: [monotonic time, image bytes].
_device_window: dict[str, list[list[float]]] = {}


def reset() -> None:
    """Forget open requests and budgets (tests)."""
    _open.clear()
    _open_ids.clear()
    _device_window.clear()


def content_type_of(header: str | None) -> str | None:
    """The normalized image type of a Content-Type header, None when the type
    may not go to a device (parameters dropped, lower case, aliases mapped)."""
    value = (header or "").split(";", 1)[0].strip().lower()
    value = CONTENT_TYPE_ALIASES.get(value, value)
    return value if value in CONTENT_TYPES else None


def _retry(seconds: float) -> int:
    return max(1, int(seconds + 0.999))


def _device_retry_after(client_id: str, now: float) -> tuple[int | None, list | None]:
    """``(retry_after, slot)``: the device's budget; a slot (time, bytes) is
    taken when the request may run (its bytes are filled in afterwards)."""
    if sum(1 for c, _e in _open if c == client_id) >= min(
        DEVICE_OPEN_MAX, DEVICE_HUB_SHARE
    ):
        return 1, None
    recent = [
        e
        for e in _device_window.get(client_id, ())
        if now - e[0] < DEVICE_WINDOW_SECONDS
    ]
    _device_window[client_id] = recent
    if len(recent) >= DEVICE_IMAGES_PER_WINDOW:
        return _retry(DEVICE_WINDOW_SECONDS - (now - recent[0][0])), None
    if sum(e[1] for e in recent) >= DEVICE_BYTES_PER_WINDOW:
        # Until enough bytes leave the window.
        total, wait = sum(e[1] for e in recent), 0.0
        for t, size in recent:
            total -= size
            wait = DEVICE_WINDOW_SECONDS - (now - t)
            if total < DEVICE_BYTES_PER_WINDOW:
                break
        return _retry(wait), None
    if len(_device_window) > hub_control.LIMITER_MAX:
        _device_window.clear()
    slot = [now, 0]
    _device_window.setdefault(client_id, []).append(slot)
    return None, slot


def _width_of(message: dict[str, Any]) -> tuple[bool, int | None]:
    """``(valid, width)`` of the optional width hint."""
    width = message.get("width")
    if width is None:
        return True, None
    if isinstance(width, bool) or not isinstance(width, int):
        return False, None
    return WIDTH_RANGE[0] <= width <= WIDTH_RANGE[1], width


async def _kids_blocked(
    db: Database,
    device: hub_control.HubDevice,
    entity_id: str,
    areas: list[str],
    ha_client,
) -> str | None:
    """The level whose kids rule blocks the entity (blocked device types,
    entity or area blocks; an entity release of a parent lifts the device
    type), None when it may be shown."""
    from services import kids_mode, kids_rules
    from services.family import service as family

    layers = kids_mode.layers_of(device.effective)
    if layers is None:
        # Parental controls whose kids rules cannot be read: fail closed.
        return kids_rules.LEVEL_PROFILE
    state = await hub_control.read_state(
        ha_client, entity_id, hub_control.STATE_READ_SECONDS
    )
    subject = kids_mode.kids_subject(device.row)
    releases = await family.releases_for_action(db, subject, entity_id)
    for area in [layers["areas"].get(a) for a in areas] or [None]:
        level = kids_rules.access_blocked(
            kids_rules.Layers(
                profile=layers["rules"],
                area=area,
                entity=layers["entities"].get(entity_id),
            ),
            entity_id,
            state,
            skip_types=releases.types,
        )
        if level is not None:
            return level
    return None


async def image(
    db: Database, ha_client, client_id: str, message: dict[str, Any]
) -> dict[str, Any]:
    """Answer to ``camera_image_get`` / ``media_image_get``."""
    from services import kids_mode, kids_rules

    domain, proxy, answer_type, takes_width = KINDS[message.get("type")]
    rid = hub_control.request_id_of(message)
    entity_id = message.get("entity_id")
    valid_width, width = _width_of(message) if takes_width else (True, None)
    if (
        rid is None
        or not is_valid_entity_id(entity_id)
        or not valid_width
        or (client_id, rid) in _open_ids
    ):
        return hub_control.error_message(rid, hub_control.ERR_INVALID_REQUEST)
    device = await hub_control.hub_device(db, client_id)
    if device is None:
        return hub_control.error_message(rid, hub_control.ERR_NOT_HUB_CONTROLLED)
    # Scope next: a closed access has an empty scope; another domain, an
    # unknown or an invisible entity are all not_visible.
    visible = await hub_control.visible_entities(db, client_id)
    if entity_id.split(".", 1)[0] != domain or entity_id not in visible:
        return hub_control.error_message(rid, hub_control.ERR_NOT_VISIBLE)
    key = (client_id, entity_id)
    if key in _open or len(_open) >= HUB_OPEN_MAX:
        return hub_control.error_message(rid, hub_control.ERR_RATE_LIMITED, 1)
    wait = hub_control.image_retry_after(client_id, entity_id)
    slot = None
    if wait is None:
        wait, slot = _device_retry_after(client_id, time.monotonic())
    if wait is not None:
        return hub_control.error_message(rid, hub_control.ERR_RATE_LIMITED, wait)
    _open.add(key)
    _open_ids.add((client_id, rid))
    try:
        if device.child:
            level = await _kids_blocked(
                db, device, entity_id, visible[entity_id], ha_client
            )
            if level is not None:
                return kids_mode.blocked_message(
                    rid,
                    entity_id,
                    kids_rules.Decision(False, kids_rules.REASON_BLOCKED, level),
                    kids_mode.utcnow(),
                )
        if ha_client is None:
            return hub_control.error_message(rid, hub_control.ERR_HA_UNAVAILABLE)
        try:
            # FETCH_TIMEOUT_SECONDS for the whole read, not per chunk.
            header, data = await asyncio.wait_for(
                ha_client.get_image(
                    proxy,
                    entity_id,
                    max_bytes=MAX_IMAGE_BYTES,
                    timeout=FETCH_TIMEOUT_SECONDS,
                    width=width,
                    height=math.ceil(width * 9 / 16) if width else None,
                ),
                FETCH_TIMEOUT_SECONDS,
            )
        except ImageTooLarge:
            logger.info("Image of %s is larger than the limit", entity_id)
            return hub_control.error_message(rid, hub_control.ERR_HA_ERROR)
        except httpx.HTTPStatusError as e:
            logger.info(
                "Image of %s: Home Assistant answered %s",
                entity_id,
                e.response.status_code,
            )
            if hub_control.unreachable(e):
                return hub_control.error_message(rid, hub_control.ERR_HA_UNAVAILABLE)
            return hub_control.error_message(rid, hub_control.ERR_HA_ERROR)
        except (httpx.HTTPError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            logger.info(
                "Image of %s: Home Assistant not reachable (%s)",
                entity_id,
                type(e).__name__,
            )
            return hub_control.error_message(rid, hub_control.ERR_HA_UNAVAILABLE)
        slot[1] = len(data)
        content_type = content_type_of(header)
        if content_type is None or not data:
            logger.info("Image of %s has no usable image type", entity_id)
            return hub_control.error_message(rid, hub_control.ERR_HA_ERROR)
        answer = {
            "type": answer_type,
            "request_id": rid,
            "entity_id": entity_id,
            "content_type": content_type,
            "data_b64": base64.b64encode(data).decode("ascii"),
        }
        if len(answer["data_b64"]) + 512 > MESSAGE_MAX_BYTES:
            return hub_control.error_message(rid, hub_control.ERR_HA_ERROR)
        return answer
    finally:
        _open.discard(key)
        _open_ids.discard((client_id, rid))
