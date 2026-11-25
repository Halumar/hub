"""Log tab of the admin UI and notifications at events.

Admin (session, Ingress, admin token):

* ``GET /api/log`` – entries newest first with filters, paged by ``before_id``
* ``GET /api/log/summary`` – unread alarms, counts, facets for the filters
* ``POST /api/log/read`` – mark alarms as read (some or all)
* ``GET /api/log/export`` – every entry of the filters (CSV built in the UI)
* ``GET/PUT /api/log/settings`` – retention 7/30/90 days, hidden persons,
  alarm threshold of rejections
* ``GET /api/log/notify-targets`` – ``notify.*`` actions of Home Assistant
  and the devices that may receive notifications
* ``GET/PUT /api/settings/event-notifications`` and ``POST …/test``
  (contract ``rest/event-notifications.json``)

``POST /api/integration/events`` lives in ``api/integration.py``; the log
stores its events as a sink of ``services.integration_events``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from api.auth import require_admin
from models.database import Database
from services import event_log, event_notify, kids_mode
from services.audit_log import log_admin_action

router = APIRouter(tags=["log"])

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
NOTIFY_RE = re.compile(r"^notify\.[a-z0-9_]{1,255}$")
HM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _db(request: Request) -> Database:
    return request.app.state.db


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def _tz(request: Request) -> str | None:
    return await kids_mode.hub_time_zone(getattr(request.app.state, "ha_client", None))


async def _filters(
    request: Request,
    category: str | None,
    range_: str | None,
    client_id: str | None,
    owner: str | None,
    profile_id: str | None,
    area_id: str | None,
    q: str | None,
    unread: bool,
    kind: str | None = None,
    important: bool = False,
) -> dict:
    if category and category not in event_log.CATEGORIES:
        raise HTTPException(status_code=422, detail="category")
    filters: dict = {
        "category": category,
        "client_id": client_id,
        "owner": owner,
        "profile_id": profile_id,
        "area_id": area_id,
        "q": (q or "").strip()[:100] or None,
        "unread": unread,
        "kind": kind,
        "important": important,
    }
    now = datetime.now(timezone.utc)
    if range_ == "today":
        filters["since"] = event_log.iso(
            event_log.start_of_day(now, await _tz(request))
        )
    elif range_ == "7d":
        filters["since"] = event_log.iso(now - timedelta(days=7))
    elif range_ not in (None, "", "all"):
        raise HTTPException(status_code=422, detail="range")
    return filters


@router.get("/log")
async def get_log(
    request: Request,
    category: str | None = Query(None, max_length=16),
    range: str | None = Query(None, max_length=8),  # noqa: A002 - query name
    client_id: str | None = Query(None, max_length=64),
    owner: str | None = Query(None, max_length=50),
    profile_id: str | None = Query(None, max_length=64),
    area_id: str | None = Query(None, max_length=64),
    kind: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=100),
    unread: bool = False,
    important: bool = False,
    before_id: int | None = Query(None, ge=1),
    limit: int = Query(event_log.PAGE_DEFAULT, ge=1, le=event_log.PAGE_MAX),
):
    """Log entries, newest first. ``has_more`` = older entries exist (``before_id``).

    ``important`` = only alarms and the kinds marked important in the
    taxonomy (data/events.json)."""
    require_admin(request)
    filters = await _filters(
        request,
        category,
        range,
        client_id,
        owner,
        profile_id,
        area_id,
        q,
        unread,
        kind,
        important,
    )
    return await event_log.list_entries(
        _db(request), filters, before_id=before_id, limit=limit
    )


@router.get("/log/export")
async def export_log(
    request: Request,
    category: str | None = Query(None, max_length=16),
    range: str | None = Query(None, max_length=8),  # noqa: A002
    client_id: str | None = Query(None, max_length=64),
    owner: str | None = Query(None, max_length=50),
    profile_id: str | None = Query(None, max_length=64),
    area_id: str | None = Query(None, max_length=64),
    q: str | None = Query(None, max_length=100),
    important: bool = False,
):
    """Every entry of the filters (at most 20 000) for the CSV export."""
    require_admin(request)
    filters = await _filters(
        request,
        category,
        range,
        client_id,
        owner,
        profile_id,
        area_id,
        q,
        False,
        important=important,
    )
    result = await event_log.list_entries(
        _db(request), filters, limit=event_log.EXPORT_MAX
    )
    log_admin_action("log_exported", f"{len(result['entries'])} entries", _ip(request))
    return result


@router.get("/log/summary")
async def get_summary(request: Request):
    require_admin(request)
    return await event_log.summary(_db(request), await _tz(request))


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[int] | None = Field(None, max_length=500)
    all: bool = False

    @model_validator(mode="after")
    def one_of(self):
        if self.all == (self.ids is not None):
            raise ValueError("give ids or all: true")
        return self


@router.post("/log/read")
async def mark_read(request: Request, body: ReadRequest):
    require_admin(request)
    db = _db(request)
    n = await event_log.mark_read(db, None if body.all else body.ids)
    from api import ws as api_ws
    from api.ws import EventAudience

    await api_ws.broadcast(
        {"type": "log_read", "data": {"ids": body.ids, "all": body.all}},
        audience=EventAudience.ADMIN,
    )
    return {"marked": n}


class AlarmThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1, le=100)
    window_minutes: int = Field(ge=1, le=1440)


class LogSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: Literal[7, 30, 90]
    hidden_owners: list[str] = Field(default_factory=list, max_length=50)
    alarm_threshold: AlarmThreshold = Field(
        default_factory=lambda: AlarmThreshold(**event_log.DEFAULT_ALARM_THRESHOLD)
    )

    @field_validator("hidden_owners")
    @classmethod
    def owners(cls, v: list[str]) -> list[str]:
        out = []
        for o in v:
            o = o.strip()
            if not o or len(o) > 50:
                raise ValueError("owner label 1–50 characters")
            if o not in out:
                out.append(o)
        return out


@router.get("/log/settings")
async def get_log_settings(request: Request):
    require_admin(request)
    return await event_log.get_settings(_db(request))


@router.put("/log/settings")
async def put_log_settings(request: Request, body: LogSettings):
    require_admin(request)
    saved = await event_log.save_settings(_db(request), body.model_dump())
    log_admin_action(
        "log_settings",
        f"retention={saved['retention_days']} hidden={len(saved['hidden_owners'])}",
        _ip(request),
    )
    return saved


@router.get("/log/notify-targets")
async def get_targets(request: Request):
    """``notify.*`` actions from Home Assistant and the eligible devices."""
    require_admin(request)
    db = _db(request)
    services: list[dict] = []
    error = None
    try:
        services = await event_notify.notify_services(request.app.state.ha_client)
    except Exception as e:  # noqa: BLE001 - HA down: say so, keep the devices
        error = "ha_unavailable"
        event_log.logger.warning("notify services not loaded: %s", e)
    return {
        "notify": services,
        "devices": await event_notify.eligible_devices(db),
        "error": error,
    }


# ── notifications at events (contract draft) ────────────────────────────────


class QuietWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        min_length=1, max_length=7
    )
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def hm(cls, v: str) -> str:
        if not HM_RE.match(v):
            raise ValueError("HH:MM")
        return v


class QuietDefault(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def hm(cls, v: str) -> str:
        if not HM_RE.match(v):
            raise ValueError("HH:MM")
        return v


class Targets(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notify: list[str] = Field(max_length=10)
    devices: list[str] = Field(max_length=50)

    @field_validator("notify")
    @classmethod
    def notify_names(cls, v: list[str]) -> list[str]:
        if any(not NOTIFY_RE.match(x) for x in v) or len(set(v)) != len(v):
            raise ValueError("notify.<service>, unique")
        return v

    @field_validator("devices")
    @classmethod
    def device_ids(cls, v: list[str]) -> list[str]:
        if any(not UUID_RE.match(x) for x in v) or len(set(v)) != len(v):
            raise ValueError("device ids, unique")
        return v


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_ids: list[str] = Field(max_length=100)
    owner_labels: list[str] = Field(max_length=50)
    profile_ids: list[str] = Field(max_length=50)
    area_ids: list[str] = Field(max_length=100)
    entity_ids: list[str] = Field(max_length=200)
    routes: list[Literal["local", "external", "unknown"]] = Field(max_length=3)

    @field_validator("entity_ids")
    @classmethod
    def entities(cls, v: list[str]) -> list[str]:
        if any(not ENTITY_RE.match(x) for x in v):
            raise ValueError("entity ids")
        return v


class Threshold(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(ge=1, le=100)
    window_minutes: int = Field(ge=1, le=1440)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str = Field(min_length=1, max_length=60)
    enabled: bool
    kinds: list[str] = Field(min_length=1, max_length=30)
    targets: Targets
    filters: Filters
    threshold: Threshold
    bundle_minutes: int = Field(ge=0, le=1440)
    quiet_hours: list[QuietWindow] = Field(max_length=14)
    lead_minutes: int | None = Field(None, ge=1, le=10080)
    delay_minutes: int | None = Field(None, ge=1, le=1440)

    @field_validator("id")
    @classmethod
    def rule_id(cls, v: str) -> str:
        if not UUID_RE.match(v):
            raise ValueError("uuid")
        return v

    @field_validator("kinds")
    @classmethod
    def kinds_known(cls, v: list[str]) -> list[str]:
        known = event_log.taxonomy()
        if any(k not in known for k in v) or len(set(v)) != len(v):
            raise ValueError("kinds of data/events.json, unique")
        return v


class EventNotifications(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retention_days: Literal[7, 30, 90]
    quiet_default: QuietDefault = Field(
        default_factory=lambda: QuietDefault(**event_notify.DEFAULT_QUIET)
    )
    rules: list[Rule] = Field(max_length=event_notify.MAX_RULES)


@router.get("/settings/event-notifications")
async def get_event_notifications(request: Request):
    require_admin(request)
    return await event_notify.get_config(_db(request))


@router.put("/settings/event-notifications")
async def put_event_notifications(request: Request, body: EventNotifications):
    require_admin(request)
    db = _db(request)
    ids = [r.id for r in body.rules]
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "notifications.duplicate_id",
                "message": "rule ids must be unique",
            },
        )
    allowed = {d["client_id"] for d in await event_notify.eligible_devices(db)}
    for rule in body.rules:
        if rule.enabled and not rule.targets.notify and not rule.targets.devices:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "notifications.no_target",
                    "message": f"rule {rule.name!r} has no target",
                    "field": rule.id,
                },
            )
        bad = [d for d in rule.targets.devices if d not in allowed]
        if bad:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "notifications.device_unknown",
                    "message": "not a paired device (or an expired guest)",
                    "field": bad[0],
                },
            )
    saved = await event_notify.save_config(db, body.model_dump(exclude_none=False))
    log_admin_action(
        "event_notifications",
        f"{sum(1 for r in body.rules if r.enabled)}/{len(body.rules)} rules on",
        _ip(request),
    )
    return saved


class TestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str | None = None
    rule: Rule | None = None

    @model_validator(mode="after")
    def one_of(self):
        if (self.rule_id is None) == (self.rule is None):
            raise ValueError("give rule_id or rule")
        return self


@router.post("/settings/event-notifications/test")
async def test_event_notification(request: Request, body: TestRequest):
    """Send a test of a stored rule (``rule_id``) or of an unsaved draft (``rule``)."""
    require_admin(request)
    db = _db(request)
    if body.rule is not None:
        rule = body.rule.model_dump()
    else:
        config = await event_notify.get_config(db)
        rule = next((r for r in config["rules"] if r["id"] == body.rule_id), None)
        if rule is None:
            raise HTTPException(status_code=404, detail="rule not found")
    targets = rule["targets"]
    if not targets["notify"] and not targets["devices"]:
        raise HTTPException(
            status_code=422,
            detail={"code": "notifications.no_target", "message": "no target"},
        )
    result = await event_notify.send_test(db, rule)
    log_admin_action("event_notification_test", rule.get("name"), _ip(request))
    return result
