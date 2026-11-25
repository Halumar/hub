"""Setting up a device from a paired device: admin view.

* ``GET /api/pairing/onboarding``: running sessions (never the session id or
  the public key).
* ``DELETE /api/pairing/onboarding/{onboarding_id}``: cancel a session; the
  waiting device gets ``onboard_error {reason: cancelled}``.

Who may confirm is the profile rule ``setup_devices``.
"""

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from api.auth import require_admin
from services import onboarding
from services.audit_log import log_admin_action

logger = logging.getLogger("hub.api.pairing")


class OnboardingSessionEntry(BaseModel):
    id: str
    client_name: str
    platform: str | None = None
    app_version: str | None = None
    device_model: str | None = None
    created_at: str
    expires_at: str
    source_ip: str
    # Device that looked the session up: {client_id, name}.
    claimed_by: dict[str, str] | None = None


class OnboardingSessionsResponse(BaseModel):
    sessions: list[OnboardingSessionEntry]
    expires_in: int = onboarding.SESSION_SECONDS


async def list_onboarding_sessions(request: Request):
    """Running onboarding sessions (devices waiting to be set up). Admin-only."""
    require_admin(request)
    return OnboardingSessionsResponse(
        sessions=[s.admin_view() for s in onboarding.sessions()],
    )


async def cancel_onboarding_session(request: Request, onboarding_id: str):
    """Cancel a running onboarding session. Admin-only."""
    require_admin(request)
    session = onboarding.by_admin_id(onboarding_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Onboarding session not found")
    from api.ws import onboarding as onboarding_ws

    await onboarding_ws.end_session(
        session, onboarding.RESULT_CANCELLED, tv_reason=onboarding.CANCELLED
    )
    log_admin_action("onboarding_cancelled", session.id)
