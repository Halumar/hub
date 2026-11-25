"""Tile settings helpers for the Admin UI. Admin-only.

* ``GET /api/tiles/capabilities``: the contract data (capabilities per domain
  and the curated icon set with the SVG path of every MDI icon), so the Admin
  UI uses the same table as the hub's validation.
* ``GET /api/areas/{area_id}/entities/{entity_id}/tile/suggestions``: sensors
  for the extra line (same HA device first) and run targets with the devices
  that see this tile but not the target itself (the admin
  decides, the Admin UI only points it out).
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from api import authz
from api.auth import require_admin
from data.mdi_paths import get_mdi_path
from services import tiles

logger = logging.getLogger("hub.api.tiles")
router = APIRouter(tags=["areas"])

# Upper bound per list in the suggestions answer.
MAX_SUGGESTIONS = 500


@router.get("/tiles/capabilities")
async def tile_capabilities(request: Request):
    """Allowed tile settings per domain and the curated icon set. Admin-only."""
    require_admin(request)
    icon_data = tiles.icon_set()
    return {
        "capabilities": tiles.capabilities(),
        "icons": {
            **icon_data,
            "icons": [
                {**icon, "path": get_mdi_path(icon["mdi"])}
                for icon in icon_data["icons"]
            ],
        },
    }


def _name(state: dict) -> str:
    return (state.get("attributes") or {}).get("friendly_name") or state["entity_id"]


async def _device_scopes(db) -> list[dict]:
    """Active paired devices with the set of areas in their list."""
    rows = await db.fetchall(
        "SELECT id, name, (access = 'guest') AS is_guest, owner_label FROM paired_clients "
        "WHERE is_active = 1 AND pending_deletion_at IS NULL ORDER BY name"
    )
    out = []
    for row in rows:
        out.append(
            {
                "client_id": row["id"],
                "name": row["name"],
                "is_guest": bool(row.get("is_guest")),
                "owner_label": row.get("owner_label"),
                "areas": await authz.listed_area_ids(db, row["id"], ignore_access=True),
            }
        )
    return out


@router.get("/areas/{area_id}/entities/{entity_id}/tile/suggestions")
async def tile_suggestions(request: Request, area_id: str, entity_id: str):
    """Sensors for ``secondary`` and run targets with their visibility. Admin-only."""
    require_admin(request)
    db = request.app.state.db
    ha_client = request.app.state.ha_client

    placement = await db.fetchone(
        "SELECT id FROM area_entities WHERE area_id = ? AND entity_id = ?",
        (area_id, entity_id),
    )
    if not placement:
        raise HTTPException(status_code=404, detail="Entity not found in area")

    try:
        states = await ha_client.get_states()
    except Exception as exc:  # noqa: BLE001
        logger.error("tile suggestions: Home Assistant not reachable: %s", exc)
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        ) from None

    device_of: dict[str, str] = {}
    try:
        for entry in await ha_client.get_entity_registry():
            if entry.get("device_id"):
                device_of[entry["entity_id"]] = entry["device_id"]
    except Exception as exc:  # noqa: BLE001 - suggestions work without devices
        logger.info("tile suggestions without device grouping: %s", exc)
    own_device = device_of.get(entity_id)

    caps = tiles.capabilities()
    sensor_domains = set(caps["secondary_entity_domains"])
    run_domains = {t["domain"] for t in caps["run_targets"]}

    sensors = []
    targets = []
    for state in states:
        eid = state.get("entity_id", "")
        domain = tiles.domain_of(eid)
        attrs = state.get("attributes") or {}
        if domain in sensor_domains:
            sensors.append(
                {
                    "entity_id": eid,
                    "name": _name(state),
                    "state": state.get("state"),
                    "unit": attrs.get("unit_of_measurement"),
                    "device_class": attrs.get("device_class"),
                    "same_device": bool(own_device)
                    and device_of.get(eid) == own_device,
                }
            )
        elif domain in run_domains:
            targets.append({"entity_id": eid, "name": _name(state), "domain": domain})
    sensors.sort(key=lambda s: (not s["same_device"], s["name"].lower()))
    targets.sort(key=lambda t: t["name"].lower())
    sensors = sensors[:MAX_SUGGESTIONS]
    targets = targets[:MAX_SUGGESTIONS]

    # Where do the targets sit (visible placements in active areas)?
    placed = await db.fetchall(
        "SELECT ae.entity_id, a.id AS area_id, a.name AS area_name "
        "FROM area_entities ae JOIN areas a ON a.id = ae.area_id "
        "WHERE ae.is_visible = 1 AND a.is_active = 1"
    )
    areas_of: dict[str, list[dict]] = {}
    for row in placed:
        areas_of.setdefault(row["entity_id"], []).append(
            {"area_id": row["area_id"], "name": row["area_name"]}
        )

    devices = await _device_scopes(db)
    viewers = [d for d in devices if area_id in d["areas"]]
    for target in targets:
        target_areas = areas_of.get(target["entity_id"], [])
        ids = {a["area_id"] for a in target_areas}
        target["areas"] = target_areas
        target["not_visible_for"] = [
            {
                "client_id": d["client_id"],
                "name": d["name"],
                "is_guest": d["is_guest"],
                "owner_label": d["owner_label"],
            }
            for d in viewers
            if not (ids & d["areas"])
        ]

    return {
        "sensors": sensors,
        "run_targets": targets,
        "tile_viewers": [
            {"client_id": d["client_id"], "name": d["name"], "is_guest": d["is_guest"]}
            for d in viewers
        ],
    }
