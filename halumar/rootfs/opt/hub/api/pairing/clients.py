"""Paired clients: list, read, update, revoke, delete, tombstone, kick."""

import logging
from typing import Any, Literal

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from api import ws as api_ws
from api.auth import require_admin
from api.ws import EventAudience
from models.database import HA_TOKEN_PENDING, Database
from models.schemas import normalize_owner_label
from services import client_activity, device_provisioning, protocol

logger = logging.getLogger("hub.api.pairing")


class AccessEarly(BaseModel):
    """Running early access of a device (contracts $defs/access_early)."""

    until: str
    by: dict[str, Any]
    request_id: str | None = None
    duration: str | None = None
    at: str


class AccessNow(BaseModel):
    """Access of a device right now (contracts $defs/access_now)."""

    open: bool
    reason: Literal["schedule", "switch"] | None = None
    until: str | None = None
    opens_at: str | None = None
    schedule: bool = False
    switch: bool | None = None
    # A Yes to the device's request opened it early (until, by).
    early: AccessEarly | None = None


class AccessSwitchUpdate(BaseModel):
    """Turn the access switch on or off (contracts rest/access-switch.request)."""

    model_config = {"extra": "forbid"}

    on: bool


class PairedClientResponse(BaseModel):
    """A paired device as the admin UI lists it (contracts rest/paired-client)."""

    id: str
    name: str
    is_active: bool
    access: str = "member"
    expires_at: str | None = None
    # Member device that set this device up: {client_id, name, method}.
    set_up_by: dict[str, str] | None = None
    created_at: str
    last_seen: str | None = None
    has_ha_token: bool = False
    # Effective rule control_path after the platform mapping (TV and watch:
    # hub becomes checked). None: the hub could not resolve the device's
    # rules (the cause is in the hub's log); its key state is unknown, and
    # has_ha_token then says whether a token exists at all.
    control_path: Literal["hub", "checked", "direct"] | None = "hub"
    # One HA user per device: set by the integration.
    ha_user_id: str | None = None
    ha_token_status: str = HA_TOKEN_PENDING
    ha_token_pending_since: str | None = None
    owner_label: str | None = None
    # Profile model: profile, own area, adjustments; rights and remote
    # access are effective profile rules (read only here).
    profile_id: str
    profile_name: str
    profile_kind: str = "person"
    child_protection: bool = False
    own_area_id: str | None = None
    override_count: int = 0
    temporary_override_count: int = 0
    # The remote PIN itself is NEVER returned, only whether the device has one.
    has_remote_pin: bool = False
    # Effective remote access; None when the device's rules cannot be
    # resolved (together with control_path None).
    remote_allowed: bool | None = False
    # Location: effective rules and what the app shares (memory of the
    # hub, never the position).
    location_rule: Literal["off", "foreground", "background"] | None = None
    location_required: bool = False
    location_shared: Literal["none", "foreground", "background"] | None = None
    location_at: str | None = None
    is_connected: bool = False
    pending_deletion: bool = False
    pending_deletion_at: str | None = None
    # Client info of the last auth/pair; null until reported.
    platform: str | None = None
    app_version: str | None = None
    app_build: str | None = None
    device_model: str | None = None
    protocol_version: int | None = None
    # The app is below the hub's minimum (or never reported its version):
    # it gets upgrade_required on its next connect. UI hint "update required".
    update_required: bool = False
    # Access hours and access switch right now.
    access_now: AccessNow


class DeviceActivityEvent(BaseModel):
    """One entry of a device's activity feed."""

    id: int
    client_id: str
    ts: str
    event: str
    detail: dict[str, object] | None = None


class DeviceActivityResponse(BaseModel):
    """Activity feed of a device, newest first (ring buffer)."""

    client_id: str
    max_events: int
    events: list[DeviceActivityEvent]


class ClientUpdate(BaseModel):
    """Name and owner label of a device (contracts client-update.request).

    Everything else is a profile rule or a device property with its own
    endpoint (api/profiles.py).
    """

    model_config = {"extra": "forbid"}

    name: str | None = Field(None, min_length=1, max_length=100)
    owner_label: str | None = Field(
        None,
        max_length=200,
        description="Owner of the device (label, max 50 characters); '' clears it",
    )

    @field_validator("owner_label")
    @classmethod
    def validate_owner_label(cls, v):
        # None = unchanged; "" clears (stored as NULL).
        return v if v is None else (normalize_owner_label(v) or "")


# Columns read for PairedClientResponse.
_CLIENT_COLUMNS = (
    "c.id, c.name, c.is_active, c.created_at, c.last_seen, c.ha_token_encrypted, "
    "c.ha_user_id, c.ha_token_status, c.ha_token_pending_since, c.owner_label, "
    "c.set_up_by_client_id, c.set_up_by_name, c.set_up_method, c.access, c.expires_at, "
    "c.pending_deletion_at, c.pending_hidden_at, c.platform, c.app_version, "
    "c.app_build, c.device_model, c.protocol_version, c.profile_id, c.own_area_id, "
    "c.settings_pin_hash, c.settings_pin_salt, p.name AS profile_name, "
    "p.kind AS profile_kind, p.child_protection, "
    "(SELECT COUNT(*) FROM device_overrides o WHERE o.client_id = c.id) AS override_count, "
    "(SELECT COUNT(*) FROM device_overrides o WHERE o.client_id = c.id "
    "AND o.expires_at IS NOT NULL) AS temporary_override_count"
)
_CLIENT_FROM = "FROM paired_clients c JOIN profiles p ON p.id = c.profile_id"


def _control_path(effective: dict | None) -> str | None:
    if effective is None:
        return None
    value = (effective.get("control_path") or {}).get("value")
    return value if value in ("checked", "direct") else "hub"


# (device, cause) already logged as not resolvable: once each, no repeats.
_unresolved_logged: set[tuple[str, str]] = set()


def _log_unresolved(client_id: str, cause: str) -> None:
    if (client_id, cause) in _unresolved_logged:
        return
    if len(_unresolved_logged) > 1000:
        _unresolved_logged.clear()
    _unresolved_logged.add((client_id, cause))
    logger.warning("Rules of device %s cannot be resolved: %s", client_id, cause)


async def _resolve_views(db: Database, ids: list[str]) -> dict[str, dict]:
    """``store.resolve_all`` of the listed devices; a device whose rules
    cannot be resolved is left out and its cause logged once."""
    from services.profiles import store as profiles

    failed: set[str] = set()
    try:
        resolved = await profiles.resolve_all(db, ids)
    except Exception:  # noqa: BLE001 - find the device(s) one by one
        resolved = {}
        for cid in ids:
            try:
                resolved.update(await profiles.resolve_all(db, [cid]))
            except Exception as e:  # noqa: BLE001 - this device is unresolved
                failed.add(cid)
                _log_unresolved(cid, f"{type(e).__name__}: {str(e)[:120]}")
    for cid in ids:
        if cid not in resolved and cid not in failed:
            _log_unresolved(cid, "its profile cannot be read")
    return resolved


def _client_response(
    c: dict,
    connected_ids,
    remote: dict[str, bool],
    access_now: dict[str, dict] | None = None,
    rules: dict[str, dict] | None = None,
    stale: set[str] | frozenset = frozenset(),
) -> PairedClientResponse:
    from services import location
    from services.profiles import resolver as res
    from services.profiles import store as profiles

    effective = (rules or {}).get(c["id"])
    location_rule = None
    if effective and effective["location"]["source"] != "not_applicable":
        location_rule = location.rule_value(effective)

    guest = c.get("access") == "guest"
    return PairedClientResponse(
        id=c["id"],
        name=c["name"],
        is_active=bool(c["is_active"]),
        access="guest" if guest else "member",
        expires_at=c.get("expires_at") if guest else None,
        set_up_by=profiles.set_up_by(c),
        created_at=c["created_at"],
        last_seen=c.get("last_seen"),
        # The device holds an HA key (resolver.holds_key; on the hub path
        # without remote access the hub keeps it for its own calls), or a
        # lost one whose rotation the hub has not accepted yet.
        has_ha_token=bool(c.get("ha_token_encrypted"))
        and (effective is None or res.holds_key(effective) or c["id"] in stale),
        control_path=_control_path(effective),
        ha_user_id=c.get("ha_user_id"),
        ha_token_status=c.get("ha_token_status") or HA_TOKEN_PENDING,
        ha_token_pending_since=c.get("ha_token_pending_since"),
        owner_label=c.get("owner_label"),
        profile_id=c["profile_id"],
        profile_name=c["profile_name"],
        profile_kind=c.get("profile_kind") or "person",
        child_protection=bool(c.get("child_protection")),
        own_area_id=c.get("own_area_id"),
        override_count=int(c.get("override_count") or 0),
        temporary_override_count=int(c.get("temporary_override_count") or 0),
        has_remote_pin=profiles.device_has_pin(c),
        remote_allowed=remote.get(c["id"]) if effective is not None else None,
        location_rule=location_rule,
        location_required=bool(effective) and location.required(effective),
        **location.status_of(c["id"]),
        is_connected=c["id"] in connected_ids,
        pending_deletion=bool(c.get("pending_deletion_at")),
        pending_deletion_at=c.get("pending_deletion_at"),
        platform=c.get("platform") if c.get("platform") in protocol.PLATFORMS else None,
        app_version=c.get("app_version"),
        app_build=c.get("app_build"),
        device_model=c.get("device_model"),
        protocol_version=c.get("protocol_version"),
        update_required=protocol.update_required(c),
        access_now=AccessNow(**((access_now or {}).get(c["id"]) or {"open": True})),
    )


async def client_views(
    db: Database, where: str = "", params: tuple = ()
) -> list[PairedClientResponse]:

    rows = await db.fetchall(
        f"SELECT {_CLIENT_COLUMNS} {_CLIENT_FROM} {where} ORDER BY c.created_at DESC",
        params,
    )
    from services import access

    ids = [r["id"] for r in rows]
    resolved = await _resolve_views(db, ids)
    rules = {cid: item["rules"] for cid, item in resolved.items()}
    remote = {cid: r["remote_access"]["value"] is True for cid, r in rules.items()}
    states = await access.access_map(db, ids, resolved)
    connected_ids = api_ws.get_connected_client_ids()
    rows = [_followed_profile(c, resolved.get(c["id"])) for c in rows]
    stale = await device_provisioning.stale_keys(db, ids)
    return [
        _client_response(c, connected_ids, remote, states, rules, stale) for c in rows
    ]


def _followed_profile(c: dict, item: dict | None) -> dict:
    """A child's watch shows the profile it follows: its phone's (kids
    badge, profile name), contracts protocol.md "A child's watch"."""
    from services.profiles import store as profiles

    if not item or not profiles.is_child_watch(item["client"]):
        return c
    prof = item["profile"]
    return {
        **c,
        "profile_id": prof["id"],
        "profile_name": prof["name"],
        "profile_kind": prof.get("kind") or "person",
        "child_protection": bool(prof["child_protection"]),
    }


async def client_view(db: Database, client_id: str) -> PairedClientResponse:
    views = await client_views(db, "WHERE c.id = ?", (client_id,))
    if not views:
        raise HTTPException(status_code=404, detail="Client not found")
    return views[0]


async def list_paired_clients(request: Request):
    """List all paired clients with live connection status. Admin-only.

    Hidden tombstones are background-only markers (kept so a returning app
    is still reset on reconnect): they are not listed.
    """
    require_admin(request)
    return await client_views(request.app.state.db, "WHERE c.pending_hidden_at IS NULL")


async def get_client(request: Request, client_id: str):
    """Get a single paired client by ID. Admin-only."""
    require_admin(request)
    return await client_view(request.app.state.db, client_id)


async def get_client_activity(
    request: Request,
    client_id: str,
    limit: int = Query(
        client_activity.MAX_EVENTS_PER_CLIENT,
        ge=1,
        le=client_activity.MAX_EVENTS_PER_CLIENT,
    ),
):
    """Activity feed of one device, newest first. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db
    exists = await db.fetchone(
        "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Client not found")
    return DeviceActivityResponse(
        client_id=client_id,
        max_events=client_activity.MAX_EVENTS_PER_CLIENT,
        events=await client_activity.list_events(db, client_id, limit),
    )


async def update_client(request: Request, client_id: str, data: ClientUpdate):
    """Update a paired client's name or owner label. Admin-only.

    A change is reported to the integration (update_client_identity), which
    renames the device's HA user.
    """
    require_admin(request)
    db: Database = request.app.state.db

    existing = await db.fetchone(
        "SELECT id, name, owner_label FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")

    identity: dict = {}
    if data.name is not None and data.name != existing["name"]:
        identity["name"] = data.name
    if data.owner_label is not None:
        owner_label = data.owner_label or None
        if owner_label != existing.get("owner_label"):
            identity["owner_label"] = owner_label

    if identity:
        await db.set_client_identity(client_id, **identity)
        logger.info(
            "Updated paired client %s (%s)", client_id, ", ".join(sorted(identity))
        )
        await device_provisioning.send_identity_update(db, client_id)
        if "name" in identity:
            from services.ha_sensors import remove_renamed_client_sensor

            try:
                await remove_renamed_client_sensor(
                    db,
                    getattr(request.app.state, "ha_client", None),
                    existing["name"],
                    client_id,
                )
            except Exception as e:  # noqa: BLE001 - the next full sync cleans up
                logger.warning("Notification sensor rename failed: %s", e)

    return await client_view(db, client_id)


async def get_area_visibility(request: Request, client_id: str, area_id: str):
    """Whether an area is assigned/visible to a client. Admin-only.

    Returns ``{"visible": bool}`` using the canonical assignment rule:
    area has no tags OR client has no tags OR their tag sets overlap.

    This is a VIEW/assignment check (does the area appear in the client's
    area list). It does NOT reflect control authorization, which is enforced
    per-entity at call_service time. A deep-linked notification area can be
    opened by any client even when ``visible`` is False; this endpoint lets the
    Web-UI surface the assignment state (e.g. badge/warning) for an admin.
    """
    require_admin(request)
    db: Database = request.app.state.db

    client = await db.fetchone(
        "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    visible = await api_ws._area_visible_for_client(db, client_id, area_id)
    if visible is None:
        raise HTTPException(status_code=404, detail="Area not found")

    return {"visible": visible}


async def revoke_client(request: Request, client_id: str):
    """Revoke (block) a paired client at once. Admin-only.

    For a lost or stolen device: the row stays as "revoked" (the app gets
    ``auth_error`` reason ``revoked``), live connections close and the
    device's HA user is removed now. Removing the row afterwards is a normal
    delete (``DELETE …/permanent``, then the tombstone).
    """
    require_admin(request)
    db: Database = request.app.state.db

    existing = await db.fetchone(
        "SELECT id, name, (access = 'guest') AS is_guest FROM paired_clients "
        "WHERE id = ?",
        (client_id,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")

    await _before_removal(db, getattr(request.app.state, "ha_client", None), client_id)
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET is_active = 0 WHERE id = ?", (client_id,)
        )
    from services import event_log

    actor = getattr(request.state, "actor", None) or "admin"
    await event_log.device_removed(
        db,
        client_id,
        how="revoked",
        actor=actor,
        set_up=await _set_up_devices(db, client_id),
    )

    await _end_invites_of(client_id)

    # A revoked client must not keep reading over its open WS:
    # close it now with the same codes as a kick.
    is_guest = bool(existing.get("is_guest"))
    await api_ws.disconnect_client(
        client_id,
        code=4003 if is_guest else 4001,
        reason="Guest session revoked" if is_guest else "Client revoked",
    )

    # Then the integration removes the device's HA user.
    await device_provisioning.send_deprovision(
        client_id, device_provisioning.DEPROVISION_REVOKED
    )
    await _remove_watches_of(
        db,
        getattr(request.app.state, "ha_client", None),
        client_id,
        actor,
        existing.get("name"),
    )
    if is_guest:
        await _sync_guests(db, getattr(request.app.state, "ha_client", None))

    logger.info("Revoked paired client: %s", client_id)


# What happens to the HA user of a device deleted while offline.
HA_USER_REMOVAL_ON_RECONNECT = "on_reconnect"
HA_USER_REMOVAL_IMMEDIATE = "immediate"


def recommended_ha_user_removal(remote_allowed: bool | None) -> str:
    """Default for an offline device: remote access → at once, home only → hold.

    A device with remote access could keep using its token from anywhere
    until it returns; a home-network-only device is harmless outside and is
    better reset cleanly when it comes back. Unknown (rules that cannot be
    resolved) → at once: the stronger removal.
    """
    if remote_allowed is False:
        return HA_USER_REMOVAL_ON_RECONNECT
    return HA_USER_REMOVAL_IMMEDIATE


async def delete_client(
    request: Request,
    client_id: str,
    ha_user_removal: Literal["on_reconnect", "immediate"] | None = Query(
        None,
        description="Only for a device that is offline when deleted: "
        "'on_reconnect' keeps its HA user until the app confirms its reset "
        "on return (max. 24 h, clean return to setup); 'immediate' removes "
        "the HA user now (the tombstone stays and is listed as pending "
        "deletion; the app is still reset on return). Default: 'immediate' "
        "for devices with remote access, "
        "'on_reconnect' for home-network-only devices. Ignored while the "
        "device is connected (reset-ack window).",
    ),
):
    """Delete a paired client and force its app to reset. Admin-only.

    Returns instantly. Order:
      1. Mark the row pending-deletion (``pending_deletion_at`` set,
         ``is_active=0``). The row is NOT hard-deleted here.
      2. Signal any LIVE connection of this client to reset itself
         (``client_removed`` over its live WS). The app wipes locally and
         replies with ``client_reset_ack`` ("wiped"), which purges the
         tombstone; a connection that never acks is force-closed (4001) after
         a short grace period WITHOUT purging. We do NOT close the socket or
         delete the row here.
      3. The device's HA user is held (``ha_user_holds`` in the status
         snapshot) until the app confirmed the reset or a grace period ran
         out, then the integration removes it (``deprovision_client``).
         Removing the user first made HA close the app's session before it
         knew it was removed, or made a returning offline app fail
         its first HA login. Online the grace period is the reset-ack
         window (``RESET_ACK_GRACE_SECONDS``); an offline device keeps its
         user until its app comes back (the hub replays ``client_removed``)
         or at most ``DELETE_HOLD_SECONDS``, unless the admin chose
         ``ha_user_removal=immediate`` (default for devices with remote
         access). Revoke removes it at once.
      4. Return 204.

    204 means the deletion took effect: the device can no longer control
    anything, a connected app is told to reset, and its HA user goes as
    described above. It does not mean the row is gone: the row stays
    as a tombstone and the list shows it with ``pending_deletion: true``
    ("Waiting for device") until the app confirms its reset. A connected
    device confirms within moments, so it leaves the list at once; an offline
    one stays listed until it returns, also with ``ha_user_removal=immediate``
    (that choice only decides when the HA user goes). This is intended: only
    the tombstone lets a returning app wipe itself.

    The signed tombstone (client_id + secret_hash) persists INDEFINITELY until
    the device's authenticated LAN wipe-ack arrives (reconnect replays the
    ``client_removed`` handshake), so the compliance endpoint keeps returning a
    signed "deleted" for it. To take it off the list without losing that,
    POST /clients/{id}/dismiss-pending hides it; an admin can force-purge via
    DELETE /pairing/clients/{id}/tombstone.
    """
    require_admin(request)
    db: Database = request.app.state.db

    existing = await db.fetchone(
        "SELECT id, name, access FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")

    from services import event_log

    actor = getattr(request.state, "actor", None) or "admin"
    await event_log.device_removed(
        db,
        client_id,
        how="deleted",
        actor=actor,
        set_up=await _set_up_devices(db, client_id),
    )

    await _before_removal(db, getattr(request.app.state, "ha_client", None), client_id)

    # 1) Mark pending-deletion (do NOT hard-delete yet). is_active=0 so the
    # client drops out of active counts and WS auth rejects it on reconnect.
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )

    await _end_invites_of(client_id)
    # Its watches go at once: they cannot reset themselves (no hub socket).
    await _remove_watches_of(
        db,
        getattr(request.app.state, "ha_client", None),
        client_id,
        actor,
        existing.get("name"),
    )
    if existing.get("access") == "guest":
        await _sync_guests(db, getattr(request.app.state, "ha_client", None))

    # 2) Signal any live WS to reset (client_removed). This neither closes the
    # socket nor deletes the row — the app's client_reset_ack does the removal.
    signaled = 0
    try:
        signaled = await api_ws.signal_client_removed(client_id)
    except Exception as e:  # noqa: BLE001 - best-effort, never crash request
        logger.warning(
            "Signal client_removed on delete failed for %s: %s", client_id, e
        )

    # 3) Remove the HA user only after the app reset (or the grace period).
    # Offline devices are released by the ack on their return or by the
    # retry loop once DELETE_HOLD_SECONDS ran out (survives restarts).
    removal = None
    if signaled:
        device_provisioning.spawn(_deprovision_after_reset(db, client_id))
    else:
        from services.profiles import store as profiles

        # Unknown (rules that cannot be resolved): the stronger removal.
        try:
            remote = (await profiles.remote_allowed_map(db, [client_id])).get(client_id)
        except Exception as e:  # noqa: BLE001 - unknown, removed at once
            _log_unresolved(client_id, f"{type(e).__name__}: {str(e)[:120]}")
            remote = None
        removal = ha_user_removal or recommended_ha_user_removal(remote)
        if removal == HA_USER_REMOVAL_IMMEDIATE:
            await device_provisioning.release_user(db, client_id)

    logger.info(
        "Marked paired client pending-deletion: %s (signaled %d live connections, "
        "offline HA user removal: %s)",
        client_id,
        signaled,
        removal or "after reset ack",
    )


async def _deprovision_after_reset(db: Database, client_id: str) -> None:
    await api_ws.wait_until_disconnected(client_id, api_ws.RESET_ACK_GRACE_SECONDS + 1)
    # No-op when the ack already purged the row (the purge released it).
    await device_provisioning.release_user(db, client_id)


async def dismiss_pending_client(request: Request, client_id: str):
    """Hide a pending-deletion tombstone from the clients list. Admin-only.

    Only valid for a client that is currently pending-deletion
    (``pending_deletion_at`` set). Sets ``pending_hidden_at`` so the row is
    excluded from the clients list, but does NOT delete the row: the tombstone
    persists as a background marker so a returning app is still rejected (4001)
    on reconnect and only then removed.
    """
    require_admin(request)
    db: Database = request.app.state.db

    existing = await db.fetchone(
        "SELECT id, name, pending_deletion_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")
    if not existing.get("pending_deletion_at"):
        raise HTTPException(status_code=400, detail="Client is not pending deletion")

    await db.set_pending_hidden(client_id)

    logger.info("Dismissed pending-deletion tombstone from list: %s", client_id)
    return {"ok": True}


async def purge_client_tombstone(request: Request, client_id: str):
    """Force-purge a pending-deletion tombstone. Admin-only.

    Escape hatch for a device that never returns: hard-deletes the tombstone
    row (client_id + secret_hash), after which the compliance endpoint stops
    signing "deleted" for it and serves the neutral "unknown". Only valid for
    a client that is pending-deletion (400 otherwise). The normal purge path
    stays the device's authenticated LAN wipe-ack (``client_reset_ack``).
    """
    require_admin(request)
    db: Database = request.app.state.db

    existing = await db.fetchone(
        "SELECT id, name, pending_deletion_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Client not found")
    if not existing.get("pending_deletion_at"):
        raise HTTPException(status_code=400, detail="Client is not pending deletion")

    async with db.transaction():
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (client_id,))
    # Nothing waits for this device any more: its HA user goes too.
    await device_provisioning.send_deprovision(
        client_id, device_provisioning.DEPROVISION_DELETED
    )
    await _remove_watches_of(
        db,
        getattr(request.app.state, "ha_client", None),
        client_id,
        getattr(request.state, "actor", None) or "admin",
        existing.get("name"),
    )

    logger.info("Force-purged pending-deletion tombstone: %s", client_id)


async def kick_client(request: Request, client_id: str):
    """Immediately disconnect and permanently delete a client. Admin-only.

    Closes any active WebSocket connections (close code 4003 for guests,
    4001 for regular clients) and removes the client from the database.
    Works for both guest and regular clients.
    """
    require_admin(request)
    db: Database = request.app.state.db
    ha_client = getattr(request.app.state, "ha_client", None)
    if not await end_client_access(db, ha_client, client_id):
        raise HTTPException(status_code=404, detail="Client not found")


# Why a client was removed at once (client_kicked.reason).
REMOVAL_KICKED = "kicked"
REMOVAL_LEFT = "left"
# Log only (detail.how): a watch removed because its phone was removed.
REMOVAL_WITH_PHONE = "with_phone"


async def client_leave(request: Request):
    """The device signs itself off with its own secret.

    REST twin of WS ``client_leave`` for a device without a hub socket:
    a watch, directly over the pinned TLS port or
    through its phone.
    Bearer = the device's client secret (checked by the auth middleware, 401
    otherwise); ``client_id`` must be that device (422). Removed like
    ``client_leave``: no tombstone, HA user removed by the integration,
    ``client_kicked {reason: left}``, audit ``client_left``.
    """
    from api.pairing.pin import _get_client_ip
    from services.audit_log import log_admin_action

    client_id = getattr(request.state, "client_id", None)
    if getattr(request.state, "auth_type", None) != "client" or not client_id:
        raise HTTPException(status_code=403, detail="Device credential required")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - any parse error is a bad request
        body = None
    if not isinstance(body, dict) or body.get("client_id") != client_id:
        raise HTTPException(status_code=422, detail="client_id must be this device")
    db: Database = request.app.state.db
    row = await db.fetchone(
        "SELECT name FROM paired_clients WHERE id = ?", (client_id,)
    )
    ha_client = getattr(request.app.state, "ha_client", None)
    if not await end_client_access(db, ha_client, client_id, reason=REMOVAL_LEFT):
        raise HTTPException(status_code=404, detail="Client not found")
    name = (row or {}).get("name") or client_id
    log_admin_action("client_left", f"{name} ({client_id})", _get_client_ip(request))
    logger.info("Client %s signed itself off (POST /api/client/leave)", client_id)
    return {"type": "client_leave_ok"}


async def end_client_access(
    db: Database,
    ha_client,
    client_id: str,
    reason: str = REMOVAL_KICKED,
    actor: str | None = None,
    with_device: dict | None = None,
) -> bool:
    """Disconnect and delete a client now (admin kick, the member device that
    set it up removing it, and the device signing itself off with
    ``client_leave``). False when the client does not exist.

    ``with_device`` ({client_id, name}): a watch that goes because its phone
    was removed; the log says so (``how`` with_phone, ``detail.with_device``)."""
    client = await db.fetchone(
        "SELECT id, name, (access = 'guest') AS is_guest FROM paired_clients "
        "WHERE id = ?",
        (client_id,),
    )
    if not client:
        return False
    await _end_invites_of(client_id)

    from services import event_log

    log_context = await event_log.client_context(db, client_id)
    set_up = await _set_up_devices(db, client_id)
    is_guest = bool(client.get("is_guest"))
    close_code = 4003 if is_guest else 4001
    close_reason = "Guest session revoked" if is_guest else "Client revoked"

    await _before_removal(db, ha_client, client_id)
    # Delete from DB first, then close: a reconnect in between must not
    # authenticate against the still active row. Then the
    # integration removes the device's HA user.
    async with db.transaction():
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (client_id,))
    closed = await api_ws.disconnect_client(
        client_id, code=close_code, reason=close_reason
    )
    await device_provisioning.send_deprovision(
        client_id, device_provisioning.DEPROVISION_DELETED
    )

    await event_log.device_removed(
        db,
        client_id,
        how=REMOVAL_WITH_PHONE if with_device else reason,
        actor=actor,
        context=log_context,
        set_up=set_up,
        with_device=with_device,
    )
    await _remove_watches_of(db, ha_client, client_id, actor, client["name"])
    logger.info(
        "Removed client '%s' (id=%s, guest=%s, reason=%s, closed %d connections)",
        client["name"],
        client_id,
        is_guest,
        reason,
        closed,
    )

    # Broadcast removal to admin UIs
    await api_ws.broadcast(
        {
            "type": "client_kicked",
            "data": {"client_id": client_id, "is_guest": is_guest, "reason": reason},
        },
        audience=EventAudience.ADMIN,
    )

    # Sync guests sensor if it was a guest
    if is_guest:
        try:
            from services.ha_sensors import sync_guests_sensor

            await sync_guests_sensor(db, ha_client)
        except Exception as e:
            logger.warning("Failed to sync guests sensor after kick: %s", e)
    return True


async def delete_all_clients(request: Request):
    """Delete ALL paired clients (both regular and guest). Admin-only.

    Disconnects all active WebSocket connections and removes all clients from DB.
    This is a destructive operation — all pairing data is permanently lost.
    """
    require_admin(request)
    db: Database = request.app.state.db

    # Get all clients for logging
    clients = await db.fetchall(
        "SELECT id, name, (access = 'guest') AS is_guest FROM paired_clients"
    )
    if not clients:
        return {"deleted": 0}

    ha_client = getattr(request.app.state, "ha_client", None)
    for c in clients:
        await _end_invites_of(c["id"])
        await _before_removal(db, ha_client, c["id"])

    # Delete all from DB first, then close every connection, then
    # one deprovision_client per device.
    async with db.transaction():
        await db.execute("DELETE FROM paired_clients")
    for c in clients:
        is_guest = bool(c.get("is_guest"))
        close_code = 4003 if is_guest else 4001
        reason = "All sessions cleared"
        try:
            await api_ws.disconnect_client(c["id"], code=close_code, reason=reason)
        except Exception:
            pass
    for c in clients:
        await device_provisioning.send_deprovision(
            c["id"], device_provisioning.DEPROVISION_CLEARED
        )

    logger.info("Deleted all %d paired clients", len(clients))

    # Broadcast to admin UIs
    await api_ws.broadcast(
        {"type": "clients_cleared", "data": {"count": len(clients)}},
        audience=EventAudience.ADMIN,
    )

    # Sync guests sensor
    try:
        from services.ha_sensors import sync_guests_sensor

        ha_client = request.app.state.ha_client
        await sync_guests_sensor(db, ha_client)
    except Exception as e:
        logger.warning("Failed to sync guests sensor after clear all: %s", e)

    return {"deleted": len(clients)}


async def _set_up_devices(db: Database, client_id: str) -> list[dict]:
    """Devices ``client_id`` set up that stay paired when it goes (not its
    watches, which go with it). Their ``set_up_by`` keeps naming the removed
    device; the log entry of the removal lists them for the admin."""
    from services import watch_access

    watches = set(await watch_access.carried_by(db, client_id))
    rows = await db.fetchall(
        "SELECT id, name FROM paired_clients WHERE set_up_by_client_id = ? "
        "AND id != ? AND pending_deletion_at IS NULL ORDER BY name",
        (client_id, client_id),
    )
    out = [
        {"client_id": r["id"], "name": r["name"]}
        for r in rows
        if r["id"] not in watches
    ]
    if out:
        logger.warning(
            "Removed device %s had set up %d device(s) that stay paired: %s",
            client_id,
            len(out),
            ", ".join(d["client_id"] for d in out),
        )
    return out


async def _before_removal(db: Database, ha_client, client_id: str) -> None:
    """While the device is still paired: its open requests end as cancelled
    (its family managers are still found), the releases it gave as a
    babysitter end and its notification sensor goes."""
    from services import access
    from services.family import service as family
    from services.ha_sensors import delete_client_notification_sensor

    try:
        await family.cancel_requests_of(db, ha_client, client_id)
        await access.end_releases_of_removed(db, client_id)
    except Exception as e:  # noqa: BLE001 - the removal goes on, the planner retries
        logger.warning("Requests/releases of %s not ended: %s", client_id, e)
    row = await db.fetchone(
        "SELECT name FROM paired_clients WHERE id = ?", (client_id,)
    )
    if row and row.get("name"):
        # Its notification texts must not stay readable in HA.
        await delete_client_notification_sensor(ha_client, row["name"], client_id)


async def _sync_guests(db: Database, ha_client) -> None:
    """The guests sensor leaves out deleted and revoked guests."""
    if ha_client is None:
        return
    try:
        from services.ha_sensors import sync_guests_sensor

        await sync_guests_sensor(db, ha_client)
    except Exception as e:  # noqa: BLE001 - the next sync retries
        logger.warning("Failed to sync guests sensor after removal: %s", e)


async def _remove_watches_of(
    db: Database, ha_client, client_id: str, actor: str | None, name: str | None
) -> None:
    """The watches whose access this device carried are removed with it
    (their HA users and tokens go through the normal deprovisioning).

    Also when the remover did not set the watch up (a member removing a
    device it set up, which then had paired its own watch): without its phone
    a watch gets no access any more (contracts protocol.md, watches), and the
    contract's set_up_device_remove removes "like the admin's Remove". The log
    names the phone it went with."""
    from services import watch_access

    phone = {"client_id": client_id, "name": name or client_id}
    for watch_id in await watch_access.carried_by(db, client_id):
        try:
            await end_client_access(
                db,
                ha_client,
                watch_id,
                reason=REMOVAL_KICKED,
                actor=actor,
                with_device=phone,
            )
        except Exception as e:  # noqa: BLE001 - the admin can still remove it
            logger.warning("Watch %s of %s not removed: %s", watch_id, client_id, e)


async def _end_invites_of(client_id: str) -> None:
    """The open guest invitation of a device ends when it loses the right or
    is removed."""
    from api.ws import guest_invites as guest_invites_ws

    await guest_invites_ws.end_invites_of(client_id)


async def set_access_switch(request: Request, client_id: str, data: AccessSwitchUpdate):
    """Turn a device's access switch on or off. Admin-only.

    The same switch Home Assistant shows as ``switch.<ns>_<name>_access``.
    409 ``no_access_switch`` when the device has none.
    """
    from services import access

    require_admin(request)
    db: Database = request.app.state.db
    try:
        await access.set_switch(
            db, client_id, data.on, by="admin", actor=_admin_name(request)
        )
    except access.SwitchError as e:
        raise HTTPException(status_code=e.status, detail=e.code) from e
    return await client_view(db, client_id)


def _admin_name(request: Request) -> str:
    return getattr(request.state, "actor", None) or "admin"
