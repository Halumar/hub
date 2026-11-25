"""Home Assistant access of watches, sealed to the watch's key.

A watch (``watchos``, ``wearos``) never has a hub socket: watchOS allows
only HTTP (TN3135). Its phone opens the onboarding session with the watch's
X25519 key; the hub stores that public key (``paired_clients.seal_key``) and
closes the onboarding connection without authenticating it, so the phone
never reads the watch's ``client_config`` (api/ws/onboarding.py).

The phone that set the watch up (``set_up_by``) asks ``watch_access_get``
and gets ``watch_access``: the watch's whole ``client_config`` (with its HA
token) sealed by HPKE to the watch's key (``info = hub/watch-access/v1``,
``aad = client_id``), ``pending`` while the integration has not issued the
token yet, or ``gone``. Whenever the watch's config changes (every
``client_config_updated`` for it or for everyone, e.g. a token rotation) the
hub pushes ``watch_access`` to every connection of that phone (debounced).
The phone passes it on unchanged; it cannot open it.

A watch whose phone has parental controls is a child's watch (contract
protocol.md "A child's watch"): it resolves with the phone's
profile and kids state (``services.profiles.store.follow_phone``), it is
``gone`` while the phone's effective rule ``watch`` is off, and
:func:`follow_phones` re-scopes or removes it after a profile write.

Contract: contracts docs/protocol.md "Watches: access through the phone".
This module does not import ``api.*`` at import time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from models.database import HA_TOKEN_PENDING, Database
from services import onboard_crypto
from services.onboarding import WATCH_PLATFORMS

logger = logging.getLogger("hub.services.watch_access")

INFO = b"hub/watch-access/v1"
STATUS_READY = "ready"
STATUS_PENDING = "pending"
STATUS_GONE = "gone"
PUSH_DEBOUNCE_SECONDS = 0.5

_context: dict[str, Any] = {}
# Watches whose access changed since the last push; ``None`` = every watch.
_dirty: set[str | None] = set()
_push_task: asyncio.Task | None = None


def register(db: Database, ha_client) -> None:
    """Database and HA client for the debounced push (set at startup)."""
    _context["db"] = db
    _context["ha_client"] = ha_client


def reset_state() -> None:
    """Forget the context and pending pushes (tests)."""
    global _push_task
    _context.clear()
    _dirty.clear()
    _push_task = None


def is_watch(row: dict | None) -> bool:
    return bool(row) and row.get("platform") in WATCH_PLATFORMS


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def seal(seal_key: str, watch_id: str, config: dict) -> dict | None:
    """``watch_access.sealed`` for ``config``; None for an unusable key."""
    public_key = onboard_crypto.parse_public_key(seal_key)
    if public_key is None:
        return None
    plaintext = json.dumps(
        {"client_id": watch_id, "issued_at": _now_iso(), "config": config},
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    try:
        enc, ciphertext = onboard_crypto.seal(
            public_key, plaintext, info=INFO, aad=watch_id.encode()
        )
    except ValueError:
        return None
    return {
        "suite": onboard_crypto.SUITE,
        "enc": onboard_crypto.b64url(enc),
        "ciphertext": onboard_crypto.b64url(ciphertext),
    }


async def _row(db: Database, watch_id: str) -> dict | None:
    return await db.fetchone(
        "SELECT id, platform, seal_key, set_up_by_client_id, set_up_method, "
        "is_active, pending_deletion_at, ha_token_status "
        "FROM paired_clients WHERE id = ?",
        (watch_id,),
    )


def _served_by(row: dict | None, phone_id: str) -> bool:
    """Whether ``phone_id`` set up this active watch and may carry its access."""
    return (
        is_watch(row)
        and bool(row.get("seal_key"))
        and bool(row.get("is_active"))
        and not row.get("pending_deletion_at")
        and row.get("set_up_method") == "onboard"
        and row.get("set_up_by_client_id") == phone_id
    )


async def message(db: Database, ha_client, watch_id: Any, phone_id: str) -> dict:
    """``watch_access`` of ``watch_id`` for the phone ``phone_id``."""
    if not isinstance(watch_id, str) or not watch_id or len(watch_id) > 64:
        return {"type": "watch_access", "client_id": "", "status": STATUS_GONE}
    gone = {"type": "watch_access", "client_id": watch_id, "status": STATUS_GONE}
    row = await _row(db, watch_id)
    if not _served_by(row, phone_id):
        # A phone that was offline when the hub removed its child's watch still
        # learns why (watch_access.reason, kept for that phone).
        reason = (await _removed(db)).get(watch_id) or {}
        if row is None and reason.get("phone") == phone_id and reason.get("reason"):
            return {**gone, "reason": reason["reason"]}
        return gone
    if await child_watch_allowed(db, phone_id) is False:
        # The phone may have no watch (parental controls without kids_watch,
        # or Hub only without remote access): no access, the hub removes the
        # watch (follow_phones).
        return gone
    from api.ws.client import _get_client_config

    config = await _get_client_config(db, watch_id, ha_client, sealed_for_watch=True)
    if config is None:
        return gone
    if config.get("ha_token") is None and (
        row.get("ha_token_status") == HA_TOKEN_PENDING or config.get("token_error")
    ):
        # The watch holds a key that is not usable yet: not issued, held back
        # until the integration has its rules, or being replaced
        # (client_config.token_error). A push follows once it is usable.
        return {"type": "watch_access", "client_id": watch_id, "status": STATUS_PENDING}
    sealed = seal(row["seal_key"], watch_id, config)
    if sealed is None:
        logger.error("Watch %s has an unusable seal key", watch_id)
        return gone
    return {
        "type": "watch_access",
        "client_id": watch_id,
        "status": STATUS_READY,
        "sealed": sealed,
    }


async def child_watch_allowed(db: Database, phone_id: str) -> bool | None:
    """Whether the phone may carry a watch. With parental controls: its
    effective rule ``watch`` is on (``kids_watch``). Without: False while the
    boundary ``hub_only`` holds ``watch`` off (the phone holds no key), else
    None (its watch has its own rules, limited by the phone's)."""
    from services.profiles import store

    resolved = await store.resolve_client(db, phone_id)
    if not resolved:
        return False
    _row, prof, rules = resolved
    watch = rules.get("watch") or {}
    if not prof["child_protection"]:
        return False if hub_only_removes_watch(watch) else None
    return watch.get("value") is True


def hub_only_removes_watch(watch: dict) -> bool:
    """The phone's effective rule ``watch`` is off through the boundary
    ``hub_only``: a phone on Hub only without remote access carries no watch."""
    return watch.get("value") is not True and watch.get("reason") == "boundary_hub_only"


REMOVED_SETTING = "watch_removed_reasons"
REMOVED_KEEP_DAYS = 30
REMOVED_MAX = 200


async def _removed(db: Database) -> dict[str, dict]:
    """Watches the hub removed with a reason: watch id -> {phone, reason, at}
    (kept 30 days, at most 200)."""
    try:
        value = json.loads(await db.get_setting(REMOVED_SETTING) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _remember_removed(
    db: Database, watch_id: str, phone_id: str, reason: str
) -> None:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=REMOVED_KEEP_DAYS)).isoformat()
    kept = {
        k: v
        for k, v in (await _removed(db)).items()
        if isinstance(v, dict) and str(v.get("at", "")) >= cutoff
    }
    kept[watch_id] = {"phone": phone_id, "reason": reason, "at": now.isoformat()}
    newest = sorted(kept.items(), key=lambda kv: kv[1]["at"])[-REMOVED_MAX:]
    await db.set_setting(REMOVED_SETTING, json.dumps(dict(newest)))


async def _remove(
    db: Database, watch_id: str, why: str, reason: str | None = None
) -> None:
    """Remove a watch like ``set_up_device_remove`` and tell its phone."""
    from api import ws as api_ws
    from api.pairing import clients as clients_api
    from services.audit_log import log_admin_action

    row = await _row(db, watch_id)
    if row is None:
        return
    phone_id = row.get("set_up_by_client_id")
    try:
        await clients_api.end_client_access(db, _context.get("ha_client"), watch_id)
    except Exception:  # noqa: BLE001 - the admin can still remove it
        logger.exception("Watch %s not removed", watch_id)
        return
    log_admin_action(why, details=f"client_id={watch_id} phone={phone_id}")
    if phone_id and reason:
        await _remember_removed(db, watch_id, phone_id, reason)
    if phone_id:
        await api_ws.send_to_client(
            phone_id,
            {
                "type": "watch_access",
                "client_id": watch_id,
                "status": STATUS_GONE,
                **({"reason": reason} if reason else {}),
            },
        )


async def _rotate(db: Database, watch_id: str) -> None:
    """A new token for the watch now (the one it held stops working)."""
    from api import ws as api_ws
    from services import device_provisioning

    row = await db.fetchone(
        "SELECT id, is_active, pending_deletion_at, ha_token_status "
        "FROM paired_clients WHERE id = ?",
        (watch_id,),
    )
    if (
        device_provisioning.is_provisionable(row)
        and row.get("ha_token_status") == "active"
    ):
        await device_provisioning.withdraw_device_key(
            db, watch_id, send=api_ws.get_integration_push_connected()
        )
    notify(watch_id)


async def follow_phones(
    db: Database,
    rescoped: list[str],
    removed: list[str],
    *,
    left: list[str] | tuple[str, ...] = (),
    reasons: dict[str, str] | None = None,
) -> None:
    """After a profile write.

    * ``removed``: a watch whose phone may no longer have one (a child's
      watch without ``kids_watch``, a watch of a phone on Hub only without
      remote access) goes (HA user and token removed); the phone hears
      ``gone`` with the ``reason`` and tells its user.
    * ``rescoped``: a watch that just became a child's watch gets a new token
      (the adult token it held stops working) and its new access at once.
    * ``left``: a watch whose phone just left parental controls stays; it
      follows its phone (``store.resolve_device``) and gets a new token.

    Otherwise an adult's watch is not removed here: it never has more than
    its phone because the hub limits it at resolve time (reason
    ``limited_by_phone``).
    """
    for watch_id in removed:
        reason = (reasons or {}).get(watch_id)
        await _remove(
            db,
            watch_id,
            "watch_removed_hub_only"
            if reason == "hub_only"
            else "watch_removed_parental_controls",
            reason,
        )
    for watch_id in [*rescoped, *left]:
        await _rotate(db, watch_id)


def removal_reason(phone_rules: dict, child: bool) -> str | None:
    """Why a phone with these effective rules may carry no watch
    ($defs/watch_removed_reason), None when it may carry one."""
    watch = phone_rules.get("watch") or {}
    if child:
        if watch.get("value") is True:
            return None
        kids_watch = phone_rules.get("kids_watch") or {}
        if kids_watch.get("reason") == "requires_remote_access":
            return "remote_access_off"
        return "kids_watch_off"
    return "hub_only" if hub_only_removes_watch(watch) else None


# Bound of one removal (it also deletes the watch's sensor in HA).
REMOVE_SECONDS = 30.0


async def remove_disallowed(db: Database) -> list[str]:
    """Remove every active watch whose phone's effective rules no longer
    allow it (a missed profile write or expiry, an upgrade that moved the
    phone to Hub only). Runs once after the start in the background;
    idempotent (a removed watch is gone). One failing watch does not stop
    the others; until it is removed it gets no key (``resolver.ha_access``
    reads its phone's rules). Returns the removed watch ids."""
    from services.profiles import store

    removed = []
    rows = await db.fetchall(
        "SELECT id, platform, seal_key, set_up_by_client_id, set_up_method, "
        "is_active, pending_deletion_at FROM paired_clients "
        "WHERE seal_key IS NOT NULL AND set_up_by_client_id IS NOT NULL"
    )
    for row in rows:
        phone_id = row["set_up_by_client_id"]
        if not _served_by(row, phone_id):
            continue
        resolved = await store.resolve_client(db, phone_id)
        if not resolved:
            continue  # the phone's own removal takes its watches along
        phone, prof, rules = resolved
        if not phone.get("is_active") or phone.get("pending_deletion_at"):
            continue
        reason = removal_reason(rules, bool(prof["child_protection"]))
        if reason is None:
            continue
        logger.warning(
            "Watch %s: its phone may carry no watch (%s), removing it",
            row["id"],
            reason,
        )
        try:
            await asyncio.wait_for(
                _remove(
                    db,
                    row["id"],
                    "watch_removed_hub_only"
                    if reason == "hub_only"
                    else "watch_removed_parental_controls",
                    reason,
                ),
                REMOVE_SECONDS,
            )
        except Exception as e:  # noqa: BLE001 - the others go on; no key meanwhile
            logger.warning("Watch %s not removed yet: %s", row["id"], e)
            if not await _finish_removal(db, row["id"]):
                continue  # still there: the next run removes it
        removed.append(row["id"])
    return removed


async def _finish_removal(db: Database, watch_id: str) -> bool:
    """A removal cut after the row was deleted: send the deprovision now (the
    integration's reconcile removes the HA user as well when this does not
    arrive). True when the watch is gone."""
    from services import device_provisioning

    if await _row(db, watch_id) is not None:
        return False
    try:
        await device_provisioning.send_deprovision(
            watch_id, device_provisioning.DEPROVISION_DELETED
        )
    except Exception as e:  # noqa: BLE001 - the reconcile removes the user
        logger.warning("Deprovision of watch %s not sent: %s", watch_id, e)
    return True


async def phone_of(db: Database, client_id: str) -> str | None:
    """The phone that carries a watch's access (None: not a watch of a phone)."""
    row = await _row(db, client_id)
    if is_watch(row) and row.get("seal_key") and row.get("set_up_by_client_id"):
        return row["set_up_by_client_id"]
    return None


async def watches_of(db: Database, phone_id: str) -> list[str]:
    """Active watches set up by ``phone_id``."""
    rows = await db.fetchall(
        "SELECT id, platform, seal_key, set_up_by_client_id, set_up_method, "
        "is_active, pending_deletion_at FROM paired_clients "
        "WHERE set_up_by_client_id = ? AND seal_key IS NOT NULL",
        (phone_id,),
    )
    return [r["id"] for r in rows if _served_by(r, phone_id)]


async def carried_by(db: Database, phone_id: str) -> list[str]:
    """Every watch whose access ``phone_id`` carries, active or not: they go
    when the phone is removed or revoked."""
    rows = await db.fetchall(
        "SELECT id, platform, set_up_method FROM paired_clients "
        "WHERE set_up_by_client_id = ? AND id != ?",
        (phone_id, phone_id),
    )
    return [
        r["id"] for r in rows if is_watch(r) and r.get("set_up_method") == "onboard"
    ]


async def push(db: Database, ha_client, watch_ids: set[str] | None) -> int:
    """Send ``watch_access`` to the connected phones of these watches (None = all).

    Returns the number of phones reached.
    """
    from api import ws as api_ws

    connected = api_ws.get_connected_client_ids()
    if not connected:
        return 0
    rows = await db.fetchall(
        "SELECT id, platform, seal_key, set_up_by_client_id, set_up_method, "
        "is_active, pending_deletion_at FROM paired_clients "
        "WHERE seal_key IS NOT NULL AND set_up_by_client_id IS NOT NULL"
    )
    reached = 0
    for row in rows:
        # A watch follows a change of its own config or of its phone's (a
        # child's watch has the phone's rules and kids state).
        if (
            watch_ids is not None
            and row["id"] not in watch_ids
            and row["set_up_by_client_id"] not in watch_ids
        ):
            continue
        phone = row["set_up_by_client_id"]
        if phone not in connected or not _served_by(row, phone):
            continue
        try:
            answer = await message(db, ha_client, row["id"], phone)
        except Exception:  # noqa: BLE001 - one watch never stops the others
            logger.exception("watch_access of %s failed", row["id"])
            continue
        if answer["status"] == STATUS_GONE:
            continue
        if await api_ws.send_to_client(phone, answer):
            reached += 1
    return reached


async def _debounced_push() -> None:
    while _dirty:
        await asyncio.sleep(PUSH_DEBOUNCE_SECONDS)
        watch_ids: set[str] | None = (
            None if None in _dirty else {w for w in _dirty if w is not None}
        )
        _dirty.clear()
        db = _context.get("db")
        if db is None:
            return
        try:
            await push(db, _context.get("ha_client"), watch_ids)
        except Exception:  # noqa: BLE001 - the watch asks again after a 401
            logger.exception("watch_access push failed")


def notify(client_id: str | None = None) -> None:
    """The config of ``client_id`` (None = everyone) may have changed.

    Called for every ``client_config_updated``; only watches are pushed.
    """
    global _push_task
    if "db" not in _context:
        return
    _dirty.add(client_id)
    if _push_task is not None and not _push_task.done():
        return
    try:
        _push_task = asyncio.get_running_loop().create_task(_debounced_push())
    except RuntimeError:
        _push_task = None


async def phone_connected(db: Database, phone_id: str) -> None:
    """A phone authenticated: rotations of its watches that waited for it
    run now, and its watches get their current access."""
    from services import device_provisioning

    watches = await watches_of(db, phone_id)
    for watch_id in watches:
        await device_provisioning.release_deferred_rotation(db, watch_id)
        notify(watch_id)
