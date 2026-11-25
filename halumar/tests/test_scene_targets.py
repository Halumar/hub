"""Scenes and scripts whose targets a profile may not switch (services/
scene_targets.py): the Admin UI hint next to the integration's check of
scene and script targets (contract docs/enforcement.md §2 step 4)."""

import json
import os
import types
import uuid
from pathlib import Path

import pytest

from api import profiles as api
from models.database import Database
from services import kids_mode, scene_targets
from services.profiles import catalog as cat
from services.profiles import rows as profile_rows

from .profile_helpers import add_client, starter_profile


class FakeHA:
    def __init__(self, states, scripts=None, registry=None, fail=False):
        self.states = states
        self.scripts = scripts or {}
        self.registry = registry or []
        self.fail = fail
        self.script_calls = 0

    async def get_states(self):
        if self.fail:
            raise ConnectionError("HA down")
        return [{"entity_id": k, **v} for k, v in self.states.items()]

    async def get_entity_registry(self):
        return self.registry

    async def get_areas(self):
        return [{"area_id": "kueche", "floor_id": "eg"}]

    async def get_floors(self):
        return [{"floor_id": "eg"}]

    async def get_script_config(self, entity_id):
        self.script_calls += 1
        if entity_id not in self.scripts:
            raise RuntimeError("not found")
        return self.scripts[entity_id]

    async def get_config(self):
        return {"time_zone": "Europe/Zurich"}


def _s(name, **attrs):
    return {"state": "off", "attributes": {"friendly_name": name, **attrs}}


STATES = {
    "scene.guten_morgen": _s(
        "Guten Morgen", entity_id=["light.kitchen", "cover.living", "sensor.temp"]
    ),
    "scene.filmabend": _s("Filmabend", entity_id=["light.living"]),
    "scene.other": _s("Andere Integration"),
    "light.kitchen": _s("Küchenlicht"),
    "light.living": _s("Stehlampe"),
    "cover.living": _s("Rollladen"),
    "cover.garage": _s("Garagentor", device_class="garage"),
    "lock.front_door": _s("Haustür"),
    "counter.visits": _s("Besuche"),
    "sensor.temp": _s("Temperatur"),
    "switch.hub_button": _s("Hub-Taste"),
    "script.morgen": _s("Morgen-Skript"),
    "script.nested": _s("Verschachtelt"),
}


@pytest.fixture(autouse=True)
def _reset():
    scene_targets.reset()
    kids_mode.reset()
    yield
    scene_targets.reset()


def _req(db, ha):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", actor="Test"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
        client=None,
    )


async def _area(db, name, entities, tiles=None, sort=0):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (area_id, name, sort, now, now),
        )
        await db.execute(
            "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, "
            "columns) VALUES (?, ?, 'S', 0, 1, 2)",
            (section_id, area_id),
        )
        for i, eid in enumerate(entities):
            tile = (tiles or {}).get(eid)
            await db.execute(
                "INSERT INTO area_entities (id, area_id, section_id, entity_id, "
                "sort_order, tile, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    area_id,
                    section_id,
                    eid,
                    i,
                    json.dumps(tile) if tile else None,
                    now,
                ),
            )
    return area_id


async def _home(db):
    living = await _area(
        db,
        "Wohnzimmer",
        ["scene.guten_morgen", "scene.filmabend", "light.living", "cover.living"],
    )
    kitchen = await _area(db, "Küche", ["light.kitchen"], sort=1)
    return living, kitchen


def _selected(*area_ids):
    return {
        "mode": "selected",
        "area_ids": list(area_ids),
        "tags": [],
        "own_area": False,
    }


async def _profile(db, suggestion="adult", **rules):
    body = cat.materialize(suggestion)
    body["rules"].update(rules)
    async with db.transaction():
        return await profile_rows.insert_profile(
            db,
            name=f"P {uuid.uuid4().hex[:4]}",
            body=body,
            now=Database.now(),
            actor=None,
        )


def _by_id(hints):
    return {h["entity_id"]: h for h in hints}


# ── script targets ──────────────────────────────────────────────────────────


REG = scene_targets.Registries(
    {
        **{k: {"entity_id": k, **v} for k, v in STATES.items()},
        "light.lamp": _s("Lampe"),
        "switch.kettle": _s("Wasserkocher"),
        "number.lamp_config": _s("Lampe Einstellung"),
        "light.hidden": _s("Versteckt"),
        "group.lights": _s("Lichter", entity_id=["light.living", "group.more"]),
        "group.more": _s("Mehr", entity_id=["light.kitchen", "group.lights"]),
    },
    entities=[
        {"entity_id": "light.kitchen", "id": "u-kitchen", "area_id": "kueche"},
        {"entity_id": "switch.kettle", "id": "u-kettle", "area_id": "kueche"},
        {"entity_id": "light.lamp", "id": "u-lamp", "device_id": "d-lamp"},
        {
            "entity_id": "number.lamp_config",
            "device_id": "d-lamp",
            "entity_category": "config",
        },
        {"entity_id": "light.hidden", "device_id": "d-lamp", "hidden_by": "user"},
        {
            "entity_id": "light.living",
            "id": "u-living",
            "labels": ["abend"],
            "area_id": "wohnzimmer",
        },
        {"entity_id": "cover.living", "area_id": "wohnzimmer", "device_id": "d-lamp"},
    ],
    devices=[{"id": "d-lamp", "area_id": "kueche", "labels": ["lampen"]}],
    areas=[
        {"area_id": "kueche", "floor_id": "eg", "labels": []},
        {"area_id": "wohnzimmer", "floor_id": "og", "labels": ["oben"]},
    ],
    floors=[{"floor_id": "eg"}, {"floor_id": "og"}],
    labels=[{"label_id": "abend"}, {"label_id": "lampen"}, {"label_id": "oben"}],
)


def _t(*steps, reg=REG):
    return scene_targets.script_targets({"sequence": list(steps)}, reg)


def test_script_targets_follow_the_sequence():
    config = {
        "alias": "x",
        "fields": {"entity_id": {"default": "light.ignored"}},
        "sequence": [
            {"action": "light.turn_on", "target": {"entity_id": "light.a"}},
            {"service": "light.turn_off", "data": {"entity_id": "light.b, Light.C"}},
            {"action": "cover.open_cover", "entity_id": ["cover.d"]},
            {"scene": "scene.e"},
            {"condition": "state", "entity_id": "binary_sensor.f", "state": "on"},
            {"wait_for_trigger": [{"trigger": "state", "entity_id": "lock.g"}]},
            {
                "choose": [
                    {
                        "conditions": [{"condition": "state", "entity_id": "sun.sun"}],
                        "sequence": [
                            {
                                "action": "switch.turn_on",
                                "target": {"entity_id": "switch.h"},
                            }
                        ],
                    }
                ],
            },
            {"action": "script.other"},
            {"action": "notify.notify", "data": {"message": "{{ now() }}"}},
        ],
    }
    targets, complete = scene_targets.script_targets(config, REG)
    assert targets == [
        "binary_sensor.f",
        "cover.d",
        "light.a",
        "light.b",
        "light.c",
        "lock.g",
        "scene.e",
        "script.other",
        "sun.sun",
        "switch.h",
    ]
    assert complete is True


def test_entity_id_all_and_none():
    """The live "Alles aus" (light.turn_off on all)."""
    lights = sorted(e for e in REG.states if e.startswith("light."))
    off = {"action": "light.turn_off", "target": {"entity_id": "all"}}
    assert _t(off) == (lights, True)
    assert _t({"action": "light.turn_off", "data": {"entity_id": "all"}}) == (
        lights,
        True,
    )
    everything, complete = _t({"action": "homeassistant.turn_off", "entity_id": "all"})
    assert complete and "counter.visits" in everything
    assert not any(e.startswith("group.") for e in everything)
    assert _t({"action": "light.turn_off", "target": {"entity_id": "none"}}) == (
        [],
        True,
    )
    # all of an integration's action: unknown.
    assert _t({"action": "hue.activate_scene", "target": {"entity_id": "all"}}) == (
        [],
        False,
    )


@pytest.mark.parametrize(
    ("target", "action", "expected"),
    [
        ({"area_id": "kueche"}, "light.turn_on", ["light.kitchen", "light.lamp"]),
        (
            {"area_id": "kueche"},
            "homeassistant.turn_off",
            ["light.kitchen", "light.lamp", "switch.kettle"],
        ),
        ({"floor_id": "eg"}, "light.turn_off", ["light.kitchen", "light.lamp"]),
        ({"floor_id": "og"}, "cover.close_cover", ["cover.living"]),
        ({"label_id": "abend"}, "light.turn_on", ["light.living"]),
        ({"label_id": "lampen"}, "light.turn_on", ["light.lamp"]),
        ({"label_id": "oben"}, "light.turn_on", ["light.living"]),
        ({"device_id": "d-lamp"}, "light.turn_on", ["light.lamp"]),
        (
            {"device_id": ["d-lamp"]},
            "homeassistant.turn_on",
            ["cover.living", "light.lamp"],
        ),
        ({"entity_id": "u-kitchen"}, "light.turn_on", ["light.kitchen"]),
        (
            {"entity_id": "group.lights"},
            "homeassistant.turn_on",
            ["light.kitchen", "light.living"],
        ),
    ],
)
def test_registries_resolve_targets(target, action, expected):
    assert _t({"action": action, "target": target}) == (expected, True)


def test_device_actions_resolve():
    step = {"device_id": "d-lamp", "domain": "light", "type": "turn_on"}
    assert _t(step) == (["light.lamp"], True)
    assert _t({**step, "entity_id": "u-living"}) == (["light.living"], True)


@pytest.mark.parametrize(
    "step",
    [
        {"action": "light.turn_on", "target": {"area_id": "keller"}},
        {"action": "light.turn_on", "target": {"device_id": "gone"}},
        {"action": "light.turn_on", "target": {"floor_id": "dach"}},
        {"action": "light.turn_on", "target": {"label_id": "weg"}},
        {"action": "light.turn_on", "target": {"entity_id": "0123456789abcdef"}},
        {"action": "light.turn_on", "target": {"entity_id": "{{ x }}"}},
        {"action": "light.turn_on", "target": {"area_id": "{{ a }}"}},
        {"action": "light.turn_on", "target": "{{ {'entity_id': 'light.a'} }}"},
        {"action": "{{ 'light.turn_on' }}", "target": {"entity_id": "light.a"}},
        {"service_template": "light.turn_on", "entity_id": "light.a"},
        {"action": "light.turn_on", "data": "{{ {'entity_id': 'light.a'} }}"},
        {"action": "rest_command.garage"},
        {"event": "open_garage"},
        {"action": "scene.apply", "data": {"entities": "{{ x }}"}},
        {"action": "scene.apply", "data": {"entities": {"{{ x }}": "on"}}},
    ],
)
def test_not_determinable(step):
    assert _t(step)[1] is False


def test_scene_apply_and_named_entities():
    assert _t(
        {"action": "scene.apply", "data": {"entities": {"light.kitchen": "on"}}},
        {
            "action": "tts.speak",
            "target": {"entity_id": "light.living"},
            "data": {"media_player_entity_id": "cover.living", "list": ["x"]},
        },
        {"action": "scene.create", "data": {"scene_id": "s"}},
    ) == (["cover.living", "light.kitchen", "light.living"], True)


def test_blueprint_script_inputs_name_the_targets():
    config = {
        "use_blueprint": {
            "path": "x.yaml",
            "input": {
                "lamp": "light.kitchen",
                "other": "nope",
                "t": {"entity_id": ["light.b"]},
                "room": {"area_id": "kueche"},
            },
        }
    }
    assert scene_targets.script_targets(config, REG) == (
        ["light.b", "light.kitchen", "light.lamp", "switch.kettle"],
        True,
    )


# ── profile hints ───────────────────────────────────────────────────────────


async def test_scene_with_a_device_outside_the_profile_areas(db):
    living, kitchen = await _home(db)
    pid = await _profile(db, areas=_selected(living))
    view = await api.get_profile(_req(db, FakeHA(STATES)), pid)
    hints = _by_id(view["scene_hints"])
    assert list(hints) == ["scene.guten_morgen"]
    hint = hints["scene.guten_morgen"]
    assert hint["name"] == "Guten Morgen"
    assert hint["area_ids"] == [living]
    assert hint["targets_unknown"] is False
    # The sensor is only read, the roller shutter is in the profile.
    assert hint["targets"] == [
        {
            "entity_id": "light.kitchen",
            "name": "Küchenlicht",
            "reason": "outside_areas",
            "area_ids": [kitchen],
        }
    ]
    # Both areas: no hint.
    pid2 = await _profile(db, areas=_selected(living, kitchen))
    assert (await api.get_profile(_req(db, FakeHA(STATES)), pid2))["scene_hints"] == []


async def test_target_not_in_any_area_has_no_area(db):
    living = await _area(db, "Wohnzimmer", ["scene.guten_morgen", "cover.living"])
    pid = await _profile(
        db, areas={"mode": "all", "area_ids": [], "tags": [], "own_area": False}
    )
    hints = _by_id(
        (await api.get_profile(_req(db, FakeHA(STATES)), pid))["scene_hints"]
    )
    target = hints["scene.guten_morgen"]["targets"][0]
    assert (target["entity_id"], target["reason"], target["area_ids"]) == (
        "light.kitchen",
        "outside_areas",
        [],
    )
    assert hints["scene.guten_morgen"]["area_ids"] == [living]


async def test_not_controllable_targets(db):
    states = {
        **STATES,
        "scene.mixed": _s(
            "Gemischt", entity_id=["counter.visits", "switch.hub_button", "light.gone"]
        ),
    }
    living = await _area(
        db, "Wohnzimmer", ["scene.mixed", "counter.visits", "switch.hub_button"]
    )
    pid = await _profile(db, areas=_selected(living))
    ha = FakeHA(
        states, registry=[{"entity_id": "switch.hub_button", "platform": "halumar"}]
    )
    hints = _by_id((await api.get_profile(_req(db, ha), pid))["scene_hints"])
    assert {t["entity_id"]: t["reason"] for t in hints["scene.mixed"]["targets"]} == {
        "counter.visits": "not_controllable",
        "switch.hub_button": "not_controllable",
    }


async def test_scripts_and_nested_scenes(db):
    scripts = {
        "script.morgen": {
            "sequence": [
                {"scene": "scene.guten_morgen"},
                {"action": "script.turn_on", "target": {"entity_id": "script.nested"}},
            ]
        },
        "script.nested": {
            "sequence": [
                {"action": "light.turn_on", "target": {"entity_id": "light.living"}}
            ]
        },
    }
    living = await _area(
        db,
        "Wohnzimmer",
        [
            "script.morgen",
            "scene.guten_morgen",
            "script.nested",
            "light.living",
            "cover.living",
        ],
    )
    pid = await _profile(db, areas=_selected(living))
    ha = FakeHA(STATES, scripts=scripts)
    hints = _by_id((await api.get_profile(_req(db, ha), pid))["scene_hints"])
    # The nested scene's kitchen light refuses the script and the scene.
    assert [t["entity_id"] for t in hints["script.morgen"]["targets"]] == [
        "light.kitchen"
    ]
    assert [t["entity_id"] for t in hints["scene.guten_morgen"]["targets"]] == [
        "light.kitchen"
    ]
    assert "script.nested" not in hints
    # Cached: a second read asks HA no further.
    calls = ha.script_calls
    await api.get_profile(_req(db, ha), pid)
    assert ha.script_calls == calls


async def test_nested_scene_outside_and_too_deep(db):
    states = {f"script.s{i}": _s(f"S{i}") for i in range(1, 5)}
    states.update(STATES)
    scripts = {
        f"script.s{i}": {
            "sequence": [
                {
                    "action": "script.turn_on",
                    "target": {"entity_id": f"script.s{i + 1}"},
                }
            ]
        }
        for i in range(1, 4)
    }
    scripts["script.s4"] = {
        "sequence": [
            {"action": "light.turn_on", "target": {"entity_id": "light.living"}}
        ]
    }
    scripts["script.morgen"] = {"sequence": [{"scene": "scene.guten_morgen"}]}
    living = await _area(
        db,
        "Wohnzimmer",
        [*(f"script.s{i}" for i in range(1, 5)), "script.morgen", "light.living"],
    )
    pid = await _profile(db, areas=_selected(living))
    hints = _by_id(
        (await api.get_profile(_req(db, FakeHA(states, scripts=scripts)), pid))[
            "scene_hints"
        ]
    )
    # s1 -> s2 -> s3 -> s4: s4 is the fourth level (max_depth 3).
    assert hints["script.s1"]["targets"] == [
        {
            "entity_id": "script.s4",
            "name": "S4",
            "reason": "not_controllable",
            "area_ids": [living],
        }
    ]
    assert "script.s2" not in hints
    # The scene itself is not in the profile.
    assert hints["script.morgen"]["targets"][0]["entity_id"] == "scene.guten_morgen"
    assert hints["script.morgen"]["targets"][0]["reason"] == "outside_areas"


async def test_run_target_of_a_tile_counts_as_released(db):
    tile = {"tap_action": {"action": "run", "target_entity_id": "scene.guten_morgen"}}
    living = await _area(
        db, "Wohnzimmer", ["light.living", "cover.living"], tiles={"light.living": tile}
    )
    pid = await _profile(db, areas=_selected(living))
    hints = _by_id(
        (await api.get_profile(_req(db, FakeHA(STATES)), pid))["scene_hints"]
    )
    assert hints["scene.guten_morgen"]["area_ids"] == [living]


async def test_alles_aus_and_area_scripts(db):
    """“Alles aus” switches every light, an area script the
    lights of that Home Assistant area: hints for those outside the profile."""
    states = {
        **STATES,
        "script.alles_aus": _s("Alles aus"),
        "script.kueche_aus": _s("Küche aus"),
        "light.group": _s("Gruppe", entity_id=["light.living", "light.kitchen"]),
        "script.gruppe": _s("Gruppe an"),
    }
    scripts = {
        "script.alles_aus": {
            "sequence": [{"action": "light.turn_off", "target": {"entity_id": "all"}}]
        },
        "script.kueche_aus": {
            "sequence": [{"action": "light.turn_off", "target": {"area_id": "kueche"}}]
        },
        "script.gruppe": {
            "sequence": [
                {"action": "light.turn_on", "target": {"entity_id": "light.group"}}
            ]
        },
    }
    registry = [{"entity_id": "light.kitchen", "area_id": "kueche"}]
    living = await _area(
        db,
        "Wohnzimmer",
        [
            "script.alles_aus",
            "script.kueche_aus",
            "script.gruppe",
            "light.living",
            "light.group",
        ],
    )
    pid = await _profile(db, areas=_selected(living))
    ha = FakeHA(states, scripts=scripts, registry=registry)
    hints = _by_id((await api.get_profile(_req(db, ha), pid))["scene_hints"])
    for script in ("script.alles_aus", "script.kueche_aus", "script.gruppe"):
        assert [(t["entity_id"], t["reason"]) for t in hints[script]["targets"]] == [
            ("light.kitchen", "outside_areas")
        ], script
        assert hints[script]["targets_unknown"] is False


async def test_kids_template_script_targets_unknown(db):
    states = {**STATES, "script.dyn": _s("Dynamisch")}
    scripts = {
        "script.dyn": {
            "sequence": [
                {"action": "light.turn_on", "target": {"entity_id": "{{ x }}"}}
            ]
        }
    }
    living = await _area(db, "Wohnzimmer", ["script.dyn", "light.living"])
    kid = await _profile(db, "child", areas=_selected(living))
    ha = FakeHA(states, scripts=scripts)
    hints = _by_id((await api.get_profile(_req(db, ha), kid))["scene_hints"])
    assert hints["script.dyn"]["targets_unknown"] is True
    adult = await _profile(db, areas=_selected(living), remote_access=False)
    assert (await api.get_profile(_req(db, ha), adult))["scene_hints"] == []


# ── kids and away ───────────────────────────────────────────────────────────


async def test_kids_blocked_targets_and_unknown_targets(db):
    states = {
        **STATES,
        "scene.abschliessen": _s(
            "Abschliessen", entity_id=["lock.front_door", "light.living"]
        ),
    }
    living = await _area(
        db,
        "Wohnzimmer",
        ["scene.abschliessen", "scene.other", "lock.front_door", "light.living"],
    )
    kid = await _profile(db, "child", areas=_selected(living))
    hints = _by_id(
        (await api.get_profile(_req(db, FakeHA(states)), kid))["scene_hints"]
    )
    assert hints["scene.abschliessen"]["targets"] == [
        {
            "entity_id": "lock.front_door",
            "name": "Haustür",
            "reason": "kids_blocked",
            "area_ids": [living],
        }
    ]
    assert hints["scene.other"] == {
        "entity_id": "scene.other",
        "name": "Andere Integration",
        "area_ids": [living],
        "targets_unknown": True,
        "targets": [],
    }
    # Adults: the lock is allowed at home, the other scene runs on its release.
    adult = await _profile(db, areas=_selected(living), remote_access=False)
    assert (await api.get_profile(_req(db, FakeHA(states)), adult))["scene_hints"] == []


async def test_target_refused_from_away(db):
    states = {
        **STATES,
        "scene.garage": _s(
            "Garage", entity_id=["cover.garage", "light.living", "lock.front_door"]
        ),
    }
    living = await _area(
        db,
        "Wohnzimmer",
        ["scene.garage", "cover.garage", "light.living", "lock.front_door"],
    )
    pid = await _profile(db, areas=_selected(living))  # adult: remote access on
    hints = _by_id(
        (await api.get_profile(_req(db, FakeHA(states)), pid))["scene_hints"]
    )
    # Garage door only "view" from away; a lock may be the safe direction.
    assert [
        (t["entity_id"], t["reason"]) for t in hints["scene.garage"]["targets"]
    ] == [("cover.garage", "remote")]
    local = await _profile(db, areas=_selected(living), remote_access=False)
    assert (await api.get_profile(_req(db, FakeHA(states)), local))["scene_hints"] == []


# ── API ─────────────────────────────────────────────────────────────────────


async def test_preview_of_a_draft_and_save(db, monkeypatch):
    from services.profiles import changes

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(changes, "apply_effects", _noop)
    monkeypatch.setattr(changes, "admin_broadcast", _noop)
    living, kitchen = await _home(db)
    pid = await _profile(db, areas=_selected(living))
    ha = FakeHA(STATES)
    preview = await api.update_profile(
        _req(db, ha),
        pid,
        api.ProfileWrite(rules={"areas": _selected(living, kitchen)}),
        dry_run=True,
    )
    body = json.loads(preview.body)
    assert preview.status_code == 202
    assert body["scene_hints"] == []
    saved = await api.update_profile(
        _req(db, ha),
        pid,
        api.ProfileWrite(rules={"areas": _selected(kitchen)}),
        dry_run=False,
    )
    assert saved["scene_hints"] == []  # no scene in the kitchen
    assert saved["rules"]["areas"]["area_ids"] == [kitchen]


async def test_device_effective_includes_its_own_area(db):
    living, kitchen = await _home(db)
    guest_profile = await starter_profile(db, "guest")
    client = await add_client(
        db,
        name="Gast",
        profile_id=guest_profile,
        own_area_id=living,
        overrides={
            "areas": {"mode": "selected", "area_ids": [], "tags": [], "own_area": True}
        },
    )
    view = await api.get_client_effective(_req(db, FakeHA(STATES)), client)
    assert [h["entity_id"] for h in view["scene_hints"]] == ["scene.guten_morgen"]
    assert view["scene_hints"][0]["targets"][0]["area_ids"] == [kitchen]
    await db.execute(
        "UPDATE paired_clients SET own_area_id = ? WHERE id = ?", (kitchen, client)
    )
    await db.commit()
    view = await api.get_client_effective(_req(db, FakeHA(STATES)), client)
    assert view["scene_hints"] == []


async def test_without_home_assistant_there_are_no_hints(db):
    living, _ = await _home(db)
    pid = await _profile(db, areas=_selected(living))
    assert (await api.get_profile(_req(db, FakeHA(STATES, fail=True)), pid))[
        "scene_hints"
    ] == []
    assert (await api.get_profile(_req(db, None), pid))["scene_hints"] == []


def test_allowlist_copy_has_run_targets():
    data = scene_targets.allowlist()
    assert data["run_targets"]["max_depth"] == 3
    assert "sensor" in data["run_targets"]["read_only_domains"]


# ── contract ────────────────────────────────────────────────────────────────


def _contract(schema):
    """Validator of a contract schema (CONTRACTS_DIR), None without a checkout."""
    root = os.environ.get("CONTRACTS_DIR")
    path = Path(root or "/nonexistent") / "schemas" / schema
    if not path.exists():
        return None
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")

    def retrieve(uri):
        return referencing.Resource.from_contents(
            json.loads(Path(uri.replace("file://", "")).read_text())
        )

    doc = json.loads(path.read_text())
    doc["$id"] = path.as_uri()
    return jsonschema.Draft202012Validator(
        doc, registry=referencing.Registry(retrieve=retrieve)
    )


def _errors(validator, doc):
    return [e.message for e in sorted(validator.iter_errors(doc), key=str)][:3]


async def test_answers_with_hints_match_the_contract(db, monkeypatch):
    """GET/PATCH profile, its dry run and the effective rules of a device carry
    scene_hints as in common/scene-hint.json, with every reason."""
    from services.profiles import changes

    names = (
        "rest/profile.json",
        "rest/profile-preview.response.json",
        "rest/client-effective.response.json",
    )
    checks = {name: _contract(name) for name in names}
    if None in checks.values():
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(changes, "apply_effects", _noop)
    monkeypatch.setattr(changes, "admin_broadcast", _noop)
    states = {
        **STATES,
        "scene.alles": _s(
            "Alles",
            entity_id=[
                "light.kitchen",
                "counter.visits",
                "switch.hub_button",
                "lock.front_door",
                "cover.garage",
            ],
        ),
    }
    living = await _area(
        db,
        "Wohnzimmer",
        [
            "scene.alles",
            "scene.other",
            "counter.visits",
            "switch.hub_button",
            "lock.front_door",
            "cover.garage",
        ],
    )
    kitchen = await _area(db, "Küche", ["light.kitchen"], sort=1)
    ha = FakeHA(
        states, registry=[{"entity_id": "switch.hub_button", "platform": "halumar"}]
    )
    kid = await _profile(db, "child", areas=_selected(living))
    adult = await _profile(db, areas=_selected(living))
    kid_view = await api.get_profile(_req(db, ha), kid)
    adult_view = await api.get_profile(_req(db, ha), adult)
    reasons = {
        t["reason"]
        for view in (kid_view, adult_view)
        for h in view["scene_hints"]
        for t in h["targets"]
    }
    assert reasons == {"outside_areas", "not_controllable", "kids_blocked", "remote"}
    assert any(h["targets_unknown"] for h in kid_view["scene_hints"])
    preview = await api.update_profile(
        _req(db, ha),
        adult,
        api.ProfileWrite(rules={"areas": _selected(kitchen)}),
        dry_run=True,
    )
    preview_body = json.loads(preview.body)
    assert preview_body["scene_hints"] == []
    preview = await api.update_profile(
        _req(db, ha),
        kid,
        api.ProfileWrite(rules={"areas": _selected(living)}),
        dry_run=True,
    )
    preview_body = json.loads(preview.body)
    assert preview_body["scene_hints"]
    saved = await api.update_profile(
        _req(db, ha),
        adult,
        api.ProfileWrite(rules={"areas": _selected(living)}),
        dry_run=False,
    )
    client = await add_client(db, name="Phone", profile_id=adult)
    effective = await api.get_client_effective(_req(db, ha), client)
    own = await api.put_client_own_area(
        _req(db, ha), client, api.OwnArea(area_id=kitchen), dry_run=False
    )
    assert effective["scene_hints"] and "scene_hints" in own
    for name, doc in (
        ("rest/profile.json", kid_view),
        ("rest/profile.json", adult_view),
        ("rest/profile.json", saved),
        ("rest/profile-preview.response.json", preview_body),
        ("rest/client-effective.response.json", effective),
        ("rest/client-effective.response.json", own),
    ):
        assert _errors(checks[name], doc) == [], name
    # The cap of the hub is the cap of the contract.
    hint_schema = json.loads(
        (Path(os.environ["CONTRACTS_DIR"]) / "schemas/rest/profile.json").read_text()
    )
    assert (
        hint_schema["properties"]["scene_hints"]["maxItems"] == scene_targets.MAX_HINTS
    )
