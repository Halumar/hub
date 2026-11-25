"""Room climate of an area (services/room_climate.py, api/room_climate.py).

Every vector of the contract (fixtures/climate/room-climate-vector, copied
1:1 into tests/fixtures/climate) against the port of the reference, the
area page API (GET/PUT, 404, 422, push) and ``room_climate`` in get_areas /
get_area for the apps.
"""

import json
import types
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from api import room_climate as api
from api import ws
from api.ws.client import _get_area_detail, _get_areas
from models.database import Database
from services import room_climate as rc

VECTORS = Path(__file__).parent / "fixtures" / "climate" / "room-climate-vector"


def _vectors():
    return [json.loads(p.read_text("utf-8")) for p in sorted(VECTORS.glob("*.json"))]


def _r(v):
    return None if v is None else round(v, 4)


def test_contract_vectors():
    vectors = _vectors()
    assert len(vectors) >= 15
    data = rc.rule()
    for v in vectors:
        exp = v["expect"]
        assert (
            rc.pick(v["entities"], v.get("ha_area_id"), v.get("admin") or {}, data)
            == exp["room_climate"]
        ), v["name"]
        for m in rc.MEASURES:
            measure = data["measures"][m]
            chosen = exp["room_climate"][m]
            assert _r(rc.value(chosen, v["entities"], measure)) == _r(
                exp["value"][m]
            ), (
                v["name"],
                m,
            )
            assert (
                rc.primary_sensor(chosen, v["entities"], measure)
                == exp["primary_sensor"][m]
            ), (v["name"], m)
        for m, opts in (exp.get("options") or {}).items():
            got = [
                {k: o[k] for k in ("entity_id", "tier", "in_room", "excluded")}
                for o in rc.options(v["entities"], v.get("ha_area_id"), m, data)
            ]
            assert got == opts, (v["name"], m)


def test_rule_is_the_contract_copy():
    data = rc.rule()
    assert data["version"] >= 1
    ids = [g["id"] for g in data["exclude"]["groups"]]
    assert {
        "fridge",
        "freezer",
        "outdoor",
        "pool",
        "boiler",
        "water",
        "processor",
    } <= set(ids)
    for group in data["exclude"]["groups"]:
        for word in group["contains"] + group["prefixes"]:
            assert rc.normalize(word) == word


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ("Kühlschrank", "fridge"),
        ("Weinkühlschrank Keller", "fridge"),
        ("sensor.kuhlschrank_temperatur", "fridge"),
        ("Tiefkühler", "freezer"),
        ("Kühlbox", "cool_box"),
        ("Außentemperatur", "outdoor"),
        ("Pool temp", "pool"),
        ("Warmwasser Speicher", "boiler"),
        ("Wassertemperatur", "water"),
        ("NAS CPU", "processor"),
        ("Haussensor", None),
        ("Wohnzimmer", None),
        ("Klimaanlage Kühlung", None),
        ("Spannung", None),
    ],
)
def test_exclude_groups(text, group):
    assert rc.excluded_group([text]) == group


# ── hub ─────────────────────────────────────────────────────────────────────


class FakeHA:
    def __init__(self, states=None, entities=None, devices=None):
        self.states = states or {}
        self.entities = entities or []
        self.devices = devices or []

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def get_entity_registry(self):
        return self.entities

    async def get_devices(self):
        return self.devices


def _temp(value, name, **attrs):
    return {
        "state": value,
        "attributes": {
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "friendly_name": name,
            **attrs,
        },
    }


def _hum(value, name):
    return {
        "state": value,
        "attributes": {
            "device_class": "humidity",
            "unit_of_measurement": "%",
            "friendly_name": name,
        },
    }


KITCHEN_STATES = {
    "light.kitchen": {"state": "on", "attributes": {"friendly_name": "Licht"}},
    "sensor.sensor_1_temperature": _temp("4.2", "Sensor 1 Temperatur"),
    "sensor.kitchen_temperature": _temp("21.4", "Raumtemperatur"),
    "sensor.kitchen_humidity": _hum("55", "Luftfeuchte"),
    "climate.kitchen": {"state": "off", "attributes": {"friendly_name": "Heizung"}},
}
KITCHEN_REGISTRY = [
    {
        "entity_id": "sensor.sensor_1_temperature",
        "device_id": "d-fridge",
        "area_id": None,
    },
    {
        "entity_id": "sensor.kitchen_temperature",
        "device_id": "d-thermo",
        "area_id": None,
    },
    {
        "entity_id": "sensor.kitchen_humidity",
        "device_id": "d-thermo",
        "area_id": None,
        "entity_category": None,
    },
]
KITCHEN_DEVICES = [
    {"id": "d-fridge", "name": "Kühlschrank", "area_id": "kitchen"},
    {"id": "d-thermo", "name": "Thermometer", "area_id": "kitchen"},
]


async def _kitchen(db, entities=None):
    now = Database.now()
    area_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, ha_area_id, created_at, "
        "updated_at) VALUES (?, ?, 0, 1, 'kitchen', ?, ?)",
        (area_id, f"Küche {area_id[:4]}", now, now),
    )
    section = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, "
        "columns) VALUES (?, ?, '', 0, 1, 2)",
        (section, area_id),
    )
    for order, entity_id in enumerate(entities or list(KITCHEN_STATES)):
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, "
            "sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, entity_id, section, order, now),
        )
    await db.commit()
    return area_id


def _ha():
    return FakeHA(KITCHEN_STATES, KITCHEN_REGISTRY, KITCHEN_DEVICES)


def _req(db, ha):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
        client=None,
    )


@pytest.fixture
def pushed(monkeypatch):
    log = []

    async def _broadcast(message, *args, **kwargs):
        log.append(message)

    async def _scoped(db, area_id, message):
        log.append(message)
        return 1

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(ws, "broadcast_area_scoped", _scoped)
    return log


async def test_get_area_sends_room_climate_without_the_columns(db):
    area_id = await _kitchen(db)
    area = await _get_area_detail(db, _ha(), area_id)
    assert area["room_climate"] == {
        "temperature": {"source": "auto", "entity_ids": ["sensor.kitchen_temperature"]},
        "humidity": {"source": "auto", "entity_ids": ["sensor.kitchen_humidity"]},
    }
    assert "room_temperature_entity_id" not in area
    assert "room_humidity_entity_id" not in area


async def test_get_areas_sends_room_climate_per_area(db):
    first = await _kitchen(db)
    second = await _kitchen(db, ["light.kitchen", "sensor.sensor_1_temperature"])
    areas = {a["id"]: a for a in await _get_areas(db, None, ha_client=_ha())}
    assert areas[first]["room_climate"]["temperature"]["entity_ids"] == [
        "sensor.kitchen_temperature"
    ]
    # Only the fridge: no room temperature at all.
    assert areas[second]["room_climate"]["temperature"] == {
        "source": "auto",
        "entity_ids": [],
    }
    assert all("room_temperature_entity_id" not in a for a in areas.values())


async def test_get_areas_without_ha_client_leaves_it_out(db):
    await _kitchen(db)
    (area,) = await _get_areas(db, None)
    assert "room_climate" not in area
    assert "room_humidity_entity_id" not in area


async def test_registry_unreadable_falls_back_to_names(db):
    class Broken(FakeHA):
        async def get_entity_registry(self):
            raise RuntimeError("down")

    area_id = await _kitchen(db)
    area = await _get_area_detail(db, Broken(KITCHEN_STATES), area_id)
    # Without the device name the fridge counts (its own name does not say so).
    assert area["room_climate"]["temperature"]["entity_ids"] == [
        "sensor.sensor_1_temperature",
        "sensor.kitchen_temperature",
    ]


async def test_settings_lists_options_with_reasons(db):
    area_id = await _kitchen(db)
    answer = await api.get_room_climate(_req(db, _ha()), area_id)
    t = answer["temperature"]
    assert t["entity_id"] is None
    assert t["pick"] == {"source": "auto", "entity_ids": ["sensor.kitchen_temperature"]}
    assert t["value"] == 21.4 and t["unit"] == "°C"
    assert [(o["entity_id"], o["excluded"], o["in_room"]) for o in t["options"]] == [
        ("sensor.sensor_1_temperature", "fridge", True),
        ("sensor.kitchen_temperature", None, True),
    ]
    assert t["options"][1]["name"] == "Raumtemperatur"
    assert answer["humidity"]["value"] == 55.0


async def test_put_stores_pushes_and_apps_follow(db, pushed):
    area_id = await _kitchen(db)
    ha = _ha()
    answer = await api.put_room_climate(
        _req(db, ha),
        area_id,
        api.RoomClimateBody(
            temperature_entity_id="sensor.sensor_1_temperature", humidity_entity_id=None
        ),
    )
    assert answer["temperature"]["entity_id"] == "sensor.sensor_1_temperature"
    assert answer["temperature"]["pick"] == {
        "source": "admin",
        "entity_ids": ["sensor.sensor_1_temperature"],
    }
    assert answer["temperature"]["value"] == 4.2
    assert pushed == [{"type": "area_updated", "data": {"area_id": area_id}}]
    area = await _get_area_detail(db, ha, area_id)
    assert area["room_climate"]["temperature"]["source"] == "admin"
    # Same choice again: nothing pushed.
    pushed.clear()
    await api.put_room_climate(
        _req(db, ha),
        area_id,
        api.RoomClimateBody(
            temperature_entity_id="sensor.sensor_1_temperature", humidity_entity_id=None
        ),
    )
    assert pushed == []
    # Back to automatic.
    answer = await api.put_room_climate(
        _req(db, ha),
        area_id,
        api.RoomClimateBody(temperature_entity_id=None, humidity_entity_id=None),
    )
    assert answer["temperature"]["pick"]["source"] == "auto"
    row = await db.fetchone(
        "SELECT room_temperature_entity_id FROM areas WHERE id = ?", (area_id,)
    )
    assert row["room_temperature_entity_id"] is None


@pytest.mark.parametrize(
    ("temperature", "humidity", "measure"),
    [
        ("light.kitchen", None, "temperature"),  # reports no temperature
        ("sensor.elsewhere", None, "temperature"),  # not placed here
        (None, "sensor.kitchen_temperature", "humidity"),  # wrong measure
        (
            "climate.kitchen",
            None,
            "temperature",
        ),  # thermostat without current_temperature
    ],
)
async def test_put_refuses_other_entities(db, pushed, temperature, humidity, measure):
    area_id = await _kitchen(db)
    with pytest.raises(HTTPException) as err:
        await api.put_room_climate(
            _req(db, _ha()),
            area_id,
            api.RoomClimateBody(
                temperature_entity_id=temperature, humidity_entity_id=humidity
            ),
        )
    assert err.value.status_code == 422
    assert err.value.detail["code"] == "room_climate_invalid"
    assert err.value.detail["measure"] == measure
    assert pushed == []
    row = await db.fetchone(
        "SELECT room_temperature_entity_id, room_humidity_entity_id FROM areas WHERE id = ?",
        (area_id,),
    )
    assert dict(row) == {
        "room_temperature_entity_id": None,
        "room_humidity_entity_id": None,
    }


async def test_unknown_area_is_404(db):
    with pytest.raises(HTTPException) as err:
        await api.get_room_climate(_req(db, _ha()), "missing")
    assert err.value.status_code == 404


def test_body_is_strict():
    with pytest.raises(ValueError):
        api.RoomClimateBody(temperature_entity_id="Bad Id", humidity_entity_id=None)
    with pytest.raises(ValueError):
        api.RoomClimateBody.model_validate({"temperature_entity_id": None})
    with pytest.raises(ValueError):
        api.RoomClimateBody.model_validate(
            {"temperature_entity_id": None, "humidity_entity_id": None, "x": 1}
        )


async def test_choice_of_an_entity_no_longer_placed_falls_back_to_auto(db):
    """The admin's sensor left the area: automatic again, and
    forget_placement clears the column."""
    area_id = await _kitchen(db)
    await db.execute(
        "UPDATE areas SET room_temperature_entity_id = 'sensor.kitchen_temperature' "
        "WHERE id = ?",
        (area_id,),
    )
    await db.execute(
        "DELETE FROM area_entities WHERE area_id = ? AND entity_id = ?",
        (area_id, "sensor.kitchen_temperature"),
    )
    await db.commit()
    area = dict(await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,)))
    out = await rc.for_areas(db, _ha(), [area])
    assert out[area_id]["temperature"]["source"] == "auto"
    view = await rc.settings(db, _ha(), area)
    assert view["temperature"]["entity_id"] is None
    await rc.forget_placement(db, area_id, "sensor.kitchen_temperature")
    await db.commit()
    row = await db.fetchone(
        "SELECT room_temperature_entity_id FROM areas WHERE id = ?", (area_id,)
    )
    assert row["room_temperature_entity_id"] is None


@pytest.mark.parametrize("how", ["remove_entity", "delete_section", "import"])
async def test_removing_the_placement_forgets_the_choice(db, pushed, monkeypatch, how):
    """Every path that deletes a placement clears the
    admin's room climate choice for it (remove entity, delete section, an
    import that replaces the area without it)."""
    import api.areas as areas_api
    import api.export as export_api
    from models.schemas import ImportRequest

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(areas_api, "_sync_sensors", _noop)
    monkeypatch.setattr(export_api, "sync_all_sensors", _noop)
    area_id = await _kitchen(db)
    await db.execute(
        "UPDATE areas SET room_temperature_entity_id = 'sensor.kitchen_temperature', "
        "room_humidity_entity_id = 'sensor.kitchen_humidity' WHERE id = ?",
        (area_id,),
    )
    await db.commit()
    req = _req(db, _ha())
    if how == "remove_entity":
        await areas_api.remove_entity_from_area(
            req, area_id, "sensor.kitchen_temperature"
        )
        expected = (None, "sensor.kitchen_humidity")
    elif how == "delete_section":
        section = await db.fetchone(
            "SELECT section_id FROM area_entities WHERE area_id = ? LIMIT 1", (area_id,)
        )
        await areas_api.delete_section(req, area_id, section["section_id"])
        expected = (None, None)
    else:
        name = (await db.fetchone("SELECT name FROM areas WHERE id = ?", (area_id,)))[
            "name"
        ]
        keep = {
            "entity_id": "sensor.kitchen_humidity",
            "columns": 1,
            "rows": 1,
        }
        data = ImportRequest(
            areas=[{"name": name, "sections": [{"title": "", "entities": [keep]}]}],
            overwrite=True,
        )
        assert (await export_api.import_areas(req, data))["imported"] == 1
        expected = (None, "sensor.kitchen_humidity")
    row = await db.fetchone(
        "SELECT room_temperature_entity_id AS t, room_humidity_entity_id AS h "
        "FROM areas WHERE id = ?",
        (area_id,),
    )
    assert (row["t"], row["h"]) == expected
