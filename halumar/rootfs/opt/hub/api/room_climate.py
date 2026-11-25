"""Room climate sensors of an area (area page, card "Room climate").

* ``GET/PUT /api/areas/{area_id}/room-climate`` – admin only, contract
  ``rest/room-climate.request.json`` / ``room-climate.response.json``. GET
  answers per measure the admin's choice, what the apps get
  (``room_climate``), the value they show now and every placed entity that
  reports the measure with why it does not count automatically. PUT
  replaces the choice (null = automatic); any other entity than an option is
  refused with 422 ``room_climate_invalid``. Connected apps re-fetch the area
  (``area_updated``).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api import ws as api_ws
from api.auth import require_admin
from models.database import Database
from services import room_climate
from services.audit_log import log_admin_action

router = APIRouter(tags=["areas"])

ENTITY_ID = r"^[a-z0-9_]+\.[a-z0-9_]+$"


class RoomClimateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature_entity_id: str | None = Field(pattern=ENTITY_ID, max_length=255)
    humidity_entity_id: str | None = Field(pattern=ENTITY_ID, max_length=255)


def _db(request: Request) -> Database:
    return request.app.state.db


async def _area(db: Database, area_id: str) -> dict:
    area = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")
    return dict(area)


@router.get("/areas/{area_id}/room-climate")
async def get_room_climate(request: Request, area_id: str):
    """Room climate sensors of the area: choice, effective pick, value, options."""
    require_admin(request)
    db = _db(request)
    return await room_climate.settings(
        db, request.app.state.ha_client, await _area(db, area_id)
    )


@router.put("/areas/{area_id}/room-climate")
async def put_room_climate(request: Request, area_id: str, body: RoomClimateBody):
    """Pick the room climate sensors (null = automatic); apps follow at once."""
    require_admin(request)
    db = _db(request)
    ha_client = request.app.state.ha_client
    area = await _area(db, area_id)
    current = await room_climate.settings(db, ha_client, area)
    wanted = {
        "temperature": body.temperature_entity_id,
        "humidity": body.humidity_entity_id,
    }
    for name, entity_id in wanted.items():
        if entity_id is None:
            continue
        if entity_id not in {o["entity_id"] for o in current[name]["options"]}:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "room_climate_invalid",
                    "measure": name,
                    "entity_id": entity_id,
                    "message": f"{entity_id} is not placed in this area or reports no {name}",
                },
            )
    if wanted != room_climate.admin_choice(area):
        async with db.transaction():
            await db.execute(
                "UPDATE areas SET room_temperature_entity_id = ?, "
                "room_humidity_entity_id = ?, updated_at = ? WHERE id = ?",
                (wanted["temperature"], wanted["humidity"], db.now(), area_id),
            )
        ip = request.client.host if request.client else None
        log_admin_action(
            "area_room_climate",
            f"{area_id} temperature={wanted['temperature'] or 'auto'} "
            f"humidity={wanted['humidity'] or 'auto'}",
            ip,
        )
        await api_ws.broadcast_area_scoped(
            db, area_id, {"type": "area_updated", "data": {"area_id": area_id}}
        )
        area = await _area(db, area_id)
    return await room_climate.settings(db, ha_client, area)
