"""Ask for early access (contract ``docs/access.md#early-access``).

A device outside its access hours (closed by the schedule) whose effective
rule ``access_requests`` is on asks for access now (``access_request``, only
over the hub WebSocket: a closed device holds no valid HA key).
Recipients: the device that set it up (``set_up_by``), every person device
whose ``setup_devices.kinds`` holds its kind and, for a babysitter, every
family manager. They answer with Yes and a duration or No; the first answer
wins. A Yes stores an early access on the device
(``paired_clients.access_early_json``, ``services/access.py``): the windows do
not close it until then, the switch still does.

Entry points: :func:`handle_message` (WebSocket and the integration relay),
:func:`answer` (app, admin UI, HA notification button), :func:`tick` (expiry,
called by the access planner), views for ``client_config`` and the admin API.
Every step is logged with its author; events ``access_requested``,
``access_request_answered``, ``access_request_expired``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from models.database import Database
from namespace import NAMESPACE

logger = logging.getLogger("hub.access_requests")

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "access_requests.json"
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
TARGET_RE = re.compile(r"^notify\.[a-z0-9_]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
ACTION_PREFIX = f"{NAMESPACE.upper()}_ACCESS_"
TAG_PREFIX = f"{NAMESPACE}-access-"
ACTION_EVENT = "mobile_app_notification_action"
REQUESTER_TYPES = ("access_request", "access_request_cancel")
RECIPIENT_TYPES = ("access_requests_get", "access_request_answer")
TYPES = REQUESTER_TYPES + RECIPIENT_TYPES
ROLE_ORDER = {"set_up_by": 0, "setup": 1, "family_manager": 2}
NAME_MAX = 50
HISTORY_DAYS = 7

MESSAGES = {
    "invalid_request": "The request is not valid.",
    "not_permitted": "This device may not do this.",
    "not_closed": "The device has access right now.",
    "switch_off": "The access switch is off; a request cannot open it.",
    "home_only": "Early access can only be requested at home.",
    "no_recipients": "Nobody can answer right now.",
    "request_limit": "Too many requests.",
    "cooldown": "Please wait before asking again.",
    "rate_limited": "Too many messages, try again later.",
    "request_not_found": "Unknown request.",
    "already_answered": "This request was already answered.",
    "duration_not_allowed": "This duration is not possible for this request.",
    "hub_unreachable": "The hub is not reachable.",
}


@cache
def catalog() -> dict[str, Any]:
    return json.loads(DATA_FILE.read_text("utf-8"))


def limits() -> dict[str, int]:
    return catalog()["limits"]


def decline_messages() -> list[str]:
    return [m["id"] for m in catalog()["decline_messages"]]


def duration_ids() -> list[str]:
    return [d["id"] for d in catalog()["durations"]]


class AccessRequestError(Exception):
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

    def message(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "access_request_error",
            "reason": self.reason,
            "message": MESSAGES.get(self.reason, self.reason),
        }
        if self.retry_after is not None:
            out["retry_after"] = max(0, int(self.retry_after))
        if self.request is not None:
            out["request"] = self.request
        return out


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str | None) -> datetime | None:
    from services import access

    return access.parse_ts(value)


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


# ── rate limits (in memory) ─────────────────────────────────────────────────


def _limiter():
    from services.family.service import Window

    global _window
    if _window is None:
        _window = Window()
    return _window


_window = None


def _rate(client_id: str, kind: str) -> None:
    lim = limits()
    if kind == "answer":
        args = ("access_answer", lim["answers_per_recipient_per_hour"], 3600.0)
    else:
        args = ("access_msg", lim["messages_per_device_per_minute"], 60.0)
    wait = _limiter().hit((client_id, args[0]), args[1], args[2])
    if wait is not None:
        raise AccessRequestError("rate_limited", retry_after=wait)


def reset() -> None:
    """Forget rate limits and sent notifications (tests)."""
    if _window is not None:
        _window.reset()
    _sent.clear()


# ── recipients ──────────────────────────────────────────────────────────────


@dataclass
class Recipient:
    client_id: str
    name: str
    owner_label: str | None
    role: str
    remote: bool
    notify_targets: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return (self.owner_label or self.name or "")[:NAME_MAX]

    def actor(self, via: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": "device",
            "client_id": self.client_id,
            "name": (self.owner_label or self.name or "")[:100],
            "via": via,
        }
        if self.owner_label:
            out["owner_label"] = self.owner_label
        return out


def admin_actor(name: str | None, via: str = "admin_ui") -> dict[str, Any]:
    return {
        "kind": "admin",
        "client_id": None,
        "name": (name or "Admin")[:100],
        "via": via,
    }


def requester_actor(by: Any) -> Any:
    """What the asking device learns about who answered or opened it: kind
    and display name – never that device's id or route (access-request.json)."""
    if not isinstance(by, dict):
        return by
    return {"kind": by.get("kind"), "client_id": None, "name": by.get("name")}


def _kind(prof: dict) -> str:
    return prof.get("kind") or "person"


async def recipients_of(db: Database, client: dict, prof: dict) -> list[Recipient]:
    """Who gets the early access requests of ``client`` right now."""
    from services import access
    from services.family import store as fstore
    from services.profiles import store as pstore

    kind = _kind(prof)
    set_up_id = client.get("set_up_by_client_id")
    now = utcnow()
    tz = access.time_zone()
    out: list[Recipient] = []
    for cid, item in (await pstore.resolve_all(db)).items():
        c, p, rules = item["client"], item["profile"], item["rules"]
        if (
            cid == client["id"]
            or not c.get("is_active")
            or c.get("pending_deletion_at")
        ):
            continue
        if c.get("access") != "member" or p["child_protection"] or _kind(p) != "person":
            continue
        if not access.state_of(c, p, rules, now=now, tz_name=tz)["open"]:
            continue
        role = None
        setup = rules.get("setup_devices", {}).get("value") or {}
        if cid == set_up_id:
            role = "set_up_by"
        elif setup.get("enabled") and kind in (setup.get("kinds") or []):
            role = "setup"
        elif (
            kind == "babysitter"
            and rules.get("family_manager", {}).get("value") is True
        ):
            role = "family_manager"
        if role is None:
            continue
        scope = await fstore.scope_row(db, cid)
        out.append(
            Recipient(
                client_id=cid,
                name=c.get("name") or "",
                owner_label=c.get("owner_label"),
                role=role,
                remote=rules.get("remote_access", {}).get("value") is True,
                notify_targets=[
                    t for t in scope["notify_targets"] if TARGET_RE.match(t)
                ],
            )
        )
    out.sort(key=lambda r: (ROLE_ORDER[r.role], r.display.casefold()))
    return out


def recipients_view(recipients: list[Recipient]) -> list[dict[str, str]]:
    """``[{name, role}]``: set_up_by first, then by name; a name once."""
    seen: set[str] = set()
    out = []
    for r in recipients:
        name = r.display
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append({"name": name, "role": r.role})
    return out[: limits()["recipients_max"]]


async def admin_notified(db: Database) -> bool:
    """An enabled notification rule of the admin for ``access_requested``."""
    from services import event_notify

    try:
        config = await event_notify.get_config(db)
    except Exception:  # noqa: BLE001 - fail closed
        return False
    for rule in config["rules"]:
        targets = rule.get("targets") or {}
        if (
            rule.get("enabled")
            and "access_requested" in (rule.get("kinds") or [])
            and (targets.get("notify") or targets.get("devices"))
        ):
            return True
    return False


# ── durations ───────────────────────────────────────────────────────────────


def durations_for(opens_at: datetime | None, now: datetime) -> list[str]:
    out = []
    max_ahead = timedelta(hours=limits()["until_window_start_max_hours"])
    if opens_at is not None and now < opens_at <= now + max_ahead:
        out.append("until_window_start")
    return out + ["1h", "3h", "today"]


def until_for(
    duration: str, now: datetime, opens_at: datetime | None, tz_name: str | None
) -> datetime | None:
    from services import kids_rules

    if duration == "until_window_start":
        return opens_at
    if duration == "today":
        local = now.astimezone(kids_rules.zone(tz_name))
        midnight = datetime.combine(
            local.date() + timedelta(days=1), datetime.min.time(), tzinfo=local.tzinfo
        )
        return midnight.astimezone(timezone.utc)
    minutes = next(
        (d.get("minutes") for d in catalog()["durations"] if d["id"] == duration), None
    )
    return now + timedelta(minutes=minutes) if minutes else None


# ── storage ─────────────────────────────────────────────────────────────────


async def request_row(db: Database, request_id: str) -> dict | None:
    return await db.fetchone(
        "SELECT * FROM access_requests WHERE id = ?", (request_id,)
    )


async def open_request_of(db: Database, client_id: str) -> dict | None:
    return await db.fetchone(
        "SELECT * FROM access_requests WHERE client_id = ? AND status = 'open' "
        "ORDER BY created_at DESC LIMIT 1",
        (client_id,),
    )


async def _close(
    db: Database, request_id: str, status: str, answer: dict | None
) -> bool:
    """Close an open request (inside the caller's transaction); False when it
    was not open any more (another answer was faster)."""
    cursor = await db.execute(
        "UPDATE access_requests SET status = ?, answer_json = ?, answered_at = ? "
        "WHERE id = ? AND status = 'open'",
        (
            status,
            json.dumps(answer, ensure_ascii=False) if answer else None,
            iso(utcnow()),
            request_id,
        ),
    )
    await db.execute(
        "DELETE FROM access_answer_tokens WHERE request_id = ?", (request_id,)
    )
    return bool(cursor.rowcount)


async def next_allowed_at(db: Database, client_id: str) -> datetime | None:
    lim = limits()
    now = utcnow()
    times = []
    rows = await db.fetchall(
        "SELECT created_at FROM access_requests WHERE client_id = ? AND created_at > ? "
        "ORDER BY created_at",
        (client_id, iso(now - timedelta(hours=1))),
    )
    if len(rows) >= lim["requests_per_hour"]:
        oldest = parse(rows[len(rows) - lim["requests_per_hour"]]["created_at"])
        if oldest:
            times.append(oldest + timedelta(hours=1))
    last_no = await db.fetchone(
        "SELECT answered_at FROM access_requests WHERE client_id = ? AND "
        "status = 'declined' ORDER BY answered_at DESC LIMIT 1",
        (client_id,),
    )
    if last_no and lim["decline_cooldown_minutes"]:
        t = parse(last_no.get("answered_at"))
        if t:
            end = t + timedelta(minutes=lim["decline_cooldown_minutes"])
            if end > now:
                times.append(end)
    return max(times) if times else None


# ── views ───────────────────────────────────────────────────────────────────


async def view(
    db: Database,
    row: dict,
    *,
    viewer: Recipient | None = None,
    can_answer: bool | None = None,
    own: bool = False,
) -> dict[str, Any]:
    """``common/access-request.json``. ``own``: the copy for the asking device."""
    client = (
        await db.fetchone(
            "SELECT c.name, c.owner_label, p.kind FROM paired_clients c "
            "LEFT JOIN profiles p ON p.id = c.profile_id WHERE c.id = ?",
            (row["client_id"],),
        )
        or {}
    )
    opens_at = parse(row.get("opens_at"))
    created = parse(row["created_at"]) or utcnow()
    now = utcnow()
    out: dict[str, Any] = {
        "request_id": row["id"],
        "client_id": row["client_id"],
        "device_name": (client.get("name") or "")[:100],
        "owner_label": client.get("owner_label"),
        "profile_kind": client.get("kind") or "person",
        "note": row.get("note"),
        "status": row["status"],
        "created_at": iso(created),
        "expires_at": iso(parse(row["expires_at"])),
        "opens_at": iso(opens_at),
        "durations": durations_for(
            opens_at, now if row["status"] == "open" else created
        ),
        "recipients": _loads(row.get("recipients_json"), []),
        "answer": _loads(row.get("answer_json"), None)
        if row["status"] in ("approved", "declined")
        else None,
    }
    if own and out["answer"]:
        out["answer"] = {
            **out["answer"],
            "by": requester_actor(out["answer"].get("by")),
        }
    if viewer is not None or can_answer is not None:
        out["can_answer"] = bool(row["status"] == "open" and (can_answer is not False))
    return out


async def request_config(
    db: Database, client: dict, prof: dict, rules: dict, hours: dict
) -> dict[str, Any] | None:
    """``client_config.access.request`` (None while the rule is off)."""
    from services import access

    if (rules.get("access_requests") or {}).get("value") is not True:
        return None
    enabled = access.schedule_enabled(rules)
    open_row = await open_request_of(db, client["id"])
    recipients = await recipients_of(db, client, prof) if enabled else []
    nxt = await next_allowed_at(db, client["id"])
    available = bool(
        enabled
        and hours.get("open") is False
        and hours.get("reason") == "schedule"
        and open_row is None
        and nxt is None
        and (recipients or await admin_notified(db))
    )
    return {
        "enabled": enabled,
        "available": available,
        "open": await view(db, open_row, own=True) if open_row else None,
        "next_allowed_at": iso(nxt),
        "recipients": recipients_view(recipients),
    }


# ── pushes ──────────────────────────────────────────────────────────────────


async def _send(client_id: str, msg: dict) -> None:
    from api import ws as api_ws

    try:
        await api_ws.send_to_client(client_id, msg)
    except Exception as e:  # noqa: BLE001 - best effort
        logger.debug("push to %s failed: %s", client_id, e)


async def push_to_recipients(
    db: Database, recipients: list[Recipient], row: dict
) -> None:
    """``access_request_update`` to every recipient; to the ones not
    connected but with remote access through the integration relay."""
    from api import ws as api_ws

    connected = api_ws.get_connected_client_ids()
    relay: list[str] = []
    for r in recipients:
        msg = {
            "type": "access_request_update",
            "request": await view(db, row, viewer=r),
        }
        if r.client_id in connected:
            await _send(r.client_id, msg)
        elif r.remote:
            relay.append(r.client_id)
    if relay and api_ws.get_integration_push_connected():
        msg = {
            "type": "access_request_update",
            "request": await view(db, row, can_answer=True),
        }
        msg["request"]["can_answer"] = row["status"] == "open"
        try:
            await api_ws.send_to_integration(
                {"type": "family_relay", "client_ids": relay[:20], "message": msg}
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("family_relay (access) failed: %s", e)


async def admin_changed(client_ids: list[str], request_id: str | None = None) -> None:
    from api import ws as api_ws
    from api.ws.broadcast import EventAudience

    try:
        await api_ws.broadcast(
            {
                "type": "access_requests_changed",
                "data": {
                    "client_ids": sorted(set(client_ids)),
                    "request_id": request_id,
                },
            },
            audience=EventAudience.ADMIN,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("access_requests_changed broadcast failed: %s", e)


async def config_changed(client_id: str) -> None:
    await _send(client_id, {"type": "client_config_updated", "data": {}})


async def request_changed(
    db: Database,
    row: dict,
    recipients: list[Recipient] | None = None,
    *,
    tell_device: bool = True,
) -> None:
    """A request was created or closed: requester, recipients, admins.

    ``tell_device`` False: the device caused it itself (ask, withdraw) and
    gets ``access_request_state`` as the answer, not twice."""
    if recipients is None:
        recipients = await _recipients_for_row(db, row)
    await push_to_recipients(db, recipients, row)
    if tell_device:
        nxt = await next_allowed_at(db, row["client_id"])
        await _send(
            row["client_id"],
            {
                "type": "access_request_state",
                "request": await view(db, row, own=True),
                "next_allowed_at": iso(nxt),
            },
        )
    await config_changed(row["client_id"])
    await admin_changed([row["client_id"]], row["id"])


async def _recipients_for_row(db: Database, row: dict) -> list[Recipient]:
    from services.profiles import store as pstore

    resolved = await pstore.resolve_client(db, row["client_id"])
    if not resolved:
        return []
    client, prof, _rules = resolved
    return await recipients_of(db, client, prof)


# ── log ─────────────────────────────────────────────────────────────────────


async def _log(
    db: Database,
    kind: str,
    *,
    client_id: str,
    category: str,
    event_kind: str | None,
    action: str | None = None,
    until: str | None = None,
    by: dict | None = None,
    detail: dict | None = None,
) -> None:
    from services import event_log

    info = dict(detail or {})
    fields: dict[str, Any] = {}
    if by:
        info["by"] = by
        via = by.get("via")
        # home/away is the route; origin (app, quick_access) stays unset.
        if via in ("home", "away"):
            fields["route"] = {"home": "local", "away": "external"}[via]
    await event_log.record(
        db,
        category=category,
        kind=kind,
        event_kind=event_kind,
        source="hub",
        client_id=client_id,
        action=action,
        until=until,
        actor=(by or {}).get("name"),
        detail=info or None,
        **fields,
    )


# ── asking ──────────────────────────────────────────────────────────────────


async def create(
    db: Database, ha_client, client_id: str, message: dict[str, Any], route: str
) -> dict:
    from services import access
    from services.onboarding import WATCH_PLATFORMS
    from services.profiles import store as pstore

    if route != "home":
        raise AccessRequestError("home_only")
    resolved = await pstore.resolve_client(db, client_id)
    if not resolved:
        raise AccessRequestError("not_permitted")
    client, prof, rules = resolved
    if (
        not client.get("is_active")
        or client.get("pending_deletion_at")
        or client.get("platform") in WATCH_PLATFORMS
        or (rules.get("access_requests") or {}).get("value") is not True
        or not access.schedule_enabled(rules)
    ):
        raise AccessRequestError("not_permitted")
    note = message.get("note")
    if note is not None:
        if not isinstance(note, str) or len(note) > limits()["note_max_length"]:
            raise AccessRequestError("invalid_request")
        note = " ".join("".join(ch for ch in note if ch.isprintable()).split()) or None
    state = access.state_of(client, prof, rules)
    if state["open"]:
        raise AccessRequestError("not_closed")
    if state["reason"] == "switch":
        raise AccessRequestError("switch_off")
    if await open_request_of(db, client_id):
        raise AccessRequestError("request_limit")
    nxt = await next_allowed_at(db, client_id)
    if nxt is not None:
        last_no = await db.fetchone(
            "SELECT answered_at FROM access_requests WHERE client_id = ? AND "
            "status = 'declined' ORDER BY answered_at DESC LIMIT 1",
            (client_id,),
        )
        cooling = bool(
            last_no
            and (parse(last_no.get("answered_at")) or utcnow())
            + timedelta(minutes=limits()["decline_cooldown_minutes"])
            > utcnow()
        )
        raise AccessRequestError(
            "cooldown" if cooling else "request_limit",
            retry_after=int((nxt - utcnow()).total_seconds()) + 1,
        )
    recipients = await recipients_of(db, client, prof)
    if not recipients and not await admin_notified(db):
        raise AccessRequestError("no_recipients")
    now = utcnow()
    row_id = str(uuid.uuid4())
    async with db.transaction():
        if await open_request_of(db, client_id):
            raise AccessRequestError("request_limit")
        await db.execute(
            "INSERT INTO access_requests (id, client_id, note, status, created_at, "
            "expires_at, opens_at, recipients_json) VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (
                row_id,
                client_id,
                note,
                iso(now),
                iso(now + timedelta(minutes=limits()["request_timeout_minutes"])),
                state["opens_at"],
                json.dumps(recipients_view(recipients), ensure_ascii=False),
            ),
        )
    row = await request_row(db, row_id)
    assert row is not None
    await _log(
        db,
        "access_requested",
        client_id=client_id,
        category="activity",
        event_kind="access_requested",
        until=state["opens_at"],
        detail={"request_id": row_id, "note": note, "recipients": len(recipients)},
    )
    await request_changed(db, row, recipients, tell_device=False)
    _spawn_notifications(db, ha_client, row, recipients)
    from services import access as access_svc

    access_svc.wake()
    logger.info(
        "early access requested by %s (%d recipient(s))", client_id, len(recipients)
    )
    return row


async def cancel(db: Database, ha_client, client_id: str, request_id: Any) -> dict:
    row = await request_row(db, request_id) if isinstance(request_id, str) else None
    if not row or row["client_id"] != client_id:
        raise AccessRequestError("request_not_found")
    if row["status"] == "open":
        async with db.transaction():
            closed = await _close(db, row["id"], "cancelled", None)
        if closed:
            row = await request_row(db, row["id"]) or row
            await _log(
                db,
                "access_request_cancelled",
                client_id=client_id,
                category="activity",
                event_kind=None,
                detail={"request_id": row["id"], "by": "device"},
            )
            await _after_close(db, ha_client, row, tell_device=False)
    return row


async def _after_close(
    db: Database, ha_client, row: dict, *, tell_device: bool = True
) -> None:
    await clear_notifications(ha_client, row["id"])
    await request_changed(db, row, tell_device=tell_device)


# ── answers (first one wins) ────────────────────────────────────────────────


async def answer(
    db: Database,
    ha_client,
    request_id: Any,
    answer_value: Any,
    duration: Any,
    message: Any,
    *,
    recipient: Recipient | None,
    admin_name: str | None = None,
    via: str,
) -> dict:
    """Answer a request (recipient device, admin, notification button)."""
    from services import access

    if not isinstance(request_id, str) or not UUID_RE.match(request_id):
        raise AccessRequestError("request_not_found")
    row = await request_row(db, request_id)
    if not row:
        raise AccessRequestError("request_not_found")
    if recipient is not None:
        current = {r.client_id: r for r in await _recipients_for_row(db, row)}
        if recipient.client_id not in current:
            raise AccessRequestError("request_not_found")
        recipient = current[recipient.client_id]
    if answer_value not in ("yes", "no"):
        raise AccessRequestError("invalid_request")
    if message is not None and message not in decline_messages():
        raise AccessRequestError("invalid_request")
    if row["status"] != "open":
        raise AccessRequestError("already_answered", request=await view(db, row))
    now = utcnow()
    if (parse(row["expires_at"]) or now) <= now:
        await tick(db, ha_client)
        raise AccessRequestError(
            "already_answered",
            request=await view(db, await request_row(db, request_id) or row),
        )
    by = recipient.actor(via) if recipient else admin_actor(admin_name, via)
    at = iso(now)
    opens_at = parse(row.get("opens_at"))
    if answer_value == "yes":
        if duration not in durations_for(opens_at, now):
            raise AccessRequestError("duration_not_allowed")
        until = until_for(duration, now, opens_at, access.time_zone())
        if until is None or until <= now:
            raise AccessRequestError("duration_not_allowed")
        result = {
            "answer": "yes",
            "duration": duration,
            "until": iso(until),
            "message": None,
            "by": by,
            "at": at,
        }
        early = {
            "until": iso(until),
            "by": by,
            "request_id": request_id,
            "duration": duration,
            "at": at,
        }
        async with db.transaction():
            if not await _close(db, request_id, "approved", result):
                raise AccessRequestError(
                    "already_answered",
                    request=await view(db, await request_row(db, request_id) or row),
                )
            await db.execute(
                "UPDATE paired_clients SET access_early_json = ? WHERE id = ?",
                (json.dumps(early, ensure_ascii=False), row["client_id"]),
            )
    else:
        if duration is not None and duration not in duration_ids():
            raise AccessRequestError("invalid_request")
        result = {
            "answer": "no",
            "duration": None,
            "until": None,
            "message": message,
            "by": by,
            "at": at,
        }
        async with db.transaction():
            if not await _close(db, request_id, "declined", result):
                raise AccessRequestError(
                    "already_answered",
                    request=await view(db, await request_row(db, request_id) or row),
                )
    row = await request_row(db, request_id) or row
    await _log(
        db,
        "access_request_answered",
        client_id=row["client_id"],
        category="change",
        event_kind="access_request_answered",
        action=answer_value,
        until=result["until"],
        by=by,
        detail={
            "request_id": request_id,
            "duration": result["duration"],
            "message": result["message"],
        },
    )
    if answer_value == "yes":
        # Opens the device now (log access_opened, client_config_updated with
        # its key, snapshot with early_until).
        from services import enforcement

        enforcement.notify_change("access")
        await access.check_transitions(db, only=[row["client_id"]])
    await _after_close(db, ha_client, row)
    access.wake()
    logger.info(
        "early access request %s of %s answered %s by %s (%s)",
        request_id,
        row["client_id"],
        answer_value,
        by["kind"],
        via,
    )
    return row


async def end_early(db: Database, client_id: str, actor: str | None) -> bool:
    """The admin ends a running early access (DELETE …/early-access)."""
    from services import access, enforcement

    row = await db.fetchone(
        "SELECT id, access_early_json FROM paired_clients WHERE id = ?", (client_id,)
    )
    if not row:
        raise AccessRequestError("request_not_found")
    early = access.running_early(row, utcnow())
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET access_early_json = NULL WHERE id = ?",
            (client_id,),
        )
    if early is None:
        return False
    await _log(
        db,
        "access_early_ended",
        client_id=client_id,
        category="change",
        event_kind=None,
        until=early.get("until"),
        by=admin_actor(actor),
        detail={"request_id": early.get("request_id")},
    )
    enforcement.notify_change("access")
    await access.check_transitions(db, only=[client_id], by="admin")
    await config_changed(client_id)
    await admin_changed([client_id])
    return True


# ── expiry (called by the access planner) ───────────────────────────────────


async def tick(db: Database, ha_client=None) -> datetime | None:
    """Expire open requests, cancel the ones that became pointless (access
    opened by itself, rule off, device gone), delete ended early accesses.
    Returns the next expiry."""
    from services import access
    from services.profiles import store as pstore

    now = utcnow()
    nxt: datetime | None = None
    # Ended early accesses: the planner closes the device; drop the value.
    for c in await db.fetchall(
        "SELECT id, access_early_json FROM paired_clients WHERE access_early_json IS NOT NULL"
    ):
        early = access.early_of(c)
        if early is None or (parse(early["until"]) or now) <= now:
            async with db.transaction():
                await db.execute(
                    "UPDATE paired_clients SET access_early_json = NULL WHERE id = ? "
                    "AND access_early_json = ?",
                    (c["id"], c["access_early_json"]),
                )
    rows = await db.fetchall("SELECT * FROM access_requests WHERE status = 'open'")
    for row in rows:
        end = parse(row["expires_at"]) or now
        status = None
        if end <= now:
            status = "expired"
        else:
            resolved = await pstore.resolve_client(db, row["client_id"])
            if not resolved:
                status = "cancelled"
            else:
                client, prof, rules = resolved
                state = access.state_of(client, prof, rules, now=now)
                if (
                    state["open"]
                    or (rules.get("access_requests") or {}).get("value") is not True
                    or not client.get("is_active")
                ):
                    status = "cancelled"
        if status is None:
            nxt = end if nxt is None or end < nxt else nxt
            continue
        async with db.transaction():
            closed = await _close(db, row["id"], status, None)
        if not closed:
            continue
        row = await request_row(db, row["id"]) or row
        if status == "expired":
            await _log(
                db,
                "access_request_expired",
                client_id=row["client_id"],
                category="activity",
                event_kind="access_request_expired",
                detail={"request_id": row["id"]},
            )
        else:
            await _log(
                db,
                "access_request_cancelled",
                client_id=row["client_id"],
                category="activity",
                event_kind=None,
                detail={"request_id": row["id"], "by": "hub"},
            )
        await _after_close(db, ha_client, row)
    # Keep the table small: closed requests older than the history.
    cutoff = iso(now - timedelta(days=HISTORY_DAYS * 5))
    async with db.transaction():
        await db.execute(
            "DELETE FROM access_requests WHERE status != 'open' AND created_at < ?",
            (cutoff,),
        )
    return nxt


# ── messages ────────────────────────────────────────────────────────────────


async def recipient_of(db: Database, client_id: str, route: str) -> Recipient | None:
    """The device as a possible recipient (member person with access); away
    only with effective remote access."""
    from services import access
    from services.profiles import store as pstore

    resolved = await pstore.resolve_client(db, client_id)
    if not resolved:
        return None
    c, p, rules = resolved
    if (
        not c.get("is_active")
        or c.get("pending_deletion_at")
        or c.get("access") != "member"
        or p["child_protection"]
        or _kind(p) != "person"
        or not access.state_of(c, p, rules)["open"]
    ):
        return None
    remote = rules.get("remote_access", {}).get("value") is True
    if route != "home" and not remote:
        return None
    return Recipient(
        client_id=client_id,
        name=c.get("name") or "",
        owner_label=c.get("owner_label"),
        role="setup",
        remote=remote,
    )


async def open_for(db: Database, recipient: Recipient) -> list[dict]:
    rows = await db.fetchall(
        "SELECT * FROM access_requests WHERE status = 'open' ORDER BY created_at"
    )
    out = []
    for row in rows:
        if (parse(row["expires_at"]) or utcnow()) <= utcnow():
            continue
        if recipient.client_id in {
            r.client_id for r in await _recipients_for_row(db, row)
        }:
            out.append(await view(db, row, viewer=recipient))
    return out[:50]


async def handle_message(
    db: Database,
    ha_client,
    client_id: str,
    message: dict[str, Any],
    route: str = "home",
) -> list[dict[str, Any]]:
    """One early access message of a device (hub WebSocket or relay)."""
    msg_type = message.get("type")
    try:
        _rate(client_id, "msg")
        if msg_type == "access_request":
            row = await create(db, ha_client, client_id, message, route)
            nxt = await next_allowed_at(db, client_id)
            return [
                {
                    "type": "access_request_state",
                    "request": await view(db, row, own=True),
                    "next_allowed_at": iso(nxt),
                }
            ]
        if msg_type == "access_request_cancel":
            if route != "home":
                raise AccessRequestError("home_only")
            row = await cancel(db, ha_client, client_id, message.get("request_id"))
            nxt = await next_allowed_at(db, client_id)
            return [
                {
                    "type": "access_request_state",
                    "request": await view(db, row, own=True),
                    "next_allowed_at": iso(nxt),
                }
            ]
        recipient = await recipient_of(db, client_id, route)
        if msg_type == "access_requests_get":
            items = await open_for(db, recipient) if recipient else []
            return [{"type": "access_requests", "requests": items}]
        if msg_type == "access_request_answer":
            if recipient is None:
                raise AccessRequestError("not_permitted")
            _rate(client_id, "answer")
            row = await answer(
                db,
                ha_client,
                message.get("request_id"),
                message.get("answer"),
                message.get("duration"),
                message.get("message"),
                recipient=recipient,
                via=route,
            )
            return [
                {
                    "type": "access_request_update",
                    "request": await view(db, row, can_answer=False),
                }
            ]
        raise AccessRequestError("invalid_request")
    except AccessRequestError as e:
        return [e.message()]


# ── admin views ─────────────────────────────────────────────────────────────


async def admin_overview(db: Database) -> dict[str, Any]:
    from services import access

    since = iso(utcnow() - timedelta(days=HISTORY_DAYS))
    rows = await db.fetchall(
        "SELECT * FROM access_requests WHERE status = 'open' OR created_at > ? "
        "ORDER BY created_at DESC LIMIT 100",
        (since,),
    )
    early = []
    for c in await db.fetchall(
        "SELECT id, name, owner_label, access_early_json FROM paired_clients "
        "WHERE access_early_json IS NOT NULL AND is_active = 1"
    ):
        running = access.running_early(c, utcnow())
        if running:
            item: dict[str, Any] = {
                "client_id": c["id"],
                "device_name": c["name"],
                "early": running,
            }
            if c.get("owner_label"):
                item["owner_label"] = c["owner_label"]
            early.append(item)
    return {
        "requests": [await view(db, r, can_answer=r["status"] == "open") for r in rows],
        "early": early,
    }


# ── Home Assistant notifications ────────────────────────────────────────────

# request id -> notify services that got it (to clear it again)
_sent: dict[str, set[str]] = {}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def tag(request_id: str) -> str:
    return f"{TAG_PREFIX}{request_id}"


async def _lang(db: Database) -> str:
    from services.profiles import store as pstore

    try:
        return "de" if (await pstore.language(db)) == "de" else "en"
    except Exception:  # noqa: BLE001
        return "de"


async def _call(ha_client, target: str, payload: dict[str, Any]) -> bool:
    if ha_client is None or not TARGET_RE.match(target):
        return False
    try:
        await ha_client.call_service("notify", target.split(".", 1)[1], payload)
        return True
    except Exception as e:  # noqa: BLE001 - a broken target must not stop the others
        logger.warning("HA notification to %s failed: %s", target, e)
        return False


def _hm(value: datetime | None, tz_name: str | None) -> str:
    from services import kids_rules

    if value is None:
        return ""
    return value.astimezone(kids_rules.zone(tz_name)).strftime("%H:%M")


async def send_notifications(
    db: Database, ha_client, row: dict, recipients: list[Recipient]
) -> int:
    """The request to the HA notify targets linked to the recipients, with
    answer buttons for ``notify.mobile_app_*``."""
    from services import access

    lang = await _lang(db)
    texts = catalog()["recipient_texts"]
    client = (
        await db.fetchone(
            "SELECT name, owner_label FROM paired_clients WHERE id = ?",
            (row["client_id"],),
        )
        or {}
    )
    device = client.get("owner_label") or client.get("name") or ""
    opens_at = parse(row.get("opens_at"))
    tz = access.time_zone()
    body = (
        texts["body"][lang].format(device=device, time=_hm(opens_at, tz))
        if opens_at
        else texts["body_no_start"][lang].format(device=device)
    )
    if row.get("note"):
        body += f" – „{row['note']}“" if lang == "de" else f" – “{row['note']}”"
    offered = durations_for(opens_at, utcnow())
    sent = 0
    for r in recipients:
        for target in r.notify_targets:
            payload: dict[str, Any] = {"title": texts["title"][lang], "message": body}
            if target.startswith("notify.mobile_app_"):
                actions = []
                for a in catalog()["notify_actions"]:
                    if a["answer"] == "yes" and a["duration"] not in offered:
                        continue
                    token = secrets.token_urlsafe(limits()["answer_token_bytes"])
                    async with db.transaction():
                        await db.execute(
                            "INSERT INTO access_answer_tokens (token_hash, request_id, "
                            "action, recipient_client_id, notify_service, expires_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                token_hash(token),
                                row["id"],
                                a["id"],
                                r.client_id,
                                target,
                                row["expires_at"],
                            ),
                        )
                    actions.append(
                        {"action": f"{ACTION_PREFIX}{token}", "title": a["label"][lang]}
                    )
                payload["data"] = {
                    "tag": tag(row["id"]),
                    "group": TAG_PREFIX.rstrip("-"),
                    "actions": actions,
                    "push": {"interruption-level": "time-sensitive"},
                }
            if await _call(ha_client, target, payload):
                _sent.setdefault(row["id"], set()).add(target)
                sent += 1
    return sent


def _spawn_notifications(
    db: Database, ha_client, row: dict, recipients: list[Recipient]
) -> None:
    if ha_client is None or not any(r.notify_targets for r in recipients):
        return
    from services import tasks

    async def run() -> None:
        try:
            await send_notifications(db, ha_client, row, recipients)
        except Exception:  # noqa: BLE001 - notifications are best effort
            logger.exception("early access notification failed")

    tasks.spawn(run(), name="access-request-notify")


async def clear_notifications(ha_client, request_id: str) -> None:
    for target in sorted(_sent.pop(request_id, set())):
        if target.startswith("notify.mobile_app_"):
            await _call(
                ha_client,
                target,
                {"message": "clear_notification", "data": {"tag": tag(request_id)}},
            )


async def answer_by_token(db: Database, ha_client, token: str) -> str:
    """applied / already_answered / expired / invalid / not_allowed."""
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return "invalid"
    async with db.transaction():
        row = await db.fetchone(
            "SELECT * FROM access_answer_tokens WHERE token_hash = ?",
            (token_hash(token),),
        )
        if row:
            await db.execute(
                "DELETE FROM access_answer_tokens WHERE token_hash = ?",
                (row["token_hash"],),
            )
    if row is None:
        return "invalid"
    request = await request_row(db, row["request_id"])
    if request is None:
        return "invalid"
    if request["status"] != "open":
        return "already_answered"
    if (parse(row["expires_at"]) or utcnow()) <= utcnow():
        return "expired"
    action = next(
        (a for a in catalog()["notify_actions"] if a["id"] == row["action"]), None
    )
    if action is None:
        return "invalid"
    recipient = await recipient_of(db, row["recipient_client_id"], "home")
    if recipient is None:
        return "not_allowed"
    try:
        await answer(
            db,
            ha_client,
            request["id"],
            action["answer"],
            action["duration"],
            None,
            recipient=recipient,
            via="ha_notification",
        )
    except AccessRequestError as e:
        return "already_answered" if e.reason == "already_answered" else "not_allowed"
    return "applied"


def on_ha_event(db: Database, ha_client):
    """HA WebSocket listener: answer buttons of early access notifications."""
    from services import tasks

    def listener(event_type: str, event_data: dict) -> None:
        if event_type != ACTION_EVENT:
            return
        action = (event_data or {}).get("action")
        if not isinstance(action, str) or not action.startswith(ACTION_PREFIX):
            return
        token = action[len(ACTION_PREFIX) :]

        async def run() -> None:
            result = await answer_by_token(db, ha_client, token)
            logger.info("early access answer from an HA notification: %s", result)

        tasks.spawn(run(), name="access-request-notify-answer")

    return listener
