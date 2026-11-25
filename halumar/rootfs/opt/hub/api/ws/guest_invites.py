"""WebSocket side of guest invitations by member devices.

* ``guest_invite_create`` / ``guest_invite_cancel``: open or withdraw the
  device's own invitation (``guest_invite`` / ``guest_invite_ended``).
  The guests a device invited are in “Set up by me”
  (``api/ws/onboarding.py``).

The guest pairs with the normal ``pair`` (``api/ws/handshake.py``), which
calls :func:`guest_from_invite` and :func:`announce_guest`. State and rules:
``services/guest_invites.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from fastapi import WebSocket

from api import ws as api_ws
from api.ws.broadcast import EventAudience
from models.database import Database
from services import client_activity, guest_invites, tasks
from services.audit_log import log_admin_action

logger = logging.getLogger("hub.api.websocket")


async def send_error(websocket: WebSocket, reason: str, **echo: str | None) -> None:
    message = {
        "type": "guest_invite_error",
        "reason": reason,
        "message": guest_invites.MESSAGES.get(reason, reason),
    }
    message.update({k: v for k, v in echo.items() if v})
    with contextlib.suppress(Exception):
        await websocket.send_json(message)


def _ip(websocket: WebSocket | None) -> str:
    client = getattr(websocket, "client", None)
    return getattr(client, "host", None) or "unknown"


async def end_invite(
    invite: guest_invites.Invite,
    result: str,
    *,
    client_id: str | None = None,
    client_name: str | None = None,
    taken: bool = False,
) -> bool:
    """Remove an invitation, tell the inviting device and admins.

    ``taken``: the caller already took it out of the store (pairing). A PIN
    that ended without a guest is remembered like a dead admin PIN, so a late
    guest gets ``pin_expired`` instead of ``pin_invalid``.
    """
    if not taken and not guest_invites.remove(invite):
        return False
    if result != guest_invites.RESULT_PAIRED:
        from api.pairing import pin as pin_store

        pin_store._retire_pin(
            invite.pin,
            pin_store.REASON_TOO_MANY_FAILURES
            if result == guest_invites.RESULT_INVALIDATED
            else pin_store.REASON_PIN_EXPIRED,
        )
    message: dict = {
        "type": "guest_invite_ended",
        "invite_id": invite.id,
        "result": result,
    }
    data: dict = {"id": invite.id, "result": result}
    if client_id:
        message.update({"client_id": client_id, "client_name": client_name or ""})
        data["client_id"] = client_id
    await api_ws.send_to_client(invite.client_id, message)
    await api_ws.broadcast(
        {"type": "guest_invite_closed", "data": data},
        audience=EventAudience.ADMIN,
    )
    logger.info(
        "Guest invitation %s of %s ended: %s", invite.id, invite.client_id, result
    )
    return True


async def end_invites_of(
    client_id: str, result: str = guest_invites.RESULT_REVOKED
) -> None:
    """End the open invitation of a device (right removed, device removed)."""
    invite = guest_invites.invite_of(client_id)
    if invite:
        await end_invite(invite, result)


async def _expire_later(invite: guest_invites.Invite) -> None:
    try:
        await asyncio.sleep(max(0.0, invite.expires_at - time.time()))
    except asyncio.CancelledError:
        return
    invite.task = None
    await end_invite(invite, guest_invites.RESULT_EXPIRED)


async def handle_create(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    from services import onboarding

    row = await guest_invites.device_row(db, client_id)
    if not await guest_invites.may_invite(db, client_id):
        await send_error(websocket, guest_invites.NOT_PERMITTED)
        return
    if not guest_invites.create_allowed(client_id):
        log_admin_action("guest_invite_rate_limited", client_id, _ip(websocket))
        await send_error(websocket, guest_invites.RATE_LIMITED)
        return
    try:
        choice = await guest_invites.choose(db, client_id, message)
    except onboarding.SetupError as e:
        reason = (
            e.reason
            if e.reason in guest_invites.MESSAGES
            else guest_invites.NOT_PERMITTED
        )
        await send_error(websocket, reason)
        return
    grant = await guest_invites.grant(db, choice)
    if not grant["all_areas"] and not grant["area_names"]:
        await send_error(websocket, guest_invites.NO_GUEST_AREAS)
        return

    # One open invitation per device: a new one replaces it.
    previous = guest_invites.invite_of(client_id)
    if previous:
        await end_invite(previous, guest_invites.RESULT_REPLACED)

    from api import pairing as api_pairing
    from api.pairing import pin as pin_store

    active = pin_store.get_active_pin()
    while True:
        pin = api_pairing._generate_pin()
        if not guest_invites.pin_in_use(pin) and not (active and active[0] == pin):
            break
    invite = guest_invites.create(
        client_id=client_id,
        inviter_name=row.get("name") or "",
        pin=pin,
        profile_id=choice.profile["id"],
        guest_expires_at=choice.guest_expires_at,
        remote=choice.remote,
        owner_label=choice.owner_label,
        access_windows=choice.access_windows,
    )
    invite.task = tasks.spawn(_expire_later(invite), name="guest-invite-expiry")

    from services.pairing_link import build_pairing_deep_link, resolve_lan_base_url

    base_url = await resolve_lan_base_url(websocket.app, db)
    from services import home_networks

    nets = await home_networks.cidrs(db)
    log_admin_action(
        "guest_invite_created", f"{invite.id} by {client_id}", _ip(websocket)
    )
    from services import event_log

    await event_log.guest_invited(
        db,
        by_client_id=client_id,
        actor=row.get("name") or None,
        profile_id=choice.profile["id"],
    )
    # No part of the PIN in the log: half of it halves the guessing work.
    logger.info("Guest invitation %s opened by %s", invite.id, client_id)
    await api_ws.broadcast(
        {
            "type": "guest_invite_opened",
            "data": {
                "id": invite.id,
                "invited_by": invite.invited_by(),
                "expires_at": guest_invites.iso(invite.expires_at),
            },
        },
        audience=EventAudience.ADMIN,
    )
    await websocket.send_json(
        {
            "type": "guest_invite",
            "invite_id": invite.id,
            "pin": pin,
            "deep_link": build_pairing_deep_link(base_url, pin, nets),
            "url": base_url,
            "expires_in": guest_invites.INVITE_SECONDS,
            "expires_at": guest_invites.iso(invite.expires_at),
            "grant": grant,
        }
    )


async def handle_cancel(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    invite_id = message.get("invite_id")
    if not isinstance(invite_id, str) or not invite_id:
        await send_error(websocket, guest_invites.INVALID_REQUEST)
        return
    invite = guest_invites.by_id(invite_id)
    if invite is None or invite.client_id != client_id:
        await send_error(websocket, guest_invites.INVITE_NOT_FOUND, invite_id=invite_id)
        return
    log_admin_action(
        "guest_invite_cancelled", f"{invite.id} by {client_id}", _ip(websocket)
    )
    # end_invite answers this device (all its connections) with guest_invite_ended.
    await end_invite(invite, guest_invites.RESULT_CANCELLED)


# ── Pairing of the guest (called from the handshake) ────────────────────────


async def guest_from_invite(
    db: Database, invite: guest_invites.Invite, platform: str | None = None
) -> dict | None:
    """Take a matched invitation for a pairing; its PIN data, or None.

    The invitation is used up here (before the first await). The inviting
    device must still have the right (fail closed); otherwise the invitation
    ends as revoked and the PIN does not work.
    """
    if not guest_invites.remove(invite):
        return None
    if not await guest_invites.may_invite(db, invite.client_id):
        await end_invite(invite, guest_invites.RESULT_REVOKED, taken=True)
        return None
    # The choice is checked again, on the platform the device reports: the
    # member's rights or the profile may have changed in the meantime (fail
    # closed).
    from services import onboarding

    message = {
        "profile_id": invite.profile_id,
        "expires_at": invite.guest_expires_at,
        "remote": invite.remote,
        "owner_label": invite.owner_label,
        "access_windows": invite.access_windows,
    }
    try:
        choice = await guest_invites.choose(
            db,
            invite.client_id,
            {k: v for k, v in message.items() if v is not None},
            platform or "ios",
        )
    except onboarding.SetupError:
        await end_invite(invite, guest_invites.RESULT_REVOKED, taken=True)
        return None
    from services.expiry import expiry_from_hours
    from services.profiles import store

    expires = choice.guest_expires_at or expiry_from_hours(
        await store.guest_default_hours(db)
    )
    return {
        "is_guest": True,
        "guest_expires_at": expires,
        "profile_id": choice.profile["id"],
        "kind": choice.profile.get("kind") or "guest",
        "owner_label": choice.owner_label,
        "overrides": choice.overrides() or None,
        "set_up_by_client_id": invite.client_id,
        "set_up_by_name": invite.inviter_name,
    }


async def announce_guest(
    db: Database,
    websocket: WebSocket,
    invite: guest_invites.Invite,
    guest_id: str,
    guest_name: str,
) -> None:
    """A guest paired with ``invite``: activity, audit, the member's live status."""
    await client_activity.record(
        db,
        invite.client_id,
        client_activity.GUEST_INVITED,
        {
            "guest_client_id": guest_id,
            "guest_client_name": guest_name,
            "kind": "guest",
        },
    )
    log_admin_action(
        "guest_invite_paired",
        f"{invite.id} guest={guest_id} invited_by={invite.client_id}",
        _ip(websocket),
    )
    await end_invite(
        invite,
        guest_invites.RESULT_PAIRED,
        client_id=guest_id,
        client_name=guest_name,
        taken=True,
    )


async def end_invalidated(invites: list[guest_invites.Invite]) -> None:
    for invite in invites:
        logger.warning("Guest invitation %s invalidated after failed PINs", invite.id)
        log_admin_action("guest_invite_invalidated", invite.id)
        await end_invite(invite, guest_invites.RESULT_INVALIDATED)
