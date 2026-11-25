"""Notifications at events: rules, thresholds, quiet hours, bundling.

The rules (contract ``rest/event-notifications.json``) live in the
setting ``event_notifications``. Each rule names taxonomy kinds
(``data/events.json``), targets (Home Assistant ``notify.*`` actions and/or
paired member devices) and filters. Every stored log entry with a taxonomy
kind passes :func:`on_entry`; the hub sends through its own Home Assistant
connection (``notify.<service>``) and the device notifications of the hub
(shown while the app is open, no push). The HA events ``<ns>_event`` and
``<ns>_alarm`` are independent of these rules (``services.event_log``).

Thresholds, bundles and quiet-hour queues are kept in memory: a restart of
the hub forgets a half-full window (the log keeps every entry). Expiring
guests/adjustments and the hub's own outage detection are timed by
:func:`tick` (every minute) with a persistent mark per rule, so a restart
never sends them twice.

Honest wording: "handed over" means Home Assistant accepted the call; whether
it arrives depends on the service. Halumar is not an alarm system.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

from brand import BRAND_NAME
from models.database import Database
from services import event_log

logger = logging.getLogger("hub.services.event_notify")

SETTINGS_KEY = "event_notifications"
MAX_RULES = 50
DEFAULT_QUIET = {"start": "22:00", "end": "07:00"}
DEFAULT_GUEST_LEAD = 24 * 60
DEFAULT_ADJUSTMENT_LEAD = 60
DEFAULT_OUTAGE_DELAY = 5
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Handled by tick() (timed per rule), not by on_entry().
TIMED_KINDS = ("guest_expiring", "adjustment_expiring")

STATUS_HANDED_OVER = "handed_over"
STATUS_FAILED = "failed"
STATUS_SHOWN = "shown"
STATUS_WAITING = "waiting_for_app"

_windows: dict[tuple[str, str], deque] = {}
_bundles: dict[str, list[dict]] = {}
_bundle_started: dict[str, datetime] = {}
_quiet_queue: dict[str, list[dict]] = {}
# Kinds with notify_min_interval_minutes: last message and the events
# held back since, per (rule, device, kind).
_interval_last: dict[tuple[str, str, str], datetime] = {}
_interval_held: dict[tuple[str, str, str], list[dict]] = {}


def reset() -> None:
    _windows.clear()
    _bundles.clear()
    _bundle_started.clear()
    _quiet_queue.clear()
    _interval_last.clear()
    _interval_held.clear()


def min_interval(kind: str | None) -> int:
    """``notify_min_interval_minutes`` of a taxonomy kind (0 = none)."""
    value = event_log.taxonomy().get(kind or "", {}).get("notify_min_interval_minutes")
    return int(value) if isinstance(value, int) and value > 0 else 0


# ── storage ─────────────────────────────────────────────────────────────────


def empty_filters() -> dict:
    return {
        "client_ids": [],
        "owner_labels": [],
        "profile_ids": [],
        "area_ids": [],
        "entity_ids": [],
        "routes": [],
    }


async def get_config(db: Database) -> dict:
    raw = await db.get_setting(SETTINGS_KEY)
    stored = {}
    if raw:
        try:
            stored = json.loads(raw)
        except ValueError:
            stored = {}
    settings = await event_log.get_settings(db)
    quiet = stored.get("quiet_default") or dict(DEFAULT_QUIET)
    return {
        "retention_days": settings["retention_days"],
        "quiet_default": quiet,
        "rules": stored.get("rules") or [],
    }


async def save_config(db: Database, config: dict) -> dict:
    """Store rules and default quiet hours; retention goes to the log settings."""
    settings = await event_log.get_settings(db)
    settings["retention_days"] = config["retention_days"]
    await event_log.save_settings(db, settings)
    body = {"rules": config["rules"], "quiet_default": config["quiet_default"]}
    async with db.transaction():
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTINGS_KEY, json.dumps(body, ensure_ascii=False, sort_keys=True)),
        )
    reset()
    return await get_config(db)


async def eligible_devices(db: Database) -> list[dict]:
    """Paired devices that may receive notifications: every paired
    device the admin chooses, also children and guests. Children see them in
    the app while the device is at home (hub channel), guests until their
    access ends; expired guests and devices being deleted are left out, and
    so are watches (no hub channel; they show their phone's notifications)."""
    from services.expiry import is_expired
    from services.notifications import is_watch

    rows = await db.fetchall(
        "SELECT c.id, c.name, c.owner_label, c.platform, c.access, c.expires_at, "
        "p.child_protection FROM paired_clients c "
        "LEFT JOIN profiles p ON p.id = c.profile_id "
        "WHERE c.is_active = 1 AND c.pending_deletion_at IS NULL ORDER BY c.name"
    )
    out = []
    for r in rows:
        if is_watch(r):
            continue
        guest = r.get("access") == "guest"
        if guest and r.get("expires_at") and is_expired(r["expires_at"]):
            continue
        out.append(
            {
                "client_id": r["id"],
                "name": r["name"],
                "owner_label": r.get("owner_label") or None,
                "platform": r.get("platform"),
                "kind": "guest"
                if guest
                else ("child" if r.get("child_protection") else "member"),
                "expires_at": r.get("expires_at") if guest else None,
            }
        )
    return out


async def prune_device(db: Database, client_id: str) -> int:
    """Remove a device that left (deleted, guest expired) from every rule's
    targets and filters; a rule without targets left is switched off."""
    config = await get_config(db)
    touched = 0
    for rule in config["rules"]:
        devices = rule["targets"].get("devices") or []
        clients = rule["filters"].get("client_ids") or []
        if client_id not in devices and client_id not in clients:
            continue
        touched += 1
        rule["targets"]["devices"] = [d for d in devices if d != client_id]
        rule["filters"]["client_ids"] = [c for c in clients if c != client_id]
        if not rule["targets"]["devices"] and not rule["targets"].get("notify"):
            rule["enabled"] = False
    if touched:
        await save_config(db, config)
        logger.info(
            "Device %s removed from %d notification rule(s)", client_id, touched
        )
    return touched


async def notify_services(ha_client: Any) -> list[dict]:
    """``notify.*`` actions of Home Assistant (name and description)."""
    services = await ha_client.get_services()
    out = []
    for dom in services or []:
        if dom.get("domain") != "notify":
            continue
        for name, info in sorted((dom.get("services") or {}).items()):
            if name in ("send_message",):
                continue  # entity based, needs a target
            out.append(
                {
                    "service": f"notify.{name}",
                    "name": (info or {}).get("name") or name,
                    "description": (info or {}).get("description") or "",
                }
            )
    return out


# ── texts ───────────────────────────────────────────────────────────────────

_REASON = {
    "de": {
        "blocked_remote": "nicht von unterwegs",
        "blocked_area": "nicht in den Bereichen",
        "blocked_view_only": "nur anzeigen",
        "blocked_quick_access": "nicht über Widgets/Siri",
        "blocked_kids": "für Kinder gesperrt",
        "blocked_kids_bedtime": "Schlafenszeit",
        "blocked_kids_cooldown": "Pause",
        "blocked_kids_color": "Farben gesperrt",
        "blocked_kids_volume": "Lautstärke-Grenze",
        "blocked_kids_paused_all": "alles pausiert",
    },
    "en": {
        "blocked_remote": "not from away",
        "blocked_area": "not in the areas",
        "blocked_view_only": "view only",
        "blocked_quick_access": "not from widgets/Siri",
        "blocked_kids": "blocked for children",
        "blocked_kids_bedtime": "bedtime",
        "blocked_kids_cooldown": "break",
        "blocked_kids_color": "colors locked",
        "blocked_kids_volume": "volume limit",
        "blocked_kids_paused_all": "everything paused",
    },
}


def _who(entry: dict) -> str:
    dev = entry.get("device_name") or ""
    owner = entry.get("owner_label")
    if dev and owner and owner not in dev:
        return f"{dev} ({owner})"
    return dev or owner or BRAND_NAME


def _what(entry: dict) -> str:
    ent = entry.get("entity_name") or entry.get("entity_id") or ""
    action = (entry.get("action") or "").replace("_", " ")
    return f"{ent} {action}".strip()


_PANEL_ACTION = {
    "de": {
        "wake": "aufgeweckt",
        "screensaver": "Bildschirmschoner",
        "home": "zum Startbereich",
        "reload": "neu geladen",
        "set_brightness": "Helligkeit gesetzt",
        "set_brightness_mode": "Helligkeitsmodus gesetzt",
        "screen_off": "Bildschirm aus",
        "announce": "Durchsage",
        "show_camera": "Kamera geöffnet",
    },
    "en": {
        "wake": "woken up",
        "screensaver": "screensaver",
        "home": "back to start",
        "reload": "reloaded",
        "set_brightness": "brightness set",
        "set_brightness_mode": "brightness mode set",
        "screen_off": "screen off",
        "announce": "announcement",
        "show_camera": "camera opened",
    },
}


def _local_hm(value: str | None) -> str:
    """HH:MM in Home Assistant's time zone (cached by the kids mode)."""
    ts = event_log.parse_ts(value)
    if ts is None:
        return ""
    from services import kids_mode

    cache = kids_mode._tz_cache
    return ts.astimezone(event_log._zone(cache[1] if cache else None)).strftime("%H:%M")


def message(entry: dict, lang: str = "de") -> str:
    """Short notification text of a log entry (no action data, no codes)."""
    de = lang == "de"
    kind = entry.get("event_kind") or entry.get("kind")
    who = _who(entry)
    prof = entry.get("profile_name") or ""
    count = entry.get("count") or 1
    reason = _REASON["de" if de else "en"].get(
        entry.get("reason") or "", entry.get("reason") or ""
    )
    away = entry.get("route") == "external"
    if kind in ("control", "kids_control"):
        where = (" · unterwegs" if de else " · away") if away else ""
        return f"{who}: {_what(entry)}{where}"
    if kind in ("denied", "kids_bedtime_attempt", "kids_pause"):
        if count > 1:
            win = (entry.get("detail") or {}).get("window_minutes")
            span = (
                f" in {win} Min." if de and win else (f" in {win} min" if win else "")
            )
            return (
                f"{count} abgelehnte Versuche{span} · {who} · {_what(entry)}"
                if de
                else f"{count} denied attempts{span} · {who} · {_what(entry)}"
            )
        return (
            f"{who}: {_what(entry)} – nicht ausgeführt ({reason})"
            if de
            else f"{who}: {_what(entry)} – not carried out ({reason})"
        )
    if kind == "kids_paused_all":
        trigger = (entry.get("detail") or {}).get("trigger")
        until = _local_hm(entry.get("until"))
        if de:
            why = "zu viele Geräte" if trigger == "entities" else "zu viele Aktionen"
            return f"{who}: alles pausiert bis {until} ({why} in kurzer Zeit)"
        why = "too many devices" if trigger == "entities" else "too many actions"
        return f"{who}: everything paused until {until} ({why} in a short time)"
    camera = (entry.get("detail") or {}).get("camera_qr")
    via_camera = (
        (
            " · mit dem Code der Home-Assistant-Kamera gekoppelt"
            if de
            else " · paired with the code shown on the Home Assistant camera"
        )
        if camera
        else ""
    )
    if kind == "device_paired":
        return (
            f"Neues Gerät gekoppelt: {who}"
            + (f" · Profil {prof}" if prof else "")
            + via_camera
            if de
            else f"New device paired: {who}"
            + (f" · profile {prof}" if prof else "")
            + via_camera
        )
    if kind == "device_removed":
        phone = ((entry.get("detail") or {}).get("with_device") or {}).get("name")
        if phone:
            return (
                f"Gerät entfernt: {who} (mit {phone})"
                if de
                else f"Device removed: {who} (with {phone})"
            )
        return f"Gerät entfernt: {who}" if de else f"Device removed: {who}"
    if kind == "profile_changed":
        if entry.get("kind") == "profile_assigned" and entry.get("client_id"):
            return (
                f"{who}: Profil jetzt „{prof}“"
                if de
                else f"{who}: profile now “{prof}”"
            )
        return f"Profil „{prof}“ geändert" if de else f"Profile “{prof}” changed"
    if kind == "adjustment_expired":
        return (
            f"{who}: befristete Anpassung beendet"
            if de
            else f"{who}: temporary adjustment ended"
        )
    if kind == "adjustment_expiring":
        return (
            f"{who}: befristete Anpassung endet bald"
            if de
            else f"{who}: temporary adjustment ends soon"
        )
    if kind == "guest_invited":
        return (
            "Gast eingeladen" + (f" · Profil {prof}" if prof else "")
            if de
            else "Guest invited" + (f" · profile {prof}" if prof else "")
        )
    if kind == "guest_paired":
        return (
            f"Gast gekoppelt: {who}{via_camera}"
            if de
            else f"Guest paired: {who}{via_camera}"
        )
    if kind == "guest_expiring":
        return (
            f"Gastzugang von {who} endet bald – verlängern auf der Admin-Seite"
            if de
            else f"Guest access of {who} ends soon – extend it on the admin page"
        )
    if kind == "guest_expired":
        return f"Gastzugang von {who} beendet" if de else f"Guest access of {who} ended"
    if kind in ("panel_command", "panel_command_rejected"):
        action = _PANEL_ACTION["de" if de else "en"].get(
            entry.get("action") or "", entry.get("action") or ""
        )
        actor = entry.get("actor") or ""
        by = (f" · von {actor}" if de else f" · by {actor}") if actor else ""
        if kind == "panel_command":
            return (
                f"Wandpanel {who}: {action}{by}"
                if de
                else f"Wall panel {who}: {action}{by}"
            )
        return (
            f"Wandpanel {who}: {action} nicht ausgeführt{by}"
            if de
            else f"Wall panel {who}: {action} not carried out{by}"
        )
    if kind == "access_requested":
        return (
            f"{who} fragt nach frühem Zugang" if de else f"{who} asks for early access"
        )
    if kind == "access_request_answered":
        by = entry.get("actor") or ""
        if entry.get("action") == "yes":
            until = _local_hm(entry.get("until"))
            return (
                f"{by} hat {who} bis {until} geöffnet"
                if de
                else f"{by} opened {who} until {until}"
            )
        return f"{by} hat Nein gesagt ({who})" if de else f"{by} said no ({who})"
    if kind == "access_request_expired":
        return (
            f"Anfrage von {who} unbeantwortet"
            if de
            else f"Request from {who} unanswered"
        )
    if kind == "tag_scanned":
        name = (entry.get("detail") or {}).get("tag_name") or "Tag"
        return f"{who}: Tag „{name}“ gescannt" if de else f"{who}: tag “{name}” scanned"
    if kind == "kids_release_requested":
        return f"{who} fragt um Freigabe" if de else f"{who} asks for approval"
    if kind == "access_group_restored":
        return (
            f"Zugriffsgruppe wiederhergestellt · {who}: Geräte-Benutzer in Home Assistant geändert – wieder auf „nur lesen“"
            if de
            else f"Access group restored · {who}: device user changed in Home Assistant – set back to read-only"
        )
    if kind == "direct_access_attempt":
        return (
            f"Direkter Zugriffsversuch · {who}: {count} Versuche, direkt statt über {BRAND_NAME} zu schalten"
            if de
            else f"Direct access attempt · {who}: {count} attempts to control directly instead of through {BRAND_NAME}"
        )
    if kind == "integration_outage":
        since = event_log.parse_ts(entry.get("since"))
        mins = (
            int((datetime.now(timezone.utc) - since).total_seconds() // 60)
            if since
            else 0
        )
        return (
            f"{BRAND_NAME}-Integration reagiert seit {mins} Min. nicht – geprüfte Geräte können nicht schalten"
            if de
            else f"{BRAND_NAME} integration not responding for {mins} min – checked devices cannot control"
        )
    if kind == "integration_started":
        return (
            f"{BRAND_NAME}-Integration wieder erreichbar"
            if de
            else f"{BRAND_NAME} integration reachable again"
        )
    label = event_log.taxonomy().get(kind or "", {}).get("label", {})
    return label.get(lang) or label.get("en") or str(kind)


def bundle_message(entries: list[dict], lang: str = "de") -> str:
    de = lang == "de"
    if len(entries) == 1:
        return message(entries[0], lang)
    who = {_who(e) for e in entries}
    kinds = {e.get("event_kind") for e in entries}
    label = event_log.taxonomy().get(next(iter(kinds)) or "", {}).get("label", {})
    what = label.get(lang) or label.get("en") or ""
    if len(kinds) > 1:
        what = "Ereignisse" if de else "events"
    who_text = (
        next(iter(who))
        if len(who) == 1
        else (f"{len(who)} Geräte" if de else f"{len(who)} devices")
    )
    return f"{who_text}: {len(entries)}× {what}"


async def _lang(db: Database) -> str:
    try:
        from services.profiles import store

        return "de" if (await store.language(db)) == "de" else "en"
    except Exception:  # noqa: BLE001
        return "en"


# ── matching ────────────────────────────────────────────────────────────────


def matches(rule: dict, entry: dict) -> bool:
    if not rule.get("enabled"):
        return False
    if entry.get("event_kind") not in rule.get("kinds", []):
        return False
    f = rule.get("filters") or {}
    checks = (
        ("client_ids", entry.get("client_id")),
        ("owner_labels", entry.get("owner_label")),
        ("profile_ids", entry.get("profile_id")),
        ("area_ids", entry.get("area_id")),
        ("entity_ids", entry.get("entity_id")),
        ("routes", entry.get("route")),
    )
    for key, value in checks:
        allowed = f.get(key) or []
        if allowed and value not in allowed:
            return False
    return True


def _hm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def in_quiet(rule: dict, now: datetime, tz_name: str | None) -> datetime | None:
    """End of the running quiet window (UTC), or None outside of one."""
    local = now.astimezone(event_log._zone(tz_name))
    minute = local.hour * 60 + local.minute
    for win in rule.get("quiet_hours") or []:
        start, end = _hm(win["start"]), _hm(win["end"])
        days = win.get("days") or list(WEEKDAYS)
        today = WEEKDAYS[local.weekday()]
        yesterday = WEEKDAYS[(local.weekday() - 1) % 7]
        if start == end:
            continue
        if start < end:
            if today in days and start <= minute < end:
                return _local_at(local, end, 0)
        else:
            if today in days and minute >= start:
                return _local_at(local, end, 1)
            if yesterday in days and minute < end:
                return _local_at(local, end, 0)
    return None


def _local_at(local: datetime, minutes: int, add_days: int) -> datetime:
    day = local + timedelta(days=add_days)
    return day.replace(
        hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0
    ).astimezone(timezone.utc)


def _passes_threshold(rule: dict, entry: dict, now: datetime) -> int:
    """Number of matching events when the threshold is reached, else 0."""
    thr = rule.get("threshold") or {"count": 1, "window_minutes": 1}
    count = int(thr.get("count") or 1)
    if count <= 1:
        return 1
    key = (rule["id"], entry.get("client_id") or "*")
    q = _windows.setdefault(key, deque(maxlen=200))
    stamp = now.timestamp()
    q.append(stamp)
    window = int(thr.get("window_minutes") or 1) * 60
    while q and q[0] < stamp - window:
        q.popleft()
    if len(q) >= count:
        n = len(q)
        q.clear()
        return n
    return 0


async def on_entry(db: Database, entry: dict, now: datetime | None = None) -> None:
    kind = entry.get("event_kind")
    if not kind or kind in TIMED_KINDS:
        return
    detail = entry.get("detail") or {}
    if detail.get("derived") or detail.get("live"):
        return  # derived alarm (from single rejections) / timed outage
    config = await get_config(db)
    now = now or datetime.now(timezone.utc)
    tz_name = None
    for rule in config["rules"]:
        if not matches(rule, entry):
            continue
        if kind == "integration_started" and "integration_outage" in rule["kinds"]:
            continue  # "back again" only after a reported outage (outage_ended)
        n = _passes_threshold(rule, entry, now)
        if not n:
            continue
        item = dict(entry)
        if n > 1:
            item["count"] = n
            item["detail"] = {
                **detail,
                "window_minutes": (rule.get("threshold") or {}).get("window_minutes"),
            }
        if rule.get("quiet_hours"):
            if tz_name is None:
                from services import kids_mode

                tz_name = await kids_mode.hub_time_zone(event_log._ha_client) or ""
            if in_quiet(rule, now, tz_name or None):
                if event_log.is_alarm_kind(kind):
                    _quiet_queue.setdefault(rule["id"], []).append(item)
                continue
        gap = min_interval(kind)
        if gap:
            key = (rule["id"], entry.get("client_id") or "*", kind)
            last = _interval_last.get(key)
            if last is not None and now - last < timedelta(minutes=gap):
                # At most one message per device and gap; the rest is bundled.
                _interval_held.setdefault(key, []).append(item)
                continue
            _interval_last[key] = now
        if int(rule.get("bundle_minutes") or 0) > 0:
            _bundles.setdefault(rule["id"], []).append(item)
            _bundle_started.setdefault(rule["id"], now)
            continue
        await send(db, rule, [item])


# ── sending ─────────────────────────────────────────────────────────────────


async def send(
    db: Database,
    rule: dict,
    entries: list[dict],
    *,
    text: str | None = None,
    test: bool = False,
) -> list[dict]:
    """Send one message to every target of ``rule``; returns the result per target."""
    lang = await _lang(db)
    body = text or bundle_message(entries, lang)
    title = BRAND_NAME + (" · Test" if test else "")
    results: list[dict] = []
    ha_client = event_log._ha_client
    for target in (rule.get("targets") or {}).get("notify") or []:
        service = target.split(".", 1)[1]
        try:
            if ha_client is None:
                raise RuntimeError("no Home Assistant connection")
            await ha_client.call_service(
                "notify", service, {"title": title, "message": body}
            )
            results.append({"target": target, "status": STATUS_HANDED_OVER})
        except Exception as e:  # noqa: BLE001 - reported per target
            logger.warning("Notification to %s failed: %s", target, e)
            results.append(
                {"target": target, "status": STATUS_FAILED, "error": str(e)[:120]}
            )
    devices = (rule.get("targets") or {}).get("devices") or []
    if devices:
        from api.notifications import _deliver_notification

        allowed = {d["client_id"] for d in await eligible_devices(db)}
        for cid in devices:
            if cid not in allowed:
                # gone, expired guest: skipped (pruned from the rule on removal)
                results.append(
                    {"target": cid, "status": STATUS_FAILED, "error": "device_gone"}
                )
                continue
            client = await db.fetchone(
                "SELECT id, name FROM paired_clients WHERE id = ?", (cid,)
            )
            try:
                _n, ws_sent = await _deliver_notification(
                    db,
                    client,
                    title=title,
                    message=body,
                    notification_type="default",
                    icon="mdi:history",
                    icon_svg=None,
                    popup=False,
                    popup_mode=None,
                    scheduled_at=None,
                    priority=70
                    if any(e.get("category") == "alarm" for e in entries)
                    else 50,
                    color="none",
                    action_type="none",
                    action_target=None,
                )
                results.append(
                    {
                        "target": cid,
                        "name": client.get("name"),
                        "status": STATUS_SHOWN if ws_sent else STATUS_WAITING,
                    }
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Device notification to %s failed: %s", cid, e)
                results.append(
                    {"target": cid, "status": STATUS_FAILED, "error": str(e)[:120]}
                )
        # The per-device HA sensor is the app's way to its notifications
        # away from home; _deliver_notification leaves it to callers.
        if ha_client is not None and any(
            r["status"] != STATUS_FAILED for r in results if r["target"] in devices
        ):
            from services.ha_sensors import sync_notification_sensors_safe

            await sync_notification_sensors_safe(db, ha_client)
    if not test:
        stamp = event_log.now_iso()
        for entry in entries:
            if entry.get("id"):
                await event_log.set_sent(
                    db,
                    entry["id"],
                    [{**r, "rule": rule.get("name"), "at": stamp} for r in results],
                )
    return results


async def send_test(db: Database, rule: dict) -> dict:
    lang = await _lang(db)
    kind = (rule.get("kinds") or ["control"])[0]
    label = event_log.taxonomy().get(kind, {}).get("label", {})
    text = (
        f"Test der Benachrichtigung „{rule.get('name')}“ ({label.get('de', kind)})"
        if lang == "de"
        else f"Test of the notification “{rule.get('name')}” ({label.get('en', kind)})"
    )
    results = await send(db, rule, [], text=text, test=True)
    fired = await event_log.push_ha_event(
        {
            "event_kind": kind,
            "ts": event_log.now_iso(),
            "client_id": None,
        },
        test=True,
    )
    return {
        "results": results,
        "ha_event": "handed_over" if fired else "integration_not_connected",
    }


# ── timed: bundles, quiet-hour queues, expiring, outage ─────────────────────


async def _mark(db: Database, key: str) -> bool:
    async with db.transaction():
        cursor = await db.execute(
            "INSERT OR IGNORE INTO log_notify_marks (key, ts) VALUES (?, ?)",
            (key, event_log.now_iso()),
        )
    return bool(cursor.rowcount)


async def tick(db: Database, now: datetime, tz_name: str | None) -> None:
    config = await get_config(db)
    rules = {r["id"]: r for r in config["rules"]}
    for rid in list(_bundles):
        rule = rules.get(rid)
        if rule is None:
            _bundles.pop(rid, None)
            _bundle_started.pop(rid, None)
            continue
        started = _bundle_started.get(rid, now)
        if now - started >= timedelta(minutes=int(rule.get("bundle_minutes") or 0)):
            items = _bundles.pop(rid, [])
            _bundle_started.pop(rid, None)
            if items:
                await send(db, rule, items)
    for key in list(_interval_held):
        rule = rules.get(key[0])
        if rule is None or not rule.get("enabled"):
            _interval_held.pop(key, None)
            continue
        last = _interval_last.get(key)
        if last is None or now - last >= timedelta(minutes=min_interval(key[2])):
            items = _interval_held.pop(key, [])
            if items:
                _interval_last[key] = now
                await send(db, rule, items)
    for rid in list(_quiet_queue):
        rule = rules.get(rid)
        if rule is None or not in_quiet(rule, now, tz_name):
            items = _quiet_queue.pop(rid, [])
            if rule and items:
                await send(db, rule, items)
    await _expiring_guests(db, now, config["rules"])
    await _expiring_adjustments(db, now, config["rules"])
    await _outage(db, now, config["rules"])


async def _expiring_guests(db: Database, now: datetime, rules: list[dict]) -> None:
    rows = await db.fetchall(
        "SELECT id, expires_at FROM paired_clients WHERE access = 'guest' "
        "AND expires_at IS NOT NULL AND expires_at != '' AND pending_deletion_at IS NULL"
    )
    leads = [
        (r, int(r.get("lead_minutes") or DEFAULT_GUEST_LEAD))
        for r in rules
        if r.get("enabled") and "guest_expiring" in r.get("kinds", [])
    ]
    for row in rows:
        end = event_log.parse_ts(row["expires_at"])
        if end is None or end <= now:
            continue
        left = end - now
        entry = None
        if left <= timedelta(
            minutes=max([DEFAULT_GUEST_LEAD] + [lead for _, lead in leads])
        ):
            entry = await _expiring_entry(
                db,
                "guest_expiring",
                row["id"],
                row["expires_at"],
                log_now=left <= timedelta(minutes=DEFAULT_GUEST_LEAD),
            )
        for rule, lead in leads:
            if left > timedelta(minutes=lead) or entry is None:
                continue
            if not matches(rule, entry):
                continue
            if await _mark(db, f"r:{rule['id']}:g:{row['id']}:{row['expires_at']}"):
                await send(db, rule, [entry])


async def _expiring_adjustments(db: Database, now: datetime, rules: list[dict]) -> None:
    rows = await db.fetchall(
        "SELECT client_id, rule_key, expires_at FROM device_overrides "
        "WHERE expires_at IS NOT NULL"
    )
    leads = [
        (r, int(r.get("lead_minutes") or DEFAULT_ADJUSTMENT_LEAD))
        for r in rules
        if r.get("enabled") and "adjustment_expiring" in r.get("kinds", [])
    ]
    for row in rows:
        end = event_log.parse_ts(row["expires_at"])
        if end is None or end <= now:
            continue
        left = end - now
        longest = max([DEFAULT_ADJUSTMENT_LEAD] + [lead for _, lead in leads])
        if left > timedelta(minutes=longest):
            continue
        entry = await _expiring_entry(
            db,
            "adjustment_expiring",
            row["client_id"],
            row["expires_at"],
            rule_key=row["rule_key"],
            log_now=left <= timedelta(minutes=DEFAULT_ADJUSTMENT_LEAD),
        )
        for rule, lead in leads:
            if (
                left > timedelta(minutes=lead)
                or entry is None
                or not matches(rule, entry)
            ):
                continue
            key = f"r:{rule['id']}:a:{row['client_id']}:{row['rule_key']}:{row['expires_at']}"
            if await _mark(db, key):
                await send(db, rule, [entry])


async def _expiring_entry(
    db: Database,
    kind: str,
    client_id: str,
    expires_at: str,
    *,
    rule_key: str | None = None,
    log_now: bool,
) -> dict | None:
    uid = f"{kind}:{client_id}:{rule_key or ''}:{expires_at}"
    row = await db.fetchone("SELECT * FROM event_log WHERE uid = ?", (uid,))
    if row:
        return await event_log.view(db, row)
    ctx = await event_log.client_context(db, client_id)
    if not log_now:
        # A rule with a longer lead: notify without a log entry yet.
        return {
            "id": None,
            "event_kind": kind,
            "kind": kind,
            "category": "change",
            "client_id": client_id,
            "until": expires_at,
            **{
                k: ctx.get(k)
                for k in ("device_name", "owner_label", "profile_id", "profile_name")
            },
        }
    return await event_log.record(
        db,
        uid=uid,
        category=event_log.CHANGE,
        kind=kind,
        event_kind=kind,
        client_id=client_id,
        context=ctx,
        until=expires_at,
        detail={"rule": rule_key},
    )


async def _outage(db: Database, now: datetime, rules: list[dict]) -> None:
    since = event_log.outage_since()
    if since is None:
        return
    for rule in rules:
        if not rule.get("enabled") or "integration_outage" not in rule.get("kinds", []):
            continue
        delay = int(rule.get("delay_minutes") or DEFAULT_OUTAGE_DELAY)
        if now - since < timedelta(minutes=delay):
            continue
        if await _mark(db, f"r:{rule['id']}:outage:{event_log.iso(since)}"):
            entry = {
                "id": None,
                "event_kind": "integration_outage",
                "kind": "integration_outage",
                "category": "alarm",
                "since": event_log.iso(since),
            }
            row = await db.fetchone(
                "SELECT id FROM event_log WHERE kind = 'integration_outage' AND since = ? "
                "AND source = 'hub'",
                (event_log.iso(since),),
            )
            if row:
                entry["id"] = row["id"]
            await send(db, rule, [entry])


async def outage_ended(db: Database, since: datetime | None, now: datetime) -> None:
    """Rules that reported the outage and also want "back again" (integration_started)."""
    if since is None:
        return
    config = await get_config(db)
    for rule in config["rules"]:
        if not rule.get("enabled") or "integration_started" not in rule.get(
            "kinds", []
        ):
            continue
        reported = await db.fetchone(
            "SELECT 1 AS x FROM log_notify_marks WHERE key = ?",
            (f"r:{rule['id']}:outage:{event_log.iso(since)}",),
        )
        if reported:
            await send(
                db,
                rule,
                [
                    {
                        "id": None,
                        "event_kind": "integration_started",
                        "kind": "integration_started",
                    }
                ],
            )


def new_rule_id() -> str:
    return str(uuid.uuid4())
