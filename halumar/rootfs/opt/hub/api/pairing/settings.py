"""Global settings: HA URLs, app URL, notification options, home screen."""

import logging
import os
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api import pairing as api_pairing
from api import ws as api_ws
from api import auth as api_auth
from api.auth import require_admin, require_ingress
from models.database import Database
from services.image_store import remove_image, replace_image

logger = logging.getLogger("hub.api.pairing")


# Home-screen background images live here (same dir api/areas.py uses).
IMAGE_DIR = "/data/images"


def _safe_remove_image(path: str | None) -> None:
    """Remove an image file only if it resolves inside IMAGE_DIR.

    The stored path comes from a DB setting, so a poisoned setting must not
    delete arbitrary files via '../' (services.image_store.remove_image
    realpath-checks the containment).
    """
    remove_image(path, api_pairing.IMAGE_DIR)


class HAUrlsResponse(BaseModel):
    """Global HA URL settings."""

    ha_url: str | None = Field(
        None, description="External HA URL (e.g. https://xxx.ui.nabu.casa)"
    )
    ha_internal_url: str | None = Field(
        None, description="Internal HA URL (e.g. http://198.51.100.178:8123)"
    )
    ha_url_detected: str | None = Field(
        None, description="Auto-detected external URL from HA"
    )
    ha_internal_url_detected: str | None = Field(
        None, description="Auto-detected internal URL from HA"
    )


class HAUrlsUpdate(BaseModel):
    """Update global HA URL settings."""

    ha_url: str | None = Field(
        None,
        description="External HA URL (empty string to clear/reset to auto-detected)",
    )
    ha_internal_url: str | None = Field(
        None,
        description="Internal HA URL (empty string to clear/reset to auto-detected)",
    )


# ── Global HA URL Settings ───────────────────────────────────────────────────


async def get_ha_urls(request: Request):
    """Get global HA URL settings. Admin-only.

    Returns both the configured URLs and the auto-detected URLs from HA.
    If no manual override is set, the auto-detected URL is used.
    """
    require_admin(request)
    db: Database = request.app.state.db

    ha_url = await db.get_setting("ha_url")
    ha_internal_url = await db.get_setting("ha_internal_url")
    ha_url_detected = await db.get_setting("ha_url_detected")
    ha_internal_url_detected = await db.get_setting("ha_internal_url_detected")

    return HAUrlsResponse(
        ha_url=ha_url,
        ha_internal_url=ha_internal_url,
        ha_url_detected=ha_url_detected,
        ha_internal_url_detected=ha_internal_url_detected,
    )


async def update_ha_urls(request: Request, data: HAUrlsUpdate):
    """Update global HA URL settings. Admin-only.

    Empty string resets to auto-detected value (removes manual override).
    These URLs are returned to the paired clients via get_client_config (the
    external one only to devices with remote access); connected clients are
    told to fetch it. A value that is not an http(s) URL (or carries
    credentials, a query or a fragment) is refused with 422.
    """
    require_admin(request)
    db: Database = request.app.state.db

    from services.ha_url import normalize_external_url

    # Only plain http(s) URLs (no credentials, query, fragment): the apps send
    # their HA token there.
    for name in ("ha_url", "ha_internal_url"):
        value = getattr(data, name)
        if value and normalize_external_url(value) is None:
            raise HTTPException(
                status_code=422,
                detail=f"{name} must be an http(s) URL without credentials, "
                "query or fragment",
            )

    if data.ha_url is not None:
        if data.ha_url:
            await db.set_setting("ha_url", normalize_external_url(data.ha_url))
        else:
            # Empty string = reset to auto-detected
            detected = await db.get_setting("ha_url_detected")
            if detected:
                await db.set_setting("ha_url", detected)
            else:
                # No detected value either, remove the setting
                async with db.transaction():
                    await db.execute("DELETE FROM settings WHERE key = 'ha_url'")

    if data.ha_internal_url is not None:
        if data.ha_internal_url:
            await db.set_setting(
                "ha_internal_url", normalize_external_url(data.ha_internal_url)
            )
        else:
            # Empty string = reset to auto-detected
            detected = await db.get_setting("ha_internal_url_detected")
            if detected:
                await db.set_setting("ha_internal_url", detected)
            else:
                async with db.transaction():
                    await db.execute(
                        "DELETE FROM settings WHERE key = 'ha_internal_url'"
                    )

    logger.info("Updated global HA URLs")

    # Return current state
    ha_url = await db.get_setting("ha_url")
    ha_internal_url = await db.get_setting("ha_internal_url")
    ha_url_detected = await db.get_setting("ha_url_detected")
    ha_internal_url_detected = await db.get_setting("ha_internal_url_detected")

    # Refresh CORS origin cache so new HA URLs take effect immediately
    request.app.state._cors_ha_url = ha_url or ""
    request.app.state._cors_ha_internal_url = ha_internal_url or ""
    # Connected apps fetch their config with the new URLs.
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})

    return HAUrlsResponse(
        ha_url=ha_url,
        ha_internal_url=ha_internal_url,
        ha_url_detected=ha_url_detected,
        ha_internal_url_detected=ha_internal_url_detected,
    )


# ── App URL Setting ──────────────────────────────────────────────────────────


class AppUrlResponse(BaseModel):
    """App URL setting response."""

    app_url: str | None = Field(
        None,
        description="Base URL under which the hub is reachable externally (e.g. https://hub.example.com)",
    )


class AppUrlUpdate(BaseModel):
    """Update app URL setting."""

    app_url: str | None = Field(
        None,
        description="Base URL (empty string to clear). Used for QR codes, image URLs, and mDNS.",
    )


async def get_app_url(request: Request):
    """Get the configured app URL. Admin-only.

    The app URL is the base URL under which Halumar is reachable externally.
    Used for QR codes, image URLs sent to clients, and mDNS TXT records.
    If not set, auto-detection from request headers is used as fallback.
    """
    require_admin(request)
    db: Database = request.app.state.db
    app_url = await db.get_setting("app_url")
    return AppUrlResponse(app_url=app_url)


async def update_app_url(request: Request, data: AppUrlUpdate):
    """Update the app URL setting. Admin-only.

    Empty string clears the setting (reverts to auto-detection).
    The change takes effect immediately for QR codes and image URLs.
    """
    require_admin(request)
    db: Database = request.app.state.db

    if data.app_url is not None:
        clean_url = (data.app_url or "").strip().rstrip("/")
        if clean_url:
            await db.set_setting("app_url", clean_url)
            request.app.state.app_url = clean_url
            logger.info("App URL set to: %s", clean_url)
        else:
            async with db.transaction():
                await db.execute("DELETE FROM settings WHERE key = 'app_url'")
            request.app.state.app_url = ""
            logger.info("App URL cleared (using auto-detection)")
        # The apps take image_base_url from the area list.
        await api_ws.notify_areas_changed(db)

    app_url = await db.get_setting("app_url")
    return AppUrlResponse(app_url=app_url)


# ── Control Mode Setting ─────────────────────────────────────────────────────


class ControlModeSettingsUpdate(BaseModel):
    """Update global default control mode."""

    control_mode: str = Field(
        "full",
        description='Global default control mode: "basic", "advanced", or "full"',
    )

    @field_validator("control_mode")
    @classmethod
    def validate_control_mode(cls, v):
        if v not in ("basic", "advanced", "full"):
            raise ValueError('Must be "basic", "advanced", or "full"')
        return v


async def get_control_mode_setting(request: Request):
    """Get global default control mode. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db
    mode = await db.get_setting("default_control_mode")
    return {"control_mode": mode or "full"}


async def update_control_mode_setting(
    request: Request, data: ControlModeSettingsUpdate
):
    """Update global default control mode. Admin-only.

    This is the fallback used by the apps when neither the entity
    nor the area has a control_mode override.
    """
    require_admin(request)
    db: Database = request.app.state.db
    await db.set_setting("default_control_mode", data.control_mode)
    logger.info("Updated default control mode to: %s", data.control_mode)

    # Broadcast so all connected clients refresh their config
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})

    return {"control_mode": data.control_mode}


# ── Pairing via Integration Setting ──────────────────────────────────────────


class PairingViaIntegrationUpdate(BaseModel):
    """Update whether the integration may start pairing windows."""

    allow_pairing_via_integration: bool = True


async def get_pairing_via_integration_setting(request: Request):
    """Get whether pairing can be started via the integration. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db
    value = await db.get_setting("allow_pairing_via_integration")
    return {"allow_pairing_via_integration": value != "false"}


async def update_pairing_via_integration_setting(
    request: Request, data: PairingViaIntegrationUpdate
):
    """Update whether pairing can be started via the integration. Admin-only.

    When disabled, POST /api/integration/start-pairing returns 403 for both
    user and guest pairings (the web UI stays the only pairing surface);
    cancelling an open window via the integration remains allowed.
    """
    require_admin(request)
    db: Database = request.app.state.db
    await db.set_setting(
        "allow_pairing_via_integration",
        "true" if data.allow_pairing_via_integration else "false",
    )
    logger.info(
        "Updated allow pairing via integration to: %s",
        data.allow_pairing_via_integration,
    )

    # Broadcast so admin UIs (and the integration push session) refresh
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})

    return {"allow_pairing_via_integration": data.allow_pairing_via_integration}


# ── Compliance Endpoint Setting ──────────────────────────────────────────────


class ComplianceEndpointUpdate(BaseModel):
    """Update whether the token-less compliance endpoint is served."""

    compliance_endpoint_enabled: bool = True


async def get_compliance_endpoint_setting(request: Request):
    """Get whether the compliance endpoint is enabled. Admin-only."""
    require_admin(request)
    db: Database = request.app.state.db
    value = await db.get_setting("compliance_endpoint_enabled")
    return {"compliance_endpoint_enabled": value != "false"}


async def update_compliance_endpoint_setting(
    request: Request, data: ComplianceEndpointUpdate
):
    """Update whether the compliance endpoint is enabled. Admin-only.

    When disabled, GET /api/integration/status omits all per-client
    compliance blobs and tombstones, and the integration's unauthenticated
    compliance view serves the neutral "unknown" body for every client.
    """
    require_admin(request)
    db: Database = request.app.state.db
    await db.set_setting(
        "compliance_endpoint_enabled",
        "true" if data.compliance_endpoint_enabled else "false",
    )
    logger.info(
        "Updated compliance endpoint enabled to: %s",
        data.compliance_endpoint_enabled,
    )

    # Broadcast so admin UIs (and the integration push session) refresh
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})

    return {"compliance_endpoint_enabled": data.compliance_endpoint_enabled}


# ── Direct Admin Access Setting ──────────────────────────────────────────────


class AdminDirectAccessUpdate(BaseModel):
    """Switch the admin page on :8100 outside Home Assistant on or off."""

    admin_direct_access: bool


async def get_admin_direct_access_setting(request: Request):
    """Whether admins may also log in directly in the home network. Admin-only."""
    require_admin(request)
    return {"admin_direct_access": api_auth.direct_admin_enabled()}


async def update_admin_direct_access_setting(
    request: Request, data: AdminDirectAccessUpdate
):
    """Switch direct admin access (HA login on :8100). Ingress only.

    Off (the default): the login and every direct session are refused, the
    admin page opens only in the Home Assistant sidebar. Switching off ends
    all direct sessions and closes their admin sockets (4401). Paired apps,
    the integration and the admin API token are not affected.
    """
    require_ingress(request)
    db: Database = request.app.state.db
    ended = await api_auth.set_direct_admin_enabled(db, data.admin_direct_access)
    closed = 0 if data.admin_direct_access else await api_ws.close_all_session_sockets()
    logger.info(
        "Direct admin access %s (sessions ended: %d, sockets closed: %d)",
        "enabled" if data.admin_direct_access else "disabled",
        ended,
        closed,
    )
    return {"admin_direct_access": data.admin_direct_access}


# ── App Appearance Settings ───────────────────────────────────────────────────


class HomeSettingsUpdate(BaseModel):
    """Update home screen settings."""

    home_title: str | None = Field(
        None, description="Home screen title. Empty string to clear."
    )


async def get_home_settings(request: Request):
    """Get home screen / app appearance settings. Admin-only.

    Returns the home title, background image paths, and the auto-detected
    HA location_name as a default reference.
    """
    require_admin(request)
    db: Database = request.app.state.db

    home_title = await db.get_setting("home_title")
    home_bg_light_path = await db.get_setting("home_bg_light_path")
    home_bg_dark_path = await db.get_setting("home_bg_dark_path")
    location_name = await db.get_setting("ha_location_name")

    return {
        "home_title": home_title,
        "home_bg_light": _image_src_setting(home_bg_light_path),
        "home_bg_dark": _image_src_setting(home_bg_dark_path),
        "location_name": location_name,
    }


async def update_home_settings(request: Request, data: HomeSettingsUpdate):
    """Update home screen title. Admin-only.

    Empty string clears the title (will send null to clients).
    """
    require_admin(request)
    db: Database = request.app.state.db

    if data.home_title is not None:
        # Empty string = user explicitly cleared the title (hide it).
        # Non-empty = custom title. Both are stored; null means "never set".
        await db.set_setting("home_title", data.home_title)

    logger.info("Updated home settings")

    # Broadcast to all connected clients so they can refresh their config
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})

    home_title = await db.get_setting("home_title")
    home_bg_light_path = await db.get_setting("home_bg_light_path")
    home_bg_dark_path = await db.get_setting("home_bg_dark_path")
    location_name = await db.get_setting("ha_location_name")

    return {
        "home_title": home_title,
        "home_bg_light": _image_src_setting(home_bg_light_path),
        "home_bg_dark": _image_src_setting(home_bg_dark_path),
        "location_name": location_name,
    }


async def upload_home_image(request: Request, image_type: str):
    """Upload a home screen background image. Admin-only.

    image_type: 'bg_light' or 'bg_dark'
    Accepts multipart form data with a 'file' field.
    """
    require_admin(request)

    if image_type not in ("bg_light", "bg_dark"):
        raise HTTPException(
            status_code=400, detail="Invalid image type. Use 'bg_light' or 'bg_dark'."
        )

    db: Database = request.app.state.db

    # Parse multipart form
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")

    # Validate content type
    allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image type: {file.content_type}. Allowed: {', '.join(allowed_types)}",
        )

    # Validate extension
    filename = file.filename or "upload.jpg"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    allowed_exts = {"jpg", "jpeg", "png", "webp", "gif"}
    if ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file extension: .{ext}. Allowed: {', '.join(allowed_exts)}",
        )

    # Read file (limit 10MB)
    max_size = 10 * 1024 * 1024
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail="Image too large (max 10MB)")

    setting_key = f"home_{image_type}_path"
    # Random file name (prevents guessing).
    image_filename = f"home_{uuid.uuid4().hex[:12]}_{image_type}.{ext}"

    async def persist(new_path: str) -> str | None:
        # Read the replaced path in the same transaction: concurrent uploads
        # each delete exactly the file they replaced.
        async with db.transaction():
            row = await db.fetchone(
                "SELECT value FROM settings WHERE key = ?", (setting_key,)
            )
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (setting_key, new_path),
            )
        return (row or {}).get("value")

    # New file atomically, then the setting, only then the old file goes.
    image_path = await replace_image(
        image_dir=api_pairing.IMAGE_DIR,
        filename=image_filename,
        content=content,
        old_path=None,
        persist=persist,
    )
    logger.info(
        "Uploaded home %s: %s (%d bytes)", image_type, image_filename, len(content)
    )

    # Broadcast to all connected clients
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})

    return {
        "image_type": image_type,
        "path": _image_src_setting(image_path),
    }


async def delete_home_image(request: Request, image_type: str):
    """Delete a home screen background image. Admin-only."""
    require_admin(request)

    if image_type not in ("bg_light", "bg_dark"):
        raise HTTPException(
            status_code=400, detail="Invalid image type. Use 'bg_light' or 'bg_dark'."
        )

    db: Database = request.app.state.db
    setting_key = f"home_{image_type}_path"
    async with db.transaction():
        row = await db.fetchone(
            "SELECT value FROM settings WHERE key = ?", (setting_key,)
        )
        await db.execute("DELETE FROM settings WHERE key = ?", (setting_key,))

    # Commit first, then the file (a crash leaves at most an orphan that the
    # start-up clean-up removes). Containment-checked: the path comes from a
    # DB setting, so never unlink anything outside IMAGE_DIR.
    _safe_remove_image((row or {}).get("value"))
    logger.info("Deleted home %s image", image_type)

    # Broadcast to all connected clients
    await api_ws.broadcast({"type": "client_config_updated", "data": {}})


def _image_src_setting(image_path: str | None) -> str | None:
    """Convert a filesystem path from settings to a relative URL."""
    if not image_path:
        return None
    return f"/images/{os.path.basename(image_path)}"
