"""Enforcement in Home Assistant: rule snapshots per device.

The hub resolves what every paired device with an HA user may operate
(profile, adjustments incl. temporary ones, boundaries, areas with the own
area, "when away", quick access, kids layers) into a rule snapshot
(contract ``schemas/common/enforcement-snapshot.json``) and hands it to the
integration, which checks every ``<ns>.control`` call with it and keeps the
device user read-only for the control path "checked".

Transport (``docs/enforcement.md`` §3): the complete set right after the
integration's push session authenticated, then the changed snapshots after
every broadcast that may affect them (debounced) and once a minute (time
based changes: expiring adjustments, parental release, guest expiry); the
status poll carries ``enforcement.rules_hashes`` as backstop and
``GET /api/integration/enforcement-rules`` delivers the complete set.

Kids (§4): counter and pause live here (``services.kids_rules.counters``);
the integration asks ``POST /api/integration/kids-check``. When the hub
switches for a child device itself (``services.hub_control``) it passes a
single-use ``grant`` in ``<ns>.control`` so the action is not counted twice;
other devices on the hub path get no grant.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from api import authz
from models.database import Database
from services import access as access_svc
from services import kids_mode, kids_rules
from services import quick_access as qa
from services.profiles import catalog as cat
from services.profiles import resolver as res
from services.profiles import store

logger = logging.getLogger("hub.services.enforcement")

SNAPSHOT_VERSION = 1
PUSH_DEBOUNCE_SECONDS = 1.0
PUSH_NOW_SECONDS = 3.0
PERIODIC_SECONDS = 60.0
MAX_PER_MESSAGE = 500
MAX_ENTITIES = 10000
MAX_QUIET_WINDOWS = 14
HA_USER_ID_LEN = 32

# Domains whose entities run something (contract data/control-allowlist.json
# release_required): released when the admin placed them for the device.
RUN_DOMAINS = frozenset({"scene", "script", "automation", "remote"})

GRANT_SECONDS = 15.0
_grants: dict[str, tuple[str, str, str, float]] = {}

# client_id -> rules_hash last pushed to the integration session.
_pushed: dict[str, str] = {}
# client_id -> control path of the snapshot the integration last got: the HA
# group of the device user follows it, so client_config follows it too.
_paths: dict[str, str] = {}
_context: dict[str, Any] = {}
_push_task: asyncio.Task | None = None
_now_tasks: set[asyncio.Task] = set()
# client_id -> open token deliveries whose snapshot is not pushed yet.
_held_tokens: dict[str, int] = {}
_again = False
_push_lock = asyncio.Lock()

_IGNORED_TYPES = frozenset(
    {
        "ping",
        "pong",
        "client_status",
        "client_status_ok",
        "client_status_updated",
        "client_connected",
        "client_disconnected",
        "enforcement_rules",
        "ha_event",
        "log_entry",
        "log_read",
    }
)


def register(db: Database, ha_client) -> None:
    """Database and HA client for the debounced push (set at startup)."""
    _context["db"] = db
    _context["ha_client"] = ha_client


def reset_state() -> None:
    """Forget pushed hashes, grants and the context (tests)."""
    global _push_task, _again
    _pushed.clear()
    _paths.clear()
    _grants.clear()
    _context.clear()
    _now_tasks.clear()
    _held_tokens.clear()
    _push_task = None
    _again = False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rules_hash(snapshot: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON without ``rules_hash`` and ``issued_at``."""
    body = {k: v for k, v in snapshot.items() if k not in ("rules_hash", "issued_at")}
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def control_path(effective: dict[str, dict]) -> str:
    """Control path of the device in Home Assistant
    (``resolver.snapshot_control_path``): direct stays direct, everything
    else (hub, checked, unknown) is checked."""
    return res.snapshot_control_path(effective)


def _valid_ha_user_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == HA_USER_ID_LEN
        and all(c in "0123456789abcdef" for c in value)
    )


# ── entities of a device ────────────────────────────────────────────────────


async def operable_entities(db: Database, client_id: str) -> dict[str, list[str]]:
    """entity_id -> listed active area ids (``[]`` = run target of a tile).

    The control scope of the device: visible entities of its listed areas
    (effective rule ``areas`` incl. the own area; inactive areas and hidden
    entities are not operable) and the run targets of tiles there.
    """
    area_ids = await authz.listed_area_ids(db, client_id, ignore_access=True)
    return await operable_in(db, area_ids)


async def operable_in(db: Database, area_ids: set[str]) -> dict[str, list[str]]:
    """:func:`operable_entities` of a set of listed active areas."""
    if not area_ids:
        return {}
    marks = ",".join("?" for _ in area_ids)
    rows = await db.fetchall(
        "SELECT ae.area_id, ae.entity_id, ae.tile FROM area_entities ae "
        "JOIN areas a ON a.id = ae.area_id "
        f"WHERE ae.area_id IN ({marks}) AND a.is_active = 1 "
        "AND (ae.is_visible IS NULL OR ae.is_visible = 1)",
        tuple(area_ids),
    )
    out: dict[str, list[str]] = {}
    run_targets: set[str] = set()
    for r in rows:
        areas = out.setdefault(r["entity_id"], [])
        if r["area_id"] not in areas:
            areas.append(r["area_id"])
        tile = kids_mode._loads(r.get("tile"))
        for key in ("tap_action", "hold_action"):
            action = tile.get(key)
            if isinstance(action, dict) and action.get("action") == "run":
                target = action.get("target_entity_id")
                if isinstance(target, str) and "." in target:
                    run_targets.add(target)
    for target in run_targets:
        out.setdefault(target, [])
    return out


def _kids_entity(
    layers: dict[str, Any], entity_id: str, area_ids: list[str], state: dict | None
) -> dict[str, Any]:
    """Kids rules of one entity, the strictest over its areas."""
    blocked = False
    windows: list[dict] = []
    color_lock = False
    volume_max: int | None = None
    rate_limit: dict | None = None
    for area_id in area_ids or [None]:
        lay = kids_rules.Layers(
            profile=layers.get("rules") or {},
            area=(layers.get("areas") or {}).get(area_id) if area_id else None,
            entity=(layers.get("entities") or {}).get(entity_id),
        )
        if kids_rules.access_blocked(lay, entity_id, state):
            blocked = True
        quiet, _ = lay.pick("quiet_hours")
        for window in quiet or []:
            if window not in windows:
                windows.append(window)
        lock, _ = lay.pick("color_lock")
        color_lock = color_lock or bool(lock)
        vmax, _ = lay.pick("volume_max")
        if isinstance(vmax, int) and not isinstance(vmax, bool):
            volume_max = vmax if volume_max is None else min(volume_max, vmax)
        rate, _ = lay.pick("rate_limit")
        if isinstance(rate, dict) and (
            rate_limit is None
            or rate["max_actions"] / max(rate["window_seconds"], 1)
            < rate_limit["max_actions"] / max(rate_limit["window_seconds"], 1)
        ):
            rate_limit = {
                "max_actions": rate["max_actions"],
                "window_seconds": rate["window_seconds"],
                "cooldown_seconds": rate.get("cooldown_seconds") or 0,
            }
    return {
        "blocked": blocked,
        "quiet_hours": [
            {"days": list(w["days"]), "start": w["start"], "end": w["end"]}
            for w in windows[:MAX_QUIET_WINDOWS]
        ],
        "color_lock": color_lock,
        "volume_max": volume_max,
        "rate_limit": rate_limit,
    }


# ── snapshot ────────────────────────────────────────────────────────────────


async def _states(ha_client) -> dict[str, dict] | None:
    if ha_client is None:
        return None
    try:
        return {s["entity_id"]: s for s in await ha_client.get_states() or []}
    except Exception as exc:  # noqa: BLE001 - device classes unknown: fail closed
        logger.debug("enforcement: HA states not available: %s", exc)
        return None


def _device_class(states: dict[str, dict] | None, entity_id: str) -> str | None:
    if not states:
        return None
    dc = ((states.get(entity_id) or {}).get("attributes") or {}).get("device_class")
    return dc if isinstance(dc, str) and dc else None


async def build_snapshot(
    db: Database,
    ha_client,
    entry: dict[str, Any],
    ha_user_id: str,
    *,
    states: dict[str, dict] | None,
    stored_qa: dict[str, dict],
    tz_name: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Rule snapshot of one device (``entry`` from ``store.resolve_all``)."""
    client, prof, effective = entry["client"], entry["profile"], entry["rules"]
    child = bool(prof["child_protection"])
    remote = effective["remote_access"]["value"] is True
    # Children are capped at checked by the resolver; Hub only is checked
    # in Home Assistant (the device user stays read-only).
    path = control_path(effective)
    catalog = cat.catalog()

    entities = await operable_entities(db, client["id"])
    if len(entities) > MAX_ENTITIES:
        entities = dict(sorted(entities.items())[:MAX_ENTITIES])

    known = states is not None
    classes = ({e: _device_class(states, e) for e in entities}, known)
    quick = await qa.resolve_for_client(
        db,
        ha_client,
        {**client, "id": client["id"]},
        stored=stored_qa,
        classes=classes,
        effective=effective,
        ignore_access=True,
    )
    quick_levels = quick["entities"] if quick.get("enabled") else {}

    access: dict[str, dict[str, Any]] = {}
    for entity_id in sorted(entities):
        dc = _device_class(states, entity_id)
        if remote:
            remote_level = res.entity_access(
                effective,
                entity_id=entity_id,
                device_class=dc,
                route="external",
                quick_access=None,
                catalog=catalog,
            )["app"]
        else:
            remote_level = "hidden"
        item: dict[str, Any] = {
            "local": "control",
            "remote": remote_level,
            "quick": quick_levels.get(entity_id) or "off",
        }
        if entity_id.split(".", 1)[0] in RUN_DOMAINS:
            item["run"] = True
        access[entity_id] = item

    kids = None
    if child:
        layers = kids_mode.layers_of(effective) or {
            "rules": {},
            "areas": {},
            "entities": {},
        }
        override = kids_mode.parse_iso(client.get("kids_override_until"))
        if override is not None and override <= now:
            override = None
        kids = {
            "time_zone": tz_name,
            "override_until": kids_mode.iso(override),
            "entities": {
                entity_id: _kids_entity(
                    layers,
                    entity_id,
                    entities[entity_id],
                    (states or {}).get(entity_id),
                )
                for entity_id in access
            },
        }
        # Releases of the family manager: optional fields, only while
        # one runs (the integration compares them with its own clock).
        from services.family import service as family

        # Pause everything: only while switched on, so snapshots of
        # profiles without the rule keep their hash.
        if layers.get("rate_limit_all"):
            kids["rate_limit_all"] = {"enabled": True, **layers["rate_limit_all"]}
        # A child's watch carries its phone's releases (one kids state).
        released = await family.active_releases(db, kids_mode.kids_subject(client), now)
        if released["bedtime"]:
            kids["bedtime_released_until"] = kids_mode.iso(released["bedtime"])
        if released["pause"]:
            kids["pause_released_until"] = kids_mode.iso(released["pause"])
        for entity_id, until in released["entities"].items():
            if entity_id in kids["entities"]:
                kids["entities"][entity_id]["release_until"] = kids_mode.iso(until)

    from services import home_networks as home_networks_svc
    from services import location as location_svc

    home_networks = await home_networks_svc.cidrs(db)
    snapshot: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "client_id": client["id"],
        "ha_user_id": ha_user_id,
        "issued_at": kids_mode.iso(now),
        "control_path": path,
        "remote_access": remote,
        "child_protection": child,
        "entities": access,
        "kids": kids,
        # Access switch and access hours (the integration's own clock).
        "access": access_svc.snapshot_access(prof, effective, client, tz_name, now),
        # Further networks that count as the local route.
        "home_networks": home_networks,
        # Limit for <ns>.report_location and client_location.
        "location": location_svc.snapshot_value(effective),
    }
    snapshot["rules_hash"] = rules_hash(snapshot)
    return snapshot


class RuleSet:
    """Snapshots of every active device with an HA user, plus the control path
    of every active device that has or is getting an HA user (contract
    ``control_paths``), also of those whose snapshot could not be built."""

    def __init__(
        self,
        snapshots: list[dict[str, Any]],
        paths: dict[str, str],
        failed: set[str],
    ) -> None:
        self.snapshots = snapshots
        self.paths = paths
        # Devices with an HA user whose snapshot failed to build: not removed.
        self.failed = failed

    def unbuilt_paths(self) -> dict[str, str]:
        """control_paths of the devices without a snapshot in this set."""
        built = {s["client_id"] for s in self.snapshots}
        return {c: p for c, p in self.paths.items() if c not in built}


def _path_of(entry: dict[str, Any] | None) -> str:
    """Resolved control path of one device; ``checked`` when unknown."""
    if entry is None:
        return "checked"
    try:
        return control_path(entry["rules"])
    except Exception:  # noqa: BLE001 - unknown means the strict path
        return "checked"


async def build_set(db: Database, ha_client) -> RuleSet:
    """Snapshots and control paths (see :class:`RuleSet`)."""
    rows = await db.fetchall(
        "SELECT id, ha_user_id, ha_token_status FROM paired_clients "
        "WHERE is_active = 1 AND pending_deletion_at IS NULL "
        "AND (ha_user_id IS NOT NULL OR ha_token_status = 'pending')"
    )
    users = {
        r["id"]: r["ha_user_id"] for r in rows if _valid_ha_user_id(r["ha_user_id"])
    }
    known = {r["id"] for r in rows if r["id"] in users or not r["ha_user_id"]}
    if not known:
        return RuleSet([], {}, set())
    now = utcnow()
    states = await _states(ha_client)
    # A watch is limited with the device classes this snapshot is built with.
    resolved = await store.resolve_all(
        db, sorted(known), kids_mode.iso(now), classes=store.classes_of(states)
    )
    paths = {client_id: _path_of(resolved.get(client_id)) for client_id in known}
    stored_qa = await qa.stored_levels(db) if users else {}
    tz_name = await kids_mode.hub_time_zone(ha_client) if users else None
    out = []
    failed: set[str] = set()
    for client_id in sorted(users):
        entry = resolved.get(client_id)
        try:
            if entry is None:
                raise LookupError("device or profile not resolvable")
            out.append(
                await build_snapshot(
                    db,
                    ha_client,
                    entry,
                    users[client_id],
                    states=states,
                    stored_qa=stored_qa,
                    tz_name=tz_name,
                    now=now,
                )
            )
        except Exception:  # noqa: BLE001 - one broken device must not stop the rest
            failed.add(client_id)
            logger.exception(
                "enforcement: rule snapshot of %s could not be built; the "
                "integration gets only its control path (%s) and keeps it held "
                "unless that is direct",
                client_id,
                paths[client_id],
            )
    if failed:
        logger.error(
            "enforcement: %d of %d rule snapshot(s) could not be built: %s",
            len(failed),
            len(users),
            ", ".join(sorted(failed)),
        )
    return RuleSet(out, paths, failed)


async def build_all(db: Database, ha_client) -> list[dict[str, Any]]:
    """Snapshots of every active device with an HA user."""
    return (await build_set(db, ha_client)).snapshots


async def build_one(db: Database, ha_client, client_id: str) -> dict[str, Any] | None:
    """Snapshot of one device (None without HA user or when inactive)."""
    row = await db.fetchone(
        "SELECT ha_user_id, is_active, pending_deletion_at FROM paired_clients "
        "WHERE id = ?",
        (client_id,),
    )
    if (
        not row
        or not row.get("is_active")
        or row.get("pending_deletion_at")
        or not _valid_ha_user_id(row.get("ha_user_id"))
    ):
        return None
    now = utcnow()
    states = await _states(ha_client)
    resolved = await store.resolve_all(
        db, [client_id], kids_mode.iso(now), classes=store.classes_of(states)
    )
    if client_id not in resolved:
        return None
    return await build_snapshot(
        db,
        ha_client,
        resolved[client_id],
        row["ha_user_id"],
        states=states,
        stored_qa=await qa.stored_levels(db),
        tz_name=await kids_mode.hub_time_zone(ha_client),
        now=now,
    )


async def rules_hashes(db: Database, ha_client) -> dict[str, str]:
    """client_id -> rules_hash of the current snapshots (status backstop)."""
    if _pushed and _context.get("fresh"):
        return dict(_pushed)
    return {s["client_id"]: s["rules_hash"] for s in await build_all(db, ha_client)}


async def ha_enforcement(db: Database, client_id: str, effective: dict) -> dict:
    """``client_config.ha_enforcement`` of a device with an HA token.

    The control path is the one of the snapshot the integration last got, not
    the profile's new value: the device user's HA group changes only with that
    snapshot, so the app never switches directly while its user is still
    read-only (checked -> direct) and keeps switching directly while it still
    may (direct -> checked). Before the first delivery since the hub started
    the profile's value applies.
    """
    path = _paths.get(client_id) or control_path(effective)
    return {"control_path": path, "rules_hash": _pushed.get(client_id)}


def delivered(snapshots: list[dict[str, Any]]) -> None:
    """The integration got exactly these snapshots (complete set)."""
    _paths.clear()
    _paths.update({s["client_id"]: s["control_path"] for s in snapshots})


def path_changed(devices: list[dict[str, Any]]) -> bool:
    """Whether a profile write changed a device's control path (preview
    ``devices`` of ``services.profiles.changes``)."""
    return any(
        d.get("ha_key", "unchanged") != "unchanged"
        or any(c.get("rule") == "control_path" for c in d.get("changes") or ())
        for d in devices
    )


@contextlib.contextmanager
def token_held(client_id: str):
    """While a new HA user's token is stored and its snapshot pushed, the app
    does not get the token yet (``client_config.ha_token`` stays null), so it
    never signs in before the integration has the device's rules."""
    _held_tokens[client_id] = _held_tokens.get(client_id, 0) + 1
    try:
        yield
    finally:
        left = _held_tokens.get(client_id, 1) - 1
        if left > 0:
            _held_tokens[client_id] = left
        else:
            _held_tokens.pop(client_id, None)


def is_token_held(client_id: str) -> bool:
    return client_id in _held_tokens


def rules_delivered(client_id: str) -> bool:
    """The integration holds a snapshot of the device (pushed since the hub
    started)."""
    return client_id in _pushed


# ── push ────────────────────────────────────────────────────────────────────


def control_paths(
    snapshots: list[dict[str, Any]], unbuilt: dict[str, str] | None = None
) -> dict[str, str]:
    """client_id -> control path, sent next to the snapshots (contract
    $defs/enforcement_control_paths): outside the versioned snapshot body, so
    an integration that rejects a snapshot still holds a checked device
    read-only instead of keeping an earlier "direct". ``unbuilt`` adds the
    devices without a snapshot (build failed, HA user being provisioned)."""
    return {**(unbuilt or {}), **{s["client_id"]: s["control_path"] for s in snapshots}}


async def _send(message: dict[str, Any]) -> bool:
    from api.ws.broadcast import send_to_integration

    return await send_to_integration(message)


async def push(db: Database, ha_client, *, complete: bool = False) -> int:
    """Send changed snapshots (or the complete set) to the integration."""
    async with _push_lock:
        # After a send that did not arrive the integration may miss any
        # snapshot: the next push carries the complete set.
        complete = complete or _context.pop("resend", False)
        rule_set = await build_set(db, ha_client)
        snapshots = rule_set.snapshots
        unbuilt = rule_set.unbuilt_paths()
        current = {s["client_id"]: s["rules_hash"] for s in snapshots}
        # A snapshot that failed to build is not a removal: the device keeps
        # its HA user; the integration gets its control path instead.
        removed = sorted(
            c for c in _pushed if c not in current and c not in rule_set.failed
        )
        if complete:
            send = snapshots
        else:
            send = [
                s for s in snapshots if _pushed.get(s["client_id"]) != s["rules_hash"]
            ]
        unbuilt_changed = unbuilt != _context.get("unbuilt", {})
        if not send and not removed and not complete and not unbuilt_changed:
            return 0
        chunks = [
            send[i : i + MAX_PER_MESSAGE] for i in range(0, len(send), MAX_PER_MESSAGE)
        ] or [[]]
        ok = True
        for i, chunk in enumerate(chunks):
            message = {
                "type": "enforcement_rules",
                "data": {
                    "snapshots": chunk,
                    "control_paths": control_paths(chunk, unbuilt if i == 0 else None),
                    "removed": removed[:MAX_PER_MESSAGE] if i == 0 else [],
                    "complete": complete and len(chunks) == 1,
                },
            }
            ok = await _send(message) and ok
        changed_clients = [
            s["client_id"]
            for s in send
            if _pushed.get(s["client_id"]) != s["rules_hash"]
        ]
        # client_config.ha_enforcement.rules_hash follows the snapshot.
        kept = {c: _pushed[c] for c in rule_set.failed if c in _pushed}
        _pushed.clear()
        _pushed.update({**kept, **current})
        _context["unbuilt"] = unbuilt
        _context["fresh"] = True
        if ok:
            # The integration has the current set (sent now or earlier).
            delivered(snapshots)
        else:
            _context["resend"] = True
            logger.info(
                "enforcement: %d snapshot(s) sent%s, %d removed",
                len(send),
                " (complete)" if complete else "",
                len(removed),
            )
        await _config_changed(changed_clients)
        return len(send)


async def _config_changed(client_ids: list[str]) -> None:
    """Connected apps re-fetch client_config (new rules_hash / control path)."""
    if not client_ids:
        return
    from api import ws as api_ws

    for client_id in client_ids:
        try:
            await api_ws.send_to_client(
                client_id, {"type": "client_config_updated", "data": {}}
            )
        except Exception:  # noqa: BLE001 - the app fetches on its next connect
            pass


async def _debounced_push() -> None:
    global _again
    while True:
        _again = False
        await asyncio.sleep(PUSH_DEBOUNCE_SECONDS)
        db, ha_client = _context.get("db"), _context.get("ha_client")
        if db is not None:
            try:
                await push(db, ha_client)
            except Exception:  # noqa: BLE001 - the periodic run retries
                logger.exception("enforcement: push failed")
        if not _again:
            return


def notify_change(message_type: str | None = None) -> None:
    """Something that may change snapshots happened: push soon (debounced)."""
    global _push_task, _again
    if message_type in _IGNORED_TYPES or (message_type or "").startswith(
        "notification"
    ):
        return
    if "db" not in _context:
        return
    if _push_task is not None and not _push_task.done():
        _again = True
        return
    try:
        _push_task = asyncio.get_running_loop().create_task(_debounced_push())
    except RuntimeError:
        _push_task = None


def _now_done(task: asyncio.Task) -> None:
    _now_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("enforcement: immediate push failed: %s", task.exception())


async def push_now(message_type: str, timeout: float = PUSH_NOW_SECONDS) -> bool:
    """Push changed snapshots right away and wait until they are sent.

    For a device that just got its HA user (token delivery): its snapshot
    reaches the integration before the app learns the token, so its first
    ``<ns>.control`` does not meet ``rules_missing`` and the user is
    read-only before the app can sign in. The push is shielded (never cut
    while sending); after ``timeout`` it finishes in the background and the
    app is told by the push itself. False = not sent yet.
    """
    db, ha_client = _context.get("db"), _context.get("ha_client")
    if db is None:
        return False
    task = asyncio.get_running_loop().create_task(push(db, ha_client))
    _now_tasks.add(task)
    task.add_done_callback(_now_done)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
    except Exception:  # noqa: BLE001 - debounced push, periodic run and poll back it up
        logger.warning("enforcement: immediate push (%s) not finished", message_type)
        notify_change(message_type)
        return False
    return True


async def integration_connected() -> None:
    """The integration's push session authenticated: send the complete set."""
    db, ha_client = _context.get("db"), _context.get("ha_client")
    if db is None:
        return
    try:
        await push(db, ha_client, complete=True)
    except Exception:  # noqa: BLE001 - backstop: status poll + GET
        logger.exception("enforcement: complete push failed")


async def periodic_loop(interval: float = PERIODIC_SECONDS) -> None:
    """Time based changes (adjustment and release ends, guest expiry)."""
    while True:
        await asyncio.sleep(interval)
        db, ha_client = _context.get("db"), _context.get("ha_client")
        if db is not None:
            try:
                await push(db, ha_client)
            except Exception:  # noqa: BLE001 - next round
                logger.exception("enforcement: periodic push failed")


# ── grants and kids check ───────────────────────────────────────────────────


def issue_grant(client_id: str, entity_id: str, action: str) -> str:
    """Single-use pre-authorization for a kids action the hub already counted."""
    now = time.monotonic()
    for key in [k for k, v in _grants.items() if v[3] <= now]:
        _grants.pop(key, None)
    grant = secrets.token_urlsafe(32)
    _grants[grant] = (client_id, entity_id, action, now + GRANT_SECONDS)
    return grant


def consume_grant(
    grant: str | None, client_id: str, entity_id: str, action: str
) -> bool:
    if not grant:
        return False
    entry = _grants.pop(grant, None)
    if entry is None:
        return False
    # Bound to device and entity; the integration may rewrite the action
    # (volume cap: volume_up -> volume_set), which stays the same action.
    return entry[:2] == (client_id, entity_id) and entry[3] > time.monotonic()


_KIDS_REASON = {
    kids_rules.REASON_RATE_LIMITED: "rate_limited",
    kids_rules.REASON_COOLDOWN: "cooldown",
    kids_rules.REASON_QUIET_HOURS: "quiet_hours",
    kids_rules.REASON_PAUSED_ALL: "paused_all",
}


async def kids_check(
    db: Database,
    ha_client,
    *,
    client_id: str,
    entity_id: str,
    action: str,
    grant: str | None,
) -> dict[str, Any]:
    """Answer of ``POST /api/integration/kids-check`` (counts an allowed action;
    releases of the family manager apply like in the hub's own path)."""
    from services.family import service as family

    if consume_grant(grant, client_id, entity_id, action):
        return {"allowed": True, "reason": None, "until": None}
    resolved = await store.resolve_client(db, client_id)
    if not resolved:
        return {"allowed": False, "reason": "blocked", "until": None}
    row, _prof, effective = resolved
    layers = kids_mode.layers_of(effective)
    if layers is None or not row.get("is_active") or row.get("pending_deletion_at"):
        return {"allowed": False, "reason": "blocked", "until": None}
    visible = await operable_entities(db, client_id)
    if entity_id not in visible:
        return {"allowed": False, "reason": "blocked", "until": None}
    # A child's watch counts on its phone: one rate limit, one pause of
    # everything, one cooldown per entity and the phone's releases for both.
    subject = kids_mode.kids_subject(row)
    now = kids_mode.utcnow()
    decision = await kids_mode.decide(
        db,
        ha_client,
        profile=layers,
        client_id=subject,
        entity_id=entity_id,
        service=action,
        data={},
        area_ids=visible[entity_id],
        override_until=kids_mode.parse_iso(row.get("kids_override_until")),
        now=now,
        releases=await family.releases_for_action(db, subject, entity_id),
    )
    if not decision.allowed:
        if decision.start_cooldown_until:
            kids_rules.counters.start_cooldown(
                subject, entity_id, decision.start_cooldown_until
            )
        if decision.start_pause_all_until:
            await kids_mode.pause_all_started(
                db, ha_client, subject, entity_id, action, decision
            )
        return {
            "allowed": False,
            "reason": _KIDS_REASON.get(decision.reason or "", "blocked"),
            "until": kids_mode.iso(decision.until),
        }
    # No await since the decision read the counters.
    kids_rules.counters.record(subject, entity_id, now)
    return {"allowed": True, "reason": None, "until": None}
