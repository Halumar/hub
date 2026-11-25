"""Operations of the family manager (contract ``docs/family.md``).

Entry points:

* :func:`handle_manager_message` – ``family_*`` messages of a family manager
  device (hub WebSocket at home, ``POST /api/integration/family`` away);
* :func:`handle_child_message` – ``kids_request`` / ``kids_request_cancel`` of
  a child device (hub WebSocket only: child devices have no HA key);
* :func:`answer_request` – the one way a request gets answered (app, admin UI,
  HA notification button): the first answer wins;
* views for ``client_config`` and the admin API.

Every message is checked here, fail closed (``docs/family.md`` "Checks of the
hub"): paired active member device, no parental controls, effective rule
``family_manager`` now, target a child device in the scope, rule manageable
and not fixed, value valid, areas within the own areas, release allowed
(:func:`services.family.check.family_check`), limits. Changes are device
adjustments of the child device (``origin = family_manager``); the last writer
wins and every change is audited with the actor.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from models.database import Database
from services.family import check, data, events
from services.family import store as fstore
from services.profiles import catalog as cat
from services.profiles import store as pstore
from services.profiles.catalog import ProfileError

logger = logging.getLogger("hub.family")

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
REQUEST_ID_MAX = 64
# family_get_activity cursor: a log row id (fits SQLite's 64-bit integer).
CURSOR_RE = re.compile(r"^[0-9]{1,18}$")
ADMIN_NAME = "Admin"

READ_TYPES = ("family_get_children", "family_get_requests", "family_get_activity")
WRITE_TYPES = (
    "family_rule_set",
    "family_rule_reset",
    "family_release",
    "family_release_end",
    "family_notify_set",
)
ANSWER_TYPES = ("family_answer",)
BABYSITTER_TYPES = ("family_get_requests", "family_answer")
MANAGER_TYPES = READ_TYPES + WRITE_TYPES + ANSWER_TYPES
CHILD_TYPES = ("kids_request", "kids_request_cancel")

MESSAGES = {
    "invalid_request": "The request is not valid.",
    "not_permitted": "This device is not a family manager.",
    "not_your_child": "This device is not one of your children.",
    "rule_not_manageable": "Family managers cannot change this rule.",
    "rule_fixed": "The child's profile fixes this rule.",
    "boundary": "A boundary decides this rule on the child's device.",
    "value_invalid": "The value does not fit this rule.",
    "areas_not_allowed": "Only rooms you see yourself.",
    "release_not_allowed": "This release is not possible.",
    "request_not_found": "Unknown request.",
    "already_answered": "This request was already answered.",
    "request_limit": "Too many requests.",
    "cooldown": "Please wait a little before asking again.",
    "requests_disabled": "Requests are switched off.",
    "rate_limited": "Too many family actions, try again later.",
    "hub_unreachable": "The hub is not reachable.",
}


class FamilyError(Exception):
    def __init__(
        self,
        reason: str,
        *,
        retry_after: int | None = None,
        request: dict | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.request = request

    def message(self, request_id: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "family_error",
            "reason": self.reason,
            "message": MESSAGES.get(self.reason, self.reason),
        }
        if request_id:
            out["request_id"] = request_id
        if self.retry_after is not None:
            out["retry_after"] = max(0, int(self.retry_after))
        if self.request is not None:
            out["request"] = self.request
        return out


# ── rate limits (in memory, like the kids counters) ─────────────────────────


class Window:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}

    def hit(self, key: tuple[str, str], limit: int, seconds: float) -> int | None:
        """Count one hit; seconds to wait when over the limit (nothing counted)."""
        now = time.monotonic()
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= seconds:
            q.popleft()
        if len(q) >= limit:
            return int(seconds - (now - q[0])) + 1
        q.append(now)
        return None

    def reset(self) -> None:
        self._hits.clear()


limiter = Window()


def _rate(client_id: str, msg_type: str) -> None:
    lim = data.limits()
    if msg_type in READ_TYPES:
        args = ("read", lim["family_reads_per_manager_per_minute"], 60.0)
    elif msg_type in ANSWER_TYPES:
        args = ("answer", lim["answers_per_manager_per_hour"], 3600.0)
    else:
        args = (
            "write",
            lim["family_writes_per_manager"],
            float(lim["family_writes_window_seconds"]),
        )
    wait = limiter.hit((client_id, args[0]), args[1], args[2])
    if wait is not None:
        raise FamilyError("rate_limited", retry_after=wait)


# ── who is who ──────────────────────────────────────────────────────────────


@dataclass
class Manager:
    client_id: str
    name: str
    owner_label: str | None
    platform: str | None
    scope: dict[str, Any]
    visible_area_ids: set[str] | None
    notify_targets: list[str]
    notify: dict[str, bool]
    remote: bool
    admin: bool = False
    children: list[dict] = field(default_factory=list)
    # A babysitter device (kind babysitter, rule babysitter_requests)
    # only answers requests while it has access; access_until = end of its
    # running access window (None = no known end).
    babysitter: bool = False
    access_until: datetime | None = None
    route: str | None = None

    def check_input(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "client_id": self.client_id,
            "access": "guest" if self.babysitter else "member",
            "child_protection": False,
            "family_manager": not self.babysitter,
            "scope": self.scope,
            "visible_area_ids": (
                None if self.visible_area_ids is None else sorted(self.visible_area_ids)
            ),
        }
        if self.babysitter:
            out.update(
                babysitter=True,
                access_open=True,
                at_home=self.route in (None, "home"),
                access_until=fstore.iso(self.access_until),
            )
        return out

    def answer_presets(self) -> list[str]:
        """Durations a babysitter may give with a Yes."""
        presets = list(data.babysitter()["presets"])
        if self.access_until is None:
            presets = [p for p in presets if p != "access_end"]
        return presets

    def actor(self, route: str | None) -> dict[str, Any]:
        if self.admin:
            return {"kind": "admin", "client_id": None, "name": self.name, "via": route}
        out: dict[str, Any] = {
            "kind": "babysitter" if self.babysitter else "family_manager",
            "client_id": self.client_id,
            "name": self.owner_label or self.name,
            "via": route,
        }
        if self.owner_label:
            out["owner_label"] = self.owner_label
        return out


def admin_manager(name: str | None = None) -> Manager:
    """The admin answers and releases without the limit of family managers to rules that are not **Fixed**."""
    return Manager(
        client_id="admin",
        name=name or ADMIN_NAME,
        owner_label=None,
        platform=None,
        scope={"all": True, "client_ids": []},
        visible_area_ids=None,
        notify_targets=[],
        notify={},
        remote=False,
        admin=True,
    )


def notify_prefs(stored: dict[str, Any]) -> dict[str, bool]:
    out = {}
    for kind, d in data.notify_defaults().items():
        value = d["enabled"]
        if d["can_disable"] and isinstance(stored.get(kind), bool):
            value = stored[kind]
        out[kind] = value
    return out


def _usable(row: dict) -> bool:
    return bool(row.get("is_active")) and not row.get("pending_deletion_at")


async def babysitter_from(row: dict, prof: dict, rules: dict) -> Manager | None:
    """A babysitter device that may answer requests right now."""
    from services import access

    if (
        not _usable(row)
        or (prof.get("kind") or "person") != "babysitter"
        or prof["child_protection"]
        or rules.get("babysitter_requests", {}).get("value") is not True
    ):
        return None
    state = access.state_of(row, prof, rules)
    if not state["open"]:
        return None
    return Manager(
        client_id=row["id"],
        name=row["name"],
        owner_label=row.get("owner_label"),
        platform=row.get("platform"),
        scope={"all": True, "client_ids": []},
        visible_area_ids=set(),
        notify_targets=[],
        notify={},
        remote=False,
        babysitter=True,
        access_until=fstore.parse(state["until"]) if state["until"] else None,
    )


async def manager_from(
    db: Database,
    row: dict,
    prof: dict,
    rules: dict,
    *,
    include_babysitter: bool = False,
) -> Manager | None:
    from api import authz
    from services import access

    if include_babysitter and (prof.get("kind") or "person") == "babysitter":
        return await babysitter_from(row, prof, rules)
    if (
        not _usable(row)
        or row.get("access") != "member"
        or prof["child_protection"]
        or rules.get("family_manager", {}).get("value") is not True
        or not access.is_open(row, prof, rules)
    ):
        return None
    sc = await fstore.scope_row(db, row["id"])
    area_scope = await authz.client_area_scope(db, row["id"])
    visible = None if area_scope.all else await authz.listed_area_ids(db, row["id"])
    full = await db.fetchone(
        "SELECT owner_label FROM paired_clients WHERE id = ?", (row["id"],)
    )
    return Manager(
        client_id=row["id"],
        name=row["name"],
        owner_label=(full or {}).get("owner_label"),
        platform=row.get("platform"),
        scope=sc["scope"],
        visible_area_ids=visible,
        notify_targets=sc["notify_targets"],
        notify=notify_prefs(sc["notify"]),
        remote=rules.get("remote_access", {}).get("value") is True,
    )


async def manager_of(
    db: Database, client_id: str, *, include_babysitter: bool = False
) -> Manager:
    resolved = await pstore.resolve_client(db, client_id)
    if not resolved:
        raise FamilyError("not_permitted")
    manager = await manager_from(db, *resolved, include_babysitter=include_babysitter)
    if manager is None:
        raise FamilyError("not_permitted")
    return manager


async def all_managers(
    db: Database, *, include_babysitters: bool = False
) -> list[Manager]:
    out = []
    for item in (await pstore.resolve_all(db)).values():
        m = await manager_from(
            db,
            item["client"],
            item["profile"],
            item["rules"],
            include_babysitter=include_babysitters,
        )
        if m:
            out.append(m)
    return out


async def child_devices(db: Database) -> list[dict]:
    """Paired member devices with parental controls (the possible children)."""
    return await db.fetchall(
        "SELECT c.id, c.name, c.owner_label, c.platform, c.last_seen, c.profile_id, "
        "p.name AS profile_name FROM paired_clients c JOIN profiles p "
        "ON p.id = c.profile_id WHERE p.child_protection = 1 AND c.access = 'member' "
        "AND c.is_active = 1 AND c.pending_deletion_at IS NULL "
        "ORDER BY COALESCE(c.owner_label, c.name) COLLATE NOCASE, c.name COLLATE NOCASE"
    )


def in_scope(manager: Manager, child_id: str) -> bool:
    if manager.client_id == child_id:
        return False
    return manager.scope["all"] or child_id in manager.scope["client_ids"]


async def children_of(db: Database, manager: Manager) -> list[dict]:
    return [c for c in await child_devices(db) if in_scope(manager, c["id"])]


async def managers_of_child(
    db: Database, child_id: str, *, include_babysitters: bool = False
) -> list[Manager]:
    return [
        m
        for m in await all_managers(db, include_babysitters=include_babysitters)
        if in_scope(m, child_id)
    ]


def child_display(row: dict) -> str:
    return row.get("owner_label") or row.get("name") or ""


# ── child context ───────────────────────────────────────────────────────────


@dataclass
class Child:
    row: dict
    prof: dict
    rules: dict[str, dict]
    fixed: list[str]
    tz: str | None
    quiet_now: bool

    @property
    def client_id(self) -> str:
        return self.row["id"]

    def blocked_types(self) -> list[str]:
        return list(self.rules.get("kids_blocked_types", {}).get("value") or [])

    def exception_blocked(self) -> list[str]:
        out = []
        for exc in self.rules.get("kids_exceptions", {}).get("value") or []:
            target = exc.get("target") or {}
            if target.get("entity_id") and (exc.get("rules") or {}).get("blocked"):
                out.append(target["entity_id"])
        return out

    def check_input(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "child_protection": True,
            "fixed": self.fixed,
            "exception_blocked_entities": self.exception_blocked(),
            "blocked_types": self.blocked_types(),
            "quiet_now": self.quiet_now,
        }


async def child_of(db: Database, ha_client, child_id: str) -> Child | None:
    """A paired member device with parental controls, else None."""
    from services import kids_mode, kids_rules

    if not isinstance(child_id, str) or not UUID_RE.match(child_id):
        return None
    resolved = await pstore.resolve_client(db, child_id)
    if not resolved:
        return None
    row, prof, rules = resolved
    if (
        not _usable(row)
        or row.get("access") != "member"
        or not prof["child_protection"]
    ):
        return None
    body = pstore.body_of(prof)
    tz = await kids_mode.hub_time_zone(ha_client)
    windows = rules.get("kids_quiet_hours", {}).get("value") or []
    quiet = kids_rules.quiet_until(windows, fstore.utcnow(), tz) is not None
    return Child(row, prof, rules, body["fixed"], tz, quiet)


def child_holds_key(child: Child) -> bool:
    """The child device holds its own HA key (``resolver.holds_key``: Checked,
    or Hub only with remote access) and may reach the hub through the
    integration (``<ns>/family``). A device on the hub path without a key
    speaks only to the hub."""
    from services.profiles import resolver as res

    return res.holds_key(child.rules)


async def entity_state(ha_client, entity_id: str) -> dict | None:
    if ha_client is None:
        return None
    try:
        return await ha_client.get_state(entity_id)
    except Exception:  # noqa: BLE001 - names and device classes are optional
        return None


async def entity_name(ha_client, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    state = await entity_state(ha_client, entity_id)
    name = ((state or {}).get("attributes") or {}).get("friendly_name")
    return name if isinstance(name, str) and name else None


async def release_device_class(ha_client, child: Child, entity_id: str) -> str | None:
    """``device_class`` for the family check of a release. With the state
    unknown (HA restarting, timeout) a class the child's blocked types name
    for this domain, so a blocked type is never released blind."""
    state = await entity_state(ha_client, entity_id)
    if state is None:
        prefix = entity_id.split(".", 1)[0] + ":"
        for blocked in child.blocked_types():
            if isinstance(blocked, str) and blocked.startswith(prefix):
                return blocked[len(prefix) :]
        return None
    dc = (state.get("attributes") or {}).get("device_class")
    return dc if isinstance(dc, str) and dc else None


# ── releases ────────────────────────────────────────────────────────────────


async def release_until(
    db: Database,
    ha_client,
    child_id: str,
    preset: str,
    manager: Manager | None = None,
) -> datetime:
    from services import kids_mode

    now = fstore.utcnow()
    p = data.presets().get(preset)
    if p is None:
        raise FamilyError("invalid_request")
    sitter = manager is not None and manager.babysitter
    if preset == "access_end":
        # "until the end of babysitting" = end of the access window.
        if not sitter or manager.access_until is None:
            raise FamilyError("preset_not_allowed")
        until = manager.access_until
    elif p["minutes"] is None:
        until = await kids_mode.bedtime_release_end(db, ha_client, child_id, now)
    else:
        until = now + timedelta(minutes=int(p["minutes"]))
    cap = now + timedelta(minutes=data.limits()["release_max_minutes"])
    if sitter and manager.access_until is not None:
        cap = min(cap, manager.access_until)
    return min(until, cap)


async def active_releases(
    db: Database, child_id: str, now: datetime | None = None
) -> dict[str, Any]:
    """{"bedtime": until|None, "pause": until|None, "entities": {id: until}}."""
    out: dict[str, Any] = {"bedtime": None, "pause": None, "entities": {}}
    for r in await fstore.releases_of(db, child_id, now):
        until = fstore.parse(r["until"])
        if r["kind"] == "entity":
            out["entities"][r["entity_id"]] = until
        else:
            out[r["kind"]] = until
    return out


async def releases_for_action(db: Database, child_id: str, entity_id: str):
    """kids_rules.Releases of one action of a child device."""
    from services import kids_rules

    active = await active_releases(db, child_id)
    entity = entity_id in active["entities"]
    return kids_rules.Releases(
        quiet=bool(active["bedtime"]) or entity,
        rate=bool(active["pause"]),
        types=entity,
    )


async def push_rules(db: Database, ha_client) -> None:
    """Hand the changed rule snapshots to the integration right away: the
    hub's own kids path calls ``<ns>.control`` next, which checks releases in
    the snapshot (the debounced push would come too late)."""
    from services import enforcement

    try:
        await enforcement.push(db, ha_client)
    except Exception as e:  # noqa: BLE001 - the debounced/periodic push retries
        logger.debug("rule snapshot push after a release failed: %s", e)


async def start_release(
    db: Database,
    ha_client,
    manager: Manager,
    child: Child,
    kind: str,
    entity_id: str | None,
    preset: str,
    route: str | None,
    request_id: str | None = None,
) -> datetime:
    until = await release_until(db, ha_client, child.client_id, preset, manager)
    by = manager.actor(route)
    async with db.transaction():
        await fstore.put_release(
            db, child.client_id, kind, entity_id, until, by, request_id
        )
    await release_started(db, ha_client, child, kind)
    if request_id is None:  # a Yes to a request is logged as the answer
        await events.log(
            db,
            events.RELEASE,
            client_id=child.client_id,
            category="change",
            event_kind=events.FAMILY_CHANGE,
            entity_id=entity_id,
            action=kind,
            until=fstore.iso(until),
            by=by,
        )
        await events.emit(
            db,
            events.FAMILY_CHANGE,
            client_id=child.client_id,
            entity_id=entity_id,
            action=f"release_{kind}",
            until=fstore.iso(until),
            extra={
                "rule": RELEASE_RULE_OF.get(kind),
                "by": by,
                "manager_id": manager.client_id,
            },
        )
    return until


async def release_started(db: Database, ha_client, child: Child, kind: str) -> None:
    """Effects of a release stored by :func:`start_release` or a Yes."""
    from services import kids_rules

    if kind == "pause":
        kids_rules.counters.clear_client(child.client_id)
    await push_rules(db, ha_client)


RELEASE_RULE_OF = {
    "bedtime": "kids_quiet_hours",
    "pause": "kids_rate_limit",
    "entity": "kids_blocked_types",
}


# ── views ───────────────────────────────────────────────────────────────────


def _since_days(days: int) -> datetime:
    return fstore.utcnow() - timedelta(days=days)


async def request_view(
    db: Database,
    ha_client,
    row: dict,
    *,
    viewer: Manager | None = None,
    child: Child | None = None,
    for_child: bool = False,
) -> dict[str, Any]:
    """``common/family-request.json``. ``for_child``: the copy for the child
    device itself (``kids_request_state``, ``client_config.kids.requests``)."""
    child_row = await db.fetchone(
        "SELECT name, owner_label FROM paired_clients WHERE id = ?", (row["client_id"],)
    )
    out: dict[str, Any] = {
        "request_id": row["id"],
        "client_id": row["client_id"],
        "child_name": child_display(child_row or {})[:100],
        "category": row["category"],
        "note": row.get("note"),
        "status": row["status"],
        "created_at": fstore.iso(fstore.parse(row["created_at"])),
        "expires_at": fstore.iso(fstore.parse(row["expires_at"])),
        "answer": fstore.answer_of(row),
    }
    if for_child and out["answer"]:
        out["answer"] = {
            **out["answer"],
            "by": fstore.child_actor(out["answer"].get("by")),
        }
    if (child_row or {}).get("owner_label"):
        out["owner_label"] = child_row["owner_label"]
    if row.get("entity_id"):
        out["entity_id"] = row["entity_id"]
        name = await entity_name(ha_client, row["entity_id"])
        if name:
            out["entity_name"] = name[:255]
    if viewer is not None and row["status"] == "open":
        out["can_approve"] = await can_approve(db, ha_client, viewer, row, child)
        if viewer.babysitter:
            out["answer_presets"] = viewer.answer_presets()
    return out


async def can_approve(
    db: Database, ha_client, manager: Manager, row: dict, child: Child | None = None
) -> bool:
    child = child or await child_of(db, ha_client, row["client_id"])
    if child is None:
        return False
    op: dict[str, Any] = {"kind": "answer_yes", "category": row["category"]}
    if manager.babysitter:
        presets = manager.answer_presets()
        if not presets:
            return False
        op["preset"] = presets[0]
    if row.get("entity_id"):
        op["entity_id"] = row["entity_id"]
        op["device_class"] = await release_device_class(
            ha_client, child, row["entity_id"]
        )
    return _yes_allowed(manager, child, op)[0]


def _yes_allowed(manager: Manager, child: Child, op: dict) -> tuple[bool, str | None]:
    if manager.admin:
        # The admin is not limited by **Fixed** rules; locks, alarm panels and the
        # admin's own exceptions still never get a release.
        relaxed = child.check_input()
        relaxed["fixed"] = []
        relaxed["quiet_now"] = False
        m = manager.check_input()
        m["scope"] = {"all": True, "client_ids": []}
        return check.family_check(m, relaxed, op)
    return check.family_check(manager.check_input(), child.check_input(), op)


async def child_view(
    db: Database, ha_client, manager: Manager, child: Child, connected: set[str]
) -> dict[str, Any]:
    from services import kids_rules
    from services.profiles import resolver as res

    body = pstore.body_of(child.prof)
    overrides = {
        o["rule_key"]: o for o in await pstore.overrides_of(db, child.client_id)
    }
    rules: dict[str, dict] = {}
    for key in data.manageable_rules():
        e = child.rules.get(key)
        if e is None:
            continue
        fixed = key in body["fixed"]
        manageable, why = True, None
        if fixed:
            manageable, why = False, "rule_fixed"
        elif e["source"] in ("boundary", "not_applicable"):
            manageable, why = False, "boundary"
        base = body["rules"].get(key, (cat.rule(key) or {}).get("default"))
        item: dict[str, Any] = {
            "value": e["value"],
            "profile_value": res._resolve_own_area(
                key, base, child.row.get("own_area_id")
            ),
            "source": e["source"],
            "fixed": fixed,
            "manageable": manageable,
            "not_manageable_reason": why,
            "expires_at": e.get("expires_at"),
            "set_by": None,
            "set_at": None,
        }
        if e.get("expires_at"):
            item["then"] = e.get("then")
        o = overrides.get(key)
        if o and e["source"] == "device":
            item["set_by"] = (o.get("created_by") or "")[:100] or None
            item["set_at"] = fstore.iso(fstore.parse(o.get("created_at")))
        rules[key] = item
    areas = await db.fetchall(
        "SELECT id, name FROM areas WHERE is_active = 1 ORDER BY sort_order, name"
    )
    area_choices = [
        {"area_id": a["id"], "name": a["name"]}
        for a in areas
        if manager.visible_area_ids is None or a["id"] in manager.visible_area_ids
    ]
    windows = child.rules.get("kids_quiet_hours", {}).get("value") or []
    now = fstore.utcnow()
    quiet_end = kids_rules.quiet_until(windows, now, child.tz)
    next_bed = fstore.iso(kids_rules.next_quiet_start(windows, now, child.tz))
    open_req = await fstore.open_request_of(db, child.client_id)
    today = await today_counts(db, child.client_id, child.tz)
    view: dict[str, Any] = {
        "client_id": child.client_id,
        "name": child.row["name"],
        "platform": child.row.get("platform") or "ios",
        "online": child.client_id in connected,
        "last_seen": None,
        "profile": {"id": child.prof["id"], "name": child.prof["name"]},
        "time_zone": child.tz,
        "rules": rules,
        "area_choices": area_choices,
        "releases": [
            fstore.release_view(r)
            for r in await fstore.releases_of(db, child.client_id)
        ],
        "status": {
            "bedtime_now": quiet_end is not None,
            "bedtime_until": fstore.iso(quiet_end),
            "next_bedtime": next_bed,
            "paused_entities": len(
                kids_rules.counters.cooldowns_of(child.client_id, now)
            ),
            "paused_all_until": fstore.iso(
                kids_rules.counters.paused_all(child.client_id, now)
            ),
            "open_request": open_req["id"] if open_req else None,
        },
        "today": {k: today[k] for k in ("actions", "blocked", "requests")},
    }
    full = await db.fetchone(
        "SELECT owner_label, last_seen FROM paired_clients WHERE id = ?",
        (child.client_id,),
    )
    if (full or {}).get("owner_label"):
        view["owner_label"] = full["owner_label"]
    if (full or {}).get("last_seen"):
        view["last_seen"] = fstore.iso(fstore.parse(full["last_seen"]))
    return view


# Log kind (event_log) -> family activity kind ($defs/family_activity_kind).
ACTIVITY_KIND = {
    "kids_control": "control",
    "control": "control",
    "denied": "blocked",
    "kids_bedtime_attempt": "blocked",
    "kids_pause": "pause",
    "kids_paused_all": "pause",
    "kids_release_requested": "request",
    "kids_request_answered": "answer",
    "kids_request_expired": "answer",
    "kids_request_cancelled": "answer",
    "family_release": "release",
    "family_release_end": "release",
    "kids_release": "release",
    "kids_pause_lifted": "release",
    "override_set": "change",
    "override_removed": "change",
    "override_expired": "change",
}
# Log reason (contract control_denial_reason) -> $defs/rule_blocked_reason.
BLOCKED_REASON = {
    "blocked_kids": "blocked",
    "blocked_kids_bedtime": "quiet_hours",
    "blocked_kids_cooldown": "cooldown",
    "blocked_kids_paused_all": "paused_all",
    "blocked_kids_color": "color_locked",
    "blocked_kids_volume": "volume_limit",
}


def _log_kinds(activity_kinds: list[str] | None) -> list[str]:
    if not activity_kinds:
        return list(ACTIVITY_KIND)
    return [k for k, v in ACTIVITY_KIND.items() if v in activity_kinds]


async def _log_rows(
    db: Database,
    client_ids: list[str],
    *,
    since: datetime,
    kinds: list[str],
    before_id: int | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Entries of the log tab for the children (read view)."""
    from services import event_log

    if not client_ids or not kinds:
        return []
    settings = await event_log.get_settings(db)
    since = max(since, event_log.parse_ts(event_log.retention_cutoff(settings)))
    marks = ",".join("?" for _ in client_ids)
    kmarks = ",".join("?" for _ in kinds)
    sql = (
        f"SELECT * FROM event_log WHERE client_id IN ({marks}) AND kind IN ({kmarks}) "  # noqa: S608 - placeholders only
        "AND ts >= ?"
    )
    params: list[Any] = [*client_ids, *kinds, event_log.iso(since)]
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    sql += " ORDER BY id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return await db.fetchall(sql, tuple(params))


async def today_counts(db: Database, child_id: str, tz: str | None) -> dict[str, int]:
    from services import kids_rules

    local = fstore.utcnow().astimezone(kids_rules.zone(tz))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await _log_rows(
        db,
        [child_id],
        since=start.astimezone(fstore.utcnow().tzinfo),
        kinds=[
            "kids_control",
            "control",
            "denied",
            "kids_bedtime_attempt",
            "kids_release_requested",
        ],
    )
    out = {"actions": 0, "blocked": 0, "requests": 0, "bedtime_attempts": 0}
    for r in rows:
        kind = ACTIVITY_KIND.get(r["kind"])
        if kind == "control":
            out["actions"] += 1
        elif kind == "blocked":
            out["blocked"] += 1
            if (
                r["kind"] == "kids_bedtime_attempt"
                or r.get("reason") == "blocked_kids_bedtime"
            ):
                out["bedtime_attempts"] += 1
        elif kind == "request":
            out["requests"] += 1
    return out


# ── pushes ──────────────────────────────────────────────────────────────────


async def push_to_managers(db: Database, managers: list[Manager], message_for) -> None:
    """``message_for(manager)`` to every hub connection of the managers; to
    the ones not connected but with remote access through the integration
    (``family_relay``)."""
    from api import ws as api_ws

    connected = api_ws.get_connected_client_ids()
    relay: dict[str, tuple[dict, list[str]]] = {}
    for m in managers:
        msg = await message_for(m)
        if msg is None:
            continue
        if m.client_id in connected:
            try:
                await api_ws.send_to_client(m.client_id, msg)
            except Exception as e:  # noqa: BLE001 - best effort
                logger.debug("family push to %s failed: %s", m.client_id, e)
        elif m.remote:
            key = fstore.dumps(msg)
            relay.setdefault(key, (msg, []))[1].append(m.client_id)
    if relay and api_ws.get_integration_push_connected():
        for msg, ids in relay.values():
            try:
                await api_ws.send_to_integration(
                    {"type": "family_relay", "client_ids": ids[:20], "message": msg}
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("family_relay failed: %s", e)


async def admin_changed(client_ids: list[str], request_id: str | None = None) -> None:
    from api import ws as api_ws
    from api.ws.broadcast import EventAudience

    try:
        await api_ws.broadcast(
            {
                "type": "family_changed",
                "data": {
                    "client_ids": sorted(set(client_ids)),
                    "request_id": request_id,
                },
            },
            audience=EventAudience.ADMIN,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("family_changed broadcast failed: %s", e)


async def config_changed(client_id: str) -> None:
    from api import ws as api_ws

    try:
        await api_ws.send_to_client(
            client_id, {"type": "client_config_updated", "data": {}}
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("client_config_updated to %s failed: %s", client_id, e)


async def child_updated(db: Database, ha_client, child_id: str) -> None:
    """``family_child_updated`` to the child's managers, ``family_changed`` to admins."""
    from api import ws as api_ws

    child = await child_of(db, ha_client, child_id)
    managers = await managers_of_child(db, child_id)
    connected = api_ws.get_connected_client_ids()

    async def message_for(m: Manager):
        if child is None:
            return None
        return {
            "type": "family_child_updated",
            "child": await child_view(db, ha_client, m, child, connected),
        }

    await push_to_managers(db, managers, message_for)
    await admin_changed([child_id])


async def request_changed(db: Database, ha_client, row: dict) -> None:
    """A request was created or closed: managers, child, admins, notifications."""
    from api import ws as api_ws

    child = await child_of(db, ha_client, row["client_id"])
    managers = await managers_of_child(db, row["client_id"], include_babysitters=True)

    async def message_for(m: Manager):
        return {
            "type": "family_request",
            "request": await request_view(db, ha_client, row, viewer=m, child=child),
        }

    await push_to_managers(db, managers, message_for)
    state = {
        "type": "kids_request_state",
        "request": await request_view(db, ha_client, row, for_child=True),
        "next_allowed_at": await next_allowed_at(db, row["client_id"]),
    }
    try:
        await api_ws.send_to_client(row["client_id"], state)
        if (
            row["client_id"] not in api_ws.get_connected_client_ids()
            and child is not None
            and child_holds_key(child)
            and api_ws.get_integration_push_connected()
        ):
            # The child is away; the integration hands it to its
            # <ns>/family/subscribe.
            await api_ws.send_to_integration(
                {
                    "type": "family_relay",
                    "client_ids": [row["client_id"]],
                    "message": state,
                }
            )
    except Exception as e:  # noqa: BLE001
        logger.debug("kids_request_state failed: %s", e)
    await config_changed(row["client_id"])
    for m in managers:
        await config_changed(m.client_id)
    await admin_changed([row["client_id"]], row["id"])


# ── child requests ──────────────────────────────────────────────────────────


async def next_allowed_at(db: Database, child_id: str) -> str | None:
    """When the child may ask again: hourly limit or the pause after a No."""
    settings = await fstore.get_settings(db)
    now = fstore.utcnow()
    times = []
    recent = await fstore.requests_since(db, child_id, now - timedelta(hours=1))
    if len(recent) >= settings["child_requests_per_hour"]:
        oldest = sorted(fstore.parse(r["created_at"]) for r in recent)[
            len(recent) - settings["child_requests_per_hour"]
        ]
        times.append(oldest + timedelta(hours=1))
    cooldown = settings["decline_cooldown_minutes"]
    if cooldown:
        last_no = await db.fetchone(
            "SELECT answered_at FROM kids_requests WHERE client_id = ? AND "
            "status = 'declined' ORDER BY answered_at DESC LIMIT 1",
            (child_id,),
        )
        if last_no and last_no.get("answered_at"):
            end = fstore.parse(last_no["answered_at"]) + timedelta(minutes=cooldown)
            if end > now:
                times.append(end)
    return fstore.iso(max(times)) if times else None


async def _check_request_limits(
    db: Database, child_id: str, category: str, settings: dict
) -> None:
    now = fstore.utcnow()
    if await fstore.open_request_of(db, child_id):
        raise FamilyError("request_limit")
    recent = await fstore.requests_since(db, child_id, now - timedelta(hours=1))
    if len(recent) >= settings["child_requests_per_hour"]:
        oldest = sorted(fstore.parse(r["created_at"]) for r in recent)[
            len(recent) - settings["child_requests_per_hour"]
        ]
        raise FamilyError(
            "request_limit",
            retry_after=int((oldest + timedelta(hours=1) - now).total_seconds()) + 1,
        )
    cooldown = settings["decline_cooldown_minutes"]
    if cooldown:
        last_no = await db.fetchone(
            "SELECT answered_at FROM kids_requests WHERE client_id = ? AND category = ? "
            "AND status = 'declined' ORDER BY answered_at DESC LIMIT 1",
            (child_id, category),
        )
        if last_no and last_no.get("answered_at"):
            end = fstore.parse(last_no["answered_at"]) + timedelta(minutes=cooldown)
            if end > now:
                raise FamilyError(
                    "cooldown", retry_after=int((end - now).total_seconds()) + 1
                )


async def allowed_categories(
    db: Database, child_id: str, settings: dict | None = None
) -> list[str]:
    """Kinds a child may ask for: its rule ``kids_requests`` (optional per
    profile/device) within the house settings."""
    settings = settings or await fstore.get_settings(db)
    if not settings["requests_enabled"]:
        return []
    rule = await pstore.effective_rule(db, child_id, "kids_requests")
    if not isinstance(rule, dict) or not rule.get("enabled"):
        return []
    wanted = set(rule.get("categories") or []) & set(settings["categories"])
    return [c for c in data.categories() if c in wanted]


async def active_babysitters(db: Database) -> list[Manager]:
    """Babysitter devices that get the children's requests right now:
    rule babysitter_requests, access open, requests on in the house settings,
    connected to the hub (= at home) – like ``client_config.babysitter.active``."""
    from api import ws as api_ws

    settings = await fstore.get_settings(db)
    if not settings.get("requests_enabled", True):
        return []
    connected = api_ws.get_connected_client_ids()
    out = []
    for item in (await pstore.resolve_all(db)).values():
        if item["client"]["id"] not in connected:
            continue
        m = await babysitter_from(item["client"], item["profile"], item["rules"])
        if m:
            out.append(m)
    return out


RECIPIENTS_MAX = 10
RECIPIENT_NAME_MAX = 50


def recipients_view(managers: list[Manager], sitters: list[Manager]) -> list[dict]:
    """``kids.requests.recipients``: family managers, then active babysitters,
    each sorted by name, a name once per role."""
    out: list[dict] = []
    for role, group in (("family_manager", managers), ("babysitter", sitters)):
        names = {(m.owner_label or m.name or "")[:RECIPIENT_NAME_MAX] for m in group}
        for name in sorted((n for n in names if n), key=str.casefold):
            out.append({"name": name, "role": role})
    return out[:RECIPIENTS_MAX]


async def kids_requests_view(db: Database, ha_client, child_id: str) -> dict[str, Any]:
    """``client_config.kids.requests`` of a child device."""
    settings = await fstore.get_settings(db)
    managers = await managers_of_child(db, child_id)
    sitters = [m for m in await active_babysitters(db) if in_scope(m, child_id)]
    open_row = await fstore.open_request_of(db, child_id)
    categories = await allowed_categories(db, child_id, settings)
    return {
        "enabled": bool(settings["requests_enabled"] and managers and categories),
        "categories": categories,
        "open": await request_view(db, ha_client, open_row, for_child=True)
        if open_row
        else None,
        "next_allowed_at": await next_allowed_at(db, child_id),
        "recipients": recipients_view(managers, sitters),
    }


# Active babysitters (id, name) last told to the children.
_sitters_seen: frozenset[tuple[str, str]] = frozenset()


async def babysitters_changed(db: Database) -> None:
    """Tell the connected child devices when the active babysitters change
    (access opens or closes, a babysitter comes home or leaves, a rename):
    their ``kids.requests.recipients`` follows with ``client_config_updated``."""
    global _sitters_seen
    from api import ws as api_ws

    try:
        now = frozenset(
            (m.client_id, m.owner_label or m.name) for m in await active_babysitters(db)
        )
    except Exception as e:  # noqa: BLE001 - best effort, the planner retries
        logger.debug("babysitter check failed: %s", e)
        return
    if now == _sitters_seen:
        return
    _sitters_seen = now
    connected = api_ws.get_connected_client_ids()
    for child in await child_devices(db):
        if child["id"] in connected:
            await config_changed(child["id"])


def reset_babysitters_seen() -> None:
    global _sitters_seen
    _sitters_seen = frozenset()


async def create_request(
    db: Database, ha_client, child_id: str, message: dict[str, Any]
) -> dict[str, Any]:
    from services import hub_control

    category = message.get("category")
    entity_id = message.get("entity_id")
    note = message.get("note")
    if category not in data.categories():
        raise FamilyError("invalid_request")
    if note is not None:
        if (
            not isinstance(note, str)
            or len(note) > data.limits()["request_note_max_length"]
        ):
            raise FamilyError("invalid_request")
        note = " ".join(note.split()) or None
    if category == "unlock_device":
        if not isinstance(entity_id, str) or not ENTITY_RE.match(entity_id):
            raise FamilyError("invalid_request")
        if entity_id not in await hub_control.visible_entities(db, child_id):
            raise FamilyError("invalid_request")
    else:
        entity_id = None
    child = await child_of(db, ha_client, child_id)
    if child is None:
        raise FamilyError("not_permitted")
    settings = await fstore.get_settings(db)
    if category not in await allowed_categories(db, child_id, settings):
        raise FamilyError("requests_disabled")
    managers = await managers_of_child(db, child_id)
    if not managers:
        raise FamilyError("requests_disabled")
    await _check_request_limits(db, child_id, category, settings)
    expires = fstore.utcnow() + timedelta(minutes=settings["request_timeout_minutes"])
    async with db.transaction():
        if await fstore.open_request_of(db, child_id):
            raise FamilyError("request_limit")
        row = await fstore.insert_request(
            db, child_id, category, entity_id, note, expires
        )
    await events.log(
        db,
        events.REQUEST,
        client_id=child_id,
        category="activity",
        event_kind=events.REQUEST,
        entity_id=entity_id,
        action=category,
        detail={"request_id": row["id"]},
    )
    await events.emit(
        db,
        "kids_release_requested",
        client_id=child_id,
        entity_id=entity_id,
        action=category,
        extra={"request_id": row["id"]},
    )
    await request_changed(db, ha_client, row)
    return row


async def handle_child_message(
    db: Database,
    ha_client,
    child_id: str,
    message: dict[str, Any],
    route: str = "home",
) -> list[dict[str, Any]]:
    """``kids_request`` / ``kids_request_cancel`` of a child device; away
    (relayed by the integration) only while the device holds a key.
    """
    msg_type = message.get("type")
    try:
        child = await child_of(db, ha_client, child_id)
        if child is None:
            raise FamilyError("not_permitted")
        if route != "home" and not child_holds_key(child):
            raise FamilyError("not_permitted")
        if msg_type == "kids_request":
            from services import access

            # Outside its access the device asks nothing (docs/access.md:
            # family -> not_permitted); withdrawing stays possible.
            if not await access.client_open(db, child_id):
                raise FamilyError("not_permitted")
            row = await create_request(db, ha_client, child_id, message)
            return [
                {
                    "type": "kids_request_state",
                    "request": await request_view(db, ha_client, row, for_child=True),
                    "next_allowed_at": await next_allowed_at(db, child_id),
                }
            ]
        request_id = message.get("request_id")
        row = (
            await fstore.request_row(db, request_id)
            if isinstance(request_id, str)
            else None
        )
        if not row or row["client_id"] != child_id:
            raise FamilyError("request_not_found")
        if row["status"] == "open":
            async with db.transaction():
                closed = await fstore.close_request(db, row["id"], "cancelled", None)
                await fstore.drop_tokens(db, row["id"])
            if closed:
                row = await fstore.request_row(db, row["id"])
                await events.log(
                    db,
                    "kids_request_cancelled",
                    client_id=child_id,
                    category="activity",
                    event_kind=None,
                    action=row["category"],
                    detail={"request_id": row["id"]},
                )
                await _after_close(db, ha_client, row)
        return [
            {
                "type": "kids_request_state",
                "request": await request_view(db, ha_client, row, for_child=True),
                "next_allowed_at": await next_allowed_at(db, child_id),
            }
        ]
    except FamilyError as e:
        return [e.message()]


async def cancel_requests_of(db: Database, ha_client, child_id: str) -> list[str]:
    """The child device is being removed: its open requests end as
    ``cancelled`` (managers told, HA notifications cleared). Called before
    the device leaves, while its managers can still be found."""
    rows = await db.fetchall(
        "SELECT id FROM kids_requests WHERE client_id = ? AND status = 'open'",
        (child_id,),
    )
    ended = []
    for r in rows:
        async with db.transaction():
            closed = await fstore.close_request(db, r["id"], "cancelled", None)
            await fstore.drop_tokens(db, r["id"])
        if not closed:
            continue
        ended.append(r["id"])
        row = await fstore.request_row(db, r["id"])
        await events.log(
            db,
            "kids_request_cancelled",
            client_id=child_id,
            category="activity",
            event_kind=None,
            action=row["category"],
            detail={"request_id": row["id"], "removed": True},
        )
        try:
            await _after_close(db, ha_client, row)
        except Exception as e:  # noqa: BLE001 - the removal goes on
            logger.warning(
                "Request %s of %s not closed cleanly: %s", r["id"], child_id, e
            )
    return ended


async def _after_close(db: Database, ha_client, row: dict) -> None:
    from services.family import notify

    await notify.clear_request(ha_client, row["id"])
    await request_changed(db, ha_client, row)


# ── answers (first one wins) ────────────────────────────────────────────────


async def answer_request(
    db: Database,
    ha_client,
    request_id: Any,
    answer: Any,
    preset: Any,
    message: Any,
    manager: Manager,
    route: str,
) -> dict[str, Any]:
    """Answer a request; returns the closed request (view without viewer)."""
    if not isinstance(request_id, str) or not UUID_RE.match(request_id):
        raise FamilyError("request_not_found")
    row = await fstore.request_row(db, request_id)
    if not row or not (manager.admin or in_scope(manager, row["client_id"])):
        raise FamilyError("request_not_found")
    if answer not in ("yes", "no"):
        raise FamilyError("invalid_request")
    if message is not None and message not in data.decline_messages():
        raise FamilyError("invalid_request")
    if row["status"] != "open":
        raise FamilyError(
            "already_answered", request=await request_view(db, ha_client, row)
        )
    if fstore.parse(row["expires_at"]) <= fstore.utcnow():
        await expire_requests(db, ha_client)
        raise FamilyError(
            "already_answered",
            request=await request_view(
                db, ha_client, await fstore.request_row(db, request_id)
            ),
        )
    child = await child_of(db, ha_client, row["client_id"])
    if child is None:
        raise FamilyError("not_your_child")
    category = data.categories()[row["category"]]
    by = manager.actor(route)
    at = fstore.iso(fstore.utcnow())
    if manager.babysitter:
        # Only these durations, never beyond the end of the access.
        if answer == "yes" and preset not in manager.answer_presets():
            raise FamilyError("preset_not_allowed")
        allowed, reason = _yes_allowed(
            manager,
            child,
            {"kind": "answer_no", "category": row["category"]},
        )
        if not allowed:
            raise FamilyError(reason or "not_permitted")
    if answer == "yes":
        if not manager.babysitter and preset not in category["presets"]:
            raise FamilyError("invalid_request")
        op: dict[str, Any] = {"kind": "answer_yes", "category": row["category"]}
        if manager.babysitter:
            op["preset"] = preset
        if row.get("entity_id"):
            op["entity_id"] = row["entity_id"]
            op["device_class"] = await release_device_class(
                ha_client, child, row["entity_id"]
            )
        allowed, reason = _yes_allowed(manager, child, op)
        if not allowed:
            raise FamilyError(reason or "release_not_allowed")
        until = await release_until(db, ha_client, child.client_id, preset, manager)
        result = {
            "answer": "yes",
            "preset": preset,
            "until": fstore.iso(until),
            "message": None,
            "by": by,
            "at": at,
        }
        async with db.transaction():
            if not await fstore.close_request(db, request_id, "approved", result):
                raise FamilyError(
                    "already_answered",
                    request=await request_view(
                        db, ha_client, await fstore.request_row(db, request_id)
                    ),
                )
            await fstore.drop_tokens(db, request_id)
            # In the same transaction: never a Yes without its release.
            await fstore.put_release(
                db,
                child.client_id,
                category["release_kind"],
                row.get("entity_id"),
                until,
                by,
                request_id,
            )
        await release_started(db, ha_client, child, category["release_kind"])
    else:
        result = {"answer": "no", "until": None, "message": message, "by": by, "at": at}
        async with db.transaction():
            if not await fstore.close_request(db, request_id, "declined", result):
                raise FamilyError(
                    "already_answered",
                    request=await request_view(
                        db, ha_client, await fstore.request_row(db, request_id)
                    ),
                )
            await fstore.drop_tokens(db, request_id)
    row = await fstore.request_row(db, request_id)
    await events.log(
        db,
        events.ANSWERED,
        client_id=row["client_id"],
        category="activity",
        event_kind=events.ANSWERED,
        entity_id=row.get("entity_id"),
        action=answer,
        until=result.get("until"),
        by=by,
        detail={
            "request_id": request_id,
            "category": row["category"],
            "preset": result.get("preset"),
            "message": result.get("message"),
        },
    )
    await events.emit(
        db,
        "kids_request_answered",
        client_id=row["client_id"],
        entity_id=row.get("entity_id"),
        action=answer,
        until=result.get("until"),
        extra={"request_id": request_id, "by": by, "category": row["category"]},
    )
    await _after_close(db, ha_client, row)
    if answer == "yes":
        await child_updated(db, ha_client, row["client_id"])
    logger.info(
        "family request %s of %s answered %s by %s (%s)",
        request_id,
        row["client_id"],
        answer,
        by.get("kind"),
        route,
    )
    return await request_view(db, ha_client, row)


async def expire_requests(db: Database, ha_client) -> list[str]:
    now = fstore.utcnow()
    rows = await db.fetchall("SELECT * FROM kids_requests WHERE status = 'open'")
    expired = []
    for row in rows:
        end = fstore.parse(row["expires_at"])
        if end is not None and end > now:
            continue
        async with db.transaction():
            closed = await fstore.close_request(db, row["id"], "expired", None)
            await fstore.drop_tokens(db, row["id"])
        if not closed:
            continue
        expired.append(row["id"])
        row = await fstore.request_row(db, row["id"])
        await events.log(
            db,
            events.EXPIRED,
            client_id=row["client_id"],
            category="activity",
            event_kind=events.EXPIRED,
            entity_id=row.get("entity_id"),
            action=row["category"],
            detail={"request_id": row["id"]},
        )
        await events.emit(
            db,
            "kids_request_expired",
            client_id=row["client_id"],
            entity_id=row.get("entity_id"),
            action=row["category"],
            extra={"request_id": row["id"], "category": row["category"]},
        )
        await _after_close(db, ha_client, row)
    return expired


async def expire_releases(db: Database, ha_client) -> list[str]:
    now = fstore.utcnow()
    rows = await db.fetchall("SELECT * FROM kids_releases")
    due = [r for r in rows if (fstore.parse(r["until"]) or now) <= now]
    if not due:
        return []
    async with db.transaction():
        for r in due:
            await db.execute("DELETE FROM kids_releases WHERE id = ?", (r["id"],))
    children = sorted({r["client_id"] for r in due})
    await push_rules(db, ha_client)
    for r in due:
        await events.log(
            db,
            events.RELEASE_END,
            client_id=r["client_id"],
            category="change",
            event_kind=None,
            entity_id=r["entity_id"] or None,
            action=r["kind"],
            until=r["until"],
            by={"kind": "hub", "name": "Hub"},
        )
    for cid in children:
        await config_changed(cid)
        await child_updated(db, ha_client, cid)
    return children


# ── manager messages ────────────────────────────────────────────────────────


def _client_ids(message: dict) -> list[str]:
    ids = message.get("client_ids")
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 10
        or len(set(map(str, ids))) != len(ids)
        or not all(isinstance(i, str) and UUID_RE.match(i) for i in ids)
    ):
        raise FamilyError("invalid_request")
    return ids


def _echo(message: dict) -> str | None:
    rid = message.get("request_id")
    return rid if isinstance(rid, str) and 0 < len(rid) <= REQUEST_ID_MAX else None


PROFILE_REASONS = {
    "override.fixed": "rule_fixed",
    "override.boundary": "boundary",
    "override.value": "value_invalid",
    "override.not_applicable": "rule_not_manageable",
    "override.user_choice": "invalid_request",
    "client_not_found": "not_your_child",
    "rule_not_found": "rule_not_manageable",
}


async def _rule_write(
    db: Database,
    ha_client,
    manager: Manager,
    child: Child,
    rule: str,
    *,
    value: Any = None,
    reset: bool,
    end: str | None,
    note: str | None,
    route: str,
) -> None:
    from services.profiles import changes

    op: dict[str, Any] = {"kind": "rule_reset" if reset else "rule_set", "rule": rule}
    if not reset:
        op["value"] = value
    allowed, reason = check.family_check(manager.check_input(), child.check_input(), op)
    if not allowed:
        raise FamilyError(reason or "not_permitted")
    overrides = await pstore.overrides_of(db, child.client_id)
    before = [pstore.override_view(o) for o in overrides if o["rule_key"] == rule]
    if rule == "areas" and manager.visible_area_ids is not None:
        # Every value the child ends up with stays within the manager's own
        # areas: the one set and the one the child falls back to when the
        # adjustment goes (reset, or the end of a timed one that replaces it).
        ends_up_with = [] if reset else [value]
        if before and (reset or end is not None):
            others = [o for o in overrides if o["rule_key"] != rule]
            below = pstore.resolve_inputs(
                pstore.device_inputs(child.row, child.prof, others)
            )
            ends_up_with.append(below.get(rule, {}).get("value"))
        for areas in ends_up_with:
            allowed, reason = check.family_check(
                manager.check_input(),
                child.check_input(),
                {"kind": "rule_set", "rule": rule, "value": areas},
            )
            if not allowed:
                raise FamilyError(reason or "areas_not_allowed")
            if (
                isinstance(areas, dict)
                and areas.get("own_area")
                and child.row.get("own_area_id") not in manager.visible_area_ids
            ):
                raise FamilyError("areas_not_allowed")
    actor_name = (manager.owner_label or manager.name)[:100]

    async def write():
        if reset:
            await pstore.delete_override(db, child.client_id, rule)
        else:
            await pstore.put_override(
                db,
                child.client_id,
                rule,
                value=value,
                user_choice=False,
                expires_at=end,
                note=note,
                actor=actor_name,
                origin="family_manager",
                created_by_client_id=manager.client_id,
            )

    try:
        _, change = await changes.run(db, write, dry_run=False)
    except ProfileError as e:
        raise FamilyError(PROFILE_REASONS.get(e.code, "value_invalid")) from e
    after: dict[str, Any] = {
        "origin": "family_manager",
        "created_by_client_id": manager.client_id,
        "via": route,
    }
    if not reset:
        after.update(value=value, expires_at=end)
    await changes.audit(
        db,
        "override_removed" if reset else "override_set",
        actor=actor_name,
        client_id=child.client_id,
        rule_key=rule,
        before=before[0] if before else None,
        after=after,
        note=note,
    )
    await changes.apply_effects(db, change)
    await events.emit(
        db,
        "family_change",
        client_id=child.client_id,
        action="reset" if reset else "set",
        until=end,
        extra={
            "rule": rule,
            "by": manager.actor(route),
            "manager_id": manager.client_id,
        },
    )


async def _for_each_child(db, ha_client, manager: Manager, ids: list[str], fn):
    results = []
    changed = []
    for cid in ids:
        child = await child_of(db, ha_client, cid)
        try:
            if child is None or not in_scope(manager, cid):
                raise FamilyError("not_your_child")
            extra = await fn(child) or {}
            results.append({"client_id": cid, "status": "ok", **extra})
            changed.append(cid)
        except FamilyError as e:
            results.append({"client_id": cid, "status": "error", "reason": e.reason})
    for cid in changed:
        await child_updated(db, ha_client, cid)
    return results


async def handle_manager_message(
    db: Database,
    ha_client,
    client_id: str,
    message: dict[str, Any],
    route: str,
) -> list[dict[str, Any]]:
    """Answer one family message of a family manager device (fail closed)."""
    from api import ws as api_ws
    from services import kids_mode

    msg_type = message.get("type")
    echo = _echo(message)
    try:
        if msg_type not in MANAGER_TYPES:
            raise FamilyError("invalid_request")
        manager = await manager_of(db, client_id, include_babysitter=True)
        if manager.babysitter:
            # Only at home (hub WebSocket), only open requests and answers.
            manager.route = route
            if route != "home" or msg_type not in BABYSITTER_TYPES:
                raise FamilyError("not_permitted")
        elif route != "home" and not manager.remote:
            # Away only with effective remote access
            # (client_config.family.remote = away).
            raise FamilyError("not_permitted")
        _rate(client_id, msg_type)

        if msg_type == "family_get_children":
            connected = api_ws.get_connected_client_ids()
            children = []
            for row in await children_of(db, manager):
                child = await child_of(db, ha_client, row["id"])
                if child:
                    children.append(
                        await child_view(db, ha_client, manager, child, connected)
                    )
            out = {"type": "family_children", "children": children}

        elif msg_type in ("family_rule_set", "family_rule_reset"):
            ids = _client_ids(message)
            rule = message.get("rule")
            if not isinstance(rule, str):
                raise FamilyError("invalid_request")
            reset = msg_type == "family_rule_reset"
            end = None
            note = None
            if not reset:
                if "value" not in message:
                    raise FamilyError("invalid_request")
                note = message.get("note")
                if note is not None and (not isinstance(note, str) or len(note) > 200):
                    raise FamilyError("invalid_request")
                try:
                    end = pstore.expires_from_request(
                        message.get("expires"),
                        message.get("expires_at"),
                        await kids_mode.hub_time_zone(ha_client),
                    )
                except ProfileError as e:
                    raise FamilyError("invalid_request") from e

            async def write(child: Child):
                await _rule_write(
                    db,
                    ha_client,
                    manager,
                    child,
                    rule,
                    value=message.get("value"),
                    reset=reset,
                    end=end,
                    note=note,
                    route=route,
                )
                return {"until": end}

            out = {
                "type": "family_result",
                "op": "rule_reset" if reset else "rule_set",
                "results": await _for_each_child(db, ha_client, manager, ids, write),
            }

        elif msg_type == "family_release":
            ids = _client_ids(message)
            kind = message.get("kind")
            preset = message.get("preset")
            entity_id = message.get("entity_id")
            if (
                kind not in ("bedtime", "pause", "entity")
                or preset not in data.presets()
            ):
                raise FamilyError("invalid_request")
            if kind == "pause" and preset == "bedtime_end":
                raise FamilyError("invalid_request")
            if kind == "entity":
                if not isinstance(entity_id, str) or not ENTITY_RE.match(entity_id):
                    raise FamilyError("invalid_request")
            else:
                entity_id = None

            async def release(child: Child):
                op: dict[str, Any] = {"kind": "release", "release_kind": kind}
                if entity_id:
                    op["entity_id"] = entity_id
                    op["device_class"] = await release_device_class(
                        ha_client, child, entity_id
                    )
                allowed, reason = check.family_check(
                    manager.check_input(), child.check_input(), op
                )
                if not allowed:
                    raise FamilyError(reason or "release_not_allowed")
                until = await start_release(
                    db, ha_client, manager, child, kind, entity_id, preset, route
                )
                await config_changed(child.client_id)
                return {"until": fstore.iso(until)}

            out = {
                "type": "family_result",
                "op": "release",
                "results": await _for_each_child(db, ha_client, manager, ids, release),
            }

        elif msg_type == "family_release_end":
            ids = _client_ids(message)
            kind = message.get("kind")
            entity_id = message.get("entity_id")
            if kind is not None and kind not in ("bedtime", "pause", "entity"):
                raise FamilyError("invalid_request")
            if entity_id is not None and (
                not isinstance(entity_id, str) or not ENTITY_RE.match(entity_id)
            ):
                raise FamilyError("invalid_request")

            async def end_release(child: Child):
                allowed, reason = check.family_check(
                    manager.check_input(), child.check_input(), {"kind": "release_end"}
                )
                if not allowed:
                    raise FamilyError(reason or "not_permitted")
                async with db.transaction():
                    ended = await fstore.end_releases(
                        db, child.client_id, kind, entity_id
                    )
                for r in ended:
                    await events.log(
                        db,
                        events.RELEASE_END,
                        client_id=child.client_id,
                        category="change",
                        event_kind=events.FAMILY_CHANGE,
                        entity_id=r["entity_id"] or None,
                        action=r["kind"],
                        by=manager.actor(route),
                    )
                if ended:
                    await push_rules(db, ha_client)
                    await config_changed(child.client_id)
                return {"until": None}

            out = {
                "type": "family_result",
                "op": "release_end",
                "results": await _for_each_child(
                    db, ha_client, manager, ids, end_release
                ),
            }

        elif msg_type == "family_get_requests":
            include_closed = (
                message.get("include_closed", True) is not False
                and not manager.babysitter
            )
            ids = [c["id"] for c in await children_of(db, manager)]
            rows = await fstore.requests_for(
                db, ids, include_closed=include_closed, since=_since_days(1)
            )
            out = {
                "type": "family_requests",
                "requests": [
                    await request_view(db, ha_client, r, viewer=manager) for r in rows
                ],
            }

        elif msg_type == "family_answer":
            request = await answer_request(
                db,
                ha_client,
                message.get("request_id"),
                message.get("answer"),
                message.get("preset"),
                message.get("message"),
                manager,
                route,
            )
            out = {"type": "family_request", "request": request}

        elif msg_type == "family_get_activity":
            out = await activity(db, ha_client, manager, message)

        else:  # family_notify_set
            prefs = message.get("notify")
            if not isinstance(prefs, dict) or not all(
                isinstance(v, bool) for v in prefs.values()
            ):
                raise FamilyError("invalid_request")
            defaults = data.notify_defaults()
            stored = (await fstore.scope_row(db, client_id))["notify"]
            for kind, value in prefs.items():
                if kind in defaults and defaults[kind]["can_disable"]:
                    stored[kind] = value
            async with db.transaction():
                await fstore.put_scope(db, client_id, notify=stored, actor=manager.name)
            await config_changed(client_id)
            out = {"type": "family_notify", "notify": notify_prefs(stored)}

    except FamilyError as e:
        return [e.message(echo)]
    if echo and out["type"] != "family_request":
        out["request_id"] = echo
    return [out]


# ── activity ────────────────────────────────────────────────────────────────


async def activity(
    db: Database, ha_client, manager: Manager, message: dict[str, Any]
) -> dict[str, Any]:
    settings = await fstore.get_settings(db)
    children = await children_of(db, manager)
    by_id = {c["id"]: c for c in children}
    wanted = message.get("client_id")
    if wanted is not None:
        if not isinstance(wanted, str):
            raise FamilyError("invalid_request")
        if wanted not in by_id:
            raise FamilyError("not_your_child")
        ids = [wanted]
    else:
        ids = list(by_id)
    kinds = message.get("kinds")
    if kinds is not None and (
        not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds)
    ):
        raise FamilyError("invalid_request")
    limit = message.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise FamilyError("invalid_request")
    limit = min(limit, data.limits()["activity_page_max"])
    since = _since_days(settings["activity_days"])
    if isinstance(message.get("since"), str):
        asked = fstore.parse(message["since"])
        if asked is None:
            raise FamilyError("invalid_request")
        since = max(since, asked)
    cursor = message.get("cursor")
    before = None
    if cursor is not None:
        if not isinstance(cursor, str) or not CURSOR_RE.match(cursor):
            raise FamilyError("invalid_request")
        before = int(cursor)
    rows = await _log_rows(
        db,
        ids,
        since=since,
        kinds=_log_kinds(kinds),
        before_id=before,
        limit=limit + 1,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    names: dict[str, str | None] = {}
    items = []
    for r in rows:
        eid = r.get("entity_id")
        if eid and eid not in names:
            names[eid] = await entity_name(ha_client, eid)
        detail = fstore._loads(r.get("detail_json"), {})
        kind = ACTIVITY_KIND[r["kind"]]
        reason = None
        if kind == "blocked":
            reason = (
                "quiet_hours"
                if r["kind"] == "kids_bedtime_attempt"
                else BLOCKED_REASON.get(r.get("reason") or "")
            )
        elif kind == "pause":
            reason = (
                "paused_all"
                if r["kind"] == "kids_paused_all"
                or r.get("reason") == "blocked_kids_paused_all"
                else "rate_limited"
            )
        by = detail.get("by")
        if not by and r.get("actor"):
            by = {"kind": "admin", "name": r["actor"][:100]}
        action = r.get("action")
        item: dict[str, Any] = {
            "id": str(r["id"]),
            "at": fstore.iso(fstore.parse(r["ts"])),
            "client_id": r["client_id"],
            "child_name": child_display(by_id.get(r["client_id"]) or {})[:100],
            "kind": kind,
            "entity_id": eid,
            "entity_name": names.get(eid) if eid else None,
            "action": action
            if isinstance(action, str) and re.match(r"^[a-z_]{1,64}$", action)
            else None,
            "reason": reason,
            "rule": detail.get("rule") if kind == "change" else None,
            "request_id": detail.get("request_id"),
            "until": fstore.iso(fstore.parse(r.get("until"))),
            "by": by,
        }
        items.append(item)
    summary = []
    for cid in ids:
        row = by_id[cid]
        child = await child_of(db, ha_client, cid)
        counts = await today_counts(db, cid, child.tz if child else None)
        summary.append({"client_id": row["id"], **counts})
    return {
        "type": "family_activity",
        "items": items,
        "next_cursor": str(rows[-1]["id"]) if more and rows else None,
        "summary": summary,
    }


# ── client_config ───────────────────────────────────────────────────────────


async def client_config_family(
    db: Database, client_id: str, rules: dict[str, dict], row: dict, child: bool
) -> dict[str, Any] | None:
    """``client_config.family`` (exactly when the effective rule is on)."""
    if child or row.get("access") != "member":
        return None
    if rules.get("family_manager", {}).get("value") is not True:
        return None
    sc = await fstore.scope_row(db, client_id)
    manager = Manager(
        client_id=client_id,
        name=row.get("name") or "",
        owner_label=row.get("owner_label"),
        platform=row.get("platform"),
        scope=sc["scope"],
        visible_area_ids=None,
        notify_targets=sc["notify_targets"],
        notify=notify_prefs(sc["notify"]),
        remote=rules.get("remote_access", {}).get("value") is True,
    )
    children = await children_of(db, manager)
    open_count = 0
    if children:
        marks = ",".join("?" for _ in children)
        r = await db.fetchone(
            f"SELECT COUNT(*) AS n FROM kids_requests WHERE status = 'open' "
            f"AND client_id IN ({marks})",
            tuple(c["id"] for c in children),
        )
        open_count = int(r["n"] or 0)
    out_children = []
    for c in children[:50]:
        item: dict[str, Any] = {
            "client_id": c["id"],
            "name": c["name"][:100],
            "platform": c.get("platform") or "ios",
        }
        if c.get("owner_label"):
            item["owner_label"] = c["owner_label"]
        out_children.append(item)
    return {
        "scope": sc["scope"],
        "children": out_children,
        "open_requests": open_count,
        "notify": manager.notify,
        "notify_targets": sc["notify_targets"],
        "remote": "away" if manager.remote else "home_only",
    }


async def client_config_babysitter(
    db: Database, row: dict, prof: dict, rules: dict, hours: dict
) -> dict[str, Any] | None:
    """``client_config.babysitter``: only devices of kind babysitter
    with the rule babysitter_requests."""
    from api import ws as api_ws

    if (prof.get("kind") or "person") != "babysitter":
        return None
    if rules.get("babysitter_requests", {}).get("value") is not True:
        return None
    settings = await fstore.get_settings(db)
    until = hours.get("until") if hours.get("open") else None
    presets = [p for p in data.babysitter()["presets"] if p != "access_end" or until]
    active = (
        bool(hours.get("open"))
        and bool(settings.get("requests_enabled", True))
        and row["id"] in api_ws.get_connected_client_ids()
    )
    return {"active": active, "presets": presets, "access_until": until}


async def client_config_kids(db: Database, ha_client, client_id: str) -> dict[str, Any]:
    """``client_config.kids.releases`` and ``kids.requests`` of a child device."""
    return {
        "releases": [
            fstore.release_view(r, for_child=True)
            for r in await fstore.releases_of(db, client_id)
        ],
        "requests": await kids_requests_view(db, ha_client, client_id),
    }
