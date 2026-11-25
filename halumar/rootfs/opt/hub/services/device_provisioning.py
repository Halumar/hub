"""One Home Assistant user per paired device: requests to the integration.

The hub never creates, rotates or revokes HA tokens. The companion integration
owns one non-admin HA user with exactly one token per paired device; the hub
tells it what to do and stores the token it delivers
(``POST /api/integration/client-token``, api/integration.py). Contract:
``contracts/docs/per-device-users.md``.

Messages to the integration go over its WebSocket push session only (never to
apps or admin UIs):

* ``provision_client``: create/ensure the device user and deliver its token
  (``token_action`` ``ensure``) or a new token (``rotate``).
* ``update_client_identity``: name, owner label, remote access or guest expiry
  changed (only for devices whose token is active).
* ``deprovision_client``: the device is gone, remove its HA user.

A lost push is not a problem: ``GET /api/integration/status`` lists every
outstanding request in ``provisioning[]`` and the device list is the desired
state the integration reconciles against. While the push session is up, the
retry loop repeats outstanding requests with a backoff. Outstanding
rotations and the backoff live in the database (``token_requests``), so a
hub restart neither loses a rotation nor resets the backoff.

A deleted device keeps its HA user until its app confirmed the reset
(``client_reset_ack``) or a grace period ran out, online and offline alike:
the snapshot lists it in ``ha_user_holds`` and the integration
leaves it alone until ``deprovision_client`` arrives.

This module does not import ``api.*``; the WebSocket layer registers the send
function (:func:`set_sender`).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from models.database import HA_TOKEN_ACTIVE, HA_TOKEN_PENDING, Database
from services.audit_log import log_admin_action

logger = logging.getLogger("hub.services.device_provisioning")

TOKEN_ACTION_ENSURE = "ensure"
TOKEN_ACTION_ROTATE = "rotate"

REASON_PAIR = "pair"
REASON_RETRY = "retry"
REASON_ROTATE = "rotate"
REASON_REPROVISION = "reprovision"

DEPROVISION_DELETED = "deleted"
DEPROVISION_REVOKED = "revoked"
DEPROVISION_GUEST_EXPIRED = "guest_expired"
DEPROVISION_CLEARED = "cleared"

# Retry loop: check every RETRY_INTERVAL_SECONDS, repeat a request after
# BACKOFF_START_SECONDS, doubling up to BACKOFF_MAX_SECONDS (token_requests).
RETRY_INTERVAL_SECONDS = 30.0
BACKOFF_START_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 900.0

# A rotation nobody answered is dropped after this long (the admin can start
# a new one; the device keeps its working token meanwhile).
ROTATION_TTL_SECONDS = 3600.0

# A device with remote access gets a new token only over the hub, i.e. at
# home. Rotating it while it is away would lock it out of Home Assistant until
# it returns, so the rotation waits until the device connects to
# the hub, but never longer than this; then it runs anyway (audit log).
ROTATION_DEFER_SECONDS = 72 * 3600.0

# token_requests.reason of a deferred rotation (internal, never sent).
REASON_DEFERRED = "deferred"

# A deleted device whose app never confirmed the reset loses its HA user
# after this long. Until then a returning app is reset by the hub
# first and its HA token keeps working, so it never fails a login at HA.
# "Revoke" removes the HA user at once (lost or stolen device).
DELETE_HOLD_SECONDS = 24 * 3600.0

# Upper bound of provisioning[] / ha_user_holds in the status snapshot
# (contract maxItems).
MAX_PROVISIONING_ITEMS = 500

# Columns client_identity() reads.
_IDENTITY = (
    "id",
    "name",
    "owner_label",
    "access",
    "expires_at",
    "is_active",
    "pending_deletion_at",
    "ha_token_status",
    "ha_user_id",
)
IDENTITY_COLUMNS = ", ".join(_IDENTITY)

Sender = Callable[[dict[str, Any]], Awaitable[bool]]

_sender: Sender | None = None

# Admin requests waiting for a rotation's token (memory only: the waiting
# HTTP request does not survive a restart anyway).
_waiters: dict[str, asyncio.Event] = {}
_background: set[asyncio.Task] = set()


def _now() -> float:
    """Wall-clock seconds (seam for tests)."""
    return time.time()


def _iso(ts: float) -> str:
    """Fixed-width UTC ISO timestamp, so stored values compare as text."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f+00:00"
    )


def _parse(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def set_sender(sender: Sender | None) -> None:
    """Register the function that sends a message to the integration session."""
    global _sender
    _sender = sender


def reset() -> None:
    """Forget all in-memory state (tests)."""
    for event in _waiters.values():
        event.set()
    _waiters.clear()


def spawn(coro: Awaitable[Any]) -> asyncio.Task:
    """Run ``coro`` in the background and keep a reference until it ends."""
    from services import tasks

    task = tasks.spawn(coro, name="device-provisioning")
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def client_identity(row: dict) -> dict[str, Any]:
    """Device identity as the integration sees it (client-identity.json).

    ``row["remote_allowed"]`` is the effective profile rule remote_access,
    filled in by :func:`with_remote_access`; guests may have it.
    """
    is_guest = row.get("access") == "guest"
    return {
        "client_id": row["id"],
        "display_name": row["name"],
        "owner_label": row.get("owner_label") or None,
        "is_guest": is_guest,
        "remote_allowed": bool(row.get("remote_allowed")),
        "guest_expires_at": row.get("expires_at") if is_guest else None,
    }


async def with_remote_access(db: Database, rows: list[dict]) -> list[dict]:
    """Rows with ``remote_allowed`` = effective rule remote_access."""
    from services.profiles import store

    remote = await store.remote_allowed_map(db, [r["id"] for r in rows])
    return [{**r, "remote_allowed": remote.get(r["id"], False)} for r in rows]


def provision_request(
    row: dict, token_action: str, reason: str, request_id: str | None = None
) -> dict[str, Any]:
    """``provision_client.data`` / one ``provisioning[]`` item."""
    data = {
        **client_identity(row),
        "token_action": token_action,
        "reason": reason,
    }
    if request_id:
        data["request_id"] = request_id
    return data


def is_provisionable(row: dict | None) -> bool:
    """Only active devices (not revoked, not awaiting deletion) get a user."""
    return (
        bool(row) and bool(row.get("is_active")) and not row.get("pending_deletion_at")
    )


async def _load(db: Database, client_id: str) -> dict | None:
    row = await db.fetchone(
        f"SELECT {IDENTITY_COLUMNS} FROM paired_clients WHERE id = ?", (client_id,)
    )
    return (await with_remote_access(db, [row]))[0] if row else None


async def _send(message: dict[str, Any]) -> bool:
    if _sender is None:
        logger.debug("No integration sender registered, %s not sent", message["type"])
        return False
    try:
        return bool(await _sender(message))
    except Exception as e:  # noqa: BLE001 - the poll fallback covers it
        logger.warning("Sending %s to the integration failed: %s", message["type"], e)
        return False


async def _note_sent(
    db: Database, client_id: str, request_id: str, token_action: str, reason: str
) -> None:
    """Persist the request and its next retry time (doubling backoff)."""
    now = _now()
    async with db.transaction():
        row = await db.fetchone(
            "SELECT token_action, retry_delay FROM token_requests WHERE client_id = ?",
            (client_id,),
        )
        if row is None:
            await db.execute(
                "INSERT INTO token_requests (client_id, request_id, token_action, "
                "reason, requested_at, next_attempt_at, retry_delay) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    client_id,
                    request_id,
                    token_action,
                    reason,
                    _iso(now),
                    _iso(now + BACKOFF_START_SECONDS),
                    BACKOFF_START_SECONDS,
                ),
            )
            return
        previous = float(row.get("retry_delay") or 0)
        delay = (
            min(previous * 2, BACKOFF_MAX_SECONDS)
            if previous
            else BACKOFF_START_SECONDS
        )
        if row["token_action"] == TOKEN_ACTION_ROTATE:
            # A rotation keeps its request id until a token answers it.
            await db.execute(
                "UPDATE token_requests SET next_attempt_at = ?, retry_delay = ? "
                "WHERE client_id = ?",
                (_iso(now + delay), delay, client_id),
            )
        else:
            await db.execute(
                "UPDATE token_requests SET request_id = ?, next_attempt_at = ?, "
                "retry_delay = ? WHERE client_id = ?",
                (request_id, _iso(now + delay), delay, client_id),
            )


def forget(client_id: str) -> None:
    """Release a waiting admin request (token arrived or client gone).

    The stored request goes with the client row (ON DELETE CASCADE), with
    the delivered token (store_client_token) or in the retry loop's
    housekeeping (revoked or deleted devices).
    """
    waiter = _waiters.pop(client_id, None)
    if waiter:
        waiter.set()


async def request_provision(
    db: Database,
    client_id: str,
    *,
    token_action: str = TOKEN_ACTION_ENSURE,
    reason: str = REASON_PAIR,
    request_id: str | None = None,
) -> bool:
    """Send ``provision_client`` for an active device. True if sent."""
    row = await _load(db, client_id)
    if not is_provisionable(row):
        return False
    request_id = request_id or str(uuid.uuid4())
    sent = await _send(
        {
            "type": "provision_client",
            "data": provision_request(row, token_action, reason, request_id),
        }
    )
    await _note_sent(db, client_id, request_id, token_action, reason)
    logger.info(
        "provision_client %s/%s for %s (%s)",
        token_action,
        reason,
        client_id,
        "sent" if sent else "integration not connected, left for the poll",
    )
    return sent


async def send_identity_update(db: Database, client_id: str) -> bool:
    """Send ``update_client_identity`` for a device whose token is active."""
    row = await _load(db, client_id)
    if not is_provisionable(row) or row.get("ha_token_status") != HA_TOKEN_ACTIVE:
        return False
    return await _send({"type": "update_client_identity", "data": client_identity(row)})


async def send_deprovision(client_id: str, reason: str) -> bool:
    """Send ``deprovision_client`` (after the removal is committed)."""
    forget(client_id)
    sent = await _send(
        {
            "type": "deprovision_client",
            "data": {"client_id": client_id, "reason": reason},
        }
    )
    logger.info(
        "deprovision_client %s for %s (%s)",
        reason,
        client_id,
        "sent" if sent else "integration not connected, its reconcile removes the user",
    )
    return sent


async def delivery_allowed(
    db: Database, row: dict, ha_user_id: str, request_id: str | None
) -> bool:
    """Whether ``POST /integration/client-token`` may store this token.

    Allowed while the device waits for its token (``pending``: the request is
    listed in ``provisioning[]``), for the request id of an open request of
    this device, or unsolicited for the HA user already stored for it (the
    integration rotates on its own when ``local_only`` changes). A token for
    another HA user outside of that is refused, so the integration token alone
    cannot hand arbitrary HA tokens to arbitrary devices.
    """
    if row.get("ha_token_status") == HA_TOKEN_PENDING:
        return True
    if row.get("ha_user_id") and row.get("ha_user_id") == ha_user_id:
        return True
    if request_id:
        open_request = await db.fetchone(
            "SELECT 1 FROM token_requests WHERE client_id = ? AND request_id = ?",
            (row["id"], request_id),
        )
        return open_request is not None
    return False


def _rotation_cutoff() -> str:
    return _iso(_now() - ROTATION_TTL_SECONDS)


async def start_rotation(
    db: Database, client_id: str, reason: str = REASON_ROTATE
) -> str:
    """Store an outstanding rotation (answered by the next delivered token).

    Returns its request id; a rotation that is still open is reused. Stored
    in ``token_requests``, so it survives a hub restart.
    """
    _waiters.setdefault(client_id, asyncio.Event())
    async with db.transaction():
        row = await db.fetchone(
            "SELECT request_id, reason FROM token_requests WHERE client_id = ? "
            "AND token_action = ? AND (reason = ? OR requested_at >= ?)",
            (client_id, TOKEN_ACTION_ROTATE, REASON_DEFERRED, _rotation_cutoff()),
        )
        if row and row["reason"] == REASON_DEFERRED:
            # No reason to wait any more (connected, or no remote access).
            await db.execute(
                "UPDATE token_requests SET reason = ?, requested_at = ?, "
                "next_attempt_at = NULL, retry_delay = 0 WHERE client_id = ?",
                (reason, _iso(_now()), client_id),
            )
        if row:
            return row["request_id"]
        request_id = str(uuid.uuid4())
        await db.execute(
            "INSERT OR REPLACE INTO token_requests (client_id, request_id, "
            "token_action, reason, requested_at, next_attempt_at, retry_delay) "
            "VALUES (?, ?, ?, ?, ?, NULL, 0)",
            (client_id, request_id, TOKEN_ACTION_ROTATE, reason, _iso(_now())),
        )
    return request_id


async def defer_rotation(db: Database, client_id: str) -> str:
    """Store a rotation that waits until the device connects.

    For a device with remote access that is away: it would lose Home
    Assistant until it returns. The rotation runs when the device connects to
    the hub (:func:`release_deferred_rotation`) or after
    :data:`ROTATION_DEFER_SECONDS` (:func:`release_expired_deferrals`). An
    open rotation (deferred or running) is reused. Returns its request id.
    """
    now = _now()
    async with db.transaction():
        row = await db.fetchone(
            "SELECT request_id FROM token_requests WHERE client_id = ? "
            "AND token_action = ? AND (reason = ? OR requested_at >= ?)",
            (client_id, TOKEN_ACTION_ROTATE, REASON_DEFERRED, _rotation_cutoff()),
        )
        if row:
            return row["request_id"]
        request_id = str(uuid.uuid4())
        await db.execute(
            "INSERT OR REPLACE INTO token_requests (client_id, request_id, "
            "token_action, reason, requested_at, next_attempt_at, retry_delay) "
            "VALUES (?, ?, ?, ?, ?, ?, 0)",
            (
                client_id,
                request_id,
                TOKEN_ACTION_ROTATE,
                REASON_DEFERRED,
                _iso(now),
                _iso(now + ROTATION_DEFER_SECONDS),
            ),
        )
    log_admin_action(
        "client_token_rotation_deferred",
        details=f"client_id={client_id} until={_iso(now + ROTATION_DEFER_SECONDS)}",
    )
    return request_id


async def rotation_deferred(db: Database, client_id: str) -> bool:
    """Whether a rotation of this device waits for it to connect."""
    row = await db.fetchone(
        "SELECT 1 FROM token_requests WHERE client_id = ? AND token_action = ? "
        "AND reason = ?",
        (client_id, TOKEN_ACTION_ROTATE, REASON_DEFERRED),
    )
    return row is not None


async def release_deferred_rotation(
    db: Database, client_id: str, *, expired: bool = False
) -> bool:
    """Start a deferred rotation now. True if there was one.

    Called when the device connected to the hub (it fetches the new token
    right away) or, with ``expired``, when the deferral ran out.
    """
    async with db.transaction():
        row = await db.fetchone(
            "SELECT request_id FROM token_requests WHERE client_id = ? "
            "AND token_action = ? AND reason = ?",
            (client_id, TOKEN_ACTION_ROTATE, REASON_DEFERRED),
        )
        if row is None:
            return False
        await db.execute(
            "UPDATE token_requests SET reason = ?, requested_at = ?, "
            "next_attempt_at = NULL, retry_delay = 0 WHERE client_id = ?",
            (REASON_ROTATE, _iso(_now()), client_id),
        )
    log_admin_action(
        "client_token_rotation_deadline_reached"
        if expired
        else "client_token_rotation_released",
        details=f"client_id={client_id}",
    )
    await request_provision(
        db,
        client_id,
        token_action=TOKEN_ACTION_ROTATE,
        reason=REASON_ROTATE,
        request_id=row["request_id"],
    )
    return True


async def release_expired_deferrals(db: Database) -> int:
    """Run every deferred rotation whose deadline passed (no endless deferral)."""
    rows = await db.fetchall(
        "SELECT client_id FROM token_requests WHERE token_action = ? AND reason = ? "
        "AND next_attempt_at <= ?",
        (TOKEN_ACTION_ROTATE, REASON_DEFERRED, _iso(_now())),
    )
    released = 0
    for row in rows:
        logger.info(
            "Deferred rotation of %s reached its deadline, rotating now",
            row["client_id"],
        )
        if await release_deferred_rotation(db, row["client_id"], expired=True):
            released += 1
    return released


def rotation_waiter(client_id: str) -> asyncio.Event:
    """Event set when a token answers the client's rotation."""
    return _waiters.setdefault(client_id, asyncio.Event())


async def rotation_pending(db: Database, client_id: str) -> bool:
    """Whether a running (not deferred) rotation waits for its token."""
    row = await db.fetchone(
        "SELECT 1 FROM token_requests WHERE client_id = ? AND token_action = ? "
        "AND reason != ? AND requested_at >= ?",
        (client_id, TOKEN_ACTION_ROTATE, REASON_DEFERRED, _rotation_cutoff()),
    )
    return row is not None


def complete_rotation(client_id: str) -> None:
    """A new token arrived (stored by store_client_token): release waiters."""
    forget(client_id)


# ── The hub path: no HA key on the device ────────

# Log kind of a withdrawn device key (hub kind, contract log-entry.json).
LOG_KIND_KEY_WITHDRAWN = "ha_key_withdrawn"


async def withdraw_device_key(
    db: Database, client_id: str, *, send: bool, catch_up: bool = False
) -> str:
    """The token the app of a device that stopped holding a key knew stops
    working (``resolver.holds_key`` false: Hub only without remote access).

    The device keeps its HA user: the hub switches for it through
    ``<ns>.control`` with that user's token, and the token never goes to
    the app (``client_config.ha_token`` null). So the token is rotated: the
    integration removes the old one once the hub stored the new one. A
    child's watch whose phone moves under parental controls lands here as
    well (re-scope). ``send`` = push the request now (else the status poll
    and the retry loop carry it). ``catch_up`` = found by
    :func:`secure_keyless_tokens`. Returns the request id.

    The old token keeps working until the hub accepted the new one (the
    integration removes it after that answer), so the log entry
    ``ha_key_withdrawn`` is written then (:func:`key_withdrawal_confirmed`),
    not here; until then the device counts as holding its old key
    (:func:`stale_keys`).
    """
    request_id = await start_rotation(db, client_id)
    if send:
        await request_provision(
            db,
            client_id,
            token_action=TOKEN_ACTION_ROTATE,
            reason=REASON_ROTATE,
            request_id=request_id,
        )
    log_admin_action(
        "client_token_withdrawal_started",
        details=f"client_id={client_id}" + (" catch_up" if catch_up else ""),
    )
    return request_id


async def store_delivered_token(
    db: Database,
    client_id: str,
    *,
    ha_user_id: str,
    refresh_token_id: str,
    token: str,
) -> bool:
    """Store a token the integration delivered (``POST
    /api/integration/client-token``) and release the rotation's waiters.

    When the device had lost its key while its app may still have known the
    old token (:func:`stale_keys`), the withdrawal is complete with this
    acceptance (the integration removes the old token after the answer):
    logged here, once. Returns whether the token was stored; storage errors
    propagate.
    """
    withdrawn = client_id in await stale_keys(db, [client_id])
    stored = await db.store_client_token(
        client_id,
        ha_user_id=ha_user_id,
        refresh_token_id=refresh_token_id,
        token=token,
    )
    if not stored:
        return False
    # The loss is settled: the integration removes every older token after
    # this answer. Cleared instead of compared again, so a clock stepped
    # back between the loss and the new token never rotates again.
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET key_lost_at = NULL WHERE id = ?", (client_id,)
        )
    complete_rotation(client_id)
    if withdrawn:
        await key_withdrawal_confirmed(db, client_id)
    return True


async def key_withdrawal_confirmed(db: Database, client_id: str) -> None:
    """The hub accepted the new token of a device whose app may still have
    known the old one (:func:`stale_keys` before the delivery): the old token
    stops working now. Logged once per withdrawal."""
    log_admin_action("client_token_withdrawn", details=f"client_id={client_id}")
    from services import event_log

    await event_log.record(
        db,
        category=event_log.CHANGE,
        kind=LOG_KIND_KEY_WITHDRAWN,
        client_id=client_id,
        detail={"note": None},
    )


async def withdraw_closed_key(db: Database, client_id: str, *, send: bool) -> bool:
    """Access closed: the token the app held stops working.

    Outside its access a device gets no token (``client_config.ha_token``
    null), but a modified app would keep the one it had: read everything in
    Home Assistant, and with the control path direct switch everything. So
    the token is rotated like when a device stops holding a key; the hub
    keeps the new one and hands it out when access opens again. Returns
    whether a rotation was started (only for an active token; the caller
    passes only devices that hold a key; one that runs already is reused).
    """
    row = await db.fetchone(
        "SELECT id, is_active, pending_deletion_at, ha_token_status FROM "
        "paired_clients WHERE id = ?",
        (client_id,),
    )
    if not is_provisionable(row) or row.get("ha_token_status") != HA_TOKEN_ACTIVE:
        return False
    if await rotation_pending(db, client_id):
        return False
    request_id = await start_rotation(db, client_id)
    if send:
        await request_provision(
            db,
            client_id,
            token_action=TOKEN_ACTION_ROTATE,
            reason=REASON_ROTATE,
            request_id=request_id,
        )
    log_admin_action("client_token_withdrawn_access", details=f"client_id={client_id}")
    return True


# Devices that may have stopped holding a key, with an active token: every
# device whose key_lost_at is set (changes.run, the migration), and every
# device with parental controls with the moments it got them: the last
# assignment from a profile that is not (or no longer) a kids profile, and
# when its profile switched parental controls on (a profile created with them
# has no such moment: its devices came by assignment or pairing). Whether the
# device holds a key now is decided by the resolver (holds_key).
_KEYLESS_CANDIDATES_SQL = (
    "SELECT c.id, c.ha_token_minted_at, c.key_lost_at, "
    "CASE WHEN p.child_protection = 1 THEN "
    "(SELECT MAX(a.ts) FROM profile_audit a "
    "LEFT JOIN profiles bp ON bp.id = json_extract(a.before_json, '$.profile_id') "
    "WHERE a.client_id = c.id AND a.action = 'profile_assigned' "
    "AND COALESCE(bp.child_protection, 0) = 0) END AS assigned_at, "
    "CASE WHEN p.child_protection = 1 THEN "
    "(SELECT MIN(r.created_at) FROM profile_revisions r "
    "WHERE r.profile_id = c.profile_id AND r.child_protection = 1 "
    "AND r.rev > (SELECT MAX(r0.rev) FROM profile_revisions r0 "
    "WHERE r0.profile_id = c.profile_id AND r0.child_protection = 0)) END "
    "AS protected_at "
    "FROM paired_clients c JOIN profiles p ON p.id = c.profile_id "
    "WHERE (p.child_protection = 1 OR c.key_lost_at IS NOT NULL) "
    "AND c.is_active = 1 "
    "AND c.pending_deletion_at IS NULL AND c.ha_token_status = ? "
    "AND c.ha_token_encrypted IS NOT NULL"
)


def _minted_before(minted: str | None, since: list[str | None]) -> bool:
    """Whether the token predates parental controls.

    Audit and revision times are stored to the second (rounded down), so a
    token of that same second counts as newer: the rotation of the switch
    itself is minted then, and a lost rotation right after pairing within
    the same second is not worth a rotation on every check. A token
    without a mint time counts as older.
    """
    minted_ts = _parse(minted)
    times = [t for t in (_parse(s) for s in since) if t is not None]
    if not times:
        return False
    return minted_ts is None or minted_ts < max(times)


# Watches of phones with parental controls (a child's watch) whose token is
# active, with the moments their PHONE got parental controls (the watch's own
# profile is its adult default). Such a watch holds its key on purpose (it
# needs remote access), so only a token older than the phone's switch is
# rotated: the re-scope rotation did not reach it.
_CHILD_WATCH_TOKENS_SQL = (
    "SELECT w.id, w.ha_token_minted_at, "
    "(SELECT MAX(a.ts) FROM profile_audit a "
    "LEFT JOIN profiles bp ON bp.id = json_extract(a.before_json, '$.profile_id') "
    "WHERE a.client_id = ph.id AND a.action = 'profile_assigned' "
    "AND COALESCE(bp.child_protection, 0) = 0) AS assigned_at, "
    "(SELECT MIN(r.created_at) FROM profile_revisions r "
    "WHERE r.profile_id = ph.profile_id AND r.child_protection = 1 "
    "AND r.rev > (SELECT MAX(r0.rev) FROM profile_revisions r0 "
    "WHERE r0.profile_id = ph.profile_id AND r0.child_protection = 0)) "
    "AS protected_at "
    "FROM paired_clients w JOIN paired_clients ph ON ph.id = w.set_up_by_client_id "
    "JOIN profiles p ON p.id = ph.profile_id "
    "WHERE p.child_protection = 1 AND w.platform IN ('watchos', 'wearos') "
    "AND w.set_up_method = 'onboard' AND w.seal_key IS NOT NULL "
    "AND w.is_active = 1 AND w.pending_deletion_at IS NULL "
    "AND w.ha_token_status = ? AND w.ha_token_encrypted IS NOT NULL"
)


async def stale_keys(db: Database, client_ids: list[str] | None = None) -> set[str]:
    """Devices that hold no key now (``resolver.holds_key`` false) but whose
    active token predates the moment they lost it: their app may still know
    a token that works until the hub accepted a new one. ``client_ids``
    limits the check (None = every device)."""
    from services.profiles import resolver as res
    from services.profiles import store

    sql, params = _KEYLESS_CANDIDATES_SQL, [HA_TOKEN_ACTIVE]
    if client_ids is not None:
        if not client_ids:
            return set()
        sql += f" AND c.id IN ({', '.join('?' for _ in client_ids)})"
        params.extend(client_ids)
    rows = await db.fetchall(sql, tuple(params))
    if not rows:
        return set()
    try:
        resolved = await store.resolve_all(db, [r["id"] for r in rows])
    except Exception:  # noqa: BLE001 - one by one; an unresolvable one is skipped
        resolved = {}
        for r in rows:
            try:
                resolved.update(await store.resolve_all(db, [r["id"]]))
            except Exception:  # noqa: BLE001 - decided once it resolves again
                continue
    out = set()
    for row in rows:
        item = resolved.get(row["id"])
        # A device that holds a key keeps it on purpose (Checked, Direct, or
        # Hub only with remote access); unresolvable: the next round.
        if item is None or res.holds_key(item["rules"]):
            continue
        if _minted_before(
            row.get("ha_token_minted_at"),
            [row.get("assigned_at"), row.get("protected_at"), row.get("key_lost_at")],
        ):
            out.add(row["id"])
    return out


async def secure_keyless_tokens(db: Database, *, send: bool = False) -> int:
    """Rotate every token that may have reached the app of a device that
    holds no key now (:func:`stale_keys`). Returns the number of rotations
    started.

    Losing the key rotates at once (``changes.apply_effects``: parental
    controls or Hub only switched on, remote access lost, also when a
    temporary adjustment ends); this catches a rotation that got lost (hub
    stopped in between, the integration did not answer within
    :data:`ROTATION_TTL_SECONDS`, the delivery failed) and the devices the
    database migration moved to Hub only. Runs at start and in the retry
    loop until the hub accepted a new token: ``key_lost_at`` and the open
    rotation (``token_requests``) survive a restart; an open rotation is
    repeated by the retry loop with its backoff, not started again.
    Idempotent (the new token is younger than the moment the key was lost).
    """
    started = 0
    for client_id in sorted(await stale_keys(db)):
        if await rotation_pending(db, client_id):
            continue
        logger.warning(
            "Device %s holds no Home Assistant key but its token predates that, "
            "rotating it",
            client_id,
        )
        await withdraw_device_key(db, client_id, send=send, catch_up=True)
        started += 1
    for row in await db.fetchall(_CHILD_WATCH_TOKENS_SQL, (HA_TOKEN_ACTIVE,)):
        if not _minted_before(
            row.get("ha_token_minted_at"),
            [row.get("assigned_at"), row.get("protected_at")],
        ):
            continue
        if await rotation_pending(db, row["id"]):
            continue
        logger.warning(
            "Watch %s of a phone with parental controls holds a token from "
            "before the switch, rotating it",
            row["id"],
        )
        await withdraw_device_key(db, row["id"], send=send, catch_up=True)
        started += 1
    return started


# Devices whose access is closed and whose active token is older than the
# last close: the withdrawal at the close got lost (only devices that hold a
# key, decided by the resolver).
_CLOSED_TOKENS_SQL = (
    "SELECT c.id, c.ha_token_minted_at, "
    "(SELECT MAX(e.ts) FROM event_log e WHERE e.client_id = c.id "
    "AND e.kind = 'access_closed') AS closed_at "
    "FROM paired_clients c JOIN profiles p ON p.id = c.profile_id "
    "WHERE c.access_state = 'closed' "
    "AND c.is_active = 1 AND c.pending_deletion_at IS NULL "
    "AND c.ha_token_status = ? AND c.ha_token_encrypted IS NOT NULL"
)


async def secure_closed_tokens(db: Database, *, send: bool = False) -> int:
    """Withdraw again every key of a closed device that predates the close.

    The rotation started when access closed is dropped after
    :data:`ROTATION_TTL_SECONDS` when the integration did not answer;
    this starts it again, like :func:`secure_keyless_tokens` for devices
    that stopped holding a key. Runs in the retry loop; idempotent (the new
    token is younger than the close). Returns the number of rotations
    started.
    """
    from services.profiles import resolver as res
    from services.profiles import store

    started = 0
    rows = await db.fetchall(_CLOSED_TOKENS_SQL, (HA_TOKEN_ACTIVE,))
    resolved = await store.resolve_all(db, [r["id"] for r in rows]) if rows else {}
    for row in rows:
        item = resolved.get(row["id"])
        if item is None or not res.holds_key(item["rules"]):
            continue  # no key on the device: secure_keyless_tokens decides
        if not _minted_before(row.get("ha_token_minted_at"), [row.get("closed_at")]):
            continue
        if await withdraw_closed_key(db, row["id"], send=send):
            logger.warning(
                "Access of %s is closed but its HA token predates the close, "
                "rotating it",
                row["id"],
            )
            started += 1
    return started


async def _housekeeping(db: Database) -> None:
    """Drop expired rotations and requests of devices that need none."""
    async with db.transaction():
        cursor = await db.execute(
            "DELETE FROM token_requests WHERE (token_action = ? AND reason != ? "
            "AND requested_at < ?) "
            "OR client_id IN (SELECT id FROM paired_clients WHERE is_active = 0 "
            "OR pending_deletion_at IS NOT NULL OR (ha_token_status = ? "
            "AND token_requests.token_action = ?))",
            (
                TOKEN_ACTION_ROTATE,
                REASON_DEFERRED,
                _rotation_cutoff(),
                HA_TOKEN_ACTIVE,
                TOKEN_ACTION_ENSURE,
            ),
        )
    if cursor.rowcount:
        logger.info("Dropped %d finished or expired token request(s)", cursor.rowcount)


_OUTSTANDING_SQL = (
    "SELECT "
    + ", ".join(f"pc.{c}" for c in _IDENTITY)
    + ", tr.request_id AS tr_request_id, tr.token_action AS tr_action, "
    "tr.reason AS tr_reason, tr.next_attempt_at AS tr_next_attempt_at "
    "FROM paired_clients pc LEFT JOIN token_requests tr ON tr.client_id = pc.id "
    "WHERE pc.is_active = 1 AND pc.pending_deletion_at IS NULL "
    "AND (pc.ha_token_status = ? OR (tr.token_action = ? AND tr.reason != ? "
    "AND tr.requested_at >= ?)) "
    "ORDER BY pc.created_at LIMIT ?"
)


async def _outstanding_rows(db: Database) -> list[dict]:
    rows = await db.fetchall(
        _OUTSTANDING_SQL,
        (
            HA_TOKEN_PENDING,
            TOKEN_ACTION_ROTATE,
            REASON_DEFERRED,
            _rotation_cutoff(),
            MAX_PROVISIONING_ITEMS,
        ),
    )
    return await with_remote_access(db, rows)


def _item(row: dict) -> dict[str, Any]:
    if row["ha_token_status"] == HA_TOKEN_PENDING:
        reason = REASON_REPROVISION if row.get("ha_user_id") else REASON_PAIR
        request_id = (
            row["tr_request_id"]
            if row.get("tr_action") == TOKEN_ACTION_ENSURE
            else None
        )
        return provision_request(row, TOKEN_ACTION_ENSURE, reason, request_id)
    return provision_request(
        row, TOKEN_ACTION_ROTATE, row["tr_reason"], row["tr_request_id"]
    )


async def outstanding_requests(db: Database) -> list[dict[str, Any]]:
    """``provisioning[]`` of the status snapshot.

    Every active device waiting for its token (``pair``, or ``reprovision``
    when it had a user before) plus every unanswered rotation.
    """
    return [_item(row) for row in await _outstanding_rows(db)]


async def retry_once(db: Database, *, connected: bool) -> int:
    """Repeat outstanding requests whose backoff ran out. Returns the count.

    Also releases the HA users of deleted devices whose hold ran out
    (independent of the integration connection: without a push the
    integration's reconcile removes them).
    """
    await _housekeeping(db)
    await release_expired_holds(db)
    await release_expired_deferrals(db)
    # Sent right below with the other outstanding requests.
    await secure_keyless_tokens(db)
    await secure_closed_tokens(db)
    if not connected:
        return 0
    now = _now()
    sent = 0
    for row in await _outstanding_rows(db):
        next_at = _parse(row.get("tr_next_attempt_at"))
        if next_at is not None and next_at > now:
            continue
        item = _item(row)
        rotate = item["token_action"] == TOKEN_ACTION_ROTATE
        if await request_provision(
            db,
            row["id"],
            token_action=item["token_action"],
            reason=REASON_RETRY,
            request_id=item.get("request_id") if rotate else None,
        ):
            sent += 1
    return sent


# ── Deleted devices: hold the HA user until the reset is confirmed ────────


async def held_user_ids(db: Database) -> list[str]:
    """``ha_user_holds`` of the status snapshot (deleted, not yet released)."""
    rows = await db.fetchall(
        "SELECT id FROM paired_clients WHERE pending_deletion_at IS NOT NULL "
        "AND ha_user_released_at IS NULL ORDER BY pending_deletion_at LIMIT ?",
        (MAX_PROVISIONING_ITEMS,),
    )
    return [row["id"] for row in rows]


async def release_user(
    db: Database, client_id: str, reason: str = DEPROVISION_DELETED
) -> bool:
    """End the hold of a deleted device and remove its HA user. True if released.

    No-op when the device is not held (already released, or its row is gone:
    the purge on the app's reset ack sends ``deprovision_client`` itself).
    """
    async with db.transaction():
        cursor = await db.execute(
            "UPDATE paired_clients SET ha_user_released_at = ? WHERE id = ? "
            "AND pending_deletion_at IS NOT NULL AND ha_user_released_at IS NULL",
            (_iso(_now()), client_id),
        )
    if not cursor.rowcount:
        return False
    await send_deprovision(client_id, reason)
    return True


async def release_expired_holds(db: Database) -> int:
    """Release every hold older than :data:`DELETE_HOLD_SECONDS`."""
    released = 0
    cutoff = _now() - DELETE_HOLD_SECONDS
    rows = await db.fetchall(
        "SELECT id, pending_deletion_at FROM paired_clients "
        "WHERE pending_deletion_at IS NOT NULL AND ha_user_released_at IS NULL"
    )
    for row in rows:
        deleted_at = _parse(row["pending_deletion_at"])
        if deleted_at is not None and deleted_at > cutoff:
            continue
        logger.info(
            "Deleted device %s never confirmed its reset, removing its HA user",
            row["id"],
        )
        if await release_user(db, row["id"]):
            released += 1
    return released


async def retry_loop(
    db: Database, is_connected: Callable[[], bool]
) -> None:  # pragma: no cover - thin loop around retry_once
    """Background task: repeat outstanding requests while the push session is up."""
    try:
        await secure_keyless_tokens(db, send=is_connected())
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - checked again in every round
        logger.warning("Checking the tokens of devices without a key failed: %s", e)
    while True:
        await asyncio.sleep(RETRY_INTERVAL_SECONDS)
        try:
            await retry_once(db, connected=is_connected())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            logger.warning("Provisioning retry failed: %s", e)
