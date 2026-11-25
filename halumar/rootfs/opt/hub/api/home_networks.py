"""Further networks that count as at home.

* ``GET/PUT /api/settings/home-networks`` – admin only, contract
  ``rest/home-networks.json``. PUT replaces the whole list and answers with the
  stored, normalized one; 422 ``{code, cidr?, message}`` for an invalid or
  public network or more than ten entries. A change is logged (audit and the
  log tab, kind ``home_networks_set``), every connected app re-fetches its
  ``client_config`` and the integration gets new rule snapshots.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from api import ws as api_ws
from api.auth import require_admin
from models.database import Database
from services import enforcement, event_log
from services import home_networks as hn
from services.audit_log import log_admin_action

router = APIRouter(tags=["settings"])


class HomeNetworkEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cidr: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=40)


class HomeNetworksBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # More than ten is refused with home_networks_too_many (contract code).
    networks: list[HomeNetworkEntry] = Field(max_length=100)


def _db(request: Request) -> Database:
    return request.app.state.db


@router.get("/settings/home-networks")
async def get_home_networks(request: Request):
    """Further networks that count as at home."""
    require_admin(request)
    return {"networks": await hn.get(_db(request))}


@router.put("/settings/home-networks")
async def put_home_networks(request: Request, body: HomeNetworksBody):
    """Replace the list; apps and the integration follow at once."""
    require_admin(request)
    db = _db(request)
    before = await hn.get(db)
    try:
        saved = await hn.save(db, [e.model_dump() for e in body.networks])
    except hn.HomeNetworkError as err:
        raise HTTPException(status_code=422, detail=err.detail()) from err
    if saved != before:
        cidrs = [n["cidr"] for n in saved]
        ip = request.client.host if request.client else None
        log_admin_action("home_networks_set", ",".join(cidrs) or "-", ip)
        await event_log.record(
            db,
            category=event_log.CHANGE,
            kind="home_networks_set",
            source="hub",
            actor=None,
            detail={
                "before": [n["cidr"] for n in before],
                "after": cidrs,
            },
        )
        await api_ws.broadcast({"type": "client_config_updated", "data": {}})
        enforcement.notify_change()
    return {"networks": saved}
