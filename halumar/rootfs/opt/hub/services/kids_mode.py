"""Kids mode: the kids rules of devices with parental controls.

Pure rule logic lives in :mod:`services.kids_rules`; the rules come from the
device's effective ``kids_*`` profile rules (``services.profiles``). This
module reads the database, talks to Home Assistant and records
audit/activity entries.

A device with parental controls is on the hub path (``services.hub_control``):
at home it switches through the hub, which checks the kids rules here and
calls the integration action ``<ns>.control`` with the device's own
(read-only) HA user and a single-use grant: the integration checks the
device's rule snapshot again, the hub does not count twice, and the HA
logbook still shows the device. When it holds a key (Checked, or remote
access) and the hub is not reachable it calls ``<ns>.control`` itself and the
integration asks ``kids-check``, which counts the action.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from models.database import Database
from services import hub_control, kids_rules
from services.audit_log import log_service_blocked, log_service_call

logger = logging.getLogger("hub.services.kids_mode")


OVERRIDE_MINUTES = (1, 240)

# One rule_blocked activity entry per (device, entity, reason) and minute.
ACTIVITY_THROTTLE_SECONDS = 60.0
_activity_last: dict[tuple[str, str, str], float] = {}

TZ_CACHE_SECONDS = 600.0
_tz_cache: tuple[float, str | None] | None = None
# The last zone HA ever reported (also from the settings after a restart):
# the fallback while HA does not answer, instead of UTC.
_tz_last_known: str | None = None
TZ_SETTING = "ha_time_zone"


def reset() -> None:
    """Forget caches and counters, also those of the hub path (tests)."""
    global _tz_cache, _tz_last_known
    _tz_cache = None
    _tz_last_known = None
    _activity_last.clear()
    kids_rules.counters.reset()
    hub_control.reset()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dumps(rules: dict[str, Any]) -> str:
    return json.dumps(rules, sort_keys=True, separators=(",", ":"))


# ── time zone ───────────────────────────────────────────────────────────────


def last_known_time_zone() -> str | None:
    """The last zone HA reported (memory or settings), None if never."""
    return _tz_last_known


def remember_time_zone(tz_name: str | None) -> None:
    """Take a zone read earlier (the setting :data:`TZ_SETTING`)."""
    global _tz_last_known
    if isinstance(tz_name, str) and tz_name:
        _tz_last_known = tz_name


async def hub_time_zone(ha_client) -> str | None:
    """HA's time zone (``/api/config``), cached; while HA does not answer the
    last zone it ever reported; ``None`` when no zone was ever read."""
    global _tz_cache
    now = time.monotonic()
    if _tz_cache and now - _tz_cache[0] < TZ_CACHE_SECONDS:
        return _tz_cache[1] or _tz_last_known
    tz_name = None
    if ha_client is not None:
        try:
            config = await ha_client.get_config()
            value = (config or {}).get("time_zone")
            tz_name = value if isinstance(value, str) and value else None
        except Exception as e:  # noqa: BLE001 - the fallback is logged
            logger.warning(
                "Could not read HA time zone (%s); using %s",
                e,
                (_tz_cache and _tz_cache[1]) or _tz_last_known or "none",
            )
            if _tz_cache:
                return _tz_cache[1] or _tz_last_known
            # Not cached: HA is still starting; ask again next time instead of
            # keeping the fallback for TZ_CACHE_SECONDS.
            return _tz_last_known
    _tz_cache = (now, tz_name)
    remember_time_zone(tz_name)
    return tz_name or _tz_last_known


# Reasons of <ns>.control (contract $defs/control_denial_reason) mapped to the
# kids answer of the hub (rule_blocked); the others are control_error
# reasons (hub_control.DENIAL_TO_ERROR).
_DENIAL_TO_BLOCKED = {
    "blocked_kids": kids_rules.REASON_BLOCKED,
    "blocked_kids_bedtime": kids_rules.REASON_QUIET_HOURS,
    "blocked_kids_cooldown": kids_rules.REASON_COOLDOWN,
    "blocked_kids_color": kids_rules.REASON_COLOR_LOCKED,
    "blocked_kids_volume": kids_rules.REASON_VOLUME_LIMIT,
    "blocked_kids_paused_all": kids_rules.REASON_PAUSED_ALL,
}
# ── device view ─────────────────────────────────────────────────────────────


def layers_of(effective: dict[str, dict]) -> dict[str, Any] | None:
    """Kids rules of a device (``kids_*`` rules resolved), None without
    parental controls. Shape of ``client_config.kids.rules``."""
    if effective.get("kids_blocked_types", {}).get("source") in (
        None,
        "not_applicable",
    ):
        return None
    from api.ws.client import kids_view

    view = kids_view(effective)
    return {
        "rules": view["profile"],
        "areas": view["areas"],
        "entities": view["entities"],
        # Pause everything (profile level only), None = off.
        "rate_limit_all": kids_rules.rate_limit_all(
            (effective.get("kids_rate_limit_all") or {}).get("value")
        ),
    }


def kids_subject(row: dict) -> str:
    """Whose kids state (counters, cooldowns, pause everything, the action
    lock, kids override, releases) a device uses: a child's watch uses its
    phone's (``store.follow_phone``). The one place
    the hub's own control path and ``kids-check`` take it from."""
    from services.profiles import store

    return store.kids_subject(row)


# ── decision for one device ────────────────────────────────────────────────


async def decide(
    db: Database,
    ha_client,
    *,
    profile: dict[str, Any],
    client_id: str | None,
    entity_id: str,
    service: str,
    data: dict[str, Any],
    area_ids: list[str],
    override_until: datetime | None,
    now: datetime | None = None,
    state: dict | None | object = None,
    use_counters: bool = True,
    releases: kids_rules.Releases = kids_rules.NO_RELEASES,
) -> kids_rules.Decision:
    """``state``: the entity's state, ``hub_control.STATE_UNKNOWN`` = read already,
    not available (rules fail closed); None = read it here."""
    now = now or utcnow()
    # Every await happens before the counters are read: the caller records
    # an allowed action right after this returns, without an await in
    # between, so parallel requests of one device cannot pass the limits
    # on the same counter reading.
    tz_name = await hub_time_zone(ha_client)
    if state is hub_control.STATE_UNKNOWN:
        state = None
    elif state is None:
        state = await hub_control.read_state(ha_client, entity_id)
    counters = kids_rules.counters
    counting = use_counters and bool(client_id)
    # Pause everything, unless a parent release or a pause release runs.
    limit_all = (
        profile.get("rate_limit_all")
        if counting
        and not releases.rate
        and not (override_until is not None and override_until > now)
        else None
    )
    if limit_all is not None:
        running = kids_rules.evaluate_all(
            limit_all,
            entity_id=entity_id,
            now=now,
            paused_until=counters.paused_all(client_id, now),
        )
        if running is not None:
            return running
    decision = kids_rules.evaluate(
        profile["rules"],
        [profile["areas"].get(a) for a in area_ids],
        profile["entities"].get(entity_id),
        entity_id=entity_id,
        service=service,
        data=data,
        state=state,
        now=now,
        tz_name=tz_name,
        recent=counters.recent(client_id, entity_id)
        if use_counters and client_id
        else (),
        cooldown_until=(
            counters.cooldown(client_id, entity_id, now)
            if use_counters and client_id
            else None
        ),
        override_until=override_until,
        releases=releases,
    )
    if decision.allowed and limit_all is not None:
        started = kids_rules.evaluate_all(
            limit_all,
            entity_id=entity_id,
            now=now,
            recent=counters.device_recent(client_id),
        )
        if started is not None:
            return started
    return decision


async def pause_all_started(
    db: Database, ha_client, client_id: str, entity_id: str, action: str, decision
) -> None:
    """A decision started the pause of everything: remember it, log
    ``kids_paused_all`` once (log, ``<ns>_event``, notifications at events)
    and tell the child's app and the admin/family views."""
    until = decision.start_pause_all_until
    kids_rules.counters.start_pause_all(client_id, until)
    info = decision.pause_all or {}
    logger.info(
        "Kids: everything of %s paused until %s (%s)",
        client_id,
        iso(until),
        info.get("trigger"),
    )
    from services import event_log

    await event_log.kids_paused_all(
        db,
        client_id,
        entity_id=entity_id,
        action=action,
        until=iso(until),
        info={**info, "window_seconds": (decision.limit or {}).get("window_seconds")},
    )
    from services.family import service as family

    await family.config_changed(client_id)
    try:
        await family.child_updated(db, ha_client, client_id)
    except Exception as e:  # noqa: BLE001 - family views refresh on their own
        logger.debug("family status after pause-all failed: %s", e)


def blocked_message(
    request_id: str | None, entity_id: str, decision: kids_rules.Decision, now: datetime
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "type": "rule_blocked",
        "request_id": request_id,
        "entity_id": entity_id,
        "reason": decision.reason,
        "level": decision.level,
        "until": iso(decision.until),
        "retry_after": decision.retry_after(now),
    }
    if decision.limit:
        msg["limit"] = decision.limit
    return msg


async def _record_blocked(
    db: Database, client_id: str, entity_id: str, service: str, decision
) -> None:
    domain = entity_id.split(".", 1)[0]
    log_service_blocked(domain, service, client_id, f"{decision.reason}:{entity_id}")
    key = (client_id, entity_id, decision.reason or "")
    now = time.monotonic()
    last = _activity_last.get(key)
    if last is not None and now - last < ACTIVITY_THROTTLE_SECONDS:
        return
    _activity_last[key] = now
    if len(_activity_last) > 5000:
        _activity_last.clear()
    from services import client_activity

    await client_activity.record(
        db,
        client_id,
        client_activity.RULE_BLOCKED,
        {
            "entity_id": entity_id,
            "blocked_reason": decision.reason,
            "level": decision.level,
            "until": iso(decision.until),
        },
    )


async def decide_and_run(
    db: Database,
    ha_client,
    client_id: str,
    client: dict,
    profile: dict[str, Any],
    area_ids: list[str],
    request_id: str | None,
    entity_id: str,
    service: str,
    data: dict[str, Any],
    state: dict | object,
) -> dict[str, Any]:
    """The kids part of ``hub_control.control`` (under the device's action
    lock): decide, count, call ``<ns>.control`` with a grant, record."""
    from services.family import events as family_events
    from services.family import service as family

    subject = client.get("kids_subject") or client_id
    now = utcnow()
    decision = await decide(
        db,
        ha_client,
        profile=profile,
        client_id=subject,
        entity_id=entity_id,
        service=service,
        data=data,
        area_ids=area_ids,
        override_until=parse_iso(client.get("kids_override_until")),
        now=now,
        state=state,
        releases=await family.releases_for_action(db, subject, entity_id),
    )
    if not decision.allowed:
        if decision.start_cooldown_until:
            kids_rules.counters.start_cooldown(
                subject, entity_id, decision.start_cooldown_until
            )
        await _record_blocked(db, client_id, entity_id, service, decision)
        if decision.start_pause_all_until:
            # One log entry for the attempt that starts the pause.
            await pause_all_started(
                db, ha_client, subject, entity_id, service, decision
            )
            return blocked_message(request_id, entity_id, decision, now)
        from services import event_log

        await event_log.hub_control(
            db,
            client_id,
            entity_id=entity_id,
            action=service,
            ok=False,
            reason=decision.reason,
            until=iso(decision.until),
            pause_start=decision.start_cooldown_until is not None,
        )
        await family_events.kids_blocked(
            db,
            client_id,
            entity_id,
            service,
            decision.reason,
            iso(decision.until),
            pause_started=decision.start_cooldown_until is not None,
        )
        return blocked_message(request_id, entity_id, decision, now)

    # Reserve the slot before the first await (HA round trip): the
    # integration's kids-check sees it; a call that fails or is cancelled
    # gives it back (rejected attempts do not count).
    kids_rules.counters.record(subject, entity_id, now)
    ran = False
    try:
        send_service = decision.service or service
        payload = dict(decision.data if decision.data is not None else data)
        from services import enforcement

        if not await hub_control.device_token(db, client_id):
            return hub_control.error_message(request_id, hub_control.ERR_NOT_READY)
        grant = enforcement.issue_grant(client_id, entity_id, send_service)
        try:
            result = await hub_control.run_as_device(
                db, ha_client, client_id, entity_id, send_service, payload, grant
            )
        except hub_control.ControlFailure as e:
            return hub_control.error_message(request_id, e.reason)
        except hub_control.ControlDenied as e:
            from services import event_log

            blocked = _DENIAL_TO_BLOCKED.get(e.reason)
            # The integration reports this call as well; the log keeps the
            # hub's entry for devices with parental controls (one entry).
            await event_log.hub_control(
                db,
                client_id,
                entity_id=entity_id,
                action=send_service,
                ok=False,
                reason=blocked or e.reason,
                until=e.until,
            )
            if blocked is None:
                return hub_control.error_message(
                    request_id,
                    hub_control.DENIAL_TO_ERROR.get(
                        e.reason, hub_control.ERR_INVALID_REQUEST
                    ),
                )
            decision = kids_rules.Decision(
                False, blocked, kids_rules.LEVEL_PROFILE, until=parse_iso(e.until)
            )
            await _record_blocked(db, client_id, entity_id, service, decision)
            return blocked_message(request_id, entity_id, decision, now)
        ran = True
    finally:
        if not ran:
            kids_rules.counters.forget(subject, entity_id, now)
    domain = entity_id.split(".", 1)[0]
    if ha_client is not None:
        try:
            ha_client.invalidate_state_cache(entity_id)
        except Exception:  # noqa: BLE001
            pass
    log_service_call(domain, send_service, client_id, entity_id)
    from services import event_log

    await event_log.hub_control(
        db, client_id, entity_id=entity_id, action=send_service, ok=True
    )
    await family_events.kids_control(db, client_id, entity_id, send_service)
    answer: dict[str, Any] = {
        "type": "control_ok",
        "request_id": request_id,
        "entity_id": entity_id,
    }
    adjusted = (result or {}).get("adjusted") or decision.adjusted
    if adjusted:
        answer["adjusted"] = adjusted
    return answer


# ── device profile, override ────────────────────────────────────────────────


async def bedtime_release_end(
    db: Database, ha_client, client_id: str, now: datetime | None = None
) -> datetime:
    """End of a parent release "until tomorrow morning".

    The end of the device's current or next bedtime (rule
    ``kids_quiet_hours``); without bedtime 07:00 local like the
    adjustment preset ``tomorrow_morning``.
    """
    from services.profiles import resolver as res
    from services.profiles import store

    now = now or utcnow()
    tz_name = await hub_time_zone(ha_client)
    windows = None
    resolved = await store.resolve_client(db, client_id)
    if resolved:
        layers = layers_of(resolved[2])
        windows = (layers or {}).get("rules", {}).get("quiet_hours")
    end = kids_rules.bedtime_end(windows, now, tz_name)
    if end is None:
        end = res.expiry("tomorrow_morning", iso(now), tz_name or "UTC")
    return end.astimezone(timezone.utc)


async def kids_status(db: Database, client_id: str) -> dict[str, Any]:
    row = await db.fetchone(
        "SELECT c.profile_id, c.kids_override_until, p.child_protection "
        "FROM paired_clients c JOIN profiles p ON p.id = c.profile_id WHERE c.id = ?",
        (client_id,),
    )
    now = utcnow()
    override = parse_iso((row or {}).get("kids_override_until"))
    if override and override <= now:
        override = None
    cooldowns = kids_rules.counters.cooldowns_of(client_id, now)
    return {
        "client_id": client_id,
        "profile_id": (row or {}).get("profile_id")
        if (row or {}).get("child_protection")
        else None,
        "override_until": iso(override),
        "paused_all_until": iso(kids_rules.counters.paused_all(client_id, now)),
        "cooldowns": [
            {"entity_id": eid, "until": iso(until)}
            for eid, until in sorted(cooldowns.items())
        ],
    }


async def set_override(
    db: Database,
    client_id: str,
    minutes: int | None,
    *,
    until_at: datetime | None = None,
) -> None:
    """Release for ``minutes`` or until ``until_at``; both ``None`` ends it."""
    until = None
    if until_at is not None:
        until = iso(until_at)
    elif minutes:
        until = iso(utcnow() + timedelta(minutes=minutes))
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET kids_override_until = ? WHERE id = ?",
            (until, client_id),
        )
