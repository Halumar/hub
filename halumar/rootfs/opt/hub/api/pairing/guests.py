"""Guests: member invitations (admin view), tag catalog, extending a
guest (at most guest_access.max_hours per grant)."""

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from api import ws as api_ws
from api.auth import require_admin
from api.ws import EventAudience
from models.database import Database
from models.schemas import ExtendGuestRequest
from services import device_provisioning, guest_invites
from services.audit_log import log_admin_action
from services.expiry import expiry_from_hours, normalize_expiry_input

logger = logging.getLogger("hub.api.pairing")


# ── Guest invitations of members: admin view ─────────────────────────


class GuestInviteEntry(BaseModel):
    id: str
    invited_by: dict[str, str]
    created_at: str
    expires_at: str


class GuestInvitesResponse(BaseModel):
    invites: list[GuestInviteEntry]
    expires_in: int = guest_invites.INVITE_SECONDS


async def list_guest_invites(request: Request):
    """Open guest invitations of member devices (never the PIN). Admin-only."""
    require_admin(request)
    return GuestInvitesResponse(
        invites=[i.admin_view() for i in guest_invites.open_invites()]
    )


async def cancel_guest_invite(request: Request, invite_id: str):
    """Cancel an open guest invitation; the member gets ``revoked``. Admin-only."""
    require_admin(request)
    invite = guest_invites.by_id(invite_id)
    if invite is None:
        raise HTTPException(status_code=404, detail="Guest invitation not found")
    from api.ws import guest_invites as guest_invites_ws

    await guest_invites_ws.end_invite(invite, guest_invites.RESULT_REVOKED)
    log_admin_action("guest_invite_revoked", invite.id)


async def get_all_tags(request: Request):
    """All area tags (sorted, unique) for the tag choice of the rule
    ``areas``. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db
    rows = await db.fetchall("SELECT DISTINCT tag FROM area_tags ORDER BY tag")
    return {"tags": [row["tag"] for row in rows]}


# ── Guest Management ─────────────────────────────────────────────────────────


async def extend_guest_expiry(
    db: Database,
    ha_client,
    client_id: str,
    *,
    hours: float | None = None,
    expires_at: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Extend a guest client's expiry time (shared admin/integration core).

    Either provide ``hours`` (adds N hours from now) or ``expires_at``
    (sets an explicit new expiry timestamp, seconds truncated). Only works
    on guest clients (400 otherwise; 404 for unknown clients). Reports the new
    expiry to the integration (update_client_identity), notifies
    the client (guest_extended), broadcasts to admins and re-syncs the
    guests sensor. Called by the admin PATCH /clients/{id}/extend and by
    POST /integration/clients/{id}/extend (api/integration.py).

    The write goes through ``changes.run`` like every profile change: the
    guest's rules, its key (an expired guest gets it back), the rule
    snapshot and its client_config follow at once. ``dry_run`` writes
    nothing and answers ``{"dry_run": true, "expires_at", "hub_remote_now",
    "hub_remote_at"}`` for the state after the extension (as in previews).

    Returns {"client_id", "expires_at", "hub_remote_now", "hub_remote_at"}.
    """
    client = await db.fetchone(
        "SELECT id, access, expires_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    if client.get("access") != "guest":
        raise HTTPException(
            status_code=400, detail="Only guest clients can be extended"
        )

    # Callers pass values validated by ExtendGuestRequest; normalizing again
    # keeps this shared core safe for any other caller.
    if expires_at:
        try:
            new_expiry = normalize_expiry_input(expires_at)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    elif hours:
        new_expiry = expiry_from_hours(hours)
    else:
        raise HTTPException(status_code=400, detail="Provide 'hours' or 'expires_at'")

    from services.profiles import changes
    from services.profiles import store

    async def _pair_after() -> tuple[bool, str | None]:
        row = await store.client_row(db, client_id)
        prof = await store.profile_row(db, row["profile_id"])
        return store.hub_remote_schedule(
            {**row, "expires_at": new_expiry},
            prof,
            await store.overrides_of(db, client_id),
        )

    if dry_run:
        hub_now, hub_at = await _pair_after()
        return {
            "dry_run": True,
            # The canonical stored form (utc_expiry), like the real answer.
            "expires_at": new_expiry,
            "hub_remote_now": hub_now,
            "hub_remote_at": hub_at,
        }

    async def _write() -> None:
        await db.execute(
            "UPDATE paired_clients SET expires_at = ? WHERE id = ?",
            (new_expiry, client_id),
        )

    _, change = await changes.run(db, _write, dry_run=False)
    await changes.apply_effects(db, change, touched=[client_id])
    hub_now, hub_at = await _pair_after()

    logger.info("Extended guest %s to %s", client_id, new_expiry)

    await changes.audit(
        db,
        "guest_extended",
        actor=None,
        client_id=client_id,
        before={"expires_at": client.get("expires_at")},
        after={"expires_at": new_expiry},
    )

    # The integration bounds a guest's HA token to its expiry; it decides
    # whether the token still covers the new one and rotates it if not.
    # Sent once: apply_effects already sent it when remote access changed.
    if change.remote.get(client_id) not in ("enabled", "disabled"):
        await device_provisioning.send_identity_update(db, client_id)

    # Notify the connected guest client about the new expiry
    try:
        await api_ws.send_to_client(
            client_id,
            {
                "type": "guest_extended",
                "expires_at": new_expiry,
            },
        )
    except Exception as e:
        logger.warning("Failed to notify client about extension: %s", e)

    # Broadcast to admin clients so the UI updates
    try:
        await api_ws.broadcast(
            {
                "type": "guest_extended",
                "data": {
                    "client_id": client_id,
                    "expires_at": new_expiry,
                },
            },
            audience=EventAudience.ADMIN,
        )
    except Exception as e:
        logger.warning("Failed to broadcast guest extension: %s", e)

    # Sync guests sensor
    try:
        from services.ha_sensors import sync_guests_sensor

        await sync_guests_sensor(db, ha_client)
    except Exception as e:
        logger.warning("Failed to sync guests sensor after extend: %s", e)

    return {
        "client_id": client_id,
        "expires_at": new_expiry,
        "hub_remote_now": hub_now,
        "hub_remote_at": hub_at,
    }


async def extend_guest(request: Request, client_id: str, data: ExtendGuestRequest):
    """Extend a guest client's expiry time. Admin-only.

    Either provide 'hours' (adds N hours from now) or 'expires_at'
    (sets an explicit new expiry timestamp). Only works on guest clients.
    Delegates to extend_guest_expiry (shared with the integration endpoint).
    """
    require_admin(request)
    return await extend_guest_expiry(
        request.app.state.db,
        request.app.state.ha_client,
        client_id,
        hours=data.hours,
        expires_at=data.expires_at,
        dry_run=data.dry_run,
    )
