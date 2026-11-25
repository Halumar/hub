"""NFC and QR tags of the hub (contract docs/links.md).

* ``GET /api/scan-tags`` – every tag (newest first, at most 100);
* ``POST /api/scan-tags`` – register a tag (``tag_id`` optional: missing = a
  new id is drawn, given = an already written tag moves to this hub);
* ``PUT /api/scan-tags/{tag_id}`` – change name or action;
* ``DELETE /api/scan-tags/{tag_id}``.

Admin only. Errors ``{"detail": {"code", "message"}}``: 404 ``not_found``,
409 ``tag_exists``, 422 ``tag_limit``, ``invalid_tag_id``, ``invalid_name``,
``invalid_target``. Changes are audited and logged (kind ``scan_tag_set``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_admin
from models.database import Database
from services import event_log
from services import scan_tags as tags
from services.audit_log import log_admin_action

router = APIRouter(tags=["areas"])

ERRORS = {
    404: {"description": "not_found"},
    409: {"description": "tag_exists"},
    422: {"description": "tag_limit, invalid_tag_id, invalid_name, invalid_target"},
}


class ScanTagAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(..., max_length=16)
    entity_id: str | None = Field(None, max_length=255)
    area_id: str | None = Field(None, max_length=36)


class ScanTagWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tag_id: str | None = Field(None, max_length=22)
    name: str = Field(..., max_length=60)
    action: ScanTagAction


def _db(request: Request) -> Database:
    return request.app.state.db


def _fail(err: tags.ScanTagError) -> HTTPException:
    return HTTPException(status_code=err.status, detail=err.detail())


async def _changed(
    request: Request, what: str, tag_id: str, before: Any, after: Any
) -> None:
    ip = request.client.host if request.client else None
    log_admin_action(f"scan_tag_{what}", tag_id[:4] + "…", ip)
    await event_log.record(
        _db(request),
        category=event_log.CHANGE,
        kind="scan_tag_set",
        source="hub",
        actor=None,
        detail={"tag_id": tag_id, "change": what, "before": before, "after": after},
    )


@router.get("/scan-tags")
async def list_scan_tags(request: Request):
    """NFC and QR tags registered in this hub."""
    require_admin(request)
    return {"tags": await tags.list_tags(_db(request)), "max": tags.MAX_TAGS}


@router.post("/scan-tags", status_code=201, responses=ERRORS)
async def create_scan_tag(request: Request, body: ScanTagWrite):
    """Register a tag; without tag_id the hub draws a new one."""
    require_admin(request)
    try:
        tag = await tags.create(
            _db(request),
            name=body.name,
            action=body.action.model_dump(),
            tag_id=body.tag_id,
        )
    except tags.ScanTagError as err:
        raise _fail(err) from None
    await _changed(request, "created", tag["tag_id"], None, tag["name"])
    return tag


@router.put("/scan-tags/{tag_id}", responses=ERRORS)
async def update_scan_tag(request: Request, tag_id: str, body: ScanTagWrite):
    """Change name and action of a tag (the id stays)."""
    require_admin(request)
    if body.tag_id is not None and body.tag_id != tag_id:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_tag_id", "message": "tag_id cannot change"},
        )
    db = _db(request)
    before = await tags.get_tag(db, tag_id)
    try:
        tag = await tags.update(
            db, tag_id, name=body.name, action=body.action.model_dump()
        )
    except tags.ScanTagError as err:
        raise _fail(err) from None
    await _changed(request, "changed", tag_id, (before or {}).get("name"), tag["name"])
    return tag


@router.delete("/scan-tags/{tag_id}", responses={404: {"description": "not_found"}})
async def delete_scan_tag(request: Request, tag_id: str):
    """Remove a tag; a written tag then answers unknown_tag."""
    require_admin(request)
    db = _db(request)
    before = await tags.get_tag(db, tag_id)
    if before is None or not await tags.delete(db, tag_id):
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "unknown tag"}
        )
    await _changed(request, "deleted", tag_id, before["name"], None)
    return {"success": True}
