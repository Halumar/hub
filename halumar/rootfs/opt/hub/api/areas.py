"""Area management API endpoints.

Schema v3: Sections-based layout model.
"""

import json
import logging
import os
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException, Request, UploadFile, File

from api import ws as api_ws
from api.auth import require_admin
from api.ws import EventAudience
from models.database import Database
from services.ha_sensors import (
    sync_area_sensor,
    sync_config_sensor,
    delete_area_sensor,
    sync_area_switch,
    delete_area_switch,
    remove_renamed_area_entities,
)
from models.schemas import (
    AREA_NAME_MAX,
    AreaCreate,
    AreaDetailResponse,
    AreaEntityCreate,
    AreaEntityResponse,
    AreaEntityUpdate,
    AreaOrderResponse,
    AreaOrderUpdate,
    AreaResponse,
    AreaUpdate,
    AreaWidgetCreate,
    AreaWidgetResponse,
    AreaWidgetUpdate,
    SectionCreate,
    SectionLayoutUpdate,
    SectionOrderUpdate,
    SectionResponse,
    SectionUpdate,
)
from services import room_climate, tiles
from services.audit_log import log_admin_action
from services.image_store import AREA_IMAGE_COLUMNS, remove_image, replace_image
from services.layout import load_sections_with_items

logger = logging.getLogger("hub.api.areas")
router = APIRouter(tags=["areas"])

IMAGE_DIR = "/data/images"

# Allowed image file extensions
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}

# ── SQL column whitelists (prevent injection via dynamic SET clauses) ────────

# Columns allowed in UPDATE areas SET ...
AREA_UPDATE_COLUMNS = {
    "name",
    "icon",
    "description",
    "color",
    "card_image_url",
    "bg_light_url",
    "bg_dark_url",
    "grid_cols",
    "dense_section_placement",
    "sort_order",
    "is_active",
    "control_mode",
    "updated_at",
}

# Columns allowed in UPDATE area_entities SET ...
AREA_ENTITY_UPDATE_COLUMNS = {
    "section_id",
    "display_name",
    "columns",
    "rows",
    "sort_order",
    "is_visible",
    "control_mode",
    "tile",
}

# Allowed widget types
VALID_WIDGET_TYPES = {
    "clock",
    "text",
    "spacer",
}

# Columns allowed in UPDATE area_widgets SET ...
WIDGET_UPDATE_COLUMNS = {
    "section_id",
    "widget_type",
    "config",
    "columns",
    "rows",
    "sort_order",
}

# Limits
MAX_SECTIONS_PER_AREA = 20
MAX_ITEMS_PER_SECTION = 50
# Areas of a hub: create, copy, the HA sync and the import keep this, so an
# export (at most MAX_AREAS areas) always imports again.
MAX_AREAS = 100


def _get_db(request: Request) -> Database:
    return request.app.state.db


def _client_ip(request: Request) -> str | None:
    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def _entity_response(entity: dict, **extra) -> AreaEntityResponse:
    """Placement row -> response; the stored tile JSON becomes an object."""
    row = {**entity, **extra}
    if not isinstance(row.get("tile"), dict):
        row["tile"] = tiles.parse_stored(row.get("tile"))
    return AreaEntityResponse(**row)


async def _sync_sensors(
    request: Request, area_id: str, include_config: bool = False
) -> None:
    """Sync HA sensors and area switch after area/entity changes (fire-and-forget, never fails the request)."""
    try:
        db = _get_db(request)
        ha_client = request.app.state.ha_client
        await sync_area_sensor(db, ha_client, area_id)
        await sync_area_switch(db, ha_client, area_id)
        if include_config:
            await sync_config_sensor(db, ha_client)
    except Exception as e:
        logger.warning("HA sensor sync failed (non-critical): %s", e)


def _validate_image_extension(filename: str | None) -> str:
    """Validate and return the file extension if allowed, or raise HTTPException."""
    if not filename or "." not in filename:
        return "jpg"  # default fallback
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image extension: .{ext}. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}",
        )
    return ext


def _image_src(image_path: str | None) -> str | None:
    """Convert filesystem image_path to servable URL."""
    if not image_path:
        return None
    filename = os.path.basename(image_path)
    return f"/images/{filename}"


def _with_image_srcs(area_dict: dict) -> dict:
    """Add computed image_src fields to an area dict."""
    area_dict["card_image_src"] = _image_src(area_dict.get("card_image_path"))
    area_dict["bg_light_src"] = _image_src(area_dict.get("bg_light_path"))
    area_dict["bg_dark_src"] = _image_src(area_dict.get("bg_dark_path"))
    return area_dict


async def _section_items(db: Database, section_id: str) -> int:
    row = await db.fetchone(
        """SELECT (SELECT COUNT(*) FROM area_entities WHERE section_id = ?)
           + (SELECT COUNT(*) FROM area_widgets WHERE section_id = ?) as cnt""",
        (section_id, section_id),
    )
    return row["cnt"] if row else 0


async def area_room(db: Database) -> bool:
    """Whether one more area fits (MAX_AREAS)."""
    row = await db.fetchone("SELECT COUNT(*) AS n FROM areas")
    return (row["n"] if row else 0) < MAX_AREAS


async def _require_area_room(db: Database) -> None:
    if not await area_room(db):
        raise HTTPException(
            status_code=400, detail=f"Maximum number of areas ({MAX_AREAS}) reached"
        )


async def _require_room(db: Database, section_id: str, adding: int = 1) -> None:
    """400 when ``adding`` items would take ``section_id`` above
    MAX_ITEMS_PER_SECTION entities and widgets. Every writer (create, move,
    layout, sync) and the import keep this bound, so that an export always
    imports again. Only growth is refused: a section that is already over
    the bound (made by an older sync) may keep its items, be reordered and
    shrink."""
    if await _section_items(db, section_id) + adding > MAX_ITEMS_PER_SECTION:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum items per section ({MAX_ITEMS_PER_SECTION}) reached",
        )


async def _get_section_count(db: Database, area_id: str) -> int:
    """Get the number of sections in an area."""
    row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM area_sections WHERE area_id = ?", (area_id,)
    )
    return row["cnt"] if row else 0


# ── Area CRUD ────────────────────────────────────────────────────────────────


@router.get("/areas", response_model=list[AreaResponse])
async def list_areas(
    request: Request,
    active: bool | None = None,
    tag: str | None = None,
):
    """List all areas with optional filters. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    sql = "SELECT a.*, COUNT(ae.id) as entity_count FROM areas a LEFT JOIN area_entities ae ON a.id = ae.area_id"
    conditions = []
    params: list = []

    if active is not None:
        conditions.append("a.is_active = ?")
        params.append(1 if active else 0)

    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    sql += " GROUP BY a.id ORDER BY a.sort_order, a.name"

    areas = await db.fetchall(sql, tuple(params))

    # Batch-fetch all tags and section counts to avoid N+1 queries
    all_tags = await db.fetchall("SELECT area_id, tag FROM area_tags")
    tags_by_area: dict[str, list[str]] = {}
    for row in all_tags:
        tags_by_area.setdefault(row["area_id"], []).append(row["tag"])

    section_counts = await db.fetchall(
        "SELECT area_id, COUNT(*) as cnt FROM area_sections GROUP BY area_id"
    )
    count_by_area = {row["area_id"]: row["cnt"] for row in section_counts}

    result = []
    for area in areas:
        tags = tags_by_area.get(area["id"], [])

        if tag and tag not in tags:
            continue

        section_count = count_by_area.get(area["id"], 0)

        result.append(
            AreaResponse(
                **_with_image_srcs(
                    {**area, "tags": tags, "section_count": section_count}
                )
            )
        )

    return result


@router.post("/areas", response_model=AreaResponse, status_code=201)
async def create_area(request: Request, area: AreaCreate):
    """Create a new area. Admin-only."""
    require_admin(request)
    db = _get_db(request)
    area_id = str(uuid.uuid4())
    now = db.now()

    try:
        async with db.transaction():
            await _require_area_room(db)
            await db.execute(
                """INSERT INTO areas (id, name, icon, description, color, card_image_url, bg_light_url, bg_dark_url,
                   grid_cols, dense_section_placement, sort_order, is_active, control_mode, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    area_id,
                    area.name,
                    area.icon,
                    area.description,
                    area.color,
                    area.card_image_url,
                    area.bg_light_url,
                    area.bg_dark_url,
                    area.grid_cols,
                    1 if area.dense_section_placement else 0,
                    area.sort_order,
                    1 if area.is_active else 0,
                    area.control_mode,
                    now,
                    now,
                ),
            )

            if area.tags:
                await db.executemany(
                    "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
                    [(area_id, t) for t in area.tags],
                )

            # Create a default section for the area
            default_section_id = str(uuid.uuid4())
            await db.execute(
                """INSERT INTO area_sections (id, area_id, title, sort_order, column_span, columns, created_at)
                   VALUES (?, ?, '', 0, 1, 2, ?)""",
                (default_section_id, area_id, now),
            )
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed: areas.name" in str(e):
            raise HTTPException(
                status_code=409, detail=f"Area '{area.name}' already exists"
            ) from None
        raise

    logger.info("Created area '%s' (id=%s)", area.name, area_id)

    await api_ws.broadcast(
        {"type": "area_created", "data": {"area_id": area_id, "name": area.name}},
        audience=EventAudience.ADMIN,
    )
    await api_ws.notify_areas_changed(
        db, {"is_active": area.is_active, "tags": area.tags or []}
    )
    await _sync_sensors(request, area_id, include_config=True)

    return AreaResponse(
        id=area_id,
        name=area.name,
        icon=area.icon,
        description=area.description,
        color=area.color,
        card_image_url=area.card_image_url,
        bg_light_url=area.bg_light_url,
        bg_dark_url=area.bg_dark_url,
        grid_cols=area.grid_cols,
        dense_section_placement=1 if area.dense_section_placement else 0,
        sort_order=area.sort_order,
        is_active=area.is_active,
        control_mode=area.control_mode,
        tags=area.tags,
        entity_count=0,
        section_count=1,
        created_at=now,
        updated_at=now,
    )


@router.put("/areas/order", response_model=AreaOrderResponse)
async def reorder_areas(request: Request, data: AreaOrderUpdate):
    """Store the order of the areas in one step. Admin-only.

    ``area_ids`` lists every area in the new order (sort_order = position).
    Unknown or missing IDs and duplicates are rejected with 422, so a stale
    list never mixes two orders. Areas whose position changed are announced
    with ``area_updated`` like a single edit; the config sensor is synced once.
    """
    require_admin(request)
    db = _get_db(request)
    if len(set(data.area_ids)) != len(data.area_ids):
        raise HTTPException(status_code=422, detail="area_ids contains duplicates")

    async with db.transaction():
        rows = await db.fetchall("SELECT id, sort_order FROM areas")
        current = {r["id"]: r["sort_order"] for r in rows}
        if set(current) != set(data.area_ids):
            raise HTTPException(
                status_code=422,
                detail="area_ids must list every area exactly once",
            )
        changed = [
            area_id
            for idx, area_id in enumerate(data.area_ids)
            if current[area_id] != idx
        ]
        now = db.now()
        for idx, area_id in enumerate(data.area_ids):
            if current[area_id] != idx:
                await db.execute(
                    "UPDATE areas SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (idx, now, area_id),
                )
    logger.info("Reordered areas (%d moved)", len(changed))

    for area_id in changed:
        await api_ws.broadcast_area_scoped(
            db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
        )
    if changed:
        try:
            await sync_config_sensor(db, request.app.state.ha_client)
        except Exception as e:  # noqa: BLE001 - never fails the request
            logger.warning("HA config sensor sync failed (non-critical): %s", e)
    return AreaOrderResponse(moved=len(changed))


@router.get("/areas/{area_id}", response_model=AreaDetailResponse)
async def get_area(request: Request, area_id: str):
    """Get area details with sections (containing entities and widgets). Admin-only."""
    require_admin(request)
    db = _get_db(request)
    ha_client = request.app.state.ha_client

    area = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")

    # Fetch sections with entities and widgets
    sections = await load_sections_with_items(db, area_id)

    # Enrich entities with current HA state
    total_entities = 0
    section_responses = []
    for section in sections:
        entity_responses = []
        for entity in section["entities"]:
            state = None
            try:
                state_data = await ha_client.get_state(entity["entity_id"])
                if state_data:
                    state = {
                        "state": state_data.get("state"),
                        "attributes": state_data.get("attributes", {}),
                        "last_changed": state_data.get("last_changed"),
                    }
            except Exception:
                pass
            tile = await tiles.tile_output(entity.get("tile"), state, ha_client)
            entity_responses.append(_entity_response(entity, state=state, tile=tile))
            total_entities += 1

        widget_responses = [AreaWidgetResponse(**w) for w in section["widgets"]]

        section_responses.append(
            SectionResponse(
                **{
                    **{
                        k: v
                        for k, v in section.items()
                        if k not in ("entities", "widgets")
                    },
                    "entities": entity_responses,
                    "widgets": widget_responses,
                }
            )
        )

    # Fetch tags
    tags_rows = await db.fetchall(
        "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
    )
    tags = [row["tag"] for row in tags_rows]

    return AreaDetailResponse(
        **_with_image_srcs(
            {
                **area,
                "tags": tags,
                "entity_count": total_entities,
                "section_count": len(sections),
                "sections": section_responses,
            }
        )
    )


@router.put("/areas/{area_id}", response_model=AreaResponse)
async def update_area(request: Request, area_id: str, area: AreaUpdate):
    """Update an area. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    updates = {}
    for field, value in area.model_dump(exclude_unset=True).items():
        if field == "tags":
            continue
        if field not in AREA_UPDATE_COLUMNS:
            continue  # Skip unknown columns
        if field == "is_active" and value is not None:
            updates[field] = 1 if value else 0
        elif field == "dense_section_placement" and value is not None:
            updates[field] = 1 if value else 0
        elif field == "control_mode":
            updates[field] = value  # String directly, or None
        else:
            updates[field] = value

    if updates:
        updates["updated_at"] = db.now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)

    try:
        async with db.transaction():
            before = await db.fetchone(
                "SELECT id, name, is_active FROM areas WHERE id = ?", (area_id,)
            )
            if not before:
                raise HTTPException(status_code=404, detail="Area not found")
            tags_before = [
                r["tag"]
                for r in await db.fetchall(
                    "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
                )
            ]
            if updates:
                values = list(updates.values()) + [area_id]
                await db.execute(
                    f"UPDATE areas SET {set_clause} WHERE id = ?", tuple(values)
                )
            if area.tags is not None:
                await db.execute("DELETE FROM area_tags WHERE area_id = ?", (area_id,))
                if area.tags:
                    await db.executemany(
                        "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
                        [(area_id, t) for t in area.tags],
                    )
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed: areas.name" in str(e):
            raise HTTPException(
                status_code=409, detail=f"Area '{area.name}' already exists"
            ) from None
        raise
    logger.info("Updated area %s", area_id)

    # Tag changes need no client_config_updated: the client config does not
    # change. area_updated goes only to the sessions that may read the area
    # now; a change of is_active or tags can also take it out of a list, so
    # every app gets areas_changed (no payload) and reloads its list.
    await api_ws.broadcast_area_scoped(
        db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
    )
    if (
        "is_active" in updates
        and bool(updates["is_active"]) != bool(before["is_active"])
    ) or (area.tags is not None and set(area.tags) != set(tags_before)):
        await api_ws.notify_areas_changed(db)
    await _sync_sensors(request, area_id, include_config=True)
    if "name" in updates and updates["name"] != before["name"]:
        ha_client = getattr(request.app.state, "ha_client", None)
        if ha_client is not None:
            await remove_renamed_area_entities(
                ha_client, before["name"], updates["name"], area_id
            )

    # Return updated area
    updated = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
    entity_count_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM area_entities WHERE area_id = ?", (area_id,)
    )
    tags_rows = await db.fetchall(
        "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
    )
    section_count = await _get_section_count(db, area_id)

    return AreaResponse(
        **_with_image_srcs(
            {
                **updated,
                "tags": [r["tag"] for r in tags_rows],
                "entity_count": entity_count_row["cnt"],
                "section_count": section_count,
            }
        )
    )


@router.patch("/areas/{area_id}", response_model=AreaResponse)
async def patch_area(request: Request, area_id: str, area: AreaUpdate):
    """Partially update an area (same as PUT but explicit PATCH). Admin-only."""
    require_admin(request)
    return await update_area(request, area_id, area)


@router.post("/areas/{area_id}/copy", response_model=AreaResponse, status_code=201)
async def copy_area(request: Request, area_id: str):
    """Create a full copy of an area including sections, entities, widgets, and tags. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        source = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
        if not source:
            raise HTTPException(status_code=404, detail="Area not found")
        await _require_area_room(db)

        new_id = str(uuid.uuid4())
        now = db.now()

        # Generate unique name, within AREA_NAME_MAX like every other writer
        def _copy_name(label: str) -> str:
            return source["name"][: AREA_NAME_MAX - len(label)].rstrip() + label

        new_name = _copy_name(" (Copy)")
        suffix = 2
        while True:
            existing = await db.fetchone(
                "SELECT id FROM areas WHERE name = ?", (new_name,)
            )
            if not existing:
                break
            new_name = _copy_name(f" (Copy {suffix})")
            suffix += 1

        # Copy area (clear uploaded image paths and ha_area_id, keep URLs)
        await db.execute(
            """INSERT INTO areas (id, name, icon, description, color,
               card_image_path, card_image_url, bg_light_path, bg_light_url, bg_dark_path, bg_dark_url,
               grid_cols, dense_section_placement, sort_order, is_active, control_mode, ha_area_id,
               room_temperature_entity_id, room_humidity_entity_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)""",
            (
                new_id,
                new_name,
                source["icon"],
                source["description"],
                source["color"],
                source.get("card_image_url"),
                source.get("bg_light_url"),
                source.get("bg_dark_url"),
                source["grid_cols"],
                source.get("dense_section_placement", 1),
                source.get("sort_order", 0),
                source.get("is_active", 1),
                source.get("control_mode", "full"),
                source.get("room_temperature_entity_id"),
                source.get("room_humidity_entity_id"),
                now,
                now,
            ),
        )

        # Copy tags
        tags_rows = await db.fetchall(
            "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
        )
        if tags_rows:
            await db.executemany(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
                [(new_id, row["tag"]) for row in tags_rows],
            )

        # Copy sections with entities and widgets
        sections = await db.fetchall(
            "SELECT * FROM area_sections WHERE area_id = ?", (area_id,)
        )
        total_entities = 0
        for section in sections:
            new_section_id = str(uuid.uuid4())
            await db.execute(
                """INSERT INTO area_sections (id, area_id, title, icon, sort_order, column_span, columns, is_collapsed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_section_id,
                    new_id,
                    section["title"],
                    section.get("icon"),
                    section["sort_order"],
                    section["column_span"],
                    section.get("columns", 2),
                    section.get("is_collapsed", 0),
                    now,
                ),
            )

            # Copy entities in this section
            entities = await db.fetchall(
                "SELECT * FROM area_entities WHERE section_id = ?", (section["id"],)
            )
            for e in entities:
                await db.execute(
                    """INSERT INTO area_entities (id, area_id, entity_id, section_id, display_name,
                       columns, rows, sort_order, is_visible, control_mode, tile, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        new_id,
                        e["entity_id"],
                        new_section_id,
                        e["display_name"],
                        e["columns"],
                        e["rows"],
                        e["sort_order"],
                        e["is_visible"],
                        e.get("control_mode"),
                        e.get("tile"),
                        now,
                    ),
                )
                total_entities += 1

            # Copy widgets in this section
            widgets = await db.fetchall(
                "SELECT * FROM area_widgets WHERE section_id = ?", (section["id"],)
            )
            for w in widgets:
                await db.execute(
                    """INSERT INTO area_widgets (id, area_id, section_id, widget_type, config,
                       columns, rows, sort_order, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        new_id,
                        new_section_id,
                        w["widget_type"],
                        w["config"],
                        w["columns"],
                        w["rows"],
                        w["sort_order"],
                        now,
                    ),
                )
    logger.info("Copied area '%s' -> '%s' (id=%s)", source["name"], new_name, new_id)

    await api_ws.broadcast(
        {"type": "area_created", "data": {"area_id": new_id, "name": new_name}},
        audience=EventAudience.ADMIN,
    )
    await api_ws.notify_areas_changed(
        db,
        {
            "is_active": bool(source.get("is_active", 1)),
            "tags": [row["tag"] for row in tags_rows],
        },
    )
    await _sync_sensors(request, new_id, include_config=True)

    tags = [row["tag"] for row in tags_rows]
    return AreaResponse(
        **_with_image_srcs(
            {
                "id": new_id,
                "name": new_name,
                "icon": source["icon"],
                "description": source["description"],
                "color": source["color"],
                "card_image_path": None,
                "card_image_url": source.get("card_image_url"),
                "bg_light_path": None,
                "bg_light_url": source.get("bg_light_url"),
                "bg_dark_path": None,
                "bg_dark_url": source.get("bg_dark_url"),
                "grid_cols": source["grid_cols"],
                "dense_section_placement": source.get("dense_section_placement", 1),
                "sort_order": source.get("sort_order", 0),
                "is_active": bool(source.get("is_active", 1)),
                "control_mode": source.get("control_mode", "full"),
                "ha_area_id": None,
                "tags": tags,
                "entity_count": total_entities,
                "section_count": len(sections),
                "created_at": now,
                "updated_at": now,
            }
        )
    )


@router.delete("/areas", status_code=204)
async def delete_all_areas(request: Request):
    """Delete ALL areas and their uploaded images. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        # Fetch all areas to clean up images and HA sensors/switches
        areas = await db.fetchall("SELECT * FROM areas")
        if not areas:
            return  # Nothing to delete
        # Cascades to area_sections, area_entities, area_tags, widgets
        await db.execute("DELETE FROM areas")
    logger.info("Deleted all %d areas", len(areas))

    # Files only after the commit: a crash in between leaves orphans (removed
    # on the next start), never rows pointing at deleted files.
    for area in areas:
        for img_col in AREA_IMAGE_COLUMNS:
            remove_image(area.get(img_col), IMAGE_DIR)

    await api_ws.broadcast(
        {"type": "areas_deleted", "data": {"count": len(areas)}},
        audience=EventAudience.ADMIN,
    )
    await api_ws.notify_areas_changed(db)

    # Clean up HA sensors and switches for each area
    try:
        ha_client = request.app.state.ha_client
        for area in areas:
            area_name = area.get("name", "")
            aid = area.get("id", "")
            if area_name:
                try:
                    await delete_area_sensor(ha_client, area_name, aid)
                    await delete_area_switch(ha_client, area_name, aid)
                except Exception:
                    pass  # Best effort
        await sync_config_sensor(db, ha_client)
    except Exception as e:
        logger.warning("HA sensor cleanup failed (non-critical): %s", e)


@router.delete("/areas/{area_id}", status_code=204)
async def delete_area(request: Request, area_id: str):
    """Delete an area and its uploaded image. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        existing = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
        if not existing:
            raise HTTPException(status_code=404, detail="Area not found")
        deleted_tags = [
            row["tag"]
            for row in await db.fetchall(
                "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
            )
        ]
        await db.execute("DELETE FROM areas WHERE id = ?", (area_id,))
    logger.info("Deleted area %s", area_id)

    for img_col in AREA_IMAGE_COLUMNS:
        remove_image(existing.get(img_col), IMAGE_DIR)

    await api_ws.broadcast(
        {"type": "area_deleted", "data": {"area_id": area_id}},
        audience=EventAudience.ADMIN,
    )
    await api_ws.notify_areas_changed(
        db,
        {
            "id": area_id,
            "is_active": bool(existing.get("is_active")),
            "tags": deleted_tags,
        },
    )

    # Delete area sensor, area switch, and update config sensor
    try:
        ha_client = request.app.state.ha_client
        area_name = existing.get("name", "")
        if area_name:
            await delete_area_sensor(ha_client, area_name, area_id)
            await delete_area_switch(ha_client, area_name, area_id)
        await sync_config_sensor(_get_db(request), ha_client)
    except Exception as e:
        logger.warning("HA sensor cleanup failed (non-critical): %s", e)


# ── Area Image Upload ────────────────────────────────────────────────────────

# Valid image types: card_image, bg_light, bg_dark
VALID_IMAGE_TYPES = {"card_image", "bg_light", "bg_dark"}


async def _upload_area_image_impl(
    request: Request, area_id: str, image_type: str, file: UploadFile
) -> AreaResponse:
    """Upload an image for an area. Internal implementation for all image types. Admin-only."""
    require_admin(request)
    if image_type not in VALID_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid image type: {image_type}")

    db = _get_db(request)
    if not await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,)):
        raise HTTPException(status_code=404, detail="Area not found")

    # Validate content type
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image type: {file.content_type}. Allowed: {', '.join(allowed_types)}",
        )

    ext = _validate_image_extension(file.filename)

    # Read file in chunks (limit 10MB)
    max_size = 10 * 1024 * 1024
    chunk_size = 64 * 1024  # 64KB chunks
    chunks = []
    total_size = 0
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_size:
            raise HTTPException(status_code=400, detail="Image too large (max 10MB)")
        chunks.append(chunk)
    content = b"".join(chunks)

    path_col = f"{image_type}_path"
    image_filename = f"{uuid.uuid4().hex[:12]}_{image_type}.{ext}"

    async def persist(new_path: str) -> str | None:
        # Read the current path in the same transaction: with two uploads at
        # once, each one removes exactly the file it replaced.
        async with db.transaction():
            row = await db.fetchone(
                f"SELECT {path_col} FROM areas WHERE id = ?", (area_id,)
            )
            if not row:
                raise HTTPException(status_code=404, detail="Area not found")
            await db.execute(
                f"UPDATE areas SET {path_col} = ?, updated_at = ? WHERE id = ?",
                (new_path, db.now(), area_id),
            )
        return row[path_col]

    # New file first, then the path in the DB, then the old file goes.
    await replace_image(
        image_dir=IMAGE_DIR,
        filename=image_filename,
        content=content,
        old_path=None,
        persist=persist,
    )

    logger.info(
        "Uploaded %s for area %s: %s (%d bytes)",
        image_type,
        area_id,
        image_filename,
        len(content),
    )

    # Return updated area
    updated = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
    entity_count_row = await db.fetchone(
        "SELECT COUNT(*) as cnt FROM area_entities WHERE area_id = ?", (area_id,)
    )
    tags_rows = await db.fetchall(
        "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
    )
    section_count = await _get_section_count(db, area_id)

    await _sync_sensors(request, area_id, include_config=True)

    await api_ws.broadcast_area_scoped(
        db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
    )

    return AreaResponse(
        **_with_image_srcs(
            {
                **updated,
                "tags": [r["tag"] for r in tags_rows],
                "entity_count": entity_count_row["cnt"],
                "section_count": section_count,
            }
        )
    )


async def _delete_area_image_impl(request: Request, area_id: str, image_type: str):
    """Delete an uploaded image for an area. Internal implementation. Admin-only."""
    require_admin(request)
    if image_type not in VALID_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid image type: {image_type}")

    db = _get_db(request)
    path_col = f"{image_type}_path"
    async with db.transaction():
        row = await db.fetchone(
            f"SELECT {path_col} FROM areas WHERE id = ?", (area_id,)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Area not found")
        await db.execute(
            f"UPDATE areas SET {path_col} = NULL, updated_at = ? WHERE id = ?",
            (db.now(), area_id),
        )
    remove_image(row[path_col], IMAGE_DIR)

    await _sync_sensors(request, area_id, include_config=True)

    await api_ws.broadcast_area_scoped(
        db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
    )


# Individual endpoints for each image type


@router.post("/areas/{area_id}/image/card", response_model=AreaResponse)
async def upload_card_image(
    request: Request, area_id: str, file: UploadFile = File(...)
):
    """Upload a card preview image for an area."""
    return await _upload_area_image_impl(request, area_id, "card_image", file)


@router.post("/areas/{area_id}/image/bg-light", response_model=AreaResponse)
async def upload_bg_light(request: Request, area_id: str, file: UploadFile = File(...)):
    """Upload a light-mode background image for an area."""
    return await _upload_area_image_impl(request, area_id, "bg_light", file)


@router.post("/areas/{area_id}/image/bg-dark", response_model=AreaResponse)
async def upload_bg_dark(request: Request, area_id: str, file: UploadFile = File(...)):
    """Upload a dark-mode background image for an area."""
    return await _upload_area_image_impl(request, area_id, "bg_dark", file)


@router.delete("/areas/{area_id}/image/card", status_code=204)
async def delete_card_image(request: Request, area_id: str):
    """Delete the card preview image for an area."""
    await _delete_area_image_impl(request, area_id, "card_image")


@router.delete("/areas/{area_id}/image/bg-light", status_code=204)
async def delete_bg_light(request: Request, area_id: str):
    """Delete the light-mode background image for an area."""
    await _delete_area_image_impl(request, area_id, "bg_light")


@router.delete("/areas/{area_id}/image/bg-dark", status_code=204)
async def delete_bg_dark(request: Request, area_id: str):
    """Delete the dark-mode background image for an area."""
    await _delete_area_image_impl(request, area_id, "bg_dark")


# ── Area Sections ────────────────────────────────────────────────────────────


@router.post(
    "/areas/{area_id}/sections", response_model=SectionResponse, status_code=201
)
async def create_section(request: Request, area_id: str, section: SectionCreate):
    """Create a section in an area. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        area = await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,))
        if not area:
            raise HTTPException(status_code=404, detail="Area not found")

        # Enforce section limit
        section_count = await _get_section_count(db, area_id)
        if section_count >= MAX_SECTIONS_PER_AREA:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum sections per area ({MAX_SECTIONS_PER_AREA}) reached",
            )

        section_id = str(uuid.uuid4())
        now = db.now()

        await db.execute(
            """INSERT INTO area_sections (id, area_id, title, icon, sort_order, column_span, columns, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                section_id,
                area_id,
                section.title,
                section.icon,
                section.sort_order,
                section.column_span,
                section.columns,
                now,
            ),
        )

    logger.info("Created section '%s' in area %s", section.title, area_id)

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "section_created",
            "data": {"area_id": area_id, "section_id": section_id},
        },
    )
    await _sync_sensors(request, area_id)

    return SectionResponse(
        id=section_id,
        area_id=area_id,
        title=section.title,
        icon=section.icon,
        sort_order=section.sort_order,
        column_span=section.column_span,
        columns=section.columns,
        is_collapsed=0,
        created_at=now,
        entities=[],
        widgets=[],
    )


@router.get("/areas/{area_id}/sections", response_model=list[SectionResponse])
async def list_sections(request: Request, area_id: str):
    """List sections for an area with entities and widgets. Admin-only."""
    require_admin(request)
    db = _get_db(request)
    sections = await load_sections_with_items(db, area_id)
    result = []
    for section in sections:
        entity_responses = [_entity_response(e) for e in section["entities"]]
        widget_responses = [AreaWidgetResponse(**w) for w in section["widgets"]]
        result.append(
            SectionResponse(
                **{
                    **{
                        k: v
                        for k, v in section.items()
                        if k not in ("entities", "widgets")
                    },
                    "entities": entity_responses,
                    "widgets": widget_responses,
                }
            )
        )
    return result


# Registered BEFORE /sections/{section_id}: Starlette matches routes in
# order, and "order" is a valid {section_id}, so the other way round every
# reorder request ended up in update_section -> 404 "Section not found".
# tests/test_route_resolution.py guards the order.
@router.put("/areas/{area_id}/sections/order")
async def reorder_sections(request: Request, area_id: str, data: SectionOrderUpdate):
    """Reorder sections in an area. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        area = await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,))
        if not area:
            raise HTTPException(status_code=404, detail="Area not found")

        for idx, sid in enumerate(data.section_ids):
            await db.execute(
                "UPDATE area_sections SET sort_order = ? WHERE id = ? AND area_id = ?",
                (idx, sid, area_id),
            )

    await api_ws.broadcast_area_scoped(
        db, area_id, {"type": "layout_updated", "data": {"area_id": area_id}}
    )
    await _sync_sensors(request, area_id)

    return {"status": "ok"}


@router.put("/areas/{area_id}/sections/{section_id}", response_model=SectionResponse)
async def update_section(
    request: Request, area_id: str, section_id: str, data: SectionUpdate
):
    """Update a section. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    existing = await db.fetchone(
        "SELECT * FROM area_sections WHERE id = ? AND area_id = ?",
        (section_id, area_id),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Section not found")

    # Explicit column whitelist for defense-in-depth (even though Pydantic validates)
    ALLOWED_SECTION_UPDATE_COLUMNS = {
        "title",
        "icon",
        "sort_order",
        "column_span",
        "columns",
        "is_collapsed",
    }
    updates = {}
    for field, value in data.model_dump(exclude_unset=True).items():
        if field not in ALLOWED_SECTION_UPDATE_COLUMNS:
            continue
        if field == "is_collapsed" and value is not None:
            updates[field] = 1 if value else 0
        else:
            updates[field] = value

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [section_id, area_id]
        async with db.transaction():
            await db.execute(
                f"UPDATE area_sections SET {set_clause} WHERE id = ? AND area_id = ?",
                tuple(values),
            )

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "section_updated",
            "data": {"area_id": area_id, "section_id": section_id},
        },
    )
    await _sync_sensors(request, area_id)

    # Return updated section with items
    updated = await db.fetchone(
        "SELECT * FROM area_sections WHERE id = ?", (section_id,)
    )
    entities = await db.fetchall(
        "SELECT * FROM area_entities WHERE section_id = ? ORDER BY sort_order",
        (section_id,),
    )
    widgets_raw = await db.fetchall(
        "SELECT * FROM area_widgets WHERE section_id = ? ORDER BY sort_order",
        (section_id,),
    )
    widgets = []
    for w in widgets_raw:
        w_dict = dict(w)
        if isinstance(w_dict.get("config"), str):
            w_dict["config"] = json.loads(w_dict["config"])
        widgets.append(w_dict)

    return SectionResponse(
        **{
            **dict(updated),
            "entities": [_entity_response(e) for e in entities],
            "widgets": [AreaWidgetResponse(**w) for w in widgets],
        }
    )


@router.delete("/areas/{area_id}/sections/{section_id}", status_code=204)
async def delete_section(request: Request, area_id: str, section_id: str):
    """Delete a section (entities and widgets cascade). Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        existing = await db.fetchone(
            "SELECT id FROM area_sections WHERE id = ? AND area_id = ?",
            (section_id, area_id),
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Section not found")

        # Its entities go with it (cascade); like a removed entity they are
        # excluded so the next HA sync does not bring them back.
        await db.execute(
            "INSERT OR REPLACE INTO area_excluded_entities (area_id, entity_id, excluded_at) "
            "SELECT area_id, entity_id, ? FROM area_entities WHERE section_id = ?",
            (db.now(), section_id),
        )
        gone = [
            r["entity_id"]
            for r in await db.fetchall(
                "SELECT entity_id FROM area_entities WHERE section_id = ?",
                (section_id,),
            )
        ]
        await db.execute("DELETE FROM area_sections WHERE id = ?", (section_id,))
        for entity_id in gone:
            await room_climate.forget_placement(db, area_id, entity_id)

    logger.info("Deleted section %s from area %s", section_id, area_id)

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "section_deleted",
            "data": {"area_id": area_id, "section_id": section_id},
        },
    )
    await _sync_sensors(request, area_id)


# ── Area Entities ────────────────────────────────────────────────────────────


async def _require_known_entity(ha_client, entity_id: str) -> None:
    """422 if Home Assistant does not know ``entity_id``.

    Only a definite "no" rejects: while HA is not connected (restart, no
    complete live mirror) the format check of the schema is all there is,
    so adding entities keeps working and an unknown one shows as unavailable.
    """
    known = getattr(ha_client, "entity_known", None)
    if not callable(known) or await known(entity_id) is not False:
        return
    raise HTTPException(
        status_code=422,
        detail=[
            {
                "loc": ["body", "entity_id"],
                "msg": f"{entity_id} does not exist in Home Assistant",
                "type": "entity_not_found",
            }
        ],
    )


@router.post(
    "/areas/{area_id}/entities", response_model=AreaEntityResponse, status_code=201
)
async def add_entity_to_area(request: Request, area_id: str, entity: AreaEntityCreate):
    """Add an entity to an area section. Admin-only.

    Optional ``tile`` is validated like on PUT.
    """
    require_admin(request)
    db = _get_db(request)
    await _require_known_entity(request.app.state.ha_client, entity.entity_id)
    tile_raw = None
    if entity.tile is not None:
        await tiles.validate_tile(
            entity.entity_id, entity.tile, request.app.state.ha_client
        )
        tile_raw = tiles.canonical_tile(entity.tile)

    try:
        async with db.transaction():
            area = await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,))
            if not area:
                raise HTTPException(status_code=404, detail="Area not found")

            # Verify section exists and belongs to this area
            section = await db.fetchone(
                "SELECT id FROM area_sections WHERE id = ? AND area_id = ?",
                (entity.section_id, area_id),
            )
            if not section:
                raise HTTPException(status_code=404, detail="Section not found in area")

            await _require_room(db, entity.section_id)

            entity_uuid = str(uuid.uuid4())
            now = db.now()
            await db.execute(
                """INSERT INTO area_entities (id, area_id, entity_id, section_id, display_name,
                   columns, rows, sort_order, is_visible, control_mode, tile, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entity_uuid,
                    area_id,
                    entity.entity_id,
                    entity.section_id,
                    entity.display_name,
                    entity.columns,
                    entity.rows,
                    entity.sort_order,
                    1 if entity.is_visible else 0,
                    entity.control_mode,
                    tile_raw,
                    now,
                ),
            )

            # Remove from exclusion list if it was excluded before
            await db.execute(
                "DELETE FROM area_excluded_entities WHERE area_id = ? AND entity_id = ?",
                (area_id, entity.entity_id),
            )
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"Entity '{entity.entity_id}' already assigned to this area",
            ) from None
        raise

    logger.info(
        "Added entity '%s' to area %s (section %s)",
        entity.entity_id,
        area_id,
        entity.section_id,
    )

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "entity_added",
            "data": {"area_id": area_id, "entity_id": entity.entity_id},
        },
    )
    await _sync_sensors(request, area_id)
    if tile_raw:
        log_admin_action(
            "tile_updated",
            f"area={area_id} entity={entity.entity_id} fields={tiles.summary(tile_raw)}",
            _client_ip(request),
        )

    return AreaEntityResponse(
        id=entity_uuid,
        area_id=area_id,
        entity_id=entity.entity_id,
        section_id=entity.section_id,
        display_name=entity.display_name,
        columns=entity.columns,
        rows=entity.rows,
        sort_order=entity.sort_order,
        is_visible=1 if entity.is_visible else 0,
        control_mode=entity.control_mode,
        tile=tiles.parse_stored(tile_raw),
        created_at=now,
    )


@router.put("/areas/{area_id}/entities/{entity_id}", response_model=AreaEntityResponse)
async def update_area_entity(
    request: Request, area_id: str, entity_id: str, data: AreaEntityUpdate
):
    """Update an entity assignment in an area. Admin-only.

    Partial update (contracts rest/area-entity-update.request). ``tile``
    replaces the whole tile object after validation against the capabilities
    of the domain and Home Assistant; ``tile: null`` resets it.
    """
    require_admin(request)
    db = _get_db(request)

    existing = await db.fetchone(
        "SELECT * FROM area_entities WHERE area_id = ? AND entity_id = ?",
        (area_id, entity_id),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Entity not found in area")

    if "tile" in data.model_fields_set and data.tile is not None:
        await tiles.validate_tile(entity_id, data.tile, request.app.state.ha_client)

    updates = {}
    for field, value in data.model_dump(exclude_unset=True).items():
        if field not in AREA_ENTITY_UPDATE_COLUMNS:
            continue  # Skip unknown columns
        if field == "tile":
            updates[field] = tiles.canonical_tile(data.tile)
        elif field == "is_visible" and value is not None:
            updates[field] = 1 if value else 0
        elif field == "control_mode":
            updates[field] = value
        elif field == "section_id" and value is not None:
            # Verify target section exists
            section = await db.fetchone(
                "SELECT id FROM area_sections WHERE id = ? AND area_id = ?",
                (value, area_id),
            )
            if not section:
                raise HTTPException(
                    status_code=404, detail="Target section not found in area"
                )
            if value != existing["section_id"]:
                await _require_room(db, value)
            updates[field] = value
        else:
            updates[field] = value

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [area_id, entity_id]
        async with db.transaction():
            await db.execute(
                f"UPDATE area_entities SET {set_clause} WHERE area_id = ? AND entity_id = ?",
                tuple(values),
            )

    updated = await db.fetchone(
        "SELECT * FROM area_entities WHERE area_id = ? AND entity_id = ?",
        (area_id, entity_id),
    )

    changed = sorted(k for k, v in updates.items() if existing.get(k) != v)
    if "tile" in changed:
        log_admin_action(
            "tile_updated",
            f"area={area_id} entity={entity_id} "
            f"fields={','.join(tiles.changed_fields(existing.get('tile'), updates['tile'])) or '-'}",
            _client_ip(request),
        )
    message = {
        "type": "entity_updated",
        "data": {"area_id": area_id, "entity_id": entity_id, "changed": changed},
    }
    await api_ws.broadcast_area_scoped(db, area_id, message)
    await _sync_sensors(request, area_id)

    ha_client = request.app.state.ha_client
    return _entity_response(
        updated, tile=await tiles.tile_output(updated.get("tile"), None, ha_client)
    )


@router.delete("/areas/{area_id}/entities/{entity_id}", status_code=204)
async def remove_entity_from_area(request: Request, area_id: str, entity_id: str):
    """Remove an entity from an area. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        existing = await db.fetchone(
            "SELECT id FROM area_entities WHERE area_id = ? AND entity_id = ?",
            (area_id, entity_id),
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Entity not found in area")

        await db.execute(
            "DELETE FROM area_entities WHERE area_id = ? AND entity_id = ?",
            (area_id, entity_id),
        )
        # No longer the area's room climate sensor.
        await room_climate.forget_placement(db, area_id, entity_id)

        # Track as excluded so it won't be re-imported during HA sync
        await db.execute(
            "INSERT OR REPLACE INTO area_excluded_entities (area_id, entity_id, excluded_at) VALUES (?, ?, ?)",
            (area_id, entity_id, db.now()),
        )
    logger.info(
        "Removed entity '%s' from area %s (excluded from future sync)",
        entity_id,
        area_id,
    )

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "entity_removed",
            "data": {"area_id": area_id, "entity_id": entity_id},
        },
    )
    await _sync_sensors(request, area_id)


@router.put("/areas/{area_id}/layout")
async def update_layout(request: Request, area_id: str, data: SectionLayoutUpdate):
    """Batch-update layout positions for entities and widgets (drag & drop). Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        area = await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,))
        if not area:
            raise HTTPException(status_code=404, detail="Area not found")

        # An item must not move into another area's section.
        own_sections = {
            row["id"]
            for row in await db.fetchall(
                "SELECT id FROM area_sections WHERE area_id = ?", (area_id,)
            )
        }
        foreign = [
            {
                "loc": ["body", "items", index, "section_id"],
                "msg": "section does not belong to this area",
                "type": "section_not_in_area",
            }
            for index, item in enumerate(data.items)
            if item.section_id not in own_sections
        ]
        if foreign:
            raise HTTPException(status_code=422, detail=foreign)

        logger.info(
            "[layout] Updating layout for area %s: %d items", area_id, len(data.items)
        )
        before = {sid: await _section_items(db, sid) for sid in own_sections}
        for item in data.items:
            if item.entity_id:
                updates = {"section_id": item.section_id, "sort_order": item.sort_order}
                if item.columns is not None:
                    updates["columns"] = item.columns
                if item.rows is not None:
                    updates["rows"] = item.rows
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                values = list(updates.values()) + [area_id, item.entity_id]
                logger.debug(
                    "[layout]   entity=%s section=%s sort_order=%d",
                    item.entity_id,
                    item.section_id,
                    item.sort_order,
                )
                await db.execute(
                    f"UPDATE area_entities SET {set_clause} WHERE area_id = ? AND entity_id = ?",
                    tuple(values),
                )
            elif item.widget_id:
                updates = {"section_id": item.section_id, "sort_order": item.sort_order}
                if item.columns is not None:
                    updates["columns"] = item.columns
                if item.rows is not None:
                    updates["rows"] = item.rows
                set_clause = ", ".join(f"{k} = ?" for k in updates)
                values = list(updates.values()) + [area_id, item.widget_id]
                logger.debug(
                    "[layout]   widget=%s section=%s sort_order=%d",
                    item.widget_id,
                    item.section_id,
                    item.sort_order,
                )
                await db.execute(
                    f"UPDATE area_widgets SET {set_clause} WHERE area_id = ? AND id = ?",
                    tuple(values),
                )
        # A section may not grow above the bound (rolled back with the batch);
        # one that keeps its count or shrinks is fine even when over it.
        for section_id, count in before.items():
            after = await _section_items(db, section_id)
            if after > count and after > MAX_ITEMS_PER_SECTION:
                raise HTTPException(
                    status_code=400,
                    detail=f"Maximum items per section ({MAX_ITEMS_PER_SECTION}) reached",
                )
    logger.info(
        "[layout] Layout committed for area %s, broadcasting layout_updated", area_id
    )

    await api_ws.broadcast_area_scoped(
        db, area_id, {"type": "layout_updated", "data": {"area_id": area_id}}
    )
    await _sync_sensors(request, area_id)

    return {"status": "ok"}


# ── Area Widgets ─────────────────────────────────────────────────────────────


@router.get("/areas/{area_id}/widgets", response_model=list[AreaWidgetResponse])
async def list_widgets(request: Request, area_id: str):
    """List widgets for an area. Admin-only."""
    require_admin(request)
    db = _get_db(request)
    widgets = await db.fetchall(
        "SELECT * FROM area_widgets WHERE area_id = ? ORDER BY sort_order",
        (area_id,),
    )
    result = []
    for w in widgets:
        w_dict = dict(w)
        if isinstance(w_dict.get("config"), str):
            w_dict["config"] = json.loads(w_dict["config"])
        result.append(AreaWidgetResponse(**w_dict))
    return result


@router.post(
    "/areas/{area_id}/widgets", response_model=AreaWidgetResponse, status_code=201
)
async def create_widget(request: Request, area_id: str, widget: AreaWidgetCreate):
    """Create a widget in an area section. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    # Validate widget_type against whitelist
    if widget.widget_type not in VALID_WIDGET_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid widget_type '{widget.widget_type}'. "
            f"Allowed: {', '.join(sorted(VALID_WIDGET_TYPES))}",
        )

    async with db.transaction():
        area = await db.fetchone("SELECT id FROM areas WHERE id = ?", (area_id,))
        if not area:
            raise HTTPException(status_code=404, detail="Area not found")

        # Verify section exists
        section = await db.fetchone(
            "SELECT id FROM area_sections WHERE id = ? AND area_id = ?",
            (widget.section_id, area_id),
        )
        if not section:
            raise HTTPException(status_code=404, detail="Section not found in area")
        await _require_room(db, widget.section_id)

        widget_id = str(uuid.uuid4())
        now = db.now()

        await db.execute(
            """INSERT INTO area_widgets (id, area_id, section_id, widget_type, config,
               columns, rows, sort_order, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                widget_id,
                area_id,
                widget.section_id,
                widget.widget_type,
                json.dumps(widget.config),
                widget.columns,
                widget.rows,
                widget.sort_order,
                now,
            ),
        )

    logger.info(
        "Created widget '%s' in area %s (section %s)",
        widget.widget_type,
        area_id,
        widget.section_id,
    )

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "widget_created",
            "data": {"area_id": area_id, "widget_id": widget_id},
        },
    )

    return AreaWidgetResponse(
        id=widget_id,
        area_id=area_id,
        section_id=widget.section_id,
        widget_type=widget.widget_type,
        config=widget.config,
        columns=widget.columns,
        rows=widget.rows,
        sort_order=widget.sort_order,
        created_at=now,
    )


@router.put("/areas/{area_id}/widgets/{widget_id}", response_model=AreaWidgetResponse)
async def update_widget(
    request: Request, area_id: str, widget_id: str, widget: AreaWidgetUpdate
):
    """Update a widget. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    existing = await db.fetchone(
        "SELECT * FROM area_widgets WHERE id = ? AND area_id = ?",
        (widget_id, area_id),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Widget not found")

    updates = {}
    for field, value in widget.model_dump(exclude_unset=True).items():
        if field not in WIDGET_UPDATE_COLUMNS:
            continue  # Skip unknown columns
        # Validate widget_type against whitelist if being changed
        if field == "widget_type" and value is not None:
            if value not in VALID_WIDGET_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid widget_type '{value}'. "
                    f"Allowed: {', '.join(sorted(VALID_WIDGET_TYPES))}",
                )
        if field == "config" and value is not None:
            updates[field] = json.dumps(value)
        elif field == "section_id" and value is not None:
            # Verify target section exists
            section = await db.fetchone(
                "SELECT id FROM area_sections WHERE id = ? AND area_id = ?",
                (value, area_id),
            )
            if not section:
                raise HTTPException(
                    status_code=404, detail="Target section not found in area"
                )
            if value != existing["section_id"]:
                await _require_room(db, value)
            updates[field] = value
        else:
            updates[field] = value

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [widget_id, area_id]
        async with db.transaction():
            await db.execute(
                f"UPDATE area_widgets SET {set_clause} WHERE id = ? AND area_id = ?",
                tuple(values),
            )

    updated = await db.fetchone(
        "SELECT * FROM area_widgets WHERE id = ? AND area_id = ?",
        (widget_id, area_id),
    )
    u_dict = dict(updated)
    if isinstance(u_dict.get("config"), str):
        u_dict["config"] = json.loads(u_dict["config"])

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "widget_updated",
            "data": {"area_id": area_id, "widget_id": widget_id},
        },
    )

    return AreaWidgetResponse(**u_dict)


@router.delete("/areas/{area_id}/widgets/{widget_id}", status_code=204)
async def delete_widget(request: Request, area_id: str, widget_id: str):
    """Delete a widget. Admin-only."""
    require_admin(request)
    db = _get_db(request)

    async with db.transaction():
        existing = await db.fetchone(
            "SELECT id FROM area_widgets WHERE id = ? AND area_id = ?",
            (widget_id, area_id),
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Widget not found")

        await db.execute("DELETE FROM area_widgets WHERE id = ?", (widget_id,))

    logger.info("Deleted widget %s from area %s", widget_id, area_id)

    await api_ws.broadcast_area_scoped(
        db,
        area_id,
        {
            "type": "widget_deleted",
            "data": {"area_id": area_id, "widget_id": widget_id},
        },
    )
