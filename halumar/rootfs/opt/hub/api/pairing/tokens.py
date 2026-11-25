"""Per-device HA token rotation (admin).

Every paired device has its own HA user, created and managed by the companion
integration (services/device_provisioning.py). The hub never mints or revokes
HA tokens; a rotation asks the integration for a new token of the same user,
which it delivers to ``POST /api/integration/client-token``.
"""

import asyncio
import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api import ws as api_ws
from api.auth import require_admin
from api.pairing.pin import _get_client_ip
from models.database import HA_TOKEN_ACTIVE, Database
from services import device_provisioning
from services.audit_log import log_admin_action

logger = logging.getLogger("hub.api.pairing")

# How long the admin request waits for the integration's new token before it
# answers 202 (the rotation keeps running, see provisioning[] / retry loop).
# Only while the integration's push session is up: without it the request
# answers at once.
ROTATE_WAIT_SECONDS = 15.0

# status of a 202 answer
STATUS_REQUESTED = "rotation_requested"  # sent, the integration is working on it
STATUS_PENDING = "pending"  # integration not connected, runs once it is


class RotateTokenResponse(BaseModel):
    """200: the integration delivered the new token. 202: still running."""

    ok: bool
    client_id: str
    ha_token_status: str | None = None
    ha_token_minted_at: str | None = None
    status: str | None = None


def _accepted(client_id: str, status: str) -> JSONResponse:
    return JSONResponse(
        status_code=202,
        content=RotateTokenResponse(
            ok=True, client_id=client_id, status=status
        ).model_dump(exclude_none=True),
    )


async def rotate_client_token(request: Request, client_id: str):
    """Ask the integration for a new HA token of this device. Admin-only.

    The rotation is stored first (it survives a hub restart).

    - 200: the new token arrived within ``ROTATE_WAIT_SECONDS`` (the app was
      told via client_config_updated).
    - 202 ``rotation_requested``: sent, the integration has not answered yet.
    - 202 ``pending``: the integration is not connected; the rotation runs as
      soon as it is (``provisioning[]`` on its next poll, retry loop once its
      push session is up). Answered at once instead of blocking.
      Also for a device with remote access that is not connected to the hub
      (away): the rotation waits until it connects, at most
      ``ROTATION_DEFER_SECONDS``, so it is not locked out of Home Assistant
      on the road.
    - 409: the device is not active or has no HA token yet.
    """
    require_admin(request)
    db: Database = request.app.state.db

    row = await db.fetchone(
        "SELECT id, is_active, pending_deletion_at, ha_token_status "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Client not found")
    if not device_provisioning.is_provisionable(row):
        raise HTTPException(status_code=409, detail="Client is not active")
    if row.get("ha_token_status") != HA_TOKEN_ACTIVE:
        raise HTTPException(
            status_code=409,
            detail="The device has no HA token yet (waiting for the integration)",
        )

    from services.profiles import store as profiles

    remote = (await profiles.remote_allowed_map(db, [client_id])).get(client_id, False)
    # A watch has no hub socket: it gets its new token through its phone,
    # so the phone's connection counts.
    from services import watch_access

    reached_by = await watch_access.phone_of(db, client_id) or client_id
    away_with_remote_access = (
        remote and reached_by not in api_ws.get_connected_client_ids()
    )
    if away_with_remote_access:
        await device_provisioning.defer_rotation(db, client_id)
        log_admin_action(
            "client_token_rotation_requested",
            details=f"client_id={client_id} deferred=until_connected",
            ip=_get_client_ip(request),
        )
        logger.info(
            "Rotation for %s deferred until the device connects (remote access)",
            client_id,
        )
        return _accepted(client_id, STATUS_PENDING)

    request_id = await device_provisioning.start_rotation(db, client_id)
    log_admin_action(
        "client_token_rotation_requested",
        details=f"client_id={client_id}",
        ip=_get_client_ip(request),
    )
    if not api_ws.get_integration_push_connected():
        logger.info(
            "Rotation for %s stored, runs once the integration is connected",
            client_id,
        )
        return _accepted(client_id, STATUS_PENDING)

    waiter = device_provisioning.rotation_waiter(client_id)
    await device_provisioning.request_provision(
        db,
        client_id,
        token_action=device_provisioning.TOKEN_ACTION_ROTATE,
        reason=device_provisioning.REASON_ROTATE,
        request_id=request_id,
    )
    try:
        await asyncio.wait_for(waiter.wait(), ROTATE_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return _accepted(client_id, STATUS_REQUESTED)

    info = await db.fetchone(
        "SELECT ha_token_status, ha_token_minted_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    if not info:
        raise HTTPException(status_code=409, detail="Client is not active")
    return RotateTokenResponse(
        ok=True,
        client_id=client_id,
        ha_token_status=info.get("ha_token_status"),
        ha_token_minted_at=info.get("ha_token_minted_at"),
    )
