"""Integration API endpoints for the Halumar integration.

These endpoints provide structured device/entity data that the HA custom
integration (`custom_components/halumar/`) uses to create real HA devices
and entities with proper device registry grouping.

Two device types are exposed:

1. **Halumar Hub** (one device):
   - Sensor: area count, connected clients count, version
   - Button: start pairing, sync sensors
   - Per area: switch (active/inactive) + sensor (entity count + details)

2. **Halumar Client** (one per paired client):
   - Binary sensor: connected/disconnected
   - Sensors: current area, device name, app version, battery, connection type
   - Device tracker: GPS location

The HA integration polls GET /api/integration/status periodically via its
DataUpdateCoordinator, and can also connect via WebSocket for real-time
push updates (client_status_updated, client_connected, client_disconnected,
area_updated broadcasts).

Auth: every endpoint here requires the DEDICATED integration token
(require_integration) — admin tokens/sessions/Ingress are rejected. The
token is rotated on every hub start under the Supervisor and announced
via the discovery API (services/discovery.py); the middleware scopes it to
/api/integration/* and records connection telemetry per request
(services/integration_status.py).

Privacy: the status payload contains device names, owners and current
areas, but never tokens or notification texts — notifications appear as
ids+counts per client only, guests as
client_id/is_connected/expires_at only, and the pairing block carries no PIN
(the QR endpoint serves the actual pairing surface).
"""

import json
import logging
import struct
import zlib
from contextlib import nullcontext
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from api import authz
from api import pairing as api_pairing
from api import ws as api_ws
from api.auth import require_integration
from api.notifications import deliver_notification_request
from api.ws import EventAudience
from brand import BRAND_NAME
from models.database import Database
from namespace import HA_DEVICE_IDENTIFIER_HUB, ha_device_identifier_client
from models.schemas import (
    AckNotificationsRequest,
    ClientTagsRequest,
    ClientTokenDelivery,
    ExtendGuestRequest,
    HAUrlReport,
    HAUrlReportResponse,
    IntegrationEventsRequest,
    IntegrationEventsResponse,
    IntegrationNotifyRequest,
    IntegrationPanelCommandRequest,
    KidsCheckRequest,
    KidsCheckResponse,
    NavigateToAreaRequest,
    NotificationHandoverRequest,
    PairingGenerateRequest,
    PanelCommandResponse,
    ShowEntityRequest,
)
from services import client_activity, device_provisioning, enforcement, mdns, panel
from services import pairing_qr
from services import integration_events
from services.profiles import resolver as profile_resolver
from services.expiry import is_expired
from services.ha_sensors import SLUG_MAX_LEN, _area_slug, _slugify
from version import VERSION

logger = logging.getLogger("hub.api.integration")
router = APIRouter(tags=["integration"])

# Sentinel area_id for navigate_to_area: send the client to its HOME screen
# instead of a real area (no area-existence check applies).
HOME_SENTINEL = "__home__"


def _image_src(image_path: str | None) -> str | None:
    """Convert filesystem path to servable URL."""
    if not image_path:
        return None
    import os

    return f"/images/{os.path.basename(image_path)}"


@router.get("/integration/status")
async def integration_status(request: Request):
    """Full status snapshot for the Halumar integration.

    Contains device names, owners and current areas; never tokens or
    notification texts.

    Returns everything the integration needs in a single call:
    - Hub info (version, area count, connected clients)
    - All areas with their status
    - All paired clients with their current status + connection state;
      notifications appear as ids+counts only (never title/message/action);
      platform and set_up_by tell the integration which device shows a
      watch's notifications while the hub is down
    - Guests reduced to client_id/is_connected/expires_at
    - pairing_active (+ non-secret window metadata; the PIN itself is NOT
      in this payload — the pairing-qr endpoint carries it)

    The HA integration's DataUpdateCoordinator calls this endpoint
    periodically (e.g. every 30s) to update all entities at once.
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db

    connected_ids = api_ws.get_connected_client_ids()
    connected_count = api_ws.get_connected_count()

    # ── Hub info ─────────────────────────────────────────────────────────
    health = {
        "version": VERSION,
        # Stable id of this hub (Bonjour iid, onboarding hub_id): the
        # integration refuses to reconcile against a hub with another id.
        "instance_id": mdns.load_instance_id(mdns.INSTANCE_ID_PATH),
        "connected_clients": connected_count,
        # Capability flag: this hub accepts the integration's
        # header-authed WebSocket push session on GET /api/ws. The integration
        # gates its WS connect on this, so an older hub is never dialed.
        "ws_push": True,
    }

    # ── Areas ────────────────────────────────────────────────────────────
    areas_raw = await db.fetchall(
        "SELECT a.*, COUNT(ae.id) as entity_count "
        "FROM areas a LEFT JOIN area_entities ae ON a.id = ae.area_id "
        "GROUP BY a.id ORDER BY a.sort_order, a.name"
    )

    # Batch-load all tags in one query (avoids N+1 per area)
    all_tags = await db.fetchall("SELECT area_id, tag FROM area_tags")
    tags_by_area: dict[str, list[str]] = {}
    for row in all_tags:
        tags_by_area.setdefault(row["area_id"], []).append(row["tag"])

    # Batch-load section counts per area
    section_counts_raw = await db.fetchall(
        "SELECT area_id, COUNT(*) as cnt FROM area_sections GROUP BY area_id"
    )
    section_counts: dict[str, int] = {
        r["area_id"]: r["cnt"] for r in section_counts_raw
    }

    # Build area name lookup for client → area resolution (avoids N+1 per client)
    area_names: dict[str, str] = {}
    areas = []
    for area in areas_raw:
        slug = _area_slug(area["name"], area["id"])
        area_names[area["id"]] = area["name"]
        areas.append(
            {
                "id": area["id"],
                "name": area["name"],
                "slug": slug,
                "icon": area.get("icon"),
                "description": area.get("description"),
                "color": area.get("color"),
                "grid_cols": area.get("grid_cols", 4),
                "sort_order": area.get("sort_order", 0),
                "is_active": bool(area.get("is_active", 1)),
                "control_mode": area.get("control_mode", "full"),
                "entity_count": area["entity_count"],
                "section_count": section_counts.get(area["id"], 0),
                "card_image_src": _image_src(area.get("card_image_path")),
                "card_image_url": area.get("card_image_url"),
                "bg_light_src": _image_src(area.get("bg_light_path")),
                "bg_light_url": area.get("bg_light_url"),
                "bg_dark_src": _image_src(area.get("bg_dark_path")),
                "bg_dark_url": area.get("bg_dark_url"),
                "tags": tags_by_area.get(area["id"], []),
            }
        )

    # ── Clients ──────────────────────────────────────────────────────────
    # secret_hash is loaded ONLY to sign the compliance blob
    # (services/compliance.py) — it never leaves this handler.
    clients_raw = await db.fetchall(
        "SELECT id, name, secret_hash, is_active, created_at, last_seen, "
        "ha_token_encrypted, ha_token_status, ha_token_minted_at, ha_user_id, "
        "owner_label, device_name, app_version, platform, "
        "set_up_by_client_id, set_up_by_name, set_up_method, "
        "battery_level, connection_type, current_area_id, "
        "(access = 'guest') AS is_guest, expires_at, pending_deletion_at, "
        "pending_hidden_at FROM paired_clients ORDER BY created_at DESC"
    )
    # Remote access and areas are effective profile rules.
    from services.profiles import store as profiles

    resolved = await profiles.resolve_all(db)
    # Devices whose app may still know a key they lost (rotation not
    # accepted yet).
    stale = await device_provisioning.stale_keys(db)

    from services.compliance import build_compliance

    compliance_enabled = (
        await db.get_setting("compliance_endpoint_enabled")
    ) != "false"

    # Signed tombstones: EVERY pending-deletion row (hidden ones included) so
    # the integration can keep serving a verifiable "deleted" until the device
    # returns to the LAN, wipes, and acks (which purges the row). PII-free:
    # client_id + the opaque compliance blob only.
    tombstones = []
    if compliance_enabled:
        tombstones = [
            {"client_id": c["id"], "compliance": build_compliance(c)}
            for c in clients_raw
            if c.get("pending_deletion_at")
        ]

    from services import access as access_svc

    tz_name = access_svc.time_zone()
    clients = []
    for c in clients_raw:
        # Hidden tombstones are background-only markers (kept so a returning
        # app is still reset on reconnect) — exclude them from the list.
        if c.get("pending_hidden_at"):
            continue

        resolved_client = resolved.get(c["id"]) or {}
        rules = resolved_client.get("rules") or {}
        panel_status = panel.status_of(c["id"])
        tags = authz.scope_from_rule((rules.get("areas") or {}).get("value")).as_tags()

        # Resolve current area name from preloaded dict (no extra query)
        area_name = (
            area_names.get(c["current_area_id"]) if c.get("current_area_id") else None
        )

        slug = _slugify(c["name"], max_len=SLUG_MAX_LEN)
        is_connected = c["id"] in connected_ids

        entry = {
            "id": c["id"],
            "name": c["name"],
            "slug": slug,
            "is_active": bool(c["is_active"]),
            "is_connected": is_connected,
            "tags": tags,
            # Key on the device (resolver.holds_key: on the hub path only
            # with remote access), or a lost one whose rotation the hub has
            # not accepted yet (it still works).
            # Rules that cannot be resolved: whatever token exists counts.
            "has_ha_token": bool(c.get("ha_token_encrypted"))
            and (not rules or profile_resolver.holds_key(rules) or c["id"] in stale),
            "created_at": c["created_at"],
            "last_seen": c.get("last_seen"),
            "device_name": c.get("device_name"),
            "app_version": c.get("app_version"),
            # A watch shows the notifications of the phone that set it up:
            # while the hub is down the integration writes them there.
            "platform": c.get("platform"),
            "set_up_by": profiles.set_up_by(c),
            "battery_level": c.get("battery_level"),
            "connection_type": c.get("connection_type")
            if is_connected
            else "disconnected",
            "current_area_id": c.get("current_area_id"),
            "current_area_name": area_name,
            "is_guest": bool(c.get("is_guest", 0)),
            "expires_at": c.get("expires_at"),
            # Client was deleted and is awaiting its app's reset. Shown in
            # the list (flagged) but excluded from active/guest counts.
            "pending_deletion": bool(c.get("pending_deletion_at")),
            "pending_deletion_at": c.get("pending_deletion_at"),
            # One HA user per device: the desired state the
            # integration reconciles its device users against.
            "ha_user_id": c.get("ha_user_id"),
            "ha_token_status": c.get("ha_token_status"),
            "owner_label": c.get("owner_label"),
            "remote_allowed": (rules.get("remote_access") or {}).get("value") is True,
            # Wall panel as a Home Assistant device.
            "battery_state": panel_status["battery_state"],
            "low_power": panel_status["low_power"],
            "panel": panel_status["panel"],
            "panel_ha": panel.panel_ha(
                {**(resolved_client.get("client") or {}), **c}, rules
            )
            if resolved_client
            else None,
            # Access hours and access switch right now: the integration
            # offers switch.<ns>_<name>_access when switch is not null.
            "access": access_svc.state_of(
                resolved_client["client"],
                resolved_client["profile"],
                rules,
                tz_name=tz_name,
            )
            if resolved_client and not c.get("pending_deletion_at")
            else None,
        }
        # Signed compliance blob. Omitted entirely when the admin
        # disabled the compliance endpoint toggle.
        if compliance_enabled:
            entry["compliance"] = build_compliance(c)
        clients.append(entry)

    # ── Pairing status (no PIN — this payload must stay secret-free) ─────
    import time as _time

    active = api_pairing.get_active_pin()
    pairing_active = False
    pairing_remaining = 0
    pairing_is_guest = False
    pairing_guest_expires_at = None

    if active:
        _pin, pin_data = active
        pairing_active = True
        pairing_remaining = max(0, int(pin_data["expires_at"] - _time.time()))
        pairing_is_guest = pin_data.get("is_guest", False)
        pairing_guest_expires_at = pin_data.get("guest_expires_at")

    # Guests reduced to the PII-free triple (matches sensor.halumar_guests).
    # Pending-deletion clients are excluded (they still appear in `clients`).
    guests = [
        {
            "client_id": c["id"],
            "is_connected": c["is_connected"],
            "expires_at": c.get("expires_at"),
        }
        for c in clients
        if c.get("is_guest") and not c.get("pending_deletion")
    ]

    # Load active notifications per client — exposed as ids + counts ONLY
    # (title/message/action content never leaves the hub via this API).
    from services.notifications import get_active_notifications_for_all_clients

    all_notifications = await get_active_notifications_for_all_clients(db)

    for c in clients:
        client_notifs = all_notifications.get(c["id"], [])
        c["notification_ids"] = [n["id"] for n in client_notifs]
        c["notification_count"] = len(client_notifs)

    return {
        "hub": health,
        "areas": areas,
        "clients": clients,
        "guests": guests,
        "guest_count": len(guests),
        "pairing_active": pairing_active,
        "pairing_remaining": pairing_remaining,
        "pairing_is_guest": pairing_is_guest,
        "pairing_guest_expires_at": pairing_guest_expires_at,
        # Add-on option pairing_qr_camera: when the camera shows the QR
        # code with the PIN (off / guests / all). The integration refuses a
        # QR the admin did not switch on.
        "pairing_qr_camera": pairing_qr.camera_mode(),
        # Owner toggle: whether POST /integration/start-pairing is allowed.
        "allow_pairing_via_integration": (
            await db.get_setting("allow_pairing_via_integration")
        )
        != "false",
        # Owner toggle: whether the integration may serve the token-less
        # compliance endpoint. When off, no compliance blobs are emitted at
        # all and the integration serves neutral "unknown".
        "compliance_endpoint_enabled": compliance_enabled,
        # Signed "deleted" tombstones for ALL pending-deletion clients
        # (hidden ones included). Empty when the toggle is off.
        "tombstones": tombstones,
        "area_count": len(areas),
        "client_count": sum(1 for c in clients if not c.get("pending_deletion")),
        # Outstanding provisioning requests: fallback for a missed
        # provision_client push, handled by the integration exactly like one.
        "provisioning": await device_provisioning.outstanding_requests(db),
        # Deleted devices whose HA user stays until the app confirmed its
        # reset or the grace period ran out: the integration must not
        # remove these users on its own.
        "ha_user_holds": await device_provisioning.held_user_ids(db),
        # Rule snapshots: backstop for a lost enforcement_rules push.
        "enforcement": {
            "rules_hashes": await enforcement.rules_hashes(
                db, getattr(request.app.state, "ha_client", None)
            )
        },
    }


@router.get("/integration/enforcement-rules")
async def integration_enforcement_rules(request: Request):
    """Rule snapshots of all devices with an HA user.

    The complete set the integration applies when the hashes of the status
    poll differ from its own (backstop for a lost ``enforcement_rules``
    push). Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    rule_set = await enforcement.build_set(
        db, getattr(request.app.state, "ha_client", None)
    )
    enforcement.delivered(rule_set.snapshots)
    return {
        "snapshots": rule_set.snapshots,
        "control_paths": enforcement.control_paths(
            rule_set.snapshots, rule_set.unbuilt_paths()
        ),
    }


@router.post("/integration/kids-check", response_model=KidsCheckResponse)
async def integration_kids_check(request: Request, body: KidsCheckRequest):
    """Kids counter and pause before an action.

    The integration asks before executing a kids action whose entity has a
    rate limit; an allowed action is counted. A ``grant`` the hub passed
    itself in ``<ns>.control`` is answered allowed without counting twice.
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    return await enforcement.kids_check(
        db,
        request.app.state.ha_client,
        client_id=body.client_id,
        entity_id=body.entity_id,
        action=body.action,
        grant=body.grant,
    )


class AccessSwitchRequest(BaseModel):
    """contracts integration/access-switch.request."""

    model_config = {"extra": "forbid"}

    client_id: str = Field(..., min_length=1, max_length=64)
    on: bool
    user: str | None = Field(None, max_length=100)


class AccessSwitchResponse(BaseModel):
    """contracts integration/access-switch.response."""

    client_id: str
    access: dict


@router.post("/integration/access-switch", response_model=AccessSwitchResponse)
async def integration_access_switch(request: Request, body: AccessSwitchRequest):
    """A Home Assistant admin or an automation turned a device's access
    switch (``switch.<ns>_<name>_access``). 404 unknown device, 409
    ``no_access_switch``. Integration-token only."""
    from services import access as access_svc

    require_integration(request)
    db: Database = request.app.state.db
    try:
        state = await access_svc.set_switch(
            db,
            body.client_id,
            body.on,
            by="ha",
            actor=f"Home Assistant · {body.user}" if body.user else "Home Assistant",
        )
    except access_svc.SwitchError as e:
        raise HTTPException(status_code=e.status, detail=e.code) from e
    return {"client_id": body.client_id, "access": state}


class LocationStatusRequest(BaseModel):
    """contracts integration/location-status.request: metadata only."""

    model_config = {"extra": "forbid"}

    client_id: str = Field(..., min_length=1, max_length=64)
    background: bool
    ts: str = Field(..., min_length=1, max_length=40)
    location_shared: Literal["none", "foreground", "background"] | None = None
    battery_level: int | None = Field(None, ge=0, le=100)


class LocationStatusResponse(BaseModel):
    """contracts integration/location-status.response."""

    client_id: str
    location_shared: str | None
    location_at: str | None


@router.post("/integration/location-status", response_model=LocationStatusResponse)
async def integration_location_status(request: Request, body: LocationStatusRequest):
    """The integration accepted a ``<ns>.report_location`` of a device:
    whether and how it shares its location, never the position. 404 unknown
    device, 422 invalid ts. Integration-token only."""
    from services import location as location_svc

    require_integration(request)
    db: Database = request.app.state.db
    if not await db.fetchone(
        "SELECT id FROM paired_clients WHERE id = ?", (body.client_id,)
    ):
        raise HTTPException(status_code=404, detail="Client not found")
    try:
        ts = location_svc.parse_ts(body.ts)
    except location_svc.LocationError as e:
        raise HTTPException(status_code=422, detail="invalid ts") from e
    return await location_svc.integration_status(
        db,
        body.client_id,
        background=body.background,
        ts=ts,
        shared=body.location_shared,
        battery_level=body.battery_level,
    )


@router.post("/integration/events", response_model=IntegrationEventsResponse)
async def integration_events_intake(request: Request, body: IntegrationEventsRequest):
    """Integration events for the log tab. Idempotent per event id.

    Data minimization: ids, entity, action name, route, origin, reason,
    counts and times only. Integration-token only.
    """
    require_integration(request)
    accepted, duplicates = await integration_events.ingest(
        [e.model_dump() for e in body.events], body.dropped
    )
    return {"accepted": accepted, "duplicates": duplicates}


async def _holds_key(db: Database, client_id: str) -> bool:
    """The device holds its own HA key now (``resolver.holds_key``); an
    unresolvable device counts as holding one (it keeps the switch-over)."""
    from services.profiles import store as profiles

    try:
        resolved = await profiles.resolve_client(db, client_id)
    except Exception:  # noqa: BLE001 - unknown: the usual switch-over
        return True
    return resolved is None or profile_resolver.holds_key(resolved[2])


def _client_token_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


@router.post("/integration/client-token")
async def integration_client_token(request: Request, body: ClientTokenDelivery):
    """Accept the token of a device's own HA user. Idempotent.

    The integration created (or rotated) the HA user's token after a
    provision_client request and delivers it here. The token is stored
    encrypted and the connected app is told to fetch its new config; only
    then does the integration remove the previous token (after this 200).

    404 ``client_not_found`` and 409 ``client_inactive`` make the integration
    remove the HA user; 503 ``db_unavailable`` makes it retry with the same
    token. The bearer is never logged or returned. Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    client_id = body.client_id

    try:
        row = await db.fetchone(
            "SELECT id, is_active, (access = 'guest') AS is_guest, expires_at, "
            "pending_deletion_at, ha_user_id, ha_refresh_token_id, ha_token_status, "
            "ha_token_encrypted "
            "FROM paired_clients WHERE id = ?",
            (client_id,),
        )
    except Exception as e:  # noqa: BLE001 - surfaced as 503, retried
        logger.error("client-token for %s: database error %s", client_id, e)
        raise _client_token_error(503, "db_unavailable", "Database unavailable")
    if not row:
        raise _client_token_error(404, "client_not_found", "Client not found")
    if (
        not device_provisioning.is_provisionable(row)
        or row.get("is_guest")
        and is_expired(row.get("expires_at"))
    ):
        raise _client_token_error(409, "client_inactive", "Client is not active")
    if not await device_provisioning.delivery_allowed(
        db, row, body.ha_user_id, body.request_id
    ):
        logger.warning(
            "client-token for %s refused: HA user %s differs from the stored one "
            "and no open request matches",
            client_id,
            body.ha_user_id,
        )
        raise _client_token_error(
            422,
            "request_required",
            "Token delivery is not bound to an open request",
        )

    if (
        row.get("ha_token_status") == "active"
        and row.get("ha_refresh_token_id") == body.refresh_token_id
        and row.get("ha_user_id") == body.ha_user_id
        and row.get("ha_token_encrypted")
    ):
        return {
            "success": True,
            "client_id": client_id,
            "ha_token_status": "active",
            "unchanged": True,
            "delivered": False,
        }

    had_token = bool(row.get("ha_token_encrypted"))
    # A new HA user has no rule snapshot at the integration yet: the app gets
    # the token only after it was pushed (else its first
    # <ns>.control met rules_missing until the debounced push 1 s later).
    new_user = row.get("ha_user_id") != body.ha_user_id
    with enforcement.token_held(client_id) if new_user else nullcontext():
        try:
            stored = await device_provisioning.store_delivered_token(
                db,
                client_id,
                ha_user_id=body.ha_user_id,
                refresh_token_id=body.refresh_token_id,
                token=body.token,
            )
        except Exception as e:  # noqa: BLE001 - surfaced as 503, retried
            logger.error("client-token for %s: storing failed (%s)", client_id, e)
            raise _client_token_error(503, "db_unavailable", "Database unavailable")
        if not stored:
            # Revoked or deleted between the check above and the write.
            raise _client_token_error(409, "client_inactive", "Client is not active")
        await enforcement.push_now("client_token")
    await client_activity.record(
        db,
        client_id,
        client_activity.TOKEN_ROTATED if had_token else client_activity.TOKEN_ISSUED,
    )
    logger.info(
        "HA token of client %s stored (ha_user_id=%s, refresh_token_id=%s)",
        client_id,
        body.ha_user_id,
        body.refresh_token_id,
    )

    # The app switches to the new token. With a previous token the old one is
    # removed by the integration right after this answer, so wait until the
    # app fetched its config; a first token needs no wait, and neither does a
    # device that holds no key (a withdrawal: there is nothing to switch to,
    # the old token goes at once).
    if had_token and await _holds_key(db, client_id):
        delivered = await api_ws.deliver_rotated_config(client_id)
    else:
        delivered = await api_ws.send_to_client(
            client_id, {"type": "client_config_updated", "data": {}}
        )

    return {
        "success": True,
        "client_id": client_id,
        "ha_token_status": "active",
        "unchanged": False,
        "delivered": delivered,
    }


@router.post("/integration/ha-url", response_model=HAUrlReportResponse)
async def integration_ha_url(request: Request, body: HAUrlReport):
    """External Home Assistant URL as HA reports it.

    The integration asks HA (``get_url`` without internal URLs: its
    ``external_url``, else the Home Assistant Cloud address) and reports the
    result here when it changes, also when the cloud connection or HA's
    configuration changes; null = none. It becomes ``ha_url_detected`` and,
    unless the admin set another external URL in the hub, the URL devices
    with remote access get in ``client_config``.

    Only a URL equal to HA's ``external_url`` (read by the hub from the
    Supervisor) or a Home Assistant Cloud address is accepted (422
    ``url_not_allowed``), so the integration token cannot send the devices'
    HA tokens elsewhere. Connected apps are told to fetch their config when
    the URL changes. Integration-token only.

    ``ha_instance_id`` (HA's instance ID) is stored and handed to the
    devices as ``client_config.ha_instance_id``: when the pinned hub does not
    answer, an HA announced over Bonjour with this ``uuid`` confirms the home
    network for unencrypted HA addresses. A change also counts as ``changed``.
    """
    require_integration(request)
    db: Database = request.app.state.db

    from services.audit_log import log_admin_action
    from services.ha_url import (
        UrlCheckUnavailable,
        apply_detected_external_url,
        normalize_external_url,
        reported_url_acceptable,
    )

    # HA's instance ID (the apps' second proof of the home network) does
    # not depend on the URL check below: stored first, also when the URL is refused.
    id_changed = False
    if (
        body.ha_instance_id
        and await db.get_setting("ha_instance_id") != body.ha_instance_id
    ):
        await db.set_setting("ha_instance_id", body.ha_instance_id)
        log_admin_action("ha_instance_id_reported")
        id_changed = True

    url = None
    if body.external_url:
        url = normalize_external_url(body.external_url)
        try:
            acceptable = url is not None and await reported_url_acceptable(
                request.app.state.ha_client, url
            )
        except UrlCheckUnavailable:
            # No definite answer from Home Assistant yet (restart): 503, which
            # the integration retries; a 422 it would keep as final.
            if id_changed:
                await api_ws.broadcast({"type": "client_config_updated", "data": {}})
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "ha_unavailable",
                    "message": "Home Assistant cannot confirm the URL yet",
                },
            ) from None
        if not acceptable:
            if id_changed:
                await api_ws.broadcast({"type": "client_config_updated", "data": {}})
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "url_not_allowed",
                    "message": "Not HA's external_url or a Nabu Casa address",
                },
            )
    url_changed = await apply_detected_external_url(db, url)
    if url_changed:
        request.app.state._cors_ha_url = await db.get_setting("ha_url") or ""
        log_admin_action(
            "ha_external_url_detected",
            details="nabu_casa"
            if url and url.endswith(".ui.nabu.casa")
            else ("external_url" if url else "none"),
        )
    changed = url_changed or id_changed
    if changed:
        await api_ws.broadcast({"type": "client_config_updated", "data": {}})
    return HAUrlReportResponse(success=True, changed=changed)


# 5x7 pixel font of the status images (rows top to bottom, 5 bits wide).
_STATUS_FONT: dict[str, list[int]] = {
    "A": [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "B": [0b11110, 0b10001, 0b10001, 0b11110, 0b10001, 0b10001, 0b11110],
    "C": [0b01110, 0b10001, 0b10000, 0b10000, 0b10000, 0b10001, 0b01110],
    "D": [0b11100, 0b10010, 0b10001, 0b10001, 0b10001, 0b10010, 0b11100],
    "E": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b11111],
    "F": [0b11111, 0b10000, 0b10000, 0b11110, 0b10000, 0b10000, 0b10000],
    "G": [0b01110, 0b10001, 0b10000, 0b10111, 0b10001, 0b10001, 0b01111],
    "H": [0b10001, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001],
    "I": [0b01110, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "J": [0b00111, 0b00010, 0b00010, 0b00010, 0b00010, 0b10010, 0b01100],
    "K": [0b10001, 0b10010, 0b10100, 0b11000, 0b10100, 0b10010, 0b10001],
    "L": [0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b10000, 0b11111],
    "M": [0b10001, 0b11011, 0b10101, 0b10101, 0b10001, 0b10001, 0b10001],
    "N": [0b10001, 0b11001, 0b10101, 0b10011, 0b10001, 0b10001, 0b10001],
    "O": [0b01110, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "P": [0b11110, 0b10001, 0b10001, 0b11110, 0b10000, 0b10000, 0b10000],
    "Q": [0b01110, 0b10001, 0b10001, 0b10001, 0b10101, 0b10010, 0b01101],
    "R": [0b11110, 0b10001, 0b10001, 0b11110, 0b10100, 0b10010, 0b10001],
    "S": [0b01110, 0b10001, 0b10000, 0b01110, 0b00001, 0b10001, 0b01110],
    "T": [0b11111, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100, 0b00100],
    "U": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "V": [0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100],
    "W": [0b10001, 0b10001, 0b10001, 0b10101, 0b10101, 0b10101, 0b01010],
    "X": [0b10001, 0b10001, 0b01010, 0b00100, 0b01010, 0b10001, 0b10001],
    "Y": [0b10001, 0b10001, 0b01010, 0b00100, 0b00100, 0b00100, 0b00100],
    "Z": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b10000, 0b11111],
    "Ä": [0b01010, 0b00000, 0b01110, 0b10001, 0b11111, 0b10001, 0b10001],
    "Ö": [0b01010, 0b00000, 0b01110, 0b10001, 0b10001, 0b10001, 0b01110],
    "Ü": [0b01010, 0b00000, 0b10001, 0b10001, 0b10001, 0b10001, 0b01110],
    "-": [0b00000, 0b00000, 0b00000, 0b11111, 0b00000, 0b00000, 0b00000],
    ".": [0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b01100, 0b01100],
    "'": [0b00100, 0b00100, 0b01000, 0b00000, 0b00000, 0b00000, 0b00000],
}


def _generate_standby_image(
    text: str = "STANDBY", lines: tuple[str, ...] = ()
) -> bytes:
    """Generate a 200x200 status PNG with ``text`` rendered as pixel art.

    ``lines`` (at most four, 15 characters each) are drawn smaller below the
    title. Uses raw PNG encoding (struct + zlib) since Pillow is not available.
    Background: #1a1d27 (Halumar surface color), text: #8b8fa3 (secondary).
    """
    W, H = 200, 200
    bg = (0x1A, 0x1D, 0x27)
    fg = (0x8B, 0x8F, 0xA3)
    border_color = (0x2A, 0x2E, 0x3A)

    # 5x7 pixel font (each char is a list of 7 rows, 5 bits wide); unknown
    # characters are drawn as a gap.
    FONT = _STATUS_FONT

    # QR-code-like corner markers (decorative)
    def draw_marker(pixels, ox, oy, size=21):
        """Draw a QR-style finder pattern at (ox, oy)."""
        for y in range(size):
            for x in range(size):
                # Outer border
                if x == 0 or x == size - 1 or y == 0 or y == size - 1:
                    pixels[oy + y][ox + x] = border_color
                # Inner border (gap)
                elif x == 2 or x == size - 3 or y == 2 or y == size - 3:
                    if 2 <= x <= size - 3 and 2 <= y <= size - 3:
                        pixels[oy + y][ox + x] = bg
                # Inner square
                elif 4 <= x <= size - 5 and 4 <= y <= size - 5:
                    if x == 4 or x == size - 5 or y == 4 or y == size - 5:
                        pixels[oy + y][ox + x] = border_color
                    else:
                        pixels[oy + y][ox + x] = fg

    # Build pixel array
    pixels = [[bg for _ in range(W)] for _ in range(H)]

    # Draw QR-style corner markers
    draw_marker(pixels, 8, 8)
    draw_marker(pixels, W - 29, 8)
    draw_marker(pixels, 8, H - 29)

    def draw_text(line: str, scale: int, top: int) -> None:
        char_w = 5 * scale
        spacing = 1 * scale
        total_w = len(line) * char_w + (len(line) - 1) * spacing
        start_x = (W - total_w) // 2
        for ci, ch in enumerate(line):
            glyph = FONT.get(ch)
            if not glyph:
                continue
            cx = start_x + ci * (char_w + spacing)
            for row_i, row_bits in enumerate(glyph):
                for col_i in range(5):
                    if row_bits & (1 << (4 - col_i)):
                        for sy in range(scale):
                            for sx in range(scale):
                                px = cx + col_i * scale + sx
                                py = top + row_i * scale + sy
                                if 0 <= px < W and 0 <= py < H:
                                    pixels[py][px] = fg

    # The title centered at scale 3; with lines, the title moves up and the
    # lines follow at scale 2 (between the corner markers).
    if lines:
        draw_text(text, 3, 64)
        for i, line in enumerate(lines[:4]):
            draw_text(line, 2, 100 + i * 18)
    else:
        draw_text(text, 3, (H - 7 * 3) // 2)

    # Encode as PNG
    def make_png(width, height, pixel_rows):
        """Minimal PNG encoder for RGB data."""

        def chunk(ctype, data):
            c = ctype + data
            return (
                struct.pack(">I", len(data))
                + c
                + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        raw = b""
        for row in pixel_rows:
            raw += b"\x00"  # filter: none
            for r, g, b in row:
                raw += struct.pack("BBB", r, g, b)
        idat = zlib.compress(raw, 9)

        out = b"\x89PNG\r\n\x1a\n"
        out += chunk(b"IHDR", ihdr)
        out += chunk(b"IDAT", idat)
        out += chunk(b"IEND", b"")
        return out

    return make_png(W, H, pixels)


# Cache the status images (they never change; they never carry a PIN)
_status_image_cache: dict[tuple[str, tuple[str, ...]], bytes] = {}


def _get_status_image(text: str, lines: tuple[str, ...] = ()) -> bytes:
    """Return the cached status image for ``text`` (STANDBY / PAIRING)."""
    key = (text, lines)
    if key not in _status_image_cache:
        _status_image_cache[key] = _generate_standby_image(text, lines)
    return _status_image_cache[key]


@router.get("/integration/pairing-qr")
async def integration_pairing_qr(request: Request):
    """Return the image of the integration's pairing camera entity.

    Every HA user and every device token can read a camera entity. The QR
    code with the pairing deep link (and so the PIN) is therefore shown only
    when the admin switched it on with the app option ``pairing_qr_camera``:
    ``guests`` during a guest pairing window,
    ``all`` during every window, and only to an integration that sends
    ``X-Pairing-Qr: accept``. Then ``X-Pairing-Pin: included``,
    ``X-Pairing-Kind: guest|device`` and ``X-Pairing-Window`` (an opaque id
    of the window, not derived from the PIN). Otherwise a status image ("PAIRING" /
    "STANDBY", with a line why no QR is shown) and ``X-Pairing-Pin:
    omitted``. Never cached: once the window ends or the PIN is used the
    next request gets the status image. Integration-token only.
    """
    require_integration(request)
    import time

    db: Database = request.app.state.db
    mode = pairing_qr.camera_mode()
    active = api_pairing.get_active_pin()
    remaining = 0
    if active:
        _pin, pin_data = active
        remaining = max(0, int(pin_data["expires_at"] - time.time()))
    pairing_active = remaining > 0
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "X-Pairing-Active": "true" if pairing_active else "false",
        "X-Pairing-Remaining": str(remaining),
    }

    accepts_qr = (
        request.headers.get(pairing_qr.ACCEPT_HEADER, "").lower()
        == pairing_qr.ACCEPT_VALUE
    )
    if (
        accepts_qr
        and active
        and pairing_active
        and pairing_qr.shows_pin(mode, active[1])
    ):
        from services import home_networks
        from services.pairing_link import build_pairing_deep_link, resolve_lan_base_url

        pin, pin_data = active
        # The address the apps reach on the LAN (as the admin UI's QR), not
        # the one the integration used for this request.
        base_url = await resolve_lan_base_url(request.app, db)
        link = build_pairing_deep_link(base_url, pin, await home_networks.cidrs(db))
        # The window may have ended or the PIN been used meanwhile.
        current = api_pairing.get_active_pin()
        if current is not None and current[1] is pin_data:
            pairing_qr.note_shown(pin_data)
            return Response(
                content=pairing_qr.render_qr(link),
                media_type="image/png",
                headers={
                    **headers,
                    "X-Pairing-Pin": "included",
                    "X-Pairing-Kind": pairing_qr.window_kind(pin_data),
                    "X-Pairing-Window": pairing_qr.window_id(pin_data),
                },
            )
        pairing_active = False
        headers["X-Pairing-Active"] = "false"
        headers["X-Pairing-Remaining"] = "0"

    from services.profiles import store as profiles

    title, lines = pairing_qr.status_text(
        mode, pairing_active, await profiles.language(db)
    )
    return Response(
        content=_get_status_image(title, lines),
        media_type="image/png",
        headers={**headers, "X-Pairing-Pin": "omitted"},
    )


@router.get("/integration/devices")
async def integration_devices(request: Request):
    """Device registry data for the HA custom integration.

    Returns the device definitions that the integration should create
    in the HA device registry, including identifiers, names, and
    manufacturer info. Called once during integration setup and when
    new devices are discovered.

    Device types:
    - hub: The Halumar hub itself (1 device)
    - client: One device per paired client
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db

    connected_ids = api_ws.get_connected_client_ids()

    # ── Hub device ───────────────────────────────────────────────────────
    area_count = await db.fetchone("SELECT COUNT(*) as cnt FROM areas")
    hub_device = {
        "type": "hub",
        "identifier": HA_DEVICE_IDENTIFIER_HUB,
        "name": BRAND_NAME,
        "manufacturer": "Xonatec",
        "model": f"{BRAND_NAME} Area Manager",
        "sw_version": VERSION,
        "area_count": area_count["cnt"] if area_count else 0,
    }

    # ── Client devices ───────────────────────────────────────────────────
    clients_raw = await db.fetchall(
        "SELECT id, name, device_name, app_version, is_active, created_at "
        "FROM paired_clients ORDER BY created_at DESC"
    )

    client_devices = []
    for c in clients_raw:
        client_devices.append(
            {
                "type": "client",
                "identifier": ha_device_identifier_client(c["id"]),
                "name": f"{BRAND_NAME}: {c['name']}",
                "manufacturer": f"{BRAND_NAME} Client",
                "model": c.get("device_name") or "Unknown Device",
                "sw_version": c.get("app_version"),
                "client_id": c["id"],
                "client_name": c["name"],
                "is_active": bool(c["is_active"]),
                "is_connected": c["id"] in connected_ids,
            }
        )

    return {
        "devices": [hub_device] + client_devices,
    }


@router.post("/integration/start-pairing")
async def integration_start_pairing(request: Request):
    """Start a pairing session (optionally for guests).

    Accepts an optional JSON body (contracts integration/start-pairing.request):
    - is_guest: bool (default false)
    - guest_expires_hours: int (default: house setting, max 2160)
    - guest_expires_at: ISO 8601 with offset (precedence over hours)
    - guest_tags: list[str] (guests: areas as a device adjustment; missing =
      the profile decides; 409 rule_fixed when the profile fixes areas)
    - profile: str (profile NAME; missing = default profile of the device type)
    - owner_label: str (optional owner of the device, max 50 characters)
    Integration-token only.

    Blocked with 403 (user AND guest starts) when the admin disabled the
    allow_pairing_via_integration setting; cancel stays available.
    """
    require_integration(request)
    db: Database = request.app.state.db

    if (await db.get_setting("allow_pairing_via_integration")) == "false":
        raise HTTPException(
            status_code=403,
            detail="Pairing via the integration is disabled in the add-on.",
        )

    import time

    # Parse optional body (empty body = default non-guest pairing). Read it
    # whatever the headers say: a chunked body has no Content-Length, and a
    # guest pairing must not silently become a member PIN.
    body = PairingGenerateRequest()
    raw_body = await request.body()
    if raw_body.strip():
        try:
            raw = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        try:
            body = PairingGenerateRequest(**raw)
        except ValidationError as exc:
            # Invalid guest parameters (naive/garbage expiry, unknown
            # url mode) -> 422 like every other validated endpoint.
            raise RequestValidationError(exc.errors(include_url=False))

    pin = api_pairing._generate_pin()
    now = time.time()
    expires_at = now + api_pairing.PAIRING_TIMEOUT_SECONDS

    pin_data = {
        "created_at": now,
        "expires_at": expires_at,
        "failed_attempts": 0,
    }

    # Profile by NAME: automations pass a readable name; missing = the
    # default profile of the device type (guests: the guest default profile).
    from services.profiles import store as profiles

    is_guest = body.is_guest
    profile_row = None
    if body.profile:
        profile_row = await db.fetchone(
            "SELECT id, child_protection, body_json FROM profiles "
            "WHERE name = ? COLLATE NOCASE",
            (body.profile.strip(),),
        )
        if not profile_row:
            raise HTTPException(
                status_code=404, detail=f"Profile '{body.profile.strip()}' not found."
            )
        pin_data["profile_id"] = profile_row["id"]
    if is_guest:
        from services.expiry import expiry_from_hours

        if profile_row is None:
            pid = await profiles.default_profile_for(db, None, True)
            profile_row = await db.fetchone(
                "SELECT id, child_protection, body_json FROM profiles WHERE id = ?",
                (pid,),
            )
            # Fixed when the window opens: the guest gets this profile even
            # when the guest default changes before the PIN is used.
            pin_data["profile_id"] = pid
        if profile_row["child_protection"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "profile.guest_child_protection",
                    "message": "a guest cannot have parental controls",
                },
            )
        # guest_expires_at is validated + normalized to UTC by the schema.
        hours = body.guest_expires_hours or await profiles.guest_default_hours(db)
        guest_expires_at = body.guest_expires_at or expiry_from_hours(hours)

        pin_data["is_guest"] = True
        pin_data["guest_expires_at"] = guest_expires_at
        if body.guest_tags is not None:
            body_fixed = profiles.body_of(
                {**profile_row, "remote_pin_hash": None, "remote_pin_salt": None}
            )["fixed"]
            if "areas" in body_fixed:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "rule_fixed", "message": "the profile fixes areas"},
                )
            pin_data["areas"] = profiles.areas_from_tags(body.guest_tags, guest=True)

    if body.owner_label:
        pin_data["owner_label"] = body.owner_label

    api_pairing.set_active_pin(pin, pin_data)

    logger.info(
        "Generated pairing PIN via integration (expires in %ds, guest=%s)",
        api_pairing.PAIRING_TIMEOUT_SECONDS,
        is_guest,
    )

    # Tell admin UIs (and the integration push session) a window opened —
    # the payload stays secret-free (never the PIN).
    await api_ws.broadcast(
        {
            "type": "pairing_started",
            "data": {
                "is_guest": bool(is_guest),
                "expires_in": api_pairing.PAIRING_TIMEOUT_SECONDS,
            },
        },
        audience=EventAudience.ADMIN,
    )

    return {
        "pin": pin,
        "expires_in": api_pairing.PAIRING_TIMEOUT_SECONDS,
        "is_guest": is_guest,
    }


@router.post("/integration/areas/{area_id}/activate")
async def integration_activate_area(request: Request, area_id: str):
    """Set an area's is_active to true. Used by HA switch entities. Integration-token only."""
    require_integration(request)
    db: Database = request.app.state.db

    area = await db.fetchone("SELECT id, name FROM areas WHERE id = ?", (area_id,))
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")

    async with db.transaction():
        await db.execute(
            "UPDATE areas SET is_active = 1, updated_at = ? WHERE id = ?",
            (Database.now(), area_id),
        )

    logger.info("Area '%s' activated via integration", area["name"])

    # Sync HA sensors + switches
    try:
        from services.ha_sensors import (
            sync_area_sensor,
            sync_area_switch,
            sync_config_sensor,
        )

        ha_client = getattr(request.app.state, "ha_client", None)
        await sync_area_sensor(db, ha_client, area_id)
        await sync_area_switch(db, ha_client, area_id)
        await sync_config_sensor(db, ha_client)
    except Exception as e:
        logger.warning("Sensor sync after activate failed: %s", e)

    # Broadcast to WS clients
    await api_ws.announce_area_active(db, area_id, True)

    return {"success": True, "area_id": area_id, "is_active": True}


@router.post("/integration/areas/{area_id}/deactivate")
async def integration_deactivate_area(request: Request, area_id: str):
    """Set an area's is_active to false. Used by HA switch entities. Integration-token only."""
    require_integration(request)
    db: Database = request.app.state.db

    area = await db.fetchone("SELECT id, name FROM areas WHERE id = ?", (area_id,))
    if not area:
        raise HTTPException(status_code=404, detail="Area not found")

    async with db.transaction():
        await db.execute(
            "UPDATE areas SET is_active = 0, updated_at = ? WHERE id = ?",
            (Database.now(), area_id),
        )

    logger.info("Area '%s' deactivated via integration", area["name"])

    # Sync HA sensors + switches
    try:
        from services.ha_sensors import (
            sync_area_sensor,
            sync_area_switch,
            sync_config_sensor,
        )

        ha_client = getattr(request.app.state, "ha_client", None)
        await sync_area_sensor(db, ha_client, area_id)
        await sync_area_switch(db, ha_client, area_id)
        await sync_config_sensor(db, ha_client)
    except Exception as e:
        logger.warning("Sensor sync after deactivate failed: %s", e)

    # Broadcast to WS clients
    await api_ws.announce_area_active(db, area_id, False)

    return {"success": True, "area_id": area_id, "is_active": False}


# ── Client commands (remote control) ────────────────────────────────────
#
# Each command is a separate endpoint for clearer HA service mapping.
# All endpoints verify the client exists, is active, and is connected
# via WebSocket before sending the command message.


async def _get_active_client(db: Database, client_id: str) -> dict:
    """Verify a client exists and is active. Returns the client row or raises."""
    client = await db.fetchone(
        "SELECT id, name, is_active FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if not client["is_active"]:
        raise HTTPException(status_code=422, detail="Client is deactivated")
    return client


async def _send_ws_command(client_id: str, client_name: str, message: dict) -> dict:
    """Send a WebSocket message to a client. Returns success response or raises."""
    sent = await api_ws.send_to_client(client_id, message)
    if not sent:
        raise HTTPException(
            status_code=422,
            detail="Client is not connected via WebSocket",
        )
    command = message.get("command", "unknown")
    logger.info("Sent '%s' to client '%s' (%s)", command, client_name, client_id)
    return {"success": True, "client_id": client_id, "command": command}


@router.post("/integration/clients/{client_id}/navigate_to_area")
async def integration_navigate_to_area(
    request: Request, client_id: str, body: NavigateToAreaRequest
):
    """Navigate a client to a specific area.

    Body: {"area_id": "uuid"} or {"area_id": "__home__"} (HOME screen).
    The client UI will switch to display the specified area.
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    client = await _get_active_client(db, client_id)

    if body.area_id != HOME_SENTINEL:
        area = await db.fetchone(
            "SELECT id, name FROM areas WHERE id = ?", (body.area_id,)
        )
        if not area:
            raise HTTPException(status_code=404, detail="Area not found")

    return await _send_ws_command(
        client_id,
        client["name"],
        {
            "type": "command",
            "command": "navigate_to_area",
            "area_id": body.area_id,
        },
    )


@router.post("/integration/clients/{client_id}/show_entity")
async def integration_show_entity(
    request: Request, client_id: str, body: ShowEntityRequest
):
    """Show an entity on a client, regardless of whether it belongs to an area.

    Body: {"entity_id": "light.living_room", "control_mode": "full"}
    The client will display a detail view for this entity.
    control_mode is optional (basic/advanced/full) and tells the client
    which level of controls to show for this entity.
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    client = await _get_active_client(db, client_id)

    ws_message = {
        "type": "command",
        "command": "show_entity",
        "entity_id": body.entity_id,
    }
    if body.control_mode:
        ws_message["control_mode"] = body.control_mode

    return await _send_ws_command(client_id, client["name"], ws_message)


@router.post(
    "/integration/clients/{client_id}/panel", response_model=PanelCommandResponse
)
async def integration_panel_command(
    request: Request, client_id: str, body: IntegrationPanelCommandRequest
):
    """Panel command from Home Assistant.

    The integration calls it from its panel entities after checking the
    caller (HA admin or automation, never a device user). The hub checks the
    device (panel, not a guest, rule panel_remote, platform), sends the
    WebSocket command panel_command and waits up to 5 s for the app's answer.
    Errors: {"detail": {"code", "message"}} with 404/422/504. Every command is
    logged with its actor. Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    actor = body.actor.model_dump() if body.actor else {"kind": "automation"}
    try:
        return await panel.send_command(
            db,
            client_id,
            body.action,
            **body.args(),
            actor=actor,
        )
    except panel.PanelError as e:
        raise HTTPException(status_code=e.status, detail=e.detail()) from None


@router.post("/integration/notify")
async def integration_notify(request: Request, body: IntegrationNotifyRequest):
    """Send a notification to one client or broadcast to all.

    Body matches POST /api/notifications (the hub's notification model):
    client_id is a paired-client id or "all" (broadcast to every active
    client except watches), plus title/message/notification_type/icon/
    popup_mode/scheduled_at/priority/color/action_type/action_target/show_time,
    and an optional ``notification_id`` (UUID chosen by the integration) that
    makes a retried call idempotent. With "all" it is a broadcast id: every device's
    notification gets UUIDv5(broadcast id, client id) and devices that
    already have theirs are skipped.

    A notification for a watch goes to the phone that set it up, the answer
    names it in ``forwarded_to`` (422 when that phone is no longer paired);
    an idempotent id is compared against that phone.

    Returns {"sent": N} for the "all" broadcast (plus ``duplicates`` with a
    broadcast id), otherwise {"notification": ..., "ws_sent": ...} (plus
    ``duplicate: true`` when the id already existed). Integration-token only.
    """
    require_integration(request)
    return await deliver_notification_request(request, body)


@router.post("/integration/notifications/handover")
async def integration_notifications_handover(
    request: Request, body: NotificationHandoverRequest
):
    """Take over notifications the integration queued while the hub was down.

    While the hub is unreachable, the integration writes notifications
    directly into the client's notification sensor (with its own UUIDs) and
    hands them over here once the hub answers again; from then on the hub is
    the source of truth. Per item:

    - unknown id -> created with the given ``notification_id``/``created_at``
      (``created``); ``status: "delivered"`` (the app confirmed it through
      Home Assistant meanwhile) is stored as delivered via 'remote' and not
      pushed again, a pending one is pushed to the connected app;
    - id of the same client already known -> ``duplicates`` (idempotent);
    - an item for a watch goes to the phone that set it up, like a live
      notification (``client_not_found`` when that phone is gone);
    - unknown client, id of another client, invalid content or older than
      72 h -> ``skipped`` with a reason.

    The notification sensors are rewritten once at the end (always, since
    the integration wrote them meanwhile). Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db

    from datetime import datetime, timedelta, timezone

    from api.notifications import _deliver_notification, notification_fields
    from models.schemas import NOTIFICATION_HANDOVER_MAX_AGE_HOURS
    from services.ha_sensors import (
        mark_handover_resolved,
        sync_notification_sensors_forced,
    )
    from services.notifications import get_notification, notification_recipient

    oldest = datetime.now(timezone.utc) - timedelta(
        hours=NOTIFICATION_HANDOVER_MAX_AGE_HOURS
    )
    created: list[str] = []
    duplicates: list[str] = []
    skipped: list[dict] = []
    clients: dict[str, dict | None] = {}
    for item in body.notifications:
        nid = item.notification_id
        if item.client_id not in clients:
            row = await db.fetchone(
                "SELECT id, name, platform, set_up_by_client_id "
                "FROM paired_clients WHERE id = ?",
                (item.client_id,),
            )
            # A watch's notification goes to its phone, like a live one.
            clients[item.client_id] = (
                await notification_recipient(db, dict(row)) if row else None
            )
        client = clients[item.client_id]
        if client is None:
            skipped.append({"notification_id": nid, "reason": "client_not_found"})
            continue
        existing = await get_notification(db, nid)
        if existing:
            if existing.get("client_id") == client["id"]:
                duplicates.append(nid)
            else:
                skipped.append({"notification_id": nid, "reason": "id_conflict"})
            continue
        if datetime.fromisoformat(item.created_at) < oldest:
            skipped.append({"notification_id": nid, "reason": "expired"})
            continue
        try:
            fields = notification_fields(item)
        except HTTPException:
            skipped.append({"notification_id": nid, "reason": "invalid"})
            continue
        await _deliver_notification(
            db,
            dict(client),
            notification_id=nid,
            created_at=item.created_at,
            delivered=item.status == "delivered",
            **fields,
        )
        created.append(nid)

    ha_client = getattr(request.app.state, "ha_client", None)
    mark_handover_resolved(ha_client, [n.notification_id for n in body.notifications])
    await sync_notification_sensors_forced(db, ha_client)

    logger.info(
        "Took over %d queued notification(s) from the integration "
        "(%d duplicate(s), %d skipped)",
        len(created),
        len(duplicates),
        len(skipped),
    )
    return {
        "success": True,
        "created": created,
        "duplicates": duplicates,
        "skipped": skipped,
    }


@router.post("/integration/clients/{client_id}/kick")
async def integration_kick_client(request: Request, client_id: str):
    """Disconnect a client's WebSocket session(s).

    The client row stays active — this only drops the live connection(s);
    the client may reconnect with its existing secret. Close code 4004 is
    deliberately NOT one of the reset codes the apps act on (4001 = removed,
    4002 = auth timeout, 4003 = guest expired), so a kick never wipes
    credentials. Use the client management API to deactivate/delete a client
    for good. Returns 200 with closed=0 when the client is offline.
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db

    client = await db.fetchone(
        "SELECT id, name FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")

    closed = await api_ws.disconnect_client(client_id, code=4004, reason="Kicked")
    logger.info(
        "Kicked client '%s' via integration (%d connection(s) closed)",
        client["name"],
        closed,
    )
    return {"success": True, "client_id": client_id, "closed": closed}


@router.post("/integration/pairing/cancel")
async def integration_cancel_pairing(request: Request):
    """Cancel any open pairing window.

    Idempotent: returns 200 also when no pairing window is open
    (cancelled=false in that case). Integration-token only.
    """
    require_integration(request)
    was_active = api_pairing.get_active_pin() is not None
    api_pairing.clear_active_pins()
    if was_active:
        logger.info("Pairing window cancelled via integration")

        await api_ws.broadcast(
            {"type": "pairing_cancelled", "data": {}},
            audience=EventAudience.ADMIN,
        )
    return {"success": True, "cancelled": was_active}


@router.post("/integration/clients/{client_id}/clear_notifications")
async def integration_clear_notifications(request: Request, client_id: str):
    """Clear all notifications on a client (sticky, normal, and permanent).

    No body required. Dismisses all in DB and sends clear command via WS.
    Returns success even if client is offline (DB state is authoritative).
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    client = await _get_active_client(db, client_id)

    # Dismiss in DB first (this is the authoritative state)
    from services.notifications import clear_client_notifications

    count = await clear_client_notifications(db, client_id)

    # Try to send clear command via WS (best-effort, client may be offline)
    ws_sent = await api_ws.send_to_client(
        client_id,
        {
            "type": "command",
            "command": "clear_notifications",
        },
    )

    # Sync HA notification sensors
    try:
        from services.ha_sensors import sync_notification_sensors_safe

        ha_client = getattr(request.app.state, "ha_client", None)
        await sync_notification_sensors_safe(db, ha_client)
    except Exception:
        pass

    logger.info(
        "Cleared %d notifications for client '%s' (ws=%s)",
        count,
        client["name"],
        ws_sent,
    )

    return {
        "success": True,
        "client_id": client_id,
        "command": "clear_notifications",
        "cleared": count,
        "ws_sent": ws_sent,
    }


@router.post("/integration/notifications/ack")
async def integration_ack_notifications(
    request: Request, body: AckNotificationsRequest
):
    """Remote fallback ack: mark notifications as delivered on a client's behalf.

    When a client app can reach only Home Assistant (the hub is reachable
    only on the home network and the app is away), it reads its notifications from the per-client HA
    sensor and confirms them through the ``halumar.ack_notifications`` HA
    service; the integration forwards the batch here. This is the
    write-scope-minimal remote counterpart of the WS ``notification_ack``
    (remote dismiss is deliberately NOT exposed).

    Per id, the same ownership rule as the WS path applies: ids that are
    unknown, belong to another client, or are no longer pending go silently to
    ``skipped`` (which also makes a duplicate/racing ack idempotent); matching
    pending ids are acknowledged with ``delivered_via='remote'``.

    For each acked id an ADMIN ``notification_delivered`` broadcast
    (data: notification_id/client_id/via="remote") is emitted, and the HA
    notification sensors are synced exactly ONCE per call (only when at least
    one id was acked) so the sensor flips to delivered for the app's next
    poll. Unknown client → 404, deactivated client → 422.

    Response: 200 {"success": true, "client_id": ..., "acked": [...],
    "skipped": [...]}. Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    client = await _get_active_client(db, body.client_id)

    from services.notifications import acknowledge_notification, get_notification

    acked: list[str] = []
    skipped: list[str] = []
    for notification_id in body.notification_ids:
        notif = await get_notification(db, notification_id)
        # Ownership check identical to the WS notification_ack path; foreign,
        # unknown and non-pending ids are skipped silently.
        if (
            not notif
            or notif.get("client_id") != body.client_id
            or notif.get("status") != "pending"
        ):
            skipped.append(notification_id)
            continue
        if await acknowledge_notification(db, notification_id, delivered_via="remote"):
            acked.append(notification_id)
        else:
            # Lost the race against a concurrent WS ack — first winner counts.
            skipped.append(notification_id)

    if acked:
        for notification_id in acked:
            await api_ws.broadcast(
                {
                    "type": "notification_delivered",
                    "data": {
                        "notification_id": notification_id,
                        "client_id": body.client_id,
                        "via": "remote",
                    },
                },
                audience=EventAudience.ADMIN,
            )

        # Exactly ONE sensor sync per call — flips the per-client notification
        # sensor to delivered so the app sees the confirmation next poll.
        try:
            from services.ha_sensors import sync_notification_sensors_safe

            ha_client = getattr(request.app.state, "ha_client", None)
            await sync_notification_sensors_safe(db, ha_client)
        except Exception:
            pass

    logger.info(
        "Remote-acked %d notification(s) for client '%s' (%d skipped)",
        len(acked),
        client["name"],
        len(skipped),
    )
    return {
        "success": True,
        "client_id": body.client_id,
        "acked": acked,
        "skipped": skipped,
    }


@router.post("/integration/clients/{client_id}/refresh")
async def integration_refresh_client(request: Request, client_id: str):
    """Force a client to reload all area and entity data.

    No body required.
    Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    client = await _get_active_client(db, client_id)

    return await _send_ws_command(
        client_id,
        client["name"],
        {
            "type": "command",
            "command": "refresh",
        },
    )


# ── Client management (tags + guest extension) ───────────────────────────
#
# Backing endpoints for the integration's manage_client_tags / extend_guest
# services and the editable guest-expiry datetime entity. Both delegate to
# the shared cores in api/pairing.py, so integration calls behave exactly
# like the admin endpoints.


@router.post("/integration/clients/{client_id}/tags")
async def integration_set_client_tags(
    request: Request, client_id: str, body: ClientTagsRequest
):
    """Set the areas of a device from the integration's tag list.

    Body: {"tags": ["kitchen", "guests"]}: the FULL new list. It becomes a
    device adjustment of the rule ``areas`` (``["*"]`` = all areas, ``[]`` =
    all for members, none for guests; ``area:<id>`` = one area). 409
    ``rule_fixed`` when the profile fixes ``areas``. Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db
    from services.profiles import changes
    from services.profiles import store as profiles
    from services.profiles.catalog import ProfileError

    row = await profiles.client_row(db, client_id)
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    value = profiles.areas_from_tags(body.tags, guest=row.get("access") == "guest")

    async def write():
        await profiles.put_override(
            db,
            client_id,
            "areas",
            value=value,
            user_choice=False,
            expires_at=None,
            note=None,
            actor="integration",
        )

    try:
        _, change = await changes.run(db, write, dry_run=False)
    except ProfileError as e:
        status = 409 if e.code == "override.fixed" else e.status
        code = "rule_fixed" if e.code == "override.fixed" else e.code
        raise HTTPException(
            status_code=status, detail={"code": code, "message": e.message}
        )
    await changes.audit(
        db,
        "override_set",
        actor="integration",
        client_id=client_id,
        rule_key="areas",
        after=value,
    )
    await changes.apply_effects(db, change)
    resolved = await profiles.resolve_client(db, client_id)
    tags = (
        authz.scope_from_rule(resolved[2]["areas"]["value"]).as_tags()
        if resolved
        else []
    )
    return {"success": True, "client_id": client_id, "tags": tags}


@router.post("/integration/clients/{client_id}/extend")
async def integration_extend_guest(
    request: Request, client_id: str, body: ExtendGuestRequest
):
    """Extend a guest client's session expiry.

    Body: {"hours": 2.5} (adds N hours from now) or {"expires_at": "ISO"}
    (explicit new expiry, seconds truncated). Only guest clients can be
    extended (400 otherwise). Delegates to extend_guest_expiry (shared with
    the admin PATCH /clients/{id}/extend) and returns the same payload
    (``dry_run`` included). Integration-token only.
    """
    require_integration(request)
    db: Database = request.app.state.db

    return await api_pairing.extend_guest_expiry(
        db,
        request.app.state.ha_client,
        client_id,
        hours=body.hours,
        expires_at=body.expires_at,
        dry_run=body.dry_run,
    )
