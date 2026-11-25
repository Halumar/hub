"""HA Entity browsing API endpoints. All endpoints are admin-only."""

import logging
from fastapi import APIRouter, HTTPException, Request

from api.auth import require_admin
from services.ha_client import is_valid_entity_id

logger = logging.getLogger("hub.api.entities")
router = APIRouter(tags=["entities"])


@router.get("/ha/entities")
async def list_ha_entities(request: Request, domain: str | None = None):
    """List all Home Assistant entities, optionally filtered by domain. Admin-only."""
    require_admin(request)
    ha_client = request.app.state.ha_client

    try:
        states = await ha_client.get_states()
    except Exception as e:
        logger.error("Failed to fetch HA entities: %s", e)
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        )

    if domain:
        states = [s for s in states if s.get("entity_id", "").startswith(f"{domain}.")]

    # Return simplified entity list
    return [
        {
            "entity_id": s["entity_id"],
            "state": s.get("state"),
            "friendly_name": s.get("attributes", {}).get(
                "friendly_name", s["entity_id"]
            ),
            "domain": s["entity_id"].split(".")[0],
            "device_class": s.get("attributes", {}).get("device_class"),
            "icon": s.get("attributes", {}).get("icon"),
            "unit_of_measurement": s.get("attributes", {}).get("unit_of_measurement"),
        }
        for s in states
    ]


@router.get("/ha/entities/{entity_id:path}")
async def get_ha_entity(request: Request, entity_id: str):
    """Get a specific HA entity's current state. Admin-only."""
    require_admin(request)
    # The {entity_id:path} converter allows "/" and "..", so validate before it
    # reaches ha_client.get_state (which builds ".../states/{entity_id}").
    if not is_valid_entity_id(entity_id):
        raise HTTPException(status_code=400, detail="Invalid entity_id")
    ha_client = request.app.state.ha_client

    try:
        state = await ha_client.get_state(entity_id)
    except Exception as e:
        logger.error("Failed to fetch entity %s: %s", entity_id, e)
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        )

    if not state:
        raise HTTPException(status_code=404, detail="Entity not found")

    return state


@router.get("/ha/areas")
async def list_ha_areas(request: Request):
    """List all Home Assistant areas. Admin-only."""
    require_admin(request)
    ha_client = request.app.state.ha_client

    try:
        areas = await ha_client.get_areas()
    except Exception as e:
        logger.error("Failed to fetch HA areas: %s", e)
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        )

    return areas


@router.get("/ha/devices")
async def list_ha_devices(request: Request):
    """List all Home Assistant devices. Admin-only."""
    require_admin(request)
    ha_client = request.app.state.ha_client

    try:
        devices = await ha_client.get_devices()
    except Exception as e:
        logger.error("Failed to fetch HA devices: %s", e)
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        )

    return devices


@router.get("/ha/domains")
async def list_ha_domains(request: Request):
    """List all entity domains with counts. Admin-only."""
    require_admin(request)
    ha_client = request.app.state.ha_client

    try:
        states = await ha_client.get_states()
    except Exception:
        raise HTTPException(
            status_code=502, detail="Failed to connect to Home Assistant"
        )

    domains: dict[str, int] = {}
    for s in states:
        domain = s["entity_id"].split(".")[0]
        domains[domain] = domains.get(domain, 0) + 1

    return [{"domain": d, "count": c} for d, c in sorted(domains.items())]
