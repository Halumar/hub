"""Persistent log of the admin UI tab "Log".

One table ``event_log`` with three categories:

- ``activity``: who switched what (``<ns>.control`` of the integration, the
  hub's control path of devices with parental controls, and for devices that
  switch directly in Home Assistant the HA ``call_service`` event mapped to
  the device by its HA user), single rejections ("not carried out");
- ``change``: the profile audit, pairing, removal, guests, parent releases,
  integration starts;
- ``alarm``: rejections from a threshold on (default 3 in 10 minutes per
  device), access alarms reported by the integration, integration outages.

Entries are kept ``retention_days`` (7, 30 or 90; default 30) and deleted
every night. Names (device, owner, profile) are stored with the entry so it
stays readable after the device or profile is gone. Activity of hidden
persons (owner labels) is not stored at all; changes and alarms always are.

Every stored entry is broadcast to admin sessions (``log_entry``), handed to
the notifier (``services.event_notify``) and, for taxonomy kinds with source
``hub``, pushed to the integration as ``ha_event`` so Home Assistant fires
``<ns>_event`` (and ``<ns>_alarm``). Recording is best effort: it never
raises into the request or connection that caused it.

Data minimization: no action data (values, codes), no IP addresses, no PINs
or tokens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from models.database import Database
from services import integration_events

logger = logging.getLogger("hub.services.event_log")

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "events.json"

ACTIVITY = "activity"
CHANGE = "change"
ALARM = "alarm"
CATEGORIES = (ACTIVITY, CHANGE, ALARM)

RETENTION_CHOICES = (7, 30, 90)
DEFAULT_RETENTION_DAYS = 30
DEFAULT_ALARM_THRESHOLD = {"count": 3, "window_minutes": 10}
SETTINGS_KEY = "log_settings"
LAST_CLEANUP_KEY = "log_last_cleanup"
# The nightly clean-up runs from this local hour on (HA's time zone).
CLEANUP_HOUR = 3

PAGE_DEFAULT = 100
PAGE_MAX = 500
EXPORT_MAX = 20000

# Profile audit action -> taxonomy kind (data/events.json).
AUDIT_EVENT_KIND = {
    "profile_assigned": "profile_changed",
    "profile_updated": "profile_changed",
    "override_expired": "adjustment_expired",
}

# Kids decision reason (hub control path) -> log kind / contract reason.
KIDS_REASON_KIND = {
    "quiet_hours": "kids_bedtime_attempt",
    "cooldown": "kids_pause",
    "paused_all": "kids_pause",
}
KIDS_REASON = {
    "blocked": "blocked_kids",
    "quiet_hours": "blocked_kids_bedtime",
    "cooldown": "blocked_kids_cooldown",
    "rate_limited": "blocked_kids_cooldown",
    "color_locked": "blocked_kids_color",
    "volume_limit": "blocked_kids_volume",
    "paused_all": "blocked_kids_paused_all",
}

# Integration event type -> log category (rules_applied is not stored).
INTEGRATION_CATEGORY = {
    "control": ACTIVITY,
    "kids_control": ACTIVITY,
    "denied": ACTIVITY,
    "kids_bedtime_attempt": ACTIVITY,
    "kids_pause": ACTIVITY,
    "access_group_restored": ALARM,
    "direct_access_attempt": ALARM,
    "integration_outage": ALARM,
    "integration_started": CHANGE,
}
REJECTION_KINDS = ("denied", "kids_bedtime_attempt", "kids_pause")

# Live state (set by register()).
_db: Database | None = None
_ha_client: Any = None
_recent_denials: dict[str, deque] = {}
_last_alarm: dict[str, float] = {}


@cache
def taxonomy() -> dict[str, dict]:
    data = json.loads(DATA_FILE.read_text("utf-8"))
    return {k["kind"]: k for k in data["kinds"]}


def is_alarm_kind(kind: str | None) -> bool:
    return bool(kind and taxonomy().get(kind, {}).get("alarm"))


@cache
def important_kinds() -> tuple[str, ...]:
    """Kinds marked ``important`` in the taxonomy (besides every alarm)."""
    return tuple(k for k, v in taxonomy().items() if v.get("important"))


def is_important(entry: dict) -> bool:
    """Alarm, or ``event_kind``/``kind`` marked important (same rule as the filter)."""
    if entry.get("category") == ALARM:
        return True
    kinds = important_kinds()
    return entry.get("event_kind") in kinds or entry.get("kind") in kinds


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds") if ts else None


def register(db: Database, ha_client: Any) -> None:
    """Remember database and HA client for listeners and background loops;
    store the events of ``POST /api/integration/events``."""
    global _db, _ha_client
    _db = db
    _ha_client = ha_client
    integration_events.add_sink(_integration_sink)


def reset() -> None:
    """Forget live state (tests)."""
    global _db, _ha_client
    _db = None
    _ha_client = None
    integration_events.remove_sink(_integration_sink)
    _recent_denials.clear()
    _last_alarm.clear()
    _hub_calls.clear()


# ── settings ────────────────────────────────────────────────────────────────


def default_settings() -> dict:
    return {
        "retention_days": DEFAULT_RETENTION_DAYS,
        "hidden_owners": [],
        "alarm_threshold": dict(DEFAULT_ALARM_THRESHOLD),
    }


async def get_settings(db: Database) -> dict:
    out = default_settings()
    raw = await db.get_setting(SETTINGS_KEY)
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
        if stored.get("retention_days") in RETENTION_CHOICES:
            out["retention_days"] = stored["retention_days"]
        if isinstance(stored.get("hidden_owners"), list):
            out["hidden_owners"] = [
                str(o) for o in stored["hidden_owners"] if isinstance(o, str) and o
            ]
        thr = stored.get("alarm_threshold")
        if isinstance(thr, dict):
            count, window = thr.get("count"), thr.get("window_minutes")
            if isinstance(count, int) and isinstance(window, int):
                out["alarm_threshold"] = {"count": count, "window_minutes": window}
    return out


async def save_settings(db: Database, settings: dict) -> dict:
    async with db.transaction():
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTINGS_KEY, json.dumps(settings, sort_keys=True)),
        )
    return await get_settings(db)


# ── context of a device ─────────────────────────────────────────────────────


async def client_context(db: Database, client_id: str | None) -> dict:
    """Name, owner, profile, access and HA user of a device (empty if unknown)."""
    if not client_id:
        return {}
    row = await db.fetchone(
        "SELECT c.id, c.name, c.owner_label, c.profile_id, c.access, c.ha_user_id, "
        "c.platform, c.set_up_by_client_id, c.set_up_method, c.seal_key, "
        "p.name AS profile_name, p.child_protection "
        "FROM paired_clients c LEFT JOIN profiles p ON p.id = c.profile_id "
        "WHERE c.id = ?",
        (client_id,),
    )
    if not row:
        return {}
    ctx = {
        "device_name": row.get("name"),
        "owner_label": row.get("owner_label") or None,
        "profile_id": row.get("profile_id"),
        "profile_name": row.get("profile_name"),
        "access": row.get("access"),
        "child_protection": bool(row.get("child_protection")),
        "ha_user_id": row.get("ha_user_id"),
    }
    # A child's watch acts for the child: its
    # phone's profile and owner, so activity and notifications at events
    # reach the parents like the phone's; the device name stays the watch's.
    from services.profiles import store

    phone = await store.phone_of_child_watch(db, row)
    if phone:
        ctx.update(
            owner_label=ctx["owner_label"] or phone.get("owner_label") or None,
            profile_id=phone["profile_id"],
            profile_name=phone["profile_name"],
            child_protection=True,
        )
    return ctx


async def area_of(db: Database, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    row = await db.fetchone(
        "SELECT ae.area_id FROM area_entities ae JOIN areas a ON a.id = ae.area_id "
        "WHERE ae.entity_id = ? ORDER BY a.sort_order, a.name LIMIT 1",
        (entity_id,),
    )
    return row["area_id"] if row else None


def entity_name(entity_id: str | None) -> str | None:
    if not entity_id or _ha_client is None:
        return None
    try:
        ws = getattr(_ha_client, "ws", None)
        state = ws.get_state(entity_id) if ws is not None else None
    except Exception:  # noqa: BLE001 - name is a nicety
        state = None
    name = ((state or {}).get("attributes") or {}).get("friendly_name")
    return name if isinstance(name, str) and name else None


# ── writing ─────────────────────────────────────────────────────────────────

_COLUMNS = (
    "uid",
    "ts",
    "category",
    "kind",
    "event_kind",
    "source",
    "client_id",
    "device_name",
    "owner_label",
    "profile_id",
    "profile_name",
    "area_id",
    "entity_id",
    "action",
    "route",
    "origin",
    "control_path",
    "result",
    "reason",
    "count",
    "since",
    "until",
    "actor",
    "detail_json",
)


async def insert(
    db: Database,
    *,
    category: str,
    kind: str,
    source: str = "hub",
    event_kind: str | None = None,
    client_id: str | None = None,
    uid: str | None = None,
    ts: str | None = None,
    detail: dict | None = None,
    context: dict | None = None,
    **fields: Any,
) -> dict | None:
    """Store one entry; returns it, or None when it was not stored (duplicate
    uid, activity of a hidden person)."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown log category {category!r}")
    ctx = context if context is not None else await client_context(db, client_id)
    settings = await get_settings(db)
    owner = fields.pop("owner_label", None) or ctx.get("owner_label")
    if category == ACTIVITY and owner and owner in settings["hidden_owners"]:
        return None
    entity_id = fields.get("entity_id")
    if entity_id and "area_id" not in fields:
        fields["area_id"] = await area_of(db, entity_id)
    clean_detail = {k: v for k, v in (detail or {}).items() if v is not None}
    if entity_id and "entity_name" not in clean_detail:
        name = entity_name(entity_id)
        if name:
            clean_detail["entity_name"] = name
    row = {
        "uid": uid or str(uuid.uuid4()),
        "ts": ts or now_iso(),
        "category": category,
        "kind": kind,
        "event_kind": event_kind,
        "source": source,
        "client_id": client_id,
        "device_name": fields.pop("device_name", None) or ctx.get("device_name"),
        "owner_label": owner,
        "profile_id": fields.pop("profile_id", None) or ctx.get("profile_id"),
        "profile_name": fields.pop("profile_name", None) or ctx.get("profile_name"),
        "detail_json": json.dumps(clean_detail, ensure_ascii=False)
        if clean_detail
        else None,
    }
    for col in _COLUMNS:
        if col not in row:
            row[col] = fields.get(col)
    marks = ",".join("?" for _ in _COLUMNS)
    async with db.transaction():
        cursor = await db.execute(
            f"INSERT OR IGNORE INTO event_log ({','.join(_COLUMNS)}) VALUES ({marks})",  # noqa: S608 - fixed columns
            tuple(row[c] for c in _COLUMNS),
        )
        if not cursor.rowcount:
            return None
        entry_id = cursor.lastrowid
    stored = await db.fetchone("SELECT * FROM event_log WHERE id = ?", (entry_id,))
    return await view(db, stored) if stored else None


async def record(db: Database | None = None, **kwargs: Any) -> dict | None:
    """Store an entry and run its effects (broadcast, HA event, notifier).

    Never raises; returns the stored entry or None.
    """
    db = db or _db
    if db is None:
        return None
    try:
        entry = await insert(db, **kwargs)
    except Exception as e:  # noqa: BLE001 - the log is best effort
        logger.warning("Log entry %s not stored: %s", kwargs.get("kind"), e)
        return None
    if entry is None:
        return None
    await after_insert(db, entry)
    return entry


async def after_insert(db: Database, entry: dict) -> None:
    try:
        from api import ws as api_ws
        from api.ws import EventAudience

        await api_ws.broadcast(
            {"type": "log_entry", "data": entry}, audience=EventAudience.ADMIN
        )
    except Exception as e:  # noqa: BLE001 - socket gone / shutting down
        logger.debug("log_entry broadcast failed: %s", e)
    kind = entry.get("event_kind")
    if kind and taxonomy().get(kind, {}).get("source") == "hub":
        await push_ha_event(entry)
    try:
        from services import event_notify

        await event_notify.on_entry(db, entry)
    except Exception as e:  # noqa: BLE001 - notifications are best effort
        logger.warning("Notification for log entry %s failed: %s", entry["id"], e)


def ha_event_data(entry: dict, test: bool = False) -> dict:
    """``<ns>_event`` data of an entry (contract integration/ha-event.event.json)."""
    kind = entry["event_kind"]
    tax = taxonomy().get(kind, {})
    data: dict[str, Any] = {
        "kind": kind,
        "category": tax.get("category", "system"),
        "at": entry["ts"],
        "client_id": entry.get("client_id"),
        "device_name": entry.get("device_name"),
        "owner_label": entry.get("owner_label"),
        "profile_id": entry.get("profile_id"),
        "entity_id": entry.get("entity_id"),
        "action": entry.get("action"),
        "route": entry.get("route"),
        "reason": entry.get("reason"),
        "count": entry.get("count") or 1,
        "since": entry.get("since"),
        "until": entry.get("until"),
    }
    if kind in ("access_opened", "access_closed"):
        # Why access changed (schedule = access hours, switch).
        data["access_reason"] = (entry.get("detail") or {}).get("reason")
    if kind == "tag_scanned":
        # The integration also fires <ns>_tag_scanned from these.
        detail = entry.get("detail") or {}
        data["tag_id"] = detail.get("tag_id")
        data["tag_name"] = detail.get("tag_name")
        data["result"] = detail.get("tag_result")
        # reason is a control_denial_reason; why a tag did not resolve
        # (unknown_tag, tags_off, ...) is the result.
        data["reason"] = None
    if test:
        data["test"] = True
    return data


async def push_ha_event(entry: dict, test: bool = False) -> bool:
    """Hand a hub event to the integration, which fires it in HA (fire-and-forget)."""
    try:
        from api.ws.broadcast import send_to_integration

        return await send_to_integration(
            {"type": "ha_event", "data": ha_event_data(entry, test)}
        )
    except Exception as e:  # noqa: BLE001 - integration session gone
        logger.debug("ha_event push failed: %s", e)
        return False


async def set_sent(db: Database, entry_id: int, sent: list[dict]) -> None:
    """Remember where an entry was notified ("Notified" in the detail panel)."""
    row = await db.fetchone(
        "SELECT detail_json FROM event_log WHERE id = ?", (entry_id,)
    )
    if not row:
        return
    detail = json.loads(row["detail_json"]) if row.get("detail_json") else {}
    detail["sent"] = (detail.get("sent") or []) + sent
    async with db.transaction():
        await db.execute(
            "UPDATE event_log SET detail_json = ? WHERE id = ?",
            (json.dumps(detail, ensure_ascii=False), entry_id),
        )


# ── sources ─────────────────────────────────────────────────────────────────


async def audit_change(
    db: Database,
    action: str,
    *,
    actor: str | None,
    profile_id: str | None,
    client_id: str | None,
    rule_key: str | None,
    before: Any,
    after: Any,
    note: str | None,
) -> None:
    """Mirror a profile audit entry (services/profiles/changes.audit)."""
    ctx = await client_context(db, client_id) if client_id else {}
    prof_name = None
    if profile_id:
        prow = await db.fetchone(
            "SELECT name FROM profiles WHERE id = ?", (profile_id,)
        )
        prof_name = (prow or {}).get("name")
        if prof_name is None and isinstance(before, dict):
            prof_name = before.get("name")
    if not client_id and profile_id:
        ctx = {}
    detail = {"rule": rule_key, "before": before, "after": after, "note": note}
    kwargs: dict[str, Any] = {}
    if profile_id:
        kwargs["profile_id"] = profile_id
        kwargs["profile_name"] = prof_name
    await record(
        db,
        category=CHANGE,
        kind=action,
        event_kind=(
            "family_change"
            if isinstance(after, dict) and after.get("origin") == "family_manager"
            else AUDIT_EVENT_KIND.get(action)
        ),
        source="hub",
        client_id=client_id,
        actor=actor,
        detail=detail,
        context=ctx,
        **kwargs,
    )


async def device_paired(
    db: Database,
    client_id: str,
    *,
    guest: bool,
    method: str | None = None,
    actor: str | None = None,
    invited_by: str | None = None,
    camera_qr: bool = False,
) -> None:
    ctx = await client_context(db, client_id)
    row = await db.fetchone(
        "SELECT expires_at, platform FROM paired_clients WHERE id = ?", (client_id,)
    )
    detail = {
        "method": method,
        "invited_by": invited_by,
        "platform": (row or {}).get("platform"),
    }
    if camera_qr:
        # Paired with the code shown on the Home Assistant camera.
        detail["camera_qr"] = True
    await record(
        db,
        category=CHANGE,
        kind="guest_paired" if guest else "device_paired",
        event_kind="guest_paired" if guest else "device_paired",
        client_id=client_id,
        context=ctx,
        actor=actor,
        until=(row or {}).get("expires_at") if guest else None,
        detail=detail,
    )


async def device_set_up(
    db: Database,
    client_id: str,
    *,
    kind: str,
    method: str,
    by_client_id: str,
    by_name: str,
) -> None:
    """A member device set up this device or guest: “iPhone of the child set
    up by the parent's iPhone as Child 6–9”. Next to the device_paired /
    guest_paired entry."""
    ctx = await client_context(db, client_id)
    row = await db.fetchone(
        "SELECT expires_at FROM paired_clients WHERE id = ?", (client_id,)
    )
    await record(
        db,
        category=CHANGE,
        kind="device_set_up",
        event_kind="device_set_up",
        client_id=client_id,
        context=ctx,
        actor=by_name or None,
        until=(row or {}).get("expires_at"),
        detail={
            "kind": kind,
            "method": method,
            "set_up_by": {"client_id": by_client_id, "name": by_name},
        },
    )


async def device_removed(
    db: Database,
    client_id: str,
    *,
    how: str,
    actor: str | None = None,
    context: dict | None = None,
    set_up: list[dict] | None = None,
    with_device: dict | None = None,
) -> None:
    """``set_up`` = devices this one set up that stay paired (their
    ``set_up_by`` names a removed device): ``detail.set_up_devices``.
    ``with_device`` = the phone a watch went with: ``detail.with_device``."""
    ctx = context if context is not None else await client_context(db, client_id)
    guest = ctx.get("access") == "guest"
    event_kind = "guest_expired" if how == "guest_expired" else "device_removed"
    kind = "device_revoked" if how == "revoked" else event_kind
    detail: dict[str, Any] = {"how": how, "guest": guest or None}
    if set_up:
        detail["set_up_devices"] = set_up
    if with_device:
        detail["with_device"] = with_device
    await record(
        db,
        category=CHANGE,
        kind=kind,
        event_kind=event_kind,
        client_id=client_id,
        context=ctx,
        actor=actor,
        detail=detail,
    )
    if how != "revoked":
        try:
            from services import event_notify

            await event_notify.prune_device(db, client_id)
        except Exception as e:  # noqa: BLE001 - best effort
            logger.warning("Notification targets of %s not pruned: %s", client_id, e)


async def guest_invited(
    db: Database,
    *,
    by_client_id: str | None,
    actor: str | None,
    until: str | None = None,
    profile_id: str | None = None,
) -> None:
    ctx = await client_context(db, by_client_id) if by_client_id else {}
    kwargs: dict[str, Any] = {}
    if profile_id:
        prow = await db.fetchone(
            "SELECT name FROM profiles WHERE id = ?", (profile_id,)
        )
        kwargs["profile_id"] = profile_id
        kwargs["profile_name"] = (prow or {}).get("name")
    await record(
        db,
        category=CHANGE,
        kind="guest_invited",
        event_kind="guest_invited",
        client_id=by_client_id,
        context=ctx,
        actor=actor,
        until=until,
        detail={"by_device": bool(by_client_id) or None},
        **kwargs,
    )


async def kids_release(
    db: Database, client_id: str, *, actor: str | None, action: str, until: str | None
) -> None:
    await record(
        db,
        category=CHANGE,
        kind="kids_release" if action == "release" else "kids_pause_lifted",
        client_id=client_id,
        actor=actor,
        until=until,
    )


async def kids_paused_all(
    db: Database,
    client_id: str,
    *,
    entity_id: str,
    action: str,
    until: str | None,
    info: dict,
) -> dict | None:
    """Everything of a child device is paused: one entry per pause,
    from either control path (the hub counts both)."""
    return await record(
        db,
        category=ACTIVITY,
        kind="kids_paused_all",
        event_kind="kids_paused_all",
        source="hub",
        client_id=client_id,
        entity_id=entity_id,
        action=action,
        result="denied",
        reason="blocked_kids_paused_all",
        until=until,
        detail={
            "trigger": info.get("trigger"),
            "actions": info.get("actions"),
            "entities": list(info.get("entities") or [])[:10],
            "window_seconds": info.get("window_seconds"),
        },
    )


async def hub_control(
    db: Database,
    client_id: str,
    *,
    entity_id: str,
    action: str,
    ok: bool,
    reason: str | None = None,
    until: str | None = None,
    pause_start: bool = False,
    child: bool = True,
) -> None:
    """The hub switched (or refused to switch) for a device on the hub path.

    With parental controls (``child``) the kids kinds: a pause is one entry,
    the attempt that starts it (``pause_start``, ``kids_pause``; for
    everything ``kids_paused_all``), and every further attempt during it only
    counts up ``count`` of that entry. Without parental controls the kinds
    ``control`` and ``denied`` with the integration's reason. The denial
    threshold for alarms counts every attempt.
    """
    if not child:
        entry = await record(
            db,
            category=ACTIVITY,
            kind="control" if ok else "denied",
            event_kind="control" if ok else "denied",
            source="hub",
            client_id=client_id,
            entity_id=entity_id,
            action=action,
            route="local",
            origin="app",
            control_path="hub",
            result="ok" if ok else "denied",
            reason=None if ok else reason,
            until=until,
        )
        if entry and not ok:
            await check_denial_threshold(db, entry)
        return
    if ok:
        kind = "kids_control"
    elif pause_start and reason == "rate_limited":
        kind = "kids_pause"
    else:
        kind = KIDS_REASON_KIND.get(reason or "", "denied")
    if not ok and until and reason in PAUSE_REPEAT_KIND:
        repeat = await _count_pause_repeat(db, client_id, entity_id, reason, until)
        if repeat:
            await check_denial_threshold(db, repeat)
            return
    entry = await record(
        db,
        category=ACTIVITY,
        kind=kind,
        event_kind=kind,
        source="hub",
        client_id=client_id,
        entity_id=entity_id,
        action=action,
        route="local",
        origin="app",
        control_path="hub",
        result="ok" if ok else "denied",
        reason=None if ok else KIDS_REASON.get(reason or "", reason),
        until=until,
    )
    if entry and not ok:
        await check_denial_threshold(db, entry)


# The hub's own <ns>.control calls (client, entity, action, monotonic
# time): the
# integration reports the same call; that report is dropped for devices
# without parental controls (the hub's entry stands for it; with parental
# controls every report of the device is dropped, see ingest_integration).
HUB_CALL_SECONDS = 30.0
HUB_CALLS_MAX = 2000
_hub_calls: deque[tuple[str, str, str, float]] = deque(maxlen=HUB_CALLS_MAX)


def expect_hub_call(
    client_id: str, entity_id: str, action: str
) -> tuple[str, str, str, float]:
    """Remember a call the hub makes now; returns its mark."""
    mark = (client_id, entity_id, action, time.monotonic())
    _hub_calls.append(mark)
    return mark


def forget_hub_call(mark: tuple[str, str, str, float]) -> None:
    """The call never reached the integration (nothing to drop)."""
    try:
        _hub_calls.remove(mark)
    except ValueError:
        pass


def _take_hub_call(
    client_id: str | None, entity_id: str | None, action: str | None
) -> bool:
    """Consume the oldest recent hub call of this device, entity and action
    (a report of any other call is stored)."""
    if not client_id or not entity_id or not action:
        return False
    now = time.monotonic()
    while _hub_calls and now - _hub_calls[0][3] > HUB_CALL_SECONDS:
        _hub_calls.popleft()
    for mark in _hub_calls:
        if mark[:3] == (client_id, entity_id, action):
            _hub_calls.remove(mark)
            return True
    return False


# Rejection reason during a running pause -> kind of the entry of that pause.
PAUSE_REPEAT_KIND = {"cooldown": "kids_pause", "paused_all": "kids_paused_all"}


async def _count_pause_repeat(
    db: Database, client_id: str, entity_id: str, reason: str, until: str
) -> dict | None:
    """Count one more attempt on the entry of the running pause; None when
    there is none (then the attempt gets its own entry)."""
    kind = PAUSE_REPEAT_KIND[reason]
    sql = (
        "SELECT id FROM event_log WHERE client_id = ? AND category = ? AND kind = ? "
        "AND until = ?"
    )
    params: list[Any] = [client_id, ACTIVITY, kind, until]
    if reason == "cooldown":
        # A device pause belongs to one entity; everything paused to all.
        sql += " AND entity_id = ?"
        params.append(entity_id)
    try:
        row = await db.fetchone(sql + " ORDER BY id DESC LIMIT 1", tuple(params))
        if row is None:
            return None
        async with db.transaction():
            await db.execute(
                "UPDATE event_log SET count = COALESCE(count, 1) + 1 WHERE id = ?",
                (row["id"],),
            )
    except Exception as e:  # noqa: BLE001 - the log is best effort
        logger.warning("Counting a repeated pause attempt failed: %s", e)
        return None
    return {
        "client_id": client_id,
        "entity_id": entity_id,
        "reason": KIDS_REASON.get(reason, reason),
        "ts": now_iso(),
    }


async def check_denial_threshold(db: Database, entry: dict) -> None:
    """Rejections become an alarm from the threshold on."""
    client_id = entry.get("client_id")
    if not client_id:
        return
    settings = await get_settings(db)
    count = settings["alarm_threshold"]["count"]
    window = settings["alarm_threshold"]["window_minutes"] * 60
    ts = parse_ts(entry["ts"]) or datetime.now(timezone.utc)
    stamp = ts.timestamp()
    q = _recent_denials.setdefault(client_id, deque(maxlen=200))
    q.append((stamp, entry.get("entity_id"), entry.get("reason")))
    while q and q[0][0] < stamp - window:
        q.popleft()
    if len(q) < count:
        return
    last = _last_alarm.get(client_id)
    if last is not None and stamp - last < window:
        return
    _last_alarm[client_id] = stamp
    reasons = [r for _, _, r in q if r]
    entities = [e for _, e, _ in q if e]
    first = datetime.fromtimestamp(q[0][0], timezone.utc)
    await record(
        db,
        category=ALARM,
        kind="denied",
        event_kind="denied",
        source="hub",
        client_id=client_id,
        entity_id=entities[-1] if entities else None,
        reason=reasons[-1] if reasons else None,
        count=len(q),
        since=iso(first),
        detail={
            "derived": True,
            "window_minutes": window // 60,
            "entities": sorted(set(entities))[:10],
        },
    )
    q.clear()


async def _integration_sink(events: list[dict], dropped: int) -> None:
    if _db is not None:
        await store_integration_events(_db, events, dropped)


async def store_integration_events(
    db: Database, events: list[dict], dropped: int
) -> dict:
    """Store new events of ``POST /api/integration/events``.

    The intake (``services/integration_events``) drops repeated ids while the
    hub runs; ``uid int:<id>`` keeps the log idempotent across restarts.
    Switching of devices with parental controls goes through the hub, which
    records it itself (``hub_control``); the integration's reports of such a
    device are not stored. For other devices on the hub path the report of a
    call the hub made in the last :data:`HUB_CALL_SECONDS` is dropped
    (:func:`expect_hub_call`); the hub's entry stands for it.
    """
    accepted = duplicates = 0
    contexts: dict[str, dict] = {}
    by_user: dict[str, str] = {}
    for ev in events:
        etype = ev["type"]
        category = INTEGRATION_CATEGORY.get(etype)
        if category is None:
            accepted += 1  # rules_applied: known, not part of the log
            continue
        client_id = ev.get("client_id")
        if not client_id and ev.get("ha_user_id"):
            if not by_user:
                for r in await db.fetchall(
                    "SELECT id, ha_user_id FROM paired_clients WHERE ha_user_id IS NOT NULL"
                ):
                    by_user[r["ha_user_id"]] = r["id"]
            client_id = by_user.get(ev["ha_user_id"])
        if client_id and client_id not in contexts:
            contexts[client_id] = await client_context(db, client_id)
        ctx = contexts.get(client_id or "", {})
        if category == ACTIVITY and (
            ctx.get("child_protection")
            or _take_hub_call(client_id, ev.get("entity_id"), ev.get("action"))
        ):
            accepted += 1
            continue
        is_rejection = etype in REJECTION_KINDS
        path = None
        if category == ACTIVITY:
            path = "hub" if ev.get("origin") == "hub" else "checked"
        try:
            entry = await insert(
                db,
                uid=f"int:{ev['id']}",
                ts=ev["at"],
                category=category,
                kind=etype,
                event_kind=etype,
                source="integration",
                client_id=client_id,
                context=ctx,
                entity_id=ev.get("entity_id"),
                action=ev.get("action"),
                route=ev.get("route"),
                origin=ev.get("origin"),
                control_path=path,
                result=("denied" if is_rejection else "ok")
                if category == ACTIVITY
                else None,
                reason=ev.get("reason"),
                count=ev.get("count"),
                since=ev.get("since"),
                detail={
                    "reliability": ev.get("reliability"),
                    "ha_group": ev.get("ha_group"),
                    "version": ev.get("version"),
                    "ha_user_id": ev.get("ha_user_id") if not client_id else None,
                },
            )
        except Exception as e:  # noqa: BLE001 - one bad event never drops the batch
            logger.warning("Integration event %s not stored: %s", ev.get("id"), e)
            continue
        if entry is None:
            if await db.fetchone(
                "SELECT 1 AS x FROM event_log WHERE uid = ?", (f"int:{ev['id']}",)
            ):
                duplicates += 1
            else:
                accepted += 1  # hidden person: accepted, not stored
            continue
        accepted += 1
        await after_insert(db, entry)
        if is_rejection:
            await check_denial_threshold(db, entry)
    if dropped:
        logger.warning("The integration dropped %d log events (queue full)", dropped)
        await record(
            db,
            category=CHANGE,
            kind="integration_dropped",
            source="integration",
            count=dropped,
        )
    return {"accepted": accepted, "duplicates": duplicates}


# ── HA call_service of devices that switch directly (control path "direct") ────────

# Domains that are never device actions of a person.
_IGNORED_DOMAINS = {"system_log", "logger", "recorder", "persistent_notification"}
_direct_cache: dict[str, tuple[float, str | None, dict]] = {}
DIRECT_CACHE_SECONDS = 30.0


async def _client_of_user(db: Database, user_id: str) -> tuple[str | None, dict]:
    now = time.monotonic()
    cached = _direct_cache.get(user_id)
    if cached and now - cached[0] < DIRECT_CACHE_SECONDS:
        return cached[1], cached[2]
    row = await db.fetchone(
        "SELECT id FROM paired_clients WHERE ha_user_id = ? AND is_active = 1",
        (user_id,),
    )
    cid = row["id"] if row else None
    ctx = await client_context(db, cid) if cid else {}
    if cid:
        try:
            from services.profiles import store

            resolved = await store.resolve_client(db, cid)
            rules = resolved[2] if resolved else {}
            ctx["control_path"] = (rules.get("control_path") or {}).get("value")
        except Exception:  # noqa: BLE001 - path is informative only
            ctx["control_path"] = None
    if len(_direct_cache) > 500:
        _direct_cache.clear()
    _direct_cache[user_id] = (now, cid, ctx)
    return cid, ctx


async def ha_call_service(db: Database, data: dict, context: dict | None) -> None:
    """An HA ``call_service`` event: log it when a device user called it directly."""
    from namespace import NAMESPACE

    user_id = (context or {}).get("user_id")
    domain = data.get("domain")
    service = data.get("service")
    if not user_id or not isinstance(domain, str) or not isinstance(service, str):
        return
    if domain == NAMESPACE or domain in _IGNORED_DOMAINS:
        return
    cid, ctx = await _client_of_user(db, user_id)
    if not cid:
        return
    if ctx.get("child_protection"):
        return  # the hub control path logs these itself
    if ctx.get("control_path") in ("hub", "checked"):
        # Read-only device user: Home Assistant rejects the call; the
        # integration reports direct access attempts.
        return
    sdata = data.get("service_data") or {}
    targets = sdata.get("entity_id")
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list) or not targets:
        targets = [None]
    for entity_id in targets[:20]:
        if entity_id is not None and not isinstance(entity_id, str):
            continue
        await record(
            db,
            category=ACTIVITY,
            kind="control",
            event_kind="control",
            source="ha",
            client_id=cid,
            context=ctx,
            entity_id=entity_id,
            action=service,
            control_path="direct",
            result="ok",
            detail={"domain": domain if entity_id is None else None},
        )


def attach_ha_listener(ha_ws: Any) -> None:
    """Follow ``call_service`` events of the persistent HA connection."""
    from services import tasks

    def _listener(event_type: str, data: dict, context: dict) -> None:
        if event_type != "call_service" or _db is None:
            return
        if not (context or {}).get("user_id"):
            return
        tasks.spawn(ha_call_service(_db, data, context), name="log-call-service")

    ha_ws.add_context_listener(_listener)


# ── reading ─────────────────────────────────────────────────────────────────


async def _names(db: Database) -> tuple[dict, dict, dict]:
    clients = {
        r["id"]: r
        for r in await db.fetchall(
            "SELECT id, name, owner_label, pending_deletion_at FROM paired_clients"
        )
    }
    profiles = {
        r["id"]: r["name"] for r in await db.fetchall("SELECT id, name FROM profiles")
    }
    areas = {
        r["id"]: r["name"] for r in await db.fetchall("SELECT id, name FROM areas")
    }
    return clients, profiles, areas


async def view(db: Database, row: dict, names: tuple | None = None) -> dict:
    clients, profiles, areas = names or await _names(db)
    detail = json.loads(row["detail_json"]) if row.get("detail_json") else {}
    client = clients.get(row.get("client_id") or "")
    device_exists = bool(client and not client.get("pending_deletion_at"))
    out = {k: row.get(k) for k in _COLUMNS if k not in ("uid", "detail_json")}
    out["id"] = row["id"]
    out["detail"] = detail or None
    out["device_exists"] = device_exists
    if client and client.get("name"):
        out["device_name"] = client["name"]
    out["profile_exists"] = bool(row.get("profile_id") in profiles)
    if row.get("profile_id") in profiles:
        out["profile_name"] = profiles[row["profile_id"]]
    out["area_name"] = areas.get(row.get("area_id") or "")
    out["entity_name"] = detail.get("entity_name")
    out["unread"] = row.get("category") == ALARM and not row.get("read_at")
    out["important"] = is_important(row)
    return out


def retention_cutoff(settings: dict, now: datetime | None = None) -> str:
    """Entries older than the retention are never shown (the nightly clean-up
    deletes them)."""
    now = now or datetime.now(timezone.utc)
    return iso(now - timedelta(days=settings["retention_days"]))


def _where(
    filters: dict, hidden: list[str], cutoff: str | None = None
) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    if cutoff:
        where.append("ts >= ?")
        params.append(cutoff)
    if hidden:
        marks = ",".join("?" for _ in hidden)
        where.append(
            f"NOT (category = 'activity' AND owner_label IN ({marks}))"  # noqa: S608
        )
        params.extend(hidden)
    if filters.get("category"):
        where.append("category = ?")
        params.append(filters["category"])
    if filters.get("since"):
        where.append("ts >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        where.append("ts < ?")
        params.append(filters["until"])
    for key, col in (
        ("client_id", "client_id"),
        ("owner", "owner_label"),
        ("profile_id", "profile_id"),
        ("area_id", "area_id"),
        ("kind", "kind"),
    ):
        if filters.get(key):
            where.append(f"{col} = ?")
            params.append(filters[key])
    if filters.get("unread"):
        where.append("category = 'alarm' AND read_at IS NULL")
    if filters.get("important"):
        kinds = important_kinds()
        marks = ",".join("?" for _ in kinds)
        where.append(
            f"(category = 'alarm' OR event_kind IN ({marks}) OR kind IN ({marks}))"  # noqa: S608 - placeholders only
        )
        params.extend(kinds * 2)
    if filters.get("q"):
        text = str(filters["q"]).lower()
        for ch in ("\\", "%", "_"):
            text = text.replace(ch, "\\" + ch)
        like = f"%{text}%"
        cols = (
            "coalesce(device_name,'')",
            "coalesce(owner_label,'')",
            "coalesce(profile_name,'')",
            "coalesce(entity_id,'')",
            "coalesce(detail_json,'')",
            "coalesce(actor,'')",
            "kind",
            "coalesce(reason,'')",
        )
        ors = [f"lower({c}) LIKE ? ESCAPE '\\'" for c in cols]
        ors.append(
            "area_id IN (SELECT id FROM areas WHERE lower(name) LIKE ? ESCAPE '\\')"
        )
        where.append("(" + " OR ".join(ors) + ")")
        params.extend([like] * len(ors))
    return where, params


async def list_entries(
    db: Database,
    filters: dict,
    *,
    before_id: int | None = None,
    limit: int = PAGE_DEFAULT,
) -> dict:
    settings = await get_settings(db)
    where, params = _where(
        filters, settings["hidden_owners"], retention_cutoff(settings)
    )
    if before_id:
        anchor = await db.fetchone(
            "SELECT ts FROM event_log WHERE id = ?", (before_id,)
        )
        if anchor:
            where.append("(ts < ? OR (ts = ? AND id < ?))")
            params.extend([anchor["ts"], anchor["ts"], before_id])
        else:
            where.append("id < ?")
            params.append(before_id)
    sql = "SELECT * FROM event_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit + 1)
    rows = await db.fetchall(sql, tuple(params))
    names = await _names(db)
    entries = [await view(db, r, names) for r in rows[:limit]]
    return {"entries": entries, "has_more": len(rows) > limit}


async def summary(db: Database, tz_name: str | None = None) -> dict:
    settings = await get_settings(db)
    hidden = settings["hidden_owners"]
    hide_sql = " AND ts >= ?"
    params: list[Any] = [retention_cutoff(settings)]
    if hidden:
        hide_sql += (
            " AND NOT (category = 'activity' AND owner_label IN ("
            + ",".join("?" for _ in hidden)
            + "))"
        )
        params += list(hidden)
    now = datetime.now(timezone.utc)
    start_today = start_of_day(now, tz_name)
    week = iso(now - timedelta(days=7))

    async def count(extra: str, args: list[Any]) -> int:
        row = await db.fetchone(
            f"SELECT COUNT(*) AS n FROM event_log WHERE {extra}{hide_sql}",  # noqa: S608
            tuple(args + params),
        )
        return int((row or {}).get("n") or 0)

    unread = await db.fetchall(
        "SELECT id, ts, kind, device_name, client_id FROM event_log "
        "WHERE category = 'alarm' AND read_at IS NULL AND ts >= ? "
        "ORDER BY ts DESC, id DESC LIMIT 20",
        (params[0],),
    )
    unread_count = await count("category = 'alarm' AND read_at IS NULL", [])
    total = await count("1 = 1", [])
    oldest = await db.fetchone(
        "SELECT MIN(ts) AS ts FROM event_log WHERE ts >= ?", (params[0],)
    )
    facets = await _facets(db)
    from api.ws import get_integration_push_connected

    from namespace import NAMESPACE

    return {
        "namespace": NAMESPACE,
        "unread_alarms": unread_count,
        "unread": unread,
        "counts": {
            "activity_today": await count(
                "category = 'activity' AND ts >= ?", [iso(start_today)]
            ),
            "changes_7d": await count("category = 'change' AND ts >= ?", [week]),
            "alarms_7d": await count("category = 'alarm' AND ts >= ?", [week]),
        },
        "total": total,
        "oldest_ts": (oldest or {}).get("ts"),
        "retention_days": settings["retention_days"],
        "hidden_owners": hidden,
        "integration_connected": get_integration_push_connected(),
        "time_zone": tz_name,
        "facets": facets,
    }


async def _facets(db: Database) -> dict:
    """Devices, persons, profiles and areas for the filter lists (log + current)."""
    devices: dict[str, dict] = {}
    for r in await db.fetchall(
        "SELECT client_id, device_name, MAX(ts) AS ts FROM event_log "
        "WHERE client_id IS NOT NULL GROUP BY client_id"
    ):
        devices[r["client_id"]] = {"id": r["client_id"], "name": r["device_name"] or ""}
    for r in await db.fetchall(
        "SELECT id, name, owner_label FROM paired_clients WHERE pending_deletion_at IS NULL"
    ):
        devices[r["id"]] = {"id": r["id"], "name": r["name"] or ""}
    owners = {
        r["owner_label"]
        for r in await db.fetchall(
            "SELECT DISTINCT owner_label FROM event_log WHERE owner_label IS NOT NULL "
            "UNION SELECT DISTINCT owner_label FROM paired_clients "
            "WHERE owner_label IS NOT NULL AND owner_label != ''"
        )
        if r.get("owner_label")
    }
    profiles: dict[str, dict] = {}
    for r in await db.fetchall(
        "SELECT profile_id, profile_name FROM event_log WHERE profile_id IS NOT NULL "
        "GROUP BY profile_id"
    ):
        profiles[r["profile_id"]] = {
            "id": r["profile_id"],
            "name": r["profile_name"] or "",
        }
    for r in await db.fetchall("SELECT id, name FROM profiles"):
        profiles[r["id"]] = {"id": r["id"], "name": r["name"]}
    areas = [
        {"id": r["id"], "name": r["name"]}
        for r in await db.fetchall(
            "SELECT id, name FROM areas ORDER BY sort_order, name"
        )
    ]
    return {
        "devices": sorted(devices.values(), key=lambda d: d["name"].lower()),
        "owners": sorted(owners, key=str.lower),
        "profiles": sorted(profiles.values(), key=lambda p: p["name"].lower()),
        "areas": areas,
    }


async def mark_read(db: Database, ids: list[int] | None) -> int:
    ts = now_iso()
    async with db.transaction():
        if ids is None:
            cursor = await db.execute(
                "UPDATE event_log SET read_at = ? WHERE category = 'alarm' AND read_at IS NULL",
                (ts,),
            )
        else:
            if not ids:
                return 0
            marks = ",".join("?" for _ in ids)
            cursor = await db.execute(
                f"UPDATE event_log SET read_at = ? WHERE category = 'alarm' "  # noqa: S608
                f"AND read_at IS NULL AND id IN ({marks})",
                (ts, *ids),
            )
    return cursor.rowcount or 0


# ── retention ───────────────────────────────────────────────────────────────


def start_of_day(now: datetime, tz_name: str | None) -> datetime:
    tz = _zone(tz_name)
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc
    )


def _zone(tz_name: str | None):
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - unknown zone: UTC
            pass
    return timezone.utc


async def cleanup(db: Database, now: datetime | None = None) -> int:
    """Delete entries older than the retention (and stale notification marks)."""
    settings = await get_settings(db)
    now = now or datetime.now(timezone.utc)
    cutoff = iso(now - timedelta(days=settings["retention_days"]))
    async with db.transaction():
        cursor = await db.execute("DELETE FROM event_log WHERE ts < ?", (cutoff,))
        await db.execute("DELETE FROM log_notify_marks WHERE ts < ?", (cutoff,))
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (LAST_CLEANUP_KEY, iso(now)),
        )
    deleted = cursor.rowcount or 0
    if deleted:
        logger.info("Log clean-up: %d entries older than %s deleted", deleted, cutoff)
    return deleted


async def cleanup_due(db: Database, now: datetime, tz_name: str | None) -> bool:
    """Nightly: once per local day, from CLEANUP_HOUR on (or after 24 h)."""
    last = parse_ts(await db.get_setting(LAST_CLEANUP_KEY))
    if last is None or now - last >= timedelta(hours=24):
        return True
    local = now.astimezone(_zone(tz_name))
    last_local = last.astimezone(_zone(tz_name))
    return local.date() > last_local.date() and local.hour >= CLEANUP_HOUR


async def housekeeping_loop(
    db: Database, ha_client: Any, interval: float = 60.0
) -> None:
    """Every minute: outage and expiry watchers; nightly the retention clean-up."""
    from services import event_notify, kids_mode

    await asyncio.sleep(5)
    while True:
        try:
            now = datetime.now(timezone.utc)
            tz_name = await kids_mode.hub_time_zone(ha_client)
            if await cleanup_due(db, now, tz_name):
                await cleanup(db, now)
            await watch_integration(db, now)
            await event_notify.tick(db, now, tz_name)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - next round tries again
            logger.error("Log housekeeping failed: %s", e, exc_info=True)
        await asyncio.sleep(interval)


# ── integration reachability (hub side) ─────────────────────────────────────

OUTAGE_LOG_AFTER = timedelta(minutes=1)
_outage: dict[str, Any] = {"seen": False, "down_since": None, "entry_id": None}


def reset_outage() -> None:
    _outage.update(seen=False, down_since=None, entry_id=None)


def outage_since() -> datetime | None:
    return _outage["down_since"]


async def watch_integration(
    db: Database, now: datetime, connected: bool | None = None
) -> None:
    """The integration's push session is down: alarm after a minute (once),
    the entry gets its end when it is back."""
    if connected is None:
        from api.ws import get_integration_push_connected

        connected = get_integration_push_connected()
    if connected:
        if _outage["entry_id"] is not None:
            async with db.transaction():
                await db.execute(
                    "UPDATE event_log SET until = ? WHERE id = ?",
                    (iso(now), _outage["entry_id"]),
                )
            from services import event_notify

            await event_notify.outage_ended(db, _outage["down_since"], now)
        _outage.update(seen=True, down_since=None, entry_id=None)
        return
    if not _outage["seen"]:
        return  # never connected since the hub started: nothing to compare
    if _outage["down_since"] is None:
        _outage["down_since"] = now
        return
    if _outage["entry_id"] is None and now - _outage["down_since"] >= OUTAGE_LOG_AFTER:
        entry = await record(
            db,
            category=ALARM,
            kind="integration_outage",
            event_kind="integration_outage",
            source="hub",
            since=iso(_outage["down_since"]),
            detail={"live": True},
        )
        _outage["entry_id"] = entry["id"] if entry else -1
