"""Quick access for widgets, controls and Siri.

The admin decides per entity where it may appear outside the app:
``off`` (nowhere), ``status`` (state shown, tapping opens the app) or
``control`` (switch without opening the app). Defaults, maxima and the
critical rules per domain come from the contract data file
``data/quick_access.json`` (byte-identical copy of contracts
``data/quick-access.json``).

* :func:`entity_rule` / :func:`effective_level` – the per-entity rule
  (admin level or domain default, capped by the domain max; critical
  entities start at ``status`` and reach ``control`` only with the admin's
  ``critical_ack``).
* :func:`resolve_for_client` – ``client_config.quick_access`` of one device:
  only entities in its area list (``authz.listed_area_ids``, ``is_visible``),
  capped by the effective profile rule ``quick_access``
  (template, then device). Guests: always disabled.
* :func:`sync_sensors` – the check sensor per device
  (``sensor.<ns>_client_<slug>_<uid8>_quick_access``). The app's widgets read
  it with the device's HA token before every action,
  so a revocation acts on the next tap even while the app is closed. Written
  only when the content changed (``force`` after a Home Assistant restart);
  sensors of removed devices are deleted.
* :func:`notify_change` – called by every WebSocket broadcast: schedules a
  debounced :func:`sync_sensors`, because the effective levels depend on
  areas, tags, visibility, profiles and pairing.

The levels are a care boundary, not a security boundary (the device's HA
user may call every service): locking a device (HA user deleted,
401) is the immediate way to take everything away.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import cache
from pathlib import Path
from typing import Any

from api import authz
from brand import BRAND_NAME
from models.database import Database

logger = logging.getLogger("hub.quick_access")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LEVELS = ("off", "status", "control")
DEVICE_MAX_LEVELS = ("status", "control")
_ORDER = {level: i for i, level in enumerate(LEVELS)}
# Upper bound of entities per device (client_config and sensor, contract maxProperties).
MAX_ENTITIES = 500
SYNC_DEBOUNCE_SECONDS = 2.0
# Broadcast types that never change a device's quick access.
_IGNORED_TYPES = frozenset(
    {
        "ping",
        "pong",
        "client_status",
        "client_status_ok",
        "client_status_updated",
        "client_connected",
        "client_disconnected",
        "client_activity",
        "guest_invite_opened",
        "guest_invite_closed",
        "onboarding_started",
        "onboarding_ended",
        "log_entry",
        "log_read",
    }
)


@cache
def data() -> dict:
    return json.loads((DATA_DIR / "quick_access.json").read_text("utf-8"))


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def rule_row(domain: str) -> dict:
    rows = data()["domains"]
    for row in rows:
        if domain in row["domains"]:
            return row
    return next(row for row in rows if "_other" in row["domains"])


def lower(a: str, b: str) -> str:
    return a if _ORDER[a] <= _ORDER[b] else b


def needs_device_class(entity_id: str) -> bool:
    """Whether the critical rule depends on the entity's device_class (cover)."""
    row = rule_row(domain_of(entity_id))
    return not row["critical"] and bool(row["critical_device_classes"])


def is_critical(
    entity_id: str, device_class: str | None, *, known: bool = True
) -> bool:
    """Critical domain, or a critical device class (garage/gate/door cover).

    ``known=False`` (Home Assistant not reachable, device_class unknown) counts
    as critical for domains with critical device classes: fail closed.
    """
    row = rule_row(domain_of(entity_id))
    if row["critical"]:
        return True
    classes = row["critical_device_classes"]
    if not classes:
        return False
    if not known:
        return True
    return device_class in classes


def entity_rule(
    entity_id: str, device_class: str | None, *, known: bool = True
) -> dict:
    """``default``, ``max`` and ``critical`` of one entity."""
    row = rule_row(domain_of(entity_id))
    critical = is_critical(entity_id, device_class, known=known)
    default = row["default"]
    if critical:
        default = lower(default, "status")
    return {"default": default, "max": row["max"], "critical": critical}


def effective_level(
    entity_id: str,
    device_class: str | None,
    stored: dict | None,
    *,
    known: bool = True,
) -> str:
    """Admin level (or default), capped by the domain max and the critical rule."""
    rule = entity_rule(entity_id, device_class, known=known)
    level = (stored or {}).get("level") or rule["default"]
    if level not in _ORDER:
        level = rule["default"]
    level = lower(level, rule["max"])
    if (
        rule["critical"]
        and level == "control"
        and not (stored or {}).get("critical_ack")
    ):
        level = "status"
    return level


class QuickAccessError(Exception):
    """422 detail of a rejected PUT (``type`` = ``quick_access.<reason>``)."""

    def __init__(self, reason: str, msg: str):
        super().__init__(msg)
        self.detail = [
            {"loc": ["body", "level"], "msg": msg, "type": f"quick_access.{reason}"}
        ]


def check_update(
    entity_id: str,
    device_class: str | None,
    level: str | None,
    critical_ack: bool,
    *,
    known: bool = True,
) -> None:
    """Raise :class:`QuickAccessError` for a level the admin may not set."""
    if level is None:
        return
    rule = entity_rule(entity_id, device_class, known=known)
    if _ORDER[level] > _ORDER[rule["max"]]:
        raise QuickAccessError(
            "not_allowed", f"{domain_of(entity_id)} allows at most {rule['max']}"
        )
    if rule["critical"] and level == "control" and not critical_ack:
        raise QuickAccessError(
            "critical_ack_required",
            "control of a critical entity needs critical_ack: true",
        )


# ── storage ──────────────────────────────────────────────────────────────────


async def stored_levels(db: Database) -> dict[str, dict]:
    rows = await db.fetchall(
        "SELECT entity_id, level, critical_ack, updated_at, updated_by FROM quick_access"
    )
    return {r["entity_id"]: dict(r) for r in rows}


async def store_level(
    db: Database,
    entity_id: str,
    level: str | None,
    critical_ack: bool,
    updated_by: str | None,
) -> None:
    async with db.transaction():
        if level is None:
            await db.execute(
                "DELETE FROM quick_access WHERE entity_id = ?", (entity_id,)
            )
            return
        await db.execute(
            "INSERT INTO quick_access (entity_id, level, critical_ack, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET "
            "level = excluded.level, critical_ack = excluded.critical_ack, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (entity_id, level, 1 if critical_ack else 0, Database.now(), updated_by),
        )


async def device_classes(
    ha_client, entity_ids: set[str]
) -> tuple[dict[str, str | None], bool]:
    """device_class of the entities whose critical rule needs it.

    Returns ``(classes, known)``; ``known`` is False when Home Assistant could
    not be asked (those entities then count as critical, fail closed).
    """
    classes: dict[str, str | None] = {}
    known = True
    for entity_id in sorted(e for e in entity_ids if needs_device_class(e)):
        if ha_client is None:
            known = False
            break
        try:
            state = await ha_client.get_state(entity_id)
        except Exception as exc:  # noqa: BLE001 - HA down: fail closed
            logger.debug("quick access: device_class of %s unknown: %s", entity_id, exc)
            known = False
            break
        classes[entity_id] = ((state or {}).get("attributes") or {}).get("device_class")
    return classes, known


# ── per device ───────────────────────────────────────────────────────────────


async def _visible_entities(
    db: Database, client_id: str, *, ignore_access: bool = False
) -> list[str]:
    area_ids = await authz.listed_area_ids(db, client_id, ignore_access=ignore_access)
    if not area_ids:
        return []
    rows = await db.fetchall(
        "SELECT ae.entity_id, ae.area_id FROM area_entities ae "
        "JOIN areas a ON a.id = ae.area_id "
        "WHERE ae.is_visible = 1 AND a.is_active = 1 "
        "ORDER BY a.sort_order, a.name, ae.sort_order"
    )
    seen: dict[str, None] = {}
    for row in rows:
        if row["area_id"] in area_ids:
            seen.setdefault(row["entity_id"], None)
    return list(seen)


def disabled(device_max: str = "status") -> dict:
    return {
        "enabled": False,
        "max": device_max,
        "sensor": None,
        "entities": {},
        "critical": [],
    }


async def resolve_for_client(
    db: Database,
    ha_client,
    client: dict,
    *,
    stored: dict[str, dict] | None = None,
    classes: tuple[dict[str, str | None], bool] | None = None,
    effective: dict | None = None,
    ignore_access: bool = False,
) -> dict:
    """``client_config.quick_access`` of ``client`` (a ``paired_clients`` row).

    Outside its access a device has no entities unless
    ``ignore_access`` (rule snapshot: the integration decides access itself).

    The device level is the effective profile rule ``quick_access``:
    off (also guests and parental controls, boundaries) = disabled,
    status/control = the device maximum.
    """
    from services.ha_sensors import client_quick_access_sensor_id
    from services.profiles import store

    if effective is None:
        resolved = await store.resolve_client(db, client["id"])
        effective = resolved[2] if resolved else {}
    level = (effective.get("quick_access") or {}).get("value")
    if client.get("access") == "guest":
        return disabled()
    enabled = level in DEVICE_MAX_LEVELS
    device_max = level if enabled else "status"
    sensor = client_quick_access_sensor_id(client.get("name") or "", client["id"])
    if (
        not enabled
        or not client.get("is_active", 1)
        or client.get("pending_deletion_at")
    ):
        return {**disabled(device_max), "sensor": sensor}
    entity_ids = await _visible_entities(db, client["id"], ignore_access=ignore_access)
    if stored is None:
        stored = await stored_levels(db)
    if classes is None:
        classes = await device_classes(ha_client, set(entity_ids))
    class_of, known = classes
    entities: dict[str, str] = {}
    critical: list[str] = []
    for entity_id in entity_ids:
        dc = class_of.get(entity_id)
        level = lower(
            effective_level(entity_id, dc, stored.get(entity_id), known=known),
            device_max,
        )
        if level == "off":
            continue
        entities[entity_id] = level
        if is_critical(entity_id, dc, known=known):
            critical.append(entity_id)
        if len(entities) >= MAX_ENTITIES:
            break
    return {
        "enabled": True,
        "max": device_max,
        "sensor": sensor,
        "entities": entities,
        "critical": critical,
    }


# ── admin list ───────────────────────────────────────────────────────────────


async def admin_items(db: Database, ha_client, only: str | None = None) -> list[dict]:
    """Rows of ``GET /api/quick-access`` (every placed entity + every stored one)."""
    stored = await stored_levels(db)
    placed = await db.fetchall("SELECT DISTINCT entity_id FROM area_entities")
    ids = sorted({r["entity_id"] for r in placed} | set(stored))
    if only is not None:
        ids = [only]
    names: dict[str, str] = {}
    class_of: dict[str, str | None] = {}
    known = True
    try:
        for state in await ha_client.get_states():
            eid = state.get("entity_id", "")
            attrs = state.get("attributes") or {}
            names[eid] = attrs.get("friendly_name") or eid
            class_of[eid] = attrs.get("device_class")
    except Exception as exc:  # noqa: BLE001 - list works without HA (critical: fail closed)
        logger.info("quick access list without Home Assistant: %s", exc)
        known = False
    items = []
    for entity_id in ids:
        row = stored.get(entity_id) or {}
        dc = class_of.get(entity_id)
        rule = entity_rule(entity_id, dc, known=known)
        items.append(
            {
                "entity_id": entity_id,
                "name": names.get(entity_id),
                "level": row.get("level"),
                "default": rule["default"],
                "max": rule["max"],
                "critical": rule["critical"],
                "effective": effective_level(entity_id, dc, row or None, known=known),
                "critical_ack": bool(row.get("critical_ack")),
                "updated_at": row.get("updated_at"),
                "updated_by": row.get("updated_by"),
            }
        )
    return items


# ── check sensors ────────────────────────────────────────────────────────────

# client_id -> (sensor entity id, written content); what this process wrote.
_written: dict[str, tuple[str, str]] = {}
_sync_task: asyncio.Task | None = None
_again = False
_sync_lock = asyncio.Lock()
_context: dict[str, Any] = {}


def register(db: Database, ha_client) -> None:
    """Database and HA client for the debounced sync (set at startup)."""
    _context["db"] = db
    _context["ha_client"] = ha_client


def ha_client():
    """The registered HA client (``None`` before startup and in tests)."""
    return _context.get("ha_client")


def reset_state() -> None:
    """Forget what was written (tests)."""
    global _sync_task, _again
    _again = False
    _written.clear()
    _context.clear()
    _sync_task = None


def _sensor_payload(client: dict, qa: dict) -> dict:
    controls = sum(1 for level in qa["entities"].values() if level == "control")
    return {
        "state": str(controls),
        "attributes": {
            "friendly_name": f"{BRAND_NAME}: {client.get('name') or client['id']} Quick access",
            "icon": "mdi:widgets" if qa["enabled"] else "mdi:widgets-outline",
            "client_id": client["id"],
            "enabled": qa["enabled"],
            "max": qa["max"],
            "entities": qa["entities"],
            "critical": qa["critical"],
            "updated_at": Database.now(),
        },
    }


async def sync_sensors(db: Database, ha_client, *, force: bool = False) -> int:
    """Write the check sensor of every paired member device; returns writes.

    Guests get none (they never have quick access; a missing sensor means
    "not allowed"). Removed or revoked devices lose theirs.
    """
    if ha_client is None:
        return 0
    async with _sync_lock:
        writes = 0
        try:
            clients = await db.fetchall(
                "SELECT * FROM paired_clients WHERE access = 'member' "
                "AND is_active = 1 AND pending_deletion_at IS NULL"
            )
            stored = await stored_levels(db)
            all_ids: set[str] = set()
            visible: dict[str, list[str]] = {}
            for client in clients:
                visible[client["id"]] = await _visible_entities(db, client["id"])
                all_ids.update(visible[client["id"]])
            classes = await device_classes(ha_client, all_ids)
            current: set[str] = set()
            for client in clients:
                qa = await resolve_for_client(
                    db, ha_client, client, stored=stored, classes=classes
                )
                sensor = qa["sensor"]
                current.add(client["id"])
                content = json.dumps(
                    [qa["enabled"], qa["max"], qa["entities"], qa["critical"]],
                    sort_keys=True,
                )
                previous = _written.get(client["id"])
                if previous and previous[0] != sensor:
                    await ha_client._delete(
                        f"http://supervisor/core/api/states/{previous[0]}"
                    )
                if not force and previous == (sensor, content):
                    continue
                try:
                    await ha_client._post(
                        f"http://supervisor/core/api/states/{sensor}",
                        _sensor_payload(client, qa),
                    )
                except Exception as exc:  # noqa: BLE001 - one refused sensor must not stop the others
                    logger.error(
                        "Failed to write quick access sensor %s: %s", sensor, exc
                    )
                    continue
                _written[client["id"]] = (sensor, content)
                writes += 1
            for client_id in [c for c in _written if c not in current]:
                sensor, _ = _written.pop(client_id)
                await ha_client._delete(f"http://supervisor/core/api/states/{sensor}")
                writes += 1
        except Exception as exc:  # noqa: BLE001 - sensors are best effort, next change retries
            logger.error("Failed to sync quick access sensors: %s", exc)
        if writes:
            logger.info("Synced %d quick access sensor(s)", writes)
        return writes


async def _debounced_sync() -> None:
    global _again
    while True:
        _again = False
        await asyncio.sleep(SYNC_DEBOUNCE_SECONDS)
        db, ha_client = _context.get("db"), _context.get("ha_client")
        if db is not None and ha_client is not None:
            await sync_sensors(db, ha_client)
        if not _again:
            return


def notify_change(message_type: str | None) -> None:
    """A broadcast went out: resync the check sensors soon (debounced)."""
    global _sync_task, _again
    if message_type in _IGNORED_TYPES or (message_type or "").startswith(
        "notification"
    ):
        return
    if not _context:
        return
    if _sync_task is not None and not _sync_task.done():
        _again = True
        return
    try:
        _sync_task = asyncio.get_running_loop().create_task(_debounced_sync())
    except RuntimeError:
        _sync_task = None
