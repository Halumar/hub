"""Messages from paired apps and admin sessions once authenticated."""

import json
import logging
import math
import os
import time
import unicodedata

from fastapi import WebSocket

from api import authz
from api import ws as api_ws
from api.ws.broadcast import EventAudience, _notify_config_fetched
from api.ws.registry import (
    ADMIN_SESSION_IDS,
    PSEUDO_CLIENT_IDS,
    _clients,
    discard_sender,
    get_connected_client_ids,
    pending_sends,
    queue_send,
)
from data.mdi_paths import get_mdi_path
from models.database import Database
from namespace import CONFIG_SENSOR_ID
from services import (
    access_requests,
    enforcement,
    guest_invites,
    kids_mode,
    hub_control,
    hub_images,
    hub_read,
    kids_rules,
    location,
    panel,
    quick_access,
    room_climate,
    scan_tags,
    state_relay,
    tiles,
)
from services import home_networks as home_networks_svc
from services.layout import load_sections_with_items
from services.profiles import catalog as cat
from services.profiles import resolver as profile_resolver
from services.profiles import store

logger = logging.getLogger("hub.api.websocket")


async def _purge_tombstone_on_ack(
    websocket: WebSocket, db: Database, client_id: str
) -> None:
    """Hard-purge a client row after its device's wipe-ack (client_reset_ack).

    THE single tombstone purge path: deletes the row (ending the signed
    "deleted" compliance state for this client), tells admin UIs the device is
    gone, replies ``client_reset_ack_ok`` and closes the socket.
    """
    async with db.transaction():
        await db.execute("DELETE FROM paired_clients WHERE id = ?", (client_id,))

    # The app confirmed its reset: now its HA user goes. Also for a
    # device that was offline when it was deleted and just came back.
    from services import device_provisioning

    await device_provisioning.send_deprovision(
        client_id, device_provisioning.DEPROVISION_DELETED
    )

    # Tell admin UIs the device is gone so the waiting row clears.
    try:
        await api_ws.broadcast(
            {
                "type": "client_disconnected",
                "data": {"client_id": client_id},
            },
            audience=EventAudience.ADMIN,
        )
    except Exception:  # noqa: BLE001 - best-effort broadcast
        pass

    # Acknowledge then close the socket.
    try:
        await websocket.send_json({"type": "client_reset_ack_ok"})
    except Exception:  # noqa: BLE001
        pass
    try:
        await websocket.close(code=1000, reason="Client removed")
    except Exception:  # noqa: BLE001
        pass

    logger.info("Purged paired client on client_reset_ack: %s", client_id)


async def _handle_client_leave(
    websocket: WebSocket, db: Database, message: dict, client_id: str | None
) -> None:
    """The device signs itself off before it resets (``client_leave``).

    Removed at once like an admin kick (row, HA user, open invitations; other
    connections close 4001/4003), without a tombstone: the device wipes itself.
    This socket gets ``client_leave_ok`` and a normal close instead.
    """
    if client_id in PSEUDO_CLIENT_IDS or not client_id:
        await websocket.send_json(
            {"type": "error", "message": "client_leave is only for paired clients"}
        )
        return
    if message.get("client_id") != client_id:
        await websocket.send_json(
            {"type": "error", "message": "client_leave client_id mismatch"}
        )
        return

    from api.pairing.clients import REMOVAL_LEFT, end_client_access
    from services.audit_log import log_admin_action

    client = await db.fetchone(
        "SELECT name FROM paired_clients WHERE id = ?", (client_id,)
    )
    # Out of the registry first, so the removal does not close this socket
    # before it got its answer.
    info = _clients.pop(websocket, None)
    try:
        removed = await end_client_access(
            db,
            getattr(websocket.app.state, "ha_client", None),
            client_id,
            reason=REMOVAL_LEFT,
        )
    except Exception as e:  # noqa: BLE001 - never raise out of the handler
        logger.error("client_leave failed for %s: %s", client_id, e)
        removed = False
    if not removed:
        if info is not None:
            _clients[websocket] = info
        await websocket.send_json(
            {"type": "error", "message": "Failed to remove this device"}
        )
        return
    discard_sender(websocket)

    ip = getattr(getattr(websocket, "client", None), "host", None)
    name = (client or {}).get("name") or client_id
    log_admin_action("client_left", f"{name} ({client_id})", ip)
    logger.info("Client %s signed itself off (client_leave)", client_id)
    try:
        await websocket.send_json({"type": "client_leave_ok"})
        await websocket.close(code=1000, reason="Client left")
    except Exception:  # noqa: BLE001 - the app may already be gone
        pass


def _image_src(image_path: str | None) -> str | None:
    """Convert filesystem image_path to servable URL (relative path)."""
    if not image_path:
        return None
    filename = os.path.basename(image_path)
    return f"/images/{filename}"


def _get_image_base_url(websocket: WebSocket) -> str:
    """Derive the image base URL for external clients (iOS app, wall panels).

    External clients need an absolute base URL to build full image URLs:
    e.g. image_base_url = "https://hub.example.com"
         card_image_src = "/images/abc.png"
         full URL = "https://hub.example.com/images/abc.png"

    Priority:
    1. app_url from app configuration (handles reverse proxy / custom domain)
    2. Auto-detect from WebSocket connection URL
    """
    # Prefer configured app_url (set in app configuration for reverse proxy setups)
    app_url = getattr(websocket.app.state, "app_url", "") or ""
    if app_url:
        return app_url

    # Fallback: derive from the WebSocket connection URL
    url = websocket.url
    host = url.hostname or "localhost"
    port = url.port or 8099
    scheme = "https" if url.scheme == "wss" else "http"
    # Omit default ports
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


# ── Data retrieval helpers ───────────────────────────────────────────────────


async def _area_visible_for_client(
    db: Database, client_id: str | None, area_id: str
) -> bool | None:
    """Return whether an area is in a client's list (``get_areas``).

    The effective profile rule ``areas`` decides (``authz.AreaScope``).
    What a client may READ (``get_area``) is decided by ``api/authz.py``.
    Returns ``None`` if the area does not exist / is inactive.
    """
    area = await db.fetchone(
        "SELECT id FROM areas WHERE id = ? AND is_active = 1", (area_id,)
    )
    if not area:
        return None
    scope = await authz.client_area_scope(db, client_id)
    area_tag_rows = await db.fetchall(
        "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
    )
    return authz.area_in_scope(scope, [row["tag"] for row in area_tag_rows], area_id)


def clamp_control_mode(mode: str | None, maximum: str | None) -> str | None:
    """A control mode limited by the rule ``control_mode_max`` (None stays None)."""
    if mode is None or maximum is None:
        return mode
    order = cat.rule("control_mode_max")["order"]
    if mode not in order or maximum not in order:
        return mode
    return order[min(order.index(mode), order.index(maximum))]


async def _control_mode_max(db: Database, client_id: str | None) -> str | None:
    if not client_id or authz.is_admin_session(client_id):
        return None
    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        return None
    return resolved[2]["control_mode_max"]["value"]


async def _get_areas(
    db: Database,
    scope: "authz.AreaScope | None" = None,
    *,
    control_mode_max: str | None = None,
    ha_client=None,
) -> list[dict]:
    """Get the active areas of a scope with entity counts and tags.

    ``scope`` None = every active area (admin). Inactive areas (is_active=0)
    are always excluded for external clients. Area control modes are clamped
    by ``control_mode_max``. With ``ha_client`` every area carries its
    ``room_climate`` (services/room_climate.py).
    """
    areas = await db.fetchall(
        "SELECT a.*, COUNT(ae.id) as entity_count "
        "FROM areas a LEFT JOIN area_entities ae ON a.id = ae.area_id "
        "WHERE a.is_active = 1 "
        "GROUP BY a.id ORDER BY a.sort_order, a.name"
    )

    # Batch-fetch all tags to avoid N+1 queries
    all_tags = await db.fetchall("SELECT area_id, tag FROM area_tags")
    tags_by_area: dict[str, list[str]] = {}
    for row in all_tags:
        tags_by_area.setdefault(row["area_id"], []).append(row["tag"])

    result = []
    for area in areas:
        area_dict = dict(area)
        tags = tags_by_area.get(area["id"], [])
        area_dict["tags"] = tags
        area_dict["card_image_src"] = _image_src(area_dict.get("card_image_path"))
        area_dict["bg_light_src"] = _image_src(area_dict.get("bg_light_path"))
        area_dict["bg_dark_src"] = _image_src(area_dict.get("bg_dark_path"))
        area_dict["control_mode"] = clamp_control_mode(
            area_dict.get("control_mode"), control_mode_max
        )

        # Resolve MDI icon SVG path for iOS rendering
        icon = area_dict.get("icon") or ""
        area_dict["icon_svg"] = (
            get_mdi_path(icon[4:]) if icon.startswith("mdi:") else None
        )

        if scope is not None and not authz.area_in_scope(scope, tags, area["id"]):
            continue

        result.append(area_dict)

    if ha_client is not None:
        climates = await room_climate.for_areas(db, ha_client, result)
        for area_dict in result:
            area_dict["room_climate"] = climates[area_dict["id"]]
    for area_dict in result:
        room_climate.strip_columns(area_dict)
    return result


def _clamp_area_modes(area: dict, maximum: str | None) -> None:
    """Clamp the control modes of an area detail (area and entities)."""
    if maximum is None:
        return
    area["control_mode"] = clamp_control_mode(area.get("control_mode"), maximum)
    for section in area.get("sections") or []:
        for entity in section.get("entities") or []:
            if isinstance(entity, dict):
                entity["control_mode"] = clamp_control_mode(
                    entity.get("control_mode"), maximum
                )


async def _get_area_detail(db: Database, ha_client, area_id: str) -> dict | None:
    """Get area detail with sections (containing entities enriched with live HA state, and widgets)."""
    area = await db.fetchone("SELECT * FROM areas WHERE id = ?", (area_id,))
    if not area:
        return None

    # Sections with their entities and widgets (shared with REST)
    sections = await load_sections_with_items(db, area_id)

    # Enrich entities with current HA state
    total_entities = 0
    for section in sections:
        for entity_dict in section["entities"]:
            state = None
            try:
                state_data = await ha_client.get_state(entity_dict["entity_id"])
                if state_data:
                    state = {
                        "state": state_data.get("state"),
                        "attributes": state_data.get("attributes", {}),
                        "last_changed": state_data.get("last_changed"),
                    }
            except Exception:
                pass
            entity_dict["state"] = state
            # Always an object ({} = default), with the hub outputs
            # icon_svg / secondary_state; same for every client (Pro is
            # enforced in the app, run targets are the admin's decision).
            entity_dict["tile"] = await tiles.tile_output(
                entity_dict.get("tile"), state, ha_client
            )
            total_entities += 1

    # Fetch tags
    tags_rows = await db.fetchall(
        "SELECT tag FROM area_tags WHERE area_id = ?", (area_id,)
    )

    area_dict = dict(area)
    area_dict["tags"] = [row["tag"] for row in tags_rows]
    area_dict["card_image_src"] = _image_src(area_dict.get("card_image_path"))
    area_dict["bg_light_src"] = _image_src(area_dict.get("bg_light_path"))
    area_dict["bg_dark_src"] = _image_src(area_dict.get("bg_dark_path"))

    # Resolve MDI icon SVG path for iOS rendering
    icon = area_dict.get("icon") or ""
    area_dict["icon_svg"] = get_mdi_path(icon[4:]) if icon.startswith("mdi:") else None

    area_dict["entity_count"] = total_entities
    area_dict["section_count"] = len(sections)
    area_dict["sections"] = sections
    known = {e["entity_id"]: e.get("state") for s in sections for e in s["entities"]}
    area_dict["room_climate"] = (
        await room_climate.for_areas(db, ha_client, [area_dict], known)
    )[area_id]
    room_climate.strip_columns(area_dict)

    return area_dict


def _rule_for_app(key: str, entry: dict, child: bool) -> dict:
    """One entry of ``client_config.rules`` (defaults omitted)."""
    rule = cat.rule(key) or {}
    out = {
        "value": entry["value"],
        "source": entry["source"],
        "reason": entry["reason"],
        "locked": entry["locked"],
        "fixed": entry["fixed"],
    }
    if entry.get("user_choice"):
        out["user_choice"] = True
    if entry.get("expires_at"):
        out["expires_at"] = entry["expires_at"]
        out["then"] = entry.get("then")
    enforcement = entry.get("enforcement") or rule.get("enforcement")
    if enforcement:
        out["enforcement"] = enforcement
    return out


def kids_view(rules: dict[str, dict]) -> dict:
    """``client_config.kids.rules``: the kids rules in the kids-rules shape."""
    value = {k: rules.get(k, {}).get("value") for k in rules}
    profile = {
        "blocked_types": value.get("kids_blocked_types") or [],
        "quiet_hours": value.get("kids_quiet_hours") or [],
        "rate_limit": value.get("kids_rate_limit"),
        "color_lock": bool(value.get("kids_color_lock")),
        "volume_max": value.get("kids_volume_max"),
    }
    areas: dict[str, dict] = {}
    entities: dict[str, dict] = {}
    for exc in value.get("kids_exceptions") or []:
        target = exc.get("target") or {}
        if target.get("area_id"):
            areas[target["area_id"]] = exc.get("rules") or {}
        elif target.get("entity_id"):
            entities[target["entity_id"]] = exc.get("rules") or {}
    return {"profile": profile, "areas": areas, "entities": entities}


def config_hash(rev: int, access: dict, rules: dict) -> str:
    import hashlib

    canonical = json.dumps(
        {"rev": rev, "access": access, "rules": rules},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _get_client_config(
    db: Database, client_id: str, ha_client=None, *, sealed_for_watch: bool = False
) -> dict | None:
    """``client_config`` of a paired device.

    The effective profile rules (resolver) with source, reason and who
    enforces them; access, profile, kids rules, quick access, the remote
    PIN hash only with ``app_settings = pin`` and the Home Assistant fields
    from ``resolver.ha_access``: a token only when the device holds a key
    (``holds_key``: Checked or Direct, or Hub only with effective remote
    access) and its access is open; the external URL only with effective
    remote access. A watch never gets its token in clear: only
    ``sealed_for_watch`` (services/watch_access.py seals the result to the
    watch's key) keeps it.
    """
    ha_url = await db.get_setting("ha_url")
    ha_internal_url = await db.get_setting("ha_internal_url")
    ha_instance_id = await db.get_setting("ha_instance_id")
    home_networks = await home_networks_svc.cidrs(db)
    default_control_mode = (await db.get_setting("default_control_mode")) or "full"
    home_title = await db.get_setting("home_title")
    home_bg_light_path = await db.get_setting("home_bg_light_path")
    home_bg_dark_path = await db.get_setting("home_bg_dark_path")

    if not client_id or client_id in ADMIN_SESSION_IDS:
        from services import access as access_svc

        return {
            "ha_token": None,
            "ha_url": ha_url,
            "ha_internal_url": ha_internal_url,
            "home_networks": home_networks,
            "config_sensor": CONFIG_SENSOR_ID,
            "home_title": home_title,
            "home_bg_light": _image_src(home_bg_light_path),
            "home_bg_dark": _image_src(home_bg_dark_path),
            "control_mode": default_control_mode,
            "access": {
                "kind": "member",
                "expires_at": None,
                "hours": {
                    "open": True,
                    "reason": None,
                    "until": None,
                    "opens_at": None,
                    "schedule": False,
                    "switch": None,
                },
                "time_zone": access_svc.client_time_zone(),
            },
            "profile": None,
            "rules": {},
            "quick_access": quick_access.disabled(),
            "settings_pin_hash": None,
            "settings_pin_salt": None,
            "settings_pin_source": None,
            "message": "HA token is only available for paired clients",
        }

    client = await db.fetchone(
        "SELECT * FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not client:
        return None
    # A child's watch follows its phone (profile, adjustments, kids state).
    client, prof, overrides, limit, tags = await store.follow_phone(db, client)
    if not prof:
        return None
    effective = store.resolve_device(client, prof, overrides, limit, tags=tags)
    child = bool(prof["child_protection"])
    rules = {
        key: _rule_for_app(key, entry, child)
        for key, entry in profile_resolver.client_rules(effective).items()
    }
    # Who set a device adjustment (admin or a family manager).
    for o in overrides:
        entry = rules.get(o["rule_key"])
        if (
            entry is not None
            and entry.get("source") == "device"
            and o.get("created_by")
        ):
            entry["set_by"] = o["created_by"][:100]

    # The per-client HA token is the token of the device's own HA user,
    # delivered by the integration. Until it arrives ha_token is None
    # (the app waits for client_config_updated). token_error distinguishes
    # "no token yet" (None) from "token exists but won't decrypt".
    from services.crypto import decrypt_token_with_status

    ha_token = None
    token_error = None
    if client.get("ha_token_encrypted") and not enforcement.is_token_held(client_id):
        ha_token, token_error = await decrypt_token_with_status(
            db, client["ha_token_encrypted"]
        )
        if token_error and client.get("is_active"):
            # E.g. a backup restored without its key file: drop the unusable
            # token and ask the integration for the same user's token again.
            from services import device_provisioning

            await db.mark_client_pending(client_id)
            device_provisioning.spawn(
                device_provisioning.request_provision(
                    db, client_id, reason=device_provisioning.REASON_REPROVISION
                )
            )

    # The external URL only with effective remote access: without it HA
    # refuses the token from outside and the app would only collect failed
    # logins (HA IP ban). A device on the hub path without remote access
    # holds no key: it reads and switches through the hub.
    remote = effective["remote_access"]["value"] is True
    effective_ha_url = ha_url if remote else None
    device_key = profile_resolver.holds_key(effective)
    if not device_key:
        ha_token = None
        token_error = None
    # Outside its access the device keeps its pairing but gets no key.
    from services import access as access_svc

    if ha_client is not None:
        await kids_mode.hub_time_zone(ha_client)
    hours = access_svc.state_of(client, prof, effective)
    if hours.get("early"):
        # The device learns who opened it early, not that device's id or route.
        hours["early"] = {
            **hours["early"],
            "by": access_requests.requester_actor(hours["early"].get("by")),
        }
    closed = not hours["open"]
    if closed:
        ha_token = None
        token_error = None
        effective_ha_url = None
    from services.onboarding import WATCH_PLATFORMS

    if client.get("platform") in WATCH_PLATFORMS and not sealed_for_watch:
        ha_token = None
    # A watch holds a key only while its phone's effective rule watch is on.
    phone = None
    if client.get("platform") in WATCH_PLATFORMS:
        phone = limit if limit is not None else await store.phone_rules(db, client)
    ha = profile_resolver.ha_access(
        effective,
        platform=cat.device_platform(client.get("platform")),
        child=child,
        access_open=not closed,
        phone=phone,
    )
    if not ha["holds_key"]:
        ha_token = None
        token_error = None
    elif (
        ha_token is None
        and not token_error
        and not closed
        and (client.get("platform") not in WATCH_PLATFORMS or sealed_for_watch)
    ):
        # Holds a key, but the token has not arrived yet or is held back
        # until the integration has the device's rule snapshot.
        token_error = "pending"

    notification_sensor = None
    if client.get("name"):
        from services.ha_sensors import client_notification_sensor_id

        notification_sensor = client_notification_sensor_id(client["name"], client_id)

    from services.tls import TLS_PORT, get_fingerprint

    tls_fingerprint = get_fingerprint()

    # The PIN hash is offline-crackable (PBKDF2 of 8 digits): it only goes to
    # a device whose settings open with it.
    pin = None
    if effective["app_settings"]["value"] == "pin":
        pin = store.effective_pin(client, prof)

    access = {
        **store.access_of(client),
        "set_up_by": store.set_up_by_shown(client),
        "hours": hours,
        "time_zone": access_svc.client_time_zone(),
    }
    # "Ask for early access" (only while the rule is on).
    request_config = await access_requests.request_config(
        db, client, prof, effective, hours
    )
    if request_config is not None:
        access["request"] = request_config
    result = {
        "ha_token": ha_token,
        "ha_url": effective_ha_url,
        "ha_internal_url": ha_internal_url,
        # Second proof of the home network (on the hub path only together
        # with the key the device gets now).
        "ha_instance_id": ha_instance_id
        if ha["ha_instance_id"]
        and (ha_token is not None or not (ha["hub_path"] or child))
        else None,
        # Further networks that count as at home (every device).
        "home_networks": home_networks,
        "tls_fingerprint": tls_fingerprint,
        "tls_port": TLS_PORT if tls_fingerprint else None,
        "config_sensor": CONFIG_SENSOR_ID,
        "notification_sensor": notification_sensor,
        "home_title": home_title,
        "home_bg_light": _image_src(home_bg_light_path),
        "home_bg_dark": _image_src(home_bg_dark_path),
        "control_mode": clamp_control_mode(
            default_control_mode, effective["control_mode_max"]["value"]
        ),
        "access": access,
        "profile": {
            "id": prof["id"],
            "name": prof["name"],
            "kind": prof.get("kind") or "person",
            "rev": prof["rev"],
            "child_protection": child,
            "managed_by": await db.get_setting("ha_owner_name"),
        },
        "rules": rules,
        # Widgets, controls and Siri of this device from rules.quick_access.
        "quick_access": await quick_access.resolve_for_client(
            db, quick_access.ha_client(), client, effective=effective
        ),
        "settings_pin_hash": pin[0] if pin else None,
        "settings_pin_salt": pin[1] if pin else None,
        "settings_pin_source": pin[2] if pin else None,
        "config_hash": config_hash(prof["rev"], access, rules),
    }
    # Control path in Home Assistant: checked = only through
    # <ns>.control. Only together with the key (Hub only: checked); without
    # it the hub controls (null).
    result["ha_enforcement"] = (
        None
        if ha["ha_enforcement"] is None or ha_token is None
        else await enforcement.ha_enforcement(db, client_id, effective)
    )
    if token_error:
        result["token_error"] = token_error
    from services.family import service as family

    if child:
        override = kids_mode.parse_iso(client.get("kids_override_until"))
        if override and override <= kids_mode.utcnow():
            override = None
        result["kids"] = {
            "control": "hub",
            # Away through Home Assistant (<ns>.control) exactly when the
            # device holds a key.
            "away": ha["kids_away"],
            "time_zone": await kids_mode.hub_time_zone(ha_client),
            "override_until": kids_mode.iso(override),
            "rules": kids_view(effective),
        }
        # A child's watch shows its phone's kids state (one state for both).
        subject = kids_mode.kids_subject(client)
        paused_all = kids_rules.counters.paused_all(subject, kids_mode.utcnow())
        if paused_all is not None:
            # Everything paused (only while it runs).
            result["kids"]["paused_all_until"] = kids_mode.iso(paused_all)
        if client.get("access") == "member":
            result["kids"].update(
                await family.client_config_kids(db, ha_client, subject)
            )
    family_config = (
        None
        if closed
        else await family.client_config_family(db, client_id, effective, client, child)
    )
    if family_config is not None:
        result["family"] = family_config
    sitter = await family.client_config_babysitter(db, client, prof, effective, hours)
    if sitter is not None:
        result["babysitter"] = sitter
    return result


# ── Client status tracking ───────────────────────────────────────────────────

# Whitelist of columns that can be updated via client_status message.
# Prevents SQL injection via dynamic column names. The app version is not
# among them: it comes with auth/pair.
_CLIENT_STATUS_COLUMNS = (
    "device_name",
    "battery_level",
    "connection_type",
    "current_area_id",
)

# Limits of client-controlled values: they are stored, broadcast to
# admin UIs and mirrored into HA sensor states (max 255 characters).
DEVICE_NAME_MAX = 100
CONNECTION_TYPE_MAX = 32
AREA_ID_MAX = 64
HOME_AREA_ID = "__home__"


def _clean_text(value, limit: int) -> str | None:
    """Strip control/format characters and whitespace, cut to ``limit``."""
    if not isinstance(value, str):
        raise ValueError("not a string")
    cleaned = "".join(
        ch for ch in value if not unicodedata.category(ch).startswith("C")
    ).strip()
    return cleaned[:limit] or None


def _number(value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("not a number")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise ValueError("out of range")
    return number


async def _status_value(db: Database, column: str, value):
    """Validated value for one client_status column; raises ValueError."""
    if value is None:
        return None
    if column == "device_name":
        return _clean_text(value, DEVICE_NAME_MAX)
    if column == "connection_type":
        return _clean_text(value, CONNECTION_TYPE_MAX)
    if column == "battery_level":
        return int(round(_number(value, -1, 100)))
    # current_area_id: an existing area or the home screen.
    if not isinstance(value, str) or len(value) > AREA_ID_MAX:
        raise ValueError("invalid area id")
    if value == HOME_AREA_ID:
        return value
    if not await db.fetchone("SELECT id FROM areas WHERE id = ?", (value,)):
        raise ValueError("unknown area")
    return value


async def _handle_client_status(db: Database, client_id: str, message: dict) -> None:
    """Update a paired client's status in the database and broadcast the change.

    Accepts any subset of: device_name, battery_level, connection_type,
    current_area_id, location_shared.
    Only whitelisted columns are written (prevents SQL injection); invalid
    values are skipped silently, text is cleaned and cut.
    battery_state, low_power and panel and location_shared are
    kept in memory only (services/panel.py, services/location.py).
    """
    updates: dict[str, object] = {}
    panel_changed = panel.update_status(client_id, message)
    if "location_shared" in message:
        try:
            panel_changed = (
                location.set_shared(client_id, message["location_shared"])
                or panel_changed
            )
        except ValueError:
            pass  # Skip invalid values silently

    for col in _CLIENT_STATUS_COLUMNS:
        if col in message:
            try:
                updates[col] = await _status_value(db, col, message[col])
            except (ValueError, TypeError):
                pass  # Skip invalid values silently

    if not updates and not panel_changed:
        return

    # Always update last_seen when status is reported
    updates["last_seen"] = Database.now()

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [client_id]
    async with db.transaction():
        await db.execute(
            f"UPDATE paired_clients SET {set_clause} WHERE id = ?", tuple(values)
        )

    logger.debug("Client %s status updated: %s", client_id, list(updates.keys()))

    await broadcast_client_status(db, client_id)


async def broadcast_client_status(db: Database, client_id: str) -> None:
    """Send a device's status to admin sessions (and the integration)."""
    client = await db.fetchone(
        "SELECT id, name, device_name, app_version, battery_level, "
        "connection_type, current_area_id, last_seen "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )

    if client:
        # Resolve current area name if set
        area_name = None
        if client.get("current_area_id"):
            area = await db.fetchone(
                "SELECT name FROM areas WHERE id = ?",
                (client["current_area_id"],),
            )
            if area:
                area_name = area["name"]

        connected_ids = get_connected_client_ids()
        status = panel.status_of(client_id)
        await api_ws.broadcast(
            {
                "type": "client_status_updated",
                "data": {
                    **dict(client),
                    "battery_state": status["battery_state"],
                    "low_power": status["low_power"],
                    "panel": status["panel"],
                    "current_area_name": area_name,
                    **location.status_of(client_id),
                    "is_connected": client_id in connected_ids,
                },
            },
            audience=EventAudience.ADMIN,
        )


# ── Authenticated message handlers ───────────────────────────────────────────


async def _handle_client_message(
    websocket: WebSocket,
    db: Database,
    message: dict,
    msg_type: str,
    client_id: str | None,
) -> None:
    """Handle a message of an authenticated app or admin session."""
    if msg_type == "ping":
        await websocket.send_json({"type": "pong"})

    elif msg_type == "pong":
        # Client responding to our ping - mark connection as alive
        info = _clients.get(websocket)
        if info:
            info["last_pong"] = time.time()

    elif msg_type == "subscribe":
        await websocket.send_json(
            {"type": "subscribed", "data": message.get("area_id", "all")}
        )

    elif msg_type == "get_areas":
        try:
            scope = await authz.client_area_scope(db, client_id)
            areas = await _get_areas(
                db,
                None if authz.is_admin_session(client_id) else scope,
                control_mode_max=await _control_mode_max(db, client_id),
                ha_client=getattr(websocket.app.state, "ha_client", None),
            )
            if not authz.is_admin_session(client_id):
                areas = [authz.strip_server_paths(a) for a in areas]
            await websocket.send_json(
                {
                    "type": "areas",
                    "data": areas,
                    "image_base_url": _get_image_base_url(websocket),
                }
            )
        except Exception as e:
            logger.error("get_areas failed: %s", e)
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Failed to load areas",
                }
            )

    elif msg_type == "get_area":
        area_id = message.get("area_id", "")
        if not area_id:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "area_id is required",
                }
            )
        else:
            try:
                ha_client = websocket.app.state.ha_client
                # A paired client only reads areas of its own scope
                # (listed or deep-linked by its notifications). Same answer as
                # for a missing area, so ids of hidden areas do not leak.
                area = None
                if await authz.can_read_area(db, client_id, area_id):
                    area = await _get_area_detail(db, ha_client, area_id)
                if area and not authz.is_admin_session(client_id):
                    area = authz.redact_area_for_client(area)
                    _clamp_area_modes(area, await _control_mode_max(db, client_id))
                if area:
                    # Debug: log section/entity details for client troubleshooting
                    for sec in area.get("sections", []):
                        ent_summary = ", ".join(
                            f"{e.get('entity_id', '?')}:so={e.get('sort_order', '?')}"
                            for e in sec.get("entities", [])
                        )
                        logger.debug(
                            "[get_area] Section '%s': columns=%s, column_span=%s, entities=[%s]",
                            sec.get("title", ""),
                            sec.get("columns"),
                            sec.get("column_span"),
                            ent_summary,
                        )
                    await websocket.send_json(
                        {
                            "type": "area",
                            "data": area,
                            "image_base_url": _get_image_base_url(websocket),
                        }
                    )
                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "message": "Area not found",
                        }
                    )
            except Exception as e:
                logger.error("get_area failed: %s", e)
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Failed to load area",
                    }
                )

    # There is no message that reads a single HA state: apps read states
    # directly from HA with their own token. "get_entity_state" gets the
    # generic "Unknown message type" error.

    elif msg_type == "get_client_config":
        try:
            config = await _get_client_config(
                db, client_id, getattr(websocket.app.state, "ha_client", None)
            )
            if config:
                await websocket.send_json(
                    {
                        "type": "client_config",
                        "data": config,
                    }
                )
                _notify_config_fetched(client_id)
            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Client config not found",
                    }
                )
        except Exception as e:
            logger.error("get_client_config failed: %s", e)
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Failed to load client config",
                }
            )

    # Entity control is not proxied: apps call HA directly with their own HA
    # token. An incoming "call_service" message gets the generic "Unknown
    # message type" error.

    elif msg_type == "control":
        # Devices on the hub path switch through the hub (services.hub_control);
        # every other device gets not_hub_controlled.
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            await websocket.send_json(
                hub_control.error_message(
                    hub_control.request_id_of(message),
                    hub_control.ERR_NOT_HUB_CONTROLLED,
                )
            )
        else:
            try:
                answer = await hub_control.control(
                    db, websocket.app.state.ha_client, client_id, message
                )
            except Exception as e:  # noqa: BLE001 - never break the socket
                logger.error("control failed: %s", e)
                answer = hub_control.error_message(
                    hub_control.request_id_of(message), hub_control.ERR_HA_ERROR
                )
            await websocket.send_json(answer)

    elif msg_type == "state_subscribe":
        # Live states for devices on the hub path; others read Home Assistant
        # with their own key.
        if (
            client_id in PSEUDO_CLIENT_IDS
            or not client_id
            or not await hub_control.uses_hub_path(db, client_id)
        ):
            await websocket.send_json(
                hub_control.error_message(None, hub_control.ERR_NOT_HUB_CONTROLLED)
            )
        elif (wait := hub_control.subscribe_retry_after(client_id)) is not None:
            await websocket.send_json(
                hub_control.error_message(None, hub_control.ERR_RATE_LIMITED, wait)
            )
        else:
            count = await state_relay.subscribe(
                websocket,
                db,
                websocket.app.state.ha_client,
                client_id,
                websocket.send_json,
                # Relayed states through the connection's queue.
                push=lambda m: queue_send(websocket, json.dumps(m)),
                backlog=lambda: pending_sends(websocket),
            )
            await websocket.send_json(
                {"type": "state_subscribed", "entity_count": count}
            )
            # Current states once: the device has no HA socket of its own.
            await state_relay.send_snapshot(websocket, websocket.app.state.ha_client)

    elif msg_type in ("history_get", "forecast_get"):
        # Sensor charts and weather forecast for devices on the hub path.
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            answer = hub_control.error_message(
                hub_read.request_id_of(message), hub_control.ERR_NOT_HUB_CONTROLLED
            )
        else:
            read = hub_read.history if msg_type == "history_get" else hub_read.forecast
            try:
                answer = await read(
                    db, websocket.app.state.ha_client, client_id, message
                )
            except Exception as e:  # noqa: BLE001 - never break the socket
                logger.error("%s failed: %s", msg_type, e)
                answer = hub_control.error_message(
                    hub_read.request_id_of(message), hub_control.ERR_HA_ERROR
                )
        await websocket.send_json(answer)

    elif msg_type in hub_images.KINDS:
        # Camera still images and media artwork for devices on the hub path.
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            answer = hub_control.error_message(
                hub_control.request_id_of(message), hub_control.ERR_NOT_HUB_CONTROLLED
            )
        else:
            try:
                answer = await hub_images.image(
                    db, websocket.app.state.ha_client, client_id, message
                )
            except Exception as e:  # noqa: BLE001 - never break the socket
                logger.error("%s failed: %s", msg_type, e)
                answer = hub_control.error_message(
                    hub_control.request_id_of(message), hub_control.ERR_HA_ERROR
                )
        await websocket.send_json(answer)

    elif msg_type == "notification_ack":
        # Client acknowledges receipt of a notification
        notification_id = message.get("notification_id", "")
        if not notification_id:
            await websocket.send_json(
                {"type": "error", "message": "notification_id is required"}
            )
        elif client_id in PSEUDO_CLIENT_IDS:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "notification_ack is only for paired clients",
                }
            )
        else:
            try:
                from services.notifications import (
                    acknowledge_notification,
                    get_notification,
                )

                # Verify the notification belongs to this client
                notif = await get_notification(db, notification_id)
                if not notif or notif.get("client_id") != client_id:
                    await websocket.send_json(
                        {"type": "error", "message": "Notification not found"}
                    )
                else:
                    await acknowledge_notification(
                        db, notification_id, delivered_via="ws"
                    )
                    await websocket.send_json(
                        {
                            "type": "notification_ack_ok",
                            "notification_id": notification_id,
                        }
                    )

                    # Broadcast to admin UIs so they see the delivery status
                    # update (same shape as the remote-ack broadcast).
                    await api_ws.broadcast(
                        {
                            "type": "notification_delivered",
                            "data": {
                                "notification_id": notification_id,
                                "client_id": client_id,
                                "via": "ws",
                            },
                        },
                        audience=EventAudience.ADMIN,
                    )

                    # Sync HA notification sensors
                    try:
                        from services.ha_sensors import sync_notification_sensors_safe

                        ha_client = websocket.app.state.ha_client
                        await sync_notification_sensors_safe(db, ha_client)
                    except Exception:
                        pass
            except Exception as e:
                logger.error("notification_ack failed: %s", e)
                await websocket.send_json(
                    {"type": "error", "message": "Failed to acknowledge notification"}
                )

    elif msg_type == "notification_dismiss":
        # Client's X-button removes a notification server-side (like a web-UI
        # delete) so it stays gone across reconnects.
        notification_id = message.get("notification_id", "")
        if not notification_id:
            await websocket.send_json(
                {"type": "error", "message": "notification_id is required"}
            )
        elif client_id in PSEUDO_CLIENT_IDS:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "notification_dismiss is only for paired clients",
                }
            )
        else:
            try:
                from services.notifications import (
                    dismiss_notification,
                    get_notification,
                )

                # Verify the notification belongs to this client.
                notif = await get_notification(db, notification_id)
                if not notif or notif.get("client_id") != client_id:
                    await websocket.send_json(
                        {"type": "error", "message": "Notification not found"}
                    )
                else:
                    await dismiss_notification(db, notification_id)
                    await websocket.send_json(
                        {
                            "type": "notification_dismiss_ok",
                            "notification_id": notification_id,
                        }
                    )

                    # Broadcast to admin UIs so they see the dismissal.
                    await api_ws.broadcast(
                        {
                            "type": "notification_dismissed",
                            "data": {
                                "notification_id": notification_id,
                                "client_id": client_id,
                            },
                        },
                        audience=EventAudience.ADMIN,
                    )

                    # Sync HA notification sensors.
                    try:
                        from services.ha_sensors import sync_notification_sensors_safe

                        ha_client = websocket.app.state.ha_client
                        await sync_notification_sensors_safe(db, ha_client)
                    except Exception:
                        pass
            except Exception as e:
                logger.error("notification_dismiss failed: %s", e)
                await websocket.send_json(
                    {"type": "error", "message": "Failed to dismiss notification"}
                )

    elif msg_type == "client_reset_ack":
        # App confirms it handled client_removed and is clearing its credentials.
        # This explicit ack is the PRIMARY trigger that removes the
        # "waiting for device" tombstone row for an online-at-delete client.
        ack_client_id = message.get("client_id", "")
        if client_id in PSEUDO_CLIENT_IDS:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "client_reset_ack is only for paired clients",
                }
            )
        elif ack_client_id != client_id:
            # Mismatched id: do NOT delete anything.
            await websocket.send_json(
                {"type": "error", "message": "client_reset_ack client_id mismatch"}
            )
        else:
            try:
                # Hard-purge the tombstone row (it was pending-deletion).
                await _purge_tombstone_on_ack(websocket, db, client_id)
            except Exception as e:  # noqa: BLE001 - never raise out of the handler
                logger.error("client_reset_ack failed: %s", e)

    elif msg_type == "client_leave":
        await _handle_client_leave(websocket, db, message, client_id)

    elif msg_type == "get_notifications":
        # THE single WS content channel: the full content of
        # this client's active notifications, with items in the SAME shape as
        # the real-time display_notification push (notification_id + content
        # fields, plus status). Used for connect-time sync while the hub
        # WS is reachable; the per-client HA notification sensor carries the
        # same push-shaped items for the remote path (only HA reachable).
        if client_id in PSEUDO_CLIENT_IDS:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "get_notifications is only for paired clients",
                }
            )
        else:
            try:
                from services.notifications import (
                    get_client_notifications,
                    notification_to_push_payload,
                )

                # The whole active set of the device (at most 50, the same as
                # its HA sensor): a card the answer does not list is gone, also
                # one whose dismiss the device missed while offline.
                notifications = await get_client_notifications(
                    db, client_id, active_only=True
                )
                await websocket.send_json(
                    {
                        "type": "notifications",
                        "notifications": [
                            notification_to_push_payload(n) for n in notifications
                        ],
                    }
                )
            except Exception as e:
                logger.error("get_notifications failed: %s", e)
                await websocket.send_json(
                    {"type": "error", "message": "Failed to load notifications"}
                )

    elif msg_type == "client_status":
        # Client reports its current device status (location, battery, current view, etc.)
        # Only paired clients (not ingress/dev) can report status.
        if client_id in PSEUDO_CLIENT_IDS:
            await websocket.send_json(
                {"type": "error", "message": "client_status is only for paired clients"}
            )
        else:
            try:
                await _handle_client_status(db, client_id, message)
                await websocket.send_json({"type": "client_status_ok"})
            except Exception as e:
                logger.error("client_status failed: %s", e)
                await websocket.send_json(
                    {"type": "error", "message": "Failed to update client status"}
                )

    elif msg_type == "location_report":
        # Position of a device on the hub path; devices off it use
        # <ns>.report_location with their own key.
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "location_report is only for paired clients",
                }
            )
        else:
            body = {k: v for k, v in message.items() if k != "type"}
            try:
                answer = await location.report(db, client_id, body, via="ws")
            except location.LocationError as err:
                answer = err.answer()
            await websocket.send_json(answer)

    elif msg_type == "tag_resolve":
        # NFC/QR tag: the hub answers what the tag does and logs the
        # scan; the app switches afterwards like a widget (quick_access).
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            await websocket.send_json(
                {"type": "error", "message": "tag_resolve is only for paired clients"}
            )
        else:
            await websocket.send_json(
                await scan_tags.resolve(
                    db, client_id, message.get("tag_id"), message.get("via")
                )
            )

    elif msg_type in ("panel_command_ok", "panel_command_error"):
        # Answer of an app to a panel_command; no reply.
        if client_id and client_id not in PSEUDO_CLIENT_IDS:
            if (
                msg_type == "panel_command_ok"
                and panel.is_pending(client_id, message.get("id"))
                and isinstance(message.get("panel"), dict)
            ):
                await _handle_client_status(db, client_id, {"panel": message["panel"]})
            panel.handle_result(client_id, message)

    elif msg_type in (
        "onboard_lookup",
        "onboard_confirm",
        "onboard_decline",
        "get_setup_grant",
        "get_set_up_devices",
        "set_up_device_remove",
    ):
        # Setting up another device: paired clients only.
        from api.ws import onboarding as onboarding_ws

        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            await onboarding_ws.send_error(websocket, "not_permitted")
        elif msg_type == "onboard_lookup":
            await onboarding_ws.handle_lookup(websocket, db, message, client_id)
        elif msg_type == "onboard_confirm":
            await onboarding_ws.handle_confirm(websocket, db, message, client_id)
        elif msg_type == "onboard_decline":
            await onboarding_ws.handle_decline(websocket, db, message, client_id)
        elif msg_type == "get_setup_grant":
            await onboarding_ws.handle_get_grant(websocket, db, message, client_id)
        elif msg_type == "get_set_up_devices":
            await onboarding_ws.handle_list_set_up(websocket, db, client_id)
        else:
            await onboarding_ws.handle_remove_set_up(websocket, db, message, client_id)

    elif msg_type == "watch_access_get":
        # The phone that set up a watch carries its sealed access.
        from services import watch_access

        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "watch_access_get is only for paired clients",
                }
            )
        else:
            await websocket.send_json(
                await watch_access.message(
                    db,
                    getattr(websocket.app.state, "ha_client", None),
                    message.get("client_id"),
                    client_id,
                )
            )

    elif msg_type in ("guest_invite_create", "guest_invite_cancel"):
        # Guest invitations by member devices: paired clients only.
        from api.ws import guest_invites as guest_invites_ws

        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            await guest_invites_ws.send_error(websocket, guest_invites.NOT_PERMITTED)
        elif msg_type == "guest_invite_create":
            await guest_invites_ws.handle_create(websocket, db, message, client_id)
        else:
            await guest_invites_ws.handle_cancel(websocket, db, message, client_id)

    elif msg_type in access_requests.TYPES:
        # Early access: requests of closed devices, answers of recipients.
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            answers = [access_requests.AccessRequestError("not_permitted").message()]
        else:
            try:
                answers = await access_requests.handle_message(
                    db,
                    getattr(websocket.app.state, "ha_client", None),
                    client_id,
                    message,
                    "home",
                )
            except Exception as e:  # noqa: BLE001 - never break the socket
                logger.error("%s failed: %s", msg_type, e, exc_info=True)
                answers = [
                    access_requests.AccessRequestError("invalid_request").message()
                ]
        for answer in answers:
            await websocket.send_json(answer)

    elif (isinstance(msg_type, str) and msg_type.startswith("family_")) or msg_type in (
        "kids_request",
        "kids_request_cancel",
    ):
        # Family manager: family_* of member devices, kids_request* of
        # child devices; the hub decides everything (services/family).
        from api.ws import family as family_ws

        await family_ws.handle(websocket, db, message, msg_type, client_id)

    else:
        await websocket.send_json(
            {
                "type": "error",
                "message": "Unknown message type",
            }
        )
