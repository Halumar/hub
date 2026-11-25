"""Temporary device adjustments end on time.

The planner deletes every adjustment whose ``expires_at`` passed (audit
``override_expired``, author = the hub) and applies the same effects as an
admin's delete: a new HA key when a device stops holding one,
``update_client_identity`` when remote access changes and
``client_config_updated`` to the devices. After a restart it catches up at once.
Apps switch to ``then`` at ``expires_at`` on their own while offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from brand import BRAND_NAME
from models.database import Database
from services.profiles import resolver as res

logger = logging.getLogger("hub.profiles.expiry")

MAX_SLEEP_SECONDS = 60.0


def _end_of(r: dict, now: datetime) -> datetime:
    """End of an adjustment; unparseable: ends now (fail closed)."""
    try:
        return res.parse_ts(r["expires_at"]) or now
    except ValueError:
        return now


def _before(r: dict) -> dict:
    return {"value": json.loads(r["value_json"]), "expires_at": r["expires_at"]}


_locks: dict[int, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    """One end of adjustments at a time (the planner and the pre-step of a
    write never end the same adjustment twice)."""
    loop = id(asyncio.get_running_loop())
    if loop not in _locks:
        _locks.clear()
        _locks[loop] = asyncio.Lock()
    return _locks[loop]


async def _due(
    db: Database, now: datetime, client_ids: list[str] | None = None
) -> list[dict]:
    rows = await db.fetchall(
        "SELECT client_id, rule_key, value_json, expires_at FROM device_overrides "
        "WHERE expires_at IS NOT NULL"
    )
    return [
        r
        for r in rows
        if _end_of(r, now) <= now
        and (client_ids is None or r["client_id"] in client_ids)
    ]


async def before_unprocessed_end(db: Database) -> str | None:
    """The moment just before the earliest end the planner has not processed
    yet, None when there is none (the "before" of a dry run)."""
    from services.profiles import store

    now = store.utcnow()
    due = await _due(db, now)
    if not due:
        return None
    return res.fmt_ts(min(_end_of(r, now) for r in due) - timedelta(seconds=1))


async def expire_due(
    db: Database, now: datetime | None = None, client_ids: list[str] | None = None
) -> list[str]:
    """Delete the ended adjustments (of ``client_ids``, else of every
    device); returns the affected device ids.

    Runs through the same effects as an admin's delete
    (``changes.apply_effects``): a device that stops holding a key (the end
    of a temporary Checked, Direct or remote access on Hub only) gets a new
    HA key, a watch its phone may no longer carry is removed, the
    integration and the devices hear of it. Each adjustment ends once: one
    deletion at a time under a lock, and only a row this run deleted is
    audited; the effects run after the lock is released.
    """
    from services.profiles import changes, store
    from services.profiles import rows as profile_rows

    async with _lock():
        # The resolver's clock: what has ended is what it no longer applies.
        now = now or store.utcnow()
        due = await _due(db, now, client_ids)
        if not due:
            return []
        ended: list[dict] = []

        async def _delete() -> None:
            ended.clear()
            for r in due:
                cursor = await db.execute(
                    "DELETE FROM device_overrides WHERE client_id = ? "
                    "AND rule_key = ? AND expires_at = ?",
                    (r["client_id"], r["rule_key"], r["expires_at"]),
                )
                if not cursor.rowcount:
                    continue  # ended by another write already
                ended.append(r)
                # In the same transaction: after a crash before the effects,
                # the key catch-up (secure_keyless_tokens) still finds the end.
                await profile_rows.audit(
                    db,
                    "override_expired",
                    now=store.now_iso(),
                    actor=BRAND_NAME,
                    client_id=r["client_id"],
                    rule_key=r["rule_key"],
                    before=_before(r),
                )

        # The state before is the one just before the earliest end (also when
        # the hub was down at the end or the clock jumped): what each device
        # held while its adjustment ran is what the diff takes away now.
        first_end = min(_end_of(r, now) for r in due)
        before_now = res.fmt_ts(first_end - timedelta(seconds=1))
        _, change = await changes.run(db, _delete, dry_run=False, before_now=before_now)
    # Outside the lock: the effects reach the integration, Home Assistant and
    # the devices; each adjustment was deleted (and audited) once above.
    if not ended:
        return []
    clients = sorted({r["client_id"] for r in ended})
    for r in ended:
        await changes.audit(
            db,
            "override_expired",
            actor=BRAND_NAME,
            client_id=r["client_id"],
            rule_key=r["rule_key"],
            before=_before(r),
            stored=True,
        )
    await changes.apply_effects(db, change, touched=clients)
    logger.info(
        "%d temporary adjustment(s) ended on %d device(s)", len(ended), len(clients)
    )
    return clients


async def expire_due_isolated(db: Database) -> None:
    """The pre-step of a write: end every due adjustment; a failure is
    isolated per device. A device whose end fails keeps no key it may have
    lost (its token is rotated) and is tried again by the next write or the
    planner; the write itself goes on."""
    from services.profiles import store

    try:
        await expire_due(db)
        return
    except Exception as e:  # noqa: BLE001 - one device at a time below
        logger.warning("Ending temporary adjustments failed, per device: %s", e)
    for client_id in sorted({r["client_id"] for r in await _due(db, store.utcnow())}):
        try:
            await expire_due(db, client_ids=[client_id])
        except Exception as e:  # noqa: BLE001 - fail closed for this device
            logger.error(
                "Ending the adjustments of %s failed (%s); rotating its token",
                client_id,
                type(e).__name__,
            )
            await _withdraw(db, client_id)


async def _withdraw(db: Database, client_id: str) -> None:
    from api import ws as api_ws
    from services import device_provisioning

    try:
        row = await db.fetchone(
            "SELECT id, is_active, pending_deletion_at, ha_token_status "
            "FROM paired_clients WHERE id = ?",
            (client_id,),
        )
        if (
            device_provisioning.is_provisionable(row)
            and row.get("ha_token_status") == "active"
            and not await device_provisioning.rotation_pending(db, client_id)
        ):
            await device_provisioning.withdraw_device_key(
                db, client_id, send=api_ws.get_integration_push_connected()
            )
    except Exception as e:  # noqa: BLE001 - the catch-up and the next run retry
        logger.warning("Token of %s not rotated: %s", client_id, e)


async def _next_end(db: Database) -> datetime | None:
    row = await db.fetchone(
        "SELECT expires_at FROM device_overrides WHERE expires_at IS NOT NULL "
        "ORDER BY expires_at LIMIT 1"
    )
    if not row:
        return None
    try:
        return res.parse_ts(row["expires_at"])
    except ValueError:
        return datetime.now(timezone.utc)


async def expiry_loop(app) -> None:
    """Supervised task: end adjustments on time (catches up after a restart)."""
    db: Database = app.state.db
    while True:
        try:
            await expire_due(db)
            nxt = await _next_end(db)
        except Exception as e:  # noqa: BLE001 - the supervisor restarts on crash only
            logger.error("Ending temporary adjustments failed: %s", e)
            nxt = None
        delay = MAX_SLEEP_SECONDS
        if nxt is not None:
            delay = min(
                MAX_SLEEP_SECONDS,
                max(0.5, (nxt - datetime.now(timezone.utc)).total_seconds()),
            )
        await asyncio.sleep(delay)
