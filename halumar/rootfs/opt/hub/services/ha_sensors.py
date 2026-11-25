"""HA Sensor sync service for Halumar.

Creates and maintains Home Assistant sensor entities that mirror the
Halumar area/entity layout. This allows iOS/mobile apps to read the
full layout configuration via the HA WebSocket API, even when connecting
remotely through Nabu Casa (where Halumar's own WebSocket is not reachable).

Sensors created:
- sensor.halumar_config: Overview of all ACTIVE areas (state = area count)
- sensor.halumar_area_{slug}: Per-area detail (state = entity count).
  Only for active areas — deactivating an area deletes its sensor.
- sensor.halumar_guests: Guest overview (state = guest count). Attributes
  carry only client_id/is_connected/expires_at per guest — no names or tags.
- sensor.halumar_client_{slug}_{uid8}_notifications: Per-client notification
  sensor (state = active count) carrying the FULL notification content in the
  ``notifications`` attribute (push-shaped items). It is the REMOTE delivery
  path: the hub is reachable only on the home network, so away from home the
  app reaches only Home Assistant (e.g. Home Assistant Cloud) and this sensor is the only notification source.
  Any HA user/token can read states — notification content is world-readable
  within HA BY DESIGN and treated as not confidential. Clients receive this
  entity id via client_config (notification_sensor) and never derive it
  themselves.

Input booleans created:
- input_boolean.halumar_area_{slug}: Per-area activation switch.
  When its state is changed in HA (states API), the area's is_active state
  changes in Halumar (state_changed event over the live connection).
  Deactivated areas are hidden from all external clients. The switch exists
  for ALL areas (it is the way to re-activate a hidden one).

Sensors and input booleans are updated automatically whenever areas,
entities, widgets, or grid layouts change in Halumar.
"""

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
import weakref
from typing import Any

import namespace
from brand import BRAND_NAME
from models.database import Database
from version import VERSION

logger = logging.getLogger("hub.services.ha_sensors")

# ALL Halumar entity slugs (area, switch, client) are capped at this length.
# HA's hard entity_id limit is 255; with the longest prefix
# ('sensor.halumar_client_' = 23) + slug (48) + '_' + uid8 (8) +
# '_notifications' (14) the worst case stays under 100 chars.
SLUG_MAX_LEN = 48

# Latin letters NFKD does not decompose, spelled as HA's slugify
# (python-slugify/text-unidecode) does.
_TRANSLITERATE = str.maketrans(
    {
        "ß": "ss",
        "ẞ": "ss",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
        "ø": "o",
        "Ø": "o",
        "đ": "d",
        "Đ": "d",
        "ð": "d",
        "Ð": "d",
        "ł": "l",
        "Ł": "l",
        "þ": "th",
        "Þ": "th",
        "ħ": "h",
        "Ħ": "h",
        "ı": "i",
        "ŋ": "ng",
        "Ŋ": "ng",
        "ŧ": "t",
        "Ŧ": "t",
    }
)


def _slugify(text: str, fallback: str = "unknown", max_len: int | None = None) -> str:
    """Convert text to a HA-safe slug (``fallback`` when nothing remains).

    Like HA's ``slugify``: one ``_`` between words, none at either end, also
    after capping at ``max_len`` (a cut right after a separator left a
    trailing ``_`` that became ``__`` in the entity id: HA 400).

    'Wohnzimmer' -> 'wohnzimmer'
    'Büro OG' -> 'buro_og'
    'Straße 1,5' -> 'strasse_15'
    'Living Room 2' -> 'living_room_2'
    """
    text = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if max_len is not None:
        text = text[:max_len].rstrip("_")
    return text or fallback


def _uid8(object_id: str) -> str:
    """First 8 alphanumerics of an id (UUID: the first 8 hex digits)."""
    return re.sub(r"[^a-z0-9]", "", object_id.lower())[:8] or "0"


def _area_slug(name: str, area_id: str) -> str:
    """Generate a stable, collision-free slug for area sensor entity IDs.

    Includes the first 8 chars of the area UUID to prevent collisions
    (e.g. 'Büro' vs 'Buro') and to keep entity IDs stable across renames.

    Slug is capped at SLUG_MAX_LEN (48) chars — see the constant above.

    'Wohnzimmer', 'abc12345-...' -> 'wohnzimmer_abc12345'
    """
    return f"{_slugify(name, max_len=SLUG_MAX_LEN)}_{_uid8(area_id)}"


def client_notification_sensor_id(client_name: str, client_id: str) -> str:
    """Entity ID of a client's notification sensor (single source of truth).

    Collision-free analogue of _area_slug for clients: appends the first 8
    chars of the lowercased, dash-stripped client UUID so two clients with the
    same name get distinct sensors. Used by BOTH the sensor sync below and
    _get_client_config (api/websocket.py), which delivers the id to the client
    as ``notification_sensor`` — clients never derive it themselves.

    slug = _slugify(name, max_len=48) with fallback "client" when the name
    slugifies to nothing; uid8 = first 8 chars of the lowercased, dash-stripped id.

    'Kitchen Panel', 'ABC12345-6789-...' ->
    'sensor.halumar_client_kitchen_panel_abc12345_notifications'
    """
    slug = _slugify(client_name, fallback="client", max_len=SLUG_MAX_LEN)
    return namespace.client_notification_sensor_id(slug, _uid8(client_id))


def client_quick_access_sensor_id(client_name: str, client_id: str) -> str:
    """Entity ID of a client's quick access check sensor.

    Same slug/uid8 rule as :func:`client_notification_sensor_id`; delivered to
    the client as ``client_config.quick_access.sensor``.
    """
    slug = _slugify(client_name, fallback="client", max_len=SLUG_MAX_LEN)
    return namespace.client_quick_access_sensor_id(slug, _uid8(client_id))


def _image_src(image_path: str | None) -> str | None:
    """Convert filesystem path to servable URL."""
    if not image_path:
        return None
    import os

    return f"/images/{os.path.basename(image_path)}"


# ── Writes to HA: change detection, ordering, echo guard ──────────
#
# A sensor is only written when its content (without ``updated_at``) changed
# or HA lost it, so the recorder does not store a new row per sync and a
# notification change does not cost one call per paired client. Two syncs of
# the same sensor never interleave (an older read never overwrites a newer
# write). The bookkeeping lives per HA client (one in production, a fresh one
# per test).

STATES_URL = "http://supervisor/core/api/states/"


class _WriteState:
    def __init__(self) -> None:
        self.posted: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        # Last area-switch state the hub wrote itself, to tell its own echo
        # from a change made in HA.
        self.switch_written: dict[str, str] = {}
        self.initial_sync_done = False
        self.last_resync: float | None = None
        # Notification ids the integration already handed over (taken over,
        # duplicate or refused): never carried over from a sensor again.
        self.handover_resolved: dict[str, None] = {}


_write_states: "weakref.WeakKeyDictionary[Any, _WriteState]" = (
    weakref.WeakKeyDictionary()
)


def _ws_state(ha_client) -> _WriteState:
    try:
        state = _write_states.get(ha_client)
        if state is None:
            state = _WriteState()
            _write_states[ha_client] = state
        return state
    except TypeError:  # not weak-referenceable: no bookkeeping
        return _WriteState()


def _lock(ha_client, key: str) -> asyncio.Lock:
    locks = _ws_state(ha_client).locks
    lock = locks.get(key)
    if lock is None:
        lock = locks[key] = asyncio.Lock()
    return lock


def _digest(payload: dict) -> str:
    attributes = dict(payload.get("attributes") or {})
    attributes.pop("updated_at", None)
    body = json.dumps(
        {"state": payload.get("state"), "attributes": attributes},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


async def _post_state(ha_client, entity_id: str, payload: dict) -> bool:
    """Write ``payload`` unless HA already has exactly this content.

    Returns True when a write happened.
    """
    state = _ws_state(ha_client)
    digest = _digest(payload)
    known = getattr(ha_client, "state_known", None)
    present = known(entity_id) if callable(known) else None
    if state.posted.get(entity_id) == digest and present is not False:
        return False
    await ha_client._post(f"{STATES_URL}{entity_id}", payload)
    state.posted[entity_id] = digest
    return True


async def _delete_state(ha_client, entity_id: str) -> None:
    _ws_state(ha_client).posted.pop(entity_id, None)
    known = getattr(ha_client, "state_known", None)
    if callable(known) and known(entity_id) is False:
        return  # already gone (live mirror): no call
    await ha_client._delete(f"{STATES_URL}{entity_id}")


def forget_written_states(ha_client) -> None:
    """Write every sensor again on the next sync (HA lost them)."""
    _ws_state(ha_client).posted.clear()


async def _sync_config_sensor(db: Database, ha_client) -> None:
    """Unserialized body of :func:`sync_config_sensor`."""
    try:
        # Only ACTIVE areas — the config sensor is the client's offline layout
        # source, so it must match the WS path (_get_areas), which filters
        # is_active=1. Otherwise a deactivated ("hidden") area reappears in the
        # app's sensor fallback when the hub is stopped.
        areas = await db.fetchall(
            "SELECT a.*, COUNT(ae.id) as entity_count "
            "FROM areas a LEFT JOIN area_entities ae ON a.id = ae.area_id "
            "WHERE a.is_active = 1 "
            "GROUP BY a.id ORDER BY a.sort_order, a.name"
        )

        areas_data = []
        for area in areas:
            tags_rows = await db.fetchall(
                "SELECT tag FROM area_tags WHERE area_id = ?", (area["id"],)
            )
            areas_data.append(
                {
                    "id": area["id"],
                    "name": area["name"],
                    "icon": area.get("icon"),
                    "description": area.get("description"),
                    "color": area.get("color"),
                    "grid_cols": area.get("grid_cols", 4),
                    "sort_order": area.get("sort_order", 0),
                    "is_active": bool(area.get("is_active", 1)),
                    "control_mode": area.get("control_mode", "full"),
                    "entity_count": area["entity_count"],
                    "card_image_src": _image_src(area.get("card_image_path")),
                    "card_image_url": area.get("card_image_url"),
                    "bg_light_src": _image_src(area.get("bg_light_path")),
                    "bg_light_url": area.get("bg_light_url"),
                    "bg_dark_src": _image_src(area.get("bg_dark_path")),
                    "bg_dark_url": area.get("bg_dark_url"),
                    "tags": [r["tag"] for r in tags_rows],
                    "sensor_entity_id": namespace.area_sensor_id(
                        _area_slug(area["name"], area["id"])
                    ),
                }
            )

        # Every HA user can read this sensor, guests included. The external
        # URL goes only to devices with remote access, per device through
        # client_config, so it is never published here.
        # Never the ha_token either.
        ha_internal_url = await db.get_setting("ha_internal_url")

        payload = {
            "state": str(len(areas)),
            "attributes": {
                "friendly_name": f"{BRAND_NAME} Configuration",
                "icon": "mdi:floor-plan",
                "areas": areas_data,
                "ha_url": None,
                "ha_internal_url": ha_internal_url,  # Internal URL for LAN
                "version": VERSION,
                "updated_at": Database.now(),
                "unit_of_measurement": "areas",
            },
        }

        await _post_state(ha_client, namespace.CONFIG_SENSOR_ID, payload)
        logger.info("Synced config sensor (%d areas)", len(areas))

    except Exception as e:
        logger.error("Failed to sync config sensor: %s", e)


async def sync_config_sensor(db: Database, ha_client) -> None:
    """Sync the main config sensor with all areas overview."""
    async with _lock(ha_client, "config"):
        await _sync_config_sensor(db, ha_client)


async def _sync_area_sensor(db: Database, ha_client, area_id: str) -> None:
    """Unserialized body of :func:`sync_area_sensor`."""
    try:
        area = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
        if not area:
            logger.warning("Area %s not found for sensor sync", area_id)
            return

        if not area.get("is_active", 1):
            await delete_area_sensor(ha_client, area["name"], area_id)
            return

        # Fetch sections with entities and widgets
        sections_raw = await db.fetchall(
            "SELECT * FROM area_sections WHERE area_id = ? ORDER BY sort_order",
            (area_id,),
        )

        total_entities = 0
        sections = []
        for section in sections_raw:
            section_id = section["id"]

            entities = await db.fetchall(
                "SELECT entity_id, display_name, columns, rows, sort_order, "
                "is_visible, control_mode "
                "FROM area_entities WHERE section_id = ? ORDER BY sort_order",
                (section_id,),
            )
            total_entities += len(entities)

            widgets_raw = await db.fetchall(
                "SELECT id, widget_type, config, columns, rows, sort_order "
                "FROM area_widgets WHERE section_id = ? ORDER BY sort_order",
                (section_id,),
            )
            widgets = []
            for w in widgets_raw:
                w_dict = dict(w)
                if isinstance(w_dict.get("config"), str):
                    try:
                        w_dict["config"] = json.loads(w_dict["config"])
                    except (json.JSONDecodeError, TypeError):
                        w_dict["config"] = {}
                widgets.append(w_dict)

            sections.append(
                {
                    "id": section["id"],
                    "title": section.get("title", ""),
                    "icon": section.get("icon"),
                    "column_span": section.get("column_span", 1),
                    "columns": section.get("columns", 2),
                    "sort_order": section.get("sort_order", 0),
                    "is_collapsed": section.get("is_collapsed", 0),
                    "entities": [
                        {
                            **dict(e),
                            "control_mode": e.get("control_mode"),
                        }
                        for e in entities
                    ],
                    "widgets": widgets,
                }
            )

        # Fetch tags
        tags_rows = await db.fetchall(
            "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
        )

        slug = _area_slug(area["name"], area["id"])
        sensor_id = namespace.area_sensor_id(slug)

        payload = {
            "state": str(total_entities),
            "attributes": {
                "friendly_name": f"{BRAND_NAME}: {area['name']}",
                "icon": area.get("icon") or "mdi:room",
                "area_id": area["id"],
                "name": area["name"],
                "description": area.get("description"),
                "color": area.get("color"),
                "grid_cols": area.get("grid_cols", 4),
                "sort_order": area.get("sort_order", 0),
                "is_active": bool(area.get("is_active", 1)),
                "control_mode": area.get("control_mode", "full"),
                "card_image_src": _image_src(area.get("card_image_path")),
                "card_image_url": area.get("card_image_url"),
                "bg_light_src": _image_src(area.get("bg_light_path")),
                "bg_light_url": area.get("bg_light_url"),
                "bg_dark_src": _image_src(area.get("bg_dark_path")),
                "bg_dark_url": area.get("bg_dark_url"),
                "tags": [r["tag"] for r in tags_rows],
                "sections": sections,
                "section_count": len(sections),
                "updated_at": Database.now(),
                "unit_of_measurement": "entities",
            },
        }

        await _post_state(ha_client, sensor_id, payload)
        logger.info(
            "Synced area sensor: %s (%s, %d sections)",
            sensor_id,
            area["name"],
            len(sections),
        )

    except Exception as e:
        logger.error("Failed to sync area sensor for %s: %s", area_id, e)


async def sync_area_sensor(db: Database, ha_client, area_id: str) -> None:
    """Sync a single area's sensor with sections-based layout.

    Inactive ("hidden") areas get NO sensor: their layout must not stay
    world-readable via the states API. Deactivating an area therefore deletes
    its existing sensor; re-activating recreates it on the next sync.
    """
    async with _lock(ha_client, f"area:{area_id}"):
        await _sync_area_sensor(db, ha_client, area_id)


async def delete_area_sensor(ha_client, area_name: str, area_id: str = "") -> None:
    """Delete an area's sensor entity (area removed or deactivated).

    Uses DELETE /core/api/states/<entity_id>, which removes the entity from
    the HA state machine. 404 (already gone) is a no-op inside _delete, so
    this is safe to call on every sync of an inactive area.
    """
    try:
        slug = (
            _area_slug(area_name, area_id)
            if area_id
            else _slugify(area_name, max_len=SLUG_MAX_LEN)
        )
        sensor_id = namespace.area_sensor_id(slug)

        await _delete_state(ha_client, sensor_id)
        logger.info("Deleted area sensor: %s", sensor_id)

    except Exception as e:
        logger.error("Failed to delete area sensor for '%s': %s", area_name, e)


async def remove_renamed_area_entities(
    ha_client, old_name: str, new_name: str, area_id: str
) -> None:
    """Delete the sensor and switch under an area's previous name.

    The entity ids contain the name slug; after a rename the new ones are
    written by the regular sync and the old ones would stay behind as
    orphans in HA.
    """
    old_slug = _area_slug(old_name, area_id)
    if old_slug == _area_slug(new_name, area_id):
        return
    for entity_id in (
        namespace.area_sensor_id(old_slug),
        namespace.area_switch_id(old_slug),
    ):
        _ws_state(ha_client).switch_written.pop(entity_id, None)
        try:
            await _delete_state(ha_client, entity_id)
        except Exception as e:  # noqa: BLE001 - the next full sync cleans up
            logger.warning("Could not remove %s after rename: %s", entity_id, e)


async def remove_renamed_client_sensor(
    db: Database, ha_client, old_name: str, client_id: str
) -> None:
    """Replace a renamed client's notification sensor (id contains the name)."""
    if ha_client is None:
        return
    row = await db.fetchone(
        "SELECT name FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not row:
        return
    old_id = client_notification_sensor_id(old_name, client_id)
    if old_id == client_notification_sensor_id(row["name"], client_id):
        return
    await sync_notification_sensors_safe(db, ha_client)
    try:
        await _delete_state(ha_client, old_id)
    except Exception as e:  # noqa: BLE001 - the next full sync cleans up
        logger.warning("Could not remove %s after rename: %s", old_id, e)


async def sync_all_sensors(db: Database, ha_client) -> None:
    """Sync all sensors (config + all areas + input_booleans + guests). Called on startup.

    Iterates ALL areas: sync_area_sensor creates/updates sensors for active
    areas and deletes any lingering sensor of an inactive one, so a restart
    also cleans up areas that were deactivated while the hub was down.
    """
    logger.info("Syncing all HA sensors...")
    # A full sync writes everything again: it runs exactly when HA may have
    # lost the entities (start, Core restart).
    forget_written_states(ha_client)

    await sync_config_sensor(db, ha_client)

    areas = await db.fetchall(
        "SELECT id, is_active FROM areas ORDER BY sort_order, name"
    )
    for area in areas:
        await sync_area_sensor(db, ha_client, area["id"])

    # Sync input_boolean switches for all areas
    await sync_all_area_switches(db, ha_client)

    await sync_guests_sensor(db, ha_client)

    await sync_client_notification_sensors(db, ha_client)

    try:
        await remove_orphan_entities(db, ha_client)
    except Exception as e:  # noqa: BLE001 - clean-up only
        logger.warning("Orphan clean-up failed: %s", e)

    from services import quick_access

    await quick_access.sync_sensors(db, ha_client, force=True)

    active_count = sum(1 for a in areas if a.get("is_active", 1))
    logger.info(
        "HA sensor sync complete (%d area sensors, %d inactive skipped)",
        active_count,
        len(areas) - active_count,
    )


async def _sync_guests_sensor(db: Database, ha_client) -> None:
    """Unserialized body of :func:`sync_guests_sensor`."""
    try:
        from api.websocket import get_connected_client_ids

        connected_ids = get_connected_client_ids()

        guests = await db.fetchall(
            "SELECT id, expires_at FROM paired_clients WHERE access = 'guest' "
            "AND is_active = 1 AND pending_deletion_at IS NULL "
            "ORDER BY created_at DESC"
        )

        guests_data = [
            {
                "client_id": g["id"],
                "is_connected": g["id"] in connected_ids,
                "expires_at": g.get("expires_at"),
            }
            for g in guests
        ]

        payload = {
            "state": str(len(guests)),
            "attributes": {
                "friendly_name": f"{BRAND_NAME} Guests",
                "icon": "mdi:account-clock",
                "guests": guests_data,
                "updated_at": Database.now(),
                "unit_of_measurement": "guests",
            },
        }

        await _post_state(ha_client, namespace.GUESTS_SENSOR_ID, payload)
        logger.info("Synced guests sensor (%d guests)", len(guests))

    except Exception as e:
        logger.error("Failed to sync guests sensor: %s", e)


async def sync_guests_sensor(db: Database, ha_client) -> None:
    """Sync the sensor.halumar_guests sensor with current guest data.

    State = number of active guests.
    Attributes = JSON array of guests with ONLY client_id, is_connected and
    expires_at. Names, tags, device names and activity timestamps are
    deliberately NOT published: any HA token can read this sensor, and the
    admin UI gets the full picture via /integration/status instead.
    """
    async with _lock(ha_client, "guests"):
        await _sync_guests_sensor(db, ha_client)


# ── Input Boolean (area activation switches) ─────────────────────────────────


async def _sync_area_switch(db: Database, ha_client, area_id: str) -> None:
    """Unserialized body of :func:`sync_area_switch`."""
    try:
        area = await db.fetchone(
            "SELECT id, name, icon, is_active FROM areas WHERE id = ?", (area_id,)
        )
        if not area:
            return

        slug = _area_slug(area["name"], area["id"])
        entity_id = namespace.area_switch_id(slug)
        is_on = bool(area.get("is_active", 1))

        payload = {
            "state": "on" if is_on else "off",
            "attributes": {
                "friendly_name": f"{BRAND_NAME}: {area['name']}",
                "icon": area.get("icon") or "mdi:eye",
                namespace.AREA_SWITCH_ID_ATTRIBUTE: area["id"],
                "editable": True,
            },
        }

        _ws_state(ha_client).switch_written[entity_id] = payload["state"]
        await _post_state(ha_client, entity_id, payload)
        logger.debug("Synced area switch: %s = %s", entity_id, "on" if is_on else "off")

    except Exception as e:
        logger.error("Failed to sync area switch for %s: %s", area_id, e)


async def sync_area_switch(db: Database, ha_client, area_id: str) -> None:
    """Create or update the input_boolean for an area's is_active state.

    Uses the HA States API to create/update the input_boolean entity.
    This creates a "virtual" entity that shows up in HA dashboards and
    automations. A change made in HA reaches the hub as a state_changed
    event (handle_area_switch_state).
    """
    async with _lock(ha_client, f"switch:{area_id}"):
        await _sync_area_switch(db, ha_client, area_id)


async def delete_area_switch(ha_client, area_name: str, area_id: str = "") -> None:
    """Remove a deleted area's switch from Home Assistant's state machine
    (DELETE /core/api/states/<entity_id>; already gone is a no-op)."""
    try:
        slug = (
            _area_slug(area_name, area_id)
            if area_id
            else _slugify(area_name, max_len=SLUG_MAX_LEN)
        )
        entity_id = namespace.area_switch_id(slug)
        _ws_state(ha_client).switch_written.pop(entity_id, None)
        await _delete_state(ha_client, entity_id)
        logger.info("Deleted area switch: %s", entity_id)

    except Exception as e:
        logger.error("Failed to delete area switch for '%s': %s", area_name, e)


async def sync_all_area_switches(db: Database, ha_client) -> None:
    """Sync input_boolean switches for all areas."""
    areas = await db.fetchall("SELECT id FROM areas ORDER BY sort_order, name")
    for area in areas:
        await sync_area_switch(db, ha_client, area["id"])
    logger.info("Synced %d area switches", len(areas))


async def handle_area_switch_state(
    db: Database,
    ha_client,
    state: dict | None,
    *,
    entity_id: str | None = None,
    area_id: str | None = None,
) -> bool:
    """Apply a change of an area switch made in HA to the area (is_active).

    Only a state that differs from what the hub itself last wrote counts:
    the echo of the hub's own write, which can arrive after the hub already
    changed the area again, must never flip the area back (the
    5 s poller compared a cached old state with the new database value).
    Returns True when the area changed.
    """
    if not isinstance(state, dict):
        return False
    entity_id = entity_id or state.get("entity_id") or ""
    ha_state = state.get("state")
    area_id = area_id or (state.get("attributes") or {}).get(
        namespace.AREA_SWITCH_ID_ATTRIBUTE
    )
    if (
        not entity_id.startswith(namespace.AREA_SWITCH_PREFIX)
        or ha_state not in ("on", "off")
        or not isinstance(area_id, str)
    ):
        return False
    written = _ws_state(ha_client).switch_written
    if written.get(entity_id) == ha_state:
        return False

    async with _lock(ha_client, f"switch:{area_id}"):
        if written.get(entity_id) == ha_state:
            return False
        # HA holds a state the hub did not write: the next write must not be
        # skipped as "unchanged", or HA keeps it and the next poll
        # flips the area back.
        _ws_state(ha_client).posted.pop(entity_id, None)
        area = await db.fetchone(
            "SELECT id, name, is_active FROM areas WHERE id = ?", (area_id,)
        )
        if (
            not area
            or namespace.area_switch_id(_area_slug(area["name"], area["id"]))
            != entity_id
        ):
            return False
        ha_is_active = ha_state == "on"
        db_is_active = bool(area["is_active"])
        written[entity_id] = ha_state
        if ha_is_active == db_is_active:
            return False
        async with db.transaction():
            await db.execute(
                "UPDATE areas SET is_active = ?, updated_at = ? WHERE id = ?",
                (1 if ha_is_active else 0, Database.now(), area_id),
            )
    logger.info(
        "Area '%s' is_active changed via HA switch: %s -> %s",
        area["name"],
        db_is_active,
        ha_is_active,
    )

    try:
        from api import ws as api_ws

        # Area-scoped plus the data-less areas_changed, like the integration
        # switch (a device without the area learns nothing about it).
        await api_ws.announce_area_active(db, area_id, ha_is_active)
    except Exception:  # noqa: BLE001 - WS broadcast is non-critical
        pass

    # Config sensor (area list changed) and the per-area sensor (deleted on
    # deactivate, recreated on activate).
    await sync_config_sensor(db, ha_client)
    await sync_area_sensor(db, ha_client, area_id)
    return True


async def poll_area_switches(db: Database, ha_client) -> None:
    """Read every area switch from HA and apply changes made there.

    After a reconnect (changes made while the connection was down) and as
    the fallback without a live connection; normally state_changed events
    drive handle_area_switch_state.
    """
    try:
        areas = await db.fetchall(
            "SELECT id, name FROM areas ORDER BY sort_order, name"
        )
        for area in areas:
            entity_id = namespace.area_switch_id(_area_slug(area["name"], area["id"]))
            state = await ha_client.get_state(entity_id)
            if state:
                await handle_area_switch_state(
                    db, ha_client, state, entity_id=entity_id, area_id=area["id"]
                )
    except Exception as e:
        logger.error("Failed to poll area switches: %s", e)


async def _sync_client_notification_sensors(db: Database, ha_client) -> None:
    """Unserialized body of :func:`sync_client_notification_sensors`."""
    try:
        from services.notifications import (
            bounded,
            get_active_notifications_for_all_clients,
            notification_to_push_payload,
        )

        rows = await db.fetchall(
            "SELECT id, name, is_active, pending_deletion_at FROM paired_clients "
            "ORDER BY created_at DESC"
        )
        # Deleted (tombstone) and revoked devices get no sensor: their texts
        # must not come back with the next sync.
        clients = [r for r in rows if r["is_active"] and not r["pending_deletion_at"]]
        gone = [r for r in rows if r not in clients]
        all_notifications = await get_active_notifications_for_all_clients(db)

        for client in clients:
            sensor_id = client_notification_sensor_id(client["name"], client["id"])
            client_notifs = all_notifications.get(client["id"], [])

            # Full content, newest first (get_active_notifications_for_all_clients
            # orders by created_at DESC), items in the shared push shape.
            notifications = [notification_to_push_payload(n) for n in client_notifs]
            carried = await _pending_handover_items(db, ha_client, sensor_id)
            if carried:
                # The same bound as the hub's own list (keep_rank, newest first).
                notifications = bounded(notifications + carried)
                kept = {n.get("notification_id") for n in notifications}
                carried = [n for n in carried if n.get("notification_id") in kept]
            count = len(notifications)

            attributes = {
                "friendly_name": f"{BRAND_NAME}: {client['name']} Notifications",
                "icon": "mdi:bell-badge" if count else "mdi:bell-outline",
                "count": count,
                "notifications": notifications,
                "updated_at": Database.now(),
            }
            if carried:
                attributes[PENDING_HANDOVER_ATTRIBUTE] = [
                    n["notification_id"] for n in carried
                ]
            payload = {"state": str(count), "attributes": attributes}

            await _post_state(ha_client, sensor_id, payload)

        # Sensors of removed devices (with their notification texts) go.
        wanted = {client_notification_sensor_id(c["name"], c["id"]) for c in clients}
        stale = {
            entity_id
            for entity_id in _ws_state(ha_client).posted
            if entity_id.startswith(namespace.CLIENT_SENSOR_PREFIX)
            and entity_id.endswith("_notifications")
            and entity_id not in wanted
        }
        known = getattr(ha_client, "state_known", None)
        for r in gone:
            entity_id = client_notification_sensor_id(r["name"], r["id"])
            # Without the live mirror only what this process wrote is known.
            if entity_id not in wanted and callable(known) and known(entity_id):
                stale.add(entity_id)
        for entity_id in sorted(stale):
            await _delete_state(ha_client, entity_id)

        logger.info("Synced notification sensors for %d clients", len(clients))

    except Exception as e:
        logger.error("Failed to sync client notification sensors: %s", e)


async def delete_client_notification_sensor(
    ha_client, client_name: str, client_id: str
) -> None:
    """A device is removed: its notification sensor (full texts) goes."""
    if ha_client is None:
        return
    try:
        async with _lock(ha_client, "notifications"):
            await _delete_state(
                ha_client, client_notification_sensor_id(client_name, client_id)
            )
    except Exception as e:  # noqa: BLE001 - the next full sync retries
        logger.warning("Notification sensor of %s not removed: %s", client_id, e)


# Attribute of a notification sensor the integration wrote while the hub was
# unreachable: ids of the notifications in it the integration has not handed
# over yet (POST /api/integration/notifications/handover).
PENDING_HANDOVER_ATTRIBUTE = "pending_handover"
# Same bounds as the integration's queue.
HANDOVER_MAX_AGE_HOURS = 72
HANDOVER_MAX_ITEMS = 50
_HANDOVER_RESOLVED_MAX = 1000


def _recent(created_at: Any) -> bool:
    from datetime import datetime, timedelta, timezone

    try:
        ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        return False
    return datetime.now(timezone.utc) - ts < timedelta(hours=HANDOVER_MAX_AGE_HOURS)


async def _pending_handover_items(db: Database, ha_client, sensor_id: str) -> list:
    """Items of a sensor written by the integration that the hub does not
    have yet (the hub was down; the handover follows).

    The hub's own write must not drop them in the meantime: the app takes the
    sensor as the removal authority and would lose the card. Read from the
    live mirror only (no HTTP per client); without it nothing is carried.
    """
    if not getattr(ha_client, "mirror_ready", False):
        return []
    state = await ha_client.get_state(sensor_id) or {}
    attributes = state.get("attributes") or {}
    pending = attributes.get(PENDING_HANDOVER_ATTRIBUTE)
    if not isinstance(pending, list) or not pending:
        return []
    resolved = _ws_state(ha_client).handover_resolved
    wanted = {p for p in pending if isinstance(p, str) and p not in resolved}
    items = [
        n
        for n in attributes.get("notifications") or []
        if isinstance(n, dict)
        and n.get("notification_id") in wanted
        and _recent(n.get("created_at"))
    ][:HANDOVER_MAX_ITEMS]
    if not items:
        return []
    ids = [n["notification_id"] for n in items]
    marks = ",".join("?" * len(ids))
    rows = await db.fetchall(
        f"SELECT id FROM client_notifications WHERE id IN ({marks})", tuple(ids)
    )
    known = {r["id"] for r in rows}
    return [n for n in items if n["notification_id"] not in known]


def mark_handover_resolved(ha_client, notification_ids) -> None:
    """The integration handed these ids over: never carry them again."""
    resolved = _ws_state(ha_client).handover_resolved
    for nid in notification_ids:
        resolved[nid] = None
    while len(resolved) > _HANDOVER_RESOLVED_MAX:
        resolved.pop(next(iter(resolved)))


async def sync_notification_sensors_forced(db: Database, ha_client) -> None:
    """Rewrite every notification sensor, also an unchanged one.

    After a handover: the integration wrote the sensors while the hub was
    unreachable, so the hub's record of its last write is stale.
    """
    posted = _ws_state(ha_client).posted
    for entity_id in list(posted):
        if entity_id.startswith(namespace.CLIENT_SENSOR_PREFIX) and entity_id.endswith(
            "_notifications"
        ):
            posted.pop(entity_id, None)
    await sync_notification_sensors_safe(db, ha_client)


async def sync_client_notification_sensors(db: Database, ha_client) -> None:
    """Sync notification sensors for all paired clients.

    Creates sensor.halumar_client_{slug}_{uid8}_notifications per client
    (see client_notification_sensor_id). State = count of active
    (pending/delivered) notifications.

    FULL CONTENT by design: this sensor is the REMOTE delivery path. The
    hub is reachable only on the home network, so away from home the app
    reaches only Home Assistant (e.g. Home Assistant Cloud) and this sensor is its only notification
    source. Attributes are EXACTLY friendly_name, icon, count,
    notifications (newest first) and updated_at, where each notifications
    item is serialized by notification_to_push_payload — the SAME helper
    that shapes the get_notifications WS response / display_notification
    push, so the sensor and WS shapes can never drift. Any HA user/token
    can read states; notification content is world-readable within HA BY
    DESIGN and treated as not confidential (see DOCS.md).

    While the hub was unreachable the integration may have written the
    sensor itself; items it lists in ``pending_handover`` and the hub does
    not have yet are kept (with that attribute) until the handover.

    """
    async with _lock(ha_client, "notifications"):
        await _sync_client_notification_sensors(db, ha_client)


async def sync_notification_sensors_safe(db: Database, ha_client) -> None:
    """Sync notification sensors, suppressing errors.

    Called after notification mutations (create, ack, delete, clear) to keep
    the HA fallback sensors up-to-date. Errors are logged but don't propagate.
    """
    try:
        await sync_client_notification_sensors(db, ha_client)
    except Exception as e:
        logger.warning("Failed to sync notification sensors: %s", e)


# Sentinel entity: sync_config_sensor always writes it, so its absence means
# HA lost our states-API entities (Core restart/update) or the startup sync
# ran before HA was ready (host boot).
CONFIG_SENSOR_ID = namespace.CONFIG_SENSOR_ID
# Minimum gap between two full resyncs while the sentinel stays missing.
RESYNC_DEBOUNCE_SECONDS = 30.0
_last_resync_attempt: float | None = None


async def _config_sensor_present(ha_client) -> bool | None:
    """Sentinel present in HA? None if HA cannot be asked right now."""
    known = getattr(ha_client, "state_known", None)
    present = known(CONFIG_SENSOR_ID) if callable(known) else None
    if present is not None:
        return present
    ha_client.invalidate_state_cache(CONFIG_SENSOR_ID)
    try:
        return await ha_client.get_state(CONFIG_SENSOR_ID) is not None
    except Exception as e:  # noqa: BLE001 - HA down/restarting: retry later
        logger.debug("Sensor presence check failed (HA not reachable?): %s", e)
        return None


async def resync_if_missing(db: Database, ha_client, *, debounce: bool = True) -> bool:
    """Full sensor sync when sensor.halumar_config is gone from HA.

    Entities created through POST /api/states are not persisted by HA: after
    a Core restart every sensor.halumar_* and input_boolean.halumar_* is
    gone. The live connection calls this on every
    (re)connect and on ``homeassistant_started`` (``debounce=False``); the
    reconcile loop calls it as a safety net, at most every
    RESYNC_DEBOUNCE_SECONDS while the sentinel stays missing. HA unreachable
    -> nothing happens, the next trigger checks again.

    Returns True when a resync was started.
    """
    global _last_resync_attempt

    async with _lock(ha_client, "resync"):
        if await _config_sensor_present(ha_client) is not False:
            return False

        now = asyncio.get_running_loop().time()
        if (
            debounce
            and _last_resync_attempt is not None
            and now - _last_resync_attempt < RESYNC_DEBOUNCE_SECONDS
        ):
            return False
        _last_resync_attempt = now

        logger.info(
            "%s is missing in Home Assistant (HA restarted or not ready at boot); "
            "running a full sensor resync",
            CONFIG_SENSOR_ID,
        )
        await sync_all_sensors(db, ha_client)
        ha_client.invalidate_state_cache()
        return True


def _is_own_states_entity(entity_id: str) -> bool:
    return (
        entity_id in (CONFIG_SENSOR_ID, namespace.GUESTS_SENSOR_ID)
        or entity_id.startswith(namespace.AREA_SENSOR_PREFIX)
        or entity_id.startswith(namespace.AREA_SWITCH_PREFIX)
        or (
            entity_id.startswith(namespace.CLIENT_SENSOR_PREFIX)
            and entity_id.endswith("_notifications")
        )
    )


async def remove_orphan_entities(db: Database, ha_client) -> int:
    """Delete states-API entities of the hub that no longer belong to anything.

    Renamed areas and clients get new entity ids (the slug contains the
    name), deleted clients keep their notification sensor, deleted areas
    their "unavailable" switch. Runs after every full sync, only
    with the live state mirror (otherwise HA's entity list is unknown), and
    never touches an entity that is in HA's entity registry (those belong to
    the integration, not to the states API). Returns the number removed.
    """
    if not getattr(ha_client, "mirror_ready", False):
        return 0
    expected = {CONFIG_SENSOR_ID, namespace.GUESTS_SENSOR_ID}
    for area in await db.fetchall("SELECT id, name, is_active FROM areas"):
        slug = _area_slug(area["name"], area["id"])
        expected.add(namespace.area_switch_id(slug))
        if area.get("is_active", 1):
            expected.add(namespace.area_sensor_id(slug))
    for client in await db.fetchall(
        "SELECT id, name FROM paired_clients WHERE is_active = 1 "
        "AND pending_deletion_at IS NULL"
    ):
        expected.add(client_notification_sensor_id(client["name"], client["id"]))

    candidates = [
        eid
        for eid in ha_client.ws.entity_ids()
        if _is_own_states_entity(eid) and eid not in expected
    ]
    if not candidates:
        return 0
    try:
        registered = {e.get("entity_id") for e in await ha_client.get_entity_registry()}
    except Exception as e:  # noqa: BLE001 - unknown registry: delete nothing
        logger.warning("Orphan clean-up skipped (entity registry: %s)", e)
        return 0
    removed = 0
    for entity_id in candidates:
        if entity_id in registered:
            continue
        try:
            await _delete_state(ha_client, entity_id)
            removed += 1
        except Exception as e:  # noqa: BLE001 - retried with the next full sync
            logger.warning("Could not remove orphan entity %s: %s", entity_id, e)
    if removed:
        logger.info(
            "Removed %d orphaned %s entities from HA: %s",
            removed,
            BRAND_NAME,
            ", ".join(e for e in candidates if e not in registered),
        )
    return removed


# ── Live connection hooks ───────────────────────────────────────────────────


def attach_live_updates(db: Database, ha_client, ha_ws) -> None:
    """Drive the sensors from the persistent HA connection.

    - every (re)connect: full sync on the first one after start, afterwards
      only if HA lost the sensors (Core restart);
    - ``homeassistant_started``: same check (HA may have dropped states
      while it was starting);
    - ``state_changed`` of an area switch: apply a change made in HA;
    - the sentinel deleted in HA: resync.
    """
    from services import tasks

    write_state = _ws_state(ha_client)

    async def _on_connect() -> None:
        if not write_state.initial_sync_done:
            await sync_all_sensors(db, ha_client)
            write_state.initial_sync_done = True
            return
        if not await resync_if_missing(db, ha_client, debounce=False):
            # Connection was lost without an HA restart: apply switch changes
            # made in the meantime (read from the fresh mirror, no HTTP).
            await poll_area_switches(db, ha_client)

    def _spawn_connect() -> None:
        tasks.spawn(_guarded(_on_connect(), "connect sync"), name="ha-sensor-sync")

    async def _connect_listener() -> None:
        _spawn_connect()

    def _event_listener(event_type: str, data: dict) -> None:
        if event_type == "homeassistant_started":
            tasks.spawn(
                _guarded(resync_if_missing(db, ha_client, debounce=False), "resync"),
                name="ha-sensor-resync",
            )
            return
        if event_type != "state_changed":
            return
        entity_id = data.get("entity_id") or ""
        if entity_id == CONFIG_SENSOR_ID and data.get("new_state") is None:
            tasks.spawn(
                _guarded(resync_if_missing(db, ha_client, debounce=False), "resync"),
                name="ha-sensor-resync",
            )
        elif entity_id.startswith(namespace.AREA_SWITCH_PREFIX) and (
            write_state.initial_sync_done
        ):
            tasks.spawn(
                _guarded(
                    handle_area_switch_state(db, ha_client, data.get("new_state")),
                    "area switch",
                ),
                name="ha-area-switch",
            )

    ha_ws.add_connect_listener(_connect_listener)
    ha_ws.add_event_listener(_event_listener)


async def _guarded(coro, what: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - logged, next trigger retries
        logger.error("HA %s failed: %s", what, e, exc_info=True)


RECONCILE_INTERVAL_SECONDS = 30


async def reconcile_loop(
    db: Database, ha_client, interval: float = RECONCILE_INTERVAL_SECONDS
) -> None:
    """Safety net next to the events: resync missing sensors. Without a live
    connection (no Supervisor) the area switches are polled here."""
    logger.info("Sensor reconcile loop started (every %ss)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            await resync_if_missing(db, ha_client)
            if getattr(ha_client, "ws", None) is None:
                await poll_area_switches(db, ha_client)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - next cycle retries
            logger.error("Sensor reconcile error: %s", e)
