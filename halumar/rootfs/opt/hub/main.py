"""Halumar Hub: gives every device in your home its own, limited view of
Home Assistant."""

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import re
import signal
import sys
import time
from contextlib import asynccontextmanager, nullcontext
from urllib.parse import urlparse

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from api.admin import router as admin_router
from api.dry_run import reject_unsupported_dry_run
from api.family import router as family_router
from api.panel import router as panel_router
from api.profiles import router as profiles_router
from api.home_networks import router as home_networks_router
from api.room_climate import router as room_climate_router
from api.scan_tags import router as scan_tags_router
from api.access_requests import router as access_requests_router
from api.log import router as log_router
from services.profiles.expiry_task import expiry_loop as profile_expiry_loop
from api.areas import router as areas_router
from api.tiles import router as tiles_router
from api.quick_access import router as quick_access_router
from api.auth import HEAD_ASSET_PATHS, AuthMiddleware, router as auth_router
from api.entities import router as entities_router
from brand import BRAND_NAME
from version import VERSION, BUILD
from api.export import router as export_router
from api.integration import router as integration_router
from api.notifications import router as notifications_router
from api.pairing import router as pairing_router
from api.sync import router as sync_router
from api.ws.handshake import MAX_WS_MESSAGE_SIZE
from api.websocket import (
    router as ws_router,
    ws_ping_loop,
    guest_expiry_loop,
    get_connected_count,
    get_integration_push_connected,
)
from models.database import Database, DatabaseUnavailableError
from namespace import HA_NOTIFICATION_ID_DATA_SAFETY
from services import db_snapshots, protocol, tasks
from services.crypto import KeyfileError, check_keyfile
from services.ha_client import HAClient
from services.ha_sensors import attach_live_updates, reconcile_loop
from services.ha_ws import HAWebSocket
from services.image_store import cleanup_orphan_images
from services.log_redaction import configure_logging
from services.mdns import start_mdns, stop_mdns
from services import csp, static_compress
from services.tls import (
    enforce_tls13_minimum,
    ensure_cert,
    get_fingerprint,
    mark_served,
    TLS_PORT,
)

logger = logging.getLogger("hub")

# Process start time (monotonic) for /api/health uptime reporting.
_START_TIME = time.monotonic()

# Global instances
db: Database | None = None
ha_client: HAClient | None = None


# Request body limits, chosen per request before the body is
# read: a small one for everything a peer can send without credentials, a
# larger one for authenticated JSON, and the large one only for the admin
# routes that carry files (images are limited to 10 MB in the handler) or an
# import.
SMALL_BODY_LIMIT = 64 * 1024
AUTHENTICATED_BODY_LIMIT = 1024 * 1024
MAX_REQUEST_BODY_SIZE = 16 * 1024 * 1024
LARGE_BODY_ROUTES = re.compile(
    r"^/api/(areas/[^/]+/image/(card|bg-light|bg-dark)"
    r"|settings/home/image/[^/]+|import/areas)$"
)
# The export of a full hub (100 areas x 20 sections x 50 entities, the most
# the writers allow) is about 35 MiB; the import takes it again.
IMPORT_BODY_LIMIT = 64 * 1024 * 1024
# The whole body must arrive within this time (a slow drip would otherwise
# hold the connection and its buffer open without end); uvicorn has no
# body-read timeout of its own (timeout_keep_alive only covers idle time).
SMALL_BODY_SECONDS = 15
LARGE_BODY_SECONDS = 120
# Auth types (AuthMiddleware) that may send more than the small limit.
_ADMIN_AUTH = ("ingress", "admin", "session")


def body_limit(scope) -> tuple[int, float]:
    """(bytes, seconds) for the body of this request.

    Decided when the app first reads the body, i.e. after AuthMiddleware set
    ``auth_type`` in the scope state: an unauthenticated peer never gets more
    than SMALL_BODY_LIMIT, also on the large routes.
    """
    auth_type = (scope.get("state") or {}).get("auth_type")
    path = scope.get("path", "")
    if auth_type in _ADMIN_AUTH and LARGE_BODY_ROUTES.match(path):
        if path == "/api/import/areas":
            return IMPORT_BODY_LIMIT, LARGE_BODY_SECONDS
        return MAX_REQUEST_BODY_SIZE, LARGE_BODY_SECONDS
    if auth_type:
        return AUTHENTICATED_BODY_LIMIT, SMALL_BODY_SECONDS
    return SMALL_BODY_LIMIT, SMALL_BODY_SECONDS


class BodySizeLimitMiddleware:
    """Request bodies above their limit get 413, too slow ones 408.

    A declared ``Content-Length`` above the largest limit is refused before
    the app runs. Otherwise the limit of :func:`body_limit` is applied when
    the app reads the body (chunked or not): the chunk that crosses it, or a
    body not complete in time, is not handed on; the app sees the client
    gone and the answer is 413 / 408 instead of whatever the app would say.
    Nothing buffers more than the limit. Pure ASGI and outermost, so it sees
    every request first (an exception raised from ``receive`` would arrive
    wrapped by the BaseHTTPMiddleware task groups).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = 0
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
        if declared > max(MAX_REQUEST_BODY_SIZE, IMPORT_BODY_LIMIT):
            await self._refuse(send, 413)
            return
        received = 0
        refused: int | None = None  # status that replaces the app's answer
        body_done = False
        limit: tuple[int, float] | None = None
        deadline = 0.0
        started = False

        async def counting_receive():
            nonlocal received, refused, body_done, limit, deadline
            if refused:
                return {"type": "http.disconnect"}
            if body_done:
                return await receive()  # waiting for a disconnect: no timer
            loop = asyncio.get_running_loop()
            if limit is None:
                limit = body_limit(scope)
                deadline = loop.time() + limit[1]
                if declared > limit[0]:
                    refused = 413
                    return {"type": "http.disconnect"}
            try:
                message = await asyncio.wait_for(
                    receive(), timeout=max(0.0, deadline - loop.time())
                )
            except asyncio.TimeoutError:
                refused = 408
                return {"type": "http.disconnect"}
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit[0]:
                    refused = 413
                    return {"type": "http.disconnect"}
                body_done = not message.get("more_body", False)
            return message

        async def guarded_send(message) -> None:
            nonlocal started
            if refused and not started:
                return  # the app's answer to a body it never got whole
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not refused:
                raise
        if refused and not started:
            await self._refuse(send, refused)

    @staticmethod
    async def _refuse(send, status: int) -> None:
        detail = "Request body too large" if status == 413 else "Request body too slow"
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses.

    Headers:
    - X-Content-Type-Options: nosniff (prevent MIME type sniffing)
    - X-Frame-Options: DENY (prevent clickjacking)
    - Referrer-Policy: strict-origin-when-cross-origin
    - Content-Security-Policy: restrict resource loading
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # X-Frame-Options: allow HA Ingress (which uses iframes) but block
        # embedding on unrelated sites.  SAMEORIGIN lets the HA frontend
        # embed the hub while still preventing click-jacking from
        # third-party origins.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"

        # Content Security Policy. Inline scripts only by
        # hash of the final HTML (this middleware runs after the Ingress
        # rewrite), connections only to the own origin. The API docs get
        # their own policy (local assets).
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            if csp.is_docs_path(request.url.path):
                response.headers["Content-Security-Policy"] = csp.docs_policy()
                return response
            body = b"".join([chunk async for chunk in response.body_iterator])
            hosts = [request.headers.get("host")]
            if getattr(request.state, "auth_type", None) == "ingress":
                hosts.append(request.headers.get("x-forwarded-host"))
            rebuilt = Response(
                content=body,
                status_code=response.status_code,
                background=response.background,
            )
            rebuilt.raw_headers = [
                (k, v)
                for k, v in response.raw_headers
                if k.lower() != b"content-length"
            ] + [(b"content-length", str(len(body)).encode())]
            rebuilt.headers["Content-Security-Policy"] = csp.page_policy(
                csp.inline_script_hashes(body), csp.websocket_sources(hosts)
            )
            return rebuilt

        return response


class IngressMiddleware(BaseHTTPMiddleware):
    """Rewrite HTML responses to fix asset paths under HA Ingress.

    HA Ingress proxies requests to add-ons under a subpath like
    /api/hassio_ingress/<session_token>/ and sends the base path
    in the X-Ingress-Path header.

    SvelteKit generates two kinds of HTML:
    - Prerendered pages (areas.html, settings.html): use relative paths
      like ./_app/... which resolve correctly because the browser knows
      the full ingress URL.
    - Fallback SPA page (index.html): uses absolute paths like /_app/...
      and base:"" which break under ingress subpath.

    This middleware:
    1. Injects window.__INGRESS_PATH for API/WS clients
    2. Fixes absolute paths in the fallback HTML (/_app/ -> ingress/_app/)
    3. Fixes SvelteKit's base:"" to use the ingress path
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Only rewrite HTML pages (not API JSON, not static JS/CSS); the API
        # docs need no ingress path and allow no inline script.
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type or csp.is_docs_path(request.url.path):
            return response

        # Only a request AuthMiddleware validated as genuine Ingress (header +
        # Supervisor proxy source IP) may steer the rewrite; a spoofed
        # X-Ingress-Path from any other source is ignored.
        if getattr(request.state, "auth_type", None) != "ingress":
            return response

        # Get ingress path from HA Supervisor proxy header
        ingress_path = request.headers.get("x-ingress-path", "").rstrip("/")
        if not ingress_path:
            return response

        # Read the response body
        body_chunks = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                body_chunks.append(chunk)
            else:
                body_chunks.append(chunk.encode("utf-8"))
        body = b"".join(body_chunks)
        html = body.decode("utf-8")

        # Inject ingress path as global variable for API/WS clients
        # Use json.dumps for safe JavaScript string escaping (prevents XSS)
        safe_path = json.dumps(ingress_path)
        ingress_script = f"<script>window.__INGRESS_PATH = {safe_path};</script>"
        html = html.replace("<head>", f"<head>\n\t\t{ingress_script}", 1)

        # Fix absolute paths (used in fallback index.html)
        # Prerendered pages use relative ./_app/ paths which are fine
        html = html.replace('href="/_app/', f'href="{ingress_path}/_app/')
        html = html.replace('src="/_app/', f'src="{ingress_path}/_app/')
        for asset in HEAD_ASSET_PATHS:
            html = html.replace(f'href="{asset}"', f'href="{ingress_path}{asset}"')
        html = html.replace('import("/_app/', f'import("{ingress_path}/_app/')

        # Fix SvelteKit base path for client-side routing
        # Fallback page has: base: ""  -> needs ingress path
        # Prerendered pages have: base: new URL(".", location)... -> already correct
        html = html.replace(
            'base: ""',
            f'base: "{ingress_path}"',
        )

        # Build new headers, removing Content-Length so it gets
        # recalculated from the (now larger) modified body
        new_headers = {
            k: v for k, v in response.headers.items() if k.lower() != "content-length"
        }

        return HTMLResponse(
            content=html,
            status_code=response.status_code,
            headers=new_headers,
        )


OPTIONS_PATH = "/data/options.json"
HA_NOTIFICATION_ID = HA_NOTIFICATION_ID_DATA_SAFETY


def _read_addon_options() -> dict:
    """Add-on options (``/data/options.json``); empty outside the Supervisor."""
    try:
        if os.path.isfile(OPTIONS_PATH):
            with open(OPTIONS_PATH) as f:
                return json.load(f)
    except Exception as e:  # noqa: BLE001 - defaults are safe
        logger.warning("Could not read %s: %s", OPTIONS_PATH, e)
    return {}


async def _remove_disallowed_watches(db) -> None:
    """Remove the watches their phones' rules no longer allow (once after
    the start; checked again at the next start when this fails)."""
    from services import watch_access

    try:
        await watch_access.remove_disallowed(db)
    except Exception as e:  # noqa: BLE001 - the watches get no key meanwhile
        logger.warning("Checking the watches failed: %s", e)


async def _notify_ha(client: HAClient | None, message: str) -> None:
    """Persistent HA notification (best effort; HA may not be ready)."""
    if client is None or not os.environ.get("SUPERVISOR_TOKEN"):
        return
    try:
        await client.call_service(
            "persistent_notification",
            "create",
            {
                "title": f"{BRAND_NAME}",
                "message": message,
                "notification_id": HA_NOTIFICATION_ID,
            },
        )
    except Exception as e:  # noqa: BLE001 - the log line carries the message
        logger.warning("Could not create HA notification: %s", e)


async def _daily_snapshot_loop(db_path: str, interval: float = 3600) -> None:
    """Daily snapshot (VACUUM INTO, newest 7 kept), checked hourly."""
    while True:
        try:
            await asyncio.to_thread(db_snapshots.take_daily_snapshot, db_path)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - retried next hour
            logger.error("Daily database snapshot failed: %s", e)
        await asyncio.sleep(interval)


async def _detect_ha_urls(app: FastAPI, db: Database, ha_client: HAClient) -> None:
    """Take HA's URLs and location name into the settings.

    Runs in the background on every (re)connect to HA, so neither a slow
    nor a stopped HA delays the start, and a changed HA configuration is
    picked up without a hub restart. Manual overrides stay.
    """
    ha_config = await ha_client.get_config()
    if not ha_config:
        return
    ext_url = ha_config.get("external_url", "")
    int_url = ha_config.get("internal_url", "")

    # If HA has no internal_url configured, build one from the local
    # IP plus HA's real port/scheme from the Supervisor.
    if not int_url:
        from services.ha_url import detect_internal_url
        from services.mdns import _get_local_ip

        local_ip = _get_local_ip()
        if local_ip and local_ip != "127.0.0.1":
            int_url = await detect_internal_url(ha_client, local_ip)
            logger.info("HA internal_url not configured, detected: %s", int_url)

    # External URL: HA's external_url if set. Without one the
    # integration reports the Nabu Casa address and keeps it
    # current (POST /api/integration/ha-url), so an
    # empty value here clears nothing.
    from services.ha_url import (
        apply_detected_external_url,
        normalize_external_url,
    )

    ext_url = normalize_external_url(ext_url)
    if ext_url:
        await apply_detected_external_url(db, ext_url)

    # Always update detected values (for UI reference)
    prev_int_detected = await db.get_setting("ha_internal_url_detected")
    if int_url:
        await db.set_setting("ha_internal_url_detected", int_url)
    existing_int = await db.get_setting("ha_internal_url")

    # Update the active URL if none is set yet or it matches the
    # previous detection (no manual override).
    if int_url:
        if not existing_int or existing_int == prev_int_detected:
            if existing_int != int_url:
                await db.set_setting("ha_internal_url", int_url)
                logger.info("Updated internal HA URL: %s", int_url)

    # Auto-detect HA location_name for home screen default title
    location_name = ha_config.get("location_name", "")
    if location_name:
        await db.set_setting("ha_location_name", location_name)
        # Set as default home_title if not yet configured
        existing_title = await db.get_setting("home_title")
        if not existing_title:
            await db.set_setting("home_title", location_name)
            logger.info("Auto-set home title from HA: %s", location_name)

    # Cache HA URLs for CORS middleware
    app.state._cors_ha_url = await db.get_setting("ha_url") or ""
    app.state._cors_ha_internal_url = await db.get_setting("ha_internal_url") or ""


async def _detect_ha_urls_logged(app: FastAPI, db: Database, ha_client) -> None:
    try:
        await _detect_ha_urls(app, db, ha_client)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - next connect tries again
        logger.warning("Could not auto-detect HA URLs (HA may not be ready): %s", e)


# First notification cleanup after the start (the hub may have been off for days).
NOTIFICATION_CLEANUP_FIRST_SECONDS = 60


async def _notification_cleanup_loop(db: Database, ha_client=None) -> None:
    """Shortly after the start, then hourly: end active notifications (not
    sticky) older than 72 h, remove dismissed ones older than 72 h."""
    from api.notifications import announce_dismissed
    from services.ha_sensors import sync_notification_sensors_safe
    from services.notifications import (
        cleanup_old_notifications,
        expire_active_notifications,
    )

    delay = NOTIFICATION_CLEANUP_FIRST_SECONDS
    while True:
        await asyncio.sleep(delay)
        delay = 3600
        try:
            expired = await expire_active_notifications(db)
            for client_id, ids in expired.items():
                await announce_dismissed(client_id, ids)
            if expired and ha_client is not None:
                await sync_notification_sensors_safe(db, ha_client)
            deleted = await cleanup_old_notifications(db)
            if deleted:
                logger.info("Cleaned up %d old dismissed notifications", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - retried next hour
            logger.error("Notification cleanup error: %s", e)


# Each shutdown step gets its own time limit; a step that fails or hangs is
# logged and the next one still runs.
SHUTDOWN_TASKS_SECONDS = 5.0
SHUTDOWN_STEP_SECONDS = 5.0


async def _shutdown_step(name: str, coro, timeout: float = SHUTDOWN_STEP_SECONDS):
    try:
        await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        logger.warning("Shutdown: %s did not finish within %.0fs", name, timeout)
    except Exception as e:  # noqa: BLE001 - continue with the next step
        logger.warning("Shutdown: %s failed: %s", name, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db, ha_client

    logger.info(f"Starting {BRAND_NAME}...")
    started_at = time.monotonic()
    options = _read_addon_options()
    app.state.startup_error = None
    app.state.startup_notices = []

    # Initialize database
    db_path = os.environ.get("HUB_DB_PATH", "/data/hub.db")
    db = Database(
        db_path, reset_on_corruption=bool(options.get("reset_corrupt_database"))
    )
    # Damaged data without a safe way out (database without snapshot, key
    # file without copy): start in an error state that explains what to do
    # (health 503, Ingress page, HA notification) instead of replacing the
    # user's data or crash-looping silently.
    startup_error: DatabaseUnavailableError | KeyfileError | None = None
    try:
        await db.initialize()
        try:
            key_notice = check_keyfile(
                await db.has_encrypted_tokens(),
                reset=bool(options.get("reset_encryption_key")),
                restored_from_ha_backup=db.restored_from_ha_backup,
            )
        except BaseException:
            await db.close()
            raise
    except (DatabaseUnavailableError, KeyfileError) as e:
        startup_error = e
        await db.close()
        db = None
    if startup_error is not None:
        app.state.db = None
        app.state.startup_error = {
            "reason": startup_error.reason,
            "action": startup_error.action,
        }
        logger.error(
            f"{BRAND_NAME} cannot start: %s What to do: %s",
            startup_error.reason,
            startup_error.action,
        )
        notifier = HAClient(os.environ.get("SUPERVISOR_TOKEN", ""))
        try:
            await _notify_ha(
                notifier,
                f"**{BRAND_NAME} cannot start.** {startup_error.reason}\n\n"
                f"What to do: {startup_error.action}",
            )
        finally:
            await notifier.close()
        yield
        logger.info(f"{BRAND_NAME} stopped (error state)")
        return
    notices = [n for n in (db.recovery_notice, key_notice) if n]
    app.state.startup_notices = notices

    # Any failure after the DB is open must close it (and the HA
    # client) before the error reaches uvicorn. An open aiosqlite worker
    # thread would otherwise keep the process alive after "Application
    # startup failed": container running, nothing answering, no restart.
    #
    # Nothing here waits for Home Assistant or the Supervisor. Work
    # that needs them (URL detection, sensor sync, discovery announce,
    # notices) runs in background tasks that retry on their own, so the hub
    # answers within ~1 s even while HA is still starting or restarting.
    tasks.registry.reset()
    ha_client = None
    try:
        # Initialize HA client
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        ha_client = HAClient(supervisor_token)

        # Store in app state for dependency injection
        app.state.db = db
        app.state.ha_client = ha_client
        # Direct admin access on :8100 (default off, see api/auth.py).
        from api.auth import load_direct_admin_setting

        await load_direct_admin_setting(db)
        # Quick access check sensors follow broadcasts.
        from services import quick_access

        quick_access.register(db, ha_client)
        # Persistent log tab: sources, notifications, retention.
        from services import event_log

        event_log.register(db, ha_client)

        # Rule snapshots for the integration follow broadcasts.
        from services import enforcement

        enforcement.register(db, ha_client)
        # A watch's remote levels are limited with HA's device classes.
        from services.profiles import store as profile_store

        profile_store.register_states(ha_client)
        # Watches: their phones get the sealed access after changes.
        from services import watch_access

        watch_access.register(db, ha_client)
        # A watch its phone's rules no longer allow does not survive an
        # upgrade (migration 20) or a missed expiry; in the background, the
        # start does not wait for it.
        tasks.spawn(_remove_disallowed_watches(db), name="watch-check")

        for notice in notices:
            tasks.spawn(_notify_ha(ha_client, notice), name="ha-notice")

        # Leftovers of an interrupted image replacement.
        try:
            await cleanup_orphan_images(db, IMAGE_DIR)
        except Exception as e:  # noqa: BLE001 - housekeeping only
            logger.warning("Image clean-up failed: %s", e)

        # Load app_url from database (set via Settings UI)
        # This is the base URL under which Halumar is reachable externally.
        # Used for QR codes, image URLs sent to clients, and mDNS records.
        # If empty, auto-detection from request headers is used as fallback.
        app_url = (await db.get_setting("app_url") or "").strip().rstrip("/")
        if app_url:
            logger.info("App URL configured: %s", app_url)
        app.state.app_url = app_url

        # Ensure image upload directory exists
        os.makedirs("/data/images", exist_ok=True)

        # Cache HA URLs for CORS middleware; refreshed after every
        # URL detection.
        app.state._cors_ha_url = await db.get_setting("ha_url") or ""
        app.state._cors_ha_internal_url = await db.get_setting("ha_internal_url") or ""

        # Persistent HA connection: state mirror, registry cache,
        # events. On every (re)connect: detect HA's URLs, sync the sensors.
        if supervisor_token:
            ha_ws = HAWebSocket(supervisor_token)
            ha_client.attach_ws(ha_ws)
            attach_live_updates(db, ha_client, ha_ws)
            event_log.attach_ha_listener(ha_ws)

            async def _on_ha_connect() -> None:
                tasks.spawn(
                    _detect_ha_urls_logged(app, db, ha_client), name="ha-url-detect"
                )

            ha_ws.add_connect_listener(_on_ha_connect)
            # Family manager: answer buttons of HA notifications.
            from services.family import notify as family_notify

            ha_ws.add_event_listener(family_notify.on_ha_event(db, ha_client))
            # Early access: answer buttons of HA notifications.
            from services import access_requests

            ha_ws.add_event_listener(access_requests.on_ha_event(db, ha_client))
            tasks.registry.supervise("ha-websocket", ha_ws.run)

            # Supervisor discovery: stage a new integration token and announce
            # host/port/token/version so the companion integration configures
            # itself; retried until the Supervisor accepted it.
            # Manual setups manage the token via Settings instead.
            from services.discovery import announce_until_done

            tasks.spawn(announce_until_done(db, ha_client), name="discovery")
        else:
            tasks.spawn(
                _detect_ha_urls_logged(app, db, ha_client), name="ha-url-detect"
            )

        # Start mDNS/Bonjour service announcement for auto-discovery (toggleable)
        mdns_enabled = options.get("mdns_enabled", True)

        if mdns_enabled:
            await start_mdns(version=VERSION, port=8099, app_url=app_url, db=db)
        else:
            logger.info(
                "mDNS discovery disabled via add-on config (mdns_enabled=false)"
            )

        # Safety net next to the HA events: resync missing sensors; poll the
        # area switches only while the live connection is down.
        tasks.registry.supervise(
            "sensor-reconcile", lambda: reconcile_loop(db, ha_client)
        )
        # Dead-connection detection of /api/ws
        tasks.registry.supervise("ws-ping", ws_ping_loop)
        tasks.registry.supervise("guest-expiry", lambda: guest_expiry_loop(app))
        tasks.registry.supervise(
            "profile-adjustment-expiry", lambda: profile_expiry_loop(app)
        )
        # Family manager: releases and requests end on time; the
        # automatic child notifications go to the linked HA notify targets.
        from services.family import events as family_events
        from services.family import notify as family_notify
        from services.family.expiry import expiry_loop as family_expiry_loop

        family_events.add_sink(family_notify.make_sink(db, ha_client))
        from services import integration_events

        integration_events.add_sink(family_events.integration_sink(db))
        tasks.registry.supervise("family-expiry", lambda: family_expiry_loop(app))
        from services.access import access_loop

        tasks.registry.supervise("access-hours", lambda: access_loop(app))
        tasks.registry.supervise(
            "notification-cleanup", lambda: _notification_cleanup_loop(db, ha_client)
        )
        tasks.registry.supervise(
            "log-housekeeping", lambda: event_log.housekeeping_loop(db, ha_client)
        )
        tasks.registry.supervise("enforcement-rules", enforcement.periodic_loop)
        # Daily database snapshot (first check right away, then hourly).
        tasks.registry.supervise("db-snapshot", lambda: _daily_snapshot_loop(db_path))

        # One HA user per device: repeat outstanding provisioning
        # requests to the integration while its push session is up.
        from services import device_provisioning

        tasks.registry.supervise(
            "device-provisioning",
            lambda: device_provisioning.retry_loop(db, get_integration_push_connected),
        )

    except BaseException:
        logger.error(f"{BRAND_NAME} startup failed, closing resources before exit")
        await _shutdown_step("background tasks", tasks.registry.shutdown(2.0))
        await _shutdown_step("mDNS", stop_mdns())
        if ha_client:
            await _shutdown_step("HA client", ha_client.close())
        await _shutdown_step("database", db.close())
        raise

    logger.info(
        f"{BRAND_NAME} started successfully in %.2fs (tasks: %s)",
        time.monotonic() - started_at,
        ", ".join(tasks.registry.names()),
    )
    yield

    # Cleanup: every step runs, each with its own time limit.
    logger.info(f"Shutting down {BRAND_NAME}...")
    await _shutdown_step(
        "background tasks",
        tasks.registry.shutdown(SHUTDOWN_TASKS_SECONDS),
        SHUTDOWN_TASKS_SECONDS + 1,
    )
    await _shutdown_step("mDNS", stop_mdns())
    if ha_client:
        await _shutdown_step("HA client", ha_client.close())
    if db:
        await _shutdown_step("database", db.close())
    logger.info(f"{BRAND_NAME} stopped")


LICENSE_INFO = {
    "name": "AGPL-3.0-only",
    "url": "https://github.com/Halumar/hub/blob/main/LICENSE",  # brandlint: allow
}

app = FastAPI(
    title=BRAND_NAME,
    description="Gives every device in your home its own, limited view of Home "
    "Assistant. External API access requires a Bearer token "
    "(see Settings > Advanced > API token).",
    version=VERSION,
    license_info=LICENSE_INFO,
    # API docs and schema are served below: admin only, local assets, and
    # switchable off with the app option api_docs.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

DOCS_ASSETS_DIR = os.path.join(os.path.dirname(__file__), "docs_assets")
_DOCS_ASSETS = {
    "swagger-ui-bundle.js": "text/javascript",
    "swagger-ui.css": "text/css",
    "swagger-init.js": "text/javascript",
    "redoc.standalone.js": "text/javascript",
    "redoc-init.js": "text/javascript",
}


def api_docs_enabled() -> bool:
    """Add-on option ``api_docs`` (default on; read per request, cheap)."""
    return _read_addon_options().get("api_docs", True) is not False


def _require_docs(request: Request) -> None:
    from api.auth import require_admin

    if not api_docs_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    require_admin(request)


def _docs_page(title: str, head: str, body: str) -> HTMLResponse:
    # Relative URLs: the page works at /api/docs and under the Ingress prefix.
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>{BRAND_NAME} - {title}</title>
{head}
</head>
<body>
{body}
</body>
</html>""",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/openapi.json", include_in_schema=False)
async def openapi_json(request: Request):
    """OpenAPI schema of the hub. Admin only."""
    _require_docs(request)
    return JSONResponse(app.openapi())


@app.get("/api/docs", include_in_schema=False)
async def swagger_html(request: Request):
    """Swagger UI from local assets. Admin only."""
    _require_docs(request)
    return _docs_page(
        "API",
        '<link rel="stylesheet" href="docs/assets/swagger-ui.css">',
        '<div id="swagger-ui"></div>\n'
        '<script src="docs/assets/swagger-ui-bundle.js"></script>\n'
        '<script src="docs/assets/swagger-init.js"></script>',
    )


@app.get("/api/redoc", include_in_schema=False)
async def redoc_html(request: Request):
    """ReDoc from local assets. Admin only."""
    _require_docs(request)
    return _docs_page(
        "ReDoc",
        "<style>body { margin: 0; padding: 0; }</style>",
        '<div id="redoc"></div>\n'
        '<script src="docs/assets/redoc.standalone.js"></script>\n'
        '<script src="docs/assets/redoc-init.js"></script>',
    )


@app.get("/api/docs/assets/{name}", include_in_schema=False)
async def docs_asset(request: Request, name: str):
    """Vendored Swagger UI / ReDoc files (scripts/update-api-docs.sh)."""
    _require_docs(request)
    media_type = _DOCS_ASSETS.get(name)
    if not media_type:
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(
        os.path.join(DOCS_ASSETS_DIR, name),
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


# OpenAPI security scheme - adds "Authorize" button in Swagger UI
from fastapi.security import HTTPBearer  # noqa: E402

security_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    description="API token (from Settings page) or paired client secret",
)

# Register the security scheme globally in OpenAPI
from fastapi.openapi.utils import get_openapi  # noqa: E402


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        license_info=app.license_info,
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "hex token",
            "description": "API token from Settings page or paired client secret",
        }
    }
    # Apply security globally to all endpoints
    openapi_schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi

# Middleware order (last added = first executed, i.e. outermost):
# 1. IngressMiddleware - rewrites HTML for Ingress subpath (innermost)
# 2. SecurityHeadersMiddleware - adds security headers; runs
#    on the rewritten HTML so the CSP script hashes match
# 3. AuthMiddleware - checks Bearer token for external API requests
# 4. CORSMiddleware - handles CORS headers (dynamic origins)
app.add_middleware(IngressMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthMiddleware)


def _get_cors_origins() -> list[str]:
    """Build dynamic CORS origins from app_url and HA URLs.

    Reads from app state (populated during lifespan startup).
    Returns a list of allowed origins, or ["*"] as fallback if none configured.
    """
    origins: set[str] = set()

    try:
        state = app.state
        # Add app_url origin
        app_url = getattr(state, "app_url", "") or ""
        if app_url:
            parsed = urlparse(app_url)
            if parsed.scheme and parsed.netloc:
                origins.add(f"{parsed.scheme}://{parsed.netloc}")

        # Add HA URLs (for cross-origin requests from HA dashboard)
        db = getattr(state, "db", None)
        if db:
            # These are cached in app state after lifespan startup
            for url_attr in ("_cors_ha_url", "_cors_ha_internal_url"):
                url = getattr(state, url_attr, "") or ""
                if url:
                    parsed = urlparse(url)
                    if parsed.scheme and parsed.netloc:
                        origins.add(f"{parsed.scheme}://{parsed.netloc}")
    except Exception:
        pass

    # Always allow localhost development
    origins.add("http://localhost:8099")
    origins.add("http://localhost:5173")

    return list(origins) if origins else ["*"]


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """CORS middleware with dynamic origins from app_url/HA URLs.

    Instead of allow_origins=["*"], checks the Origin header against
    configured URLs. Allows Bearer-token-based access without cookies.
    """

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin", "")

        # Handle preflight (OPTIONS)
        if request.method == "OPTIONS" and origin:
            allowed_origins = _get_cors_origins()
            from starlette.responses import Response

            if origin in allowed_origins:
                return Response(
                    status_code=200,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-CSRF-Token",
                        "Access-Control-Max-Age": "3600",
                    },
                )
            else:
                # Non-matching origin: return 204 without CORS headers
                # so it doesn't fall through to AuthMiddleware (which would 401)
                return Response(status_code=204)

        response = await call_next(request)

        # Add CORS headers for actual requests
        if origin:
            allowed_origins = _get_cors_origins()
            if origin in allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Methods"] = (
                    "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                )
                response.headers["Access-Control-Allow-Headers"] = (
                    "Authorization, Content-Type, X-CSRF-Token"
                )

        return response


app.add_middleware(DynamicCORSMiddleware)


class StartupErrorMiddleware(BaseHTTPMiddleware):
    """Error state (damaged data, see lifespan): every request but the health
    check gets 503 with the reason and what to do; HTML for the Ingress page.
    Outermost middleware, so nothing below touches the missing database."""

    async def dispatch(self, request: Request, call_next):
        error = getattr(request.app.state, "startup_error", None)
        if not error or request.url.path == "/api/health":
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": error["reason"], "action": error["action"]},
                status_code=503,
            )
        import html as _html

        page = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{BRAND_NAME} – error</title></head>"
            "<body style='font-family:sans-serif;max-width:40em;margin:2em auto'>"
            f"<h1>{BRAND_NAME} cannot start</h1>"
            f"<p>{_html.escape(error['reason'])}</p>"
            f"<h2>What to do</h2><p>{_html.escape(error['action'])}</p>"
            "</body></html>"
        )
        return HTMLResponse(page, status_code=503)


app.add_middleware(StartupErrorMiddleware)
# Outermost: the body limit applies before anything reads a body.
app.add_middleware(BodySizeLimitMiddleware)

# Include API routers
# A dry run is never carried out for real (api/dry_run.py); not the WebSocket.
_DRY_RUN_GUARD = [Depends(reject_unsupported_dry_run)]
app.include_router(auth_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(areas_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(tiles_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(quick_access_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(entities_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(sync_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(export_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(integration_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(notifications_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(pairing_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(ws_router, prefix="/api")
app.include_router(admin_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(profiles_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(family_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(panel_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(log_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(home_networks_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(room_climate_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(scan_tags_router, prefix="/api", dependencies=_DRY_RUN_GUARD)
app.include_router(access_requests_router, prefix="/api", dependencies=_DRY_RUN_GUARD)


@app.get("/api/health")
async def health_check(request: Request):
    """Health check endpoint (also the Supervisor watchdog target).

    Includes process uptime and the number of currently connected
    (authenticated) WebSocket clients for basic observability. Probes the
    database: if it is not usable (closed, stuck, locked so writes fail) the
    endpoint answers 503 instead of "ok", so the
    Supervisor watchdog can restart the hub.
    """
    database = getattr(request.app.state, "db", None) or db
    startup_error = getattr(request.app.state, "startup_error", None)
    if startup_error:
        body = {
            "status": "error",
            "db": "unavailable",
            "error": startup_error["reason"],
            "action": startup_error["action"],
            "version": VERSION,
            "build": BUILD,
            "uptime_seconds": int(time.monotonic() - _START_TIME),
        }
        return JSONResponse(body, status_code=503)
    db_error = (
        await database.probe() if database is not None else "database not initialized"
    )
    last_snapshot = None
    if database is not None:
        try:
            last_snapshot = db_snapshots.latest_snapshot_info(database.db_path)
        except OSError:
            pass
    body = {
        "status": "ok" if db_error is None else "error",
        "db": "ok" if db_error is None else db_error,
        "version": VERSION,
        "build": BUILD,
        "uptime_seconds": int(time.monotonic() - _START_TIME),
        "connected_clients": get_connected_count(),
        # protocol_version, min_protocol_version, min_app_version:
        # apps check compatibility before pairing.
        **protocol.protocol_fields(),
        # SHA-256 fingerprint of the self-signed TLS leaf cert (the value a
        # client pins). None when no cert exists / TLS is not enabled — never a
        # fake value. Also published in the QR, Bonjour TXT, and client_config.
        "tls_fingerprint": get_fingerprint(),
        # Newest database snapshot {"file", "created"} or None.
        "last_snapshot": last_snapshot,
        # True if this start restored data (snapshot, HA backup) or replaced
        # the encryption key; details in the log and an HA notification.
        "data_recovered": bool(getattr(request.app.state, "startup_notices", None)),
    }
    if db_error is not None:
        logger.warning("Health check failed: %s", db_error)
        return JSONResponse(body, status_code=503)
    return body


# Serve uploaded images - ensure directory exists before mounting
IMAGE_DIR = os.environ.get("HUB_IMAGE_DIR", "/data/images")
os.makedirs(IMAGE_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=IMAGE_DIR), name="images")

# Serve static frontend files with SPA fallback
# StaticFiles(html=True) alone can't handle SPA deep links like /areas/<uuid>
# because there's no matching file. We mount static for real files (_app/, *.html)
# and add a catch-all route that serves index.html for unknown paths.
static_dir = os.path.join(os.path.dirname(__file__), "static")
# Not every Python's table knows the web manifest (nosniff is set on every response).
mimetypes.add_type("application/manifest+json", ".webmanifest")
_spa_fallback = (
    os.path.join(static_dir, "index.html") if os.path.isdir(static_dir) else None
)

# Cache headers for different asset types
# _app/immutable/ files have content hashes in filenames - safe to cache forever
_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
# HTML pages must always be revalidated (they reference hashed JS/CSS)
_HTML_NO_CACHE = "no-cache, no-store, must-revalidate"


def _html_response(file_path: str) -> FileResponse:
    """Return an HTML FileResponse with no-cache headers."""
    return FileResponse(
        file_path,
        media_type="text/html",
        headers={"Cache-Control": _HTML_NO_CACHE},
    )


if os.path.isdir(static_dir):
    # Mount _app/immutable/ with aggressive caching (content-hashed filenames)
    _app_dir = os.path.join(static_dir, "_app")

    @app.get("/_app/immutable/{file_path:path}")
    async def serve_immutable(request: Request, file_path: str):
        """Serve immutable assets with long-term cache headers.

        Outside Ingress, JS/CSS go out gzip-compressed (services/static_compress).
        """
        real_app = os.path.realpath(_app_dir)
        resolved = os.path.realpath(os.path.join(_app_dir, "immutable", file_path))
        if not resolved.startswith(real_app + os.sep):
            return HTMLResponse("Not found", status_code=404)
        if not os.path.isfile(resolved):
            return HTMLResponse("Not found", status_code=404)
        headers = {"Cache-Control": _IMMUTABLE_CACHE, "Vary": "Accept-Encoding"}
        if static_compress.accepts_gzip(request):
            compressed = await static_compress.gzip_response(resolved, headers)
            if compressed is not None:
                return compressed
        return FileResponse(resolved, headers=headers)

    # Mount remaining _app/ files (version.json etc.) with standard StaticFiles
    app.mount(
        "/_app",
        StaticFiles(directory=_app_dir),
        name="static_app",
    )


@app.get("/{full_path:path}")
async def spa_fallback(request: Request, full_path: str):
    """Serve static files or SPA fallback for client-side routing.

    Order:
    1. Validate path stays within static_dir (prevent path traversal)
    2. Try exact file match (e.g. areas.html for /areas)
    3. Fall back to index.html for SPA routing (e.g. /areas/<uuid>)

    All HTML responses include Cache-Control: no-cache to ensure browsers
    always fetch the latest version (which references content-hashed JS/CSS).
    """
    # Unknown API paths are a 404 in JSON, not the SPA page with status 200
    # (clients would otherwise fail on parsing HTML as JSON).
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if _spa_fallback is None:
        return HTMLResponse("<h1>Frontend not built</h1>", status_code=500)

    # Try exact file match first
    if full_path:
        # Resolve the real path and validate it stays within static_dir
        # This prevents path traversal attacks (e.g. /../../../etc/passwd)
        real_static = os.path.realpath(static_dir)
        file_path = os.path.realpath(os.path.join(static_dir, full_path))

        # Security check: resolved path must be within static_dir
        if not file_path.startswith(real_static + os.sep) and file_path != real_static:
            return _html_response(_spa_fallback)

        # Check for direct file (e.g. favicon.png)
        if os.path.isfile(file_path):
            if file_path.endswith(".html"):
                return _html_response(file_path)
            return FileResponse(file_path)

        # Check for .html variant (e.g. /areas -> areas.html)
        html_path = file_path + ".html"
        if os.path.isfile(html_path) and html_path.startswith(real_static + os.sep):
            return _html_response(html_path)

    # SPA fallback - let SvelteKit handle routing client-side
    return _html_response(_spa_fallback)


# Passed to every uvicorn server of the hub. proxy_headers is off: the hub
# reads neither X-Forwarded-For nor X-Forwarded-Proto. uvicorn's default
# (proxy_headers on, forwarded_allow_ips "127.0.0.1") would take the client
# address and the scheme from these headers for every loopback peer, and with
# host_network every host process is one, so a forwarded address would pass
# for the Supervisor Ingress proxy. Ingress requests come from 172.30.32.2
# itself (services/ingress.py), so the hub needs no proxy headers.
#
# ws_max_size: uvicorn's default takes WebSocket messages up to 16 MiB into
# memory before the hub sees them, also from a peer that never
# authenticates. The hub refuses anything above MAX_WS_MESSAGE_SIZE
# characters anyway; 4 bytes per character is the most UTF-8 needs.
UVICORN_OPTIONS = {
    "proxy_headers": False,
    "ws_max_size": 4 * MAX_WS_MESSAGE_SIZE,
}


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description=f"{BRAND_NAME} Server")
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["trace", "debug", "info", "warning", "error"],
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    # Map trace to debug for uvicorn
    uv_level = "debug" if args.log_level == "trace" else args.log_level

    # Root logging with secret redaction; httpx/httpcore quiet, websockets and
    # aiosqlite never below INFO (services/log_redaction.py).
    configure_logging(uv_level)

    # Read the app option that turns on TLS. Mirror how the lifespan reads
    # options.json for mdns_enabled. The app option defaults to true (dual
    # HTTP+TLS serving); without an options.json (dev runs outside the
    # Supervisor, no /data volume for the cert) we stay on the plain single
    # server.
    tls_enabled = False
    try:
        options_path = "/data/options.json"
        if os.path.isfile(options_path):
            with open(options_path) as f:
                options = json.load(f)
            tls_enabled = bool(options.get("tls_enabled", False))
    except Exception as e:
        logger.warning("Could not read options.json for TLS setting: %s", e)

    if not tls_enabled:
        # PLAIN PATH — the pre-TLS behavior: a single plain-HTTP uvicorn
        # server. Do not change this branch.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=uv_level,
            access_log=args.log_level in ("trace", "debug"),
            **UVICORN_OPTIONS,
        )
        return

    # TLS enabled ⇒ dual-server path (see _run_dual_servers).
    _run_dual_servers(args, uv_level)


def _run_dual_servers(args: argparse.Namespace, uv_level: str) -> None:
    """Serve plain HTTP (:8099, HA Ingress) AND TLS (:8100, external clients)
    on ONE event loop.

    Only reached when the app option ``tls_enabled`` is true. The DB and
    ha_client created in the app lifespan are bound to a single event loop, so
    a second thread/loop is NOT possible — both uvicorn servers must share this
    one loop. This path needs a real ``/data`` volume + bindable sockets, so it
    cannot be runtime-tested in the pytest harness; it is kept minimal, correct,
    and py_compile-clean.
    """
    # Generate (or reuse) the self-signed cert before binding the TLS socket.
    # Add the detected LAN IP to the cert SAN so standard validation still works
    # for anyone fronting the hub with a CA-valid cert. 127.0.0.1 is added by
    # ensure_cert itself, so we skip loopback here.
    from services.mdns import _get_local_ip

    local_ip = _get_local_ip()
    extra_ips = [local_ip] if local_ip and local_ip != "127.0.0.1" else []
    cert_path, key_path = ensure_cert(extra_ips=extra_ips)
    logger.info(
        "TLS enabled: serving https/wss (TLS 1.3+) on :%d (fingerprint %s) "
        "alongside plain HTTP on :%d for HA Ingress",
        TLS_PORT,
        get_fingerprint(),
        args.port,
    )

    access_log = args.log_level in ("trace", "debug")

    # The HTTP server owns the app lifespan (DB init, mDNS announce, background
    # tasks) — it must run EXACTLY ONCE.
    http_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=uv_level,
        access_log=access_log,
        **UVICORN_OPTIONS,
    )
    # The TLS server runs the SAME app object but with lifespan="off", so
    # startup/shutdown does not run a second time (a duplicate DB init / mDNS
    # register would clash with the HTTP server's).
    tls_config = uvicorn.Config(
        app,
        host=args.host,
        port=TLS_PORT,
        log_level=uv_level,
        access_log=access_log,
        ssl_certfile=cert_path,
        ssl_keyfile=key_path,
        lifespan="off",
        **UVICORN_OPTIONS,
    )

    # Enforce the TLS 1.3 floor (B3). uvicorn builds the serving SSLContext in
    # Config.load() (PROTOCOL_TLS_SERVER, which would still accept TLS 1.2);
    # load it here and raise the minimum before serve() reuses the loaded
    # config. The :8099 server stays plain HTTP for HA Ingress (no context).
    tls_config.load()
    if tls_config.ssl is not None:
        enforce_tls13_minimum(tls_config.ssl)
    # From now on the hub advertises the fingerprint of the cert this server
    # holds, also after a regenerate until the restart.
    mark_served()

    http_server = uvicorn.Server(http_config)
    tls_server = uvicorn.Server(tls_config)

    async def _serve() -> None:
        loop = asyncio.get_running_loop()

        def _shutdown() -> None:
            http_server.should_exit = True
            tls_server.should_exit = True

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            # No loop-level signal support (non-unix): keep uvicorn's built-in
            # per-server capture_signals() handling as a fallback.
            pass
        else:
            # ONE shared handler must own SIGINT/SIGTERM so BOTH servers drain.
            # Each uvicorn.Server.serve() otherwise wraps itself in
            # capture_signals(), which re-registers signal.signal handlers —
            # the last server to start would win and only stop ITSELF, plus its
            # exit replay (raise_signal) would re-deliver the signal. Suppress
            # it per server (uvicorn >= 0.29 removed install_signal_handlers).
            http_server.capture_signals = nullcontext
            tls_server.capture_signals = nullcontext

        # Both servers run on this single loop and live and die together: if
        # one returns (e.g. the HTTP server after a failed lifespan startup),
        # the other is told to exit too, otherwise the TLS server would keep
        # the process alive without an app behind it.
        async def _run(server: uvicorn.Server, other: uvicorn.Server) -> None:
            try:
                await server.serve()
            finally:
                other.should_exit = True

        await asyncio.gather(
            _run(http_server, tls_server), _run(tls_server, http_server)
        )

    asyncio.run(_serve())
    if not http_server.started:
        # Same exit code uvicorn.run uses for a failed startup, so the s6
        # finish script stops the container and the Supervisor watchdog can
        # restart the hub.
        sys.exit(3)


if __name__ == "__main__":
    main()
