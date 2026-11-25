"""Read authorization for paired clients.

Paired apps and guests read areas (with live entity states fetched by the
add-on with the Supervisor token) over the WebSocket. This module decides what
a paired client may read (``api/ws/client.py``). Paired clients have no REST
access, and the WebSocket has no message that reads an arbitrary HA state.

Read scope of a paired client
-----------------------------
* **Areas:** exactly the areas the client's ``get_areas`` list shows: the
  active areas of its effective profile rule ``areas`` (all, or the
  selected areas, the areas with one of the selected tags and the device's
  own area). Guests have no implicit areas (fail closed); a selected area
  that was deleted matches nothing.
  Additionally an active area that is the tap target of one of the
  client's own (not dismissed) notifications: notification links are
  admin-authored, so they may open an area outside the list.
* Admin sessions (``ingress``/``dev``) are not restricted.

Every state that leaves the hub towards a paired client goes through
:func:`sanitize_state`: camera ``access_token``, token-bearing
``entity_picture`` URLs and GPS fields of ``device_tracker``/``person`` are
removed. Area payloads carry no server file paths.

Pictures of media players and cameras
-------------------------------------
Home Assistant's picture of these entities is a link with the entity's
access token (``/api/camera_proxy/…?token=…``,
``/api/media_player_proxy/…?token=…&cache=…``), and ``entity_picture_local``
is the same proxy link. So for these domains no picture link is relayed:
``entity_picture`` and ``entity_picture_local`` are removed, and so is every
attribute whose value (also inside lists and objects) is a link or carries a
``token=``/``authSig=`` parameter (``media_content_id`` of a stream, the
``stream_source``/``still_image_url`` some camera integrations add, …).
Instead ``attributes.hub_picture_id`` marks the picture
(:func:`picture_id`): it is there exactly when Home Assistant's state has
an ``entity_picture``, changes with the artwork (the ``cache`` parameter)
and stays the same when only the token rotates. The apps fetch the image
through the hub (``media_image_get``/``camera_image_get``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from models.database import Database

# Same set as api/ws/registry.ADMIN_SESSION_IDS (not imported: cycle).
ADMIN_SESSION_IDS = frozenset({"ingress", "dev", "admin"})

# Attributes that grant access on their own (camera_proxy?token=…).
SENSITIVE_ATTRIBUTES = ("access_token",)

logger = logging.getLogger("hub.api.authz")

# Domains whose picture is a token link: never relayed, marked instead.
PICTURE_DOMAINS = ("media_player", "camera")
PICTURE_ID_ATTRIBUTE = "hub_picture_id"
# Fixed purpose label of the marker's key (crypto.derived_key).
PICTURE_ID_PURPOSE = b"hub picture marker v1"
PICTURE_ID_LENGTH = 16
# Link attributes of these domains that are always removed.
PICTURE_LINK_ATTRIBUTES = ("entity_picture", "entity_picture_local")
# Query parameters that grant access on their own (HA proxy token, signed path).
TOKEN_PARAMETERS = ("token=", "authsig=")

# Location fields of the people/tracker domains (GPS of household members).
GPS_DOMAINS = ("device_tracker", "person")
GPS_ATTRIBUTES = (
    "latitude",
    "longitude",
    "gps_accuracy",
    "altitude",
    "course",
    "speed",
    "vertical_accuracy",
)

# Server file paths in area rows; clients get the servable *_src instead.
SERVER_PATH_KEYS = ("card_image_path", "bg_light_path", "bg_dark_path")


# Reserved client tag "all areas" in the integration's client tags (the
# areas rule shown as tags). Never an area tag (rejected by the area
# schemas).
ALL_AREAS_TAG = "*"

# Reserved client tag prefix: "area:<area id>" stands for one area of the
# areas rule. Never an area tag (rejected by the area schemas).
AREA_PIN_PREFIX = "area:"


def area_pin(area_id: str) -> str:
    """Client tag that stands for ``area_id``."""
    return f"{AREA_PIN_PREFIX}{area_id}"


def is_reserved_area_tag(tag: str) -> bool:
    """Whether ``tag`` may not be used as an area tag ("*", "area:…")."""
    return tag == ALL_AREAS_TAG or tag.startswith(AREA_PIN_PREFIX)


def is_admin_session(client_id: str | None) -> bool:
    """True for admin sessions (Ingress, dev auto-auth, web-UI cookie)."""
    return client_id in ADMIN_SESSION_IDS


@dataclass(frozen=True)
class AreaScope:
    """Areas a device sees: the effective profile rule ``areas``."""

    all: bool = False
    area_ids: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()

    def contains(self, area_id: str | None, area_tags: list[str]) -> bool:
        if self.all:
            return True
        if area_id and area_id in self.area_ids:
            return True
        return any(t in self.tags for t in area_tags)

    def as_tags(self) -> list[str]:
        """The scope in the integration's client-tag form."""
        if self.all:
            return [ALL_AREAS_TAG]
        return list(self.tags) + [area_pin(a) for a in self.area_ids]


NO_AREAS = AreaScope()
ALL_AREAS = AreaScope(all=True)


def scope_from_rule(value: Any) -> AreaScope:
    """Scope of a resolved ``areas`` value (missing/invalid = none, fail closed)."""
    if not isinstance(value, dict):
        return NO_AREAS
    if value.get("mode") == "all":
        return ALL_AREAS
    return AreaScope(
        False,
        tuple(
            dict.fromkeys(a for a in value.get("area_ids", []) if isinstance(a, str))
        ),
        tuple(dict.fromkeys(t for t in value.get("tags", []) if isinstance(t, str))),
    )


async def client_area_scope(
    db: Database, client_id: str | None, *, ignore_access: bool = False
) -> AreaScope:
    """Area scope of a paired client; admin sessions see everything.

    A missing, revoked or deleted client sees nothing (fail closed);
    an expired guest neither (the resolver sets every rule to its strictest
    value); a device outside its access hours or with its access switch off
    neither unless ``ignore_access`` (the configured scope, e.g. for the
    rule snapshot that the integration evaluates with its own clock).
    """
    if not client_id or is_admin_session(client_id):
        return ALL_AREAS
    from services.profiles import store

    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        return NO_AREAS
    client, profile, rules = resolved
    if not client.get("is_active") or client.get("pending_deletion_at"):
        return NO_AREAS
    if not ignore_access:
        from services import access

        if not access.is_open(client, profile, rules):
            return NO_AREAS
    return scope_from_rule(rules["areas"]["value"])


def area_in_scope(
    scope: AreaScope, area_tags: list[str], area_id: str | None = None
) -> bool:
    """List rule for one active area (``get_areas``, :func:`listed_area_ids`)."""
    return scope.contains(area_id, area_tags)


async def listed_area_ids(
    db: Database, client_id: str | None, *, ignore_access: bool = False
) -> set[str]:
    """Active area ids in the client's area list (same rule as ``get_areas``)."""
    scope = await client_area_scope(db, client_id, ignore_access=ignore_access)
    return await scope_area_ids(db, scope)


async def scope_area_ids(db: Database, scope: AreaScope) -> set[str]:
    """Active area ids within ``scope``."""
    rows = await db.fetchall("SELECT id FROM areas WHERE is_active = 1")
    active = {r["id"] for r in rows}
    if scope.all:
        return active
    tag_rows = await db.fetchall("SELECT area_id, tag FROM area_tags")
    tagged = {r["area_id"] for r in tag_rows if r["tag"] in scope.tags}
    return {a for a in active if a in scope.area_ids or a in tagged}


async def _notification_targets(
    db: Database, client_id: str, action_type: str
) -> set[str]:
    rows = await db.fetchall(
        "SELECT action_target FROM client_notifications "
        "WHERE client_id = ? AND status != 'dismissed' AND action_type = ? "
        "AND action_target IS NOT NULL AND action_target != ''",
        (client_id, action_type),
    )
    return {r["action_target"] for r in rows}


async def readable_area_ids(db: Database, client_id: str | None) -> set[str]:
    """Listed areas plus active areas deep-linked by the client's notifications."""
    ids = await listed_area_ids(db, client_id)
    if not client_id:
        return ids
    if not ids and not await _access_open(db, client_id):
        return ids  # no notification deep link outside the access
    linked = await _notification_targets(db, client_id, "area") - ids
    if linked:
        placeholders = ",".join("?" for _ in linked)
        rows = await db.fetchall(
            f"SELECT id FROM areas WHERE is_active = 1 AND id IN ({placeholders})",
            tuple(linked),
        )
        ids |= {r["id"] for r in rows}
    return ids


async def _access_open(db: Database, client_id: str) -> bool:
    from services import access

    return await access.client_open(db, client_id)


async def can_read_area(db: Database, client_id: str | None, area_id: str) -> bool:
    """Whether ``client_id`` may read the detail of ``area_id``."""
    if is_admin_session(client_id):
        return True
    if not client_id or not area_id:
        return False
    return area_id in await readable_area_ids(db, client_id)


def picture_id(picture: str) -> str | None:
    """Marker of a picture link (``hub_picture_id``), None without a key.

    The first 16 hex characters of HMAC-SHA-256 over the link without its
    ``token`` query parameter (the other parameters sorted, no fragment),
    keyed with this hub's key for the purpose :data:`PICTURE_ID_PURPOSE`.
    """
    from services import crypto

    try:
        key = crypto.derived_key(PICTURE_ID_PURPOSE)
    except Exception as e:  # noqa: BLE001 - no marker rather than no state
        logger.warning("Picture marker not available: %s", type(e).__name__)
        return None
    parts = urlsplit(picture)
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k != "token"
    )
    normalized = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )
    digest = hmac.new(key, normalized.encode(), hashlib.sha256).hexdigest()
    return digest[:PICTURE_ID_LENGTH]


def _carries_link(value: Any, depth: int = 0) -> bool:
    """Whether ``value`` (or a string inside it) is a link or carries a token."""
    if isinstance(value, str):
        low = value.lower()
        return (
            "://" in low
            or low.startswith("/api/")
            or any(p in low for p in TOKEN_PARAMETERS)
        )
    if depth >= 4:
        return False
    if isinstance(value, dict):
        return any(_carries_link(v, depth + 1) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_carries_link(v, depth + 1) for v in value)
    return False


def sanitize_attributes(
    entity_id: str, attributes: dict[str, Any] | None
) -> dict[str, Any]:
    """Copy of ``attributes`` without tokens, picture links and (for
    people/trackers) GPS; media players and cameras get ``hub_picture_id``."""
    if not isinstance(attributes, dict):
        return {}
    domain = entity_id.split(".", 1)[0] if isinstance(entity_id, str) else ""
    clean = {k: v for k, v in attributes.items() if k not in SENSITIVE_ATTRIBUTES}
    if domain in PICTURE_DOMAINS:
        picture = attributes.get("entity_picture")
        clean = {
            k: v
            for k, v in clean.items()
            if k not in PICTURE_LINK_ATTRIBUTES
            and k != PICTURE_ID_ATTRIBUTE
            and not _carries_link(v)
        }
        if isinstance(picture, str) and picture:
            marker = picture_id(picture)
            if marker:
                clean[PICTURE_ID_ATTRIBUTE] = marker
        return clean
    for key in PICTURE_LINK_ATTRIBUTES:
        value = clean.get(key)
        if isinstance(value, str) and any(p in value.lower() for p in TOKEN_PARAMETERS):
            clean.pop(key)
    if domain in GPS_DOMAINS:
        for key in GPS_ATTRIBUTES:
            clean.pop(key, None)
    return clean


def sanitize_state(
    entity_id: str, state: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Copy of a state dict with sanitized ``attributes`` (input untouched)."""
    if not isinstance(state, dict):
        return state
    clean = dict(state)
    if "attributes" in clean:
        clean["attributes"] = sanitize_attributes(entity_id, clean.get("attributes"))
    return clean


def strip_server_paths(area: dict[str, Any]) -> dict[str, Any]:
    """Remove server file paths from an area dict (in place) and return it."""
    for key in SERVER_PATH_KEYS:
        area.pop(key, None)
    return area


def _visible(entity: Any) -> bool:
    """A placement the device may see (hidden ones are not sent: neither the
    placement nor its state)."""
    return not isinstance(entity, dict) or entity.get("is_visible") in (None, 1, True)


def redact_area_for_client(area: dict[str, Any]) -> dict[str, Any]:
    """Strip server paths, drop hidden placements and sanitize every entity
    state of an area detail."""
    strip_server_paths(area)
    for section in area.get("sections") or []:
        if isinstance(section.get("entities"), list):
            section["entities"] = [e for e in section["entities"] if _visible(e)]
        for entity in section.get("entities") or []:
            if isinstance(entity, dict) and entity.get("state") is not None:
                entity["state"] = sanitize_state(
                    entity.get("entity_id", ""), entity["state"]
                )
            tile = entity.get("tile") if isinstance(entity, dict) else None
            if isinstance(tile, dict) and tile.get("secondary_state") is not None:
                sec = tile.get("secondary") or {}
                tile["secondary_state"] = sanitize_state(
                    sec.get("entity_id", ""), tile["secondary_state"]
                )
    return area
