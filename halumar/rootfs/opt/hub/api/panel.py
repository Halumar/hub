"""Panel card of the admin UI.

* ``GET /api/pairing/clients/{client_id}/panel`` – last panel status, battery,
  which entities the device has in Home Assistant and the effective panel
  rules;
* ``POST /api/pairing/clients/{client_id}/panel`` – test buttons: the same
  checks and answers as ``POST /api/integration/clients/{id}/panel``, logged
  with the actor ``admin_ui``.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from api.auth import require_admin
from models.database import Database
from models.schemas import PanelCommandRequest, PanelCommandResponse
from services import panel

router = APIRouter(tags=["clients"])

NOT_FOUND = {404: {"description": "Unknown device."}}
COMMAND_ERRORS = {
    404: {"description": "not_found"},
    422: {"description": "Rejected by the hub or the app (detail.code)."},
    504: {"description": "timeout: the device did not answer within 5 s."},
}


def _db(request: Request) -> Database:
    return request.app.state.db


@router.get("/pairing/clients/{client_id}/panel", responses=NOT_FOUND)
async def get_client_panel(request: Request, client_id: str):
    """Panel card of a device (status, battery, Home Assistant entities)."""
    require_admin(request)
    view = await panel.card(_db(request), client_id)
    if view is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "Client not found"}
        )
    return view


@router.post(
    "/pairing/clients/{client_id}/panel",
    response_model=PanelCommandResponse,
    responses=COMMAND_ERRORS,
)
async def post_client_panel(
    request: Request, client_id: str, body: PanelCommandRequest
):
    """Panel command from the admin UI (test buttons)."""
    require_admin(request)
    try:
        return await panel.send_command(
            _db(request),
            client_id,
            body.action,
            **body.args(),
            actor={"kind": "admin_ui"},
        )
    except panel.PanelError as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from None
