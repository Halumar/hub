"""Events of the integration for the log tab – intake and hand-over.

``POST /api/integration/events`` delivers batches of structured events
(contract ``schemas/common/enforcement-event.json``). This module is the
hub's single intake: it drops repeated ids (the integration retries after a
timeout), keeps the last events in memory and hands every new event to the
registered sinks. The persistent log, alarms and notifications (log tab,
``rest/event-notifications.json``) register a sink with :func:`add_sink`; the
intake itself stores nothing on disk.

Sink contract: ``async def sink(events: list[dict], dropped: int) -> None``;
``events`` are the new events as plain dicts (only the contract fields,
``None`` values left out), in the order received; ``dropped`` is what the
integration lost while the hub was unreachable. A failing sink is logged and
does not affect the others or the answer.

``emit_ha_event`` pushes a hub event (source hub in ``data/events.json``) to
the integration, which fires it as ``<ns>_event`` / ``<ns>_alarm``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("hub.services.integration_events")

SEEN_IDS_MAX = 5000
RECENT_MAX = 500

Sink = Callable[[list[dict[str, Any]], int], Awaitable[None]]

_sinks: list[Sink] = []
_seen: OrderedDict[str, None] = OrderedDict()
_recent: deque[dict[str, Any]] = deque(maxlen=RECENT_MAX)
_totals = {"accepted": 0, "duplicates": 0, "dropped": 0}


def add_sink(sink: Sink) -> None:
    """Receive every new integration event (log store, alarms, notifications)."""
    if sink not in _sinks:
        _sinks.append(sink)


def remove_sink(sink: Sink) -> None:
    if sink in _sinks:
        _sinks.remove(sink)


def reset() -> None:
    """Forget sinks, ids and events (tests)."""
    _sinks.clear()
    _seen.clear()
    _recent.clear()
    for key in _totals:
        _totals[key] = 0


def recent(limit: int = RECENT_MAX) -> list[dict[str, Any]]:
    """Newest last; for diagnostics and tests."""
    return list(_recent)[-limit:]


def totals() -> dict[str, int]:
    return dict(_totals)


async def ingest(events: list[dict[str, Any]], dropped: int) -> tuple[int, int]:
    """Take a batch; returns ``(accepted, duplicates)``."""
    new: list[dict[str, Any]] = []
    duplicates = 0
    for event in events:
        event_id = event["id"]
        if event_id in _seen:
            duplicates += 1
            continue
        _seen[event_id] = None
        while len(_seen) > SEEN_IDS_MAX:
            _seen.popitem(last=False)
        clean = {k: v for k, v in event.items() if v is not None}
        new.append(clean)
        _recent.append(clean)
    _totals["accepted"] += len(new)
    _totals["duplicates"] += duplicates
    _totals["dropped"] += dropped
    if dropped:
        logger.warning(
            "The integration lost %d event(s) while the hub was away", dropped
        )
    for event in new:
        logger.info(
            "integration event %s client=%s entity=%s action=%s reason=%s",
            event["type"],
            event.get("client_id"),
            event.get("entity_id"),
            event.get("action"),
            event.get("reason"),
        )
    if new or dropped:
        for sink in list(_sinks):
            try:
                await sink(new, dropped)
            except Exception:  # noqa: BLE001 - one sink must not break the intake
                logger.exception("integration event sink failed")
    return len(new), duplicates


async def emit_ha_event(event: dict[str, Any]) -> bool:
    """Push a hub event to the integration (``ha_event``, fire-and-forget)."""
    from api.ws.broadcast import send_to_integration

    return await send_to_integration({"type": "ha_event", "data": event})
