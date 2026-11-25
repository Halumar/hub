"""Handshake of /api/ws: endpoint, slots, auth, pairing, guest expiry."""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from services import state_relay
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from api import ws as api_ws
from api.auth import (
    SESSION_COOKIE_NAME,
    _is_secure_transport,
    _validate_session,
    ingress_user,
    integration_token_valid,
)
from api.ws import guest_invites as guest_invites_ws
from api.ws import onboarding as onboarding_ws
from api.ws.admin import _handle_integration_message
from api.ws.broadcast import EventAudience
from api.ws.client import _handle_client_message, _purge_tombstone_on_ack
from api.ws.registry import (
    ADMIN_SESSION_CLIENT_ID,
    discard_sender,
    INTEGRATION_CLIENT_ID,
    PSEUDO_CLIENT_IDS,
    _clients,
    get_connected_client_ids,
)
from models.database import Database
from services import (
    access,
    audit_log,
    client_activity,
    client_pairing,
    device_provisioning,
    enforcement,
    guest_invites,
    integration_source,
    protocol,
    tasks,
    watch_access,
)
from services.audit_log import log_admin_access, log_auth_failure, log_auth_success
from services.expiry import is_expired
from services.ingress import ip_is_trusted as ingress_ip_is_trusted
from services.ingress import is_trusted_ingress
from services.integration_status import (
    INTEGRATION_VERSION_HEADER,
    record_auth_failure,
    record_request,
)

logger = logging.getLogger("hub.api.websocket")
router = APIRouter(tags=["websocket"])


# ── Security constants ───────────────────────────────────────────────────────

# Maximum concurrent WebSocket connections (separate limits)
MAX_WS_CONNECTIONS_AUTH = 100  # authenticated clients
# Unauthenticated (pending auth/pair) connections are limited per source IP,
# so one LAN host cannot lock every app out of reconnecting and pairing. The global cap only bounds memory.
MAX_WS_UNAUTH_PER_IP = 5
MAX_WS_CONNECTIONS_UNAUTH = 200

# Timeout for unauthenticated connections: must auth within 30s
UNAUTH_TIMEOUT_SECONDS = 30

# Maximum pairing attempts per connection before disconnect
MAX_PAIR_ATTEMPTS = 5

# Maximum auth attempts per connection before disconnect (brute-force prevention)
MAX_AUTH_ATTEMPTS = 5

# Maximum incoming WebSocket message size (64 KB)
MAX_WS_MESSAGE_SIZE = 64 * 1024

# Frames that are no message (binary, no JSON, too deeply nested, no object):
# an unauthenticated connection is closed after this many (1008), so a peer
# cannot keep the hub answering and logging without limit.
MAX_INVALID_FRAMES = 5
INVALID_FRAMES_CLOSE_CODE = 1008

# Marker of a binary frame from _receive_text.
_BINARY = object()


async def _receive_text(websocket: WebSocket):
    """Next text frame; ``_BINARY`` for a binary one (``receive_text`` raised
    KeyError out of the handler for it)."""
    try:
        return await websocket.receive_text()
    except KeyError:
        # Starlette took the frame and found no "text" in it: binary.
        return _BINARY


async def _invalid_frame(websocket: WebSocket, authenticated: bool, text: str) -> bool:
    """Answer a frame that is no message; True when the connection was closed
    (unauthenticated and over MAX_INVALID_FRAMES)."""
    await websocket.send_json({"type": "error", "message": text})
    if authenticated:
        return False
    info = _clients.get(websocket)
    if info is None:
        return False
    info["invalid_frames"] = info.get("invalid_frames", 0) + 1
    if info["invalid_frames"] < MAX_INVALID_FRAMES:
        return False
    logger.warning(
        "WebSocket closed: %d invalid frames before authentication",
        info["invalid_frames"],
    )
    await websocket.close(code=INVALID_FRAMES_CLOSE_CODE, reason="Invalid messages")
    return True


# Admin web-UI sessions (session cookie): close code when the session ended
# (expired, logged out, evicted); the UI returns to its login page.
SESSION_EXPIRED_CLOSE_CODE = 4401
# How often an idle admin socket re-checks its session (seconds).
SESSION_RECHECK_SECONDS = 60

# Machine-readable reasons of auth_error / pair_error (contracts
# $defs/auth_error_reason, $defs/pair_error_reason). The message stays an
# English text for logs; clients decide on the reason only.
AUTH_INVALID_SECRET = "invalid_secret"
AUTH_REVOKED = "revoked"
AUTH_GUEST_EXPIRED = "guest_expired"
AUTH_RATE_LIMITED = "rate_limited"
AUTH_INVALID_CLIENT_INFO = "invalid_client_info"

PAIR_INVALID_REQUEST = "invalid_request"
PAIR_INVALID_CLIENT_INFO = "invalid_client_info"
PAIR_PIN_INVALID = "pin_invalid"
PAIR_PIN_EXPIRED = "pin_expired"
PAIR_TOO_MANY_FAILURES = "too_many_failures"
PAIR_RATE_LIMITED = "rate_limited"
PAIR_ALREADY_AUTHENTICATED = "already_authenticated"
PAIR_CONNECTION_EXPIRED = "connection_expired"
PAIR_PLATFORM_NOT_ALLOWED = "platform_not_allowed"

_AUTH_MESSAGES = {
    AUTH_INVALID_SECRET: "Invalid client secret",
    AUTH_REVOKED: "Client revoked",
    AUTH_GUEST_EXPIRED: "Guest access expired",
    AUTH_RATE_LIMITED: "Too many auth attempts. Reconnect to try again.",
}

_PAIR_MESSAGES = {
    PAIR_PIN_INVALID: "Invalid pairing PIN",
    PAIR_PIN_EXPIRED: "Pairing PIN expired",
    PAIR_TOO_MANY_FAILURES: "Pairing PIN invalidated after too many failed attempts",
    PAIR_ALREADY_AUTHENTICATED: "Already authenticated - pairing not needed",
    PAIR_CONNECTION_EXPIRED: "Connection expired. Reconnect to try again.",
}


async def _send_auth_error(
    websocket: WebSocket, reason: str, message: str | None = None
) -> None:
    await websocket.send_json(
        {
            "type": "auth_error",
            "reason": reason,
            "message": message or _AUTH_MESSAGES.get(reason, reason),
        }
    )


async def _send_pair_error(
    websocket: WebSocket, reason: str, message: str | None = None
) -> None:
    await websocket.send_json(
        {
            "type": "pair_error",
            "reason": reason,
            "message": message or _PAIR_MESSAGES.get(reason, reason),
        }
    )


def _pairing_module():
    """``api.pairing``, resolved lazily: it imports ``api.ws`` (the one cycle)."""
    import api.pairing

    return api.pairing


def _ws_client_ip(websocket: WebSocket) -> str:
    """Source IP of the TCP connection (never a forwarded header)."""
    client = getattr(websocket, "client", None)
    return getattr(client, "host", None) or "unknown"


def _conn_info(websocket: WebSocket) -> dict:
    """Registry entry of a connection ({} if unknown or not hashable)."""
    try:
        return _clients.get(websocket) or {}
    except TypeError:
        return {}


def _is_closed(websocket: WebSocket) -> bool:
    """True once our side closed the socket (purge, revoke, eviction)."""
    state = getattr(websocket, "application_state", None)
    return state is not None and state != WebSocketState.CONNECTED


def _same_origin(websocket: WebSocket) -> bool:
    """Origin of the page equals the host it connects to (no CSWSH).

    The session cookie is ``SameSite=Lax``, which does not stop a foreign
    page from opening a WebSocket with it; browsers always send ``Origin``.
    """
    origin = (websocket.headers.get("origin") or "").rstrip("/").lower()
    host = (websocket.headers.get("host") or "").lower()
    if not origin or not host:
        return False
    scheme = "https" if websocket.url.scheme in ("wss", "https") else "http"
    return hmac.compare_digest(origin.encode(), f"{scheme}://{host}".encode())


def _admin_session_token(websocket: WebSocket) -> str | None:
    """Session token of a valid admin web-UI cookie on this handshake, else None.

    Same rules as the HTTP AuthMiddleware: TLS only, User-Agent bound, and on
    top of it the Origin must match the Host.
    """
    token = (getattr(websocket, "cookies", None) or {}).get(SESSION_COOKIE_NAME)
    if not token or not _is_secure_transport(websocket):
        return None
    if not _same_origin(websocket):
        logger.warning(
            "WS session cookie with foreign Origin %r refused",
            websocket.headers.get("origin"),
        )
        log_auth_failure("ws_session", "origin_mismatch", _ws_client_ip(websocket))
        return None
    if not _validate_session(token, websocket.headers.get("user-agent", "")):
        return None
    return token


async def _end_admin_session(websocket: WebSocket) -> None:
    """Tell an admin socket its session ended and close it with 4401."""
    try:
        await websocket.send_json({"type": "session_expired"})
    except Exception:  # noqa: BLE001 - socket may already be gone
        pass
    try:
        await websocket.close(code=SESSION_EXPIRED_CLOSE_CODE, reason="Session expired")
    except Exception:  # noqa: BLE001
        pass
    _clients.pop(websocket, None)


async def close_session_sockets(session_token: str) -> int:
    """Close every admin socket bound to ``session_token`` (logout)."""
    closed = 0
    for ws, info in list(_clients.items()):
        bound = info.get("session_token")
        if bound and hmac.compare_digest(bound.encode(), session_token.encode()):
            await _end_admin_session(ws)
            closed += 1
    return closed


async def close_all_session_sockets() -> int:
    """Close every admin socket of a direct session (direct access switched off)."""
    closed = 0
    for ws, info in list(_clients.items()):
        if info.get("session_token"):
            await _end_admin_session(ws)
            closed += 1
    return closed


async def _reject_outdated_app(
    websocket: WebSocket, evaluation: protocol.Evaluation
) -> None:
    """Answer ``upgrade_required`` and close 4426."""
    # The socket may already be gone; the client then retries anyway.
    with contextlib.suppress(Exception):
        await websocket.send_json(protocol.upgrade_required_message(evaluation))
    with contextlib.suppress(Exception):
        await websocket.close(
            code=protocol.UPGRADE_REQUIRED_CLOSE_CODE, reason="Upgrade required"
        )


async def _store_client_info(
    db: Database, client_id: str, info: protocol.ClientInfo
) -> str | None:
    """Persist the reported client info; returns the previous app version.

    Never the platform: it is fixed at pairing (``protocol.same_device_platform``).
    """
    row = await db.fetchone(
        "SELECT app_version FROM paired_clients WHERE id = ?", (client_id,)
    )
    columns = {k: v for k, v in info.columns().items() if k != "platform"}
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET "
            + ", ".join(f"{k} = ?" for k in columns)
            + " WHERE id = ?",
            (*columns.values(), client_id),
        )
    return row.get("app_version") if row else None


async def _paired_platform(db: Database, client_id: str) -> str | None:
    """Platform stored for the device at pairing."""
    row = await db.fetchone(
        "SELECT platform FROM paired_clients WHERE id = ?", (client_id,)
    )
    return (row or {}).get("platform")


def _ip_is_trusted(client_ip: str) -> bool:
    """Return True if client_ip may carry Ingress trust (see services/ingress.py)."""
    return ingress_ip_is_trusted(client_ip)


# ── Security helpers ─────────────────────────────────────────────────────────


def _hash_secret(secret: str) -> str:
    """Hash a secret using SHA-256."""
    return hashlib.sha256(secret.encode()).hexdigest()


async def guest_expiry_loop(app) -> None:
    """Background task: check for expired guests every 60 seconds.

    - Disconnects active WS connections with close code 4003
    - Deletes expired guest records from the database
    - Syncs the guests sensor in HA
    - Broadcasts removal to admin UI
    """
    while True:
        await asyncio.sleep(60)
        try:
            db: Database = app.state.db

            # Compare as aware datetimes, never as text (string order is not
            # time order for "+01:00" values). Unparseable values count as
            # expired (fail closed).
            now = datetime.now(timezone.utc)
            guests = await db.fetchall(
                "SELECT id, name, expires_at FROM paired_clients "
                "WHERE access = 'guest' AND expires_at IS NOT NULL AND expires_at != ''"
            )
            expired = [g for g in guests if is_expired(g["expires_at"], now=now)]

            if not expired:
                continue

            from services import event_log

            for guest in expired:
                guest_id = guest["id"]
                guest_name = guest["name"]
                log_context = await event_log.client_context(db, guest_id)
                # Delete first, then close: a reconnect in between must not
                # authenticate against the still active row.
                async with db.transaction():
                    await db.execute(
                        "DELETE FROM paired_clients WHERE id = ?", (guest_id,)
                    )
                closed = await api_ws.disconnect_client(
                    guest_id, code=4003, reason="Guest session expired"
                )
                # Connection closed first, then the HA user goes.
                await device_provisioning.send_deprovision(
                    guest_id, device_provisioning.DEPROVISION_GUEST_EXPIRED
                )
                await event_log.device_removed(
                    db, guest_id, how="guest_expired", context=log_context
                )
                logger.info(
                    "Expired guest '%s' (id=%s) removed, closed %d connections",
                    guest_name,
                    guest_id,
                    closed,
                )

            # Broadcast removal to admin UIs
            await api_ws.broadcast(
                {
                    "type": "guests_expired",
                    "data": {
                        "count": len(expired),
                        "client_ids": [g["id"] for g in expired],
                    },
                },
                audience=EventAudience.ADMIN,
            )

            # Sync guests sensor in HA
            try:
                from services.ha_sensors import sync_guests_sensor

                ha_client = app.state.ha_client
                await sync_guests_sensor(db, ha_client)
            except Exception as e:
                logger.warning("Failed to sync guests sensor after expiry: %s", e)

        except asyncio.CancelledError:
            logger.info("Guest expiry loop stopped")
            break
        except Exception as e:
            logger.error("Guest expiry loop error: %s", e)


async def _match_pending_deletion(db: Database, secret: str) -> str | None:
    """Return the client_id of a pending-deletion row matching ``secret``.

    Does NOT delete anything: the signed tombstone (client_id + secret_hash)
    must survive until the device's authenticated wipe-ack
    (``client_reset_ack``) so the compliance endpoint keeps returning a
    signed "deleted". The caller replays the ``client_removed`` handshake on
    a match.
    """
    if not secret:
        return None
    secret_hash = _hash_secret(secret)
    row = await db.fetchone(
        "SELECT id FROM paired_clients "
        "WHERE secret_hash = ? AND pending_deletion_at IS NOT NULL",
        (secret_hash,),
    )
    return row["id"] if row else None


async def _check_client_secret(
    db: Database, secret: str
) -> tuple[str | None, str | None]:
    """Verify a client secret: ``(client_id, None)`` or ``(None, reason)``.

    ``reason`` is the ``auth_error`` reason: ``invalid_secret``
    (no device with this secret), ``revoked`` (deactivated by an admin) or
    ``guest_expired`` (the guest row is removed here).
    """
    if not secret or not isinstance(secret, str):
        return None, AUTH_INVALID_SECRET

    secret_hash = _hash_secret(secret)
    client = await db.fetchone(
        "SELECT id, is_active, (access = 'guest') AS is_guest, expires_at "
        "FROM paired_clients "
        "WHERE secret_hash = ? AND pending_deletion_at IS NULL",
        (secret_hash,),
    )
    if not client:
        return None, AUTH_INVALID_SECRET
    if not client.get("is_active"):
        return None, AUTH_REVOKED

    if client.get("is_guest"):
        # Aware datetime compare; a naive value is UTC, an
        # unparseable one counts as expired (fail closed).
        if client.get("expires_at") and is_expired(client["expires_at"]):
            # Expired guest - delete and reject
            async with db.transaction():
                await db.execute(
                    "DELETE FROM paired_clients WHERE id = ?",
                    (client["id"],),
                )
            await device_provisioning.send_deprovision(
                client["id"], device_provisioning.DEPROVISION_GUEST_EXPIRED
            )
            logger.info("Rejected expired guest client: %s", client["id"])
            return None, AUTH_GUEST_EXPIRED

    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET last_seen = ? WHERE id = ?",
            (db.now(), client["id"]),
        )
    return client["id"], None


async def _verify_client_secret(db: Database, secret: str) -> str | None:
    """Verify a client secret and return client_id if valid, else None."""
    client_id, _reason = await _check_client_secret(db, secret)
    return client_id


def _is_ingress(websocket: WebSocket) -> bool:
    """Check if the WebSocket connection comes through HA Ingress.

    Same rule as HTTP (services/ingress.py): X-Ingress-Path header AND source
    IP of the Supervisor Ingress proxy, preventing header spoofing by other
    add-ons or host processes.
    """
    return is_trusted_ingress(websocket, "WebSocket")


async def _auth_disabled(db: Database) -> bool:
    """Return True only for explicit dev-mode auto-auth (fail-closed).

    Dev auto-auth grants a fresh WebSocket admin access as client_id="dev"
    WITHOUT any credential. That must never happen implicitly just because no
    api_token has been configured yet (e.g. on a fresh install) — otherwise any
    external client could connect as admin. It is therefore gated behind an
    explicit operator opt-in (the HUB_DEV env var); without it a missing
    token does NOT disable auth and the connection must pair/auth normally.

    When the env opt-in IS set, auto-auth still applies only when no API token
    exists at all — checking both the plaintext ``api_token`` setting and the
    hash, since after token regeneration only ``api_token_hash`` exists.
    """
    if not os.environ.get("HUB_DEV"):
        return False
    token = await db.get_setting("api_token")
    token_hash = await db.get_setting("api_token_hash")
    return not token and not token_hash


# ── Pairing helper ───────────────────────────────────────────────────────────


async def _platform_allowed(db: Database, pin_data: dict, info) -> bool:
    """Whether the PIN's profile (else the default of the device type) is
    offered on the reported platform (``client_pairing.check_platform``)."""
    from services.profiles import store as profiles
    from services.profiles.catalog import ProfileError

    guest = bool(pin_data.get("is_guest"))
    profile_id = pin_data.get("profile_id")
    try:
        if not profile_id or not await profiles.profile_row(db, profile_id):
            profile_id = await profiles.default_profile_for(db, info.platform, guest)
        prof = await profiles.profile_row(db, profile_id)
        client_pairing.check_platform(prof, info.platform, guest=guest)
    except ProfileError as e:
        # A missing default profile is answered by the pairing itself.
        return e.code != client_pairing.PLATFORM_NOT_ALLOWED
    return True


async def _handle_pair(
    websocket: WebSocket, db: Database, message: dict
) -> tuple[bool, str | None]:
    """Handle pairing via WebSocket. Returns (success, client_id)."""
    from api.pairing import (
        _generate_secret,
        clear_ip_failures,
        match_pin,
        register_pin_failure,
        remove_pin,
        retired_pin_reason,
    )

    pin = message.get("pin", "")
    client_name = message.get("client_name", "")

    # Non-string values are a malformed request: no lookup, no PIN attempt.
    if not isinstance(pin, str) or not isinstance(client_name, str):
        await _send_pair_error(
            websocket, PAIR_INVALID_REQUEST, "'pin' and 'client_name' must be strings"
        )
        return False, None

    if not pin or not client_name:
        await _send_pair_error(
            websocket, PAIR_INVALID_REQUEST, "Both 'pin' and 'client_name' are required"
        )
        return False, None

    if len(client_name) > 100:
        await _send_pair_error(
            websocket,
            PAIR_INVALID_REQUEST,
            "client_name must be 100 characters or less",
        )
        return False, None

    # Client info before the PIN: a too old app must not use it up.
    evaluation = protocol.evaluate(message)
    if evaluation.upgrade_reason:
        logger.info(
            "Pairing refused, app too old (%s): platform=%s app_version=%s pv=%s",
            evaluation.upgrade_reason,
            evaluation.info.platform,
            evaluation.info.app_version,
            evaluation.info.protocol_version,
        )
        await _reject_outdated_app(websocket, evaluation)
        return False, None
    if evaluation.error:
        await _send_pair_error(websocket, PAIR_INVALID_CLIENT_INFO, evaluation.error)
        return False, None
    info = evaluation.info

    # Constant-time PIN comparison to prevent timing attacks. The
    # admin's PIN first, then the guest invitations of members.
    result = match_pin(pin)
    invite = None if result else guest_invites.match(pin)
    if invite is not None:
        from services.profiles import catalog as cat

        if cat.device_platform(info.platform) not in cat.kind_platforms("guest"):
            # A guest is offered only on the platforms of its kind; the
            # invitation stays open for the right device.
            await _send_pair_error(websocket, PAIR_PLATFORM_NOT_ALLOWED)
            return False, None
        invite_data = await guest_invites_ws.guest_from_invite(
            db, invite, info.platform
        )
        if invite_data is not None:
            result = (pin, invite_data)
        else:
            invite = None
            await _send_pair_error(websocket, PAIR_PIN_INVALID)
            return False, None

    if not result:
        # A recently expired or invalidated PIN is no guess: tell the device
        # why (it needs a new PIN) without counting a failure.
        retired = retired_pin_reason(pin)
        if retired:
            await _send_pair_error(websocket, retired)
            return False, None
        invalidated = await register_pin_failure(_ws_client_ip(websocket))
        await _send_pair_error(
            websocket, PAIR_TOO_MANY_FAILURES if invalidated else PAIR_PIN_INVALID
        )
        return False, None

    matched_pin, pin_data = result

    # Capture the PIN metadata BEFORE removing the PIN
    is_guest = pin_data.get("is_guest", False)
    guest_expires_at = pin_data.get("guest_expires_at")
    owner_label = pin_data.get("owner_label")
    set_up_by_client_id = pin_data.get("set_up_by_client_id")
    set_up_by_name = pin_data.get("set_up_by_name")
    # The PIN was shown on the integration's camera (pairing_qr_camera).
    camera_qr = bool(pin_data.get("qr_camera_shown"))

    if invite is None and not await _platform_allowed(db, pin_data, info):
        # A refused platform does not use up the PIN (the right device can
        # still pair); the attempt counts against the limits like a wrong PIN.
        await register_pin_failure(_ws_client_ip(websocket), "platform_not_allowed")
        await _send_pair_error(websocket, PAIR_PLATFORM_NOT_ALLOWED)
        return False, None

    # PIN is single-use - remove it (an invitation was taken above)
    if invite is None:
        remove_pin(matched_pin)
    clear_ip_failures(_ws_client_ip(websocket))

    # Permanent secret for this client (stored as hash only). The device
    # starts without an HA token: the integration creates its HA user and
    # delivers the token (announce_paired below). Profile: the one of
    # the PIN, else the default profile of the device type.
    from services.profiles.catalog import ProfileError

    try:
        client_id, client_secret = await client_pairing.insert_client(
            db,
            client_name=client_name,
            info=info,
            is_guest=is_guest,
            guest_expires_at=guest_expires_at,
            profile_id=pin_data.get("profile_id"),
            own_area_id=pin_data.get("own_area_id"),
            areas=pin_data.get("areas"),
            owner_label=owner_label,
            secret=_generate_secret(),
            set_up_by_client_id=set_up_by_client_id,
            set_up_by_name=set_up_by_name,
            set_up_method="invite" if set_up_by_client_id else None,
            overrides=pin_data.get("overrides"),
        )
    except ProfileError as e:
        logger.warning("Pairing refused: %s", e.code)
        reason = (
            PAIR_PLATFORM_NOT_ALLOWED
            if e.code == client_pairing.PLATFORM_NOT_ALLOWED
            else PAIR_INVALID_REQUEST
        )
        await _send_pair_error(websocket, reason, e.message)
        return False, None

    pin_type = "guest " if is_guest else ""
    logger.info(
        "Paired new %sclient via WebSocket: '%s' (id=%s)",
        pin_type,
        client_name,
        client_id,
    )

    response = {
        "type": "pair_ok",
        "client_id": client_id,
        "client_name": client_name,
        "secret": client_secret,
        **protocol.protocol_fields(),
    }
    if is_guest:
        response["is_guest"] = True
        response["expires_at"] = guest_expires_at

    await websocket.send_json(response)
    await client_pairing.announce_paired(
        db,
        getattr(websocket.app.state, "ha_client", None),
        client_id=client_id,
        client_name=client_name,
        info=info,
        is_guest=is_guest,
        method="invite" if invite is not None else "pin",
        invited_by=set_up_by_client_id,
        camera_qr=camera_qr,
        set_up={
            "kind": pin_data.get("kind") or "guest",
            "method": "invite",
            "by_client_id": set_up_by_client_id,
            "by_name": set_up_by_name or "",
        }
        if set_up_by_client_id
        else None,
    )
    if invite is not None:
        await guest_invites_ws.announce_guest(
            db, websocket, invite, client_id, client_name
        )
    return True, client_id


async def _handle_message(
    websocket: WebSocket, message: dict, authenticated: bool, client_id: str | None
) -> tuple[bool, str | None]:
    """Handle an incoming WebSocket message. Returns updated (authenticated, client_id)."""
    db: Database = websocket.app.state.db
    msg_type = message.get("type", "")

    # Integration push session: read-only (api/ws/admin.py).
    if authenticated and client_id == INTEGRATION_CLIENT_ID:
        await _handle_integration_message(websocket, msg_type)
        return authenticated, client_id

    # ── Pairing (allowed for unauthenticated clients) ────────────────────
    if msg_type == "pair":
        if authenticated:
            await _send_pair_error(websocket, PAIR_ALREADY_AUTHENTICATED)
            return authenticated, client_id

        # Enforce per-connection pairing attempt limit
        client_info = _clients.get(websocket)
        if not client_info:
            # Connection was evicted (e.g. by ping loop) — reject
            await _send_pair_error(websocket, PAIR_CONNECTION_EXPIRED)
            return authenticated, client_id
        pair_attempts = client_info.get("pair_attempts", 0)
        client_ip = _ws_client_ip(websocket)
        if pair_attempts >= MAX_PAIR_ATTEMPTS:
            await _send_pair_error(
                websocket,
                PAIR_RATE_LIMITED,
                "Too many pairing attempts. Reconnect to try again.",
            )
            logger.warning(
                "WS pairing attempt limit reached (%d/%d), rejecting",
                pair_attempts,
                MAX_PAIR_ATTEMPTS,
            )
            await _pairing_module().register_pin_failure(client_ip, "rate_limited")
            return authenticated, client_id
        if not _pairing_module().pair_ip_allowed(client_ip):
            await _send_pair_error(
                websocket,
                PAIR_RATE_LIMITED,
                "Too many pairing attempts. Please try again later.",
            )
            logger.warning("WS pairing rate limit exceeded for IP: %s", client_ip)
            await _pairing_module().register_pin_failure(client_ip, "rate_limited")
            return authenticated, client_id

        # Increment attempt counter
        if websocket in _clients:
            _clients[websocket]["pair_attempts"] = pair_attempts + 1

        success, new_client_id = await _handle_pair(websocket, db, message)
        if success:
            # Auto-authenticate after successful pairing
            authenticated = True
            client_id = new_client_id
            _clients[websocket] = {
                "authenticated": True,
                "client_id": client_id,
                "pair_attempts": pair_attempts + 1,
                "auth_attempts": _clients.get(websocket, {}).get("auth_attempts", 0),
                "ip": client_ip,
            }
            logger.info(
                "WebSocket client auto-authenticated after pairing: %s", client_id
            )
        return authenticated, client_id

    # ── Onboarding by a paired device (unauthenticated) ─────────────
    if msg_type == "onboard_start":
        if authenticated:
            await onboarding_ws.send_error(websocket, "already_authenticated")
        else:
            await onboarding_ws.handle_start(websocket, message)
        return authenticated, client_id

    # ── Auth (allowed for unauthenticated clients) ───────────────────────
    if msg_type == "auth":
        if authenticated:
            await websocket.send_json(
                {
                    "type": "auth_ok",
                    "message": "Already authenticated",
                    **protocol.protocol_fields(),
                }
            )
            return authenticated, client_id

        # Rate-limit auth attempts per connection (brute-force prevention)
        client_info = _clients.get(websocket, {})
        auth_attempts = client_info.get("auth_attempts", 0)
        if auth_attempts >= MAX_AUTH_ATTEMPTS:
            await _send_auth_error(websocket, AUTH_RATE_LIMITED)
            logger.warning(
                "WebSocket auth rate limit reached (%d attempts), rejecting",
                auth_attempts,
            )
            return authenticated, client_id
        if websocket in _clients:
            _clients[websocket]["auth_attempts"] = auth_attempts + 1

        secret = message.get("secret", "")
        if not isinstance(secret, str):
            # Hashing a number or an object raised out of the socket.
            log_auth_failure("ws_secret", reason=AUTH_INVALID_SECRET)
            await _send_auth_error(websocket, AUTH_INVALID_SECRET)
            return authenticated, client_id
        # A removed client (pending-deletion) reconnecting on the LAN gets the
        # client_removed reset handshake replayed: it wipes locally, then acks
        # "wiped" via client_reset_ack (accepted unauthenticated for exactly
        # this pending id), which purges the signed tombstone. The row is NOT
        # deleted here; a connection that never acks is dropped by the unauth
        # timeout (4002) and the tombstone persists.
        pending_id = await _match_pending_deletion(db, secret)
        if pending_id:
            log_auth_failure("ws_secret", reason="client_removed")
            info = _clients.setdefault(websocket, {})
            info["reset_signaled"] = True
            info["pending_removal_client_id"] = pending_id
            try:
                await websocket.send_json(
                    {
                        "type": "client_removed",
                        "client_id": pending_id,
                        "reason": "deleted",
                    }
                )
            except Exception:  # noqa: BLE001 - broken socket; loop cleans up
                pass
            return authenticated, client_id

        new_client_id, auth_reason = await _check_client_secret(db, secret)
        paired_platform = (
            await _paired_platform(db, new_client_id) if new_client_id else None
        )
        evaluation = (
            protocol.evaluate(message, paired_platform) if new_client_id else None
        )
        if (
            new_client_id
            and evaluation.info.platform is not None
            and not protocol.same_device_platform(
                paired_platform, evaluation.info.platform
            )
        ):
            # A valid platform other than the paired one; a malformed one
            # stays the invalid_client_info below. Nothing is stored.
            audit_log.log_client_info_mismatch(
                new_client_id,
                "platform",
                paired_platform,
                evaluation.info.platform,
                _ws_client_ip(websocket),
            )
            await _send_auth_error(
                websocket,
                AUTH_INVALID_CLIENT_INFO,
                "Client info does not match this device's pairing",
            )
            return authenticated, client_id
        if new_client_id and evaluation.upgrade_reason:
            # Known device, app too old: keep the pairing, store
            # what it reported so the Admin UI shows "update required".
            log_auth_failure("ws_secret", reason="upgrade_required")
            try:
                await _store_client_info(db, new_client_id, evaluation.info)
            except Exception as e:  # noqa: BLE001 - the answer matters more
                logger.warning("Client info of %s not stored: %s", new_client_id, e)
            await client_activity.record(
                db,
                new_client_id,
                client_activity.UPGRADE_REQUIRED,
                {
                    "reason": evaluation.upgrade_reason,
                    "app_version": evaluation.info.app_version,
                    "protocol_version": evaluation.info.protocol_version,
                    "platform": evaluation.info.platform,
                },
            )
            logger.info(
                "Client %s refused, app too old (%s)",
                new_client_id,
                evaluation.upgrade_reason,
            )
            await _reject_outdated_app(websocket, evaluation)
            return authenticated, client_id
        if new_client_id and evaluation.error:
            log_auth_failure("ws_secret", reason="invalid_client_info")
            await _send_auth_error(
                websocket, AUTH_INVALID_CLIENT_INFO, evaluation.error
            )
            return authenticated, client_id
        if new_client_id:
            info = evaluation.info
            previous_version = await _store_client_info(db, new_client_id, info)
            authenticated = True
            client_id = new_client_id
            # Fetch guest info for auth_ok response
            client_row = await db.fetchone(
                "SELECT (access = 'guest') AS is_guest, expires_at FROM paired_clients "
                "WHERE id = ?",
                (new_client_id,),
            )
            _clients[websocket] = {
                "authenticated": True,
                "client_id": client_id,
                "is_guest": bool(client_row.get("is_guest")) if client_row else False,
                "pair_attempts": _clients.get(websocket, {}).get("pair_attempts", 0),
                "auth_attempts": _clients.get(websocket, {}).get("auth_attempts", 0),
                "ip": _ws_client_ip(websocket),
            }
            auth_response = {
                "type": "auth_ok",
                "client_id": client_id,
                **protocol.protocol_fields(),
            }
            if client_row and client_row.get("is_guest"):
                auth_response["is_guest"] = True
                auth_response["expires_at"] = client_row.get("expires_at")
            await websocket.send_json(auth_response)
            logger.info("WebSocket client authenticated: %s", client_id)
            log_auth_success("ws_secret", client_id=client_id)
            if previous_version and previous_version != info.app_version:
                await client_activity.record(
                    db,
                    client_id,
                    client_activity.APP_UPDATED,
                    {
                        "previous_app_version": previous_version,
                        "app_version": info.app_version,
                    },
                )
            await client_activity.record(
                db,
                client_id,
                client_activity.CONNECTED,
                {
                    "app_version": info.app_version,
                    "app_build": info.app_build,
                    "protocol_version": info.protocol_version,
                },
            )
            # Tell admin UIs (and the integration push session) the client is
            # online. This is THE client_connected trigger for paired clients:
            # they always authenticate via this message, never at connection
            # open (the open-path broadcast only ever saw ingress/dev, which
            # are excluded — it fired for no one).
            await api_ws.broadcast(
                {
                    "type": "client_connected",
                    "data": {"client_id": client_id},
                },
                audience=EventAudience.ADMIN,
            )
            # A rotation that waited for this device to come home:
            # it is connected now and fetches the new token at once.
            device_provisioning.spawn(
                device_provisioning.release_deferred_rotation(db, client_id)
            )
            # The same for the watches this phone carries.
            device_provisioning.spawn(watch_access.phone_connected(db, client_id))
            # A babysitter at home gets the children's requests.
            access.wake()
        else:
            log_auth_failure("ws_secret", reason=auth_reason or AUTH_INVALID_SECRET)
            await _send_auth_error(websocket, auth_reason or AUTH_INVALID_SECRET)
        return authenticated, client_id

    # ── client_reset_ack from an unauthenticated pending-removal socket ──
    # The LAN wipe-ack of a returning deleted device: its auth attempt was
    # answered with client_removed (see the auth handler) instead of
    # authenticating, and this ack is accepted for exactly that pending id.
    if msg_type == "client_reset_ack" and not authenticated:
        pending_id = _clients.get(websocket, {}).get("pending_removal_client_id")
        ack_client_id = message.get("client_id", "")
        if pending_id and ack_client_id == pending_id:
            try:
                await _purge_tombstone_on_ack(websocket, db, pending_id)
            except Exception as e:  # noqa: BLE001 - never raise out of handler
                logger.error("client_reset_ack failed: %s", e)
        else:
            await websocket.send_json(
                {"type": "error", "message": "client_reset_ack client_id mismatch"}
            )
        return authenticated, client_id

    # ── Reject all other messages if not authenticated ───────────────────
    if not authenticated:
        await websocket.send_json(
            {
                "type": "auth_required",
                "message": "Authenticate first (send auth or pair message)",
            }
        )
        return authenticated, client_id

    # ── Removed client in its reset grace period ─────────────────────────
    # After client_removed only the wipe-ack (and heartbeats) are served; no
    # reads for a deleted client.
    if _conn_info(websocket).get("reset_signaled") and msg_type not in (
        "client_reset_ack",
        "ping",
        "pong",
    ):
        await websocket.send_json({"type": "error", "message": "Client removed"})
        return authenticated, client_id

    # ── Authenticated message handlers (api/ws/client.py) ────────────────
    await _handle_client_message(websocket, db, message, msg_type, client_id)
    return authenticated, client_id


# ── WebSocket endpoint ───────────────────────────────────────────────────────


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time communication.

    This is the primary channel for external clients (wall panels, mobile apps)
    and carries the companion integration's read-only push session.
    Supports pairing, data retrieval, notifications, and live broadcasts.

    Security:
    - Max 100 concurrent connections enforced
    - Ingress: auto-authenticated (with IP validation)
    - Integration: "Authorization: Bearer <integration token>" verified
      BEFORE accept (the AuthMiddleware never sees websocket scopes, so the
      check MUST live here). Valid => read-only session as
      client_id="integration"; present-but-invalid => close 4003 without
      accept; absent => unchanged behavior below.
    - External: must send {"type": "auth", "secret": "..."} to authenticate
    - Max 5 pairing attempts per connection
    - No api_token configured: auth disabled (dev mode)
    """
    # Enforce connection limits BEFORE accepting (separate counters)
    auth_count = sum(1 for info in _clients.values() if info.get("authenticated"))
    unauth_count = len(_clients) - auth_count
    client_ip = _ws_client_ip(websocket)
    unauth_from_ip = sum(
        1
        for info in _clients.values()
        if not info.get("authenticated") and info.get("ip") == client_ip
    )

    db_pre: Database = websocket.app.state.db

    # ── Integration handshake auth (header Bearer, BEFORE accept) ────────
    # The integration's aiohttp session sends its dedicated token as a
    # default header on ws_connect. A present-but-invalid Bearer header is
    # rejected outright (stale/rotated token — counted for the System tab),
    # never downgraded to an anonymous session.
    is_integration = False
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if await integration_token_valid(db_pre, token):
            if not integration_source.source_allowed(client_ip):
                log_auth_failure("ws_integration", "untrusted_source", client_ip)
                logger.warning(
                    "Integration token from a foreign address refused (WS) — "
                    "closing 4003 without accept"
                )
                await websocket.close(code=4003, reason="Invalid integration token")
                return
            is_integration = True
        else:
            record_auth_failure()
            log_auth_failure("ws_integration", reason="invalid_or_stale_token")
            logger.warning(
                "Rejected WS handshake with invalid Bearer header (integration "
                "token stale/rotated?) — closing 4003 without accept"
            )
            await websocket.close(code=4003, reason="Invalid integration token")
            return

    # Admin web UI outside Ingress: session cookie (TLS, same Origin).
    session_token = None
    if not is_integration and not api_ws._is_ingress(websocket):
        session_token = _admin_session_token(websocket)

    # Pre-determine if this connection will start as authenticated
    will_auth = (
        is_integration
        or session_token is not None
        or api_ws._is_ingress(websocket)
        or await api_ws._auth_disabled(db_pre)
    )

    if will_auth:
        if auth_count >= MAX_WS_CONNECTIONS_AUTH:
            logger.warning(
                "WebSocket auth connection limit reached (%d/%d), rejecting",
                auth_count,
                MAX_WS_CONNECTIONS_AUTH,
            )
            await websocket.close(code=4029, reason="Too many connections")
            return
    else:
        if unauth_from_ip >= MAX_WS_UNAUTH_PER_IP:
            logger.warning(
                "WebSocket unauth connection limit per IP reached for %s (%d/%d), "
                "rejecting",
                client_ip,
                unauth_from_ip,
                MAX_WS_UNAUTH_PER_IP,
            )
            await websocket.close(code=4029, reason="Too many connections")
            return
        if unauth_count >= MAX_WS_CONNECTIONS_UNAUTH:
            logger.warning(
                "WebSocket unauth connection limit reached (%d/%d), rejecting",
                unauth_count,
                MAX_WS_CONNECTIONS_UNAUTH,
            )
            await websocket.close(code=4029, reason="Too many connections")
            return

    await websocket.accept()

    db: Database = websocket.app.state.db
    authenticated = False
    client_id = None

    # Check if auth is needed (integration header, Ingress or dev auto-auth)
    if is_integration:
        authenticated = True
        client_id = INTEGRATION_CLIENT_ID
    elif session_token is not None:
        authenticated = True
        client_id = ADMIN_SESSION_CLIENT_ID
        session = _validate_session(session_token) or {}
        log_admin_access("WS", "/api/ws", "session", client_ip, session.get("username"))
    elif api_ws._is_ingress(websocket) or await api_ws._auth_disabled(db):
        authenticated = True
        client_id = "ingress" if api_ws._is_ingress(websocket) else "dev"
        # Admin sessions are audited like admin HTTP requests.
        log_admin_access(
            "WS", "/api/ws", client_id, client_ip, ingress_user(websocket.headers)
        )

    _clients[websocket] = {
        "authenticated": authenticated,
        "client_id": client_id,
        "pair_attempts": 0,
        "auth_attempts": 0,
        "connected_at": time.time(),
        "ip": client_ip,
    }
    if session_token is not None:
        _clients[websocket]["session_token"] = session_token
    if is_integration:
        # Version header captured at handshake; re-recorded on every pong so
        # the telemetry window (90s) stays warm from the 30s ping cadence.
        _clients[websocket]["integration_version"] = websocket.headers.get(
            INTEGRATION_VERSION_HEADER
        )
        record_request(websocket.headers.get(INTEGRATION_VERSION_HEADER))
        log_auth_success("ws_integration")
    logger.info(
        "WebSocket client connected (auth=%s, client=%s, total: %d)",
        authenticated,
        client_id,
        len(_clients),
    )

    # If authenticated, send auth_ok immediately. The integration session's
    # auth_ok doubles as its implicit subscribe-to-everything.
    if authenticated:
        auth_msg = {
            "type": "auth_ok",
            "client_id": client_id,
            **protocol.protocol_fields(),
        }
        # Include guest info for paired clients
        if client_id and client_id not in PSEUDO_CLIENT_IDS:
            client_row = await db.fetchone(
                "SELECT (access = 'guest') AS is_guest, expires_at FROM paired_clients "
                "WHERE id = ?",
                (client_id,),
            )
            if client_row and client_row.get("is_guest"):
                auth_msg["is_guest"] = True
                auth_msg["expires_at"] = client_row.get("expires_at")
        await websocket.send_json(auth_msg)
        if is_integration:
            # Rule snapshots of every device right after auth_ok.
            tasks.spawn(
                enforcement.integration_connected(), name="enforcement-complete"
            )
        # NOTE: no client_connected broadcast here — paired clients are never
        # authenticated at connection open (they auth via the auth message,
        # which is where the broadcast fires), and ingress/dev/integration
        # sessions are not clients.
    else:
        # Not authenticated - tell client what to do (timeout enforced)
        await websocket.send_json(
            {
                "type": "auth_required",
                "message": "Send auth with secret or pair with PIN",
                "timeout": UNAUTH_TIMEOUT_SECONDS,
            }
        )

    try:
        while True:
            # Our side closed the socket (wipe-ack purge, revoke, eviction):
            # stop reading instead of calling receive on a closed socket
            # and never answer a revoked session.
            if _is_closed(websocket) or websocket not in _clients:
                break
            # A confirmed onboarding authenticated this connection from the
            # confirming device's handler: adopt it.
            if not authenticated:
                entry = _clients.get(websocket) or {}
                if entry.get("authenticated"):
                    authenticated = True
                    client_id = entry.get("client_id")
                    continue
            # Enforce auth timeout for unauthenticated connections
            if not authenticated:
                client_info = _clients.get(websocket, {})
                connected_at = client_info.get("connected_at", time.time())
                # A running onboarding session keeps the connection open until
                # it expires.
                remaining = (
                    onboarding_ws.deadline(
                        websocket, connected_at + UNAUTH_TIMEOUT_SECONDS
                    )
                    - time.time()
                )
                if remaining <= 0:
                    await onboarding_ws.expire(websocket)
                    logger.warning(
                        "WebSocket auth timeout (%ds) - disconnecting unauthenticated client",
                        UNAUTH_TIMEOUT_SECONDS,
                    )
                    await websocket.send_json(
                        {
                            "type": "auth_timeout",
                            "message": "Authentication timeout. Reconnect to try again.",
                        }
                    )
                    await websocket.close(code=4002, reason="Auth timeout")
                    return
                try:
                    data = await asyncio.wait_for(
                        _receive_text(websocket), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    entry = _clients.get(websocket) or {}
                    if entry.get("authenticated") or (
                        onboarding_ws.deadline(websocket, 0) > time.time()
                    ):
                        continue  # onboarded meanwhile, or the session runs on
                    await onboarding_ws.expire(websocket)
                    logger.warning(
                        "WebSocket auth timeout (%ds) - disconnecting unauthenticated client",
                        UNAUTH_TIMEOUT_SECONDS,
                    )
                    await websocket.send_json(
                        {
                            "type": "auth_timeout",
                            "message": "Authentication timeout. Reconnect to try again.",
                        }
                    )
                    await websocket.close(code=4002, reason="Auth timeout")
                    return
            elif session_token is not None:
                # Admin web-UI session: end the socket as soon as the session
                # ends (expiry, eviction, logout), even while idle.
                session = _validate_session(session_token)
                if not session:
                    logger.info("Admin WS session ended, closing 4401")
                    await _end_admin_session(websocket)
                    break
                wait = min(
                    SESSION_RECHECK_SECONDS,
                    max(0.01, session["expires_at"] - time.time()),
                )
                try:
                    data = await asyncio.wait_for(
                        _receive_text(websocket), timeout=wait
                    )
                except asyncio.TimeoutError:
                    continue
            else:
                data = await _receive_text(websocket)

            if _is_closed(websocket) or websocket not in _clients:
                break
            if not authenticated and (_clients.get(websocket) or {}).get(
                "authenticated"
            ):
                authenticated = True
                client_id = _clients[websocket].get("client_id")

            if data is _BINARY:
                if await _invalid_frame(
                    websocket, authenticated, "Binary frames are not supported"
                ):
                    break
                continue

            # Enforce message size limit (64 KB)
            if len(data) > MAX_WS_MESSAGE_SIZE:
                if await _invalid_frame(
                    websocket,
                    authenticated,
                    f"Message too large (max {MAX_WS_MESSAGE_SIZE // 1024}KB)",
                ):
                    break
                continue

            try:
                message = json.loads(data)
            except (json.JSONDecodeError, RecursionError):
                # RecursionError: nesting deeper than the parser's limit.
                if await _invalid_frame(websocket, authenticated, "Invalid JSON"):
                    break
                continue
            # Every handler reads fields of an object with a string type; a
            # list, a string or {"type": 5} is an invalid frame.
            if not isinstance(message, dict) or not isinstance(
                message.get("type", ""), str
            ):
                if await _invalid_frame(
                    websocket,
                    authenticated,
                    "Message must be a JSON object with a string type",
                ):
                    break
                continue
            authenticated, client_id = await _handle_message(
                websocket, message, authenticated, client_id
            )

    except WebSocketDisconnect:
        pass
    finally:
        # Capture client_id before removing from _clients.
        entry = _clients.get(websocket) or {}
        disconnected_client_id = client_id or (
            entry.get("client_id") if entry.get("authenticated") else None
        )
        await onboarding_ws.on_disconnect(websocket)
        state_relay.unsubscribe(websocket)
        _clients.pop(websocket, None)
        discard_sender(websocket)
        logger.info("WebSocket client disconnected (total: %d)", len(_clients))

        # Broadcast disconnect event for paired clients (admin-only)
        if disconnected_client_id and disconnected_client_id not in PSEUDO_CLIENT_IDS:
            # Check if this client has no more connections (might have multiple)
            still_connected = disconnected_client_id in get_connected_client_ids()
            if not still_connected:
                # NO tombstone purge here: a signaled connection that tears
                # down WITHOUT an explicit client_reset_ack has NOT confirmed
                # its local wipe, so the signed tombstone must persist (the
                # compliance endpoint keeps returning signed "deleted") until
                # the device's wipe-ack arrives or an admin force-purges.
                try:
                    await api_ws.broadcast(
                        {
                            "type": "client_disconnected",
                            "data": {"client_id": disconnected_client_id},
                        },
                        audience=EventAudience.ADMIN,
                    )
                except Exception:
                    pass  # Best-effort, server might be shutting down
                await client_activity.record(
                    db, disconnected_client_id, client_activity.DISCONNECTED
                )
                access.wake()
