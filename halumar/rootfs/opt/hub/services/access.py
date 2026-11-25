"""Access hours and access switch of a device (contract docs/access.md).

Access = the effective rule ``access_hours`` allows now (when enabled) AND the
device's access switch is on (when it has one). A device has a switch when its
access hours are enabled or its profile is of kind guest, house_sitter or
babysitter; the state is ``paired_clients.access_switch`` (default on), shown
in Home Assistant as ``switch.<ns>_<name>_access``.

Outside its access a device stays paired but sees nothing: the hub's read
scope is empty (``api/authz.py``), ``client_config`` carries no HA token, the
hub control path, family and setting up refuse. The integration decides
``<ns>.control`` with the snapshot field ``access`` and its own clock.

The planner (:func:`access_loop`) notices every change (window start/end,
switch, rule), logs ``access_opened`` / ``access_closed``, pushes
``client_config_updated`` and ends the releases a babysitter gave when its
access closes. It also tells the children when the active babysitters change
(``kids.requests.recipients``); connects and disconnects wake it. The last logged state is ``paired_clients.access_state`` so a
restart does not log again. Reference: contract ``tools/access_ref.py``.

Early access: a Yes to the device's request (``services/access_requests``)
stores ``paired_clients.access_early_json`` ``{until, by, request_id, duration,
at}``. Until then the windows do not close the device (the switch still does);
a window that starts before it ends continues it. The planner deletes it at
``until`` and wakes at that edge like at a window edge.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from models.database import Database

logger = logging.getLogger("hub.access")

SWITCH_KINDS = frozenset({"guest", "house_sitter", "babysitter"})
MAX_SLEEP_SECONDS = 60.0
NEXT_OPEN_DAYS = 8

_wake: asyncio.Event | None = None
# HA client of the running app (HA notifications of early access requests).
_ha_client = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def time_zone() -> str | None:
    """HA's time zone as last read by ``kids_mode.hub_time_zone``, else the
    last one it ever reported; None when no zone was ever read."""
    from services import kids_mode

    cached = getattr(kids_mode, "_tz_cache", None)
    return (cached[1] if cached else None) or kids_mode.last_known_time_zone()


def client_time_zone() -> str:
    """The zone the devices get as ``client_config.access.time_zone``: HA's
    zone, ``UTC`` while none (or an unknown one) is known. The apps compute
    every edge of the access windows in it, not in the device's zone."""
    tz_name = time_zone()
    if tz_name:
        try:
            ZoneInfo(tz_name)
            return tz_name
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "UTC"


def hours_of(rules: dict[str, dict]) -> dict[str, Any]:
    """Effective ``access_hours`` value ({} when the rule does not apply)."""
    value = (rules.get("access_hours") or {}).get("value")
    return value if isinstance(value, dict) else {}


def schedule_enabled(rules: dict[str, dict]) -> bool:
    return bool(hours_of(rules).get("enabled"))


def windows_of(rules: dict[str, dict]) -> list[dict]:
    hours = hours_of(rules)
    if not hours.get("enabled"):
        return []
    return [w for w in hours.get("windows") or [] if isinstance(w, dict)]


def has_switch(profile_kind: str | None, rules: dict[str, dict]) -> bool:
    return (profile_kind or "person") in SWITCH_KINDS or schedule_enabled(rules)


def switch_state(client: dict, profile_kind: str | None, rules: dict) -> bool | None:
    if not has_switch(profile_kind, rules):
        return None
    value = client.get("access_switch")
    return True if value is None else bool(value)


def parse_ts(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def early_of(client: dict) -> dict | None:
    """Stored early access of a device, running or not."""
    raw = client.get("access_early_json")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or parse_ts(value.get("until")) is None:
        return None
    return value


def running_early(client: dict, now: datetime) -> dict | None:
    early = early_of(client)
    if early is None:
        return None
    until = parse_ts(early.get("until"))
    return early if until is not None and until > now else None


def open_until(
    windows: list[dict],
    now: datetime,
    tz_name: str | None,
    early_until: datetime | None = None,
) -> datetime | None:
    """End of the access ``now`` is in: windows followed through touching
    ones, plus a running early access as one more span from now."""
    from services import kids_rules

    if early_until is None or early_until <= now:
        return kids_rules.quiet_until(windows, now, tz_name) if windows else None
    point, until = now, None
    for _ in range(64):
        ends = []
        w = kids_rules.quiet_until(windows, point, tz_name) if windows else None
        if w is not None:
            ends.append(w)
        if point < early_until:
            ends.append(early_until)
        if not ends or max(ends) <= point:
            break
        point = until = max(ends)
    return until


def next_open(
    windows: list[dict], now: datetime, tz_name: str | None
) -> datetime | None:
    from services import kids_rules

    return kids_rules.next_quiet_start(windows, now, tz_name) if windows else None


def compute(
    *,
    schedule: bool,
    windows: list[dict],
    switch: bool | None,
    now: datetime,
    tz_name: str | None,
    early: dict | None = None,
) -> dict[str, Any]:
    """``$defs/access_now`` (same as the contract's ``access_ref.access_now``)."""
    running = early if early and (parse_ts(early.get("until")) or now) > now else None
    early_until = parse_ts(running["until"]) if running else None
    until = open_until(windows, now, tz_name, early_until) if schedule else None
    schedule_ok = (not schedule) or until is not None
    switch_ok = switch is not False
    opens_at = None
    if schedule_ok and switch_ok:
        reason = None
    elif not switch_ok:
        reason, until = "switch", None
    else:
        reason, until = "schedule", None
        opens_at = next_open(windows, now, tz_name)
    out: dict[str, Any] = {
        "open": schedule_ok and switch_ok,
        "reason": reason,
        "until": iso(until),
        "opens_at": iso(opens_at),
        "schedule": schedule,
        "switch": switch,
    }
    if early is not None:
        out["early"] = running
    return out


def state_of(
    client: dict,
    profile: dict,
    rules: dict[str, dict],
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> dict[str, Any]:
    """Access of one resolved device right now.

    Access hours without a known time zone (HA never reported one) close
    the device instead of counting the windows in UTC; a running
    early access still opens it (absolute end).
    """
    kind = profile.get("kind") or "person"
    tz = tz_name if tz_name is not None else time_zone()
    schedule = schedule_enabled(rules)
    return compute(
        schedule=schedule,
        windows=windows_of(rules) if tz is not None else [],
        switch=switch_state(client, kind, rules),
        now=now or utcnow(),
        tz_name=tz,
        early=early_of(client),
    )


def is_open(client: dict, profile: dict, rules: dict[str, dict]) -> bool:
    return bool(state_of(client, profile, rules)["open"])


async def client_state(db: Database, client_id: str) -> dict[str, Any] | None:
    from services.profiles import store

    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        return None
    client, prof, rules = resolved
    return state_of(client, prof, rules)


async def client_open(db: Database, client_id: str | None) -> bool:
    """Whether a paired device has access now (unknown devices: False)."""
    if not client_id:
        return False
    state = await client_state(db, client_id)
    return bool(state and state["open"])


async def access_map(
    db: Database, client_ids: list[str], resolved: dict[str, dict] | None = None
) -> dict[str, dict]:
    """client_id -> access now for many devices (admin list, integration
    status); ``resolved``: their ``store.resolve_all`` items, when the
    caller has them."""
    from services.profiles import store

    if not client_ids:
        return {}
    tz_name = time_zone()
    now = utcnow()
    if resolved is None:
        resolved = await store.resolve_all(db, client_ids)
    return {
        cid: state_of(
            item["client"], item["profile"], item["rules"], now=now, tz_name=tz_name
        )
        for cid, item in resolved.items()
        if cid in client_ids
    }


def snapshot_access(
    profile: dict,
    rules: dict[str, dict],
    client: dict,
    tz_name: str | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Snapshot field ``access`` (``$defs/enforcement_access``) or None."""
    kind = profile.get("kind") or "person"
    if not has_switch(kind, rules):
        return None
    schedule = schedule_enabled(rules)
    early = running_early(client, now or utcnow()) if schedule else None
    return {
        "switch": switch_state(client, kind, rules) is not False,
        "hours": schedule,
        "windows": windows_of(rules) if schedule else [],
        "time_zone": tz_name,
        "early_until": iso(parse_ts(early["until"])) if early else None,
    }


# ── switch ──────────────────────────────────────────────────────────────────


class SwitchError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


async def set_switch(
    db: Database, client_id: str, on: bool, *, by: str, actor: str | None
) -> dict[str, Any]:
    """Turn a device's access switch on or off (admin UI or Home Assistant).

    Returns the new access state; raises :class:`SwitchError` (404 unknown,
    409 no_access_switch)."""
    from services import enforcement
    from services.profiles import changes, store

    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        raise SwitchError(404, "not_found")
    client, prof, rules = resolved
    if not has_switch(prof.get("kind"), rules):
        raise SwitchError(409, "no_access_switch")
    before = switch_state(client, prof.get("kind"), rules)
    if before != on:
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET access_switch = ? WHERE id = ?",
                (1 if on else 0, client_id),
            )
        await changes.audit(
            db,
            "access_switch_set",
            actor=actor or ("Home Assistant" if by == "ha" else None),
            client_id=client_id,
            before={"on": before},
            after={"on": on, "by": by},
        )
        enforcement.notify_change("access_switch")
    await check_transitions(db, only=[client_id], by=by)
    state = await client_state(db, client_id)
    return state or {}


# ── planner ─────────────────────────────────────────────────────────────────


def wake() -> None:
    """Re-check every device now (rules, profiles or switches changed)."""
    if _wake is not None:
        _wake.set()


async def _end_babysitter_releases(db: Database, client_id: str | None) -> list[str]:
    """Releases a babysitter gave end when its access closes (docs/access.md).

    ``client_id`` None: those of every babysitter device that is no longer
    paired and active (removed, expired, revoked).
    """
    import json

    rows = await db.fetchall("SELECT id, client_id, by_json FROM kids_releases")
    present = None
    if client_id is None:
        present = {
            r["id"]
            for r in await db.fetchall(
                "SELECT id FROM paired_clients WHERE is_active = 1 "
                "AND pending_deletion_at IS NULL"
            )
        }
    ended: list[str] = []
    for r in rows:
        try:
            by = json.loads(r["by_json"] or "{}")
        except ValueError:
            continue
        if by.get("kind") != "babysitter":
            continue
        if (present is None and by.get("client_id") == client_id) or (
            present is not None and by.get("client_id") not in present
        ):
            ended.append(r["client_id"])
            async with db.transaction():
                await db.execute("DELETE FROM kids_releases WHERE id = ?", (r["id"],))
    return sorted(set(ended))


async def _tell_released(db: Database, children: list[str]) -> None:
    """Children whose babysitter releases ended: config, family views."""
    from api import ws as api_ws
    from services.family import service as family

    for child_id in children:
        try:
            await api_ws.send_to_client(
                child_id, {"type": "client_config_updated", "data": {}}
            )
        except Exception as e:  # noqa: BLE001 - best effort
            logger.debug("client_config_updated to %s failed: %s", child_id, e)
    await family.push_rules(db, None)
    for child_id in children:
        await family.admin_changed([child_id])


async def end_releases_of_removed(db: Database, client_id: str) -> list[str]:
    """A babysitter device is removed: the releases it gave end now, as at
    the close of its access. Returns the children concerned."""
    children = await _end_babysitter_releases(db, client_id)
    if children:
        await _tell_released(db, children)
        wake()  # the children name who gets their requests
    return children


async def _log(
    db: Database, client_id: str, state: dict[str, Any], by: str | None
) -> None:
    from services import event_log

    kind = "access_opened" if state["open"] else "access_closed"
    early = state.get("early") if state["open"] else None
    await event_log.record(
        db,
        category=event_log.CHANGE,
        kind=kind,
        event_kind=kind,
        client_id=client_id,
        until=state["until"] if state["open"] else state["opens_at"],
        detail={
            "reason": state["reason"] or ("switch" if by else "schedule"),
            "until": state["until"],
            "opens_at": state["opens_at"],
            "by": by,
            # Opened early by a Yes to the device's request.
            "early": {
                "until": early["until"],
                "by": (early.get("by") or {}).get("name"),
            }
            if early
            else None,
        },
    )


async def _withdraw_key(db: Database, client_id: str) -> None:
    """The HA token the app held stops working when access closes."""
    from api import ws as api_ws
    from services import device_provisioning

    try:
        await device_provisioning.withdraw_closed_key(
            db, client_id, send=api_ws.get_integration_push_connected()
        )
    except Exception as e:  # noqa: BLE001 - the retry loop and the next close retry
        logger.warning("Access closed: key of %s not withdrawn: %s", client_id, e)


async def check_transitions(
    db: Database,
    *,
    now: datetime | None = None,
    only: list[str] | None = None,
    by: str | None = None,
) -> datetime | None:
    """Compare every device's access with the last logged state; act on
    changes. Returns the next time something can change (window edge)."""
    from api import ws as api_ws
    from services import enforcement, quick_access
    from services.profiles import store

    now = now or utcnow()
    tz_name = time_zone()
    nxt: datetime | None = None
    if only is None:
        # Expire requests, cancel pointless ones, drop ended early access.
        from services import access_requests

        try:
            nxt = await access_requests.tick(db, _ha_client)
        except Exception as e:  # noqa: BLE001 - the next run retries
            logger.warning("Early access check failed: %s", e)
    resolved = await store.resolve_all(db, only)
    changed: list[str] = []
    released_children: set[str] = set()
    if only is None:
        released_children.update(await _end_babysitter_releases(db, None))
    for client_id, item in resolved.items():
        client = item["client"]
        if not client.get("is_active") or client.get("pending_deletion_at"):
            continue
        if store.is_child_watch(client):
            # A child's watch has its phone's access: the phone's change is
            # logged once and pushes the watch's access with it.
            continue
        state = state_of(
            client, item["profile"], item["rules"], now=now, tz_name=tz_name
        )
        early = state.get("early")
        for edge in (state["until"], state["opens_at"], early and early["until"]):
            if edge:
                t = datetime.fromisoformat(edge.replace("Z", "+00:00"))
                nxt = t if nxt is None or t < nxt else nxt
        stored = client.get("access_state")
        current = "open" if state["open"] else "closed"
        if stored == current:
            continue
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET access_state = ? WHERE id = ?",
                (current, client_id),
            )
        # First evaluation of an open device (new device, update): nothing to log.
        if stored is None and state["open"]:
            continue
        changed.append(client_id)
        await _log(db, client_id, state, by if (only and client_id in only) else None)
        if not state["open"]:
            released_children.update(await _end_babysitter_releases(db, client_id))
            # A device that holds a key (also a child with a read-only key)
            # loses the one its app knew; one on the hub path without a key
            # has none on the device.
            from services.profiles import resolver as res

            if res.holds_key(item["rules"]):
                await _withdraw_key(db, client_id)
    for client_id in changed + sorted(released_children):
        try:
            await api_ws.send_to_client(
                client_id, {"type": "client_config_updated", "data": {}}
            )
        except Exception as e:  # noqa: BLE001 - best effort
            logger.debug("client_config_updated to %s failed: %s", client_id, e)
    if changed:
        from services.profiles import changes as pchanges

        await pchanges.admin_broadcast([], changed)
        quick_access.notify_change("profiles_changed")
        enforcement.notify_change("access")
        logger.info("access changed on %d device(s)", len(changed))
    from services.family import service as family

    if released_children:
        await family.push_rules(db, None)
        for child_id in released_children:
            await family.admin_changed([child_id])
    # Children name who gets their requests, active babysitters included.
    await family.babysitters_changed(db)
    return nxt


async def access_loop(app) -> None:
    """Supervised task: act on access changes on time (catches up after a restart)."""
    global _wake, _ha_client
    from services import kids_mode

    db: Database = app.state.db
    _ha_client = getattr(app.state, "ha_client", None)
    _wake = asyncio.Event()
    stored_tz = None
    try:
        stored_tz = await db.get_setting(kids_mode.TZ_SETTING)
        kids_mode.remember_time_zone(stored_tz)
    except Exception as e:  # noqa: BLE001 - read again from HA below
        logger.warning("Stored time zone not read: %s", e)
    while True:
        nxt = None
        # Cleared before the check: a wake() during it runs the loop again
        # instead of being lost until the next window edge.
        _wake.clear()
        try:
            tz = await kids_mode.hub_time_zone(getattr(app.state, "ha_client", None))
            if tz and tz != stored_tz:
                # Kept for a restart while HA does not answer.
                await db.set_setting(kids_mode.TZ_SETTING, tz)
                stored_tz = tz
            nxt = await check_transitions(db)
        except Exception as e:  # noqa: BLE001 - the supervisor restarts on crash only
            logger.error("Access check failed: %s", e)
        delay = MAX_SLEEP_SECONDS
        if nxt is not None:
            delay = min(
                MAX_SLEEP_SECONDS, max(0.5, (nxt - utcnow()).total_seconds() + 0.5)
            )
        try:
            await asyncio.wait_for(_wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


def reset_state() -> None:
    global _wake, _ha_client
    _wake = None
    _ha_client = None


__all__ = [
    "SWITCH_KINDS",
    "SwitchError",
    "access_loop",
    "check_transitions",
    "client_open",
    "client_state",
    "compute",
    "has_switch",
    "is_open",
    "set_switch",
    "snapshot_access",
    "state_of",
    "time_zone",
    "wake",
]
