"""Admin and integration REST of the family manager.

Admin (``require_admin``):

* ``GET/PUT /api/pairing/clients/{client_id}/family`` – children (all or a
  selection) and HA notify targets of a family manager device; the right
  itself is the profile rule ``family_manager`` (profile API);
* ``GET /api/family`` – overview: family managers, children, open requests,
  house settings;
* ``GET/PUT /api/settings/family`` – requests on/off, categories, timeout,
  limits, activity days;
* ``POST /api/family/requests/{request_id}/answer`` – the admin answers a
  child's request (not limited by **Fixed** rules; first answer wins).

Integration (``require_integration``), the away route of the apps:

* ``POST /api/integration/family`` – one ``family_*`` message (or, from a
  child device with remote access, ``kids_request*``) of the
  device whose HA user called the integration (``client_id`` comes from the
  integration, never from the app); answers like the hub WebSocket; refused
  for a device that is connected to the hub (it is at home);
* ``POST /api/integration/family/notify-answer`` – a notification button the
  integration relayed (the hub also receives the HA event itself).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api.auth import require_admin, require_integration
from models.database import Database
from services import access_requests
from services.audit_log import log_admin_action
from services.family import data, service
from services.family import store as fstore
from services.family.notify import answer_by_token

logger = logging.getLogger("hub.api.family")

RELAY_CONNECTED_TEXT = "This device is connected to the hub at home."
router = APIRouter(tags=["family"])

UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
NOTIFY_PATTERN = r"^notify\.[a-z0-9_]{1,64}$"
NOT_FOUND = {404: {"description": "Unknown device or request."}}
CONFLICT = {409: {"description": "Conflict (detail.code)."}}


def _db(request: Request) -> Database:
    return request.app.state.db


def _ha(request: Request):
    return getattr(request.app.state, "ha_client", None)


def _actor(request: Request) -> str:
    return getattr(request.state, "actor", None) or "admin"


# ── request bodies ──────────────────────────────────────────────────────────


class ChildrenScope(BaseModel):
    """contracts $defs/family_children_scope"""

    model_config = ConfigDict(extra="forbid")
    all: bool
    client_ids: list[str] = Field(max_length=50)


class ClientFamily(BaseModel):
    """contracts rest/client-family.request.json"""

    model_config = ConfigDict(extra="forbid")
    scope: ChildrenScope
    notify_targets: list[str] | None = Field(default=None, max_length=5)


class FamilySettings(BaseModel):
    """contracts rest/family-settings.json"""

    model_config = ConfigDict(extra="forbid")
    requests_enabled: bool
    categories: list[Literal["stay_up", "unlock_device", "end_pause"]]
    request_timeout_minutes: int = Field(ge=2, le=30)
    child_requests_per_hour: int = Field(ge=1, le=20)
    decline_cooldown_minutes: int = Field(ge=0, le=120)
    activity_days: int = Field(ge=1, le=30)


class FamilyAnswer(BaseModel):
    """contracts rest/family-answer.request.json"""

    model_config = ConfigDict(extra="forbid")
    answer: Literal["yes", "no"]
    preset: Literal["15m", "30m", "1h", "2h", "bedtime_end"] | None = None
    message: Literal["not_now", "come_to_me", "tomorrow"] | None = None


class IntegrationFamily(BaseModel):
    """contracts integration/family.request.json"""

    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(pattern=UUID_PATTERN)
    message: dict[str, Any]


class NotifyAnswer(BaseModel):
    """contracts integration/family-notify-answer.request.json"""

    model_config = ConfigDict(extra="forbid")
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{22}$")
    notify_service: str | None = Field(default=None, pattern=NOTIFY_PATTERN)
    ha_user_name: str | None = Field(default=None, max_length=100)


# ── helpers ─────────────────────────────────────────────────────────────────


def _family_error(e: service.FamilyError, status: int) -> HTTPException:
    detail: dict[str, Any] = {
        "code": e.reason,
        "message": service.MESSAGES.get(e.reason, e.reason),
    }
    if e.request is not None:
        detail["request"] = e.request
    return HTTPException(status_code=status, detail=detail)


async def _fixed_manageable(db: Database, profile_id: str) -> list[str]:
    from services.profiles import store as pstore

    prof = await pstore.profile_row(db, profile_id)
    if not prof:
        return []
    fixed = set(pstore.body_of(prof)["fixed"])
    return [k for k in data.manageable_rules() if k in fixed]


async def _child_item(db: Database, row: dict) -> dict[str, Any]:
    item: dict[str, Any] = {
        "client_id": row["id"],
        "name": row["name"][:100],
        "profile_name": row.get("profile_name"),
        "fixed_manageable": await _fixed_manageable(db, row["profile_id"]),
    }
    if row.get("owner_label"):
        item["owner_label"] = row["owner_label"]
    return item


async def _client_family_view(db: Database, client_id: str) -> dict[str, Any]:
    from services.profiles import store as pstore

    resolved = await pstore.resolve_client(db, client_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Client not found")
    row, prof, rules = resolved
    sc = await fstore.scope_row(db, client_id)
    manager = await service.manager_from(db, row, prof, rules)
    probe = manager or service.Manager(
        client_id=client_id,
        name=row["name"],
        owner_label=None,
        platform=row.get("platform"),
        scope=sc["scope"],
        visible_area_ids=None,
        notify_targets=sc["notify_targets"],
        notify={},
        remote=False,
    )
    return {
        "client_id": client_id,
        "family_manager": manager is not None,
        "scope": sc["scope"],
        "notify_targets": sc["notify_targets"],
        "children": [
            await _child_item(db, c) for c in await service.children_of(db, probe)
        ],
    }


# ── admin ───────────────────────────────────────────────────────────────────


@router.get("/pairing/clients/{client_id}/family", responses=NOT_FOUND)
async def get_client_family(request: Request, client_id: str):
    """Children and HA notify targets of a family manager device."""
    require_admin(request)
    return await _client_family_view(_db(request), client_id)


@router.put("/pairing/clients/{client_id}/family", responses={**NOT_FOUND, **CONFLICT})
async def put_client_family(request: Request, client_id: str, body: ClientFamily):
    """Set the children (all or a selection) and the HA notify targets."""
    require_admin(request)
    db = _db(request)
    row = await db.fetchone(
        "SELECT c.id, c.access, c.platform, p.child_protection FROM paired_clients c "
        "JOIN profiles p ON p.id = c.profile_id WHERE c.id = ?",
        (client_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    from services.profiles import store as pstore

    # A watch is never a family manager: the rule family_manager does not
    # apply to watches (catalog), and a child's watch follows its phone's
    # parental controls.
    if (
        row["access"] != "member"
        or row["child_protection"]
        or row.get("platform") in pstore.WATCH_PLATFORMS
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "family.not_applicable",
                "message": "guests, watches and devices with parental controls are never family managers",
            },
        )
    import re

    ids = list(dict.fromkeys(body.scope.client_ids))
    targets = list(dict.fromkeys(body.notify_targets or []))
    if any(not re.match(service.UUID_RE.pattern, i) for i in ids):
        raise HTTPException(
            status_code=422,
            detail={"code": "scope.client_ids", "message": "client ids must be uuids"},
        )
    if any(not re.match(NOTIFY_PATTERN, t) for t in targets):
        raise HTTPException(
            status_code=422,
            detail={"code": "notify_targets", "message": "notify.<service> expected"},
        )
    children = {c["id"] for c in await service.child_devices(db)}
    ids = [i for i in ids if i in children and i != client_id]
    async with db.transaction():
        await fstore.put_scope(
            db,
            client_id,
            scope={"all": body.scope.all, "client_ids": [] if body.scope.all else ids},
            notify_targets=targets if body.notify_targets is not None else None,
            actor=_actor(request),
        )
    log_admin_action(
        "family_scope_set",
        f"client_id={client_id} all={body.scope.all} children={len(ids)} targets={len(targets)}",
        None,
    )
    await service.config_changed(client_id)
    await service.admin_changed([client_id])
    return await _client_family_view(db, client_id)


@router.get("/family")
async def get_family(request: Request):
    """Overview: family managers, children, open requests, settings."""
    require_admin(request)
    db = _db(request)
    ha_client = _ha(request)
    managers = await service.all_managers(db)
    children = await service.child_devices(db)
    managers_out = []
    for m in managers:
        child_ids = [c["id"] for c in children if service.in_scope(m, c["id"])]
        item: dict[str, Any] = {
            "client_id": m.client_id,
            "name": m.name[:100],
            "scope": m.scope,
            "child_ids": child_ids,
            "notify_targets": m.notify_targets,
            "remote": "away" if m.remote else "home_only",
        }
        if m.owner_label:
            item["owner_label"] = m.owner_label
        managers_out.append(item)
    children_out = []
    for c in children:
        item = await _child_item(db, c)
        item["manager_ids"] = [
            m.client_id for m in managers if service.in_scope(m, c["id"])
        ]
        children_out.append(item)
    admin = service.admin_manager()
    open_rows = await fstore.requests_for(
        db,
        [c["id"] for c in children],
        include_closed=False,
        since=fstore.utcnow(),
    )
    closed = [
        r
        for r in await fstore.requests_for(
            db,
            [c["id"] for c in children],
            include_closed=True,
            since=fstore.utcnow() - timedelta(hours=24),
        )
        if r["status"] != "open"
    ]
    return {
        "managers": managers_out,
        "children": children_out,
        "recent_requests": [
            await service.request_view(db, ha_client, r) for r in closed
        ],
        "open_requests": [
            await service.request_view(db, ha_client, r, viewer=admin)
            for r in open_rows
        ],
        "settings": await fstore.get_settings(db),
    }


@router.get("/settings/family")
async def get_family_settings(request: Request):
    """House settings of the family manager (requests, limits, activity)."""
    require_admin(request)
    return await fstore.get_settings(_db(request))


@router.put("/settings/family")
async def put_family_settings(request: Request, body: FamilySettings):
    """Change the house settings of the family manager."""
    require_admin(request)
    db = _db(request)
    try:
        async with db.transaction():
            saved = await fstore.put_settings(db, body.model_dump())
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail={"code": str(e), "message": f"invalid {e}"}
        ) from e
    log_admin_action("family_settings_changed", None, None)
    children = [c["id"] for c in await service.child_devices(db)]
    for cid in children:
        await service.config_changed(cid)
    await service.admin_changed(children)
    return saved


@router.post(
    "/family/requests/{request_id}/answer", responses={**NOT_FOUND, **CONFLICT}
)
async def answer_family_request(request: Request, request_id: str, body: FamilyAnswer):
    """The admin answers a child's request (first answer wins)."""
    require_admin(request)
    if body.answer == "yes" and body.preset is None:
        raise HTTPException(
            status_code=422, detail={"code": "preset", "message": "yes needs a preset"}
        )
    try:
        return await service.answer_request(
            _db(request),
            _ha(request),
            request_id,
            body.answer,
            body.preset,
            body.message,
            service.admin_manager(_actor(request)[:100]),
            "admin_ui",
        )
    except service.FamilyError as e:
        status = 404 if e.reason in ("request_not_found", "not_your_child") else 409
        if e.reason == "invalid_request":
            status = 422
        raise _family_error(e, status) from e


# ── integration ─────────────────────────────────────────────────────────────


@router.post("/integration/family", responses={429: {"description": "Rate limited."}})
async def integration_family(request: Request, body: IntegrationFamily):
    """A family message of a device away from home, relayed by the integration."""
    from api import ws as api_ws

    require_integration(request)
    msg_type = body.message.get("type")
    if not isinstance(msg_type, str) or msg_type not in (
        *access_requests.TYPES,
        *service.MANAGER_TYPES,
        *service.CHILD_TYPES,
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "message.type",
                "message": "family_*, kids_request* or access_request* message expected",
            },
        )
    early = msg_type in access_requests.TYPES
    error = access_requests.AccessRequestError if early else service.FamilyError
    try:
        if body.client_id in api_ws.get_connected_client_ids():
            # The device is connected to the hub right now: it is at home and
            # speaks for itself there. The relay never speaks for it (the hub
            # cannot check who the integration took the id from).
            logger.warning(
                "Relayed %s for %s refused: the device is connected to the hub",
                msg_type,
                body.client_id,
            )
            # The reason stays not_permitted (the contract has no own one); the
            # text says what happened, and the app's request_id is echoed as
            # the handlers do.
            refusal = error("not_permitted").message()
            refusal["message"] = RELAY_CONNECTED_TEXT
            request_id = body.message.get("request_id")
            if not early and isinstance(request_id, str) and request_id:
                refusal["request_id"] = request_id
            messages = [refusal]
        elif early:
            # Recipients away answer early access requests; a request itself
            # gets home_only (a closed device holds no valid HA key).
            messages = await access_requests.handle_message(
                _db(request), _ha(request), body.client_id, body.message, "away"
            )
        elif msg_type in service.CHILD_TYPES:
            # A child device away (only while it holds a key).
            messages = await service.handle_child_message(
                _db(request), _ha(request), body.client_id, body.message, "away"
            )
        else:
            messages = await service.handle_manager_message(
                _db(request), _ha(request), body.client_id, body.message, "away"
            )
    except Exception as e:  # noqa: BLE001 - the error answer, never a 500
        logger.error("Relayed %s failed: %s", msg_type, e, exc_info=True)
        messages = [error("invalid_request").message()]
    if any(
        m.get("type") in ("family_error", "access_request_error")
        and m.get("reason") == "rate_limited"
        for m in messages
    ):
        return JSONResponse({"messages": messages}, status_code=429)
    return {"messages": messages[:10]}


@router.post("/integration/family/notify-answer")
async def integration_family_notify_answer(request: Request, body: NotifyAnswer):
    """A button of a family notification, relayed by the integration."""
    require_integration(request)
    result, view = await answer_by_token(
        _db(request), _ha(request), body.token, body.notify_service
    )
    return {"result": result, "request": view}
