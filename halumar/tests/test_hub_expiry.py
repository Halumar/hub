"""The end of a temporary value takes the key away on the real clock.

The planner (``expiry_task.expire_due``) runs at or after an end; the
resolver then already ignores the ended adjustment. These tests move the
resolver's clock (``store.utcnow``) and the planner's together, like the
running hub, and check every way a key ends: a temporary Checked or remote
access on Hub only, a child's temporary remote access, many ends at once, a
hub that was down across the end, an unreachable integration, the end of a
guest access and access hours that close.
"""

# The fixtures of test_hub_path are imported by name.
# ruff: noqa: F811

from datetime import datetime, timedelta, timezone

import api.websocket as ws
from models.database import Database
from services import access, kids_mode, watch_access
from services.profiles import catalog, expiry_task
from services.profiles import rows as profile_rows
from services.profiles import store

from .test_hub_path import (  # noqa: F401 - fixtures
    FakeHA,
    _clean_state,
    _device,
    _get_client_config,
    dp,
    events,
    pchanges,
)


async def _hub_profile(db, *, child=False):
    body = catalog.materialize("child" if child else "adult")
    body["rules"].update(control_path="hub", remote_access=False)
    async with db.transaction():
        return await profile_rows.insert_profile(
            db,
            name=f"Hub {'Kind' if child else 'Person'}",
            body=body,
            now=Database.now(),
            actor=None,
        )


async def _old_token(db, client_id):
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_minted_at = ? WHERE id = ?",
            ("2026-01-01T08:00:00+00:00", client_id),
        )


async def _temporary(db, client_id, rule, value, end):
    async def _write():
        await store.put_override(
            db,
            client_id,
            rule,
            value=value,
            user_choice=False,
            expires_at=end.isoformat(),
            note=None,
            actor="test",
        )

    _, change = await pchanges.run(db, _write, dry_run=False)
    await pchanges.apply_effects(db, change)


def _clock(monkeypatch, when):
    """The resolver's and the planner's clock at ``when``."""
    monkeypatch.setattr(store, "utcnow", lambda: when)
    monkeypatch.setattr(access, "utcnow", lambda: when)
    return when


async def _setup(db, monkeypatch, rule, value, *, child=False):
    removed = []

    async def _remove(db_, watch_id, why, reason=None):
        removed.append((watch_id, reason))

    monkeypatch.setattr(watch_access, "_remove", _remove)
    phone = await _device(db, profile_id=await _hub_profile(db, child=child))
    watch = None
    if not child:
        watch = await _device(
            db,
            platform="watchos",
            profile="adult",
            set_up_by_client_id=phone,
            set_up_method="onboard",
            seal_key="k" * 43,
        )
    await _old_token(db, phone)
    end = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    await _temporary(db, phone, rule, value, end)
    cfg = await _get_client_config(db, phone, FakeHA())
    assert cfg["ha_token"] == "device-token"
    return phone, watch, end, removed


def _rotations(events, client_id):
    return [
        m
        for m in events["integration"]
        if m["type"] == "provision_client"
        and m["data"]["client_id"] == client_id
        and m["data"]["token_action"] == "rotate"
    ]


async def _key_lost(db, client_id):
    row = await db.fetchone(
        "SELECT key_lost_at FROM paired_clients WHERE id = ?", (client_id,)
    )
    return row["key_lost_at"]


async def test_end_of_a_temporary_checked_on_the_real_clock(db, events, monkeypatch):
    phone, watch, end, removed = await _setup(
        db, monkeypatch, "control_path", "checked"
    )
    later = _clock(monkeypatch, end + timedelta(seconds=1))
    assert await expiry_task.expire_due(db, now=later) == [phone]
    assert await _key_lost(db, phone)
    assert await dp.rotation_pending(db, phone)
    assert _rotations(events, phone)
    assert await dp.stale_keys(db) == {phone}
    assert removed == [(watch, "hub_only")]
    assert (phone, {"type": "client_config_updated", "data": {}}) in events["to"]
    assert (await _get_client_config(db, phone, FakeHA()))["ha_token"] is None


async def test_end_of_a_temporary_remote_access_on_the_real_clock(
    db, events, monkeypatch
):
    """Hub only with a temporary remote access: at the end the key is
    rotated and the HA user is local only again (identity update)."""
    phone, _watch, end, _removed = await _setup(db, monkeypatch, "remote_access", True)
    events["integration"].clear()
    later = _clock(monkeypatch, end + timedelta(seconds=1))
    assert await expiry_task.expire_due(db, now=later) == [phone]
    assert await _key_lost(db, phone)
    assert _rotations(events, phone)
    identity = [
        m
        for m in events["integration"]
        if m["type"] == "update_client_identity" and m["data"]["client_id"] == phone
    ]
    assert identity and identity[-1]["data"]["remote_allowed"] is False


async def test_end_of_a_childs_temporary_remote_access(db, events, monkeypatch):
    kid, _watch, end, _removed = await _setup(
        db, monkeypatch, "remote_access", True, child=True
    )
    events["integration"].clear()
    later = _clock(monkeypatch, end + timedelta(seconds=1))
    assert await expiry_task.expire_due(db, now=later) == [kid]
    assert await _key_lost(db, kid)
    assert _rotations(events, kid)
    cfg = await _get_client_config(db, kid, FakeHA())
    assert cfg["ha_token"] is None and cfg["kids"]["away"] == "none"


async def test_hub_down_across_the_end_is_caught_up_at_start(db, events, monkeypatch):
    """The hub was down for two days across the end: the first run of the
    planner after the start takes the key away, and the catch-up has
    something to rotate until the new token is accepted."""
    phone, watch, end, removed = await _setup(
        db, monkeypatch, "control_path", "checked"
    )
    later = _clock(monkeypatch, end + timedelta(days=2))
    events["integration"].clear()
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
    assert await expiry_task.expire_due(db, now=later) == [phone]
    assert await _key_lost(db, phone)
    assert await dp.stale_keys(db) == {phone}
    assert removed == [(watch, "hub_only")]
    # The integration is away: the rotation waits in token_requests and
    # goes out with the next round once it is back.
    items = await dp.outstanding_requests(db)
    assert [i["token_action"] for i in items if i["client_id"] == phone] == ["rotate"]
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    await dp.retry_once(db, connected=True)
    assert _rotations(events, phone)
    await dp.store_delivered_token(
        db, phone, ha_user_id="u-p", refresh_token_id="rt-p", token="hub-token"
    )
    assert await dp.stale_keys(db) == set()
    assert await dp.secure_keyless_tokens(db) == 0


async def test_many_ends_at_once(db, events, monkeypatch):
    """Ends at different moments, all past: every device loses its key."""
    _clock(monkeypatch, datetime.now(timezone.utc))
    devices = []
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    prof = await _hub_profile(db)
    for i in range(5):
        cid = await _device(db, profile_id=prof)
        await _old_token(db, cid)
        rule, value = (("control_path", "checked"), ("remote_access", True))[i % 2]
        await _temporary(db, cid, rule, value, start + timedelta(minutes=10 * i))
        devices.append(cid)
    later = _clock(monkeypatch, start + timedelta(hours=3))
    assert sorted(await expiry_task.expire_due(db, now=later)) == sorted(devices)
    assert await dp.stale_keys(db) == set(devices)
    for cid in devices:
        assert await dp.rotation_pending(db, cid)


async def test_an_end_still_ahead_after_a_clock_step_takes_nothing(
    db, events, monkeypatch
):
    """A clock stepped back before the end: nothing ends, nothing is lost."""
    phone, _watch, end, _removed = await _setup(
        db, monkeypatch, "control_path", "checked"
    )
    earlier = _clock(monkeypatch, end - timedelta(hours=2))
    assert await expiry_task.expire_due(db, now=earlier) == []
    assert not await _key_lost(db, phone)
    assert (await _get_client_config(db, phone, FakeHA()))["ha_token"] == "device-token"


async def test_an_expired_guest_gets_no_key(db, events, monkeypatch):
    """At its expiry a guest has no key at all (strictest values), before the
    hub removes it with its HA user."""
    guest = await _device(
        db,
        guest=True,
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        overrides={"remote_access": True},
    )
    _clock(monkeypatch, datetime.now(timezone.utc) + timedelta(hours=2))
    cfg = await _get_client_config(db, guest, FakeHA())
    assert cfg["rules"]["control_path"]["value"] == "hub"
    assert cfg["ha_token"] is None and cfg["ha_enforcement"] is None


async def test_access_hours_closing_rotates_a_held_key_on_the_real_clock(
    db, events, monkeypatch
):
    import time

    prof = await _hub_profile(db)
    async with db.transaction():
        await db.execute(
            "UPDATE profiles SET body_json = json_set(body_json, "
            "'$.rules.access_hours', json(?)) WHERE id = ?",
            (
                '{"enabled": true, "windows": [{"days": ["mon","tue","wed","thu",'
                '"fri","sat","sun"], "start": "08:00", "end": "20:00"}]}',
                prof,
            ),
        )
    phone = await _device(db, profile_id=prof, remote=True)
    monkeypatch.setattr(kids_mode, "_tz_cache", (time.monotonic(), "UTC"))
    day = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    _clock(monkeypatch, day)
    await access.check_transitions(db, now=day)
    assert not await dp.rotation_pending(db, phone)
    night = _clock(monkeypatch, day.replace(hour=21))
    await access.check_transitions(db, now=night)
    assert await dp.rotation_pending(db, phone)
    assert (await _get_client_config(db, phone, FakeHA()))["ha_token"] is None


async def test_an_admin_write_after_an_unprocessed_end_still_takes_the_key(
    db, events, monkeypatch
):
    """The end passed; before the planner ran, the admin deletes the same
    adjustment. The write processes the ended adjustments first, so the key
    is still withdrawn."""
    phone = await _device(db, profile_id=await _hub_profile(db))
    await _old_token(db, phone)
    end = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)
    await _temporary(db, phone, "control_path", "checked", end)
    _clock(monkeypatch, end + timedelta(seconds=2))

    async def _delete():
        await store.delete_override(db, phone, "control_path")

    _, change = await pchanges.run(db, _delete, dry_run=False)
    await pchanges.apply_effects(db, change)
    assert await _key_lost(db, phone)
    assert await dp.rotation_pending(db, phone)
    assert await expiry_task.expire_due(db, now=end + timedelta(seconds=3)) == []


async def test_overlapping_ends_withdraw_the_key_at_the_later_one(
    db, events, monkeypatch
):
    phone = await _device(db, profile_id=await _hub_profile(db))
    await _old_token(db, phone)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    t1, t2 = now + timedelta(hours=1), now + timedelta(hours=2)
    await _temporary(db, phone, "control_path", "checked", t1)
    await _temporary(db, phone, "remote_access", True, t2)
    later = _clock(monkeypatch, t1 + timedelta(seconds=1))
    await expiry_task.expire_due(db, now=later)
    assert (await _get_client_config(db, phone, FakeHA()))["ha_token"] == "device-token"
    assert await _key_lost(db, phone) is None
    later = _clock(monkeypatch, t2 + timedelta(seconds=1))
    await expiry_task.expire_due(db, now=later)
    assert await dp.rotation_pending(db, phone)
