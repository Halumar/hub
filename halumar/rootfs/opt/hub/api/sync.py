"""HA Area sync API endpoints. All endpoints are admin-only."""

import logging
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException, Request

from api import ws as api_ws
from api.auth import require_admin
from api.ws import EventAudience
from api.areas import MAX_AREAS, MAX_ITEMS_PER_SECTION, MAX_SECTIONS_PER_AREA, area_room
from models.schemas import (
    AREA_NAME_MAX,
    ICON_MAX,
    SyncAreaEntitiesRequest,
    SyncFromHARequest,
    SyncResult,
    clip_area_display_name,
)
from services.ha_sensors import sync_all_sensors

logger = logging.getLogger("hub.api.sync")
router = APIRouter(tags=["sync"])


async def _fetch_registry(ha_client) -> tuple[list[dict], list[dict]]:
    """Entity and device registry from HA (empty lists if HA fails)."""
    try:
        entities = await ha_client.get_entity_registry()
        devices = await ha_client.get_devices()
    except Exception as e:
        logger.warning("Failed to fetch the HA registries: %s", e, exc_info=True)
        return [], []
    logger.debug(
        "Entity import: fetched %d entities, %d devices from HA",
        len(entities) if entities else 0,
        len(devices) if devices else 0,
    )
    return entities or [], devices or []


def _entities_for_area(
    entities: list[dict], devices: list[dict], ha_area_id: str
) -> list[dict]:
    """Entities assigned to an HA area, directly or via their device.

    Returns a list of entity dicts with entity_id, name, and domain.
    """
    device_area_map = {
        d.get("id"): d.get("area_id")
        for d in devices
        if d.get("id") and d.get("area_id")
    }
    area_entities = []
    for entity in entities:
        entity_id = entity.get("entity_id", "")
        device_id = entity.get("device_id")
        in_area = entity.get("area_id") == ha_area_id or (
            device_id and device_area_map.get(device_id) == ha_area_id
        )
        if not in_area or entity.get("disabled_by") or entity.get("hidden_by"):
            continue
        area_entities.append(
            {
                "entity_id": entity_id,
                "name": entity.get("name") or entity.get("original_name") or "",
                "domain": entity_id.split(".")[0] if "." in entity_id else "",
            }
        )
    logger.debug(
        "Entity import: found %d entities for area %s", len(area_entities), ha_area_id
    )
    return area_entities


async def _add_entities(
    db,
    hub_area_id: str,
    area_name: str,
    area_entities: list[dict],
    allowed_domains: set[str] | None,
    next_order: int | None,
    errors: list[str] | None = None,
) -> int:
    """Insert new, not excluded entities (caller holds the transaction).

    ``next_order`` None: sort_order = position in ``area_entities``; else
    appended from ``next_order`` on. A section takes MAX_ITEMS_PER_SECTION
    items like everywhere else: when it is full the next untitled section is
    used (created up to MAX_SECTIONS_PER_AREA); what does not fit is
    reported in ``errors``. Returns the number of added entities.
    """
    room = _SectionRoom(db, hub_area_id)
    added = 0
    left_out = 0
    for idx, entity in enumerate(area_entities):
        entity_id = entity["entity_id"]
        if allowed_domains and entity.get("domain", "") not in allowed_domains:
            continue
        if await db.fetchone(
            "SELECT id FROM area_entities WHERE area_id = ? AND entity_id = ?",
            (hub_area_id, entity_id),
        ):
            continue
        if await db.fetchone(
            "SELECT 1 FROM area_excluded_entities WHERE area_id = ? AND entity_id = ?",
            (hub_area_id, entity_id),
        ):
            logger.debug(
                "Entity %s excluded from area %s, skipping", entity_id, area_name
            )
            continue
        section_id = await room.next()
        if section_id is None:
            left_out += 1
            continue
        try:
            await db.execute(
                """INSERT INTO area_entities
                   (id, area_id, entity_id, section_id, display_name, columns, rows, sort_order, is_visible, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, 1, ?, 1, ?)""",
                (
                    str(uuid.uuid4()),
                    hub_area_id,
                    entity_id,
                    section_id,
                    # Same bounds as a display name set by the admin.
                    clip_area_display_name(entity["name"]),
                    idx if next_order is None else next_order + added,
                    db.now(),
                ),
            )
        except sqlite3.IntegrityError as e:
            logger.warning("Failed to add entity %s: %s", entity_id, e)
            continue
        room.used(section_id)
        added += 1
        logger.debug("Added entity %s to area %s", entity_id, area_name)
    if left_out and errors is not None:
        errors.append(
            f"Area '{area_name}': {left_out} entities not added "
            f"(at most {MAX_SECTIONS_PER_AREA} sections of {MAX_ITEMS_PER_SECTION})"
        )
    return added


class _SectionRoom:
    """Sections of an area with free places, in order; new ones on demand."""

    def __init__(self, db, area_id: str) -> None:
        self.db = db
        self.area_id = area_id
        self.counts: dict[str, int] | None = None

    async def _load(self) -> dict[str, int]:
        rows = await self.db.fetchall(
            """SELECT s.id,
                      (SELECT COUNT(*) FROM area_entities e WHERE e.section_id = s.id)
                    + (SELECT COUNT(*) FROM area_widgets w WHERE w.section_id = s.id) AS n
               FROM area_sections s WHERE s.area_id = ? ORDER BY s.sort_order""",
            (self.area_id,),
        )
        return {r["id"]: r["n"] for r in rows}

    async def next(self) -> str | None:
        if self.counts is None:
            self.counts = await self._load()
        for section_id, n in self.counts.items():
            if n < MAX_ITEMS_PER_SECTION:
                return section_id
        if len(self.counts) >= MAX_SECTIONS_PER_AREA:
            return None
        section_id = await _new_section(self.db, self.area_id, len(self.counts))
        self.counts[section_id] = 0
        return section_id

    def used(self, section_id: str) -> None:
        self.counts[section_id] += 1


async def _new_section(db, area_id: str, sort_order: int) -> str:
    """Untitled like the section of ``POST /areas``: the apps show a section
    title as a heading, and "Default" is no heading."""
    section_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO area_sections (id, area_id, title, sort_order, column_span, columns, created_at)
           VALUES (?, ?, '', ?, 1, 2, ?)""",
        (section_id, area_id, sort_order, db.now()),
    )
    return section_id


async def _announce_sync(
    db, created: list[tuple[str, str]], changed: list[str]
) -> None:
    """Tell the admin UI, the integration and the apps what a sync changed,
    like the single-area endpoints do: created areas as
    ``area_created`` (admin, integration) plus one ``areas_changed`` for the
    apps whose list can contain them; changed areas (name, icon, imported
    entities) as ``area_updated`` to the sessions that may read them.
    """
    for area_id, name in created:
        await api_ws.broadcast(
            {"type": "area_created", "data": {"area_id": area_id, "name": name}},
            audience=EventAudience.ADMIN,
        )
    if created:
        # Synced areas are active and untagged.
        await api_ws.notify_areas_changed(db, {"is_active": True, "tags": []})
    for area_id in changed:
        await api_ws.broadcast_area_scoped(
            db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
        )


@router.post("/sync/from-ha", response_model=SyncResult)
async def sync_from_ha(request: Request, body: SyncFromHARequest | None = None):
    """Import HA Areas as Halumar areas. Admin-only.

    Options:
    - area_ids: List of HA area IDs to import (null = all areas)
    - include_entities: If true, also import entities assigned to each area
    - domains: List of domains to import (null = all domains)
    """
    # A new HA area becomes a Halumar area with the HA name and icon. An area
    # imported before keeps the name and icon the admin gave it (the sync
    # only fills a missing icon and adds new entities); ``updated`` counts
    # the areas where that changed something. (Comment, not docstring: the
    # docstring is the description in the contract's OpenAPI snapshot.)
    require_admin(request)
    db = request.app.state.db
    ha_client = request.app.state.ha_client
    result = SyncResult()

    include_entities = body.include_entities if body else False
    allowed_domains = set(body.domains) if body and body.domains else None

    try:
        areas = await ha_client.get_areas()
    except Exception as e:
        logger.error("Failed to fetch HA areas: %s", e)
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        )

    # Filter by area_ids if provided
    filter_ids = body.area_ids if body and body.area_ids else None
    if filter_ids is not None:
        filter_set = set(filter_ids)
        areas = [a for a in areas if a.get("area_id") in filter_set]

    # Everything from HA first; the transaction must not wait for HA.
    entities, devices = (
        await _fetch_registry(ha_client) if include_entities else ([], [])
    )

    created: list[tuple[str, str]] = []
    changed: list[str] = []
    async with db.transaction():
        for area in areas:
            ha_area_id = area.get("area_id")
            # Same bounds as POST /areas, so the area exports and imports again.
            name = str(area.get("name") or "").strip()[:AREA_NAME_MAX].strip()
            icon = area.get("icon")
            if not isinstance(icon, str) or not icon or len(icon) > ICON_MAX:
                icon = None
            if not name:
                result.errors.append(f"HA area '{ha_area_id}' has no name, skipped")
                continue
            existing = await db.fetchone(
                "SELECT id, name, icon FROM areas WHERE ha_area_id = ?", (ha_area_id,)
            )
            icon_filled = False
            try:
                if existing:
                    # Name and icon belong to the admin once the area exists:
                    # the sync only fills an icon the area does not have.
                    hub_area_id = existing["id"]
                    if icon and not existing.get("icon"):
                        icon_filled = True
                        await db.execute(
                            "UPDATE areas SET icon = ?, updated_at = ? WHERE id = ?",
                            (icon, db.now(), hub_area_id),
                        )
                else:
                    if not await area_room(db):
                        result.errors.append(
                            f"Failed to import area '{name}': at most {MAX_AREAS} areas"
                        )
                        continue
                    hub_area_id = str(uuid.uuid4())
                    now = db.now()
                    await db.execute(
                        """INSERT INTO areas (id, name, icon, ha_area_id, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (hub_area_id, name, icon, ha_area_id, now, now),
                    )
            except sqlite3.IntegrityError as e:
                # e.g. a Halumar area with the same name already exists
                result.errors.append(f"Failed to import area '{name}': {e}")
                continue
            if not existing:
                result.created += 1
                created.append((hub_area_id, name))
            result.synced += 1

            added = 0
            if include_entities:
                area_entities = _entities_for_area(entities, devices, ha_area_id)
                logger.info(
                    "Importing %d entities for area %s (ha_area_id=%s)",
                    len(area_entities),
                    name,
                    ha_area_id,
                )
                added = await _add_entities(
                    db,
                    hub_area_id,
                    name,
                    area_entities,
                    allowed_domains,
                    None,
                    result.errors,
                )
                result.entities_added += added
            if existing and (added or icon_filled):
                result.updated += 1
                changed.append(hub_area_id)
    logger.info(
        "Synced from HA: %d areas (%d created, %d updated, %d entities added)",
        result.synced,
        result.created,
        result.updated,
        result.entities_added,
    )
    await _announce_sync(db, created, changed)

    # Sync all HA sensors after import (multiple areas may have changed)
    try:
        await sync_all_sensors(db, ha_client)
    except Exception as e:
        logger.warning("HA sensor sync after import failed (non-critical): %s", e)

    return result


@router.post("/sync/area/{area_id}/entities", response_model=SyncResult)
async def sync_area_entities_from_ha(
    request: Request, area_id: str, body: SyncAreaEntitiesRequest | None = None
):
    """Sync entities for a single area from HA. Admin-only.

    Imports new entities from HA that are assigned to this area's ha_area_id.
    Respects excluded entities (entities the user previously removed).
    Does NOT remove entities that are no longer in HA.

    Options:
    - domains: List of domains to import (null = all domains)
    """
    require_admin(request)
    db = request.app.state.db
    ha_client = request.app.state.ha_client
    result = SyncResult()

    allowed_domains = set(body.domains) if body and body.domains else None

    # Get area with ha_area_id
    area = await db.fetchone(
        "SELECT id, name, ha_area_id FROM areas WHERE id = ?", (area_id,)
    )
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")

    ha_area_id = area.get("ha_area_id")
    if not ha_area_id:
        raise HTTPException(
            status_code=400,
            detail="Area is not linked to a Home Assistant area. Import it from HA first.",
        )

    name = area.get("name", "")
    hub_area_id = area["id"]

    logger.info("Syncing entities for area %s (ha_area_id=%s)", name, ha_area_id)
    entities, devices = await _fetch_registry(ha_client)
    area_entities = _entities_for_area(entities, devices, ha_area_id)
    logger.info("Found %d entities in HA for area %s", len(area_entities), name)

    async with db.transaction():
        if not await db.fetchone("SELECT id FROM areas WHERE id = ?", (hub_area_id,)):
            raise HTTPException(status_code=404, detail="Area not found")
        max_order = await db.fetchone(
            "SELECT MAX(sort_order) as max_order FROM area_entities WHERE area_id = ?",
            (hub_area_id,),
        )
        next_order = (max_order["max_order"] or 0) + 1 if max_order else 0
        result.entities_added = await _add_entities(
            db,
            hub_area_id,
            name,
            area_entities,
            allowed_domains,
            next_order,
            result.errors,
        )
    result.synced = 1
    if result.entities_added:
        await _announce_sync(db, [], [hub_area_id])

    logger.info(
        "Synced entities for area %s: %d new entities added",
        name,
        result.entities_added,
    )

    # Sync sensors for this area
    try:
        from services.ha_sensors import sync_area_sensor

        await sync_area_sensor(db, ha_client, hub_area_id)
    except Exception as e:
        logger.warning("HA sensor sync after entity import failed: %s", e)

    return result
