"""Add-on observability/admin API endpoints. All endpoints are admin-only.

These talk to the Home Assistant Supervisor (via the SUPERVISOR_TOKEN-backed
HAClient) to expose hub logs and a self-restart trigger. They require
``hassio_api: true`` (and an appropriate ``hassio_role``) in config.yaml.
"""

import asyncio
import json
import logging
import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api import ws as api_ws
from api.auth import require_admin
from namespace import HA_DOMAIN
from services import audit_log, integration_status, tls
from services.audit_log import log_admin_action
from version import VERSION

logger = logging.getLogger("hub.api.admin")
router = APIRouter(tags=["admin"])


@router.get("/admin/logs", summary="Get hub logs")
async def get_addon_logs(request: Request, lines: int = 200):
    """Return recent hub log lines (plain text, newest last). Admin-only.

    Proxies the Supervisor ``/addons/self/logs`` endpoint and trims the
    output to the last ``lines`` lines. If the Supervisor is unreachable the
    error message is returned in the ``logs`` field so the UI can surface it.
    """
    require_admin(request)
    ha_client = request.app.state.ha_client

    # Clamp to a sane range (1..5000) to avoid abuse / empty responses.
    lines = max(1, min(lines, 5000))

    try:
        logs = await ha_client.get_addon_logs(lines)
    except Exception as e:
        logger.error("Failed to fetch add-on logs from Supervisor: %s", e)
        return {"logs": f"Failed to fetch logs from Supervisor: {e}"}

    return {"logs": logs}


@router.post("/admin/restart", summary="Restart the hub")
async def restart_addon(request: Request):
    """Trigger a hub restart via the Supervisor. Admin-only."""
    require_admin(request)
    ha_client = request.app.state.ha_client

    try:
        await ha_client.restart_addon()
    except Exception as e:
        logger.error("Failed to restart add-on via Supervisor: %s", e)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to restart add-on via Supervisor: {e}",
        )

    return {"ok": True}


@router.get("/admin/integration-status")
async def get_integration_status(request: Request):
    """Connection telemetry for the companion HA integration. Admin-only.

    In-memory tracking (reset on restart) fed by every authenticated
    /api/integration/* request — see services/integration_status.py.
    ``connected`` = last authenticated request younger than 90s;
    ``last_request_ts`` is Unix epoch seconds (null = never seen);
    ``push_connected`` = the integration's WS push session is live right now.
    """
    require_admin(request)
    return {
        **integration_status.get_status(),
        "push_connected": api_ws.get_integration_push_connected(),
        "addon_version": VERSION,
        "ha_version": await _ha_version(request),
        "ha_domain": HA_DOMAIN,
    }


async def _ha_version(request: Request) -> str | None:
    """Home Assistant version from GET /api/config (Supervisor proxy), or None."""
    app = getattr(request, "app", None)
    ha_client = getattr(getattr(app, "state", None), "ha_client", None)
    if ha_client is None:
        return None
    try:
        config = await asyncio.wait_for(ha_client.get_config(), 5)
    except Exception as e:  # noqa: BLE001 - informational only
        logger.debug("HA version unavailable: %s", e)
        return None
    version = config.get("version") if isinstance(config, dict) else None
    return version if isinstance(version, str) else None


# ── Encryption / TLS ─────────────────────────────────────────────────────────
#
# Surfaces the self-signed TLS state to the admin Web-UI so an
# operator can read the pinned SHA-256 fingerprint out-of-band and rotate the
# cert. The private key never leaves the service layer — only the public cert
# summary is exposed here.


def _read_tls_enabled() -> bool:
    """Read the ``tls_enabled`` app option the same way ``main.py`` does.

    Defaults to ``False`` (and on any read error) so the UI degrades to the
    unchanged plain-HTTP assumption rather than falsely claiming TLS is on.
    """
    try:
        options_path = "/data/options.json"
        if os.path.isfile(options_path):
            with open(options_path) as f:
                options = json.load(f)
            return bool(options.get("tls_enabled", False))
    except Exception as e:
        logger.warning("Could not read options.json for TLS setting: %s", e)
    return False


@router.get("/settings/tls")
async def get_tls_settings(request: Request):
    """Return the current TLS state for the admin UI. Admin-only.

    Exposes the hub's ``tls_enabled`` option, the external TLS port, the pinned
    fingerprint, and a public cert summary. Never returns the private key or
    its path. ``fingerprint``/``cert`` are ``None`` when no cert exists yet.
    """
    # fingerprint: the cert the running server presents; cert: the cert on
    # disk; pending_fingerprint: a regenerated cert served after the restart.
    require_admin(request)
    return {
        "tls_enabled": _read_tls_enabled(),
        "tls_port": tls.TLS_PORT,
        "fingerprint": tls.get_fingerprint(),
        "pending_fingerprint": tls.pending_fingerprint(),
        "cert": tls.cert_info(),
    }


@router.post("/settings/tls/regenerate")
async def regenerate_tls_cert(request: Request):
    """Rotate the self-signed TLS cert and return the new fingerprint. Admin-only.

    Adds the detected LAN IP to the cert SAN (127.0.0.1 is added by the service
    itself, so loopback is skipped). Does NOT auto-restart: the running TLS
    server keeps serving the old cert until the operator restarts the hub,
    and every paired client must re-pair against the new fingerprint.
    """
    require_admin(request)

    # Match main.py's dual-server path: SAN in the detected LAN IP, skip loopback.
    from services.mdns import _get_local_ip

    local_ip = _get_local_ip()
    extra_ips = [local_ip] if local_ip and local_ip != "127.0.0.1" else []

    try:
        fingerprint = tls.regenerate(extra_ips=extra_ips)
    except Exception as e:
        logger.error("Failed to regenerate TLS cert: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to regenerate TLS certificate: {e}",
        )

    logger.info("TLS cert regenerated by admin (new fingerprint %s)", fingerprint)
    return {
        "fingerprint": fingerprint,
        "restart_required": True,
        "message": (
            "Certificate regenerated. Restart the add-on to serve the new "
            "cert; all paired clients must re-pair."
        ),
    }


# ── Setup assistant ────────────────────────────────────────────────
#
# Progress of the admin's first-run assistant. Whether a step is done is
# derived by the UI from the live data (integration status, URLs, areas,
# devices); the hub only remembers where the admin is, which steps were
# skipped and whether the assistant was finished or put aside, so it opens by
# itself once on a fresh hub and not again in another browser.

SETUP_ASSISTANT_SETTING = "setup_assistant"
SetupStep = Literal["integration", "connection", "urls", "area", "device", "done"]


class SetupAssistantState(BaseModel):
    """``status``: new (never opened), in_progress, dismissed, completed."""

    model_config = {"extra": "forbid"}

    status: Literal["new", "in_progress", "dismissed", "completed"] = "new"
    step: SetupStep = "integration"
    skipped: list[SetupStep] = Field(default_factory=list, max_length=6)


async def _load_setup_assistant(db) -> SetupAssistantState:
    raw = await db.get_setting(SETUP_ASSISTANT_SETTING)
    if not raw:
        return SetupAssistantState()
    try:
        return SetupAssistantState.model_validate_json(raw)
    except ValueError:
        logger.warning("Stored setup assistant state is invalid; starting over")
        return SetupAssistantState()


@router.get("/admin/setup-assistant", response_model=SetupAssistantState)
async def get_setup_assistant(request: Request):
    """Progress of the setup assistant. Admin-only."""
    require_admin(request)
    return await _load_setup_assistant(request.app.state.db)


@router.put("/admin/setup-assistant", response_model=SetupAssistantState)
async def update_setup_assistant(request: Request, data: SetupAssistantState):
    """Store the progress of the setup assistant. Admin-only."""
    require_admin(request)
    db = request.app.state.db
    state = data.model_copy(update={"skipped": list(dict.fromkeys(data.skipped))})
    previous = await _load_setup_assistant(db)
    await db.set_setting(SETUP_ASSISTANT_SETTING, state.model_dump_json())
    if state.status != previous.status:
        log_admin_action("setup_assistant", f"status={state.status} step={state.step}")
    return state


# ── Audit view ─────────────────────────────────────────────────────
#
# The newest admin-relevant audit entries since the hub started (in-memory
# ring buffer of services/audit_log.py); the hub log keeps everything.

AuditCategory = Literal["action", "change", "read", "security"]


class AuditEntry(BaseModel):
    id: int
    ts: str
    event: str
    category: AuditCategory
    level: Literal["info", "warning"]
    action: str | None = None
    details: str | None = None
    auth: str | None = None
    user: str | None = None
    client_id: str | None = None
    ip: str | None = None


class AuditResponse(BaseModel):
    max_entries: int
    entries: list[AuditEntry]


@router.get("/admin/audit", response_model=AuditResponse)
async def get_audit(
    request: Request,
    limit: int = Query(200, ge=1, le=audit_log.AUDIT_BUFFER_SIZE),
    category: list[AuditCategory] | None = Query(
        None, description="Only these categories (repeat the parameter)"
    ),
):
    """Newest audit entries first (admin actions, admin requests, security
    events) since the hub started. Admin-only."""
    require_admin(request)
    return AuditResponse(
        max_entries=audit_log.AUDIT_BUFFER_SIZE,
        entries=audit_log.recent_entries(limit, set(category) if category else None),
    )
