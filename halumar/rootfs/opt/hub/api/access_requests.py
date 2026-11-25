"""Early access requests in the admin UI (contract docs/access.md).

* ``GET /api/access-requests`` – open requests and the ones of the last 7 days,
  running early accesses (``rest/access-requests.response.json``);
* ``POST /api/access-requests/{request_id}/answer`` – the admin answers like a
  recipient device (first answer wins);
* ``DELETE /api/pairing/clients/{client_id}/early-access`` – end a running
  early access before its end.

Admin only. Errors ``{"detail": {"code", "message"}}``: 404
``request_not_found``, 409 ``already_answered`` (with ``request``),
``duration_not_allowed``, 422 ``invalid_request``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from api.auth import require_admin
from models.database import Database
from services import access_requests as svc
from services.audit_log import log_admin_action

router = APIRouter(tags=["clients"])

NOT_FOUND = {404: {"description": "request_not_found"}}
CONFLICT = {409: {"description": "already_answered, duration_not_allowed"}}


class AccessRequestAnswer(BaseModel):
    """contracts rest/access-request-answer.request.json"""

    model_config = ConfigDict(extra="forbid")
    answer: Literal["yes", "no"]
    duration: Literal["until_window_start", "1h", "3h", "today"] | None = None
    message: Literal["not_now", "later", "call_me"] | None = None


class EarlyEnded(BaseModel):
    """Answer of DELETE …/early-access: whether an early access was running."""

    ended: bool


def _db(request: Request) -> Database:
    return request.app.state.db


def _actor(request: Request) -> str:
    return getattr(request.state, "actor", None) or "admin"


def _error(e: svc.AccessRequestError) -> HTTPException:
    status = {"request_not_found": 404, "invalid_request": 422}.get(e.reason, 409)
    detail: dict = {"code": e.reason, "message": svc.MESSAGES.get(e.reason, e.reason)}
    if e.request is not None:
        detail["request"] = e.request
    return HTTPException(status_code=status, detail=detail)


@router.get("/access-requests")
async def get_access_requests(request: Request):
    """Early access requests (open and last 7 days) and running early accesses."""
    require_admin(request)
    return await svc.admin_overview(_db(request))


@router.post(
    "/access-requests/{request_id}/answer", responses={**NOT_FOUND, **CONFLICT}
)
async def answer_access_request(
    request: Request, request_id: str, body: AccessRequestAnswer
):
    """The admin answers an early access request (first answer wins)."""
    require_admin(request)
    if body.answer == "yes" and body.duration is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "duration", "message": "yes needs a duration"},
        )
    db = _db(request)
    try:
        row = await svc.answer(
            db,
            getattr(request.app.state, "ha_client", None),
            request_id,
            body.answer,
            body.duration,
            body.message,
            recipient=None,
            admin_name=_actor(request),
            via="admin_ui",
        )
    except svc.AccessRequestError as e:
        raise _error(e) from e
    log_admin_action(
        "access_request_answered", f"request_id={request_id} answer={body.answer}", None
    )
    return await svc.view(db, row, can_answer=False)


@router.delete(
    "/pairing/clients/{client_id}/early-access",
    responses=NOT_FOUND,
    response_model=EarlyEnded,
)
async def end_early_access(request: Request, client_id: str):
    """End a running early access of a device now (the device closes again
    when its access hours do not allow it)."""
    require_admin(request)
    db = _db(request)
    try:
        ended = await svc.end_early(db, client_id, _actor(request))
    except svc.AccessRequestError as e:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "Client not found"},
        ) from e
    if ended:
        log_admin_action("access_early_ended", f"client_id={client_id}", None)
    return EarlyEnded(ended=ended)
