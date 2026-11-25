"""History and weather forecast for devices on the hub path.

A device on the hub path (``services.hub_control``) may hold no HA key, so
the sensor charts (recorder history) and the weather forecast
(``weather.get_forecasts``) come from the hub:

* ``history_get {request_id, entity_id, hours}`` → ``history``
* ``forecast_get {request_id, entity_id, forecast_type}`` → ``forecast``

Same scope as ``state_subscribe`` (:func:`services.hub_control.visible_entities`:
placed in one of the device's readable areas, or the run target of such a
tile; nothing while its access is closed). Both together at most 10 per 10 s
and device (``control_error {rate_limited, retry_after}``). Devices off the
hub path get ``not_hub_controlled``. The hub reads with its own connection
to Home Assistant; nothing is written. Failures answer ``control_error``
with the request's ``request_id``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from models.database import Database
from services import hub_control
from services.ha_client import is_valid_entity_id

logger = logging.getLogger("hub.services.hub_read")

HOURS_MIN = 1
HOURS_MAX = 168
HISTORY_MAX_POINTS = 1000
STATE_MAX_LENGTH = 255
FORECAST_MAX = 240
FORECAST_TYPES = ("daily", "hourly", "twice_daily")
# WeatherEntityFeature of Home Assistant.
FORECAST_FEATURE = {"daily": 1, "hourly": 2, "twice_daily": 4}


class ReadFailure(Exception):
    def __init__(self, reason: str, retry_after: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


request_id_of = hub_control.request_id_of


async def _check(
    db: Database, client_id: str, message: dict[str, Any]
) -> tuple[str, str]:
    """``(request_id, entity_id)`` of a valid request for a visible entity."""
    if not await hub_control.uses_hub_path(db, client_id):
        raise ReadFailure(hub_control.ERR_NOT_HUB_CONTROLLED)
    # Every request counts, also a refused one (flooding).
    wait = hub_control.read_retry_after(client_id)
    if wait is not None:
        raise ReadFailure(hub_control.ERR_RATE_LIMITED, wait)
    rid = request_id_of(message)
    entity_id = message.get("entity_id")
    if rid is None or not is_valid_entity_id(entity_id):
        raise ReadFailure(hub_control.ERR_INVALID_REQUEST)
    if entity_id not in await hub_control.visible_entities(db, client_id):
        raise ReadFailure(hub_control.ERR_NOT_VISIBLE)
    return rid, entity_id


def _iso(ts: Any) -> str | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def history_points(rows: list[dict]) -> tuple[list[dict[str, str]], bool]:
    """Compressed recorder rows → ``[{state, last_changed}]`` oldest first,
    evenly thinned out to :data:`HISTORY_MAX_POINTS` (first and last kept)."""
    points: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("s"), str):
            continue
        changed = _iso(row.get("lc", row.get("lu")))
        if changed is None:
            continue
        points.append({"state": row["s"][:STATE_MAX_LENGTH], "last_changed": changed})
    n = len(points)
    if n <= HISTORY_MAX_POINTS:
        return points, False
    m = HISTORY_MAX_POINTS
    return [points[round(i * (n - 1) / (m - 1))] for i in range(m)], True


def forecast_entries(entries: list[Any]) -> list[dict[str, Any]]:
    return [
        e for e in entries if isinstance(e, dict) and isinstance(e.get("datetime"), str)
    ][:FORECAST_MAX]


def _error(request_id: str | None, f: ReadFailure) -> dict[str, Any]:
    return hub_control.error_message(request_id, f.reason, f.retry_after)


async def history(
    db: Database, ha_client, client_id: str, message: dict[str, Any], now=None
) -> dict[str, Any]:
    """Answer to ``history_get`` (``history`` or ``control_error``)."""
    try:
        rid, entity_id = await _check(db, client_id, message)
        hours = message.get("hours")
        if (
            isinstance(hours, bool)
            or not isinstance(hours, int)
            or not HOURS_MIN <= hours <= HOURS_MAX
        ):
            raise ReadFailure(hub_control.ERR_INVALID_REQUEST)
        if ha_client is None:
            raise ReadFailure(hub_control.ERR_HA_UNAVAILABLE)
        end = now or datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        try:
            rows = await ha_client.get_history(
                entity_id, start.isoformat(), end.isoformat()
            )
        except Exception as e:  # noqa: BLE001 - unreachable or refused
            if hub_control.unreachable(e):
                logger.info(
                    "History of %s: Home Assistant not reachable (%s)", entity_id, e
                )
                raise ReadFailure(hub_control.ERR_HA_UNAVAILABLE) from e
            logger.warning("History of %s failed: %s", entity_id, e)
            raise ReadFailure(hub_control.ERR_HA_ERROR) from e
        points, truncated = history_points(rows)
        return {
            "type": "history",
            "request_id": rid,
            "entity_id": entity_id,
            "hours": hours,
            "states": points,
            "truncated": truncated,
        }
    except ReadFailure as f:
        return _error(request_id_of(message), f)


async def forecast(
    db: Database, ha_client, client_id: str, message: dict[str, Any]
) -> dict[str, Any]:
    """Answer to ``forecast_get`` (``forecast`` or ``control_error``)."""
    try:
        rid, entity_id = await _check(db, client_id, message)
        kind = message.get("forecast_type")
        if not entity_id.startswith("weather.") or kind not in FORECAST_TYPES:
            raise ReadFailure(hub_control.ERR_INVALID_REQUEST)
        if ha_client is None:
            raise ReadFailure(hub_control.ERR_HA_UNAVAILABLE)
        try:
            state = await ha_client.get_state(entity_id)
        except Exception as e:  # noqa: BLE001 - HA not reachable
            raise ReadFailure(hub_control.ERR_HA_UNAVAILABLE) from e
        if not state:
            raise ReadFailure(hub_control.ERR_HA_UNAVAILABLE)
        features = (state.get("attributes") or {}).get("supported_features") or 0
        if not isinstance(features, int) or not features & FORECAST_FEATURE[kind]:
            raise ReadFailure(hub_control.ERR_INVALID_REQUEST)
        try:
            entries = await ha_client.get_forecasts(entity_id, kind)
        except Exception as e:  # noqa: BLE001 - unreachable or refused
            if hub_control.unreachable(e):
                logger.info(
                    "Forecast of %s: Home Assistant not reachable (%s)", entity_id, e
                )
                raise ReadFailure(hub_control.ERR_HA_UNAVAILABLE) from e
            logger.warning("Forecast of %s failed: %s", entity_id, e)
            raise ReadFailure(hub_control.ERR_HA_ERROR) from e
        return {
            "type": "forecast",
            "request_id": rid,
            "entity_id": entity_id,
            "forecast_type": kind,
            "forecast": forecast_entries(entries),
        }
    except ReadFailure as f:
        return _error(request_id_of(message), f)
