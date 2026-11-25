"""Wall panel as a Home Assistant device.

The apps report their panel status with ``client_status`` (``battery_state``,
``low_power``, ``panel``). The hub keeps only the last value per device, in
memory: a panel resends it after every (re)connect and as heartbeat, so a hub
restart loses nothing that matters, and no schema migration is needed.

Which entities the integration creates on the client device is decided here
(``panel_ha``): sensors for every device with an app connection (phones
and tablets too, not watches, never guests) as ``panel_sensors`` allows –
the screen state on panels, "app active" everywhere else; control entities
only for devices that are panels by their rules (iPad or Android tablet with
``kiosk`` on, Apple TV or Android TV) with ``panel_remote``. Commands from
Home Assistant or the admin UI go through :func:`send_command`: the hub checks platform, panel mode, guest boundary and
``panel_remote``, sends the WebSocket command ``panel_command`` and waits up
to 5 s for ``panel_command_ok`` / ``panel_command_error``. Every command is
logged (``panel_command`` / ``panel_command_rejected``) with its actor.

Reserved in the contract: the actions ``announce``
(rule ``panel_announce``, text never logged) and ``show_camera`` (a camera
the device may see), offered only when the app lists them in
``panel.actions``; the capabilities ``pip``, ``proximity_sensor``,
``device_admin``, ``device_owner``, ``background_connection``, ``nfc``,
``charge_limit_detected`` and the Android values ``battery_temperature`` /
``battery_health``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from models.database import Database

logger = logging.getLogger("hub.services.panel")

COMMAND_TIMEOUT = 5.0

SCREENS = ("active", "dimmed", "screensaver", "night", "off")
BATTERY_STATES = ("charging", "full", "unplugged", "unknown")
BRIGHTNESS_MODES = ("auto", "manual")
ACTIONS = (
    "wake",
    "screensaver",
    "home",
    "reload",
    "set_brightness",
    "set_brightness_mode",
    "screen_off",
    "announce",
    "show_camera",
)
LOCKS = ("none", "pinning", "lock_task", "device_admin")
BATTERY_HEALTH = (
    "good",
    "overheat",
    "cold",
    "dead",
    "over_voltage",
    "failure",
    "unknown",
)
# Boolean capabilities of panel (contract common/panel-status.json).
CAPABILITIES = (
    "light_sensor",
    "pip",
    "proximity_sensor",
    "device_admin",
    "device_owner",
    "background_connection",
    "nfc",
    "charge_limit_detected",
)
# panel_ha.sensors in this order (contract $defs/panel_ha_sensor).
SENSORS = (
    "battery",
    "charging",
    "screen",
    "app",
    "brightness",
    "illuminance",
    "battery_temperature",
)

# Actions and control entities per platform (panel_ha.controls).
PLATFORM_ACTIONS: dict[str, tuple[str, ...]] = {
    "ipados": (
        "wake",
        "screensaver",
        "home",
        "reload",
        "set_brightness",
        "set_brightness_mode",
        "announce",
        "show_camera",
    ),
    "tvos": ("wake", "screensaver", "home", "reload", "show_camera"),
    # Android: tablet like the iPad plus screen_off, TV like the Apple TV.
    "android_tablet": (
        "wake",
        "screensaver",
        "home",
        "reload",
        "set_brightness",
        "set_brightness_mode",
        "screen_off",
        "announce",
        "show_camera",
    ),
    "android_tv": ("wake", "screensaver", "home", "reload", "show_camera"),
}
# Offered only when the app reports it in panel.actions (lock level dependent;
# announce and show_camera are reserved: apps add them later).
CAPABILITY_ONLY = frozenset({"screen_off", "announce", "show_camera"})
# Rule values of panel_sensors each platform delivers (phones too; watches
# have no hub connection and report nothing). On a device that is no panel the
# rule value "screen" becomes the sensor "app" (app in front or not).
PLATFORM_SENSORS: dict[str, tuple[str, ...]] = {
    "ios": ("battery", "charging", "screen", "brightness"),
    "ipados": ("battery", "charging", "screen", "brightness"),
    "tvos": ("screen",),
    "android": ("battery", "charging", "screen", "brightness", "battery_temperature"),
    "android_tablet": (
        "battery",
        "charging",
        "screen",
        "brightness",
        "battery_temperature",
    ),
    "android_tv": ("screen",),
}
TABLETS = frozenset({"ipados", "android_tablet"})
TVS = frozenset({"tvos", "android_tv"})
ACTION_CONTROL = {
    "set_brightness": "brightness",
    "set_brightness_mode": "brightness_mode",
    "wake": "wake",
    "screensaver": "screensaver",
    "home": "home",
    "reload": "reload",
}
CONTROL_ORDER = (
    "brightness",
    "brightness_mode",
    "wake",
    "screensaver",
    "home",
    "reload",
)

# HTTP status per error code (contract integration/panel-command.error.json).
ERROR_STATUS = {"not_found": 404, "timeout": 504}
APP_ERROR_CODES = ("unsupported", "not_in_panel_mode", "not_pro", "invalid", "failed")
# Default panel_announce when a profile has no value (contract catalog default).
ANNOUNCE_DEFAULT = {"enabled": False, "volume_max": 80, "respect_night": True}

ACTOR_LABEL = {"automation": "Automation", "admin_ui": "Admin UI"}

CAMERA_RE = re.compile(r"^camera\.[a-z0-9_]+$")


class PanelError(Exception):
    """A panel command failed: contract ``code`` and HTTP ``status``."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = ERROR_STATUS.get(code, 422)
        self.message = message or code

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


# ── last status (memory) ──────────────────────────────────────────────────────

_status: dict[str, dict[str, Any]] = {}
_pending: dict[str, tuple[str, asyncio.Future]] = {}


def reset() -> None:
    """Forget every status and pending command (tests)."""
    _status.clear()
    for _client, fut in _pending.values():
        if not fut.done():
            fut.cancel()
    _pending.clear()


def forget(client_id: str) -> None:
    """A device was removed: drop its status."""
    _status.pop(client_id, None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def clean_panel(raw: Any) -> dict[str, Any] | None:
    """Valid fields of a reported panel object; invalid ones are dropped."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    if raw.get("screen") in SCREENS:
        out["screen"] = raw["screen"]
    if isinstance(raw.get("panel_mode"), bool):
        out["panel_mode"] = raw["panel_mode"]
    if "brightness" in raw:
        if raw["brightness"] is None:
            out["brightness"] = None
        else:
            value = _number(raw["brightness"], 0, 1)
            if value is not None:
                out["brightness"] = round(value, 3)
    if raw.get("brightness_mode") in BRIGHTNESS_MODES:
        out["brightness_mode"] = raw["brightness_mode"]
    if "illuminance" in raw:
        if raw["illuminance"] is None:
            out["illuminance"] = None
        else:
            value = _number(raw["illuminance"], 0, 200000)
            if value is not None:
                out["illuminance"] = round(value, 1)
    actions = raw.get("actions")
    if isinstance(actions, list):
        out["actions"] = [a for a in ACTIONS if a in actions]
    for key in CAPABILITIES:
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    if raw.get("lock") in LOCKS:
        out["lock"] = raw["lock"]
    if "battery_temperature" in raw:
        if raw["battery_temperature"] is None:
            out["battery_temperature"] = None
        else:
            value = _number(raw["battery_temperature"], -40, 100)
            if value is not None:
                out["battery_temperature"] = round(value, 1)
    if raw.get("battery_health") is None and "battery_health" in raw:
        out["battery_health"] = None
    elif raw.get("battery_health") in BATTERY_HEALTH:
        out["battery_health"] = raw["battery_health"]
    return out


def update_status(client_id: str, message: dict[str, Any]) -> bool:
    """Merge the panel/battery fields of a client_status; True if any was valid."""
    entry = _status.setdefault(client_id, {})
    changed = False
    if "battery_state" in message:
        value = message["battery_state"]
        if value is None or value in BATTERY_STATES:
            entry["battery_state"] = value
            changed = True
    if "low_power" in message:
        value = message["low_power"]
        if value is None or isinstance(value, bool):
            entry["low_power"] = value
            changed = True
    if "panel" in message:
        raw = message["panel"]
        if raw is None:
            entry["panel"] = None
            entry["panel_updated_at"] = _now()
            changed = True
        else:
            panel = clean_panel(raw)
            if panel is not None:
                entry["panel"] = {**(entry.get("panel") or {}), **panel}
                entry["panel_updated_at"] = _now()
                changed = True
    if not entry:
        _status.pop(client_id, None)
    return changed


def status_of(client_id: str) -> dict[str, Any]:
    """Last battery state, low power, panel and its time (missing = None)."""
    entry = _status.get(client_id) or {}
    return {
        "battery_state": entry.get("battery_state"),
        "low_power": entry.get("low_power"),
        "panel": entry.get("panel"),
        "panel_updated_at": entry.get("panel_updated_at"),
    }


# ── which entities exist ──────────────────────────────────────────────────────


def _rule(rules: dict, key: str) -> Any:
    entry = rules.get(key) or {}
    if entry.get("source") == "not_applicable":
        return None
    return entry.get("value")


def _eligible(client: dict) -> bool:
    """Neither a guest nor deactivated or pending deletion."""
    if client.get("access") == "guest" or client.get("is_guest"):
        return False
    return bool(client.get("is_active", 1)) and not client.get("pending_deletion_at")


def is_panel(client: dict, rules: dict) -> bool:
    """A panel by its rules: tablet (iPad, Android) with wall panel mode on, or
    a TV (Apple TV, Android TV); never
    guests, deactivated or pending-deletion devices."""
    if not _eligible(client):
        return False
    platform = client.get("platform")
    if platform in TVS:
        return True
    return platform in TABLETS and _rule(rules, "kiosk") is True


def remote_allowed(client: dict, rules: dict) -> bool:
    return is_panel(client, rules) and _rule(rules, "panel_remote") is True


def panel_ha(client: dict, rules: dict) -> dict[str, list[str]] | None:
    """Entities of the device in Home Assistant (status ``clients[].panel_ha``).

    ``None`` for guests, deactivated devices and platforms without a status
    (watches: ``panel_sensors`` does not apply). Sensors for every other device,
    controls only for panels with ``panel_remote``.
    """
    if not _eligible(client):
        return None
    platform = client.get("platform") or ""
    wanted = _rule(rules, "panel_sensors")
    if not isinstance(wanted, list) or platform not in PLATFORM_SENSORS:
        return None
    panel_device = is_panel(client, rules)
    allowed = set(PLATFORM_SENSORS[platform])
    panel = status_of(client["id"]).get("panel") or {}
    if platform == "android_tablet" and panel.get("light_sensor") is True:
        allowed.add("illuminance")
    chosen = {s for s in wanted if s in allowed}
    if "screen" in chosen and not panel_device:
        chosen.discard("screen")
        chosen.add("app")
    sensors = [s for s in SENSORS if s in chosen]
    controls: list[str] = []
    if panel_device and _rule(rules, "panel_remote") is True:
        offered = {
            ACTION_CONTROL[a]
            for a in PLATFORM_ACTIONS.get(platform, ())
            if a in ACTION_CONTROL
        }
        controls = [c for c in CONTROL_ORDER if c in offered]
    return {"sensors": sensors, "controls": controls}


def supported_actions(client: dict, rules: dict) -> list[str]:
    """Actions to offer (platform ∩ reported capabilities when reported)."""
    if not is_panel(client, rules):
        return []
    actions = list(PLATFORM_ACTIONS.get(client.get("platform") or "", ()))
    reported = (status_of(client["id"]).get("panel") or {}).get("actions")
    if isinstance(reported, list) and reported:
        return [a for a in actions if a in reported]
    return [a for a in actions if a not in CAPABILITY_ONLY]


# ── commands ──────────────────────────────────────────────────────────────────


def _int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _validate_args(
    action: str,
    level: Any = None,
    mode: Any = None,
    text: Any = None,
    volume: Any = None,
    chime: Any = None,
    entity_id: Any = None,
) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if action == "announce":
        if not isinstance(text, str) or not text.strip() or len(text) > 300:
            raise PanelError(
                "invalid", "text (1–300 characters) is required for announce"
            )
        args["text"] = text.strip()
        if volume is not None:
            value = _int(volume, 0, 100)
            if value is None:
                raise PanelError("invalid", "volume must be 0–100")
            args["volume"] = value
        if chime is not None:
            if not isinstance(chime, bool):
                raise PanelError("invalid", "chime must be true or false")
            args["chime"] = chime
    elif action == "show_camera":
        if not isinstance(entity_id, str) or not CAMERA_RE.match(entity_id):
            raise PanelError("invalid", "entity_id of a camera is required")
        args["entity_id"] = entity_id
    elif action == "set_brightness":
        value = _number(level, 0.05, 1)
        if value is None:
            raise PanelError("invalid", "level 0.05–1 is required for set_brightness")
        args["level"] = round(value, 3)
    elif action == "set_brightness_mode":
        if mode not in BRIGHTNESS_MODES:
            raise PanelError("invalid", "mode auto or manual is required")
        args["mode"] = mode
    return args


async def _check(db: Database, client_id: str, action: str) -> tuple[dict, dict]:
    from services.profiles import store as profiles

    resolved = await profiles.resolve_client(db, client_id)
    if not resolved:
        raise PanelError("not_found", "Client not found")
    client, _profile, rules = resolved
    if not client.get("is_active") or client.get("pending_deletion_at"):
        raise PanelError("deactivated", "Client is deactivated")
    if client.get("access") == "guest":
        raise PanelError("guest", "Guests have no wall panel control")
    if not is_panel(client, rules):
        raise PanelError("not_a_panel", "The device is not a wall panel")
    if _rule(rules, "panel_remote") is not True:
        raise PanelError("remote_off", "Control from Home Assistant is off")
    if action not in supported_actions(client, rules):
        raise PanelError("unsupported", f"{action} is not supported by this device")
    return client, rules


def announce_rule(rules: dict) -> dict[str, Any]:
    """Effective panel_announce (catalog default when the rule has no value)."""
    value = _rule(rules, "panel_announce")
    return (
        {**ANNOUNCE_DEFAULT, **value}
        if isinstance(value, dict)
        else dict(ANNOUNCE_DEFAULT)
    )


async def _check_rule_args(
    db: Database, client_id: str, action: str, rules: dict, args: dict[str, Any]
) -> None:
    """Rule checks of the reserved actions (announce, show_camera)."""
    if action == "announce":
        rule = announce_rule(rules)
        if rule.get("enabled") is not True:
            raise PanelError("announce_off", "Announcements are off for this device")
        cap = rule.get("volume_max")
        if isinstance(cap, int) and not isinstance(cap, bool):
            args["volume"] = min(args.get("volume", cap), cap)
    elif action == "show_camera":
        from services import hub_control

        visible = await hub_control.visible_entities(db, client_id)
        if args["entity_id"] not in visible:
            raise PanelError("not_visible", "The camera is not visible for this device")


def is_pending(client_id: str, command_id: Any) -> bool:
    pending = _pending.get(command_id) if isinstance(command_id, str) else None
    return bool(pending and pending[0] == client_id and not pending[1].done())


def handle_result(client_id: str, message: dict[str, Any]) -> bool:
    """panel_command_ok / panel_command_error of an app; True if it was awaited."""
    command_id = message.get("id")
    pending = _pending.get(command_id) if isinstance(command_id, str) else None
    if not pending or pending[0] != client_id:
        return False
    fut = pending[1]
    if fut.done():
        return False
    if message.get("type") == "panel_command_ok":
        fut.set_result({"ok": True})
    else:
        code = message.get("code")
        if code not in APP_ERROR_CODES:
            code = "failed"
        fut.set_result({"ok": False, "code": code})
    return True


def _camera(action: str, args: dict[str, Any]) -> str | None:
    """The camera of a show_camera command for the log (never the announce text)."""
    entity_id = args.get("entity_id")
    if (
        action == "show_camera"
        and isinstance(entity_id, str)
        and CAMERA_RE.match(entity_id)
    ):
        return entity_id
    return None


def actor_label(actor: dict | None) -> str:
    actor = actor or {}
    if actor.get("kind") == "ha_admin":
        return str(actor.get("name") or "Home Assistant")[:100]
    return ACTOR_LABEL.get(actor.get("kind") or "", "Home Assistant")


async def _log(
    db: Database,
    client_id: str,
    action: str,
    actor: dict | None,
    error: str | None,
    entity_id: str | None = None,
) -> None:
    from services import event_log

    known = await db.fetchone(
        "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
    )
    kind = "panel_command" if error is None else "panel_command_rejected"
    await event_log.record(
        db,
        category=event_log.ACTIVITY,
        kind=kind,
        event_kind=kind,
        source="hub",
        client_id=client_id if known else None,
        entity_id=entity_id,
        action=action if action in ACTIONS else None,
        result="ok" if error is None else "denied",
        actor=actor_label(actor),
        detail={
            "panel_action": action if action in ACTIONS else None,
            "error_code": error,
            "actor_kind": (actor or {}).get("kind"),
        },
    )


async def send_command(
    db: Database,
    client_id: str,
    action: str,
    *,
    actor: dict | None = None,
    timeout: float = COMMAND_TIMEOUT,
    **raw_args: Any,
) -> dict[str, Any]:
    """Check, send and await one panel command; raises :class:`PanelError`.

    ``raw_args``: level, mode (brightness), text, volume, chime (announce),
    entity_id (show_camera); unknown keys are ignored.
    """
    from api import ws as api_ws

    try:
        if action not in ACTIONS:
            raise PanelError("unsupported", f"unknown action {action}")
        _client, rules = await _check(db, client_id, action)
        known = ("level", "mode", "text", "volume", "chime", "entity_id")
        args = _validate_args(action, **{k: raw_args.get(k) for k in known})
        await _check_rule_args(db, client_id, action, rules, args)
        command_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _pending[command_id] = (client_id, fut)
        try:
            sent = await api_ws.send_to_client(
                client_id,
                {
                    "type": "command",
                    "command": "panel_command",
                    "id": command_id,
                    "action": action,
                    **args,
                },
            )
            if not sent:
                raise PanelError(
                    "not_connected", "Client is not connected via WebSocket"
                )
            try:
                result = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise PanelError(
                    "timeout", f"The device did not answer within {timeout:g} s"
                ) from None
        finally:
            _pending.pop(command_id, None)
        if not result["ok"]:
            raise PanelError(result["code"], f"The device answered {result['code']}")
    except PanelError as e:
        await _log(db, client_id, action, actor, e.code, _camera(action, raw_args))
        logger.info("Panel command %s for %s rejected: %s", action, client_id, e.code)
        raise
    await _log(db, client_id, action, actor, None, _camera(action, raw_args))
    logger.info("Panel command %s executed on %s", action, client_id)
    return {
        "success": True,
        "client_id": client_id,
        "action": action,
        "panel": status_of(client_id)["panel"],
    }


# ── admin card ────────────────────────────────────────────────────────────────


async def card(db: Database, client_id: str) -> dict[str, Any] | None:
    """GET /api/pairing/clients/{id}/panel (contract rest/client-panel.response.json)."""
    from api import ws as api_ws
    from services.profiles import store as profiles

    resolved = await profiles.resolve_client(db, client_id)
    if not resolved:
        return None
    client, _profile, rules = resolved
    row = await db.fetchone(
        "SELECT battery_level, is_active, pending_deletion_at FROM paired_clients "
        "WHERE id = ?",
        (client_id,),
    )
    client = {**client, **(row or {})}
    status = status_of(client_id)
    return {
        "client_id": client_id,
        "platform": client.get("platform"),
        "is_connected": client_id in api_ws.get_connected_client_ids(),
        "is_panel": is_panel(client, rules),
        "panel": status["panel"],
        "battery_level": client.get("battery_level"),
        "battery_state": status["battery_state"],
        "low_power": status["low_power"],
        "panel_ha": panel_ha(client, rules),
        "remote": remote_allowed(client, rules),
        "supported_actions": supported_actions(client, rules),
        "updated_at": status["panel_updated_at"],
    }
