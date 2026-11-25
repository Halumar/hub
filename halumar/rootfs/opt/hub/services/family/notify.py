"""Home Assistant notifications of the family manager.

The hub calls ``notify.<service>`` itself (Supervisor API) for the targets the
admin linked to a family manager (``family_scopes.notify_targets``):

* a child's request – for ``notify.mobile_app_*`` with answer buttons; every
  button carries its own single-use token (16 random bytes, only the SHA-256
  is stored) in the action id ``<NS>_FAMILY_<token>``; other services get the
  text only;
* the automatic child notifications (``data/family.json`` ``notify_defaults``)
  the family manager has not switched off; repeating kinds are bundled (one
  per child, kind and target in ``BUNDLE_SECONDS``).

A tap fires ``mobile_app_notification_action`` in HA; :func:`on_ha_event`
receives it over the hub's own HA WebSocket connection and answers the
request (route ``ha_notification``). When a request is closed the
notification is cleared by its tag. No push of our own: without HA targets a
request only reaches open apps and the admin UI.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from typing import Any

from models.database import Database
from namespace import FAMILY_ACTION_PREFIX, FAMILY_NOTIFICATION_TAG_PREFIX
from services.family import data
from services.family import store as fstore

logger = logging.getLogger("hub.family.notify")

TARGET_RE = re.compile(r"^notify\.[a-z0-9_]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
ACTION_EVENT = "mobile_app_notification_action"
BUNDLE_SECONDS = 600.0

# request id -> notify services that got it (to clear it again)
_sent: dict[str, set[str]] = {}
_bundle: dict[tuple, float] = {}

TEXTS = {
    "de": {
        "request_title": "Anfrage von {child}",
        "only_admin": "Das kann nur der Admin freigeben.",
        "answered_yes": "{by} hat {child} bis {until} freigegeben.",
        "answered_no": "{by} hat Nein gesagt ({child}).",
        "expired": "Anfrage von {child} unbeantwortet.",
        "bedtime": "{child} hat in der Schlafenszeit versucht, etwas einzuschalten.",
        "pause": "{child} hat eine Pause wegen zu vieler Aktionen.",
        "control": "{child} hat {entity} geschaltet.",
        "change": "{by} hat bei {child} „{rule}“ geändert.",
    },
    "en": {
        "request_title": "Request from {child}",
        "only_admin": "Only the admin can allow this.",
        "answered_yes": "{by} allowed {child} until {until}.",
        "answered_no": "{by} said no ({child}).",
        "expired": "Request from {child} unanswered.",
        "bedtime": "{child} tried to switch something on during bedtime.",
        "pause": "{child} has a break after too many actions.",
        "control": "{child} switched {entity}.",
        "change": "{by} changed “{rule}” for {child}.",
    },
}


def reset() -> None:
    _sent.clear()
    _bundle.clear()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def tag(request_id: str) -> str:
    return f"{FAMILY_NOTIFICATION_TAG_PREFIX}{request_id}"


async def language(db: Database) -> str:
    from services.profiles import store as pstore

    lang = await pstore.language(db)
    return lang if lang in TEXTS else "de"


async def _call(ha_client, target: str, payload: dict[str, Any]) -> bool:
    if ha_client is None or not TARGET_RE.match(target):
        return False
    try:
        await ha_client.call_service("notify", target.split(".", 1)[1], payload)
        return True
    except Exception as e:  # noqa: BLE001 - a broken target must not stop the others
        logger.warning("HA notification to %s failed: %s", target, e)
        return False


def _is_mobile_app(target: str) -> bool:
    return target.startswith("notify.mobile_app_")


async def _local_time(ha_client, value: str | None) -> str:
    from services import kids_mode, kids_rules

    dt = fstore.parse(value)
    if dt is None:
        return ""
    tz = await kids_mode.hub_time_zone(ha_client)
    return dt.astimezone(kids_rules.zone(tz)).strftime("%H:%M")


async def send_request(db: Database, ha_client, row: dict) -> int:
    """Notify the targets of every family manager of the child; returns the
    number of notifications sent."""
    from services.family import service

    managers = await service.managers_of_child(db, row["client_id"])
    if not managers:
        return 0
    lang = await language(db)
    texts = TEXTS[lang]
    child = await service.child_of(db, ha_client, row["client_id"])
    child_row = await db.fetchone(
        "SELECT name, owner_label FROM paired_clients WHERE id = ?", (row["client_id"],)
    )
    child_name = service.child_display(child_row or {})
    category = data.categories()[row["category"]]
    entity = (
        await service.entity_name(ha_client, row.get("entity_id"))
        or row.get("entity_id")
        or ""
    )
    body = f"{child_name} " + data.label(category, "manager_label", lang).replace(
        "{entity}", entity
    )
    if row.get("note"):
        body += f" – „{row['note']}“" if lang == "de" else f" – “{row['note']}”"
    sent = 0
    for m in managers:
        targets = [t for t in m.notify_targets if TARGET_RE.match(t)]
        if not targets:
            continue
        approve = await service.can_approve(db, ha_client, m, row, child)
        for target in targets:
            payload: dict[str, Any] = {
                "title": texts["request_title"].format(child=child_name),
                "message": body if approve else f"{body}\n{texts['only_admin']}",
            }
            if _is_mobile_app(target):
                actions = []
                for a in data.notify_actions():
                    if a["answer"] == "yes" and (
                        not approve or a["preset"] not in category["presets"]
                    ):
                        continue
                    token = secrets.token_urlsafe(data.limits()["answer_token_bytes"])
                    async with db.transaction():
                        await fstore.insert_token(
                            db,
                            token_hash(token),
                            row["id"],
                            a["id"],
                            m.client_id,
                            target,
                            row["expires_at"],
                        )
                    actions.append(
                        {
                            "action": f"{FAMILY_ACTION_PREFIX}{token}",
                            "title": data.label(a, "label", lang),
                        }
                    )
                payload["data"] = {
                    "tag": tag(row["id"]),
                    "group": FAMILY_NOTIFICATION_TAG_PREFIX.rstrip("-"),
                    "actions": actions,
                    "push": {"interruption-level": "time-sensitive"},
                }
            if await _call(ha_client, target, payload):
                _sent.setdefault(row["id"], set()).add(target)
                sent += 1
    return sent


async def clear_request(ha_client, request_id: str) -> None:
    for target in sorted(_sent.pop(request_id, set())):
        if _is_mobile_app(target):
            await _call(
                ha_client,
                target,
                {"message": "clear_notification", "data": {"tag": tag(request_id)}},
            )


async def send_simple(
    db: Database,
    ha_client,
    kind: str,
    child_id: str,
    message: str,
    *,
    bundle: bool,
    skip_manager: str | None = None,
) -> int:
    from services.family import service

    sent = 0
    for m in await service.managers_of_child(db, child_id):
        if m.client_id == skip_manager or not m.notify.get(kind):
            continue
        for target in m.notify_targets:
            if not TARGET_RE.match(target):
                continue
            if bundle:
                key = (child_id, kind, target)
                now = time.monotonic()
                if now - _bundle.get(key, -BUNDLE_SECONDS) < BUNDLE_SECONDS:
                    continue
                _bundle[key] = now
            if await _call(ha_client, target, {"message": message}):
                sent += 1
    return sent


def make_sink(db: Database, ha_client):
    """Event sink (services.family.events) for the automatic notifications."""
    from services.profiles import catalog as cat

    async def handle(event: dict[str, Any], extra: dict[str, Any]) -> None:
        kind = event["kind"]
        child_id = event.get("client_id")
        if not child_id:
            return
        lang = await language(db)
        texts = TEXTS[lang]
        child = event.get("owner_label") or event.get("device_name") or ""
        if kind == "kids_release_requested":
            row = await fstore.request_row(db, extra.get("request_id", ""))
            if row:
                await send_request(db, ha_client, row)
        elif kind == "kids_request_answered":
            by = extra.get("by") or {}
            if event.get("action") == "yes":
                text = texts["answered_yes"].format(
                    by=by.get("name", ""),
                    child=child,
                    until=await _local_time(ha_client, event.get("until")),
                )
            else:
                text = texts["answered_no"].format(by=by.get("name", ""), child=child)
            await send_simple(
                db,
                ha_client,
                kind,
                child_id,
                text,
                bundle=False,
                skip_manager=by.get("client_id"),
            )
        elif kind == "kids_request_expired":
            await send_simple(
                db,
                ha_client,
                "kids_request_answered",
                child_id,
                texts["expired"].format(child=child),
                bundle=False,
            )
        elif kind == "kids_bedtime_attempt":
            await send_simple(
                db,
                ha_client,
                kind,
                child_id,
                texts["bedtime"].format(child=child),
                bundle=True,
            )
        elif kind == "kids_pause":
            await send_simple(
                db,
                ha_client,
                kind,
                child_id,
                texts["pause"].format(child=child),
                bundle=True,
            )
        elif kind == "kids_control":
            from services.family import service

            entity = await service.entity_name(
                ha_client, event.get("entity_id")
            ) or event.get("entity_id", "")
            await send_simple(
                db,
                ha_client,
                kind,
                child_id,
                texts["control"].format(child=child, entity=entity),
                bundle=True,
            )
        elif kind == "family_change":
            by = extra.get("by") or {}
            rule = cat.rule(extra.get("rule", "")) or {}
            await send_simple(
                db,
                ha_client,
                kind,
                child_id,
                texts["change"].format(
                    by=by.get("name", ""),
                    child=child,
                    rule=(rule.get("label") or {}).get(lang, extra.get("rule", "")),
                ),
                bundle=False,
                skip_manager=extra.get("manager_id"),
            )

    async def sink(event: dict[str, Any], extra: dict[str, Any]) -> None:
        # HA calls may take seconds: never make the child or the app wait.
        from services import tasks

        async def run() -> None:
            try:
                await handle(event, extra)
            except Exception:  # noqa: BLE001 - notifications are best effort
                logger.exception("family notification failed")

        tasks.spawn(run(), name="family-notify")

    sink.handle = handle  # type: ignore[attr-defined]
    return sink


# ── answers from a notification button ──────────────────────────────────────


async def answer_by_token(
    db: Database,
    ha_client,
    token: str,
    notify_service: str | None = None,
) -> tuple[str, dict | None]:
    """(result, request) with result applied / already_answered / expired /
    invalid / not_allowed (``integration/family-notify-answer.response.json``)."""
    from services.family import service

    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return "invalid", None
    async with db.transaction():
        row = await fstore.take_token(db, token_hash(token))
    if row is None:
        return "invalid", None
    request = await fstore.request_row(db, row["request_id"])
    if request is None:
        return "invalid", None
    if request["status"] != "open":
        return "already_answered", await service.request_view(db, ha_client, request)
    if (fstore.parse(row["expires_at"]) or fstore.utcnow()) <= fstore.utcnow():
        return "expired", await service.request_view(db, ha_client, request)
    action = next((a for a in data.notify_actions() if a["id"] == row["action"]), None)
    if action is None:
        return "invalid", None
    try:
        manager = await service.manager_of(db, row["manager_client_id"] or "")
    except service.FamilyError:
        return "not_allowed", None
    if (
        notify_service
        and row.get("notify_service")
        and notify_service != row["notify_service"]
    ):
        return "invalid", None
    try:
        view = await service.answer_request(
            db,
            ha_client,
            request["id"],
            action["answer"],
            action["preset"],
            None,
            manager,
            "ha_notification",
        )
    except service.FamilyError as e:
        if e.reason == "already_answered":
            return "already_answered", e.request
        return "not_allowed", None
    return "applied", view


def on_ha_event(db: Database, ha_client):
    """HA WebSocket listener: answer buttons of family notifications."""
    from services import tasks

    def listener(event_type: str, event_data: dict) -> None:
        if event_type != ACTION_EVENT:
            return
        action = (event_data or {}).get("action")
        if not isinstance(action, str) or not action.startswith(FAMILY_ACTION_PREFIX):
            return
        token = action[len(FAMILY_ACTION_PREFIX) :]

        async def run() -> None:
            result, _ = await answer_by_token(db, ha_client, token)
            logger.info("family answer from an HA notification: %s", result)

        tasks.spawn(run(), name="family-notify-answer")

    return listener
