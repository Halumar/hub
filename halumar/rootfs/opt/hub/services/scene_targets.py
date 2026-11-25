"""Scenes and scripts whose targets a profile or device may not switch.

The integration refuses a released scene or script when one of its targets
is not allowed for the device on its own (contract ``docs/enforcement.md``
§2 step 4). This module finds those targets in advance for the Admin UI
(profile editor and save preview, area page, "Effective rules" of a device):
the same targets (scene: state attribute ``entity_id``; script: its
sequence resolved like Home Assistant runs it – ``entity_id: all``,
devices, areas, floors and labels through the registries) judged with the
same snapshot inputs
(areas of the profile, levels from away, kids blocked device types).
Only the static part: bedtime, releases of the family manager and stored
scene states (color lock, volume cap, safe direction) depend on the
moment or are not visible to the hub.

Reasons per target:

- ``outside_areas``: not in the areas of the profile (snapshot
  ``entities``), also scenes and scripts among the targets;
- ``not_controllable``: domain outside the control allowlist, an entity of
  the integration or nested deeper than ``run_targets.max_depth``;
- ``kids_blocked``: blocked for children (device type, area or entity);
- ``remote``: the scene can be run from away, the target not.

``targets_unknown`` (parental controls only): a scene of another
integration or a script whose targets are only known when it runs
(templates, events, unknown ids, integration actions without a target); it
runs for children only while the family manager releases it.
"""

from __future__ import annotations

import json
import logging
import time
from functools import cache
from pathlib import Path
from typing import Any

from api import authz
from models.database import Database
from namespace import HA_DOMAIN
from services import enforcement, kids_mode
from services.ha_client import is_valid_entity_id
from services.profiles import catalog as cat
from services.profiles import resolver as res
from services.profiles import store

logger = logging.getLogger("hub.services.scene_targets")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_SECONDS = 30.0
MAX_HINTS = 50
# Stored state of a lock or alarm may be the safe direction (lock, arm):
# then a view level from away is enough, which the hub cannot see.
SAFE_DOMAINS = frozenset({"lock", "alarm_control_panel"})

# script entity_id -> (time, raw configuration), None = no config
_script_cache: dict[str, tuple[float, dict | None]] = {}


def reset() -> None:
    _script_cache.clear()


@cache
def allowlist() -> dict:
    """Copy of contracts ``data/control-allowlist.json``."""
    return json.loads((DATA_DIR / "control_allowlist.json").read_text("utf-8"))


def _domains() -> frozenset[str]:
    return frozenset(d["domain"] for d in allowlist()["domains"])


def _run_domains() -> frozenset[str]:
    return frozenset(d["domain"] for d in allowlist()["run_targets"]["domains"])


def _read_only() -> frozenset[str]:
    return frozenset(allowlist()["run_targets"]["read_only_domains"])


# ── targets ─────────────────────────────────────────────────────────────────

SELECTOR_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")
STEP_KEYS = ("action", "service", "service_template")
SCRIPT_ACTIONS = frozenset({"turn_on", "turn_off", "toggle", "reload"})
# Actions without targets that do not switch anything.
QUIET_DOMAINS = frozenset(
    {"logbook", "notify", "persistent_notification", "system_log"}
)


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value or "{#" in value)


def _ids(value: Any) -> list[str] | None:
    """Ids of a selector value; None = rendered at run time or malformed."""
    if isinstance(value, str):
        items: list[Any] = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return None
    out = []
    for item in items:
        if not isinstance(item, str) or _is_template(item):
            return None
        if item.strip():
            out.append(item.strip())
    return out


class Registries:
    """States and Home Assistant registries for resolving script targets."""

    def __init__(
        self,
        states: dict[str, dict],
        entities: list[dict] | None = None,
        devices: list[dict] | None = None,
        areas: list[dict] | None = None,
        floors: list[dict] | None = None,
        labels: list[dict] | None = None,
    ) -> None:
        self.states = states
        self.entities = [e for e in entities or [] if isinstance(e, dict)]
        self.devices = [d for d in devices or [] if isinstance(d, dict)]
        self.areas = [a for a in areas or [] if isinstance(a, dict)]
        self.by_uuid = {
            e["id"]: e["entity_id"]
            for e in self.entities
            if isinstance(e.get("id"), str) and isinstance(e.get("entity_id"), str)
        }
        self.device_ids = {d.get("id") for d in self.devices}
        self.area_ids = {a.get("area_id") for a in self.areas}
        self.floor_ids = {f.get("floor_id") for f in floors or []}
        self.label_ids = {lb.get("label_id") for lb in labels or []}
        self.domains = {e.split(".", 1)[0] for e in states}

    def entity_domain(self, domain: str) -> bool:
        return domain in self.domains or domain in _domains() or domain in _read_only()


class _Walk:
    """Entities a script switches, resolved like Home Assistant runs it
    (the integration's check, contract ``docs/enforcement.md`` §2 step 4):
    ``entity_id: all`` = every entity of the action's domain, devices, areas,
    floors and labels through the registries, registry ids, old style groups,
    scripts called as actions, ``scene.apply``/``scene.create`` entities,
    device actions, entity ids in data, entities of conditions and waits.
    Templates, events, unknown ids and integration actions without a target
    make it incomplete (``targets_unknown`` with parental controls)."""

    def __init__(self, reg: Registries) -> None:
        self.reg = reg
        self.found: set[str] = set()
        self.complete = True

    def _all(self, domain: str) -> set[str]:
        states = self.reg.states
        if domain == "homeassistant":
            return {e for e in states if not e.startswith("group.")}
        if self.reg.entity_domain(domain):
            return {e for e in states if e.split(".", 1)[0] == domain}
        self.complete = False
        return set()

    def _entity_ids(self, values: list[str]) -> list[str]:
        out = []
        for value in values:
            lowered = value.lower()
            if is_valid_entity_id(lowered):
                out.append(lowered)
            elif value in self.reg.by_uuid:
                out.append(self.reg.by_uuid[value])
            else:
                self.complete = False
        return out

    def _expand_groups(self, entity_ids: list[str], seen: set[str]) -> set[str]:
        out: set[str] = set()
        for eid in entity_ids:
            members = (
                (self.reg.states.get(eid) or {}).get("attributes", {}).get("entity_id")
            )
            if eid.startswith("group.") and isinstance(members, list):
                if eid not in seen:
                    seen.add(eid)
                    out |= self._expand_groups(
                        [m.lower() for m in members if isinstance(m, str)], seen
                    )
            else:
                out.add(eid)
        return out

    def _resolve(self, selector: dict[str, list[str]], domain: str | None) -> set[str]:
        reg = self.reg
        ids = selector.get("entity_id", [])
        found: set[str] = set()
        if "all" in ids:
            found |= self._all(domain or "homeassistant")
        found |= self._expand_groups(
            self._entity_ids([v for v in ids if v not in ("all", "none")]), set()
        )
        areas = set(selector.get("area_id", []))
        devices = set(selector.get("device_id", []))
        indirect: set[str] = set()
        for floor in selector.get("floor_id", []):
            if floor not in reg.floor_ids:
                self.complete = False
            areas |= {a.get("area_id") for a in reg.areas if a.get("floor_id") == floor}
        for label in selector.get("label_id", []):
            if label not in reg.label_ids:
                self.complete = False
            indirect |= {
                e["entity_id"]
                for e in reg.entities
                if label in (e.get("labels") or []) and not e.get("hidden_by")
            }
            devices |= {
                d.get("id") for d in reg.devices if label in (d.get("labels") or [])
            }
            areas |= {
                a.get("area_id") for a in reg.areas if label in (a.get("labels") or [])
            }
        if (areas - reg.area_ids) or (devices - reg.device_ids):
            self.complete = False

        def primary(e: dict) -> bool:
            return not e.get("hidden_by") and not e.get("entity_category")

        by_area = {d.get("id") for d in reg.devices if d.get("area_id") in areas}
        for e in reg.entities:
            if not primary(e) or not isinstance(e.get("entity_id"), str):
                continue
            if (
                e.get("device_id") in devices
                or e.get("area_id") in areas
                or (e.get("device_id") in by_area and not e.get("area_id"))
            ):
                indirect.add(e["entity_id"])
        if (
            domain is not None
            and domain != "homeassistant"
            and reg.entity_domain(domain)
        ):
            indirect = {e for e in indirect if e.split(".", 1)[0] == domain}
        return found | indirect

    def _selector(self, *sources: Any) -> tuple[dict[str, list[str]], bool]:
        selector: dict[str, list[str]] = {}
        ok = True
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in SELECTOR_KEYS:
                value = source.get(key)
                if value in (None, "", []):
                    continue
                ids = _ids(value)
                if ids is None:
                    ok = False
                    continue
                if key == "entity_id":
                    ids = [i.lower() for i in ids]
                selector.setdefault(key, []).extend(ids)
        return selector, ok

    def _named(self, node: Any) -> None:
        if isinstance(node, str):
            lowered = node.strip().lower()
            if is_valid_entity_id(lowered) and lowered in self.reg.states:
                self.found.add(lowered)
        elif isinstance(node, dict):
            for key, value in node.items():
                self._named(key)
                self._named(value)
        elif isinstance(node, list):
            for item in node:
                self._named(item)

    def action(self, step: dict) -> None:
        name = step.get("action", step.get("service"))
        if (
            "service_template" in step
            or not isinstance(name, str)
            or _is_template(name)
        ):
            self.complete = False
            return
        domain, _, service = name.partition(".")
        target = step.get("target")
        if target is not None and not isinstance(target, dict):
            self.complete = False
            target = None
        sources: list[Any] = [step, target]
        for key in ("data", "data_template"):
            data = step.get(key)
            if data is None:
                continue
            if not isinstance(data, dict):
                self.complete = False
                continue
            sources.append(data)
            self._named(data)
        selector, ok = self._selector(*sources)
        if not ok:
            self.complete = False
        if domain == "script" and service not in SCRIPT_ACTIONS:
            self.found.add(f"script.{service}")
            return
        if domain == "scene" and service in ("apply", "create"):
            for source in sources[2:]:
                entities = source.get("entities")
                if entities is None:
                    continue
                if not isinstance(entities, dict) or any(
                    _is_template(k) for k in entities
                ):
                    self.complete = False
                if isinstance(entities, dict):
                    self.found |= set(
                        self._entity_ids([k for k in entities if not _is_template(k)])
                    )
        if any(selector.values()):
            self.found |= self._resolve(selector, domain)
        elif not (domain in QUIET_DOMAINS or self.reg.entity_domain(domain)):
            self.complete = False

    def walk(self, node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                self.walk(item)
            return
        if not isinstance(node, dict):
            return
        if any(key in node for key in STEP_KEYS):
            self.action(node)
            return
        if isinstance(node.get("event"), str) and not (
            "trigger" in node or "platform" in node
        ):
            self.complete = False
        if isinstance(node.get("scene"), str):
            self.found |= set(self._entity_ids([node["scene"]]))
        selector, _ok = self._selector(node)
        if selector.get("entity_id"):
            selector["entity_id"] = [
                v for v in selector["entity_id"] if v not in ("all", "none")
            ]
        domain = node.get("domain")
        if "type" in node and isinstance(domain, str) and selector.get("device_id"):
            if selector.get("entity_id"):
                selector = {"entity_id": selector["entity_id"]}
            self.found |= self._resolve(selector, domain)
        elif any(selector.values()):
            self.found |= self._resolve(selector, None)
        for key, value in node.items():
            if key not in SELECTOR_KEYS:
                self.walk(value)


def script_targets(config: dict, reg: Registries) -> tuple[list[str], bool]:
    """(entities, complete) of a raw script configuration."""
    walk = _Walk(reg)
    if "use_blueprint" in config:
        # The blueprint's sequence is not visible to the hub: its inputs
        # count (selectors resolved, strings naming existing entities).
        inputs = (config.get("use_blueprint") or {}).get("input") or {}
        walk.walk(inputs)
        _strings(inputs, set(reg.states), walk.found)
    else:
        walk.walk(config.get("sequence") or [])
    return sorted(walk.found), walk.complete


def _strings(node: Any, known: set[str], found: set[str]) -> None:
    if isinstance(node, str):
        if node in known:
            found.add(node)
    elif isinstance(node, list):
        for item in node:
            _strings(item, known, found)
    elif isinstance(node, dict):
        for value in node.values():
            _strings(value, known, found)


async def _script_config(ha_client, entity_id: str) -> dict | None:
    hit = _script_cache.get(entity_id)
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        config = await ha_client.get_script_config(entity_id)
    except Exception as exc:  # noqa: BLE001 - HA down or not found: no hint
        logger.debug("scene hints: %s config not readable: %s", entity_id, exc)
        config = None
    value = config or None
    _script_cache[entity_id] = (now, value)
    return value


async def targets_of(
    ha_client, entity_id: str, reg: Registries
) -> tuple[list[str], bool] | None:
    """(targets, complete) of a scene or script, None if unknown right now."""
    states = reg.states
    if entity_id.startswith("scene."):
        state = states.get(entity_id)
        if state is None:
            return None
        members = (state.get("attributes") or {}).get("entity_id")
        if isinstance(members, list):
            return sorted({m for m in members if isinstance(m, str)}), True
        return [], False  # scene of another integration
    if entity_id not in states:
        return None
    config = await _script_config(ha_client, entity_id)
    return script_targets(config, reg) if config else None


async def _registries(ha_client, states: dict[str, dict]) -> Registries:
    async def read(name: str) -> list[dict]:
        try:
            return await getattr(ha_client, name)() or []
        except Exception as exc:  # noqa: BLE001 - registry unreadable: ids unknown
            logger.debug("scene hints: %s failed: %s", name, exc)
            return []

    return Registries(
        states,
        entities=await read("get_entity_registry"),
        devices=await read("get_devices"),
        areas=await read("get_areas"),
        floors=await read("get_floors"),
        labels=await read("get_labels"),
    )


# ── judging ─────────────────────────────────────────────────────────────────


class _Judge:
    def __init__(
        self,
        ha_client,
        *,
        effective: dict,
        entities: dict[str, list[str]],
        reg: Registries,
    ) -> None:
        self.ha = ha_client
        self.effective = effective
        self.entities = entities
        self.reg = reg
        self.states = reg.states
        self.own = {
            e["entity_id"]
            for e in reg.entities
            if e.get("platform") == HA_DOMAIN and isinstance(e.get("entity_id"), str)
        }
        self.layers = kids_mode.layers_of(effective)
        self.remote = (effective.get("remote_access") or {}).get("value") is True
        self.catalog = cat.catalog()
        self.max_depth = int(allowlist()["run_targets"]["max_depth"])

    def _remote_level(self, entity_id: str) -> str:
        dc = enforcement._device_class(self.states, entity_id)
        return res.entity_access(
            self.effective,
            entity_id=entity_id,
            device_class=dc,
            route="external",
            quick_access=None,
            catalog=self.catalog,
        )["app"]

    def _reason(self, target: str, remote_check: bool) -> str | None:
        domain = target.split(".", 1)[0]
        if domain not in _domains() or target in self.own:
            return "not_controllable"
        if target not in self.entities:
            return "outside_areas"
        if self.layers is not None:
            k = enforcement._kids_entity(
                self.layers, target, self.entities[target], self.states.get(target)
            )
            if k["blocked"]:
                return "kids_blocked"
        if remote_check:
            level = self._remote_level(target)
            if level == "hidden" or (level == "view" and domain not in SAFE_DOMAINS):
                return "remote"
        return None

    async def walk(
        self,
        entity_id: str,
        depth: int,
        seen: set[str],
        remote_check: bool,
        out: dict[str, str],
    ) -> bool:
        """Collect refused targets into ``out``; False = targets unknown."""
        found = await targets_of(self.ha, entity_id, self.reg)
        if found is None:
            return True
        targets, complete = found
        return complete & await self._judge(targets, depth, seen, remote_check, out)

    async def _judge(
        self,
        targets: list[str],
        depth: int,
        seen: set[str],
        remote_check: bool,
        out: dict[str, str],
    ) -> bool:
        known = True
        for target in targets:
            if target in seen:
                continue
            domain = target.split(".", 1)[0]
            if domain in _read_only() or target not in self.states:
                continue
            seen.add(target)
            reason = self._reason(target, remote_check)
            if reason is None and domain in _run_domains():
                if depth >= self.max_depth:
                    reason = "not_controllable"
                else:
                    known &= await self.walk(target, depth + 1, seen, remote_check, out)
            elif reason is None:
                # A group among the targets switches its members as well.
                members = (self.states[target].get("attributes") or {}).get("entity_id")
                if isinstance(members, list):
                    known &= await self._judge(
                        sorted(m for m in members if isinstance(m, str)),
                        depth,
                        seen,
                        remote_check,
                        out,
                    )
            if reason is not None:
                out.setdefault(target, reason)
        return known


def _name(entity_id: str, states: dict[str, dict], names: dict[str, str]) -> str:
    if names.get(entity_id):
        return names[entity_id]
    attrs = (states.get(entity_id) or {}).get("attributes") or {}
    name = attrs.get("friendly_name")
    return name if isinstance(name, str) and name else entity_id


async def _placements(db: Database) -> tuple[dict[str, list[str]], dict[str, str]]:
    """entity_id -> active areas where it is placed visibly, and its hub name."""
    rows = await db.fetchall(
        "SELECT ae.area_id, ae.entity_id, ae.display_name FROM area_entities ae "
        "JOIN areas a ON a.id = ae.area_id WHERE a.is_active = 1 "
        "AND (ae.is_visible IS NULL OR ae.is_visible = 1) "
        "ORDER BY a.sort_order, a.name"
    )
    areas: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for r in rows:
        areas.setdefault(r["entity_id"], []).append(r["area_id"])
        if r.get("display_name") and r["entity_id"] not in names:
            names[r["entity_id"]] = r["display_name"]
    return areas, names


async def _run_areas(db: Database, area_ids: set[str]) -> dict[str, list[str]]:
    """Run targets of tiles -> the areas of those tiles (within ``area_ids``)."""
    if not area_ids:
        return {}
    marks = ",".join("?" for _ in area_ids)
    rows = await db.fetchall(
        "SELECT area_id, tile FROM area_entities "
        f"WHERE area_id IN ({marks}) AND tile IS NOT NULL",
        tuple(area_ids),
    )
    out: dict[str, list[str]] = {}
    for r in rows:
        tile = kids_mode._loads(r.get("tile"))
        for key in ("tap_action", "hold_action"):
            action = tile.get(key)
            if isinstance(action, dict) and action.get("action") == "run":
                target = action.get("target_entity_id")
                if isinstance(target, str) and r["area_id"] not in out.get(target, []):
                    out.setdefault(target, []).append(r["area_id"])
    return out


async def _states(ha_client) -> dict[str, dict] | None:
    if ha_client is None:
        return None
    try:
        return {s["entity_id"]: s for s in await ha_client.get_states() or []}
    except Exception as exc:  # noqa: BLE001 - HA not reachable: no hints
        logger.debug("scene hints: HA states not available: %s", exc)
        return None


async def hints(
    db: Database,
    ha_client,
    *,
    effective: dict,
    area_ids: set[str],
    entities: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Released scenes and scripts with targets that would be refused."""
    states = await _states(ha_client)
    if not states:
        return []
    run = _run_domains()
    released = sorted(e for e in entities if e.split(".", 1)[0] in run)
    if not released:
        return []
    placed, names = await _placements(db)
    run_areas = await _run_areas(db, area_ids)
    judge = _Judge(
        ha_client,
        effective=effective,
        entities=entities,
        reg=await _registries(ha_client, states),
    )
    child = judge.layers is not None
    out: list[dict[str, Any]] = []
    for entity_id in released:
        if entity_id not in states:
            continue
        remote_check = judge.remote and judge._remote_level(entity_id) == "control"
        refused: dict[str, str] = {}
        known = await judge.walk(entity_id, 1, {entity_id}, remote_check, refused)
        unknown = child and not known
        if not refused and not unknown:
            continue
        where = [a for a in placed.get(entity_id, []) if a in area_ids]
        where += [a for a in run_areas.get(entity_id, []) if a not in where]
        out.append(
            {
                "entity_id": entity_id,
                "name": _name(entity_id, states, names),
                "area_ids": where,
                "targets_unknown": unknown,
                "targets": [
                    {
                        "entity_id": target,
                        "name": _name(target, states, names),
                        "reason": reason,
                        "area_ids": placed.get(target, []),
                    }
                    for target, reason in sorted(refused.items())
                ],
            }
        )
        if len(out) >= MAX_HINTS:
            break
    return out


# ── entry points ────────────────────────────────────────────────────────────


def profile_effective(body: dict) -> dict[str, dict]:
    """Effective rules of a profile body without a device (no own area)."""
    return store.resolve_inputs(
        {
            "platform": cat.device_platform("ios"),
            "access": {"kind": "member", "expires_at": None},
            "profile": body,
            "overrides": [],
            "device": {"own_area_id": None, "has_remote_pin": False},
            "now": store.now_iso(),
        }
    )


async def for_profile(db: Database, ha_client, body: dict) -> list[dict[str, Any]]:
    """Hints of a profile body (stored profile or a draft of the editor)."""
    try:
        effective = profile_effective(body)
        scope = authz.scope_from_rule(effective["areas"]["value"])
        area_ids = await authz.scope_area_ids(db, scope)
        entities = await enforcement.operable_in(db, area_ids)
        return await hints(
            db, ha_client, effective=effective, area_ids=area_ids, entities=entities
        )
    except Exception:  # noqa: BLE001 - hints never break the page
        logger.exception("scene hints of a profile failed")
        return []


async def for_client(db: Database, ha_client, client_id: str) -> list[dict[str, Any]]:
    """Hints of a device (its own area and adjustments included)."""
    try:
        resolved = await store.resolve_client(db, client_id)
        if not resolved:
            return []
        _client, _prof, effective = resolved
        area_ids = await authz.listed_area_ids(db, client_id, ignore_access=True)
        entities = await enforcement.operable_in(db, area_ids)
        return await hints(
            db, ha_client, effective=effective, area_ids=area_ids, entities=entities
        )
    except Exception:  # noqa: BLE001 - hints never break the page
        logger.exception("scene hints of a device failed")
        return []
