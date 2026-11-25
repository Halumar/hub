"""Room climate of an area: which entities give its room temperature and humidity.

Port of the contract reference ``tools/room_climate_ref.py`` (rule
``data/room-climate.json``, ``docs/room-climate.md``; the shared vectors in
``tests/fixtures/climate`` run against it). The hub decides with what only
it knows (registry entity category, device name and model, the Home
Assistant area of entity and device) and sends the candidates as
``room_climate`` with every area; the apps compute the value from the live
states. The admin may pick one entity per measure on the area page
(``areas.room_temperature_entity_id`` / ``room_humidity_entity_id``).
"""

from __future__ import annotations

import json
import logging
import math
import unicodedata
from functools import cache
from pathlib import Path
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.services.room_climate")

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "room_climate.json"
MEASURES = ("temperature", "humidity")
COLUMNS = {
    "temperature": "room_temperature_entity_id",
    "humidity": "room_humidity_entity_id",
}
UNKNOWN_STATES = ("unavailable", "unknown", "", None)


@cache
def rule() -> dict:
    """Copy of contracts ``data/room-climate.json``."""
    return json.loads(DATA_FILE.read_text("utf-8"))


# ── text ────────────────────────────────────────────────────────────────────

_FOLD = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def normalize(text: str) -> str:
    """Lower case, umlauts transcribed, other diacritics dropped, other characters -> one space."""
    out = "".join(_FOLD.get(c, c) for c in text.lower())
    out = "".join(
        c for c in unicodedata.normalize("NFKD", out) if not unicodedata.combining(c)
    )
    words: list[str] = []
    word = ""
    for c in out:
        if "a" <= c <= "z" or "0" <= c <= "9":
            word += c
        elif word:
            words.append(word)
            word = ""
    if word:
        words.append(word)
    return " ".join(words)


def excluded_group(texts: list[str | None], data: dict | None = None) -> str | None:
    """Id of the first exclude group one of the texts matches, else None."""
    data = data or rule()
    normalized = [normalize(t) for t in texts if isinstance(t, str) and t]
    for group in data["exclude"]["groups"]:
        for text in normalized:
            padded = " " + text
            if any(k in text for k in group["contains"]):
                return group["id"]
            if any(" " + k in padded for k in group["prefixes"]):
                return group["id"]
    return None


# ── entities (the vector shape: entity_id, display_name, visible, state, registry) ──


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _attributes(entity: dict) -> dict:
    return (entity.get("state") or {}).get("attributes") or {}


def device_class(entity: dict) -> str | None:
    value = _attributes(entity).get("device_class")
    if value is None:
        reg = entity.get("registry") or {}
        value = reg.get("device_class") or reg.get("original_device_class")
    return value if isinstance(value, str) else None


def tier(entity: dict, measure: dict) -> str | None:
    """device (thermostat reporting the attribute), sensor (of the device class) or None."""
    domain = _domain(entity["entity_id"])
    if domain in measure["device_domains"]:
        return "device" if measure["device_attribute"] in _attributes(entity) else None
    if domain == "sensor" and device_class(entity) == measure["sensor_device_class"]:
        return "sensor"
    return None


def _texts(entity: dict) -> list[str | None]:
    reg = entity.get("registry") or {}
    device = reg.get("device") or {}
    return [
        entity["entity_id"],
        entity.get("display_name"),
        _attributes(entity).get("friendly_name"),
        reg.get("name"),
        reg.get("original_name"),
        device.get("name_by_user"),
        device.get("name"),
        device.get("model"),
    ]


def excluded(entity: dict, data: dict | None = None) -> str | None:
    data = data or rule()
    category = (entity.get("registry") or {}).get("entity_category")
    if category in data["exclude"]["entity_categories"]:
        return "entity_category"
    return excluded_group(_texts(entity), data)


def in_room(entity: dict, ha_area_id: str | None) -> bool:
    """The entity, or (without an own area) its device, is in the area's HA area."""
    if not ha_area_id:
        return False
    reg = entity.get("registry") or {}
    area = reg.get("area_id") or (reg.get("device") or {}).get("area_id")
    return area == ha_area_id


def _placed(entities: list[dict], *, visible_only: bool) -> list[dict]:
    seen: set[str] = set()
    out = []
    for e in entities:
        if visible_only and not e.get("visible", True):
            continue
        if e["entity_id"] in seen:
            continue
        seen.add(e["entity_id"])
        out.append(e)
    return out


def auto_ids(
    entities: list[dict],
    measure: dict,
    ha_area_id: str | None,
    data: dict | None = None,
) -> list[str]:
    """Candidates of the rule in layout order (in each tier the in-room ones win)."""
    data = data or rule()
    placed = _placed(entities, visible_only=True)
    tiers: dict[str, list[tuple[dict, bool]]] = {"device": [], "sensor": []}
    for e in placed:
        t = tier(e, measure)
        if t is None or excluded(e, data):
            continue
        tiers[t].append((e, in_room(e, ha_area_id)))
    keep: set[str] = set()
    for members in tiers.values():
        room = [e for e, inside in members if inside]
        keep.update(e["entity_id"] for e in (room or [e for e, _ in members]))
    return [e["entity_id"] for e in placed if e["entity_id"] in keep]


def pick(
    entities: list[dict],
    ha_area_id: str | None,
    admin: dict[str, str | None],
    data: dict | None = None,
) -> dict:
    """``room_climate`` of an area."""
    data = data or rule()
    out = {}
    for name in MEASURES:
        chosen = admin.get(name)
        if chosen:
            out[name] = {"source": "admin", "entity_ids": [chosen]}
        else:
            out[name] = {
                "source": "auto",
                "entity_ids": auto_ids(
                    entities, data["measures"][name], ha_area_id, data
                ),
            }
    return out


def options(
    entities: list[dict], ha_area_id: str | None, name: str, data: dict | None = None
) -> list[dict]:
    """Every placed entity that reports the measure, with why it does not count."""
    data = data or rule()
    measure = data["measures"][name]
    out = []
    for e in _placed(entities, visible_only=False):
        t = tier(e, measure)
        if t is None:
            continue
        out.append(
            {
                "entity_id": e["entity_id"],
                "name": e.get("display_name")
                or _attributes(e).get("friendly_name")
                or e["entity_id"],
                "tier": t,
                "in_room": in_room(e, ha_area_id),
                "excluded": excluded(e, data),
                "value": read(e, measure),
            }
        )
    return out


# ── values ──────────────────────────────────────────────────────────────────


def _number(raw: Any) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def read(entity: dict | None, measure: dict) -> float | None:
    """Value of one entity: the attribute of a thermostat, else the numeric state."""
    if not entity or not entity.get("state"):
        return None
    state = entity["state"]
    if state.get("state") in UNKNOWN_STATES:
        return None
    if _domain(entity["entity_id"]) in measure["device_domains"]:
        return _number((state.get("attributes") or {}).get(measure["device_attribute"]))
    return _number(state.get("state"))


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _tier_values(
    chosen: dict, by_id: dict[str, dict], measure: dict
) -> list[tuple[str, float]]:
    ids = chosen["entity_ids"]
    if chosen["source"] == "admin":
        v = read(by_id.get(ids[0]) if ids else None, measure)
        return [(ids[0], v)] if v is not None else []
    for devices in (True, False):
        found = []
        for i in ids:
            if (_domain(i) in measure["device_domains"]) != devices:
                continue
            v = read(by_id.get(i), measure)
            if v is not None:
                found.append((i, v))
        if found:
            return found
    return []


def value(chosen: dict, entities: list[dict], measure: dict) -> float | None:
    by_id = {e["entity_id"]: e for e in entities}
    return median([v for _, v in _tier_values(chosen, by_id, measure)])


def unit(chosen: dict, entities: list[dict], measure: dict) -> str | None:
    """unit_of_measurement of the first entity the value comes from."""
    by_id = {e["entity_id"]: e for e in entities}
    for entity_id, _ in _tier_values(chosen, by_id, measure):
        attrs = _attributes(by_id[entity_id])
        found = attrs.get("unit_of_measurement") or attrs.get("temperature_unit")
        if isinstance(found, str):
            return found
        if _domain(entity_id) in measure["device_domains"]:
            return "%" if measure["device_attribute"] == "current_humidity" else None
    return None


def primary_sensor(chosen: dict, entities: list[dict], measure: dict) -> str | None:
    """The candidate sensor closest to the room value (ties: the earlier one)."""
    by_id = {e["entity_id"]: e for e in entities}
    room = value(chosen, entities, measure)
    sensors = []
    for i in chosen["entity_ids"]:
        if _domain(i) != "sensor":
            continue
        v = read(by_id.get(i), measure)
        if v is not None:
            sensors.append((i, v))
    if room is None or not sensors:
        return None
    return min(sensors, key=lambda s: abs(s[1] - room))[0]


# ── hub data ────────────────────────────────────────────────────────────────


async def _placements(db: Database, area_ids: list[str]) -> dict[str, list[dict]]:
    """Placements per area in layout order (section order, then entity order)."""
    if not area_ids:
        return {}
    marks = ",".join("?" for _ in area_ids)
    rows = await db.fetchall(
        "SELECT ae.area_id, ae.entity_id, ae.display_name, ae.is_visible "
        "FROM area_entities ae JOIN area_sections s ON s.id = ae.section_id "
        f"WHERE ae.area_id IN ({marks}) "
        "ORDER BY s.sort_order, s.id, ae.sort_order, ae.id",
        tuple(area_ids),
    )
    out: dict[str, list[dict]] = {a: [] for a in area_ids}
    for row in rows:
        out[row["area_id"]].append(
            {
                "entity_id": row["entity_id"],
                "display_name": row["display_name"],
                "visible": bool(row["is_visible"])
                if row["is_visible"] is not None
                else True,
            }
        )
    return out


class _Registry:
    """Entity and device registry, read once per call (cached by the HA mirror)."""

    def __init__(self, entities: list[dict], devices: list[dict]) -> None:
        self.entities = {e.get("entity_id"): e for e in entities if isinstance(e, dict)}
        self.devices = {d.get("id"): d for d in devices if isinstance(d, dict)}

    @classmethod
    async def load(cls, ha_client) -> _Registry:
        async def read(name: str) -> list[dict]:
            try:
                return await getattr(ha_client, name)() or []
            except Exception as exc:  # noqa: BLE001 - registry unreadable: names only
                logger.debug("room climate: %s failed: %s", name, exc)
                return []

        return cls(await read("get_entity_registry"), await read("get_devices"))

    def entry(self, entity_id: str) -> dict | None:
        e = self.entities.get(entity_id)
        if e is None:
            return None
        device = self.devices.get(e.get("device_id")) if e.get("device_id") else None
        return {
            "entity_category": e.get("entity_category"),
            "name": e.get("name"),
            "original_name": e.get("original_name"),
            "device_class": e.get("device_class"),
            "original_device_class": e.get("original_device_class"),
            "area_id": e.get("area_id"),
            "device": None
            if device is None
            else {
                "name": device.get("name"),
                "name_by_user": device.get("name_by_user"),
                "model": device.get("model"),
                "area_id": device.get("area_id"),
            },
        }


async def _state(ha_client, entity_id: str) -> dict | None:
    try:
        data = await ha_client.get_state(entity_id)
    except Exception:  # noqa: BLE001 - unknown state: does not count
        return None
    if not data:
        return None
    return {"state": data.get("state"), "attributes": data.get("attributes") or {}}


async def _entities(
    ha_client,
    placements: list[dict],
    registry: _Registry,
    states: dict[str, dict | None] | None = None,
) -> list[dict]:
    out = []
    for p in placements:
        entity_id = p["entity_id"]
        if states is not None and entity_id in states:
            known = states[entity_id]
            state = (
                None
                if not known
                else {
                    "state": known.get("state"),
                    "attributes": known.get("attributes") or {},
                }
            )
        else:
            state = await _state(ha_client, entity_id)
        out.append({**p, "state": state, "registry": registry.entry(entity_id)})
    return out


def admin_choice(
    area: dict, placements: list[dict] | None = None
) -> dict[str, str | None]:
    """The admin's entity per measure. With ``placements``: only one still
    placed in the area counts (contract: "one entity placed in the area");
    a removed one falls back to automatic."""
    placed = None if placements is None else {p["entity_id"] for p in placements}
    return {
        name: (area.get(col) or None)
        if placed is None or area.get(col) in placed
        else None
        for name, col in COLUMNS.items()
    }


async def forget_placement(db: Database, area_id: str, entity_id: str) -> None:
    """The entity left the area: drop it as the admin's choice. Call where a
    placement is deleted, after the delete."""
    for col in COLUMNS.values():
        await db.execute(
            f"UPDATE areas SET {col} = NULL WHERE id = ? AND {col} = ? "
            "AND NOT EXISTS (SELECT 1 FROM area_entities WHERE area_id = ? "
            "AND entity_id = ?)",
            (area_id, entity_id, area_id, entity_id),
        )


async def for_areas(
    db: Database,
    ha_client,
    areas: list[dict],
    states: dict[str, dict | None] | None = None,
) -> dict[str, dict]:
    """``room_climate`` per area id (one registry read for all of them).

    ``states``: live states already read (get_area), by entity id.
    """
    if not areas:
        return {}
    placements = await _placements(db, [a["id"] for a in areas])
    registry = await _Registry.load(ha_client)
    out = {}
    for area in areas:
        entities = await _entities(
            ha_client, placements.get(area["id"], []), registry, states
        )
        out[area["id"]] = pick(
            entities,
            area.get("ha_area_id"),
            admin_choice(area, placements.get(area["id"], [])),
        )
    return out


async def settings(db: Database, ha_client, area: dict) -> dict:
    """Answer of GET/PUT /api/areas/{id}/room-climate."""
    placements = (await _placements(db, [area["id"]]))[area["id"]]
    entities = await _entities(ha_client, placements, await _Registry.load(ha_client))
    data = rule()
    choice = admin_choice(area, placements)
    chosen = pick(entities, area.get("ha_area_id"), choice, data)
    out = {}
    for name in MEASURES:
        measure = data["measures"][name]
        out[name] = {
            "entity_id": choice[name],
            "pick": chosen[name],
            "value": value(chosen[name], entities, measure),
            "unit": unit(chosen[name], entities, measure),
            "options": options(entities, area.get("ha_area_id"), name, data)[:200],
        }
    return out


def strip_columns(area: dict) -> None:
    """The admin's columns stay internal; apps get ``room_climate``."""
    for col in COLUMNS.values():
        area.pop(col, None)
