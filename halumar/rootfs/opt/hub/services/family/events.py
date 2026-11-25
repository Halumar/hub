"""Family events in the log tab and the automatic child
notifications of the family managers.

Storage: the persistent log ``event_log`` (``services.event_log``) is the one
place: requests, answers, releases and – through the profile audit – kids rule
changes of a family manager are log entries with an ``event_kind`` of the
taxonomy (``data/events.json``: ``kids_release_requested``,
``kids_request_answered``, ``kids_request_expired``, ``family_change``); the
log pushes them to the integration (``ha_event`` → ``<ns>_event``) and to the
notifications at events. The children's activity of the family managers is a
read view of the same log (``services.family.service.activity``).

``emit`` hands an event to the in-process sinks (the automatic child
notifications to the HA notify targets linked to a family manager,
``services.family.notify``). Kids events of the integration's
``<ns>.control`` reach them through ``integration_events.add_sink`` (one
intake, ``POST /api/integration/events``); those of the hub's own kids control
path through :func:`kids_control` / :func:`kids_blocked`.

Everything here is best effort: a failing write or sink is logged and never
breaks the action that caused it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.family.events")

# sink(event, extra): event = contract shape (integration/ha-event.event.json),
# extra = family details for the notification texts (rule, actor), never logged.
Sink = Callable[[dict[str, Any], dict[str, Any]], Awaitable[None]]

_sinks: list[Sink] = []

# Log kinds of the family (log entry ``kind``; event_kind of the taxonomy).
REQUEST = "kids_release_requested"
ANSWERED = "kids_request_answered"
EXPIRED = "kids_request_expired"
RELEASE = "family_release"
RELEASE_END = "family_release_end"
FAMILY_CHANGE = "family_change"

# Kids events of the integration the family managers hear about.
INTEGRATION_KIDS = ("kids_control", "kids_bedtime_attempt", "kids_pause")


def add_sink(sink: Sink) -> None:
    if sink not in _sinks:
        _sinks.append(sink)


def remove_sink(sink: Sink) -> None:
    if sink in _sinks:
        _sinks.remove(sink)


def reset() -> None:
    """Forget sinks (tests)."""
    _sinks.clear()


async def child_of(db: Database, client_id: str) -> tuple[str, dict | None]:
    """(child id, watch row): a child's watch counts for its phone, the child;
    the watch is named in the entry."""
    from services.profiles import store

    row = await db.fetchone(
        "SELECT id, name, platform, set_up_by_client_id, set_up_method, seal_key "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    phone = await store.phone_of_child_watch(db, row) if row else None
    return (phone["id"], row) if phone else (client_id, None)


async def log(
    db: Database,
    kind: str,
    *,
    client_id: str,
    category: str,
    event_kind: str | None,
    entity_id: str | None = None,
    action: str | None = None,
    until: str | None = None,
    by: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
) -> dict | None:
    """One family entry of the log tab (never raises)."""
    from services import event_log

    info = dict(detail or {})
    client_id, watch = await child_of(db, client_id)
    if watch:
        info["watch"] = {"client_id": watch["id"], "name": watch["name"]}
    if by:
        info["by"] = by
    fields: dict[str, Any] = {}
    if entity_id:
        fields["entity_id"] = entity_id
    if by and by.get("via"):
        # home/away is the route; origin is where in the app it was
        # triggered (app, quick_access) and stays unset here.
        fields["route"] = {"home": "local", "away": "external"}.get(by["via"])
    return await event_log.record(
        db,
        category=category,
        kind=kind,
        event_kind=event_kind,
        source="hub",
        client_id=client_id,
        action=action,
        until=until,
        actor=(by or {}).get("name"),
        detail=info or None,
        **fields,
    )


async def emit(
    db: Database,
    kind: str,
    *,
    client_id: str | None,
    entity_id: str | None = None,
    action: str | None = None,
    reason: str | None = None,
    until: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hand an event to the in-process sinks; returns the event."""
    from services.family import store

    event: dict[str, Any] = {
        "kind": kind,
        "category": "kids",
        "at": store.iso(store.utcnow()),
        "client_id": client_id,
        "entity_id": entity_id,
        "action": action,
        "reason": reason,
        "until": until,
    }
    if client_id:
        child_id, watch = await child_of(db, client_id)
        event["client_id"] = child_id
        row = await db.fetchone(
            "SELECT name, owner_label, profile_id FROM paired_clients WHERE id = ?",
            (child_id,),
        )
        if row:
            event.update(
                # A child's watch: the child's entry, naming the watch.
                device_name=watch["name"] if watch else row["name"],
                owner_label=row.get("owner_label"),
                profile_id=row.get("profile_id"),
            )
    clean = {k: v for k, v in event.items() if v is not None}
    for sink in list(_sinks):
        try:
            await sink(clean, extra or {})
        except Exception:  # noqa: BLE001 - one sink must not break the others
            logger.exception("family event sink failed")
    return clean


# ── kids control path of the hub (services.kids_mode) ───────────────────────


async def kids_control(
    db: Database, client_id: str, entity_id: str, service: str
) -> None:
    await emit(
        db, "kids_control", client_id=client_id, entity_id=entity_id, action=service
    )


async def kids_blocked(
    db: Database,
    client_id: str,
    entity_id: str,
    service: str,
    reason: str | None,
    until: str | None,
    pause_started: bool,
) -> None:
    if pause_started:
        await emit(
            db, "kids_pause", client_id=client_id, entity_id=entity_id, until=until
        )
    elif reason == "quiet_hours":
        await emit(
            db,
            "kids_bedtime_attempt",
            client_id=client_id,
            entity_id=entity_id,
            action=service,
            until=until,
        )


# ── kids events of the integration (POST /api/integration/events) ───────────


def integration_sink(db: Database):
    """``integration_events.add_sink``: kids events of ``<ns>.control``."""

    async def sink(events: list[dict[str, Any]], _dropped: int) -> None:
        for e in events:
            kind = e.get("type")
            if kind not in INTEGRATION_KIDS or not e.get("client_id"):
                continue
            # The hub's own kids control path emits these itself (grant).
            if e.get("origin") == "hub":
                continue
            if kind == "kids_control" and e.get("result") not in (None, "ok"):
                continue
            await emit(
                db,
                kind,
                client_id=e["client_id"],
                entity_id=e.get("entity_id"),
                action=e.get("action"),
                until=e.get("until"),
            )

    return sink
