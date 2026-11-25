"""Export/Import API endpoints. All endpoints are admin-only.

Schema v3: Sections-based layout model. Export format v2.
"""

import json
import logging
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from api import ws as api_ws
from api.areas import (
    MAX_AREAS,
    MAX_ITEMS_PER_SECTION,
    MAX_SECTIONS_PER_AREA,
    VALID_WIDGET_TYPES,
    area_room,
)
from api.auth import require_admin
from api.ws import EventAudience
from models.schemas import (
    AreaCreate,
    AreaEntityCreate,
    AreaWidgetCreate,
    ExportData,
    ImportRequest,
    SectionCreate,
)
from models.database import Database
from services import room_climate, tiles
from services.ha_sensors import sync_all_sensors
from version import VERSION

logger = logging.getLogger("hub.api.export")
router = APIRouter(tags=["export"])

# ── Import validation constants ────────────────────────────────────

# Section and widget bounds are those of the REST writers (api/areas.py,
# models/schemas.py): an export of whatever they accepted imports again.

# Maximum number of areas per import: what a hub may hold at all
MAX_IMPORT_AREAS = MAX_AREAS


def _export_entity(entity: dict) -> dict:
    """Placement row for the export; tile settings as an object."""
    out = dict(entity)
    out["tile"] = tiles.parse_stored(out.get("tile"))
    return out


@router.get("/export/areas", response_model=ExportData)
async def export_all_areas(request: Request):
    """Export all areas with sections, entities, and widgets. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db

    areas = await db.fetchall("SELECT * FROM areas ORDER BY sort_order, name")
    export_areas = []

    for area in areas:
        # Fetch sections with entities and widgets
        sections = await db.fetchall(
            "SELECT * FROM area_sections WHERE area_id = ? ORDER BY sort_order",
            (area["id"],),
        )

        export_sections = []
        for section in sections:
            entities = await db.fetchall(
                "SELECT * FROM area_entities WHERE section_id = ? ORDER BY sort_order",
                (section["id"],),
            )
            widgets_raw = await db.fetchall(
                "SELECT * FROM area_widgets WHERE section_id = ? ORDER BY sort_order",
                (section["id"],),
            )
            widgets = []
            for w in widgets_raw:
                w_dict = dict(w)
                if isinstance(w_dict.get("config"), str):
                    w_dict["config"] = json.loads(w_dict["config"])
                widgets.append(w_dict)

            export_sections.append(
                {
                    **dict(section),
                    "entities": [_export_entity(e) for e in entities],
                    "widgets": widgets,
                }
            )

        tags_rows = await db.fetchall(
            "SELECT tag FROM area_tags WHERE area_id = ?", (area["id"],)
        )
        excluded_rows = await db.fetchall(
            "SELECT entity_id FROM area_excluded_entities WHERE area_id = ?",
            (area["id"],),
        )

        export_areas.append(
            {
                **dict(area),
                "tags": [r["tag"] for r in tags_rows],
                "sections": export_sections,
                "excluded_entities": [r["entity_id"] for r in excluded_rows],
            }
        )

    return ExportData(
        format_version=2,
        version=VERSION,
        exported_at=Database.now(),
        areas=export_areas,
    )


@router.get("/export/areas/{area_id}")
async def export_area(request: Request, area_id: str):
    """Export a single area. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db

    area = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")

    # Fetch sections with entities and widgets
    sections = await db.fetchall(
        "SELECT * FROM area_sections WHERE area_id = ? ORDER BY sort_order",
        (area_id,),
    )

    export_sections = []
    for section in sections:
        entities = await db.fetchall(
            "SELECT * FROM area_entities WHERE section_id = ? ORDER BY sort_order",
            (section["id"],),
        )
        widgets_raw = await db.fetchall(
            "SELECT * FROM area_widgets WHERE section_id = ? ORDER BY sort_order",
            (section["id"],),
        )
        widgets = []
        for w in widgets_raw:
            w_dict = dict(w)
            if isinstance(w_dict.get("config"), str):
                w_dict["config"] = json.loads(w_dict["config"])
            widgets.append(w_dict)

        export_sections.append(
            {
                **dict(section),
                "entities": [_export_entity(e) for e in entities],
                "widgets": widgets,
            }
        )

    tags_rows = await db.fetchall(
        "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
    )
    excluded_rows = await db.fetchall(
        "SELECT entity_id FROM area_excluded_entities WHERE area_id = ?",
        (area_id,),
    )

    return {
        **dict(area),
        "tags": [r["tag"] for r in tags_rows],
        "sections": export_sections,
        "excluded_entities": [r["entity_id"] for r in excluded_rows],
    }


class _InvalidArea(ValueError):
    """An import entry that cannot be imported (reported, never written)."""


# Placeholder for the section_id the models require; the real id is only
# created when the section is written.
_PENDING_SECTION = "import"


def _check(model, data: dict, what: str, exclude: tuple[str, ...] = ()):
    """``data`` validated by the model of the REST endpoint (unknown keys of
    the export such as ``id`` or ``created_at`` are ignored)."""
    fields = {k: data[k] for k in model.model_fields if k in data and k not in exclude}
    if "section_id" in model.model_fields:
        fields["section_id"] = _PENDING_SECTION
    try:
        return model(**fields)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or what
        raise _InvalidArea(f"{what}: invalid '{loc}': {first['msg']}") from None


def _list_of_objects(value, what: str) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise _InvalidArea(f"{what} must be a list of objects")
    return value


def _validate_area(area_data: dict) -> tuple[AreaCreate, list[dict], list[str]]:
    """Check one import entry completely before anything is written.

    Areas, sections and entities follow the same models as ``POST /areas``,
    ``POST .../sections`` and ``POST .../entities``; the returned sections
    hold the validated values.
    """
    fields = {k: area_data[k] for k in AreaCreate.model_fields if k in area_data}
    fields.setdefault("grid_cols", 4)
    try:
        area = AreaCreate(**fields)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "area"
        raise _InvalidArea(f"invalid '{loc}': {first['msg']}") from None
    ha_area_id = area_data.get("ha_area_id")
    if ha_area_id is not None and not isinstance(ha_area_id, str):
        raise _InvalidArea("'ha_area_id' must be a string")

    sections = []
    for section in _list_of_objects(area_data.get("sections"), "'sections'"):
        checked = _check(SectionCreate, section, "section").model_dump()
        collapsed = section.get("is_collapsed", False)
        if not isinstance(collapsed, (bool, int)):
            raise _InvalidArea("section: 'is_collapsed' must be a boolean")
        checked["is_collapsed"] = 1 if collapsed else 0
        checked["entities"] = []
        for entity in _list_of_objects(section.get("entities"), "'entities'"):
            if not entity.get("entity_id"):
                continue  # nothing to place
            row = _check(AreaEntityCreate, entity, "entity", exclude=("tile",))
            checked["entities"].append(
                {
                    **row.model_dump(exclude={"section_id", "tile"}),
                    "tile": entity.get("tile"),
                }
            )
        # The type list and the config are checked when written (a widget
        # with an unknown type or a bad config is skipped, not the area).
        checked["widgets"] = []
        for widget in _list_of_objects(section.get("widgets"), "'widgets'"):
            row = _check(AreaWidgetCreate, widget, "widget", exclude=("config",))
            checked["widgets"].append(
                {
                    **row.model_dump(exclude={"section_id", "config"}),
                    "config": widget.get("config", {}),
                }
            )
        sections.append(checked)

    excluded = area_data.get("excluded_entities") or []
    if not isinstance(excluded, list) or not all(isinstance(e, str) for e in excluded):
        raise _InvalidArea("'excluded_entities' must be a list of strings")
    return area, sections, excluded


async def _write_area(
    db: Database,
    area_id: str,
    existing: bool,
    area: AreaCreate,
    area_data: dict,
    sections_list: list[dict],
    excluded: list[str],
    errors: list[str],
) -> None:
    """All rows of one imported area (caller holds the transaction)."""
    name = area.name
    now = db.now()
    row = (
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
        area_data.get("ha_area_id"),
    )
    replaced: list[str] = []
    if existing:
        # Replace the content (sections cascade to entities/widgets)
        replaced = [
            r["entity_id"]
            for r in await db.fetchall(
                "SELECT entity_id FROM area_entities WHERE area_id = ?", (area_id,)
            )
        ]
        await db.execute("DELETE FROM area_sections WHERE area_id = ?", (area_id,))
        await db.execute("DELETE FROM area_tags WHERE area_id = ?", (area_id,))
        await db.execute(
            "DELETE FROM area_excluded_entities WHERE area_id = ?", (area_id,)
        )
        await db.execute(
            """UPDATE areas SET icon=?, description=?, color=?,
               card_image_url=?, bg_light_url=?, bg_dark_url=?,
               grid_cols=?, dense_section_placement=?, sort_order=?, is_active=?,
               control_mode=?, ha_area_id=?, updated_at=? WHERE id=?""",
            (*row, now, area_id),
        )
    else:
        await db.execute(
            """INSERT INTO areas (id, name, icon, description, color,
               card_image_url, bg_light_url, bg_dark_url,
               grid_cols, dense_section_placement, sort_order, is_active,
               control_mode, ha_area_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (area_id, name, *row, now, now),
        )

    if len(sections_list) > MAX_SECTIONS_PER_AREA:
        errors.append(
            f"Area '{name}': too many sections ({len(sections_list)}, max {MAX_SECTIONS_PER_AREA}) — truncated"
        )
        sections_list = sections_list[:MAX_SECTIONS_PER_AREA]

    # If no sections provided, create a default section
    if not sections_list:
        await db.execute(
            """INSERT INTO area_sections (id, area_id, title, sort_order, column_span, columns, created_at)
               VALUES (?, ?, '', 0, 1, 2, ?)""",
            (str(uuid.uuid4()), area_id, now),
        )

    seen_entities: set[str] = set()
    for section_data in sections_list:
        section_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO area_sections (id, area_id, title, icon, sort_order, column_span, columns, is_collapsed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                section_id,
                area_id,
                section_data["title"],
                section_data["icon"],
                section_data["sort_order"],
                section_data["column_span"],
                section_data["columns"],
                section_data["is_collapsed"],
                now,
            ),
        )

        entities_list = section_data.get("entities") or []
        widgets_list = section_data.get("widgets") or []
        if len(entities_list) + len(widgets_list) > MAX_ITEMS_PER_SECTION:
            errors.append(
                f"Area '{name}' section: too many items ({len(entities_list) + len(widgets_list)}, max {MAX_ITEMS_PER_SECTION}) — truncated"
            )
            entities_list = entities_list[:MAX_ITEMS_PER_SECTION]
            widgets_list = widgets_list[: MAX_ITEMS_PER_SECTION - len(entities_list)]

        entity_rows = []
        for entity in entities_list:
            eid = entity["entity_id"]
            # Duplicates (one entity per area) are skipped
            if eid in seen_entities:
                continue
            seen_entities.add(eid)
            tile_raw, tile_error = tiles.tile_from_import(entity.get("tile"), eid)
            if tile_error:
                errors.append(f"Area '{name}': {tile_error}")
            entity_rows.append(
                (
                    str(uuid.uuid4()),
                    area_id,
                    eid,
                    section_id,
                    entity["display_name"],
                    entity["columns"],
                    entity["rows"],
                    entity["sort_order"],
                    1 if entity["is_visible"] else 0,
                    entity["control_mode"],
                    tile_raw,
                    now,
                )
            )
        if entity_rows:
            await db.executemany(
                """INSERT INTO area_entities (id, area_id, entity_id, section_id, display_name,
                   columns, rows, sort_order, is_visible, control_mode, tile, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                entity_rows,
            )

        for widget in widgets_list:
            wtype = widget["widget_type"]
            if wtype not in VALID_WIDGET_TYPES:
                errors.append(
                    f"Area '{name}': skipped widget with invalid type '{wtype}'"
                )
                continue
            if not isinstance(widget["config"], dict):
                # Only an object: the readers parse the stored text as one.
                errors.append(
                    f"Area '{name}': skipped widget '{wtype}' — invalid config type"
                )
                continue
            try:
                # Key count and size of config, as for POST .../widgets.
                _check(AreaWidgetCreate, widget, "widget")
            except _InvalidArea as e:
                errors.append(f"Area '{name}': skipped widget '{wtype}' — {e}")
                continue
            config = json.dumps(widget["config"])
            await db.execute(
                """INSERT INTO area_widgets (id, area_id, section_id, widget_type, config,
                   columns, rows, sort_order, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    area_id,
                    section_id,
                    wtype,
                    config,
                    widget["columns"],
                    widget["rows"],
                    widget["sort_order"],
                    now,
                ),
            )

    if area.tags:
        await db.executemany(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)",
            [(area_id, tag) for tag in area.tags],
        )
    if excluded:
        await db.executemany(
            "INSERT OR IGNORE INTO area_excluded_entities (area_id, entity_id, excluded_at) VALUES (?, ?, ?)",
            [(area_id, entity_id, now) for entity_id in excluded],
        )
    # A room climate choice whose entity the import did not place again
    # (forget_placement keeps it while the entity is still placed).
    for entity_id in replaced:
        await room_climate.forget_placement(db, area_id, entity_id)


async def _announce_import(
    db: Database, created: list[tuple[str, str]], replaced: list[str]
) -> None:
    """Tell the admin UI, the integration and the apps what an import changed,
    like ``POST /areas`` and the sync do: new areas as ``area_created``
    (admin), replaced areas as ``area_updated`` to the sessions that may read
    them, and ``areas_changed`` to every app (imported tags and ``is_active``
    can add an area to a list or take it out; the message has no payload).
    """
    for area_id, name in created:
        await api_ws.broadcast(
            {"type": "area_created", "data": {"area_id": area_id, "name": name}},
            audience=EventAudience.ADMIN,
        )
    for area_id in replaced:
        await api_ws.broadcast_area_scoped(
            db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
        )
    if created or replaced:
        await api_ws.notify_areas_changed(db)


@router.post("/import/areas", response_model=dict)
async def import_areas(request: Request, data: ImportRequest):
    """Import areas from exported data. Admin-only.

    Every entry is validated first (same rules as ``POST /areas``
    limits); invalid entries are reported and skipped. The import runs in ONE
    transaction, each area in its own savepoint: concurrent writes wait, a
    failing area is rolled back alone, and a database error rolls back the
    whole import (503, nothing imported, safe to retry). If no entry could be
    imported because all of them were invalid, the answer is 422.
    """
    require_admin(request)
    db: Database = request.app.state.db
    imported = 0
    skipped = 0
    failed = 0
    errors: list[str] = []
    created: list[tuple[str, str]] = []
    replaced: list[str] = []

    # Limit total areas per import
    if len(data.areas) > MAX_IMPORT_AREAS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many areas ({len(data.areas)}). Maximum is {MAX_IMPORT_AREAS}.",
        )

    try:
        async with db.transaction():
            for idx, area_data in enumerate(data.areas):
                # ImportRequest guarantees objects; their content is untrusted
                try:
                    area, sections, excluded = _validate_area(area_data)
                except _InvalidArea as e:
                    failed += 1
                    errors.append(
                        f"Area '{area_data.get('name') or f'#{idx}'}' not imported: {e}"
                    )
                    continue

                existing = await db.fetchone(
                    "SELECT id FROM areas WHERE name = ?", (area.name,)
                )
                if existing and not data.overwrite:
                    skipped += 1
                    continue
                if not existing and not await area_room(db):
                    failed += 1
                    errors.append(
                        f"Area '{area.name}' not imported: at most {MAX_AREAS} areas"
                    )
                    continue

                area_errors: list[str] = []
                area_id = existing["id"] if existing else str(uuid.uuid4())
                try:
                    async with db.transaction():  # savepoint for this area
                        await _write_area(
                            db,
                            area_id,
                            bool(existing),
                            area,
                            area_data,
                            sections,
                            excluded,
                            area_errors,
                        )
                except (
                    sqlite3.IntegrityError,
                    sqlite3.DataError,
                    sqlite3.InterfaceError,
                    sqlite3.ProgrammingError,
                ) as e:
                    # Rows of this area only; the savepoint is rolled back
                    logger.error("Import failed for area '%s': %s", area.name, e)
                    failed += 1
                    errors.append(f"Area '{area.name}' not imported: {e}")
                    continue
                errors.extend(area_errors)
                imported += 1
                if existing:
                    replaced.append(area_id)
                else:
                    created.append((area_id, area.name))
    except sqlite3.Error as e:
        logger.error("Import rolled back (database error): %s", e)
        raise HTTPException(
            status_code=503,
            detail="Import failed because of a database error; nothing was "
            "imported. Try again.",
        ) from None

    if failed and not imported and not skipped:
        raise HTTPException(
            status_code=422,
            detail="Nothing imported: " + "; ".join(errors[:5]),
        )

    await _announce_import(db, created, replaced)

    # Sync all HA sensors after import (multiple areas may have changed)
    if imported > 0:
        try:
            ha_client = request.app.state.ha_client
            await sync_all_sensors(db, ha_client)
        except Exception as e:
            logger.warning("HA sensor sync after import failed (non-critical): %s", e)

    return {
        "imported": imported,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }
