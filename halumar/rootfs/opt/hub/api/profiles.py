"""Profile model: admin API, kids run-time actions, device control.

Admin (``require_admin``), contract ``openapi/hub.openapi.json`` (tag ``profiles``):

* ``GET /api/profile-rules`` (catalog), ``GET /api/profile-suggestions``,
  ``POST /api/profile-suggestions/{suggestion_id}``
* ``GET/POST /api/profiles``, ``GET/PATCH/DELETE /api/profiles/{id}``,
  ``POST …/duplicate``, ``POST …/apply-suggestion``, ``POST /api/profiles/from-device``
* ``GET/PUT /api/settings/profiles`` (default profiles, guest default duration)
* ``PUT /api/pairing/clients/{id}/profile``, ``GET …/effective``,
  ``PUT/DELETE …/overrides/{rule_key}``, ``PUT …/own-area``, ``PUT …/remote-pin``,
  ``POST /api/pairing/clients/bulk``
* ``GET /api/profile-audit`` (history with before/after)
* unchanged: ``POST /api/profiles/{id}/check`` (kids rule test),
  ``GET …/kids-status``, ``POST/DELETE …/kids-override``

Writes accept ``?dry_run=true`` and answer the preview (202,
``profile-preview.response.json``) without saving anything.

Device: ``POST /api/client/control`` with the device secret as Bearer token
(accepted on the device paths only, ``api/auth.py``). Same checks as WS
``control``. ``POST /api/client/location``: position of a device with
parental controls (``services/location.py``).
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api import ws as api_ws
from api.auth import require_admin
from models.database import Database
from services import client_activity, event_log, hub_control, kids_mode, kids_rules
from services import pin_hash
from services import scene_targets
from services.audit_log import log_admin_action
from services.ha_client import is_valid_entity_id
from services.profiles import catalog as cat
from services.profiles import changes, rows, store
from services.profiles.catalog import ProfileError

logger = logging.getLogger("hub.api.profiles")
router = APIRouter(tags=["profiles"])

SLOTS = cat.DEFAULT_SLOTS

# Documented extra answers (OpenAPI): preview of a dry run, conflicts.
PREVIEW = {202: {"description": "Preview (dry_run=true): nothing saved."}}
CONFLICT = {409: {"description": "Conflict (detail.code)."}}
NOT_FOUND = {404: {"description": "Unknown device or profile."}}


def _db(request: Request) -> Database:
    return request.app.state.db


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _actor(request: Request) -> str:
    return getattr(request.state, "actor", None) or "admin"


def _error(e: ProfileError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail())


def _preview(change: changes.Change) -> JSONResponse:
    return JSONResponse(change.preview(), status_code=202)


async def _profile_or_404(db: Database, profile_id: str) -> dict:
    row = await store.profile_row(db, profile_id)
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    return row


async def _client_or_404(db: Database, client_id: str) -> dict:
    row = await store.client_row(db, client_id)
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    return row


def _public_body(body: dict) -> dict:
    """Profile body for audit/diff: never a PIN."""
    return {
        k: body[k]
        for k in ("kind", "child_protection", "rules", "fixed", "user_choice")
        if k in body
    }


# ── request bodies ──────────────────────────────────────────────────────────


ProfileKind = Literal[
    "guest", "child", "panel", "tv", "house_sitter", "babysitter", "person"
]


class ProfileWrite(BaseModel):
    """contracts rest/profile-write.request.json"""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=300)
    icon: str | None = Field(
        default=None, max_length=100, pattern=r"^mdi:[a-z0-9]+(-[a-z0-9]+)*$"
    )
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    kind: ProfileKind | None = None
    child_protection: bool | None = None
    rules: dict[str, Any] | None = None
    fixed: list[str] | None = None
    user_choice: list[str] | None = None
    remote_pin: str | None = Field(default=None, pattern=r"^[0-9]{8}$")
    remote_pin_fixed: bool | None = None


class ProfileName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)


class FromDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(max_length=64)
    name: str = Field(max_length=200)
    assign: bool = True


class ProfileSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_profiles: dict[str, str]
    guest_default_hours: int = Field(ge=1, le=2160)


class ClientProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(max_length=64)
    discard_overrides: bool = False


class DeviceOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    user_choice: bool = False
    expires: Literal["1h", "today", "tomorrow_morning", "7d"] | None = None
    expires_at: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=200)


class OwnArea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area_id: str | None = Field(max_length=64)


class RemotePin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str | None = Field(pattern=r"^[0-9]{8}$")


class Bulk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_ids: list[str] = Field(min_length=1, max_length=200)
    action: Literal["assign_profile", "set_own_area", "set_override", "remove_override"]
    profile_id: str | None = Field(default=None, max_length=64)
    discard_overrides: bool = False
    area_id: str | None = Field(default=None, max_length=64)
    rule: str | None = Field(default=None, max_length=64)
    value: Any = None
    expires: Literal["1h", "today", "tomorrow_morning", "7d"] | None = None
    expires_at: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=200)
    dry_run: bool = False


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: str
    service: str = Field(max_length=64)
    data: dict[str, Any] | None = None
    area_ids: list[str] | None = Field(default=None, max_length=50)
    at: str | None = None


class OverrideRequest(BaseModel):
    """Parent release: ``minutes`` or ``until: "bedtime_end"`` (exactly one)."""

    model_config = ConfigDict(extra="forbid")
    minutes: int | None = Field(
        None, ge=kids_mode.OVERRIDE_MINUTES[0], le=kids_mode.OVERRIDE_MINUTES[1]
    )
    until: Literal["bedtime_end"] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> OverrideRequest:
        if (self.minutes is None) == (self.until is None):
            raise ValueError("give either minutes or until")
        return self


# ── catalog and suggestions ─────────────────────────────────────────────────


@router.get("/profile-rules")
async def get_profile_rules(request: Request):
    """The rule catalog the hub was built with (data/profile-rules.json)."""
    require_admin(request)
    return cat.catalog()


@router.get("/profile-suggestions")
async def list_profile_suggestions(request: Request):
    """All suggestions (gallery) with the profiles created from each."""
    require_admin(request)
    db = _db(request)
    used: dict[str, list[str]] = {}
    for row in await db.fetchall(
        "SELECT id, origin_suggestion FROM profiles WHERE origin_suggestion IS NOT NULL "
        "ORDER BY name COLLATE NOCASE"
    ):
        used.setdefault(row["origin_suggestion"], []).append(row["id"])
    out = []
    for sug in cat.suggestions()["suggestions"]:
        item = copy.deepcopy(sug)
        item["profile_ids"] = used.get(sug["id"], [])
        out.append(item)
    return {"catalog_version": cat.suggestions()["catalog_version"], "suggestions": out}


@router.post("/profile-suggestions/{suggestion_id}", status_code=201)
async def add_profile_suggestion(
    request: Request, suggestion_id: str, body: ProfileName | None = None
):
    """Copy a suggestion into a new profile (never linked to it afterwards)."""
    require_admin(request)
    db = _db(request)
    sug = cat.suggestion(suggestion_id)
    if not sug:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    lang = await store.language(db)
    try:
        if body and body.name:
            name = store.clean_name(body.name)
        else:
            name = await store.free_name(db, rows.suggestion_name(sug, lang))
        async with db.transaction():
            pid = await store.insert_profile(
                db,
                name=name,
                body=await store.preselect_areas(
                    db, cat.materialize(suggestion_id), sug.get("area_patterns")
                ),
                actor=_actor(request),
                icon=sug.get("icon"),
                color=sug.get("color"),
                description=sug["description"].get(lang),
                origin=(sug["id"], sug["version"]),
            )
    except ProfileError as e:
        raise _error(e)
    await changes.audit(
        db,
        "suggestion_added",
        actor=_actor(request),
        profile_id=pid,
        after={"suggestion_id": sug["id"], "version": sug["version"]},
    )
    await changes.admin_broadcast([pid])
    return await store.profile_view(db, await store.profile_row(db, pid))


# ── profiles ────────────────────────────────────────────────────────────────


@router.get("/profiles")
async def list_profiles(request: Request):
    require_admin(request)
    db = _db(request)
    defaults = await store.default_profiles(db)
    return {
        "profiles": [
            await store.profile_view(db, row, defaults=defaults)
            for row in await store.profile_rows(db)
        ],
        "max_profiles": cat.max_profiles(),
    }


def _apply_write(body: dict, data: ProfileWrite) -> dict:
    """New body from a write request (rules merge per key, lists replace).

    Saving stores the comfort rules filled in on reading as profile values.
    """
    body = copy.deepcopy(body)
    body.pop("filled", None)
    if data.kind is not None:
        body["kind"] = data.kind
    switched_on = data.child_protection is True and not body.get("child_protection")
    if data.child_protection is not None:
        body["child_protection"] = data.child_protection
    if switched_on and "control_path" not in (data.rules or {}):
        # Parental controls start on the hub path (no key on the device);
        # Checked stays possible when the write names it.
        body["rules"]["control_path"] = "hub"
    if data.rules is not None:
        body["rules"].update(cat.check_rules(data.rules))
    if data.fixed is not None:
        body["fixed"] = cat.check_keys(data.fixed, "fixed")
    if data.user_choice is not None:
        body["user_choice"] = cat.check_user_choice(
            cat.check_keys(data.user_choice, "user_choice")
        )
    return body


def _pin_columns(
    data: ProfileWrite, fields: set[str]
) -> tuple[str | None, str | None] | None:
    if "remote_pin" not in fields:
        return None
    if data.remote_pin is None:
        return None, None
    return pin_hash.hash_pin(data.remote_pin)


def _description(value: str | None) -> str | None:
    """Card sentence of a profile: trimmed, empty = none."""
    value = " ".join((value or "").split())
    return value or None


@router.post("/profiles", status_code=201)
async def create_profile(request: Request, data: ProfileWrite):
    """Empty profile: catalog defaults, default fixed / user's choice."""
    require_admin(request)
    db = _db(request)
    if not data.name:
        raise HTTPException(
            status_code=422,
            detail={"code": "name", "message": "name is required", "field": "name"},
        )
    try:
        name = store.clean_name(data.name)
        body = _apply_write(cat.materialize(None), data)
        pin = _pin_columns(data, data.model_fields_set)
        async with db.transaction():
            pid = await store.insert_profile(
                db,
                name=name,
                body=body,
                actor=_actor(request),
                icon=data.icon,
                color=data.color,
                description=_description(data.description),
            )
            if pin or data.remote_pin_fixed:
                row = await store.profile_row(db, pid)
                await store.save_profile(
                    db,
                    row,
                    body=body,
                    actor=_actor(request),
                    pin=pin,
                    pin_fixed=data.remote_pin_fixed,
                )
    except ProfileError as e:
        raise _error(e)
    await changes.audit(
        db,
        "profile_created",
        actor=_actor(request),
        profile_id=pid,
        after=_public_body(body),
    )
    if pin and pin[0]:
        await changes.audit(
            db, "profile_remote_pin_set", actor=_actor(request), profile_id=pid
        )
    await changes.admin_broadcast([pid])
    return await store.profile_view(db, await store.profile_row(db, pid))


async def _with_hints(request: Request, view: dict, body: dict) -> dict:
    """Profile view plus ``scene_hints`` (scenes and scripts with targets
    the profile may not switch, services.scene_targets)."""
    view["scene_hints"] = await scene_targets.for_profile(
        _db(request), getattr(request.app.state, "ha_client", None), body
    )
    return view


@router.get("/profiles/{profile_id}")
async def get_profile(request: Request, profile_id: str):
    require_admin(request)
    db = _db(request)
    row = await _profile_or_404(db, profile_id)
    return await _with_hints(
        request, await store.profile_view(db, row), store.body_of(row)
    )


@router.patch("/profiles/{profile_id}", responses=PREVIEW)
async def update_profile(
    request: Request, profile_id: str, data: ProfileWrite, dry_run: bool = Query(False)
):
    """Rules merge per key, fixed/user_choice replace."""
    require_admin(request)
    db = _db(request)
    row = await _profile_or_404(db, profile_id)
    before = store.body_of(row)
    try:
        name = store.clean_name(data.name) if data.name is not None else None
        if name is not None:
            await store.ensure_name_free(db, name, profile_id)
        body = _apply_write(before, data)
        pin = _pin_columns(data, data.model_fields_set)

        async def write():
            current = await store.profile_row(db, profile_id)
            await store.save_profile(
                db,
                current,
                body=body,
                actor=_actor(request),
                name=name,
                icon=data.icon if "icon" in data.model_fields_set else store.UNSET,
                color=data.color if "color" in data.model_fields_set else store.UNSET,
                description=_description(data.description)
                if "description" in data.model_fields_set
                else store.UNSET,
                pin=pin,
                pin_fixed=data.remote_pin_fixed,
            )

        _, change = await changes.run(db, write, dry_run=dry_run)
    except ProfileError as e:
        raise _error(e)
    if dry_run:
        preview = change.preview()
        preview["scene_hints"] = await scene_targets.for_profile(
            db, getattr(request.app.state, "ha_client", None), body
        )
        return JSONResponse(preview, status_code=202)
    await changes.audit(
        db,
        "profile_updated",
        actor=_actor(request),
        profile_id=profile_id,
        before=_public_body(before),
        after=_public_body(body),
    )
    if pin is not None:
        await changes.audit(
            db,
            "profile_remote_pin_set",
            actor=_actor(request),
            profile_id=profile_id,
            note="removed" if pin[0] is None else None,
        )
    await changes.apply_effects(db, change)
    await changes.admin_broadcast([profile_id])
    row = await store.profile_row(db, profile_id)
    return await _with_hints(
        request, await store.profile_view(db, row), store.body_of(row)
    )


@router.delete(
    "/profiles/{profile_id}", status_code=204, responses={**PREVIEW, **CONFLICT}
)
async def delete_profile(
    request: Request,
    profile_id: str,
    replacement: str | None = Query(None, max_length=64),
    dry_run: bool = Query(False),
):
    """Delete a profile; devices move to ``replacement``."""
    require_admin(request)
    db = _db(request)
    row = await _profile_or_404(db, profile_id)
    defaults = await store.default_profiles(db)
    if profile_id in defaults.values():
        raise HTTPException(
            status_code=409,
            detail={
                "code": "profile_is_default",
                "message": "the profile is a default profile",
                "slots": [s for s, p in defaults.items() if p == profile_id],
            },
        )
    count = await db.fetchone("SELECT COUNT(*) AS n FROM profiles")
    if count["n"] <= 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "last_profile",
                "message": "the last profile cannot be deleted",
            },
        )
    devices = await db.fetchall(
        "SELECT id, access FROM paired_clients WHERE profile_id = ?", (profile_id,)
    )
    target = None
    if devices:
        if not replacement:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "profile_in_use",
                    "message": f"{len(devices)} devices use this profile",
                    "device_count": len(devices),
                },
            )
        if replacement == profile_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "replacement",
                    "message": "the replacement is the profile itself",
                    "field": "replacement",
                },
            )
        target = await store.profile_row(db, replacement)
        if not target:
            raise HTTPException(status_code=404, detail="Replacement profile not found")
        if target["child_protection"] and any(d["access"] == "guest" for d in devices):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "profile.guest_child_protection",
                    "message": "guest devices cannot move to a profile with parental controls",
                    "field": "replacement",
                },
            )

    async def write():
        for d in devices:
            await store.assign(db, d["id"], replacement, discard_overrides=False)
        await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))

    try:
        _, change = await changes.run(db, write, dry_run=dry_run)
    except ProfileError as e:
        raise _error(e)
    if dry_run:
        return _preview(change)
    await changes.audit(
        db,
        "profile_deleted",
        actor=_actor(request),
        profile_id=profile_id,
        before={"name": row["name"], **_public_body(store.body_of(row))},
        after={"replacement": replacement} if replacement else None,
    )
    for d in devices:
        await changes.audit(
            db,
            "profile_assigned",
            actor=_actor(request),
            client_id=d["id"],
            profile_id=replacement,
            before={"profile_id": profile_id},
            after={"profile_id": replacement},
        )
    await changes.apply_effects(db, change)
    await changes.admin_broadcast([profile_id] + ([replacement] if replacement else []))
    return Response(status_code=204)


@router.post("/profiles/{profile_id}/duplicate", status_code=201)
async def duplicate_profile(
    request: Request, profile_id: str, body: ProfileName | None = None
):
    require_admin(request)
    db = _db(request)
    row = await _profile_or_404(db, profile_id)
    lang = await store.language(db)
    try:
        if body and body.name:
            name = store.clean_name(body.name)
        else:
            prefix = "Kopie von " if lang == "de" else "Copy of "
            name = await store.free_name(db, prefix + row["name"])
        source = store.body_of(row)
        async with db.transaction():
            pid = await store.insert_profile(
                db,
                name=name,
                body=source,
                actor=_actor(request),
                icon=row.get("icon"),
                color=row.get("color"),
                description=row.get("description"),
            )
            if row.get("remote_pin_hash"):
                await db.execute(
                    "UPDATE profiles SET remote_pin_hash = ?, remote_pin_salt = ?, "
                    "remote_pin_fixed = ? WHERE id = ?",
                    (
                        row["remote_pin_hash"],
                        row["remote_pin_salt"],
                        row["remote_pin_fixed"],
                        pid,
                    ),
                )
    except ProfileError as e:
        raise _error(e)
    await changes.audit(
        db,
        "profile_created",
        actor=_actor(request),
        profile_id=pid,
        after=_public_body(source),
        note=f"duplicate of {profile_id}",
    )
    await changes.admin_broadcast([pid])
    return await store.profile_view(db, await store.profile_row(db, pid))


@router.post(
    "/profiles/{profile_id}/apply-suggestion", responses={**PREVIEW, **CONFLICT}
)
async def apply_profile_suggestion(
    request: Request, profile_id: str, dry_run: bool = Query(False)
):
    """Take over the newer version of the suggestion (only on request)."""
    require_admin(request)
    db = _db(request)
    row = await _profile_or_404(db, profile_id)
    sug = cat.suggestion(row.get("origin_suggestion") or "")
    if not sug or sug["version"] <= (row.get("origin_version") or 0):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "no_suggestion_update",
                "message": "no newer version of the suggestion",
            },
        )
    before = store.body_of(row)
    body = cat.materialize(sug["id"])
    body["kind"] = before["kind"]

    async def write():
        await store.save_profile(
            db,
            await store.profile_row(db, profile_id),
            body=body,
            actor=_actor(request),
            origin_version=sug["version"],
        )

    try:
        _, change = await changes.run(db, write, dry_run=dry_run)
    except ProfileError as e:
        raise _error(e)
    if dry_run:
        return _preview(change)
    await changes.audit(
        db,
        "suggestion_applied",
        actor=_actor(request),
        profile_id=profile_id,
        before=_public_body(before),
        after=_public_body(body),
        note=f"{sug['id']} v{sug['version']}",
    )
    await changes.apply_effects(db, change)
    await changes.admin_broadcast([profile_id])
    return await store.profile_view(db, await store.profile_row(db, profile_id))


@router.post("/profiles/from-device", status_code=201)
async def create_profile_from_device(request: Request, data: FromDevice):
    """Profile from a device: its profile values with all its adjustments."""
    # Only the permanent adjustments go into the profile; temporary ones stay
    # temporary on the device. (Comment, not docstring: the docstring
    # is the description in the contract's OpenAPI snapshot.)
    require_admin(request)
    db = _db(request)
    client = await _client_or_404(db, data.client_id)
    prof = await store.profile_row(db, client["profile_id"])
    source = store.body_of(prof)
    body = copy.deepcopy(source)
    uc = set(body["user_choice"])
    for o in await store.overrides_of(db, data.client_id):
        if not cat.rule(o["rule_key"]) or o.get("expires_at"):
            continue
        body["rules"][o["rule_key"]] = json.loads(o["value_json"])
        if o["user_choice"]:
            uc.add(o["rule_key"])
        else:
            uc.discard(o["rule_key"])
    body["user_choice"] = sorted(uc & cat.user_choice_allowed())
    try:
        name = store.clean_name(data.name)

        async def write():
            pid = await store.insert_profile(
                db,
                name=name,
                body=body,
                actor=_actor(request),
                icon=prof.get("icon"),
                color=prof.get("color"),
            )
            if data.assign:
                await store.assign(db, data.client_id, pid, discard_overrides=False)
                # The permanent ones are in the profile now.
                await db.execute(
                    "DELETE FROM device_overrides WHERE client_id = ? "
                    "AND expires_at IS NULL",
                    (data.client_id,),
                )
            return pid

        pid, change = await changes.run(db, write, dry_run=False)
    except ProfileError as e:
        raise _error(e)
    await changes.audit(
        db,
        "profile_created",
        actor=_actor(request),
        profile_id=pid,
        client_id=data.client_id,
        after=_public_body(body),
        note="from device",
    )
    if data.assign:
        await changes.audit(
            db,
            "profile_assigned",
            actor=_actor(request),
            client_id=data.client_id,
            profile_id=pid,
            before={"profile_id": prof["id"]},
            after={"profile_id": pid},
        )
    await changes.apply_effects(db, change)
    await changes.admin_broadcast([pid])
    return await store.profile_view(db, await store.profile_row(db, pid))


# ── house settings ──────────────────────────────────────────────────────────


@router.get("/settings/profiles")
async def get_profile_settings(request: Request):
    require_admin(request)
    db = _db(request)
    return {
        "default_profiles": await store.default_profiles(db),
        "guest_default_hours": await store.guest_default_hours(db),
    }


@router.put("/settings/profiles")
async def put_profile_settings(request: Request, data: ProfileSettings):
    require_admin(request)
    db = _db(request)
    if set(data.default_profiles) != set(SLOTS):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "default_profiles",
                "message": f"exactly the slots {list(SLOTS)}",
                "field": "default_profiles",
            },
        )
    for slot, pid in data.default_profiles.items():
        row = await store.profile_row(db, pid)
        if not row:
            raise HTTPException(status_code=404, detail="Profile not found")
        if slot == "guest" and row["child_protection"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "profile.guest_child_protection",
                    "message": "the guest default cannot have parental controls",
                    "field": "default_profiles.guest",
                },
            )
    before = {
        "default_profiles": await store.default_profiles(db),
        "guest_default_hours": await store.guest_default_hours(db),
    }
    async with db.transaction():
        for slot, pid in data.default_profiles.items():
            await rows.set_setting(db, rows.default_setting_key(slot), pid)
        await rows.set_setting(
            db, rows.SETTING_GUEST_HOURS, str(data.guest_default_hours)
        )
    after = data.model_dump()
    await changes.audit(
        db,
        "default_profiles_changed",
        actor=_actor(request),
        before=before,
        after=after,
    )
    await changes.admin_broadcast(sorted(set(data.default_profiles.values())))
    return after


# ── devices ─────────────────────────────────────────────────────────────────


@router.put("/pairing/clients/{client_id}/profile", responses=PREVIEW)
async def assign_client_profile(
    request: Request, client_id: str, data: ClientProfile, dry_run: bool = Query(False)
):
    """Assign a profile (adjustments of rules it fixes are deleted)."""
    require_admin(request)
    db = _db(request)
    client = await _client_or_404(db, client_id)

    async def write():
        await store.assign(
            db, client_id, data.profile_id, discard_overrides=data.discard_overrides
        )

    try:
        _, change = await changes.run(db, write, dry_run=dry_run)
    except ProfileError as e:
        raise _error(e)
    if dry_run:
        return _preview(change)
    from api.pairing.clients import client_view

    if client["profile_id"] != data.profile_id or data.discard_overrides:
        kids_rules.counters.clear_client(client_id)
        prof = await store.profile_row(db, data.profile_id)
        await client_activity.record(
            db,
            client_id,
            client_activity.PROFILE_CHANGED,
            {
                "profile_id": data.profile_id,
                "profile_name": prof["name"],
                "child_protection": bool(prof["child_protection"]),
            },
        )
        await changes.audit(
            db,
            "profile_assigned",
            actor=_actor(request),
            client_id=client_id,
            profile_id=data.profile_id,
            before={"profile_id": client["profile_id"]},
            after={
                "profile_id": data.profile_id,
                "discard_overrides": data.discard_overrides,
            },
        )
    await changes.apply_effects(db, change)
    return await client_view(db, client_id)


@router.get("/pairing/clients/{client_id}/effective", responses=NOT_FOUND)
async def get_client_effective(request: Request, client_id: str):
    require_admin(request)
    try:
        return await _effective(request, _db(request), client_id)
    except ProfileError as e:
        raise _error(e)


async def _effective(request: Request, db: Database, client_id: str) -> dict:
    """Effective rules of a device plus ``scene_hints``
    (services.scene_targets)."""
    view = await store.effective_view(db, client_id)
    view["scene_hints"] = await scene_targets.for_client(
        db, getattr(request.app.state, "ha_client", None), client_id
    )
    return view


async def _tz(request: Request) -> str:
    return (
        await kids_mode.hub_time_zone(getattr(request.app.state, "ha_client", None))
        or "UTC"
    )


@router.put("/pairing/clients/{client_id}/overrides/{rule_key}", responses=PREVIEW)
async def put_client_override(
    request: Request,
    client_id: str,
    rule_key: str,
    data: DeviceOverride,
    dry_run: bool = Query(False),
):
    """Adjust one rule on one device, permanently or until a time."""
    require_admin(request)
    db = _db(request)
    await _client_or_404(db, client_id)
    if "value" not in data.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "override.value",
                "message": "value is required",
                "field": "value",
            },
        )
    before = [
        store.override_view(o)
        for o in await store.overrides_of(db, client_id)
        if o["rule_key"] == rule_key
    ]
    try:
        end = store.expires_from_request(
            data.expires, data.expires_at, await _tz(request)
        )
        if data.note is not None and len(data.note) > cat.note_max_length():
            raise ProfileError(422, "override.note", "note too long", "note")

        async def write():
            await store.put_override(
                db,
                client_id,
                rule_key,
                value=data.value,
                user_choice=data.user_choice,
                expires_at=end,
                note=data.note,
                actor=_actor(request),
            )

        _, change = await changes.run(db, write, dry_run=dry_run)
    except ProfileError as e:
        raise _error(e)
    if dry_run:
        return _preview(change)
    await changes.audit(
        db,
        "override_set",
        actor=_actor(request),
        client_id=client_id,
        rule_key=rule_key,
        before=before[0] if before else None,
        after={"value": data.value, "user_choice": data.user_choice, "expires_at": end},
        note=data.note,
    )
    await changes.apply_effects(db, change)
    return await _effective(request, db, client_id)


@router.delete("/pairing/clients/{client_id}/overrides/{rule_key}", responses=PREVIEW)
async def delete_client_override(
    request: Request, client_id: str, rule_key: str, dry_run: bool = Query(False)
):
    """ "Use profile value": drop the adjustment of one rule."""
    require_admin(request)
    db = _db(request)
    await _client_or_404(db, client_id)
    before = [
        store.override_view(o)
        for o in await store.overrides_of(db, client_id)
        if o["rule_key"] == rule_key
    ]
    if not before:
        raise HTTPException(status_code=404, detail="Adjustment not found")

    async def write():
        await store.delete_override(db, client_id, rule_key)

    _, change = await changes.run(db, write, dry_run=dry_run)
    if dry_run:
        return _preview(change)
    await changes.audit(
        db,
        "override_removed",
        actor=_actor(request),
        client_id=client_id,
        rule_key=rule_key,
        before=before[0],
    )
    await changes.apply_effects(db, change)
    return await _effective(request, db, client_id)


@router.put("/pairing/clients/{client_id}/own-area", responses=PREVIEW)
async def put_client_own_area(
    request: Request, client_id: str, data: OwnArea, dry_run: bool = Query(False)
):
    require_admin(request)
    db = _db(request)
    client = await _client_or_404(db, client_id)

    async def write():
        await store.set_own_area(db, client_id, data.area_id)

    try:
        _, change = await changes.run(db, write, dry_run=dry_run)
    except ProfileError as e:
        raise _error(e)
    if dry_run:
        return _preview(change)
    await changes.audit(
        db,
        "own_area_set",
        actor=_actor(request),
        client_id=client_id,
        before={"own_area_id": client.get("own_area_id")},
        after={"own_area_id": data.area_id},
    )
    await changes.apply_effects(db, change, touched=[client_id])
    return await _effective(request, db, client_id)


@router.put("/pairing/clients/{client_id}/remote-pin")
async def put_client_remote_pin(request: Request, client_id: str, data: RemotePin):
    """Remote PIN of the device (8 digits, PBKDF2 only, never logged)."""
    require_admin(request)
    db = _db(request)
    client = await _client_or_404(db, client_id)
    prof = await store.profile_row(db, client["profile_id"])
    if (
        data.pin is not None
        and prof.get("remote_pin_fixed")
        and prof.get("remote_pin_hash")
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "remote_pin.fixed",
                "message": "the profile PIN is fixed",
                "field": "pin",
            },
        )
    pin = pin_hash.hash_pin(data.pin) if data.pin is not None else (None, None)

    async def write():
        await db.execute(
            "UPDATE paired_clients SET settings_pin_hash = ?, settings_pin_salt = ? WHERE id = ?",
            (pin[0], pin[1], client_id),
        )

    _, change = await changes.run(db, write, dry_run=False)
    await changes.audit(
        db,
        "remote_pin_set",
        actor=_actor(request),
        client_id=client_id,
        note="removed" if data.pin is None else None,
    )
    await changes.apply_effects(db, change, touched=[client_id])
    if not change.changed_ids:
        # The hash itself changed (not visible in the rules): the app needs it.
        await api_ws.send_to_client(
            client_id, {"type": "client_config_updated", "data": {}}
        )
    return await _effective(request, db, client_id)


@router.post("/pairing/clients/bulk")
async def bulk_update_clients(request: Request, data: Bulk):
    """Change several devices at once (4 actions, ``dry_run``)."""
    require_admin(request)
    db = _db(request)
    if data.action == "assign_profile" and not data.profile_id:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "profile_id",
                "message": "profile_id is required",
                "field": "profile_id",
            },
        )
    if data.action == "set_own_area" and "area_id" not in data.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "area_id",
                "message": "area_id is required",
                "field": "area_id",
            },
        )
    if data.action in ("set_override", "remove_override") and not data.rule:
        raise HTTPException(
            status_code=422,
            detail={"code": "rule", "message": "rule is required", "field": "rule"},
        )
    if data.action == "set_override" and "value" not in data.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail={"code": "value", "message": "value is required", "field": "value"},
        )
    if data.action == "assign_profile" and not await store.profile_row(
        db, data.profile_id
    ):
        raise HTTPException(status_code=404, detail="Profile not found")
    if (
        data.action == "set_own_area"
        and data.area_id
        and not await db.fetchone("SELECT id FROM areas WHERE id = ?", (data.area_id,))
    ):
        raise HTTPException(status_code=404, detail="Area not found")
    if data.rule and not cat.rule(data.rule):
        raise HTTPException(
            status_code=422,
            detail={
                "code": f"rules.{data.rule}",
                "message": "unknown rule",
                "field": "rule",
            },
        )
    end = None
    if data.action == "set_override":
        try:
            end = store.expires_from_request(
                data.expires, data.expires_at, await _tz(request)
            )
            cat.check_value(data.rule, data.value)
        except ProfileError as e:
            raise _error(e)
    reasons = {
        "profile.guest_child_protection": "guest_child_protection",
        "override.fixed": "rule_fixed",
        "override.boundary": "boundary",
        "override.not_applicable": "not_applicable",
        "override.value": "invalid_value",
        "client_not_found": "not_found",
    }
    results: list[dict] = []

    async def write():
        for cid in dict.fromkeys(data.client_ids):
            before = await db.fetchone(
                "SELECT profile_id, own_area_id FROM paired_clients WHERE id = ?",
                (cid,),
            )
            if not before:
                results.append(
                    {"client_id": cid, "status": "skipped", "reason": "not_found"}
                )
                continue
            overs_before = [o["rule_key"] for o in await store.overrides_of(db, cid)]
            try:
                async with db.transaction():
                    if data.action == "assign_profile":
                        await store.assign(
                            db,
                            cid,
                            data.profile_id,
                            discard_overrides=data.discard_overrides,
                        )
                    elif data.action == "set_own_area":
                        await store.set_own_area(db, cid, data.area_id)
                    elif data.action == "set_override":
                        await store.put_override(
                            db,
                            cid,
                            data.rule,
                            value=data.value,
                            user_choice=False,
                            expires_at=end,
                            note=data.note,
                            actor=_actor(request),
                        )
                    else:
                        await store.delete_override(db, cid, data.rule)
            except ProfileError as e:
                results.append(
                    {
                        "client_id": cid,
                        "status": "skipped",
                        "reason": reasons.get(e.code, "invalid_value"),
                    }
                )
                continue
            after = await db.fetchone(
                "SELECT profile_id, own_area_id FROM paired_clients WHERE id = ?",
                (cid,),
            )
            overs_after = [o["rule_key"] for o in await store.overrides_of(db, cid)]
            changed = (
                after != before
                or overs_after != overs_before
                or data.action == "set_override"
            )
            results.append(
                {
                    "client_id": cid,
                    "status": "changed" if changed else "unchanged",
                    "reason": None,
                }
            )

    _, change = await changes.run(db, write, dry_run=data.dry_run)
    if data.dry_run:
        return {"dry_run": True, "results": results, "preview": change.preview()}
    for r in results:
        if r["status"] == "changed":
            action = {
                "assign_profile": "profile_assigned",
                "set_own_area": "own_area_set",
                "set_override": "override_set",
                "remove_override": "override_removed",
            }[data.action]
            await changes.audit(
                db,
                action,
                actor=_actor(request),
                client_id=r["client_id"],
                profile_id=data.profile_id,
                rule_key=data.rule,
                after={"value": data.value, "expires_at": end}
                if data.action == "set_override"
                else None,
                note="bulk",
            )
    await changes.apply_effects(db, change)
    log_admin_action(
        "clients_bulk", f"{data.action} {len(data.client_ids)}", _ip(request)
    )
    return {"dry_run": False, "results": results, "preview": None}


# ── audit ───────────────────────────────────────────────────────────────────


@router.get("/profile-audit")
async def get_profile_audit(
    request: Request,
    client_id: str | None = Query(None, max_length=64),
    profile_id: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
):
    """History of the profile model (newest first) with before/after."""
    require_admin(request)
    db = _db(request)
    where, params = [], []
    if client_id:
        where.append("client_id = ?")
        params.append(client_id)
    if profile_id:
        where.append("profile_id = ?")
        params.append(profile_id)
    sql = "SELECT * FROM profile_audit"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    entries = []
    for r in await db.fetchall(sql, tuple(params)):
        entries.append(
            {
                "id": r["id"],
                "ts": r["ts"],
                "action": r["action"],
                "actor": r["actor"],
                "profile_id": r["profile_id"],
                "client_id": r["client_id"],
                "rule": r["rule_key"],
                "before": json.loads(r["before_json"]) if r["before_json"] else None,
                "after": json.loads(r["after_json"]) if r["after_json"] else None,
                "note": r["note"],
            }
        )
    return {"entries": entries}


# ── kids rule test and run-time actions ──────────────────────────────


@router.post("/profiles/{profile_id}/check")
async def check_rules(request: Request, profile_id: str, body: CheckRequest):
    """Dry run: would the profile's kids rules allow this action now (or at ``at``)?

    Without ``area_ids`` the areas the entity is placed in are used. Counters
    and pauses of devices are not considered.
    """
    require_admin(request)
    db = _db(request)
    row = await _profile_or_404(db, profile_id)
    if not row["child_protection"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_child_protection",
                "message": "the profile has no parental controls",
            },
        )
    if not is_valid_entity_id(body.entity_id):
        raise HTTPException(status_code=422, detail="entity_id")
    at = kids_mode.parse_iso(body.at) if body.at else kids_mode.utcnow()
    if at is None:
        raise HTTPException(status_code=422, detail="at")
    area_ids = body.area_ids
    if area_ids is None:
        rows_ = await db.fetchall(
            "SELECT DISTINCT area_id FROM area_entities WHERE entity_id = ?",
            (body.entity_id,),
        )
        area_ids = [r["area_id"] for r in rows_]
    effective = store.resolve_inputs(
        store.device_inputs(
            {"platform": "ios", "access": "member", "own_area_id": None}, row, []
        )
    )
    profile = kids_mode.layers_of(effective)
    decision = await kids_mode.decide(
        db,
        request.app.state.ha_client,
        profile=profile,
        client_id=None,
        entity_id=body.entity_id,
        service=body.service,
        data=body.data or {},
        area_ids=area_ids,
        override_until=None,
        now=at,
        use_counters=False,
    )
    if decision.allowed:
        result: dict[str, Any] = {"allowed": True, "service": decision.service}
        if decision.adjusted:
            result["adjusted"] = decision.adjusted
        return result
    msg = kids_mode.blocked_message(None, body.entity_id, decision, at)
    return {
        "allowed": False,
        "reason": msg["reason"],
        "level": msg["level"],
        "until": msg["until"],
        "limit": msg.get("limit"),
    }


async def _child_row(db: Database, client_id: str) -> dict:
    row = await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (client_id,))
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    child = await db.fetchone(
        "SELECT p.child_protection FROM paired_clients c JOIN profiles p "
        "ON p.id = c.profile_id WHERE c.id = ?",
        (client_id,),
    )
    if not (child and child["child_protection"]):
        raise HTTPException(status_code=409, detail="not_child")
    return row


@router.get("/pairing/clients/{client_id}/kids-status")
async def get_kids_status(request: Request, client_id: str):
    require_admin(request)
    db = _db(request)
    if not await db.fetchone(
        "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
    ):
        raise HTTPException(status_code=404, detail="Client not found")
    return await kids_mode.kids_status(db, client_id)


@router.post("/pairing/clients/{client_id}/kids-override")
async def start_override(request: Request, client_id: str, body: OverrideRequest):
    """Release a child device (no quiet hours/rate limit) for ``minutes`` or
    until the end of its bedtime ("until tomorrow morning")."""
    require_admin(request)
    db = _db(request)
    await _child_row(db, client_id)
    until_at = None
    if body.until == "bedtime_end":
        until_at = await kids_mode.bedtime_release_end(
            db, request.app.state.ha_client, client_id
        )
    await kids_mode.set_override(db, client_id, body.minutes, until_at=until_at)
    kids_rules.counters.clear_client(client_id)
    status = await kids_mode.kids_status(db, client_id)
    detail: dict[str, Any] = {"action": "release", "until": status["override_until"]}
    if body.minutes is not None:
        detail["minutes"] = body.minutes
    await client_activity.record(db, client_id, client_activity.KIDS_OVERRIDE, detail)
    await event_log.kids_release(
        db,
        client_id,
        actor=_actor(request),
        action="release",
        until=status["override_until"],
    )
    log_admin_action(
        "kids_override",
        details=f"client_id={client_id} "
        + (
            f"minutes={body.minutes}"
            if body.minutes is not None
            else f"until={body.until}"
        ),
        ip=_ip(request),
    )
    await api_ws.send_to_client(
        client_id, {"type": "client_config_updated", "data": {}}
    )
    return status


@router.delete("/pairing/clients/{client_id}/kids-override")
async def end_override(request: Request, client_id: str):
    """ "Lift pause": end a release and every running cooldown of the device."""
    require_admin(request)
    db = _db(request)
    await _child_row(db, client_id)
    await kids_mode.set_override(db, client_id, None)
    cleared = kids_rules.counters.clear_client(client_id)
    await client_activity.record(
        db,
        client_id,
        client_activity.KIDS_OVERRIDE,
        {"action": "lift", "cooldowns_cleared": cleared},
    )
    log_admin_action(
        "kids_pause_lifted", details=f"client_id={client_id}", ip=_ip(request)
    )
    await event_log.kids_release(
        db, client_id, actor=_actor(request), action="lift", until=None
    )
    await api_ws.send_to_client(
        client_id, {"type": "client_config_updated", "data": {}}
    )
    return await kids_mode.kids_status(db, client_id)


# ── device REST control ─────────────────────────────────────────────────────

_STATUS = {
    hub_control.ERR_NOT_HUB_CONTROLLED: 403,
    hub_control.ERR_NOT_VISIBLE: 403,
    hub_control.ERR_INVALID_REQUEST: 422,
    hub_control.ERR_NOT_READY: 409,
    hub_control.ERR_HA_ERROR: 502,
    hub_control.ERR_HA_UNAVAILABLE: 503,
}


@router.post("/client/control")
async def client_control(request: Request):
    """Control one entity as a device on the hub path (same as WS ``control``).

    200 ``control_ok``, 403 ``rule_blocked`` / ``control_error``, 422/409/
    502/503 ``control_error``.
    """
    if getattr(request.state, "auth_type", None) != "client":
        raise HTTPException(status_code=403, detail="Device credential required")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any parse error is a bad request
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            hub_control.error_message(None, hub_control.ERR_INVALID_REQUEST),
            status_code=422,
        )
    answer = await hub_control.control(
        _db(request), request.app.state.ha_client, request.state.client_id, body
    )
    if answer["type"] == "control_ok":
        return answer
    if answer["type"] == "rule_blocked":
        return JSONResponse(answer, status_code=403)
    return JSONResponse(answer, status_code=_STATUS.get(answer["reason"], 400))


@router.post("/client/location")
async def client_location(request: Request):
    """Position of a device on the hub path without a hub socket (same
    as WS ``location_report``). 200 ``location_report_ok``, 403/422/429
    ``location_report_error`` (429 with ``Retry-After``)."""
    from services import location

    if getattr(request.state, "auth_type", None) != "client":
        raise HTTPException(status_code=403, detail="Device credential required")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any parse error is a bad request
        body = None
    try:
        return await location.report(
            _db(request), request.state.client_id, body, via="rest"
        )
    except location.LocationError as err:
        headers = (
            {"Retry-After": str(err.retry_after)}
            if err.retry_after is not None
            else None
        )
        return JSONResponse(
            err.answer(), status_code=location.STATUS[err.reason], headers=headers
        )
