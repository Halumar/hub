"""Wall panel as a Home Assistant device: status, entities, commands.

(a) client_status battery_state / low_power / panel are validated, merged and
    kept in memory (no migration);
(b) panel_ha decides the entities of the integration: sensors for every
    device (iPhone/iPad without wall panel get "app" instead of the
    screen state), controls only for the iPad in wall panel mode and the Apple
    TV (wake/screensaver/home/reload); never guests or watches;
    panel_sensors / panel_remote limit them;
(c) POST /api/integration/clients/{id}/panel and the admin test buttons check
    the device, send panel_command and wait for panel_command_ok/_error,
    with contract codes and HTTP 404/422/504;
(d) every command is logged as panel_command / panel_command_rejected with
    the actor;
(e) GET /api/integration/status carries panel, panel_ha, battery_state.
"""

import asyncio
import json
import types
import uuid

import fastapi
import pytest

import api.integration as apiintg
import api.panel as apipanel
from api import ws as api_ws
from api.ws import client as ws_client
from models.schemas import IntegrationPanelCommandRequest, PanelCommandRequest
from services import event_log, integration_events, panel

from .profile_helpers import add_client

IPAD_PANEL = {
    "screen": "dimmed",
    "brightness": 0.15,
    "brightness_mode": "auto",
    "actions": [
        "wake",
        "screensaver",
        "home",
        "reload",
        "set_brightness",
        "set_brightness_mode",
    ],
}


def _request(db, auth="integration"):
    state = types.SimpleNamespace(db=db, ha_client=None)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(state=state),
    )


@pytest.fixture(autouse=True)
def _isolate():
    panel.reset()
    event_log.reset()
    integration_events.reset()
    yield
    panel.reset()
    event_log.reset()


@pytest.fixture
def sent(monkeypatch):
    """Capture broadcasts and panel commands; the fake app answers via .answer."""
    box = types.SimpleNamespace(commands=[], broadcasts=[], answer=None, online=True)

    async def _send(client_id, message):
        if not box.online:
            return False
        box.commands.append((client_id, message))
        if box.answer is not None:
            reply = box.answer(message)
            if reply is not None:

                async def _later():
                    await asyncio.sleep(0)
                    await ws_client._handle_client_message(
                        None,
                        _DB[0],
                        {**reply, "id": message["id"]},
                        reply["type"],
                        client_id,
                    )

                asyncio.get_running_loop().create_task(_later())
        return True

    async def _broadcast(message, *a, **k):
        box.broadcasts.append(message)

    monkeypatch.setattr(api_ws, "send_to_client", _send)
    monkeypatch.setattr(api_ws, "broadcast", _broadcast)
    return box


_DB: list = [None]


async def _ipad(db, **kw):
    kw.setdefault("overrides", {"kiosk": True})
    return await add_client(
        db, name="Küche Panel", platform="ipados", profile="wall_panel", **kw
    )


async def _log_kinds(db):
    rows = await db.fetchall(
        "SELECT kind, result, actor, detail_json FROM event_log ORDER BY id"
    )
    return [
        (r["kind"], r["result"], r["actor"], json.loads(r["detail_json"] or "{}"))
        for r in rows
    ]


# ── (a) status ────────────────────────────────────────────────────────────────


def test_update_status_validates_and_merges():
    cid = str(uuid.uuid4())
    assert panel.update_status(
        cid,
        {
            "battery_state": "charging",
            "low_power": False,
            "panel": {
                **IPAD_PANEL,
                "screen": "sleeping",
                "brightness": 1.5,
                "lock": "x",
            },
        },
    )
    st = panel.status_of(cid)
    assert st["battery_state"] == "charging"
    assert st["low_power"] is False
    assert "screen" not in st["panel"] and "brightness" not in st["panel"]
    assert st["panel"]["actions"][0] == "wake"
    panel.update_status(cid, {"panel": {"screen": "active", "brightness": 0.5}})
    assert panel.status_of(cid)["panel"]["screen"] == "active"
    assert panel.status_of(cid)["panel"]["brightness_mode"] == "auto"  # merged
    assert not panel.update_status(cid, {"battery_state": "discharging"})
    assert panel.status_of(cid)["battery_state"] == "charging"
    panel.update_status(cid, {"panel": None})
    assert panel.status_of(cid)["panel"] is None
    assert not panel.update_status(str(uuid.uuid4()), {"device_name": "x"})


async def test_client_status_stores_panel_and_broadcasts(db, sent):
    cid = await _ipad(db)
    await ws_client._handle_client_status(
        db, cid, {"type": "client_status", "battery_level": 64, "panel": IPAD_PANEL}
    )
    data = sent.broadcasts[-1]["data"]
    assert sent.broadcasts[-1]["type"] == "client_status_updated"
    assert data["panel"]["screen"] == "dimmed"
    assert data["battery_level"] == 64
    # Only panel fields still broadcast (heartbeat of a panel).
    sent.broadcasts.clear()
    await ws_client._handle_client_status(db, cid, {"panel": {"screen": "active"}})
    assert (
        sent.broadcasts and sent.broadcasts[-1]["data"]["panel"]["screen"] == "active"
    )


# ── (b) panel_ha ──────────────────────────────────────────────────────────────


async def _client_rules(db, cid):
    from services.profiles import store

    client, _p, rules = await store.resolve_client(db, cid)
    return client, rules


async def _ha_of(db, cid):
    return panel.panel_ha(*(await _client_rules(db, cid)))


async def test_panel_ha_ipad_wall_panel(db):
    cid = await _ipad(db)
    assert await _ha_of(db, cid) == {
        "sensors": ["battery", "charging", "screen", "brightness"],
        "controls": [
            "brightness",
            "brightness_mode",
            "wake",
            "screensaver",
            "home",
            "reload",
        ],
    }


async def test_panel_ha_ipad_without_wall_panel_mode(db):
    # Sensors, "app active" instead of the screen state, no controls –
    # even with panel_remote on (it needs the wall panel mode).
    cid = await add_client(
        db,
        name="iPad",
        platform="ipados",
        profile="adult",
        overrides={"panel_remote": True},
    )
    assert await _ha_of(db, cid) == {
        "sensors": ["battery", "charging", "app", "brightness"],
        "controls": [],
    }
    assert panel.supported_actions(*(await _client_rules(db, cid))) == []


async def test_panel_ha_apple_tv(db):
    cid = await add_client(db, name="TV", platform="tvos", profile="apple_tv")
    assert await _ha_of(db, cid) == {
        "sensors": ["screen"],
        "controls": ["wake", "screensaver", "home", "reload"],
    }


async def test_panel_ha_iphone_sensors_without_controls(db):
    # The iPhone reports battery, charging, app active, brightness.
    phone = await add_client(db, name="Phone", platform="ios", profile="adult")
    assert await _ha_of(db, phone) == {
        "sensors": ["battery", "charging", "app", "brightness"],
        "controls": [],
    }
    android = await add_client(db, name="Pixel", platform="android", profile="adult")
    assert await _ha_of(db, android) == await _ha_of(db, phone)


async def test_panel_ha_never_guest_or_watch(db):
    guest = await add_client(
        db, name="Gast", platform="ipados", guest=True, overrides={"kiosk": True}
    )
    phone_guest = await add_client(db, name="Gast 2", platform="ios", guest=True)
    watch = await add_client(db, name="Watch", platform="watchos", profile="adult")
    wear = await add_client(db, name="Wear", platform="wearos", profile="adult")
    for cid in (guest, phone_guest, watch, wear):
        assert await _ha_of(db, cid) is None


async def test_panel_ha_adult_suggestion_has_no_remote(db):
    # The suggestion Erwachsene Person sets panel_remote off – an adult iPad
    # that turns its wall panel mode on gets the screen state, no controls.
    cid = await add_client(
        db, name="iPad", platform="ipados", profile="adult", overrides={"kiosk": True}
    )
    assert await _ha_of(db, cid) == {
        "sensors": ["battery", "charging", "screen", "brightness"],
        "controls": [],
    }


async def test_panel_ha_sensor_rule_limits_phone(db):
    cid = await add_client(
        db,
        name="Phone",
        platform="ios",
        profile="adult",
        overrides={"panel_sensors": ["battery", "screen"]},
    )
    assert await _ha_of(db, cid) == {"sensors": ["battery", "app"], "controls": []}
    none = await add_client(
        db,
        name="Phone 2",
        platform="ios",
        profile="adult",
        overrides={"panel_sensors": []},
    )
    assert await _ha_of(db, none) == {"sensors": [], "controls": []}


async def test_client_status_from_phone_outside_panel(db, sent):
    # A phone reports its status like a panel; panel_mode is kept,
    # brightness null in the background.
    cid = await add_client(db, name="Phone", platform="ios", profile="adult")
    await ws_client._handle_client_status(
        db,
        cid,
        {
            "type": "client_status",
            "battery_level": 52,
            "battery_state": "unplugged",
            "panel": {
                "screen": "active",
                "panel_mode": False,
                "brightness": 0.62,
                "actions": [],
            },
        },
    )
    assert sent.broadcasts[-1]["data"]["panel"] == {
        "screen": "active",
        "panel_mode": False,
        "brightness": 0.62,
        "actions": [],
    }
    await ws_client._handle_client_status(
        db, cid, {"panel": {"screen": "off", "panel_mode": "no", "brightness": None}}
    )
    status = panel.status_of(cid)
    assert status["panel"]["screen"] == "off"
    assert status["panel"]["brightness"] is None
    assert status["panel"]["panel_mode"] is False  # invalid value dropped
    assert status["battery_state"] == "unplugged"


async def test_command_to_phone_is_not_a_panel(db, sent):
    cid = await add_client(db, name="Phone", platform="ios", profile="adult")
    with pytest.raises(panel.PanelError) as err:
        await panel.send_command(db, cid, "wake", actor={"kind": "automation"})
    assert err.value.code == "not_a_panel"
    assert not [c for c in sent.commands if c[1].get("command")]


async def test_panel_ha_rules_limit_entities(db):
    cid = await _ipad(
        db,
        overrides={"kiosk": True, "panel_remote": False, "panel_sensors": ["screen"]},
    )
    assert await _ha_of(db, cid) == {"sensors": ["screen"], "controls": []}


# ── (c)/(d) commands ──────────────────────────────────────────────────────────


async def test_command_executed_and_logged(db, sent):
    _DB[0] = db
    cid = await _ipad(db)
    sent.answer = lambda m: {
        "type": "panel_command_ok",
        "panel": {
            "screen": "active",
            "brightness": m["level"],
            "brightness_mode": "manual",
        },
    }
    result = await apiintg.integration_panel_command(
        _request(db),
        cid,
        IntegrationPanelCommandRequest(
            action="set_brightness",
            level=0.4,
            actor={"kind": "ha_admin", "name": "Admin One"},
        ),
    )
    assert result["success"] is True and result["action"] == "set_brightness"
    assert result["panel"]["brightness"] == 0.4
    (_cid, cmd) = [c for c in sent.commands if c[1].get("command")][0]
    assert cmd["type"] == "command" and cmd["command"] == "panel_command"
    assert cmd["action"] == "set_brightness" and cmd["level"] == 0.4 and cmd["id"]
    kinds = await _log_kinds(db)
    assert kinds[-1][0] == "panel_command" and kinds[-1][1] == "ok"
    assert kinds[-1][2] == "Admin One"
    assert kinds[-1][3]["actor_kind"] == "ha_admin"
    assert "level" not in kinds[-1][3]  # no values in the log


async def test_app_error_is_422_and_logged(db, sent):
    _DB[0] = db
    cid = await _ipad(db)
    sent.answer = lambda m: {"type": "panel_command_error", "code": "not_pro"}
    with pytest.raises(fastapi.HTTPException) as exc:
        await apiintg.integration_panel_command(
            _request(db), cid, IntegrationPanelCommandRequest(action="wake")
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "not_pro"
    kinds = await _log_kinds(db)
    assert kinds[-1][0] == "panel_command_rejected"
    assert kinds[-1][2] == "Automation" and kinds[-1][3]["error_code"] == "not_pro"


async def test_unknown_app_code_becomes_failed(db, sent):
    _DB[0] = db
    cid = await _ipad(db)
    sent.answer = lambda m: {"type": "panel_command_error", "code": "boom"}
    with pytest.raises(panel.PanelError) as exc:
        await panel.send_command(db, cid, "wake")
    assert exc.value.code == "failed"


async def test_timeout_is_504(db, sent):
    cid = await _ipad(db)
    with pytest.raises(panel.PanelError) as exc:
        await panel.send_command(db, cid, "home", timeout=0.05)
    assert exc.value.code == "timeout" and exc.value.status == 504
    assert not panel._pending


async def test_not_connected(db, sent):
    cid = await _ipad(db)
    sent.online = False
    with pytest.raises(panel.PanelError) as exc:
        await panel.send_command(db, cid, "wake")
    assert exc.value.code == "not_connected"


@pytest.mark.parametrize(
    ("setup", "action", "kw", "code", "status"),
    [
        ("unknown", "wake", {}, "not_found", 404),
        ("phone", "wake", {}, "not_a_panel", 422),
        ("ipad_no_kiosk", "wake", {}, "not_a_panel", 422),
        ("guest", "wake", {}, "guest", 422),
        ("remote_off", "wake", {}, "remote_off", 422),
        ("tv", "set_brightness", {"level": 0.5}, "unsupported", 422),
        ("ipad", "screen_off", {}, "unsupported", 422),
        ("ipad", "explode", {}, "unsupported", 422),
        ("ipad", "set_brightness", {"level": 0.01}, "invalid", 422),
        ("ipad", "set_brightness", {}, "invalid", 422),
        ("ipad", "set_brightness_mode", {"mode": "loud"}, "invalid", 422),
        ("inactive", "wake", {}, "deactivated", 422),
    ],
)
async def test_hub_checks(db, sent, setup, action, kw, code, status):
    if setup == "unknown":
        cid = str(uuid.uuid4())
    elif setup == "phone":
        cid = await add_client(db, name="Phone", platform="ios", profile="adult")
    elif setup == "ipad_no_kiosk":
        cid = await add_client(db, name="iPad", platform="ipados", profile="adult")
    elif setup == "guest":
        cid = await add_client(
            db, name="G", platform="ipados", guest=True, overrides={"kiosk": True}
        )
    elif setup == "remote_off":
        cid = await _ipad(db, overrides={"kiosk": True, "panel_remote": False})
    elif setup == "tv":
        cid = await add_client(db, name="TV", platform="tvos", profile="apple_tv")
    elif setup == "inactive":
        cid = await _ipad(db, is_active=0)
    else:
        cid = await _ipad(db)
    with pytest.raises(panel.PanelError) as exc:
        await panel.send_command(db, cid, action, **kw)
    assert (exc.value.code, exc.value.status) == (code, status)
    assert [c for c in sent.commands if c[1].get("command") == "panel_command"] == []
    kinds = await _log_kinds(db)
    assert kinds[-1][0] == "panel_command_rejected"
    assert kinds[-1][3]["error_code"] == code


async def test_reported_capabilities_limit_actions(db, sent):
    cid = await _ipad(db)
    panel.update_status(cid, {"panel": {"actions": ["wake"]}})
    with pytest.raises(panel.PanelError) as exc:
        await panel.send_command(db, cid, "home")
    assert exc.value.code == "unsupported"


async def test_result_of_another_device_is_ignored(db, sent):
    cid = await _ipad(db)
    task = asyncio.create_task(panel.send_command(db, cid, "wake", timeout=0.3))
    await asyncio.sleep(0.01)
    command_id = sent.commands[0][1]["id"]
    assert not panel.handle_result(
        str(uuid.uuid4()), {"type": "panel_command_ok", "id": command_id}
    )
    assert panel.handle_result(cid, {"type": "panel_command_ok", "id": command_id})
    assert (await task)["success"] is True


async def test_admin_test_button_and_card(db, sent):
    _DB[0] = db
    cid = await _ipad(db)
    panel.update_status(cid, {"battery_state": "full", "panel": IPAD_PANEL})
    sent.answer = lambda m: {
        "type": "panel_command_ok",
        "panel": {"screen": "screensaver"},
    }
    result = await apipanel.post_client_panel(
        _request(db, "admin"), cid, PanelCommandRequest(action="screensaver")
    )
    assert result["panel"]["screen"] == "screensaver"
    card = await apipanel.get_client_panel(_request(db, "admin"), cid)
    assert card["is_panel"] and card["remote"]
    assert card["battery_state"] == "full"
    assert card["supported_actions"][0] == "wake"
    assert card["panel_ha"]["controls"][0] == "brightness"
    kinds = await _log_kinds(db)
    assert kinds[-1][2] == "Admin UI"
    with pytest.raises(fastapi.HTTPException) as exc:
        await apipanel.get_client_panel(_request(db, "admin"), str(uuid.uuid4()))
    assert exc.value.status_code == 404


async def test_admin_endpoints_need_admin(db):
    with pytest.raises(fastapi.HTTPException) as exc:
        await apipanel.get_client_panel(_request(db, "client"), str(uuid.uuid4()))
    assert exc.value.status_code in (401, 403)


# ── (e) integration status ───────────────────────────────────────────────────


async def test_integration_status_carries_panel(db, sent):
    cid = await _ipad(db)
    phone = await add_client(db, name="Phone", platform="ios", profile="adult")
    panel.update_status(
        cid, {"battery_state": "charging", "low_power": True, "panel": IPAD_PANEL}
    )
    status = await apiintg.integration_status(_request(db))
    by_id = {c["id"]: c for c in status["clients"]}
    assert by_id[cid]["panel"]["screen"] == "dimmed"
    assert by_id[cid]["battery_state"] == "charging" and by_id[cid]["low_power"] is True
    assert by_id[cid]["panel_ha"]["sensors"] == [
        "battery",
        "charging",
        "screen",
        "brightness",
    ]
    # The phone gets sensors (no status reported yet), no controls.
    assert by_id[phone]["panel"] is None
    assert by_id[phone]["panel_ha"] == {
        "sensors": ["battery", "charging", "app", "brightness"],
        "controls": [],
    }
    watch = await add_client(db, name="Watch", platform="watchos", profile="adult")
    status = await apiintg.integration_status(_request(db))
    assert {c["id"]: c for c in status["clients"]}[watch]["panel_ha"] is None


# ── Android ────────────────────────────────────────────────────────────


async def test_android_tablet_and_tv(db, sent):
    tablet = await add_client(
        db, name="Tablet", platform="android_tablet", profile="wall_panel"
    )
    tv = await add_client(
        db, name="Google TV", platform="android_tv", profile="apple_tv"
    )
    assert await _ha_of(db, tablet) == {
        # The wall panel suggestion v4 adds the battery temperature.
        "sensors": [
            "battery",
            "charging",
            "screen",
            "brightness",
            "battery_temperature",
        ],
        "controls": [
            "brightness",
            "brightness_mode",
            "wake",
            "screensaver",
            "home",
            "reload",
        ],
    }
    assert await _ha_of(db, tv) == {
        "sensors": ["screen"],
        "controls": ["wake", "screensaver", "home", "reload"],
    }
    # screen_off only as a reported capability; illuminance only with a light sensor.
    with pytest.raises(panel.PanelError) as exc:
        await panel.send_command(db, tablet, "screen_off")
    assert exc.value.code == "unsupported"
    panel.update_status(
        tablet,
        {
            "panel": {
                "actions": ["wake", "screen_off"],
                "light_sensor": True,
                "illuminance": 120,
            }
        },
    )
    assert (
        "illuminance" not in (await _ha_of(db, tablet))["sensors"]
    )  # not in panel_sensors
    from services.profiles import store

    client, _p, rules = await store.resolve_client(db, tablet)
    rules = {
        **rules,
        "panel_sensors": {**rules["panel_sensors"], "value": ["illuminance"]},
    }
    assert panel.panel_ha(client, rules)["sensors"] == ["illuminance"]
    assert panel.supported_actions(client, rules) == ["wake", "screen_off"]


async def test_profile_without_stored_panel_rules_keeps_remote(db):
    # A profile copied before the panel rules existed has no stored
    # value; it resolves to the catalog default (on); the new suggestions change nothing.
    from services.profiles import store

    cid = await _ipad(db)
    client, profile, _rules = await store.resolve_client(db, cid)
    body = json.loads(profile["body_json"])
    for key in ("panel_remote", "panel_sensors"):
        body["rules"].pop(key, None)
    async with db.transaction():
        await db.execute(
            "UPDATE profiles SET body_json = ? WHERE id = ?",
            (json.dumps(body), profile["id"]),
        )
    assert await _ha_of(db, cid) == {
        "sensors": ["battery", "charging", "screen", "brightness"],
        "controls": [
            "brightness",
            "brightness_mode",
            "wake",
            "screensaver",
            "home",
            "reload",
        ],
    }
