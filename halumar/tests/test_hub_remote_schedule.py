"""When a device is, or later becomes, Hub only with remote access.

On Hub only with remote access a device holds a read-only Home Assistant
key. The admin UI warns before it hands one out, also when it happens only
at a later scheduled end of another rule. The hub computes it
(``store.hub_remote_schedule``) and reports it in every preview
(``devices[].hub_remote_now`` / ``hub_remote_at``), on the effective rules
of a device and for a device paired with a profile
(``hub_remote_pairing``). The tests also cover the expiry pre-step of
writes: a dry run writes nothing, an end happens once, a failing end does
not block other writes.
"""

# The fixtures of test_hub_path are imported by name.
# ruff: noqa: F811

import asyncio
from datetime import datetime, timedelta, timezone

from api import profiles as profiles_api
from services.profiles import catalog, expiry_task, store
from services.profiles import rows as profile_rows

from .test_hub_expiry import _clock, _hub_profile, _key_lost, _old_token
from .test_hub_path import (  # noqa: F401 - fixtures
    _clean_state,
    _device,
    dp,
    events,
    pchanges,
)
from models.database import Database


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


async def _profile(db, **rules):
    body = catalog.materialize("adult")
    body["rules"].update(rules)
    async with db.transaction():
        return await profile_rows.insert_profile(
            db,
            name=f"P {sorted(rules.items())}",
            body=body,
            now=Database.now(),
            actor=None,
        )


async def _override(db, client_id, rule, value, end=None, *, dry_run):
    async def _write():
        await store.put_override(
            db,
            client_id,
            rule,
            value=value,
            user_choice=False,
            expires_at=end.isoformat() if end else None,
            note=None,
            actor="test",
        )

    _, change = await pchanges.run(db, _write, dry_run=dry_run)
    if not dry_run:
        await pchanges.apply_effects(db, change)
    return change


def _entry(change, client_id):
    return next(d for d in change.preview()["devices"] if d["client_id"] == client_id)


async def test_remote_on_while_a_temporary_checked_runs(db, events):
    """Hub only profile, temporary Checked until T; the admin switches remote
    access on for good: right after the change not Hub only (Checked), at T
    Hub only with remote access."""
    phone = await _device(db, profile_id=await _hub_profile(db))
    end = _now() + timedelta(hours=2)
    await _override(db, phone, "control_path", "checked", end, dry_run=False)
    change = await _override(db, phone, "remote_access", True, dry_run=True)
    entry = _entry(change, phone)
    assert entry["hub_remote_now"] is False
    assert entry["hub_remote_at"] == store.res.fmt_ts(end)


async def test_level_to_hub_only_while_remote_access_is_off_until_t(db, events):
    """Profile with remote access; the device has remote access off until T;
    the admin sets the level to Hub only for good."""
    prof = await _profile(db, control_path="checked", remote_access=True)
    phone = await _device(db, profile_id=prof)
    end = _now() + timedelta(hours=3)
    await _override(db, phone, "remote_access", False, end, dry_run=False)
    change = await _override(db, phone, "control_path", "hub", dry_run=True)
    entry = _entry(change, phone)
    assert entry["ha_key"] == "removed"
    assert entry["hub_remote_now"] is False
    assert entry["hub_remote_at"] == store.res.fmt_ts(end)


async def test_hub_only_with_remote_access_right_after_the_change(db, events):
    phone = await _device(db, profile_id=await _hub_profile(db))
    change = await _override(db, phone, "remote_access", True, dry_run=True)
    entry = _entry(change, phone)
    assert (entry["hub_remote_now"], entry["hub_remote_at"]) == (True, None)


async def test_an_untouched_device_with_a_later_end_is_listed(db, events):
    """A write that changes none of a device's rules but alters when it is on
    Hub only with remote access lists it: a guest with remote access off
    until T whose access ended before T is extended past T."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    end = _now() + timedelta(hours=2)
    guest = await _device(
        db,
        guest=True,
        expires_at=(end - timedelta(hours=1)).isoformat(),
        profile_id=prof,
    )
    await _override(db, guest, "remote_access", False, end, dry_run=False)

    async def _extend():
        await db.execute(
            "UPDATE paired_clients SET expires_at = ? WHERE id = ?",
            ((end + timedelta(days=1)).isoformat(), guest),
        )

    _, change = await pchanges.run(db, _extend, dry_run=True)
    preview = change.preview()
    entry = _entry(change, guest)
    assert entry["changes"] == [] and entry["ha_key"] == "unchanged"
    assert (entry["hub_remote_now"], entry["hub_remote_at"]) == (
        False,
        store.res.fmt_ts(end),
    )
    assert preview["affected_count"] == 0


async def test_an_unrelated_change_lists_no_device(db, events):
    """Devices that are, or will be, on Hub only with remote access are not
    listed for a change that does not alter that (a rename, another rule
    elsewhere)."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    soon = await _device(db, profile_id=prof)
    end = _now() + timedelta(hours=1)
    await _override(db, soon, "control_path", "checked", end, dry_run=False)
    already = await _device(db, profile_id=prof)
    other = await _device(db, profile="adult")

    async def _rename():
        await db.execute("UPDATE profiles SET name = 'Umbenannt' WHERE id = ?", (prof,))

    _, change = await pchanges.run(db, _rename, dry_run=True)
    assert change.preview()["devices"] == []
    change = await _override(db, other, "remote_access", False, dry_run=True)
    listed = [d["client_id"] for d in change.preview()["devices"]]
    assert listed == [other] and soon not in listed and already not in listed


async def test_bulk_and_assign_with_kept_adjustments(db, events):
    """Assigning a Hub only profile with remote access while keeping a
    temporary Checked: Hub only with remote access at its end."""
    target = await _profile(db, control_path="hub", remote_access=True)
    phones = [await _device(db, profile="adult", remote=False) for _ in range(2)]
    end = _now() + timedelta(hours=1)
    for phone in phones:
        await _override(db, phone, "control_path", "checked", end, dry_run=False)
    async with db.transaction():
        await db.execute(
            "DELETE FROM device_overrides WHERE rule_key = 'remote_access'"
        )

    async def _assign():
        for phone in phones:
            await store.assign(db, phone, target, discard_overrides=False)

    _, change = await pchanges.run(db, _assign, dry_run=True)
    for phone in phones:
        entry = _entry(change, phone)
        assert entry["hub_remote_now"] is False
        assert entry["hub_remote_at"] == store.res.fmt_ts(end)


async def test_overlapping_ends_give_the_first_moment(db, events):
    """Checked until T1 and remote access off until T2 > T1 on a Hub only
    profile with remote access: Hub only with remote access from T2."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    phone = await _device(db, profile_id=prof)
    t1, t2 = _now() + timedelta(hours=1), _now() + timedelta(hours=2)
    await _override(db, phone, "control_path", "checked", t1, dry_run=False)
    await _override(db, phone, "remote_access", False, t2, dry_run=False)
    now, at = store.hub_remote_schedule(
        await store.client_row(db, phone),
        await store.profile_row(db, prof),
        await store.overrides_of(db, phone),
    )
    assert (now, at) == (False, store.res.fmt_ts(t2))


async def test_a_guest_that_expires_first_never_gets_there(db, events):
    """A guest with remote access off until after its expiry: null."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    expires = _now() + timedelta(hours=1)
    guest = await _device(
        db, guest=True, expires_at=expires.isoformat(), profile_id=prof
    )
    await _override(
        db, guest, "remote_access", False, expires + timedelta(hours=1), dry_run=False
    )
    effective = await store.effective_view(db, guest)
    assert (effective["hub_remote_now"], effective["hub_remote_at"]) == (False, None)


async def test_tv_and_watch_never_set_the_fields(db, events):
    prof = await _profile(db, control_path="hub", remote_access=True)
    for platform in ("tvos", "watchos"):
        cid = await _device(db, platform=platform, profile_id=prof)
        effective = await store.effective_view(db, cid)
        assert (effective["hub_remote_now"], effective["hub_remote_at"]) == (
            False,
            None,
        )


async def test_effective_rules_and_pairing_check_name_it(db, events):
    prof = await _profile(db, control_path="hub", remote_access=True)
    phone = await _device(db, profile_id=prof)
    effective = await store.effective_view(db, phone)
    assert (effective["hub_remote_now"], effective["hub_remote_at"]) == (True, None)
    view = await store.profile_view(db, await store.profile_row(db, prof))
    pairing = view["hub_remote_pairing"]
    assert pairing["member"]["ios"] is True and pairing["guest"]["android"] is True
    assert pairing["member"]["tvos"] is False and pairing["member"]["wearos"] is False
    checked = await store.profile_view(
        db, await store.profile_row(db, await _profile(db, control_path="checked"))
    )
    assert not any(checked["hub_remote_pairing"]["member"].values())


# ── a profile without a stored level ──────────────────────────────────────


async def test_a_profile_without_a_stored_level_shows_the_resolvers_fallback(db):
    """Guest kinds and children resolve a missing control_path to hub, and
    the profile and effective views say the same; a person profile keeps the
    catalog default."""
    for suggestion, kind, want in (
        ("guest", "guest", "hub"),
        ("babysitter", "babysitter", "hub"),
        ("child", "child", "hub"),
        ("adult", "person", "checked"),
    ):
        body = catalog.materialize(suggestion)
        body["rules"].pop("control_path")
        async with db.transaction():
            pid = await profile_rows.insert_profile(
                db,
                name=f"Ohne {suggestion}",
                body=body,
                now=Database.now(),
                actor=None,
                kind=kind,
            )
        view = await store.profile_view(db, await store.profile_row(db, pid))
        assert view["rules"]["control_path"] == want, suggestion
        cid = await _device(db, profile_id=pid, guest=kind in ("guest", "babysitter"))
        effective = await store.effective_view(db, cid)
        assert effective["rules"]["control_path"]["profile_value"] == want, suggestion
        assert effective["rules"]["control_path"]["value"] == want, suggestion


# ── the expiry pre-step of writes ─────────────────────────────────────────


async def _ended(db, monkeypatch):
    phone = await _device(db, profile_id=await _hub_profile(db))
    await _old_token(db, phone)
    end = _now() + timedelta(hours=1)
    await _override(db, phone, "control_path", "checked", end, dry_run=False)
    _clock(monkeypatch, end + timedelta(seconds=2))
    return phone


async def _noop():
    return None


async def test_a_dry_run_writes_nothing_and_still_shows_the_loss(
    db, events, monkeypatch
):
    phone = await _ended(db, monkeypatch)
    _, change = await pchanges.run(db, _noop, dry_run=True)
    left = await db.fetchone(
        "SELECT COUNT(*) AS n FROM device_overrides WHERE client_id = ?", (phone,)
    )
    assert left["n"] == 1
    assert not await dp.rotation_pending(db, phone)
    assert change.ha_key.get(phone) == "removed"


async def test_the_planner_and_a_write_end_an_adjustment_once(db, events, monkeypatch):
    phone = await _ended(db, monkeypatch)
    await asyncio.gather(
        expiry_task.expire_due(db), pchanges.run(db, _noop, dry_run=False)
    )
    audits = await db.fetchall(
        "SELECT id FROM profile_audit WHERE client_id = ? AND action = 'override_expired'",
        (phone,),
    )
    assert len(audits) == 1
    assert await dp.rotation_pending(db, phone)
    assert await _key_lost(db, phone)


async def test_a_failing_end_does_not_block_other_writes(db, events, monkeypatch):
    """The end of one device fails: other writes go on, and that device's
    token is rotated (fail closed)."""
    phone = await _ended(db, monkeypatch)
    other = await _device(db, profile="adult")
    original = expiry_task.expire_due

    async def _broken(db_, now=None, client_ids=None):
        if client_ids is None or phone in client_ids:
            raise RuntimeError("cannot end")
        return await original(db_, now, client_ids)

    monkeypatch.setattr(expiry_task, "expire_due", _broken)
    change = await _override(db, other, "remote_access", False, dry_run=False)
    assert other in change.changed_ids
    assert await dp.rotation_pending(db, phone)


async def test_the_rest_api_preview_carries_the_fields(db, events):
    import json
    import types

    phone = await _device(db, profile_id=await _hub_profile(db))
    req = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )
    out = await profiles_api.put_client_override(
        req,
        phone,
        "remote_access",
        profiles_api.DeviceOverride(value=True),
        dry_run=True,
    )
    body = json.loads(out.body)
    entry = next(d for d in body["devices"] if d["client_id"] == phone)
    assert entry["hub_remote_now"] is True and entry["hub_remote_at"] is None


# ── extending a guest ───────────────────────────────────────────────────────


async def _extend(db, guest, hours, *, dry_run):
    from api.pairing import guests as guests_api

    return await guests_api.extend_guest_expiry(
        db, None, guest, hours=hours, dry_run=dry_run
    )


async def test_extending_a_guest_past_its_remote_end_names_the_moment(db, events):
    """Remote access off until +30 h, access ending in 2 h, extended to
    48 h: Hub only with remote access from +30 h."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    now = _now()
    guest = await _device(
        db,
        guest=True,
        expires_at=(now + timedelta(hours=2)).isoformat(),
        profile_id=prof,
    )
    remote_end = now + timedelta(hours=30)
    await _override(db, guest, "remote_access", False, remote_end, dry_run=False)
    before = await db.fetchone(
        "SELECT expires_at FROM paired_clients WHERE id = ?", (guest,)
    )
    preview = await _extend(db, guest, 48, dry_run=True)
    # One format for both answers: the canonical stored form (utc_expiry).
    assert preview["dry_run"] is True and preview["expires_at"].endswith("+00:00")
    assert (preview["hub_remote_now"], preview["hub_remote_at"]) == (
        False,
        store.res.fmt_ts(remote_end),
    )
    after = await db.fetchone(
        "SELECT expires_at FROM paired_clients WHERE id = ?", (guest,)
    )
    assert after == before  # the dry run wrote nothing
    real = await _extend(db, guest, 48, dry_run=False)
    assert (real["hub_remote_now"], real["hub_remote_at"]) == (
        False,
        store.res.fmt_ts(remote_end),
    )
    assert real["expires_at"].endswith("+00:00")


async def test_reopening_an_expired_guest_gives_its_key_back_at_once(
    db, events, monkeypatch
):
    """An expired guest (not swept yet) on Hub only with remote access: the
    extension goes through the common change path, so its config and key
    follow at once."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    guest = await _device(
        db,
        guest=True,
        expires_at=(_now() - timedelta(minutes=1)).isoformat(),
        profile_id=prof,
    )
    from .test_hub_path import FakeHA, _get_client_config

    assert (await _get_client_config(db, guest, FakeHA()))["ha_token"] is None
    events["to"].clear()
    result = await _extend(db, guest, 24, dry_run=False)
    assert (result["hub_remote_now"], result["hub_remote_at"]) == (True, None)
    assert (guest, {"type": "client_config_updated", "data": {}}) in events["to"]
    assert (await _get_client_config(db, guest, FakeHA()))["ha_token"] == "device-token"


async def test_extending_a_guest_without_remote_access_names_nothing(db, events):
    guest = await _device(
        db, guest=True, expires_at=(_now() + timedelta(hours=2)).isoformat()
    )
    preview = await _extend(db, guest, 24, dry_run=True)
    assert (preview["hub_remote_now"], preview["hub_remote_at"]) == (False, None)


async def test_an_unknown_stored_level_shows_hub_only(db):
    body = catalog.materialize("adult")
    body["rules"]["control_path"] = "everything"
    async with db.transaction():
        await db.execute(
            "INSERT INTO profiles (id, name, kind, child_protection, body_json, rev, "
            "created_at, updated_at) VALUES ('odd', 'Odd', 'person', 0, ?, 1, 'x', 'x')",
            (__import__("json").dumps({**body, "schema_version": 1}),),
        )
    view = await store.profile_view(db, await store.profile_row(db, "odd"))
    assert view["rules"]["control_path"] == "hub"


async def test_reopening_a_guest_sends_one_identity_update(db, events):
    """Reopening an expired guest with remote access switches its remote
    access on again: the integration hears it once."""
    prof = await _profile(db, control_path="hub", remote_access=True)
    guest = await _device(
        db,
        guest=True,
        expires_at=(_now() - timedelta(minutes=1)).isoformat(),
        profile_id=prof,
    )
    events["integration"].clear()
    await _extend(db, guest, 24, dry_run=False)
    updates = [
        m
        for m in events["integration"]
        if m["type"] == "update_client_identity" and m["data"]["client_id"] == guest
    ]
    assert len(updates) == 1 and updates[0]["data"]["remote_allowed"] is True
