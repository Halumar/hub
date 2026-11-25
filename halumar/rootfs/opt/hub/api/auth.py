"""Authentication and authorization middleware for Halumar.

Security model:
- Ingress requests: validated by X-Ingress-Path header + trusted source IP
- External web UI access requires HA login (validates against HA Auth API via Supervisor)
  - Only when direct admin access is switched on (setting admin_direct_access,
    default off, changed only through Ingress); off, login and sessions are
    refused and the admin page opens only through Home Assistant
  - Session token stored as cookie, also accepted as Bearer token
- External REST API requires Bearer token (admin API token or session token);
  paired clients use the WebSocket, plus the device-secret paths of
  CLIENT_SECRET_PATHS (control and location with parental controls, signing
  off a watch)
- API docs (/api/docs, /api/redoc, /api/openapi.json) are admin only and
  can be switched off with the app option api_docs
- Health endpoint is always public
- WebSocket auth is handled separately via pairing secrets

Role-based access control:
- auth_type is set on request.state by the middleware:
  - "ingress": trusted HA Ingress request (admin-level)
  - "admin": admin API token
  - "session": HA login session via cookie or Bearer (admin-level)
  - "integration": dedicated integration token (scoped to /api/integration/*)
- Endpoints use require_admin(request) to restrict to admin-level callers.
- /api/integration/* endpoints use require_integration(request): they accept
  EXACTLY the dedicated integration token (rotated on every startup under the
  Supervisor and announced via the discovery API — see services/discovery.py).
  The token is scoped: it opens nothing outside /api/integration/*.

Security hardening:
- Constant-time token comparison (hmac.compare_digest) to prevent timing attacks
- Rate limiting on login endpoint (5 attempts per IP per minute)
- Ingress validation: checks source IP is from trusted Docker/Supervisor networks
- Cookie secure flag set dynamically based on request protocol
- Hashed secret storage for paired clients and API token
"""

import hashlib
import hmac
import logging
import os
import secrets
import time

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from brand import BRAND_NAME
from models.database import Database
from services.audit_log import (
    log_admin_access,
    log_auth_success,
    log_auth_failure,
    log_token_generated,
    log_rate_limited,
)
from services import integration_source
from services.ingress import ip_is_trusted as ingress_ip_is_trusted
from services.ingress import is_trusted_ingress
from services.integration_status import (
    INTEGRATION_VERSION_HEADER,
    record_auth_failure,
    record_request,
)

logger = logging.getLogger("hub.auth")

router = APIRouter(tags=["auth"])

# Paths that never require authentication (even externally). The API docs
# and the OpenAPI schema are admin only: under Ingress they run in
# the HA origin.
# The REST paths a paired device may call with its client secret: control
# through the hub (kids mode), its position and signing itself
# off without a hub socket (the watch). Every other path keeps rejecting client
# secrets.
CLIENT_CONTROL_PATH = "/api/client/control"
CLIENT_LEAVE_PATH = "/api/client/leave"
# Position of a device with parental controls without a hub socket.
CLIENT_LOCATION_PATH = "/api/client/location"
CLIENT_SECRET_PATHS = frozenset(
    {CLIENT_CONTROL_PATH, CLIENT_LEAVE_PATH, CLIENT_LOCATION_PATH}
)

# Icons and the web manifest in the page head (admin UI build, frontend/static and
# src/routes/manifest.webmanifest). Static, public like the favicon: browsers fetch
# the manifest without cookies.
HEAD_ASSET_PATHS = (
    "/favicon.svg",
    "/favicon-16.png",
    "/favicon-32.png",
    "/favicon-48.png",
    "/apple-touch-icon.png",
    "/icon-maskable-192.png",
    "/icon-maskable-512.png",
    "/manifest.webmanifest",
)

PUBLIC_PATHS = {
    "/api/health",
    *HEAD_ASSET_PATHS,
}

# Path prefixes that never require authentication
PUBLIC_PREFIXES = (
    "/api/auth/login",  # HA login endpoint (credentials are the auth)
    "/api/auth/logout",  # Logout endpoint (just clears cookie)
    "/api/auth/session",  # Session check endpoint
    "/_app/",  # Static assets
    "/images/",  # Uploaded images
    "/favicon",  # Favicon
)


def _ip_is_trusted(client_ip: str) -> bool:
    """Return True if client_ip may carry Ingress trust (see services/ingress.py).

    Only the Supervisor Ingress proxy 172.30.32.2 qualifies; loopback and the
    rest of the hassio network only with the HUB_DEV flag.
    """
    return ingress_ip_is_trusted(client_ip)


# ── Rate Limiting ───────────────────────────────────────────────────────────

# Rate limiter: LRU-bounded dict to prevent unbounded memory growth
from collections import OrderedDict  # noqa: E402

_rate_limits: OrderedDict[str, list[float]] = OrderedDict()
_rate_limit_last_cleanup: float = 0.0

# Rate limit: max attempts per window
RATE_LIMIT_MAX = 5
RATE_LIMIT_WINDOW = 60  # seconds
# Full cleanup interval: purge stale keys every 5 minutes
_RATE_LIMIT_CLEANUP_INTERVAL = 300  # seconds
# Maximum number of tracked IPs/keys (LRU eviction beyond this)
_RATE_LIMIT_MAX_KEYS = 10000


def _rate_limit_check(key: str) -> bool:
    """Check if a rate limit key is within limits. Returns True if allowed.

    Uses an OrderedDict with LRU eviction to bound memory usage.
    Periodically purges stale keys. Max 10,000 tracked keys.
    """
    global _rate_limit_last_cleanup
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW

    # Periodic full cleanup: remove all keys with no recent entries
    if now - _rate_limit_last_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
        stale_keys = [
            k
            for k, timestamps in _rate_limits.items()
            if not timestamps or timestamps[-1] < cutoff
        ]
        for k in stale_keys:
            del _rate_limits[k]
        _rate_limit_last_cleanup = now

    # LRU eviction: if at max capacity, remove oldest entries
    while len(_rate_limits) >= _RATE_LIMIT_MAX_KEYS and _rate_limits:
        _rate_limits.popitem(last=False)

    # Cleanup old entries for this key
    if key in _rate_limits:
        _rate_limits[key] = [t for t in _rate_limits[key] if t > cutoff]
        _rate_limits.move_to_end(key)  # Mark as recently used
    else:
        _rate_limits[key] = []

    if len(_rate_limits[key]) >= RATE_LIMIT_MAX:
        return False

    _rate_limits[key].append(now)
    return True


# Failed logins per account, counted per source address: one address can
# only block itself for this account; the account as a whole is blocked only
# by many failures from many addresses.
LOGIN_FAILURE_WINDOW = 900  # seconds
LOGIN_FAILURES_PER_IP = 5
LOGIN_FAILURES_PER_USER = 30
LOGIN_FAILURE_MIN_IPS = 5
_login_failures: OrderedDict[str, list[tuple[float, str]]] = OrderedDict()


def _recent_login_failures(username: str) -> list[tuple[float, str]]:
    cutoff = time.time() - LOGIN_FAILURE_WINDOW
    failures = [f for f in _login_failures.get(username, []) if f[0] > cutoff]
    if failures:
        _login_failures[username] = failures
    else:
        _login_failures.pop(username, None)
    return failures


def _login_blocked(username: str, ip: str) -> bool:
    failures = _recent_login_failures(username)
    if sum(1 for _ts, src in failures if src == ip) >= LOGIN_FAILURES_PER_IP:
        return True
    return (
        len(failures) >= LOGIN_FAILURES_PER_USER
        and len({src for _ts, src in failures}) >= LOGIN_FAILURE_MIN_IPS
    )


def _record_login_failure(username: str, ip: str) -> None:
    while len(_login_failures) >= _RATE_LIMIT_MAX_KEYS and _login_failures:
        _login_failures.popitem(last=False)
    failures = _recent_login_failures(username)
    failures.append((time.time(), ip))
    _login_failures[username] = failures[-200:]
    _login_failures.move_to_end(username)


def _get_client_ip(request: Request) -> str:
    """Get client IP from the actual TCP connection (not headers).

    Uses request.client.host directly instead of trusting X-Forwarded-For,
    which any client can forge to escape the rate limit.
    """
    if request.client:
        return request.client.host
    return "unknown"


def _is_secure_transport(request: Request) -> bool:
    """True when the request arrived over TLS (:8100).

    Admin credentials (HA password, session, admin API token) must not cross
    the LAN in clear text on :8099. HA Ingress is handled
    before this check. ``HUB_DEV`` allows plain HTTP for local development.
    """
    if os.environ.get("HUB_DEV"):
        return True
    return request.url.scheme in ("https", "wss")


# Direct admin access outside Home Assistant (HA login on :8100, session
# cookie, session as Bearer, admin WebSocket with the cookie). Off by default;
# only an Ingress request switches it (require_ingress). The value lives in the
# settings table and is cached here, the single process reads it per request.
DIRECT_ADMIN_SETTING = "admin_direct_access"
DIRECT_ADMIN_OFF_REASON = "admin_ingress_only"
DIRECT_ADMIN_OFF_DETAIL = "The admin page is only available through Home Assistant."
_direct_admin_enabled = False


def direct_admin_enabled() -> bool:
    """True when admins may log in outside Home Assistant."""
    return _direct_admin_enabled


async def load_direct_admin_setting(db: Database) -> bool:
    """Read the stored setting into the cache (startup)."""
    global _direct_admin_enabled
    _direct_admin_enabled = await db.get_setting(DIRECT_ADMIN_SETTING) == "true"
    return _direct_admin_enabled


async def set_direct_admin_enabled(db: Database, enabled: bool) -> int:
    """Store the setting; switching it off ends every direct session.

    Returns the number of sessions ended. Their admin sockets are closed by
    the caller (api.ws.close_all_session_sockets).
    """
    global _direct_admin_enabled
    await db.set_setting(DIRECT_ADMIN_SETTING, "true" if enabled else "false")
    _direct_admin_enabled = enabled
    if enabled:
        return 0
    ended = len(_sessions)
    _sessions.clear()
    return ended


def direct_admin_off_response(status_code: int = 403) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": DIRECT_ADMIN_OFF_DETAIL, "reason": DIRECT_ADMIN_OFF_REASON},
    )


TLS_REQUIRED_DETAIL = (
    "Admin access outside Home Assistant is only available over HTTPS "
    f"(port 8100). Use the {BRAND_NAME} panel in the Home Assistant sidebar or "
    "https://<host>:8100."
)

# ── Admin audit ────────────────────────────────────────────────

# Read requests on these prefixes are audited; every state-changing admin
# request is audited regardless of path.
_AUDITED_READ_PREFIXES = (
    "/api/auth/",
    "/api/admin/",
    "/api/export",
    "/api/pairing/status",
    "/api/pairing/clients",
    "/api/settings/tls",
)
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _should_audit(method: str, path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    return method not in _SAFE_METHODS or path.startswith(_AUDITED_READ_PREFIXES)


def ingress_user(headers) -> str | None:
    """HA user of an Ingress request (header set by the Supervisor proxy).

    Only meaningful once the source was verified as the Ingress proxy.
    """
    return headers.get("x-remote-user-name") or headers.get("x-remote-user-id")


def _audit_admin_request(
    request: Request, auth_type: str, user: str | None = None
) -> None:
    if _should_audit(request.method, request.url.path):
        log_admin_access(
            request.method,
            request.url.path,
            auth_type,
            _get_client_ip(request),
            user,
        )


# ── Hashing helpers ─────────────────────────────────────────────────────────


def hash_secret(secret: str) -> str:
    """Hash a secret using SHA-256 for storage."""
    return hashlib.sha256(secret.encode()).hexdigest()


def verify_secret(plain: str, hashed: str) -> bool:
    """Constant-time comparison of a plain secret against its hash."""
    return hmac.compare_digest(hash_secret(plain), hashed)


async def rotate_integration_token(db: Database) -> str:
    """Mint a new dedicated integration API token and return its plaintext.

    Only the SHA-256 hash is persisted (settings key ``integration_token_hash``);
    the plaintext exists in memory just long enough to be handed out (discovery
    payload or the regenerate endpoint's one-time response). Any previously
    issued integration token is invalidated immediately. Never log the return
    value.
    """
    token = secrets.token_hex(32)
    await db.set_setting("integration_token_hash", hash_secret(token))
    await db.set_setting(PENDING_INTEGRATION_TOKEN_KEY, "")
    # An explicit regenerate revokes: the old token is a plain failure.
    _forget_replaced_integration_token()
    logger.info("Integration token rotated (stored as hash)")
    return token


# Discovery hands a NEW integration token to HA; it becomes the only valid one
# once the Supervisor accepted the announce. Until then both the
# current and the staged token are accepted: HA may start using the new token
# before the announce call returns here, and a failed announce must not lock
# out the integration that still holds the current one.
PENDING_INTEGRATION_TOKEN_KEY = "integration_token_pending_hash"


async def stage_integration_token(db: Database) -> str:
    """Mint a token that is valid next to the current one. Never log it."""
    token = secrets.token_hex(32)
    await db.set_setting(PENDING_INTEGRATION_TOKEN_KEY, hash_secret(token))
    return token


# The token replaced by the last discovery rotation (hash, in memory only) and
# until when a request with it counts as expected: after a simultaneous Core
# and hub start the integration presents it once, then takes over the
# announced token. Such a request is still refused (401); it is only logged
# as information and not counted as an authentication failure.
REPLACED_TOKEN_NOTICE_SECONDS = 600.0
_replaced_integration_token: tuple[str, float] | None = None


def _forget_replaced_integration_token() -> None:
    global _replaced_integration_token
    _replaced_integration_token = None


def is_recently_replaced_integration_token(token: str) -> bool:
    """True for the token the last start replaced, shortly after the switch."""
    replaced = _replaced_integration_token
    if not token or replaced is None:
        return False
    stored, until = replaced
    return time.monotonic() < until and verify_secret(token, stored)


async def activate_integration_token(db: Database, token: str) -> None:
    """Make the staged ``token`` the only valid integration token."""
    global _replaced_integration_token
    previous = await db.get_setting("integration_token_hash")
    # Set before the switch: a request with the previous token that is
    # checked while the switch commits must already find it here.
    _replaced_integration_token = (
        (previous, time.monotonic() + REPLACED_TOKEN_NOTICE_SECONDS)
        if previous and previous != hash_secret(token)
        else None
    )
    async with db.transaction():
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("integration_token_hash", hash_secret(token)),
        )
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, '')",
            (PENDING_INTEGRATION_TOKEN_KEY,),
        )
    logger.info("Integration token rotated (stored as hash)")


async def discard_staged_integration_token(db: Database, token: str) -> None:
    """Drop the staged ``token`` (announce failed); the current one stays."""
    if await db.get_setting(PENDING_INTEGRATION_TOKEN_KEY) == hash_secret(token):
        await db.set_setting(PENDING_INTEGRATION_TOKEN_KEY, "")


async def integration_token_valid(db: Database, token: str) -> bool:
    """True for the current integration token or a staged one.

    Both hashes are read in ONE statement: read one by one, a check running
    while activate_integration_token switches them could see the current
    hash from before and the pending one from after the switch, and refuse
    the token HA just received.
    """
    if not token:
        return False
    rows = await db.fetchall(
        "SELECT value FROM settings WHERE key IN (?, ?)",
        ("integration_token_hash", PENDING_INTEGRATION_TOKEN_KEY),
    )
    return any(row["value"] and verify_secret(token, row["value"]) for row in rows)


# ── Session management ──────────────────────────────────────────────────────

# In-memory session store: {token: {"username": str, "created_at": float,
#   "expires_at": float, "user_agent": str}}
# Sessions are bound to User-Agent to limit stolen cookie reuse.
_sessions: dict[str, dict] = {}
_session_last_cleanup: float = 0.0

# Session lifetime: 24 hours
SESSION_LIFETIME = 86400
# Session cleanup interval: purge expired sessions every 5 minutes
_SESSION_CLEANUP_INTERVAL = 300  # seconds
# Maximum active sessions per user (prevents unbounded memory growth)
MAX_SESSIONS_PER_USER = 10
# Maximum total sessions (absolute cap)
MAX_TOTAL_SESSIONS = 500

SESSION_COOKIE_NAME = "hub_session"
CSRF_COOKIE_NAME = "hub_csrf"
CSRF_HEADER_NAME = "x-csrf-token"

# HTTP methods that require CSRF validation (state-changing)
CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _cleanup_expired_sessions():
    """Remove expired sessions from memory (runs at most once per cleanup interval)."""
    global _session_last_cleanup
    now = time.time()

    # Only run cleanup periodically to avoid O(n) scan on every session creation
    if now - _session_last_cleanup < _SESSION_CLEANUP_INTERVAL:
        return

    expired = [t for t, s in _sessions.items() if s["expires_at"] < now]
    for t in expired:
        del _sessions[t]
    _session_last_cleanup = now
    if expired:
        logger.debug("Cleaned up %d expired sessions", len(expired))


def _create_session(username: str, user_agent: str = "") -> str:
    """Create a new session and return the token.

    Enforces per-user and total session limits.
    Binds session to User-Agent.
    """
    _cleanup_expired_sessions()

    # Enforce per-user session limit: remove oldest sessions for this user
    user_sessions = [(t, s) for t, s in _sessions.items() if s["username"] == username]
    if len(user_sessions) >= MAX_SESSIONS_PER_USER:
        # Sort by created_at, remove oldest
        user_sessions.sort(key=lambda x: x[1]["created_at"])
        for old_token, _ in user_sessions[
            : len(user_sessions) - MAX_SESSIONS_PER_USER + 1
        ]:
            _sessions.pop(old_token, None)

    # Enforce total session limit
    while len(_sessions) >= MAX_TOTAL_SESSIONS:
        # Remove the globally oldest session
        oldest_token = min(_sessions, key=lambda t: _sessions[t]["created_at"])
        del _sessions[oldest_token]

    token = secrets.token_hex(32)
    csrf_token = secrets.token_hex(32)
    now = time.time()
    _sessions[token] = {
        "username": username,
        "created_at": now,
        "expires_at": now + SESSION_LIFETIME,
        "user_agent": user_agent,
        "csrf_token": csrf_token,
    }
    logger.info("Created session for user: %s (total: %d)", username, len(_sessions))
    return token


def _validate_session(token: str, user_agent: str = "") -> dict | None:
    """Validate a session token using constant-time comparison.

    Returns session data or None. Checks User-Agent binding.
    """
    # Switched off, no direct session counts (they were ended with the switch).
    if not _direct_admin_enabled:
        return None
    # Piggyback periodic cleanup on validation (hot path)
    _cleanup_expired_sessions()

    # Use constant-time lookup: iterate all sessions
    matched_token = None
    matched_session = None
    for stored_token, session in _sessions.items():
        if hmac.compare_digest(stored_token.encode(), token.encode()):
            matched_token = stored_token
            matched_session = session
            break

    if matched_token is None:
        return None

    if matched_session["expires_at"] < time.time():
        del _sessions[matched_token]
        return None

    # Check User-Agent binding (if session has one stored)
    stored_ua = matched_session.get("user_agent", "")
    if (
        stored_ua
        and user_agent
        and not hmac.compare_digest(stored_ua.encode(), user_agent.encode())
    ):
        logger.warning(
            "Session UA mismatch for user %s (possible session theft)",
            matched_session["username"],
        )
        return None
    return matched_session


def _destroy_session(token: str):
    """Destroy a session."""
    # Use constant-time lookup
    to_delete = None
    for stored_token in _sessions:
        if hmac.compare_digest(stored_token.encode(), token.encode()):
            to_delete = stored_token
            break
    if to_delete:
        del _sessions[to_delete]


# ── Ingress validation ──────────────────────────────────────────────────────


def _is_trusted_ingress(request: Request) -> bool:
    """Validate that an Ingress request comes from the Supervisor Ingress proxy.

    Checks:
    1. X-Ingress-Path header must be present
    2. Source IP must be the Supervisor Ingress proxy 172.30.32.2 (loopback and
       the rest of the hassio network only with the HUB_DEV flag)

    Any other add-on or host process can set the header, so it is ignored from
    every other source.
    """
    return is_trusted_ingress(request, "HTTP")


# ── Auth Middleware ──────────────────────────────────────────────────────────


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce authentication on external requests.

    No authentication required for:
    1. Ingress requests (validated: X-Ingress-Path header + trusted source IP)
    2. Public paths (health, docs, login)
    3. Static file paths (/_app/, /images/, favicon)
    4. WebSocket upgrade requests (handled by WS auth)

    For /api/ paths: Bearer token required (API token, integration token, or session token)
    For non-/api/ paths (frontend): session cookie required, redirect to login if missing
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Default: no auth info on request.state
        request.state.auth_type = None  # "ingress" | "admin" | "session" | "client"
        request.state.client_id = (
            None  # paired client UUID (only for auth_type="client")
        )

        # 1. Ingress - validated against trusted source IP
        if _is_trusted_ingress(request):
            request.state.auth_type = "ingress"
            request.state.actor = ingress_user(request.headers)
            _audit_admin_request(request, "ingress", request.state.actor)
            return await call_next(request)

        # 2. Public paths - always accessible
        if path in PUBLIC_PATHS:
            return await call_next(request)

        # 3. Public prefixes - static assets, pairing, login
        for prefix in PUBLIC_PREFIXES:
            if path.startswith(prefix):
                return await call_next(request)

        # 4. Real WebSocket upgrades are handled by the ASGI "websocket" path,
        # which never reaches this BaseHTTPMiddleware (it only sees "http").
        # An "Upgrade: websocket" request header alone goes through normal
        # authentication. Only a genuine ASGI websocket scope skips, which
        # never occurs here; the /api/ws route enforces its own auth.
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        # --- Authentication required from here ---

        db = getattr(request.app.state, "db", None)
        if not db:
            # DB not ready yet (startup) - reject instead of allowing through.
            # Return (not raise): raising HTTPException inside a
            # BaseHTTPMiddleware.dispatch is not routed through FastAPI's
            # exception handlers and surfaces as an unhandled ASGI error
            # (500 + full ExceptionGroup traceback) instead of a clean status.
            return JSONResponse(
                status_code=503,
                content={"detail": "Service starting up. Please try again shortly."},
            )

        auth_header = request.headers.get("authorization", "")
        if path in CLIENT_SECRET_PATHS:
            client_id = None
            if auth_header.startswith("Bearer "):
                client_id = await self._validate_client_secret(
                    db, auth_header[7:].strip()
                )
            if not client_id:
                log_auth_failure("client", "invalid_secret", _get_client_ip(request))
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized. Invalid or revoked token."},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.auth_type = "client"
            request.state.client_id = client_id
            return await call_next(request)

        # Try all auth methods in order:
        # 1. Bearer token (API, integration or session token)
        # 2. Session cookie (for web UI)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            auth_info = await self._validate_token(db, token)
            if auth_info and auth_info["auth_type"] in ("admin", "session"):
                if not _is_secure_transport(request):
                    logger.warning(
                        "Admin credential over plain HTTP refused: %s %s",
                        request.method,
                        path,
                    )
                    log_auth_failure(
                        auth_info["auth_type"], "plain_http", _get_client_ip(request)
                    )
                    return JSONResponse(
                        status_code=403, content={"detail": TLS_REQUIRED_DETAIL}
                    )
            if auth_info:
                auth_type = auth_info["auth_type"]

                # The integration token is scoped: it opens ONLY
                # /api/integration/*. Reject it anywhere else.
                if auth_type == "integration" and not path.startswith(
                    "/api/integration/"
                ):
                    logger.warning(
                        "Integration token used outside its scope: %s %s",
                        request.method,
                        path,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": "The integration token is only valid "
                            "for /api/integration/* endpoints."
                        },
                    )

                # The integration token counts only from this host:
                # it sits in every HA Core backup, the LAN must not use it.
                if auth_type == "integration" and not integration_source.source_allowed(
                    _get_client_ip(request)
                ):
                    log_auth_failure(
                        "integration", "untrusted_source", _get_client_ip(request)
                    )
                    logger.warning(
                        "Integration token from a foreign address refused: %s %s",
                        request.method,
                        path,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": "The integration token is only accepted "
                            "from Home Assistant on this host."
                        },
                    )

                # Telemetry: every authenticated integration request updates
                # the in-memory tracker (see services/integration_status.py).
                if auth_type == "integration":
                    record_request(request.headers.get(INTEGRATION_VERSION_HEADER))

                request.state.auth_type = auth_type
                request.state.client_id = auth_info.get("client_id")
                if auth_type in _ADMIN_AUTH_TYPES:
                    _audit_admin_request(request, auth_type, auth_info.get("username"))
                return await call_next(request)

            # Invalid token on API path -> 401
            if path.startswith("/api/"):
                if path.startswith(
                    "/api/integration/"
                ) and is_recently_replaced_integration_token(token):
                    # Expected once after a simultaneous Core and add-on
                    # start: the integration retries with the token announced
                    # via discovery. Refused all the same, but no failure.
                    logger.info(
                        "Integration used the token replaced at this start: "
                        "%s %s refused; it takes over the announced token",
                        request.method,
                        path,
                    )
                elif path.startswith("/api/integration/"):
                    # Help the operator diagnose the GET /api/integration/status
                    # 401 spam: the HA integration is polling with a stale
                    # integration token (the hub rotates it on every start
                    # under the Supervisor and re-announces via discovery).
                    record_auth_failure()
                    logger.warning(
                        "Unauthorized API access (invalid token): %s %s — "
                        "integration token invalid/stale — restart the add-on "
                        "to re-announce discovery, or regenerate the "
                        "integration token in Settings for manual setups",
                        request.method,
                        path,
                    )
                else:
                    logger.warning(
                        "Unauthorized API access (invalid token): %s %s",
                        request.method,
                        path,
                    )
                # Return (not raise): a raised HTTPException here escapes
                # FastAPI's handlers and becomes a 500 + ExceptionGroup
                # traceback on every rejected request.
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized. Invalid or revoked token."},
                    headers={"WWW-Authenticate": "Bearer"},
                )

        # Check session cookie (with User-Agent binding)
        session_token = request.cookies.get(SESSION_COOKIE_NAME)
        if session_token and _is_secure_transport(request):
            ua = request.headers.get("user-agent", "")
            session = _validate_session(session_token, ua)
            if session:
                # CSRF validation for state-changing requests via cookies
                if request.method in CSRF_METHODS:
                    csrf_header = request.headers.get(CSRF_HEADER_NAME, "")
                    csrf_expected = session.get("csrf_token", "")
                    if (
                        not csrf_header
                        or not csrf_expected
                        or not hmac.compare_digest(
                            csrf_header.encode(), csrf_expected.encode()
                        )
                    ):
                        logger.warning(
                            "CSRF validation failed: %s %s",
                            request.method,
                            path,
                        )
                        # Return (not raise): see note above — keeps this a
                        # clean 403 instead of a 500 + traceback.
                        return JSONResponse(
                            status_code=403,
                            content={
                                "detail": "CSRF token missing or invalid. Refresh the page."
                            },
                        )
                request.state.auth_type = "session"
                request.state.actor = session.get("username")
                _audit_admin_request(request, "session", session.get("username"))
                return await call_next(request)

        # No valid auth found
        if path.startswith("/api/"):
            if path.startswith("/api/integration/"):
                record_auth_failure()
                logger.warning(
                    "Unauthorized API access (no auth): %s %s — "
                    "integration endpoints require the dedicated integration "
                    "token (announced via Supervisor discovery, or generated "
                    "in Settings for manual setups)",
                    request.method,
                    path,
                )
            else:
                logger.warning(
                    "Unauthorized API access (no auth): %s %s", request.method, path
                )
                if not _direct_admin_enabled:
                    # The admin UI shows its "only through Home Assistant" page.
                    return direct_admin_off_response(401)
            # Return (not raise): see note above — keeps this a clean 401
            # instead of a 500 + ExceptionGroup traceback every request.
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Unauthorized. Provide a valid Bearer token or login via web UI."
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Non-API path (frontend page) without session -> serve login page
        # The SPA will detect missing session and show login
        return await call_next(request)

    @staticmethod
    async def _validate_client_secret(db, token: str) -> str | None:
        """Client id of an active paired device with this secret, else None.

        Only used for :data:`CLIENT_SECRET_PATHS`.
        """
        if not token or len(token) > 256:
            return None
        row = await db.fetchone(
            "SELECT id, secret_hash FROM paired_clients WHERE secret_hash = ? "
            "AND is_active = 1 AND pending_deletion_at IS NULL",
            (hash_secret(token),),
        )
        if not row or not verify_secret(token, row["secret_hash"]):
            return None
        return row["id"]

    @staticmethod
    async def _validate_token(db: Database, token: str) -> dict | None:
        """Validate a Bearer token against the API, integration and session tokens.

        Uses constant-time comparison (hmac.compare_digest) to prevent timing attacks.
        Secrets are stored as SHA-256 hashes in the database.

        Returns dict with auth_type and optional client_id, or None if invalid.
        """
        if not token:
            return None

        # Check admin API token (stored as hash)
        api_token_hash = await db.get_setting("api_token_hash")
        if api_token_hash and verify_secret(token, api_token_hash):
            return {"auth_type": "admin"}

        # Check the dedicated integration token (stored as hash). Scoped to
        # /api/integration/* — the middleware enforces the path restriction.
        if await integration_token_valid(db, token):
            return {"auth_type": "integration"}

        # Paired-client secrets are not a credential here: apps use the
        # WebSocket and, with their secret as Bearer, only the paths in
        # CLIENT_SECRET_PATHS (handled before this lookup).

        # Check session tokens (e.g. used as Bearer from frontend)
        session = _validate_session(token)
        if session:
            return {"auth_type": "session", "username": session.get("username")}

        return None


# ── Role-based access helpers ───────────────────────────────────────────────

# Auth types that have admin privileges
_ADMIN_AUTH_TYPES = {"ingress", "admin", "session"}


def require_admin(request: Request) -> None:
    """Raise 403 if the caller is not an admin-level user.

    Admin-level auth types: ingress, admin (API token), session (HA login).
    Paired clients (auth_type="client") are rejected.
    """
    auth_type = getattr(request.state, "auth_type", None)
    if auth_type not in _ADMIN_AUTH_TYPES:
        raise HTTPException(
            status_code=403,
            detail="Admin access required. Paired clients cannot access this endpoint.",
        )


def require_ingress(request: Request) -> None:
    """Raise 403 unless the request came through Home Assistant Ingress.

    For settings that decide who may administer the hub at all: a direct
    session or the admin API token cannot change them.
    """
    if getattr(request.state, "auth_type", None) != "ingress":
        raise HTTPException(
            status_code=403,
            detail="Only available in the Home Assistant sidebar (Ingress).",
        )


def require_integration(request: Request) -> None:
    """Raise 403 unless the caller authenticated with the integration token.

    /api/integration/* accepts EXACTLY the dedicated integration token —
    admin tokens, sessions, Ingress and paired-client secrets are rejected.
    The token itself is scoped to these paths by the middleware, so together
    this yields a clean 1:1 mapping of token <-> surface.
    """
    if getattr(request.state, "auth_type", None) != "integration":
        raise HTTPException(
            status_code=403,
            detail="Integration token required. These endpoints accept only "
            "the dedicated integration token.",
        )


# ── HA admin check for the web login ──────────────────────────

HA_ADMIN_GROUP_ID = "system-admin"


def _group_ids_from_auth_response(resp) -> list[str]:
    """``group_ids`` reported by Supervisor ``/auth``, or [] if absent.

    Supervisor 2026.11+ answers ``{"result": "ok", "data": {"group_ids": [...]}}``
    when HA Core reports them (Core includes ``system-admin`` for owners and
    admins); older Supervisor/Core versions return no groups.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 - empty or non-JSON body
        return []
    data = body.get("data") if isinstance(body, dict) else None
    group_ids = data.get("group_ids") if isinstance(data, dict) else None
    if not isinstance(group_ids, list):
        return []
    return [g for g in group_ids if isinstance(g, str)]


async def _resolve_is_admin(request: Request, username: str, auth_resp) -> bool:
    """Whether the HA user who just logged in is an HA administrator.

    1. ``group_ids`` from the Supervisor ``/auth`` answer, when reported.
    2. Otherwise HA's user list (``config/auth/list`` over the Supervisor core
       proxy; the proxy acts as the admin Supervisor user), matched on the
       normalized username (``strip().casefold()``, as HA's auth provider).

    Raises when the user list cannot be read; the caller fails closed.
    """
    group_ids = _group_ids_from_auth_response(auth_resp)
    if group_ids:
        return HA_ADMIN_GROUP_ID in group_ids

    ha_client = getattr(request.app.state, "ha_client", None)
    if ha_client is None:
        raise RuntimeError("no Home Assistant client")
    users = await ha_client._ws_command({"type": "config/auth/list"})
    wanted = username.strip().casefold()
    for user in users or []:
        name = user.get("username")
        if not isinstance(name, str) or name.strip().casefold() != wanted:
            continue
        if user.get("system_generated") or not user.get("is_active", True):
            return False
        return bool(user.get("is_owner")) or HA_ADMIN_GROUP_ID in (
            user.get("group_ids") or []
        )
    return False


# ── Login schemas ───────────────────────────────────────────────────────────


class LoginRequest(BaseModel):
    """HA login credentials."""

    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    """Login response with session info."""

    success: bool
    username: str | None = None
    message: str | None = None


# ── Auth endpoints ──────────────────────────────────────────────────────────


@router.post("/auth/login", response_model=LoginResponse)
async def login(request: Request, data: LoginRequest):
    """Login with Home Assistant credentials.

    Validates username/password against the HA Auth API via Supervisor proxy.
    On success, sets a session cookie for web UI access. Only while direct
    admin access is switched on (default off): otherwise 403 with reason
    ``admin_ingress_only`` before the credentials are checked.

    Rate limited: 5 attempts per IP per minute; 5 failures per account and
    address in 15 minutes; an account is blocked as a whole only after 30
    failures from at least 5 addresses.
    """
    client_ip = _get_client_ip(request)

    # Direct admin access is off: refused before anything is checked.
    if not _direct_admin_enabled:
        log_auth_failure("ha_login", DIRECT_ADMIN_OFF_REASON, client_ip)
        return direct_admin_off_response()

    # The HA password never travels in clear text over the LAN.
    if not _is_secure_transport(request):
        logger.warning("Login over plain HTTP refused from %s", client_ip)
        log_auth_failure("ha_login", "plain_http", client_ip)
        raise HTTPException(status_code=403, detail=TLS_REQUIRED_DETAIL)

    # Rate limiting: every attempt per IP; failed attempts per account and
    # address, and per account only from many addresses.
    rate_key = f"login:{client_ip}"
    user_key = data.username.strip().casefold()
    if _login_blocked(user_key, client_ip) or not _rate_limit_check(rate_key):
        logger.warning("Login rate limit exceeded for IP: %s", client_ip)
        log_rate_limited("login", client_ip)
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please try again later.",
        )

    supervisor_token = getattr(request.app.state, "ha_client", None)
    if supervisor_token:
        supervisor_token = supervisor_token._token
    else:
        supervisor_token = ""

    # Validate against HA Auth API
    # POST http://supervisor/auth with JSON body + X-Supervisor-Token header
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "http://supervisor/auth",
                headers={
                    "X-Supervisor-Token": supervisor_token,
                    "Content-Type": "application/json",
                },
                json={"username": data.username, "password": data.password},
            )

        if resp.status_code == 200:
            # Valid HA credentials. Only HA administrators become Halumar
            # admins; undeterminable rights fail closed.
            try:
                is_admin = await _resolve_is_admin(request, data.username, resp)
            except Exception as e:  # noqa: BLE001 - fail closed, never 500
                logger.error(
                    "Could not verify admin rights of '%s': %s", data.username, e
                )
                log_auth_failure("ha_login", "admin_check_failed", client_ip)
                raise HTTPException(
                    status_code=503,
                    detail="Could not verify Home Assistant admin rights. "
                    "Please try again later.",
                )
            if not is_admin:
                logger.warning(
                    "Login refused for non-admin HA user '%s' from %s",
                    data.username,
                    client_ip,
                )
                log_auth_failure("ha_login", "not_admin", client_ip)
                raise HTTPException(
                    status_code=403,
                    detail="Only Home Assistant administrators can sign in to "
                    f"{BRAND_NAME}.",
                )

            # Auth successful - create session (bound to User-Agent)
            user_agent = request.headers.get("user-agent", "")
            session_token = _create_session(data.username, user_agent)

            # Determine secure flag from actual connection, not headers
            # In the HA Add-on context, direct access is always HTTP (port 8099).
            # HTTPS is only via Ingress (which does not reach this path) or a reverse proxy.
            # We check the actual URL scheme as primary source; XFF proto as fallback
            # only for reverse proxy setups where scheme is rewritten.
            # Login only happens over TLS (checked above): cookies are
            # always Secure, never derived from X-Forwarded-Proto.
            is_https = True

            # Get CSRF token from session for the cookie
            session_data = _validate_session(session_token, user_agent)
            csrf_token = session_data.get("csrf_token", "") if session_data else ""

            response = JSONResponse(
                content={
                    "success": True,
                    "username": data.username,
                    "message": "Login successful",
                }
            )
            response.set_cookie(
                key=SESSION_COOKIE_NAME,
                value=session_token,
                max_age=SESSION_LIFETIME,
                httponly=True,
                samesite="lax",
                secure=is_https,
                path="/",
            )
            # CSRF token cookie (readable by JavaScript, NOT httponly)
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=csrf_token,
                max_age=SESSION_LIFETIME,
                httponly=False,  # Must be readable by frontend JS
                samesite="lax",
                secure=is_https,
                path="/",
            )
            logger.info("User '%s' logged in successfully", data.username)
            log_auth_success("ha_login", client_id=data.username, ip=client_ip)
            return response
        else:
            logger.warning(
                "Failed login attempt for user '%s' from %s",
                data.username,
                client_ip,
            )
            log_auth_failure("ha_login", "invalid_credentials", client_ip)
            _record_login_failure(user_key, client_ip)
            raise HTTPException(
                status_code=401,
                detail="Invalid username or password.",
            )

    except httpx.ConnectError:
        logger.error("Cannot connect to HA Supervisor for auth validation")
        raise HTTPException(
            status_code=502,
            detail="Cannot connect to Home Assistant for authentication.",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Auth validation error: %s", e)
        raise HTTPException(
            status_code=500,
            detail="Authentication service error.",
        )


@router.post("/auth/logout")
async def logout(request: Request):
    """Logout - destroy session and clear cookie.

    A valid session is only ended with its CSRF token, like every
    other state-changing request of the session.
    """
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    session = (
        _validate_session(session_token, request.headers.get("user-agent", ""))
        if session_token
        else None
    )
    if session:
        csrf_header = request.headers.get(CSRF_HEADER_NAME, "")
        csrf_expected = session.get("csrf_token", "")
        if (
            not csrf_header
            or not csrf_expected
            or not hmac.compare_digest(csrf_header.encode(), csrf_expected.encode())
        ):
            logger.warning("Logout without valid CSRF token refused")
            raise HTTPException(
                status_code=403,
                detail="CSRF token missing or invalid. Refresh the page.",
            )
    if session_token:
        _destroy_session(session_token)
        # Live admin sockets of this session end now (close 4401).
        from api import ws as api_ws

        await api_ws.close_session_sockets(session_token)

    # From the connection itself, never from X-Forwarded-Proto.
    is_https = request.url.scheme == "https"

    response = JSONResponse(content={"success": True, "message": "Logged out"})
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    # Clear CSRF cookie on logout
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        httponly=False,
        samesite="lax",
        secure=is_https,
        path="/",
    )
    logger.info("User logged out")
    return response


@router.get("/auth/session")
async def check_session(request: Request):
    """Check if the current session is valid.

    Returns session info if authenticated, otherwise {authenticated: false,
    direct_access} and, with direct access off, ha_url (Home Assistant in
    the home network, or null). The frontend shows the login page or the
    "only through Home Assistant" page.
    """
    # Ingress - validated against trusted source IP
    if _is_trusted_ingress(request):
        return {"authenticated": True, "method": "ingress"}

    # Check session cookie (sessions are TLS-only, see AuthMiddleware)
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    if session_token and _is_secure_transport(request):
        ua = request.headers.get("user-agent", "")
        session = _validate_session(session_token, ua)
        if session:
            return {
                "authenticated": True,
                "method": "session",
                "username": session["username"],
            }

    # Check Bearer token
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        db = getattr(request.app.state, "db", None)
        if db:
            # Check API token (hashed)
            api_token_hash = await db.get_setting("api_token_hash")
            if (
                api_token_hash
                and verify_secret(token, api_token_hash)
                and _is_secure_transport(request)
            ):
                return {"authenticated": True, "method": "api_token"}

    if _direct_admin_enabled:
        return {"authenticated": False, "direct_access": True}
    # Off: the page points to Home Assistant (its address in the home network).
    ha_url = None
    db = getattr(request.app.state, "db", None)
    if db:
        ha_url = await db.get_setting("ha_internal_url") or None
    return {"authenticated": False, "direct_access": False, "ha_url": ha_url}


# ── API Token management endpoints ──────────────────────────────────────────


@router.get("/auth/token")
async def get_api_token(request: Request):
    """Check if an API token exists (does NOT return the actual token).

    The token is only shown once when generated/regenerated.
    This endpoint just confirms a token exists.
    Admin-only: paired clients cannot access this endpoint.
    """
    require_admin(request)
    db: Database = request.app.state.db
    return {"has_token": bool(await db.get_setting("api_token_hash"))}


@router.post("/auth/token/regenerate")
async def regenerate_api_token(request: Request):
    """Regenerate the API token. Returns the new token ONCE.

    The token is only shown at this moment - it cannot be retrieved again.
    Store it securely. All external clients using the old token will lose access.
    The token is stored as a SHA-256 hash in the database.
    Admin-only: paired clients cannot access this endpoint.
    """
    require_admin(request)
    db: Database = request.app.state.db
    new_token = secrets.token_hex(32)

    await db.set_setting("api_token_hash", hash_secret(new_token))

    logger.info("API token regenerated (stored as hash)")
    log_token_generated("api_token", _get_client_ip(request))
    return {
        "api_token": new_token,
        "message": "Store this token securely. It will not be shown again.",
    }


# ── Integration token management endpoints ──────────────────────────────────
#
# The dedicated token for the companion HA integration (/api/integration/*).
# Under the Supervisor it is rotated on every hub start and handed to the
# integration via the discovery API (services/discovery.py) — these endpoints
# exist for MANUAL setups (container installs) and for inspection.


@router.get("/auth/integration-token")
async def get_integration_token_status(request: Request):
    """Report whether an integration token exists (never returns the token).

    ``supervisor_managed`` tells the UI that the token is rotated and
    delivered automatically on every hub start (discovery), so manual
    copying is only needed when it is False. Admin-only.
    """
    require_admin(request)
    db: Database = request.app.state.db
    has_hash = await db.get_setting("integration_token_hash")
    return {
        "has_token": bool(has_hash),
        "supervisor_managed": bool(os.environ.get("SUPERVISOR_TOKEN")),
    }


@router.post("/auth/integration-token/regenerate")
async def regenerate_integration_token(request: Request):
    """Rotate the integration token. Returns the new token ONCE.

    Invalidates the previous integration token immediately; a configured
    integration keeps failing (counted + logged) until it is reconfigured —
    or, under the Supervisor, until the next hub start re-announces the
    then-current token via discovery. Admin-only.
    """
    require_admin(request)
    db: Database = request.app.state.db
    new_token = await rotate_integration_token(db)

    log_token_generated("integration_token", _get_client_ip(request))
    return {
        "integration_token": new_token,
        "message": "Store this token securely. It will not be shown again.",
    }
