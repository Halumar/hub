"""WebSocket side of setting up a device from a paired device.

* ``onboard_start`` (unauthenticated, TLS only): opens a session for the
  waiting device; its connection stays open until the session ends.
* ``onboard_lookup`` / ``onboard_confirm`` / ``onboard_decline`` (paired
  clients): read (QR code or code), confirm or decline a session.
* ``get_setup_grant``: what the device may give (type first, invitation).
* ``get_set_up_devices`` / ``set_up_device_remove``: “Set up by me”.

State and rules: ``services/onboarding.py``. On confirm the device is created
like a PIN pairing (``services/client_pairing.py``) and the waiting
connection gets ``onboard_ok`` with the secret sealed to its key; from then on
it is authenticated as the new device. A watch is the exception:
its phone holds that connection, so it is closed after ``onboard_ok`` without
being authenticated, and the watch's HA access goes to the phone only sealed
to the watch's key (``watch_access``, services/watch_access.py).
"""

from __future__ import annotations

import contextlib
import logging
import uuid

from fastapi import WebSocket

from api import ws as api_ws
from api.auth import _is_secure_transport
from api.ws.broadcast import EventAudience
from api.ws.registry import _clients
from models.database import Database
from services import (
    client_activity,
    client_pairing,
    mdns,
    onboard_crypto,
    onboarding,
    protocol,
)
from services.audit_log import log_admin_action, log_pairing_failure

logger = logging.getLogger("hub.api.websocket")

MAX_START_ATTEMPTS = 5


async def send_error(
    websocket: WebSocket, reason: str, session_id: str | None = None
) -> None:
    message = {
        "type": "onboard_error",
        "reason": reason,
        "message": onboarding.MESSAGES.get(reason, reason),
    }
    if session_id:
        message["session_id"] = session_id
    with contextlib.suppress(Exception):
        await websocket.send_json(message)


def _ip(websocket: WebSocket) -> str:
    client = getattr(websocket, "client", None)
    return getattr(client, "host", None) or "unknown"


def own_hub_id() -> str:
    return mdns.load_instance_id()


async def end_session(
    session: onboarding.Session,
    result: str,
    *,
    tv_reason: str | None = None,
    extra: dict | None = None,
) -> bool:
    """Remove a session, tell the waiting device (``tv_reason``) and admins."""
    if not onboarding.remove(session):
        return False
    if tv_reason:
        await send_error(session.websocket, tv_reason, session.session_id)
    await api_ws.broadcast(
        {
            "type": "onboarding_ended",
            "data": {"id": session.id, "result": result, **(extra or {})},
        },
        audience=EventAudience.ADMIN,
    )
    logger.info(
        "Onboarding session %s of '%s' ended: %s",
        session.id,
        session.client_name,
        result,
    )
    return True


async def on_disconnect(websocket: WebSocket) -> None:
    """The waiting device closed its connection: its session ends."""
    session = onboarding.session_of(websocket)
    if session:
        await end_session(session, onboarding.RESULT_DISCONNECTED)


async def expire(websocket: WebSocket) -> bool:
    """End the expired session of ``websocket``; True if there was one."""
    session = onboarding.session_of(websocket)
    if not session:
        return False
    await end_session(
        session, onboarding.RESULT_EXPIRED, tv_reason=onboarding.SESSION_EXPIRED
    )
    return True


def deadline(websocket: WebSocket, default: float) -> float:
    """Auth deadline of an unauthenticated connection (session expiry wins)."""
    session = onboarding.session_of(websocket)
    return max(default, session.expires_at) if session else default


# ── Waiting device ──────────────────────────────────────────────────────────


async def handle_start(websocket: WebSocket, message: dict) -> None:
    """``onboard_start`` of an unauthenticated connection."""
    ip = _ip(websocket)
    info_entry = _clients.get(websocket)
    if info_entry is None:
        await send_error(websocket, onboarding.CONNECTION_EXPIRED)
        return
    attempts = info_entry.get("onboard_attempts", 0) + 1
    info_entry["onboard_attempts"] = attempts
    if attempts > MAX_START_ATTEMPTS:
        log_pairing_failure("onboard_rate_limited", ip)
        await send_error(websocket, onboarding.RATE_LIMITED)
        return
    if not _is_secure_transport(websocket):
        log_pairing_failure("onboard_tls_required", ip)
        await send_error(websocket, onboarding.TLS_REQUIRED)
        return

    client_name = message.get("client_name")
    key_text = message.get("public_key")
    if (
        not isinstance(client_name, str)
        or not client_name.strip()
        or len(client_name) > 100
        or not isinstance(key_text, str)
    ):
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return
    public_key = onboard_crypto.parse_public_key(key_text)
    if public_key is None:
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return

    # Client info like pair: a too old app gets upgrade_required.
    evaluation = protocol.evaluate(message)
    if evaluation.upgrade_reason:
        from api.ws.handshake import _reject_outdated_app

        await _reject_outdated_app(websocket, evaluation)
        return
    if evaluation.error:
        await send_error(websocket, onboarding.INVALID_CLIENT_INFO)
        return
    if evaluation.info.platform not in onboarding.ONBOARD_PLATFORMS:
        # The web pairs with the PIN.
        await send_error(websocket, onboarding.PLATFORM_NOT_SUPPORTED)
        return

    if not onboarding.start_allowed(ip, websocket):
        log_pairing_failure("onboard_rate_limited", ip)
        await send_error(websocket, onboarding.RATE_LIMITED)
        return

    previous = onboarding.session_of(websocket)
    if previous:
        await end_session(previous, onboarding.RESULT_REPLACED)

    session = onboarding.create(
        websocket=websocket,
        ip=ip,
        public_key=public_key,
        public_key_b64=key_text,
        client_name=client_name.strip(),
        info=evaluation.info,
    )
    log_admin_action("onboarding_started", session.id, ip)
    await websocket.send_json(
        {
            "type": "onboard_session",
            "session_id": session.session_id,
            "hub_id": own_hub_id(),
            "code": session.code,
            "expires_in": onboarding.SESSION_SECONDS,
            "expires_at": onboarding.iso(session.expires_at),
        }
    )
    started = {
        "id": session.id,
        "client_name": session.client_name,
        "platform": session.info.platform,
        "expires_at": onboarding.iso(session.expires_at),
    }
    if session.info.device_model:
        # Optional in the contract, never null.
        started["device_model"] = session.info.device_model
    await api_ws.broadcast(
        {"type": "onboarding_started", "data": started},
        audience=EventAudience.ADMIN,
    )


# ── Confirming device ───────────────────────────────────────────────────────


async def _session_by_code(
    websocket: WebSocket, client_id: str, code
) -> onboarding.Session | None:
    """Session of a typed code; counts wrong codes (too_many_attempts)."""
    if not isinstance(code, str) or len(code) != onboarding.CODE_DIGITS:
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return None
    if not onboarding.code_attempt_allowed(client_id):
        log_pairing_failure("onboard_code_rate_limited", _ip(websocket))
        await send_error(websocket, onboarding.TOO_MANY_ATTEMPTS)
        return None
    session = onboarding.find_by_code(code)
    if session is None:
        onboarding.record_code_failure(client_id)
        log_pairing_failure("onboard_code_not_found", _ip(websocket))
        await send_error(websocket, onboarding.SESSION_NOT_FOUND)
        return None
    # Used up: a code finds its session once; the confirming device goes on
    # with session_id, public_key and hub_id from onboard_info.
    session.code = None
    return session


async def _checked_session(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> onboarding.Session | None:
    """Common checks of lookup/confirm; sends the error and returns None."""
    by_code = "code" in message
    session_id = message.get("session_id")
    if by_code:
        if any(k in message for k in ("session_id", "public_key", "hub_id")):
            await send_error(websocket, onboarding.INVALID_REQUEST)
            return None
    elif (
        not isinstance(session_id, str)
        or not isinstance(message.get("public_key"), str)
        or not isinstance(message.get("hub_id"), str)
    ):
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return None
    if not await onboarding.may_set_up(db, client_id):
        log_pairing_failure("onboard_not_permitted", _ip(websocket))
        await send_error(websocket, onboarding.NOT_PERMITTED)
        return None
    if not onboarding.failures_allowed(client_id):
        await send_error(websocket, onboarding.RATE_LIMITED)
        return None

    if by_code:
        session = await _session_by_code(websocket, client_id, message.get("code"))
        if session is None:
            return None
        session_id = session.session_id
    else:
        session = onboarding.find(session_id)
        if session is None:
            onboarding.record_failure(client_id)
            log_pairing_failure("onboard_session_not_found", _ip(websocket))
            await send_error(websocket, onboarding.SESSION_NOT_FOUND)
            return None
    if session.expired():
        await end_session(
            session, onboarding.RESULT_EXPIRED, tv_reason=onboarding.SESSION_EXPIRED
        )
        await send_error(websocket, onboarding.SESSION_EXPIRED, session_id)
        return None
    if not by_code:
        mismatch = onboarding.matches(
            session, message["public_key"], message["hub_id"], own_hub_id()
        )
        if mismatch == onboarding.HUB_MISMATCH:
            onboarding.record_failure(client_id)
            log_pairing_failure("onboard_hub_mismatch", _ip(websocket))
            await send_error(websocket, onboarding.HUB_MISMATCH)
            return None
        if mismatch:
            # The QR code does not show the key this session was opened with:
            # someone relays or altered it. Fail closed for both sides.
            onboarding.record_failure(client_id)
            logger.warning(
                "Onboarding session %s invalidated: public key mismatch "
                "(confirming device %s)",
                session.id,
                client_id,
            )
            log_admin_action("onboarding_invalidated", session.id, _ip(websocket))
            await end_session(
                session,
                onboarding.RESULT_INVALIDATED,
                tv_reason=onboarding.SESSION_INVALIDATED,
            )
            await send_error(websocket, onboarding.SESSION_INVALIDATED, session_id)
            return None
    return session


async def _device_name(db: Database, client_id: str) -> str:
    row = await db.fetchone(
        "SELECT name FROM paired_clients WHERE id = ?", (client_id,)
    )
    return (row or {}).get("name") or ""


async def _child_watch_lookup(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> bool:
    """A child's phone binds its watch's session (docs/setup.md "A child's
    watch"): only with the QR values of a watch session, only one phone per
    session. True when handled (answered), False for any other device."""
    if await onboarding.may_set_up(db, client_id):
        return False
    found = await onboarding.child_watch_phone(db, client_id)
    if found is None:
        return False
    if "code" in message or not all(
        isinstance(message.get(k), str) for k in ("session_id", "public_key", "hub_id")
    ):
        # The child's phone opened the session itself: it has its id and key.
        await send_error(websocket, onboarding.NOT_PERMITTED)
        return True
    if not onboarding.failures_allowed(client_id):
        await send_error(websocket, onboarding.RATE_LIMITED)
        return True
    session = onboarding.find(message["session_id"])
    if session is None:
        onboarding.record_failure(client_id)
        await send_error(websocket, onboarding.SESSION_NOT_FOUND)
        return True
    if session.expired():
        await end_session(
            session, onboarding.RESULT_EXPIRED, tv_reason=onboarding.SESSION_EXPIRED
        )
        await send_error(websocket, onboarding.SESSION_EXPIRED, session.session_id)
        return True
    mismatch = onboarding.matches(
        session, message["public_key"], message["hub_id"], own_hub_id()
    )
    if mismatch:
        onboarding.record_failure(client_id)
        if mismatch == onboarding.SESSION_INVALIDATED:
            await end_session(
                session,
                onboarding.RESULT_INVALIDATED,
                tv_reason=onboarding.SESSION_INVALIDATED,
            )
        await send_error(websocket, mismatch, session.session_id)
        return True
    bound = session.child_phone
    if session.info.platform not in onboarding.WATCH_PLATFORMS or (
        bound is not None and bound["client_id"] != client_id
    ):
        onboarding.record_failure(client_id)
        await send_error(websocket, onboarding.NOT_PERMITTED, session.session_id)
        return True
    name = found[0].get("name") or ""
    if bound is None:
        session.child_phone = {"client_id": client_id, "name": name}
        log_admin_action("onboarding_child_watch_bound", session.id, _ip(websocket))
    payload = {
        "type": "onboard_info",
        "session_id": session.session_id,
        "public_key": session.public_key_b64,
        "hub_id": own_hub_id(),
        "client_name": session.client_name,
        "platform": session.info.platform,
        "app_version": session.info.app_version,
        "expires_at": onboarding.iso(session.expires_at),
        "grant": onboarding.child_watch_grant(name),
    }
    if session.info.device_model:
        payload["device_model"] = session.info.device_model
    await websocket.send_json(payload)
    return True


async def handle_lookup(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    if await _child_watch_lookup(websocket, db, message, client_id):
        return
    session = await _checked_session(websocket, db, message, client_id)
    if session is None:
        return
    try:
        grant = await onboarding.grant_for(
            db, client_id, session.info.platform, session=session
        )
    except onboarding.SetupError as e:
        await send_error(websocket, e.reason, session.session_id)
        return
    if not grant["kinds"]:
        # No allowed kind fits this device (e.g. an Apple TV without tv); a
        # watch without the rule watch (not_permitted), or whose default
        # profile gives more than the phone has (profile_not_allowed: the
        # watch never gets more than its phone).
        log_pairing_failure("onboard_kind_not_allowed", _ip(websocket))
        reason = onboarding.KIND_NOT_ALLOWED
        if grant["own_watch"]:
            found = await onboarding.confirmer(db, client_id)
            watch_on = (
                bool(found) and (found[2].get("watch") or {}).get("value") is True
            )
            reason = (
                onboarding.PROFILE_NOT_ALLOWED if watch_on else onboarding.NOT_PERMITTED
            )
        await send_error(websocket, reason, session.session_id)
        return
    payload = {
        "type": "onboard_info",
        "session_id": session.session_id,
        "public_key": session.public_key_b64,
        "hub_id": own_hub_id(),
        "client_name": session.client_name,
        "platform": session.info.platform,
        "app_version": session.info.app_version,
        "expires_at": onboarding.iso(session.expires_at),
        "grant": grant,
    }
    if session.info.device_model:
        payload["device_model"] = session.info.device_model
    await websocket.send_json(payload)
    if client_id not in session.claimed_ids:
        session.claimed_ids.add(client_id)
        name = await _device_name(db, client_id)
        session.claimed_by = {"client_id": client_id, "name": name}
        with contextlib.suppress(Exception):
            await session.websocket.send_json(
                {
                    "type": "onboard_claimed",
                    "session_id": session.session_id,
                    "by_name": name,
                }
            )
        await api_ws.broadcast(
            {
                "type": "onboarding_claimed",
                "data": {"id": session.id, "claimed_by": session.claimed_by},
            },
            audience=EventAudience.ADMIN,
        )


async def handle_decline(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    session_id = message.get("session_id")
    session = onboarding.find(session_id) if isinstance(session_id, str) else None
    if session is None:
        await send_error(websocket, onboarding.SESSION_NOT_FOUND)
        return
    if not await onboarding.may_set_up(db, client_id):
        await send_error(websocket, onboarding.NOT_PERMITTED)
        return
    log_admin_action("onboarding_declined", session.id, _ip(websocket))
    await end_session(
        session,
        onboarding.RESULT_DECLINED,
        tv_reason=onboarding.DECLINED,
        extra={"confirmed_by": client_id},
    )


async def handle_confirm(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    if "code" in message:
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return
    session = await _checked_session(websocket, db, message, client_id)
    if session is None:
        return
    try:
        plan = await onboarding.plan_confirm(db, client_id, session, message)
    except onboarding.SetupError as e:
        # Never more than the confirming device; the session stays open.
        onboarding.record_failure(client_id)
        log_pairing_failure(f"onboard_{e.reason}", _ip(websocket))
        await send_error(websocket, e.reason, session.session_id)
        return
    # Single use: take the session out before the first await below.
    if not onboarding.remove(session):
        await send_error(websocket, onboarding.SESSION_NOT_FOUND)
        return
    tv = session.websocket
    tv_entry = _clients.get(tv)
    if tv_entry is None or tv_entry.get("authenticated"):
        await send_error(websocket, onboarding.SESSION_NOT_FOUND)
        return

    by_name = await _device_name(db, client_id)
    # A child's watch belongs to the child's phone (it carries the watch's
    # access); the adult who confirmed it is confirmed_by.
    owner_id = plan.child_phone["client_id"] if plan.child_phone else client_id
    owner_name = await _device_name(db, owner_id) if plan.child_phone else by_name
    from services.profiles.catalog import ProfileError

    watch = session.info.platform in onboarding.WATCH_PLATFORMS
    # Seal first: a key that cannot be sealed to leaves no device behind.
    new_id, secret = str(uuid.uuid4()), client_pairing.generate_secret()
    try:
        sealed = onboard_crypto.seal_credentials(
            session.public_key, session.session_id, new_id, secret
        )
    except ValueError as e:
        logger.warning("Onboarding refused: key cannot be sealed to (%s)", e)
        await send_error(tv, onboarding.CANCELLED, session.session_id)
        await send_error(websocket, onboarding.INVALID_REQUEST, session.session_id)
        return
    try:
        new_id, secret = await client_pairing.insert_client(
            db,
            client_name=plan.client_name,
            info=session.info,
            is_guest=plan.guest,
            guest_expires_at=plan.expires_at,
            profile_id=plan.stored_profile_id or plan.profile["id"],
            own_area_id=plan.own_area_id,
            owner_label=plan.owner_label,
            overrides=plan.overrides,
            set_up_by_client_id=owner_id,
            set_up_by_name=owner_name,
            set_up_method="onboard",
            seal_key=session.public_key_b64 if watch else None,
            client_id=new_id,
            secret=secret,
        )
    except ProfileError as e:
        logger.warning("Onboarding refused: %s", e.code)
        await send_error(tv, onboarding.CANCELLED, session.session_id)
        await send_error(websocket, onboarding.PROFILE_NOT_ALLOWED, session.session_id)
        return
    if plan.managers:
        await onboarding.add_to_managers(db, new_id, plan.managers, by_name)
    # The waiting connection is the new device from now on (like pair_ok).
    # Not for a watch: its phone holds the connection and must never read the
    # watch's client_config; it is closed after onboard_ok.
    if not watch:
        tv_entry.update({"authenticated": True, "client_id": new_id})
    try:
        await tv.send_json(
            {
                "type": "onboard_ok",
                "client_id": new_id,
                "client_name": plan.client_name,
                "sealed": sealed,
                **protocol.protocol_fields(),
            }
        )
        if watch:
            await tv.close(code=1000, reason="Watch onboarded")
    except Exception as e:  # noqa: BLE001 - the device may be gone; it gets nothing
        logger.warning("onboard_ok not delivered to %s: %s", new_id, e)
    logger.info(
        "Set up new client '%s' (id=%s) as %s with profile %s, confirmed by %s",
        plan.client_name,
        new_id,
        plan.kind,
        plan.profile["id"],
        client_id,
    )
    areas_note = (
        "all"
        if plan.area.all_areas
        else f"{plan.area.mode}:{len(plan.area.area_names)}"
    )
    log_admin_action(
        "onboarding_confirmed",
        f"{new_id} by {client_id} kind={plan.kind} profile={plan.profile['id']} "
        f"areas={areas_note}",
        _ip(websocket),
    )
    await websocket.send_json(
        {
            "type": "onboard_confirm_ok",
            "session_id": session.session_id,
            "client_id": new_id,
            "client_name": plan.client_name,
            "all_areas": plan.area.all_areas,
            "area_names": plan.area.area_names,
            "kind": plan.kind,
            "profile": {"id": plan.profile["id"], "name": plan.profile["name"]},
            "owner_label": plan.owner_label,
            "expires_at": plan.expires_at,
            "remote_allowed": plan.remote_allowed,
            **({"access_hours": plan.access_hours} if plan.access_hours else {}),
        }
    )
    await client_pairing.announce_paired(
        db,
        getattr(websocket.app.state, "ha_client", None),
        client_id=new_id,
        client_name=plan.client_name,
        info=session.info,
        is_guest=plan.guest,
        method="onboard",
        confirmed_by=client_id,
        set_up={
            "kind": plan.kind,
            "method": "onboard",
            "by_client_id": client_id,
            "by_name": by_name,
        },
    )
    await client_activity.record(
        db,
        client_id,
        client_activity.DEVICE_ADDED,
        {
            "added_client_id": new_id,
            "added_client_name": plan.client_name,
            "kind": plan.kind,
            "profile_name": plan.profile["name"],
        },
    )
    await api_ws.broadcast(
        {
            "type": "onboarding_ended",
            "data": {
                "id": session.id,
                "result": onboarding.RESULT_CONFIRMED,
                "client_id": new_id,
                "confirmed_by": client_id,
            },
        },
        audience=EventAudience.ADMIN,
    )


# ── Grant before scanning, “Set up by me” ───────────────────────────────────


async def handle_get_grant(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    from services.protocol import PLATFORMS

    platform = message.get("platform", "ios")
    purpose = message.get("purpose", "onboard")
    if (
        platform not in PLATFORMS
        or platform == "web"
        or purpose
        not in (
            "onboard",
            "invite",
        )
    ):
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return
    try:
        grant = await onboarding.grant_for(db, client_id, platform, purpose)
    except onboarding.SetupError as e:
        await send_error(websocket, e.reason)
        return
    await websocket.send_json(
        {
            "type": "setup_grant",
            "platform": platform,
            "purpose": purpose,
            "grant": grant,
        }
    )


async def set_up_devices_of(db: Database, client_id: str) -> list[dict]:
    rows = await db.fetchall(
        "SELECT c.id, c.name, c.owner_label, c.platform, c.access, c.expires_at, "
        "c.created_at, c.set_up_method, p.id AS profile_id, p.name AS profile_name, "
        "p.kind AS profile_kind FROM paired_clients c JOIN profiles p "
        "ON p.id = c.profile_id WHERE c.set_up_by_client_id = ? AND c.is_active = 1 "
        "AND c.pending_deletion_at IS NULL ORDER BY c.created_at DESC",
        (client_id,),
    )
    connected = api_ws.get_connected_client_ids()
    return [
        {
            "client_id": r["id"],
            "name": r["name"],
            "owner_label": r.get("owner_label"),
            "platform": r.get("platform"),
            "access": r["access"],
            "expires_at": r.get("expires_at"),
            "profile": {
                "id": r["profile_id"],
                "name": r["profile_name"],
                "kind": r.get("profile_kind") or "person",
            },
            "method": r.get("set_up_method") or "onboard",
            "set_up_at": r["created_at"],
            "is_connected": r["id"] in connected,
        }
        for r in rows[:200]
    ]


async def handle_list_set_up(
    websocket: WebSocket, db: Database, client_id: str
) -> None:
    if not await onboarding.is_member(db, client_id):
        await send_error(websocket, onboarding.NOT_PERMITTED)
        return
    await websocket.send_json(
        {"type": "set_up_devices", "devices": await set_up_devices_of(db, client_id)}
    )


async def handle_remove_set_up(
    websocket: WebSocket, db: Database, message: dict, client_id: str
) -> None:
    target = message.get("client_id")
    if not isinstance(target, str) or not target:
        await send_error(websocket, onboarding.INVALID_REQUEST)
        return
    if not await onboarding.is_member(db, client_id):
        await send_error(websocket, onboarding.NOT_PERMITTED)
        return
    row = await db.fetchone(
        "SELECT id, is_active, pending_deletion_at, set_up_by_client_id "
        "FROM paired_clients WHERE id = ?",
        (target,),
    )
    if (
        not row
        or not row.get("is_active")
        or row.get("pending_deletion_at")
        or row.get("set_up_by_client_id") != client_id
        or target == client_id
    ):
        await send_error(websocket, onboarding.DEVICE_NOT_FOUND)
        return
    from api.pairing.clients import end_client_access

    by_name = await _device_name(db, client_id)
    await end_client_access(
        db, getattr(websocket.app.state, "ha_client", None), target, actor=by_name
    )
    log_admin_action(
        "set_up_device_removed", f"{target} by {client_id}", _ip(websocket)
    )
    await websocket.send_json({"type": "set_up_device_removed", "client_id": target})
