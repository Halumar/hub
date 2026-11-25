"""Quick access levels per entity for widgets, controls and Siri. Admin-only.

* ``GET /api/quick-access``: every entity placed in an area plus every entity
  with an admin choice (``schemas/rest/quick-access.response.json``).
* ``PUT /api/quick-access/{entity_id}``: sets the level (``null`` = domain
  default). 422 ``quick_access.not_allowed`` above the domain max,
  ``quick_access.critical_ack_required`` for ``control`` of a critical entity
  without the confirmed warning. Audited, then ``client_config_updated`` to
  every device; the check sensors follow (debounced).
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from api.auth import require_admin
from services import audit_log, quick_access
from services.ha_client import is_valid_entity_id

logger = logging.getLogger("hub.api.quick_access")
router = APIRouter(tags=["areas"])


class QuickAccessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["off", "status", "control"] | None
    critical_ack: bool = False


@router.get("/quick-access")
async def list_quick_access(request: Request):
    """Quick access level of every placed or configured entity. Admin-only."""
    require_admin(request)
    items = await quick_access.admin_items(
        request.app.state.db, request.app.state.ha_client
    )
    return {"items": items}


@router.put("/quick-access/{entity_id}")
async def update_quick_access(
    request: Request, entity_id: str, body: QuickAccessUpdate
):
    """Set (or reset with ``level: null``) the level of one entity. Admin-only."""
    require_admin(request)
    if not is_valid_entity_id(entity_id):
        raise HTTPException(status_code=422, detail="Invalid entity_id")
    db = request.app.state.db
    ha_client = request.app.state.ha_client
    classes, known = await quick_access.device_classes(ha_client, {entity_id})
    try:
        quick_access.check_update(
            entity_id,
            classes.get(entity_id),
            body.level,
            body.critical_ack,
            known=known,
        )
    except quick_access.QuickAccessError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from None
    user = getattr(request.state, "user_name", None) or getattr(
        request.state, "auth_type", None
    )
    await quick_access.store_level(db, entity_id, body.level, body.critical_ack, user)
    audit_log.log_admin_action(
        "quick_access_updated",
        f"entity={entity_id} level={body.level} critical_ack={body.critical_ack}",
        request.client.host if request.client else None,
    )
    from api import websocket as api_ws

    await api_ws.broadcast({"type": "client_config_updated", "data": {}})
    items = await quick_access.admin_items(db, ha_client, only=entity_id)
    return {"items": items}
