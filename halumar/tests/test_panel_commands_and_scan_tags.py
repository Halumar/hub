"""Panel capabilities, panel commands and scan tags.

(a) panel status: the capabilities, lock device_admin, battery
    temperature and health are validated and merged, invalid values dropped;
(b) panel_ha: battery_temperature on Android phones and tablets only;
(c) panel_command announce / show_camera: offered only when the app lists
    them, rule panel_announce (announce_off, volume capped), camera inside
    the device's areas (not_visible), arguments checked (invalid), text
    never logged;
(d) scan tags: admin REST, resolve with ok / unknown_tag / tags_off /
    not_visible / rate_limited, logged as tag_scanned with the tag fields for
    the integration's <ns>_tag_scanned;
(e) migration v16: scan_tags table, the Android tablet default slot moves
    from the wall panel profile to the phone default (other choices stay);
(f) catalog: camera_live on TVs, TV profile kiosk/camera not fixed, rules
    panel_announce / panel_off_mode / scan_tags.
"""

import json
import sqlite3
import types
import uuid

import fastapi
import pytest

import api.integration as apiintg
import api.scan_tags as apitags
from models.database import SCHEMA_VERSION, Database
from models.schemas import IntegrationPanelCommandRequest
from services import event_log, integration_events, panel, scan_tags
from services.profiles import catalog as cat
from services.profiles import rows, store

from .profile_helpers import add_client
from .test_panel import _DB, IPAD_PANEL, _request, sent  # noqa: F401 - fixture

TAG = "q8Zr3vT1yL0pXw5nB2cD4e"


@pytest.fixture(autouse=True)
def _isolate():
    panel.reset()
    event_log.reset()
    integration_events.reset()
    scan_tags.reset()
    yield
    panel.reset()
    event_log.reset()
    scan_tags.reset()


def _admin(db):
    state = types.SimpleNamespace(db=db, ha_client=None)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", is_admin=True),
        app=types.SimpleNamespace(state=state),
        client=None,
        headers={},
        cookies={},
    )


async def _area_with(db, entity_ids):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, 'Eingang', 0, 1, ?, ?)",
        (area_id, now, now),
    )
    await db.execute(
        "INSERT INTO area_sections (id, area_id, title, sort_order) VALUES (?, ?, 'A', 0)",
        (section_id, area_id),
    )
    for n, entity_id in enumerate(entity_ids):
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, sort_order, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, entity_id, section_id, n, now),
        )
    await db.commit()
    return area_id


async def _panel(db, actions, **kw):
    cid = await add_client(
        db,
        name="Flur Panel",
        platform=kw.pop("platform", "android_tablet"),
        profile=kw.pop("profile", "wall_panel"),
        **kw,
    )
    panel.update_status(
        cid, {"panel": {**IPAD_PANEL, "panel_mode": True, "actions": actions}}
    )
    return cid


# ── (a) status ────────────────────────────────────────────────────────────────


def test_new_status_fields_are_validated():
    raw = {
        "pip": True,
        "proximity_sensor": True,
        "device_admin": True,
        "device_owner": False,
        "background_connection": True,
        "nfc": True,
        "charge_limit_detected": False,
        "lock": "device_admin",
        "battery_temperature": 31.46,
        "battery_health": "good",
        "actions": ["wake", "announce", "show_camera", "hack"],
    }
    out = panel.clean_panel(raw)
    assert out["lock"] == "device_admin" and out["battery_temperature"] == 31.5
    assert out["battery_health"] == "good" and out["pip"] is True
    assert out["actions"] == ["wake", "announce", "show_camera"]
    bad = panel.clean_panel(
        {"lock": "root", "battery_temperature": 300, "battery_health": "bad", "nfc": 1}
    )
    assert bad == {}
    assert panel.clean_panel({"battery_temperature": None, "battery_health": None}) == {
        "battery_temperature": None,
        "battery_health": None,
    }


# ── (b) entities ──────────────────────────────────────────────────────────────


async def test_battery_temperature_only_on_android(db):
    phone = await add_client(db, name="Pixel", platform="android", profile="adult")
    iphone = await add_client(db, name="iPhone", platform="ios", profile="adult")
    for cid in (phone, iphone):
        client, _p, rules = await store.resolve_client(db, cid)
        rules = {
            **rules,
            "panel_sensors": {
                **rules["panel_sensors"],
                "value": ["battery", "battery_temperature"],
            },
        }
        ha = panel.panel_ha(client, rules)
        if cid == phone:
            assert ha["sensors"] == ["battery", "battery_temperature"]
        else:
            assert ha["sensors"] == ["battery"]


# ── (c) commands ──────────────────────────────────────────────────────────────


async def _command(db, cid, **kw):
    return await apiintg.integration_panel_command(
        _request(db), cid, IntegrationPanelCommandRequest(**kw)
    )


async def test_reserved_actions_need_the_capability(db, sent):  # noqa: F811
    _DB[0] = db
    cid = await _panel(db, ["wake"])
    assert "announce" not in panel.supported_actions(
        {"id": cid, "platform": "android_tablet", "is_active": 1},
        {"kiosk": {"value": True}},
    )
    with pytest.raises(fastapi.HTTPException) as e:
        await _command(db, cid, action="announce", text="Hallo")
    assert e.value.detail["code"] == "unsupported"


async def test_announce_rule_and_volume_cap(db, sent):  # noqa: F811
    _DB[0] = db
    cid = await _panel(db, ["wake", "announce"])
    sent.answer = lambda m: {"type": "panel_command_ok", "panel": {"screen": "active"}}
    result = await _command(
        db, cid, action="announce", text="  Es hat geklingelt ", volume=95, chime=False
    )
    assert result["success"] is True
    cmd = [c for _cid, c in sent.commands if c.get("command") == "panel_command"][-1]
    # wall panel suggestion v4: on, volume_max 80
    assert cmd["text"] == "Es hat geklingelt" and cmd["volume"] == 80
    assert cmd["chime"] is False
    rows_ = await db.fetchall("SELECT detail_json FROM event_log ORDER BY id")
    assert all("geklingelt" not in (r["detail_json"] or "") for r in rows_)
    with pytest.raises(fastapi.HTTPException) as e:
        await _command(db, cid, action="announce")
    assert e.value.detail["code"] == "invalid"
    with pytest.raises(fastapi.HTTPException) as e:
        await _command(db, cid, action="announce", text="x", volume=101)
    assert e.value.detail["code"] == "invalid"


async def test_announce_off(db, sent):  # noqa: F811
    _DB[0] = db
    off = {"enabled": False, "volume_max": 50, "respect_night": True}
    cid = await _panel(db, ["wake", "announce"], overrides={"panel_announce": off})
    with pytest.raises(fastapi.HTTPException) as e:
        await _command(db, cid, action="announce", text="Hallo")
    assert e.value.detail["code"] == "announce_off"


async def test_show_camera_checks_visibility(db, sent):  # noqa: F811
    _DB[0] = db
    await _area_with(db, ["camera.front_door"])
    tv = await _panel(
        db, ["wake", "show_camera"], platform="android_tv", profile="apple_tv"
    )
    sent.answer = lambda m: {"type": "panel_command_ok", "panel": {"screen": "active"}}
    ok = await _command(db, tv, action="show_camera", entity_id="camera.front_door")
    assert ok["action"] == "show_camera"
    with pytest.raises(fastapi.HTTPException) as e:
        await _command(db, tv, action="show_camera", entity_id="camera.garage")
    assert e.value.detail["code"] == "not_visible"
    with pytest.raises(fastapi.HTTPException) as e:
        await _command(db, tv, action="show_camera", entity_id="light.kitchen")
    assert e.value.detail["code"] == "invalid"
    logged = await db.fetchall(
        "SELECT kind, entity_id FROM event_log WHERE kind LIKE 'panel_command%' ORDER BY id"
    )
    assert logged[0]["entity_id"] == "camera.front_door"


# ── (d) scan tags ─────────────────────────────────────────────────────────────


async def test_scan_tag_admin_rest(db):
    req = _admin(db)
    body = apitags.ScanTagWrite(
        name="Flur Licht", action={"kind": "toggle", "entity_id": "light.hallway"}
    )
    created = await apitags.create_scan_tag(req, body)
    assert scan_tags.TAG_ID_RE.match(created["tag_id"])
    assert created["url"].endswith(f"/t/{created['tag_id']}")
    again = apitags.ScanTagWrite(
        tag_id=created["tag_id"],
        name="x",
        action={"kind": "show", "entity_id": "light.x"},
    )
    with pytest.raises(fastapi.HTTPException) as e:
        await apitags.create_scan_tag(req, again)
    assert e.value.status_code == 409 and e.value.detail["code"] == "tag_exists"
    for bad in (
        {"kind": "toggle"},
        {"kind": "run", "entity_id": "light.hallway"},
        {"kind": "show"},
        {"kind": "show", "area_id": str(uuid.uuid4())},
        {"kind": "explode", "entity_id": "light.x"},
    ):
        with pytest.raises(fastapi.HTTPException) as e:
            await apitags.create_scan_tag(
                req, apitags.ScanTagWrite(name="x", action=bad)
            )
        assert e.value.detail["code"] == "invalid_target", bad
    changed = await apitags.update_scan_tag(
        req,
        created["tag_id"],
        apitags.ScanTagWrite(
            name="Szene", action={"kind": "run", "entity_id": "scene.evening"}
        ),
    )
    assert changed["action"]["kind"] == "run"
    listed = await apitags.list_scan_tags(req)
    assert listed["max"] == 100 and [t["name"] for t in listed["tags"]] == ["Szene"]
    assert (await apitags.delete_scan_tag(req, created["tag_id"]))["success"]
    with pytest.raises(fastapi.HTTPException) as e:
        await apitags.delete_scan_tag(req, created["tag_id"])
    assert e.value.status_code == 404
    kinds = [
        r["kind"]
        for r in await db.fetchall(
            "SELECT kind FROM event_log WHERE category = 'change'"
        )
    ]
    assert kinds == ["scan_tag_set"] * 3


async def test_scan_tag_admin_needs_admin(db):
    req = _request(db, auth="client")
    with pytest.raises(fastapi.HTTPException) as e:
        await apitags.list_scan_tags(req)
    assert e.value.status_code in (401, 403)


async def test_resolve_results_and_log(db, monkeypatch):
    pushed = []

    async def _push(entry, test=False):
        pushed.append(event_log.ha_event_data(entry, test))
        return True

    monkeypatch.setattr(event_log, "push_ha_event", _push)
    await _area_with(db, ["light.hallway"])
    phone = await add_client(db, name="Pixel", platform="android", profile="adult")
    guest = await add_client(db, name="Gast", platform="ios", guest=True)
    await scan_tags.create(
        db,
        name="Flur",
        action={"kind": "toggle", "entity_id": "light.hallway"},
        tag_id=TAG,
    )
    hidden = await scan_tags.create(
        db, name="Garage", action={"kind": "toggle", "entity_id": "light.garage"}
    )
    ok = await scan_tags.resolve(db, phone, TAG, "nfc")
    assert ok == {
        "type": "tag_result",
        "tag_id": TAG,
        "result": "ok",
        "tag": {
            "name": "Flur",
            "action": {"kind": "toggle", "entity_id": "light.hallway", "area_id": None},
        },
    }
    assert (await scan_tags.resolve(db, guest, TAG))["result"] == "tags_off"
    assert (await scan_tags.resolve(db, phone, hidden["tag_id"]))[
        "result"
    ] == "not_visible"
    unknown = await scan_tags.resolve(db, phone, "A" * 22)
    assert unknown == {
        "type": "tag_result",
        "tag_id": "A" * 22,
        "result": "unknown_tag",
    }
    assert (await scan_tags.resolve(db, phone, "../x"))["type"] == "error"
    assert (await scan_tags.get_tag(db, TAG))["last_scanned_at"]
    first = pushed[0]
    assert first["kind"] == "tag_scanned" and first["result"] == "ok"
    assert first["tag_id"] == TAG and first["tag_name"] == "Flur"
    assert [p["result"] for p in pushed] == [
        "ok",
        "tags_off",
        "not_visible",
        "unknown_tag",
    ]


async def test_resolve_rate_limit(db):
    phone = await add_client(db, name="Pixel", platform="android", profile="adult")
    for _ in range(scan_tags.RATE_LIMIT):
        assert (await scan_tags.resolve(db, phone, TAG))["result"] == "unknown_tag"
    limited = await scan_tags.resolve(db, phone, TAG)
    assert limited["result"] == "rate_limited" and limited["retry_after"] >= 1


async def test_resolve_rule_off(db):
    await _area_with(db, ["light.hallway"])
    phone = await add_client(
        db,
        name="Pixel",
        platform="android",
        profile="adult",
        overrides={"scan_tags": False},
    )
    await scan_tags.create(
        db,
        name="Flur",
        action={"kind": "toggle", "entity_id": "light.hallway"},
        tag_id=TAG,
    )
    assert (await scan_tags.resolve(db, phone, TAG))["result"] == "tags_off"


async def test_ws_tag_resolve(db):
    from api.ws import client as ws_client

    phone = await add_client(db, name="Pixel", platform="android", profile="adult")
    out = []

    class _WS:
        async def send_json(self, message):
            out.append(message)

    await ws_client._handle_client_message(
        _WS(),
        db,
        {"type": "tag_resolve", "tag_id": TAG, "via": "qr"},
        "tag_resolve",
        phone,
    )
    assert out == [{"type": "tag_result", "tag_id": TAG, "result": "unknown_tag"}]


# ── (e) migration v16 ─────────────────────────────────────────────────────────


async def _downgrade_to_v15(path):
    db = Database(path)
    await db.initialize()
    await db.close()
    conn = sqlite3.connect(path)
    defaults = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    conn.execute("DROP TABLE scan_tags")
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()
    return defaults


async def test_v16_adds_scan_tags_and_keeps_the_defaults(tmp_path):
    """v16 only adds the tag table; the Android tablet keeps the Wall panel
    default like the iPad."""
    path = str(tmp_path / "v15.db")
    before = await _downgrade_to_v15(path)
    db = Database(path)
    await db.initialize()
    try:
        version = await db.fetchone("PRAGMA user_version")
        assert list(version.values()) == [SCHEMA_VERSION] == [20]
        assert await db.fetchall("SELECT * FROM scan_tags") == []
        tablet = rows.default_setting_key("android_tablet")
        ipad = rows.default_setting_key("ipados")
        after = await db.fetchall(
            "SELECT key, value FROM settings WHERE key IN (?, ?)", (tablet, ipad)
        )
        assert {r["key"]: r["value"] for r in after} == {
            tablet: before[tablet],
            ipad: before[ipad],
        }
        assert before[tablet] == before[ipad]
    finally:
        await db.close()


async def test_v16_runs_twice_without_harm(tmp_path):
    """A database that already has scan_tags (the v16 of the freeze draft)
    keeps its tags when the step runs again."""
    path = str(tmp_path / "v16.db")
    db = Database(path)
    await db.initialize()
    await db.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO scan_tags (tag_id, name, action_kind, entity_id, created_at, "
        "updated_at) VALUES ('t', 'n', 'show', 'light.x', 'a', 'a')"
    )
    conn.execute("PRAGMA user_version = 15")
    conn.commit()
    conn.close()
    db = Database(path)
    await db.initialize()
    try:
        assert [
            r["tag_id"] for r in await db.fetchall("SELECT tag_id FROM scan_tags")
        ] == ["t"]
    finally:
        await db.close()


# ── (f) catalog ───────────────────────────────────────────────────────────────


def test_catalog_reservations():
    rules = {r["key"]: r for r in cat.catalog()["rules"]}
    assert {"tvos", "android_tv"} <= set(rules["camera_live"]["platforms"])
    assert rules["scan_tags"]["pro"] and rules["panel_announce"]["pro"]
    assert rules["panel_off_mode"]["default"] == "black"
    sugs = {s["id"]: s for s in cat.suggestions()["suggestions"]}
    tv = sugs["apple_tv"]
    assert tv["rules"]["kiosk"] is False and tv["rules"]["camera_live"] is False
    assert "kiosk" not in tv["fixed"] and "camera_live" not in tv["fixed"]
    assert "panel_announce" in sugs["kids_room_panel"]["fixed"]
    assert "android_tablet" not in sugs["adult"]["default_for"]
    assert sugs["wall_panel"]["default_for"] == ["ipados", "android_tablet"]
    assert json.dumps(sugs["wall_panel"]["rules"]["panel_announce"]) == json.dumps(
        {"enabled": True, "volume_max": 80, "respect_night": True}
    )
