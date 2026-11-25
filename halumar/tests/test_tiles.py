"""Tile settings per area entity.

Validation against the capabilities table (one source: contract data file),
Home Assistant checks (run target / sensor exist, 503 without HA), admin
only, canonical storage, delivery in get_area (Admin REST and WS, identical
for every client), area-scoped entity_updated with ``changed``, audit log,
copy/export/import, and the suggestions/capabilities endpoints.
"""

import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from fastapi import HTTPException
from pydantic import ValidationError

import api.areas as areas
import api.export as export
import api.tiles as tiles_api
from api import authz
from api.websocket import _handle_message
import importlib
from api.ws import registry
from data.mdi_paths import MDI_PATHS
from models.database import Database
from models.schemas import AreaEntityCreate, AreaEntityUpdate, TileSettings
from services import tiles

bc = importlib.import_module("api.ws.broadcast")
CONTRACT_DATA = Path(__file__).resolve().parents[1] / "rootfs/opt/hub/data"


class _FakeHA:
    def __init__(self, states=None, *, fail=False):
        self.fail = fail
        self.states = states or {
            "light.stehlampe": {"state": "on", "attributes": {"friendly_name": "Lamp"}},
            "switch.tv": {"state": "on", "attributes": {"friendly_name": "TV"}},
            "lock.door": {"state": "locked", "attributes": {}},
            "climate.hvac": {"state": "heat", "attributes": {}},
            "scene.filmabend": {"state": "x", "attributes": {"friendly_name": "Film"}},
            "script.gute_nacht": {
                "state": "off",
                "attributes": {"friendly_name": "GN"},
            },
            "sensor.tv_power": {
                "state": "412",
                "attributes": {"unit_of_measurement": "W", "friendly_name": "TV W"},
            },
            "sensor.other": {"state": "1", "attributes": {}},
            "person.manager_one": {
                "state": "home",
                "attributes": {
                    "latitude": 47.1,
                    "longitude": 8.5,
                    "icon": "mdi:account",
                },
            },
            "light.ha_icon": {"state": "off", "attributes": {"icon": "mdi:floor-lamp"}},
        }
        self.calls = []

    async def get_state(self, entity_id):
        self.calls.append(entity_id)
        if self.fail:
            raise RuntimeError("HA down")
        state = self.states.get(entity_id)
        if state is None:
            return None
        return {
            **state,
            "entity_id": entity_id,
            "last_changed": "2026-11-18T10:00:00+00:00",
        }

    async def get_states(self):
        if self.fail:
            raise RuntimeError("HA down")
        return [{**v, "entity_id": k} for k, v in self.states.items()]

    async def get_entity_registry(self):
        return [
            {"entity_id": "switch.tv", "device_id": "dev-tv"},
            {"entity_id": "sensor.tv_power", "device_id": "dev-tv"},
            {"entity_id": "sensor.other", "device_id": "dev-x"},
        ]


def _request(db, ha=None, auth_type="ingress"):
    state = SimpleNamespace(db=db, ha_client=ha or _FakeHA(), auth_type=auth_type)
    return SimpleNamespace(
        state=state,
        app=SimpleNamespace(state=state),
        client=SimpleNamespace(host="127.0.0.1"),
    )


async def _area(db, entities=("light.stehlampe",), *, tags=(), is_active=1, name=None):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (area_id, name or area_id[:8], is_active, now, now),
    )
    await db.execute(
        "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, "
        "columns) VALUES (?, ?, 'S', 0, 1, 2)",
        (section_id, area_id),
    )
    for i, entity_id in enumerate(entities):
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, "
            "sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, entity_id, section_id, i, now),
        )
    for tag in tags:
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
        )
    await db.commit()
    return area_id, section_id


async def _client(db, tags=(), *, is_guest=False, name="C"):
    return await add_client(
        db, name=name, last_seen=Database.now(), guest=is_guest, tags=list(tags)
    )


async def _put(db, area_id, entity_id, body, ha=None, auth_type="ingress"):
    data = AreaEntityUpdate.model_validate(body)
    return await areas.update_area_entity(
        _request(db, ha, auth_type), area_id, entity_id, data
    )


async def _stored(db, area_id, entity_id):
    row = await db.fetchone(
        "SELECT tile FROM area_entities WHERE area_id = ? AND entity_id = ?",
        (area_id, entity_id),
    )
    return row["tile"]


@pytest.fixture
def scoped(monkeypatch):
    """Capture area-scoped broadcasts."""
    sent = []

    async def _capture(db, area_id, message):
        sent.append((area_id, message))
        return 1

    monkeypatch.setattr(areas.api_ws, "broadcast_area_scoped", _capture)
    return sent


def _types(exc):
    return [d["type"] for d in exc.value.detail]


# ── contract data ────────────────────────────────────────────────────────────


class TestContractData:
    def test_every_curated_icon_exists_in_the_hub_mdi_table(self):
        icons = tiles.icon_set()["icons"]
        assert len(icons) >= 100
        assert [i["mdi"] for i in icons if i["mdi"] not in MDI_PATHS] == []

    def test_capabilities_have_one_row_per_domain_and_other(self):
        seen = []
        for row in tiles.capabilities()["domains"]:
            seen += row["domains"]
        assert len(seen) == len(set(seen)) and "_other" in seen
        assert tiles.capability_row("light")["variants"] == [
            "compact",
            "large",
            "slider",
        ]
        assert tiles.capability_row("unknown_domain")["domains"] == ["_other"]

    def test_literals_match_the_capabilities(self):
        from typing import get_args

        from models import schemas

        caps = tiles.capabilities()
        variants = {v for r in caps["domains"] for v in r["variants"]}
        assert variants <= set(get_args(schemas.TileVariant))
        taps = {a for r in caps["domains"] for a in r["tap_actions"]} | set(
            caps["always"]["tap_actions"]
        )
        assert taps <= set(get_args(schemas.TileActionKind))
        assert set(caps["always"]["hold_actions"]) == set(
            get_args(schemas.TileHoldActionKind)
        )
        assert {t["domain"] for t in caps["run_targets"]} == {
            "scene",
            "script",
            "button",
            "input_button",
        }

    def test_vendored_files_are_contract_copies(self):
        # scripts/ci/contracts-check.sh compares them byte by byte with the
        # pinned contract; here only: valid JSON with the expected version.
        for name in ("tile_capabilities.json", "tile_icons.json"):
            assert json.loads((CONTRACT_DATA / name).read_text())["version"] == 1


# ── schema (structure) ───────────────────────────────────────────────────────


class TestSchema:
    @pytest.mark.parametrize(
        "tile, reason",
        [
            ({"tap_action": {"action": "run"}}, "tile.target_required"),
            (
                {"tap_action": {"action": "run", "target_entity_id": "light.x"}},
                "tile.target_domain",
            ),
            (
                {"tap_action": {"action": "toggle", "target_entity_id": "script.x"}},
                "tile.field_not_allowed",
            ),
            ({"hidden_on": ["phone", "tablet", "tv"]}, "tile.hidden_everywhere"),
            ({"hidden_on": ["tv", "tv"]}, "tile.hidden_duplicate"),
            ({"hidden_on": []}, "tile.hidden_empty"),
            ({"secondary": {"source": "attribute"}}, "tile.attribute_required"),
            ({"secondary": {"source": "entity"}}, "tile.entity_required"),
            (
                {"secondary": {"source": "entity", "entity_id": "light.x"}},
                "tile.entity_domain",
            ),
            (
                {"secondary": {"source": "state", "attribute": "brightness"}},
                "tile.field_not_allowed",
            ),
            (
                {"secondary": {"source": "state", "entity_id": "sensor.x"}},
                "tile.field_not_allowed",
            ),
            ({"accent": "#ff0000"}, "literal_error"),
            ({"icon": "fa:home"}, "string_pattern_mismatch"),
            ({"hold_action": {"action": "run"}}, "literal_error"),
            ({"colour": "red"}, "extra_forbidden"),
            ({"icon_svg": "M0Z"}, "extra_forbidden"),
            ({"tap_action": {"action": "toggle", "data": {"x": 1}}}, "extra_forbidden"),
        ],
    )
    def test_rejected(self, tile, reason):
        with pytest.raises(ValidationError) as exc:
            TileSettings.model_validate(tile)
        assert reason in [e["type"] for e in exc.value.errors()]

    def test_update_rejects_unknown_root_keys(self):
        with pytest.raises(ValidationError):
            AreaEntityUpdate.model_validate({"name": "x"})

    @pytest.mark.parametrize(
        "raw, expected",
        [("  Lampe  ", "Lampe"), ("", None), ("   ", None), (None, None)],
    )
    def test_display_name_normalized(self, raw, expected):
        assert AreaEntityUpdate(display_name=raw).display_name == expected

    @pytest.mark.parametrize("raw", ["x" * 101, "Lampe\x07", "a\nb"])
    def test_display_name_rejected(self, raw):
        with pytest.raises(ValidationError):
            AreaEntityUpdate(display_name=raw)
        with pytest.raises(ValidationError):
            AreaEntityCreate(entity_id="light.x", section_id="s", display_name=raw)

    def test_canonical_form(self):
        tile = TileSettings.model_validate(
            {"variant": "slider", "icon": "mdi:floor-lamp", "accent": None}
        )
        assert (
            tiles.canonical_tile(tile) == '{"icon":"mdi:floor-lamp","variant":"slider"}'
        )
        assert tiles.canonical_tile(TileSettings()) is None
        assert tiles.canonical_tile(None) is None


# ── capabilities matrix (parametrized from the contract table) ──────────────


def _matrix():
    caps = tiles.capabilities()
    cases = []
    for row in caps["domains"]:
        domain = row["domains"][0]
        entity_id = f"{domain}.x" if domain != "_other" else "zone.x"
        for variant in ("compact", "large", "slider", "action"):
            cases.append((entity_id, {"variant": variant}, variant in row["variants"]))
        cases.append((entity_id, {"icon": "mdi:lamp"}, row["icon"]))
        cases.append((entity_id, {"accent": "media"}, row["accent"]))
        cases.append((entity_id, {"confirm": True}, row["confirm"]))
        for action in ("toggle", "more_info", "none", "default"):
            ok = action in row["tap_actions"] or action in caps["always"]["tap_actions"]
            cases.append((entity_id, {"tap_action": {"action": action}}, ok))
        cases.append(
            (
                entity_id,
                {"tap_action": {"action": "run", "target_entity_id": "script.x"}},
                "run" in row["tap_actions"],
            )
        )
        cases.append(
            (
                entity_id,
                {"secondary": {"source": "entity", "entity_id": "sensor.x"}},
                row["secondary_entity"],
            )
        )
        for attr in row["attributes"]:
            cases.append(
                (
                    entity_id,
                    {"secondary": {"source": "attribute", "attribute": attr}},
                    True,
                )
            )
        cases.append(
            (
                entity_id,
                {"secondary": {"source": "attribute", "attribute": "zzz"}},
                False,
            )
        )
    return cases


@pytest.mark.parametrize("entity_id, tile, allowed", _matrix())
def test_capability_matrix(entity_id, tile, allowed):
    errors = tiles.capability_errors(entity_id, TileSettings.model_validate(tile))
    assert (errors == []) is allowed, errors


def test_unknown_mdi_icon_rejected():
    errors = tiles.capability_errors(
        "light.x", TileSettings(icon="mdi:does-not-exist-anywhere")
    )
    assert [e["type"] for e in errors] == ["tile.icon_unknown"]
    assert (
        tiles.capability_errors("light.x", TileSettings(icon="sf:lamp.floor.fill"))
        == []
    )


# ── PUT /api/areas/{id}/entities/{entity_id} ────────────────────────────────


class TestPut:
    async def test_set_read_reset(self, db, scoped, caplog):
        area_id, _ = await _area(db)
        body = {
            "display_name": " Stehlampe ",
            "tile": {
                "icon": "mdi:floor-lamp",
                "variant": "slider",
                "accent": "light",
                "secondary": {"source": "attribute", "attribute": "brightness"},
                "tap_action": {"action": "toggle"},
                "hold_action": {"action": "more_info"},
                "hidden_on": ["tv"],
            },
        }
        with caplog.at_level(logging.INFO, logger="hub.security"):
            res = await _put(db, area_id, "light.stehlampe", body)
        assert res.display_name == "Stehlampe"
        assert res.tile["variant"] == "slider"
        assert res.tile["icon_svg"] == MDI_PATHS["floor-lamp"]
        stored = json.loads(await _stored(db, area_id, "light.stehlampe"))
        assert list(stored) == sorted(stored) and "icon_svg" not in stored
        assert scoped[-1] == (
            area_id,
            {
                "type": "entity_updated",
                "data": {
                    "area_id": area_id,
                    "entity_id": "light.stehlampe",
                    "changed": ["display_name", "tile"],
                },
            },
        )
        audit = [
            r.getMessage() for r in caplog.records if "tile_updated" in r.getMessage()
        ]
        assert (
            audit
            and "fields=accent,hidden_on,hold_action,icon,secondary,tap_action,variant"
            in audit[0]
        )
        assert "floor-lamp" not in audit[0], "the audit log names fields, not values"

        detail = await areas.get_area(_request(db), area_id)
        tile = detail.sections[0].entities[0].tile
        assert tile["accent"] == "light" and tile["icon_svg"]

        res = await _put(db, area_id, "light.stehlampe", {"tile": None})
        assert res.tile == {}
        assert await _stored(db, area_id, "light.stehlampe") is None
        assert scoped[-1][1]["data"]["changed"] == ["tile"]

    async def test_empty_object_is_default(self, db, scoped):
        area_id, _ = await _area(db)
        await _put(db, area_id, "light.stehlampe", {"tile": {"variant": "large"}})
        await _put(db, area_id, "light.stehlampe", {"tile": {}})
        assert await _stored(db, area_id, "light.stehlampe") is None

    async def test_other_fields_keep_tile(self, db, scoped):
        area_id, _ = await _area(db)
        await _put(db, area_id, "light.stehlampe", {"tile": {"variant": "large"}})
        res = await _put(db, area_id, "light.stehlampe", {"columns": 2})
        assert res.tile == {"variant": "large"}
        assert scoped[-1][1]["data"]["changed"] == ["columns"]

    async def test_capability_error_is_422_with_field_location(self, db, scoped):
        area_id, _ = await _area(db, ["lock.door"])
        with pytest.raises(HTTPException) as exc:
            await _put(
                db,
                area_id,
                "lock.door",
                {"tile": {"accent": "media", "icon": "mdi:lamp"}},
            )
        assert exc.value.status_code == 422
        assert sorted(_types(exc)) == [
            "tile.field_not_allowed",
            "tile.field_not_allowed",
        ]
        assert {tuple(d["loc"]) for d in exc.value.detail} == {
            ("body", "tile", "accent"),
            ("body", "tile", "icon"),
        }
        assert await _stored(db, area_id, "lock.door") is None and scoped == []

    async def test_run_target_must_exist(self, db, scoped):
        area_id, _ = await _area(db, ["switch.tv"])
        body = {
            "tile": {"tap_action": {"action": "run", "target_entity_id": "script.nope"}}
        }
        with pytest.raises(HTTPException) as exc:
            await _put(db, area_id, "switch.tv", body)
        assert exc.value.status_code == 422
        assert _types(exc) == ["tile.target_not_found"]
        assert exc.value.detail[0]["loc"] == [
            "body",
            "tile",
            "tap_action",
            "target_entity_id",
        ]

    async def test_run_target_not_visible_anywhere_is_allowed(self, db, scoped):
        """The admin decides; the target need not be placed."""
        area_id, _ = await _area(db, ["switch.tv"])
        body = {
            "tile": {
                "tap_action": {
                    "action": "run",
                    "target_entity_id": "script.gute_nacht",
                },
                "confirm": True,
            }
        }
        res = await _put(db, area_id, "switch.tv", body)
        assert res.tile["tap_action"]["target_entity_id"] == "script.gute_nacht"

    async def test_secondary_sensor_must_exist(self, db, scoped):
        area_id, _ = await _area(db, ["switch.tv"])
        body = {"tile": {"secondary": {"source": "entity", "entity_id": "sensor.gone"}}}
        with pytest.raises(HTTPException) as exc:
            await _put(db, area_id, "switch.tv", body)
        assert _types(exc) == ["tile.entity_not_found"]

    async def test_ha_unavailable_is_503_and_stores_nothing(self, db, scoped):
        area_id, _ = await _area(db, ["switch.tv"])
        body = {
            "tile": {
                "tap_action": {"action": "run", "target_entity_id": "scene.filmabend"}
            }
        }
        with pytest.raises(HTTPException) as exc:
            await _put(db, area_id, "switch.tv", body, ha=_FakeHA(fail=True))
        assert exc.value.status_code == 503
        assert await _stored(db, area_id, "switch.tv") is None

    async def test_too_large(self, db, scoped, monkeypatch):
        monkeypatch.setattr(tiles, "MAX_TILE_BYTES", 20)
        area_id, _ = await _area(db)
        with pytest.raises(HTTPException) as exc:
            await _put(
                db, area_id, "light.stehlampe", {"tile": {"icon": "mdi:floor-lamp"}}
            )
        assert _types(exc) == ["tile.too_large"]

    async def test_admin_only(self, db, scoped):
        area_id, _ = await _area(db)
        with pytest.raises(HTTPException) as exc:
            await _put(db, area_id, "light.stehlampe", {"tile": {}}, auth_type="client")
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await tiles_api.tile_capabilities(_request(db, auth_type="client"))
        assert exc.value.status_code == 403
        with pytest.raises(HTTPException) as exc:
            await tiles_api.tile_suggestions(
                _request(db, auth_type="client"), area_id, "light.stehlampe"
            )
        assert exc.value.status_code == 403

    async def test_unknown_placement_404(self, db, scoped):
        area_id, _ = await _area(db)
        with pytest.raises(HTTPException) as exc:
            await _put(db, area_id, "light.unknown", {"tile": {}})
        assert exc.value.status_code == 404


class TestAddEntity:
    async def test_add_with_tile(self, db, monkeypatch):
        sent = []

        async def _bc(message, audience=None):
            sent.append(message)

        monkeypatch.setattr(areas.api_ws, "broadcast", _bc)
        area_id, section_id = await _area(db, [])
        res = await areas.add_entity_to_area(
            _request(db),
            area_id,
            AreaEntityCreate(
                entity_id="scene.filmabend",
                section_id=section_id,
                tile={"variant": "action", "accent": "media"},
            ),
        )
        assert res.tile == {"accent": "media", "variant": "action"}
        assert (
            json.loads(await _stored(db, area_id, "scene.filmabend"))["variant"]
            == "action"
        )

    async def test_add_with_invalid_tile_stores_nothing(self, db):
        area_id, section_id = await _area(db, [])
        with pytest.raises(HTTPException) as exc:
            await areas.add_entity_to_area(
                _request(db),
                area_id,
                AreaEntityCreate(
                    entity_id="lock.door",
                    section_id=section_id,
                    tile={"accent": "safe"},
                ),
            )
        assert exc.value.status_code == 422
        assert await db.fetchall("SELECT * FROM area_entities") == []


# ── delivery to apps (WS get_area) ───────────────────────────────────────────


class _FakeWS:
    def __init__(self, db, ha):
        self.app = SimpleNamespace(
            state=SimpleNamespace(db=db, ha_client=ha, app_url="http://test")
        )
        self.url = SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def _get_area(db, ha, client_id, area_id):
    ws = _FakeWS(db, ha)
    await _handle_message(
        ws,
        {"type": "get_area", "area_id": area_id},
        authenticated=True,
        client_id=client_id,
    )
    return ws.sent[-1]


def _entities(reply):
    return {e["entity_id"]: e for s in reply["data"]["sections"] for e in s["entities"]}


class TestDelivery:
    async def test_same_tile_for_member_guest_and_admin(self, db, scoped):
        area_id, _ = await _area(
            db, ["switch.tv", "light.stehlampe", "person.manager_one"], tags=["gast"]
        )
        await _put(
            db,
            area_id,
            "switch.tv",
            {
                "tile": {
                    "tap_action": {
                        "action": "run",
                        "target_entity_id": "script.gute_nacht",
                    },
                    "secondary": {"source": "entity", "entity_id": "sensor.tv_power"},
                    "hidden_on": ["tv"],
                }
            },
        )
        member = await _client(db)
        guest = await _client(db, ["gast"], is_guest=True)
        ha = _FakeHA()
        replies = [
            await _get_area(db, ha, c, area_id) for c in (member, guest, "ingress")
        ]
        tiles_out = [_entities(r)["switch.tv"]["tile"] for r in replies]
        assert tiles_out[0] == tiles_out[1] == tiles_out[2]
        tile = tiles_out[0]
        assert tile["tap_action"] == {
            "action": "run",
            "target_entity_id": "script.gute_nacht",
        }
        assert tile["secondary_state"]["state"] == "412"
        assert tile["hidden_on"] == ["tv"]
        # Every entity carries an object, {} = default.
        assert _entities(replies[0])["light.stehlampe"]["tile"] == {}

    async def test_ha_icon_becomes_icon_svg(self, db):
        area_id, _ = await _area(db, ["light.ha_icon"])
        reply = await _get_area(db, _FakeHA(), "ingress", area_id)
        assert _entities(reply)["light.ha_icon"]["tile"] == {
            "icon_svg": MDI_PATHS["floor-lamp"]
        }

    async def test_sf_icon_has_no_svg(self, db, scoped):
        area_id, _ = await _area(db)
        await _put(
            db, area_id, "light.stehlampe", {"tile": {"icon": "sf:lamp.floor.fill"}}
        )
        reply = await _get_area(db, _FakeHA(), "ingress", area_id)
        assert _entities(reply)["light.stehlampe"]["tile"] == {
            "icon": "sf:lamp.floor.fill"
        }

    async def test_secondary_state_is_sanitized_for_clients(self, db):
        area_id, _ = await _area(db, ["switch.tv"])
        # Stored directly: a tracker as secondary entity is not allowed by the
        # schema, but the output path must still never leak GPS.
        await db.execute(
            "UPDATE area_entities SET tile = ? WHERE entity_id = 'switch.tv'",
            (
                json.dumps(
                    {
                        "secondary": {
                            "source": "entity",
                            "entity_id": "person.manager_one",
                        }
                    }
                ),
            ),
        )
        await db.commit()
        member = await _client(db)
        reply = await _get_area(db, _FakeHA(), member, area_id)
        state = _entities(reply)["switch.tv"]["tile"]["secondary_state"]
        assert "latitude" not in state["attributes"]
        admin = await _get_area(db, _FakeHA(), "ingress", area_id)
        assert (
            "latitude"
            in _entities(admin)["switch.tv"]["tile"]["secondary_state"]["attributes"]
        )

    async def test_broken_stored_value_is_default(self, db):
        area_id, _ = await _area(db)
        await db.execute("UPDATE area_entities SET tile = 'not json'")
        await db.commit()
        reply = await _get_area(db, _FakeHA(), "ingress", area_id)
        assert _entities(reply)["light.stehlampe"]["tile"] == {}


# ── events ───────────────────────────────────────────────────────────────────


class _Sock:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class TestScopedBroadcast:
    async def test_only_sessions_that_read_the_area(self, db, monkeypatch):
        area_id, _ = await _area(db, tags=["og"])
        member = await _client(db)  # no tags: every active area
        tagged_other = await _client(db, ["ug"])
        guest_in = await _client(db, ["og"], is_guest=True)
        guest_out = await _client(db, [], is_guest=True)
        socks = {}
        fake = {}
        for cid in (
            member,
            tagged_other,
            guest_in,
            guest_out,
            "ingress",
            registry.INTEGRATION_CLIENT_ID,
        ):
            socks[cid] = _Sock()
            fake[socks[cid]] = {"authenticated": True, "client_id": cid}
        resetting = _Sock()
        fake[resetting] = {
            "authenticated": True,
            "client_id": member,
            "reset_signaled": True,
        }
        unauth = _Sock()
        fake[unauth] = {"authenticated": False, "client_id": None}
        monkeypatch.setattr(bc, "_clients", fake)
        message = {
            "type": "entity_updated",
            "data": {
                "area_id": area_id,
                "entity_id": "light.stehlampe",
                "changed": ["tile"],
            },
        }
        sent = await bc.broadcast_area_scoped(db, area_id, message)
        got = {cid for cid, s in socks.items() if s.sent}
        assert got == {member, guest_in, "ingress", registry.INTEGRATION_CLIENT_ID}
        assert sent == 4 and resetting.sent == [] and unauth.sent == []
        assert socks[member].sent == [message]

    async def test_inactive_area_reaches_admin_only(self, db, monkeypatch):
        area_id, _ = await _area(db, is_active=0)
        member = await _client(db)
        socks = {cid: _Sock() for cid in (member, "ingress")}
        monkeypatch.setattr(
            bc,
            "_clients",
            {s: {"authenticated": True, "client_id": cid} for cid, s in socks.items()},
        )
        await bc.broadcast_area_scoped(
            db, area_id, {"type": "entity_updated", "data": {}}
        )
        assert socks[member].sent == [] and socks["ingress"].sent


# ── copy, export, import ─────────────────────────────────────────────────────


class TestCopyExportImport:
    async def test_copy_keeps_tile(self, db, scoped, monkeypatch):
        async def _noop(*a, **k):
            return 0

        monkeypatch.setattr(areas.api_ws, "broadcast", _noop)
        monkeypatch.setattr(areas.api_ws, "notify_areas_changed", _noop)
        area_id, _ = await _area(db, name="Wohnen")
        await _put(db, area_id, "light.stehlampe", {"tile": {"variant": "large"}})
        copy = await areas.copy_area(_request(db), area_id)
        assert json.loads(await _stored(db, copy.id, "light.stehlampe")) == {
            "variant": "large"
        }

    async def test_export_and_import_round_trip(self, db, scoped):
        area_id, _ = await _area(db, ["light.stehlampe", "lock.door"], name="Export")
        await _put(
            db,
            area_id,
            "light.stehlampe",
            {"tile": {"variant": "slider", "accent": "light"}},
        )
        exported = await export.export_area(_request(db), area_id)
        entities = {e["entity_id"]: e for e in exported["sections"][0]["entities"]}
        assert entities["light.stehlampe"]["tile"] == {
            "accent": "light",
            "variant": "slider",
        }
        assert entities["lock.door"]["tile"] == {}

    def test_import_validates_offline(self):
        assert tiles.tile_from_import({"variant": "slider"}, "light.x") == (
            '{"variant":"slider"}',
            None,
        )
        assert (
            tiles.tile_from_import('{"variant":"large"}', "light.x")[0]
            == '{"variant":"large"}'
        )
        assert tiles.tile_from_import(None, "light.x") == (None, None)
        raw, err = tiles.tile_from_import({"accent": "safe"}, "lock.door")
        assert raw is None and "tile.field_not_allowed" in err
        raw, err = tiles.tile_from_import({"colour": "x"}, "light.x")
        assert raw is None and "dropped" in err


# ── Admin helpers ────────────────────────────────────────────────────────────


class TestAdminHelpers:
    async def test_capabilities_carry_icon_paths(self, db):
        res = await tiles_api.tile_capabilities(_request(db))
        assert res["capabilities"]["version"] == 1
        icon = next(i for i in res["icons"]["icons"] if i["mdi"] == "floor-lamp")
        assert (
            icon["path"] == MDI_PATHS["floor-lamp"] and icon["sf"] == "lamp.floor.fill"
        )

    async def test_suggestions(self, db):
        area_id, _ = await _area(
            db, ["switch.tv", "scene.filmabend"], tags=["gast"], name="Wohnen"
        )
        member = await _client(db, name="iPhone")
        guest = await _client(db, ["gast"], is_guest=True, name="Gast-iPad")
        await _client(db, ["ug"], name="Fremd")  # does not see the tile at all
        res = await tiles_api.tile_suggestions(_request(db), area_id, "switch.tv")
        assert res["sensors"][0]["entity_id"] == "sensor.tv_power"
        assert res["sensors"][0]["same_device"] is True
        targets = {t["entity_id"]: t for t in res["run_targets"]}
        assert targets["scene.filmabend"]["areas"] == [
            {"area_id": area_id, "name": "Wohnen"}
        ]
        assert targets["scene.filmabend"]["not_visible_for"] == []
        hidden = {
            d["client_id"] for d in targets["script.gute_nacht"]["not_visible_for"]
        }
        assert hidden == {member, guest}
        assert {v["client_id"] for v in res["tile_viewers"]} == {member, guest}

    async def test_suggestions_errors(self, db):
        area_id, _ = await _area(db)
        with pytest.raises(HTTPException) as exc:
            await tiles_api.tile_suggestions(_request(db), area_id, "light.nope")
        assert exc.value.status_code == 404
        with pytest.raises(HTTPException) as exc:
            await tiles_api.tile_suggestions(
                _request(db, _FakeHA(fail=True)), area_id, "light.stehlampe"
            )
        assert exc.value.status_code == 502


def test_authz_redaction_handles_missing_tile():
    area = {"sections": [{"entities": [{"entity_id": "light.x", "state": None}]}]}
    assert authz.redact_area_for_client(area) == area
