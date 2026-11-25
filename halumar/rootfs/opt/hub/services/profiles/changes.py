"""Profile writes with preview, audit and side effects.

Every write of the profile model goes through :func:`run`: the effective
rules of all devices are computed before and after the write (resolver), a
dry run rolls the transaction back and answers the difference
(``profile-preview.response.json``), a real write applies the side effects:
``client_config_updated`` to the changed devices, HA token rotation when a
device stops holding a key (``resolver.holds_key``), ``update_client_identity``
when remote access changes, a quick-access sensor resync and an admin
broadcast.
Remote PINs never appear in a preview or the audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from models.database import Database
from services.profiles import catalog as cat
from services.profiles import resolver as res
from services.profiles import rows, store

logger = logging.getLogger("hub.profiles")


class _Rollback(Exception):
    pass


@dataclass
class Change:
    """Result of a write: what changed per device (resolver before/after)."""

    devices: list[dict] = field(default_factory=list)
    loosened: set[str] = field(default_factory=set)
    changed_ids: list[str] = field(default_factory=list)
    ha_key: dict[str, str] = field(default_factory=dict)
    remote: dict[str, str] = field(default_factory=dict)
    profile_ids: set[str] = field(default_factory=set)
    # Watches of phones with parental controls (protocol.md "A child's
    # watch"): re-scoped by this write (rotate the adult token) or no longer
    # allowed (removed: the phone's effective rule watch is off); watches of
    # phones on Hub only without remote access (removed, reason hub_only).
    watches_rescoped: list[str] = field(default_factory=list)
    watches_removed: list[str] = field(default_factory=list)
    # Why each removed watch goes ($defs/watch_removed_reason) and its name.
    removed_reasons: dict[str, str] = field(default_factory=dict)
    removed_names: dict[str, str] = field(default_factory=dict)
    # Watches whose phone just left parental controls: kept, they follow
    # their phone (limited_by_phone) and get a new token.
    watches_left: list[str] = field(default_factory=list)
    # Devices the write does not change whose pair (hub_remote_now,
    # hub_remote_at) it alters: listed in the preview only.
    preview_only: list[dict] = field(default_factory=list)

    def preview(self) -> dict:
        out = {
            "dry_run": True,
            "affected_count": len(self.devices),
            "loosened_rules": sorted(self.loosened),
            "devices": [*self.devices, *self.preview_only],
        }
        if self.watches_removed:
            # Watches the write would remove (a child's watch only).
            out["watches_removed"] = [
                {
                    "client_id": cid,
                    "name": (self.removed_names.get(cid) or "")[:100],
                    "reason": self.removed_reasons.get(cid, "kids_watch_off"),
                }
                for cid in self.watches_removed
            ]
        return out


def _public(e: dict) -> dict:
    return {
        k: e[k]
        for k in (
            "value",
            "source",
            "reason",
            "locked",
            "fixed",
            "user_choice",
            "expires_at",
            "then",
        )
    }


async def _snapshot(
    db: Database, now: str | None = None, *, schedule: bool = False
) -> dict[str, dict]:
    """State of every device; ``schedule``: also when it is or becomes Hub
    only with remote access (``store.hub_remote_schedule``)."""
    out = {}
    for cid, item in (await store.resolve_all(db, now=now)).items():
        out[cid] = {
            "name": item["client"]["name"],
            "profile_id": item["client"]["profile_id"],
            "child": bool(item["profile"]["child_protection"]),
            "rules": item["rules"],
            "holds_key": res.holds_key(item["rules"]),
            "overrides": {o["rule_key"] for o in item["overrides"]},
            "active": bool(item["client"].get("is_active"))
            and not item["client"].get("pending_deletion_at"),
            "carrier": store.carrier_of(item["client"]),
            # The PIN that opens the settings (client_config.settings_pin_*).
            "pin": (store.effective_pin(item["client"], item["profile"]) or ())[:2]
            if item["rules"]["app_settings"]["value"] == "pin"
            else (),
        }
        if schedule:
            # Always at the real moment, also for a "before" resolved earlier:
            # before and after compare at the same time.
            out[cid]["hub_remote"] = store.hub_remote_schedule(
                item["client"], item["profile"], item["overrides"], store.now_iso()
            )
    return out


def _diff(before: dict[str, dict], after: dict[str, dict]) -> Change:
    change = Change()
    for cid, a in after.items():
        b = before.get(cid)
        if b is None:
            continue
        changes = []
        for key, e_after in a["rules"].items():
            e_before = b["rules"].get(key)
            if e_before is None:
                continue
            # Everything client_config.rules carries (and config_hash covers).
            if res.json_equal(_public(e_before), _public(e_after)):
                continue
            loosens = cat.loosens(key, e_before["value"], e_after["value"])
            if loosens:
                change.loosened.add(key)
            changes.append(
                {
                    "rule": key,
                    "before": _public(e_before),
                    "after": _public(e_after),
                    "loosens": loosens,
                }
            )
        removed = sorted(b["overrides"] - a["overrides"])
        rb = b["rules"]["remote_access"]["value"] is True
        ra = a["rules"]["remote_access"]["value"] is True
        # The app holds an HA key unless it is on Hub only without remote
        # access (resolver.holds_key).
        key_before = b["holds_key"]
        key_after = a["holds_key"]
        ha_key = "unchanged"
        if key_before and not key_after:
            ha_key = "removed"
        elif key_after and not key_before:
            ha_key = "restored"
        remote = "unchanged" if rb == ra else ("enabled" if ra else "disabled")
        moved = a["profile_id"] != b["profile_id"]
        hub_now, hub_at = a.get("hub_remote", (False, None))
        if (
            changes
            or removed
            or ha_key != "unchanged"
            or remote != "unchanged"
            or moved
        ):
            change.devices.append(
                {
                    "client_id": cid,
                    "name": a["name"],
                    "changes": changes,
                    "overrides_removed": removed,
                    "ha_key": ha_key,
                    "remote_access": remote,
                    "hub_remote_now": hub_now,
                    "hub_remote_at": hub_at,
                }
            )
            change.changed_ids.append(cid)
            change.ha_key[cid] = ha_key
            change.remote[cid] = remote
            change.profile_ids.update({a["profile_id"], b["profile_id"]})
        else:
            if a["pin"] != b["pin"]:
                # A new settings PIN is pushed, but never shown in a preview.
                change.changed_ids.append(cid)
                change.profile_ids.update({a["profile_id"], b["profile_id"]})
            if (hub_now, hub_at) != b.get("hub_remote", (False, None)):
                # The change alters when the device is on Hub only with
                # remote access, without changing any of its rules now.
                change.preview_only.append(
                    {
                        "client_id": cid,
                        "name": a["name"],
                        "changes": [],
                        "overrides_removed": [],
                        "ha_key": "unchanged",
                        "remote_access": "unchanged",
                        "hub_remote_now": hub_now,
                        "hub_remote_at": hub_at,
                    }
                )
    _follow_phones(change, before, after)
    return change


def _follow_phones(
    change: Change, before: dict[str, dict], after: dict[str, dict]
) -> None:
    """Watches whose phone has parental controls after the write: removed
    when the phone's effective rule ``watch`` is off (no ``kids_watch``, or
    no remote access), re-scoped when they were not a child's watch before
    (their adult token is rotated). An adult's watch is never removed by a
    write: it follows its phone (``store.resolve_device``); one whose phone
    just left parental controls gets a new token. A watch whose phone is on
    Hub only without remote access goes as well (reason ``hub_only``: the
    phone holds no key, its rule ``watch`` is off through the boundary)."""
    from services import watch_access

    for cid, a in after.items():
        phone_id = a.get("carrier") or ""
        phone = after.get(phone_id)
        if not a["active"] or phone is None:
            continue
        if not phone["child"]:
            if watch_access.hub_only_removes_watch(phone["rules"].get("watch") or {}):
                change.watches_removed.append(cid)
                change.removed_names[cid] = a["name"]
                change.removed_reasons[cid] = "hub_only"
            elif (before.get(cid) or {}).get("child"):
                change.watches_left.append(cid)
            continue
        if (phone["rules"].get("watch") or {}).get("value") is not True:
            change.watches_removed.append(cid)
            change.removed_names[cid] = a["name"]
            kids_watch = phone["rules"].get("kids_watch") or {}
            if kids_watch.get("reason") == "requires_remote_access":
                reason = "remote_access_off"
            elif cid in before and not before[cid]["child"]:
                reason = "parental_controls"
            else:
                reason = "kids_watch_off"
            change.removed_reasons[cid] = reason
        elif cid in before and not before[cid]["child"]:
            change.watches_rescoped.append(cid)


async def _mark_key_lost(
    db: Database, before: dict[str, dict], after: dict[str, dict]
) -> None:
    """``paired_clients.key_lost_at`` for every device that stops holding a
    key (``resolver.holds_key`` from true to false), in the write's own
    transaction: the HA key catch-up (``secure_keyless_tokens``) then finds
    it whatever way it was lost and even after a crash before the
    effects."""
    now = store.now_iso()
    for cid, a in after.items():
        b = before.get(cid)
        if b is not None and b["holds_key"] and not a["holds_key"]:
            await db.execute(
                "UPDATE paired_clients SET key_lost_at = ? WHERE id = ?",
                (now, cid),
            )


async def run(
    db: Database,
    write: Callable[[], Awaitable[Any]],
    *,
    dry_run: bool,
    before_now: str | None = None,
) -> tuple[Any, Change]:
    """Run ``write`` in one transaction; dry run = roll back, return the diff.

    ``before_now``: the moment the state before the write is resolved at
    (default: now). The end of temporary adjustments passes the moment just
    before the earliest end: on the clock after the end the resolver ignores
    an ended adjustment already, and the diff would show nothing to take
    away (no key withdrawal, no identity update)."""
    result = None
    if before_now is None:
        from services.profiles import expiry_task

        if dry_run:
            # A preview writes nothing: it compares with the state just
            # before the earliest end the planner has not processed yet.
            before_now = await expiry_task.before_unprocessed_end(db)
        else:
            # Ended adjustments the planner has not processed yet go first:
            # else this write would resolve them as gone already and take
            # nothing away.
            await expiry_task.expire_due_isolated(db)
    before = await _snapshot(db, before_now, schedule=True)
    after: dict[str, dict] = {}
    try:
        async with db.transaction():
            result = await write()
            after = await _snapshot(db, schedule=True)
            if dry_run:
                raise _Rollback()
            await _mark_key_lost(db, before, after)
    except _Rollback:
        pass
    return result, _diff(before, after)


async def audit(
    db: Database,
    action: str,
    *,
    actor: str | None,
    profile_id: str | None = None,
    client_id: str | None = None,
    rule_key: str | None = None,
    before: Any = None,
    after: Any = None,
    note: str | None = None,
    stored: bool = False,
) -> None:
    """Persistent profile audit (before/after) plus the admin audit view.

    ``stored``: the caller wrote the audit row in its own transaction
    (expiry, together with the delete); only the views are added here.
    """
    from services.audit_log import log_admin_action

    if not stored:
        async with db.transaction():
            await rows.audit(
                db,
                action,
                now=store.now_iso(),
                actor=actor,
                profile_id=profile_id,
                client_id=client_id,
                rule_key=rule_key,
                before=before,
                after=after,
                note=note,
            )
    parts = [
        f"{k}={v}"
        for k, v in (
            ("profile_id", profile_id),
            ("client_id", client_id),
            ("rule", rule_key),
        )
        if v
    ]
    log_admin_action(action, " ".join(parts) or None, None)
    from services import event_log

    await event_log.audit_change(
        db,
        action,
        actor=actor,
        profile_id=profile_id,
        client_id=client_id,
        rule_key=rule_key,
        before=before,
        after=after,
        note=note,
    )


async def apply_effects(
    db: Database, change: Change, touched: list[str] | tuple[str, ...] = ()
) -> None:
    """Tell devices, integration and admin UIs what the write changed.

    ``touched`` = devices the write changed without changing their effective
    rules (own area, remote PIN): only the admin UIs hear about them.
    """
    from api import ws as api_ws
    from services import device_provisioning, enforcement, kids_rules, quick_access

    if enforcement.path_changed(change.devices):
        # The device user's HA group follows the snapshot: send it now instead
        # of after the debounce, before the devices hear of the change.
        await enforcement.push_now("profiles_changed")
    for cid in change.changed_ids:
        if change.ha_key.get(cid) == "removed":
            kids_rules.counters.clear_client(cid)
            row = await db.fetchone(
                "SELECT id, is_active, pending_deletion_at, ha_token_status "
                "FROM paired_clients WHERE id = ?",
                (cid,),
            )
            if (
                device_provisioning.is_provisionable(row)
                and row.get("ha_token_status") == "active"
            ):
                # The token the app knew must stop working (the hub keeps the new one).
                await device_provisioning.withdraw_device_key(
                    db, cid, send=api_ws.get_integration_push_connected()
                )
        if change.remote.get(cid) in ("enabled", "disabled"):
            await device_provisioning.send_identity_update(db, cid)
        try:
            await api_ws.send_to_client(
                cid, {"type": "client_config_updated", "data": {}}
            )
        except Exception as e:  # noqa: BLE001 - best effort
            logger.debug("client_config_updated to %s failed: %s", cid, e)
    await end_lost_invites(db, change.changed_ids)
    if change.watches_removed or change.watches_rescoped or change.watches_left:
        from services import watch_access

        await watch_access.follow_phones(
            db,
            change.watches_rescoped,
            change.watches_removed,
            left=change.watches_left,
            reasons=change.removed_reasons,
        )
    await admin_broadcast(
        sorted(p for p in change.profile_ids if p),
        [*change.changed_ids, *touched],
    )
    quick_access.notify_change("profiles_changed")
    enforcement.notify_change("profiles_changed")


async def end_lost_invites(db: Database, client_ids: list[str]) -> None:
    """An open guest invitation ends when its device loses the kind guest of
    the rule ``setup_devices`` (result ``revoked``)."""
    from api.ws import guest_invites as guest_invites_ws
    from services import guest_invites

    for cid in client_ids:
        if guest_invites.invite_of(cid) and not await guest_invites.may_invite(db, cid):
            try:
                await guest_invites_ws.end_invites_of(cid)
            except Exception as e:  # noqa: BLE001 - best effort
                logger.debug("ending the invitation of %s failed: %s", cid, e)


async def admin_broadcast(
    profile_ids: list[str], client_ids: list[str] | None = None
) -> None:
    """``profiles_changed`` to admin sessions and the integration.

    Profiles (name, rules, new, deleted, defaults) or the profile state of
    devices (``client_ids``: assignment, adjustments, own area, expiry)
    changed. Devices get their own ``client_config_updated``.
    """
    from api import ws as api_ws
    from api.ws.broadcast import EventAudience
    from services import access

    access.wake()  # access may have changed with the rules
    try:
        await api_ws.broadcast(
            {
                "type": "profiles_changed",
                "data": {
                    "profile_ids": sorted(set(profile_ids)),
                    "client_ids": sorted(set(client_ids or [])),
                },
            },
            audience=EventAudience.ADMIN,
        )
    except Exception as e:  # noqa: BLE001 - best effort
        logger.debug("profile broadcast failed: %s", e)
