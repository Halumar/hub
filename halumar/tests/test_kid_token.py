"""No HA key on a device with parental controls on the hub path.

A device with parental controls keeps its read-only HA user: the hub switches
for it through ``<ns>.control`` with that user's token and, without remote
access, never hands the token to the app. Every way into parental controls
(assignment, bulk, delete with replacement, the profile itself) rotates the
token the app knew; the log entry ``ha_key_withdrawn`` follows once the hub
accepted the new token (the old one works until then).
``secure_keyless_tokens`` catches a rotation that got lost (hub stopped in
between, integration silent beyond the rotation TTL) at start and in the
retry loop. ``has_ha_token`` = the device holds a key, or still holds the
old one until the rotation is accepted.
"""

import types
from datetime import datetime, timedelta, timezone

import api.websocket as ws
import pytest
from api import integration as api_integration
from api import profiles as api
from api.pairing import clients as clients_api
from models.database import Database
from services import crypto, kids_mode
from services import device_provisioning as dp

from .profile_helpers import add_client, starter_profile

UTC = timezone.utc
OLD = "2026-01-01T08:00:00.000000+00:00"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    kids_mode.reset()
    yield
    crypto.reset_cache()


@pytest.fixture
def events(monkeypatch):
    log = {"to": [], "integration": []}

    async def _send(client_id, message):
        log["to"].append((client_id, message))
        return True

    async def _broadcast(message, *a, **k):
        return None

    async def _sender(message):
        log["integration"].append(message)
        return True

    monkeypatch.setattr(ws, "send_to_client", _send)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    monkeypatch.setattr(dp, "_sender", _sender)
    return log


def _req(db, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth, actor="Admin One"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


async def _device(db, *, minted_at=OLD, **columns):
    """A device whose app holds an active token minted at ``minted_at``."""
    return await add_client(
        db,
        ha_token_status="active",
        ha_token_encrypted=await crypto.encrypt_token(db, "app-token"),
        ha_user_id="u-" + columns.get("name", "x"),
        ha_token_minted_at=minted_at,
        **columns,
    )


def _rotations(events, client_id):
    return [
        m
        for m in events["integration"]
        if m["type"] == "provision_client"
        and m["data"]["client_id"] == client_id
        and m["data"]["token_action"] == dp.TOKEN_ACTION_ROTATE
    ]


async def _withdrawn(db, client_id):
    rows = await db.fetchall(
        "SELECT detail_json FROM event_log WHERE kind = ? AND client_id = ?",
        (dp.LOG_KIND_KEY_WITHDRAWN, client_id),
    )
    return [r["detail_json"] for r in rows]


async def _deliver(db, client_id, token="hub-token"):
    """The integration delivers the rotated token (minted now) and the hub
    accepts it (the path of ``POST /api/integration/client-token``)."""
    assert await dp.store_delivered_token(
        db, client_id, ha_user_id="u-new", refresh_token_id="rt-new", token=token
    )


async def _has_key(db, client_id):
    return (await clients_api.client_view(db, client_id)).has_ha_token


# ── every way into parental controls rotates ───────────────────────────────


async def test_assigning_a_kids_profile_withdraws_the_key(db, events):
    phone = await _device(db, name="Child One", profile="adult")
    assert await _has_key(db, phone)
    kid = await starter_profile(db, "child")
    await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=kid), dry_run=False
    )
    assert len(_rotations(events, phone)) == 1
    assert await dp.rotation_pending(db, phone)
    # The app gets no key from now on, but the old one works until the hub
    # accepted the rotated token: no log entry yet, the device still counts
    # as holding it.
    assert await _withdrawn(db, phone) == []
    assert await _has_key(db, phone)
    cfg = await ws._get_client_config(db, phone)
    assert cfg["ha_token"] is None and cfg["ha_url"] is None
    # Nothing left to catch up once the new token arrived; the withdrawal is
    # logged then, and the hub keeps the user for its path.
    await _deliver(db, phone)
    assert await _withdrawn(db, phone) == [None]
    assert await dp.secure_keyless_tokens(db) == 0
    assert not await _has_key(db, phone)
    status = await api_integration.integration_status(_req(db, auth="integration"))
    me = next(c for c in status["clients"] if c["id"] == phone)
    assert me["has_ha_token"] is False and me["ha_token_status"] == "active"


async def test_bulk_assignment_withdraws_every_key(db, events):
    a = await _device(db, name="A", profile="adult")
    b = await _device(db, name="B", profile="adult")
    kid = await starter_profile(db, "child")
    await api.bulk_update_clients(
        _req(db),
        api.Bulk(client_ids=[a, b], action="assign_profile", profile_id=kid),
    )
    assert len(_rotations(events, a)) == 1 and len(_rotations(events, b)) == 1
    await _deliver(db, a)
    await _deliver(db, b)
    assert await _withdrawn(db, a) and await _withdrawn(db, b)


async def test_profile_switched_to_parental_controls_withdraws_the_key(db, events):
    p = await api.create_profile(_req(db), api.ProfileWrite(name="Familie"))
    phone = await _device(db, name="P", profile_id=p["id"])
    await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(child_protection=True), dry_run=False
    )
    assert len(_rotations(events, phone)) == 1
    await _deliver(db, phone)
    assert await _withdrawn(db, phone)
    assert await dp.secure_keyless_tokens(db) == 0


async def test_delete_with_kids_replacement_withdraws_the_key(db, events):
    p = await api.create_profile(_req(db), api.ProfileWrite(name="Alt"))
    phone = await _device(db, name="D", profile_id=p["id"])
    kid = await starter_profile(db, "child")
    await api.delete_profile(_req(db), p["id"], replacement=kid, dry_run=False)
    assert len(_rotations(events, phone)) == 1


async def test_without_integration_the_rotation_waits_for_the_poll(
    db, events, monkeypatch
):
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
    phone = await _device(db, name="Off", profile="adult")
    kid = await starter_profile(db, "child")
    await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=kid), dry_run=False
    )
    assert _rotations(events, phone) == []
    items = await dp.outstanding_requests(db)
    assert [i["token_action"] for i in items if i["client_id"] == phone] == [
        dp.TOKEN_ACTION_ROTATE
    ]


async def test_back_to_adult_hands_out_the_hub_token(db, events):
    phone = await _device(db, name="Back", profile="adult")
    kid = await starter_profile(db, "child")
    adult = await starter_profile(db, "adult")
    await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=kid), dry_run=False
    )
    await _deliver(db, phone, token="never-on-the-app")
    events["integration"].clear()
    await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=adult), dry_run=False
    )
    assert _rotations(events, phone) == []
    assert await _has_key(db, phone)
    assert (await ws._get_client_config(db, phone))["ha_token"] == "never-on-the-app"
    assert await dp.secure_keyless_tokens(db) == 0


async def test_paired_as_child_needs_nothing(db, events):
    kid = await starter_profile(db, "child")
    phone = await _device(db, name="K", profile_id=kid, minted_at=Database.now())
    assert not await _has_key(db, phone)
    assert await dp.secure_keyless_tokens(db) == 0
    assert await _withdrawn(db, phone) == []


async def test_moving_between_kids_profiles_rotates_nothing(db, events):
    kid = await starter_profile(db, "child")
    other = await api.create_profile(
        _req(db), api.ProfileWrite(name="Kind 2", child_protection=True)
    )
    phone = await _device(db, name="KK", profile_id=kid, minted_at=Database.now())
    await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=other["id"]), dry_run=False
    )
    assert _rotations(events, phone) == []
    assert await dp.secure_keyless_tokens(db) == 0


# ── catching up a lost rotation (start, retry loop) ─────────────────────────


async def _assign_without_effects(db, client_id, profile_id, ts):
    """What a hub stopped between the write and its effects leaves behind."""
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET profile_id = ? WHERE id = ?",
            (profile_id, client_id),
        )
        await db.execute(
            "INSERT INTO profile_audit (ts, action, actor, profile_id, client_id, "
            "before_json, after_json) VALUES (?, 'profile_assigned', 'x', ?, ?, ?, ?)",
            (
                ts,
                profile_id,
                client_id,
                '{"profile_id": "gone"}',
                f'{{"profile_id": "{profile_id}"}}',
            ),
        )


async def test_lost_rotation_after_assignment_is_caught_up(db, events):
    phone = await _device(db, name="Lost", profile="adult")
    kid = await starter_profile(db, "child")
    await _assign_without_effects(db, phone, kid, "2026-06-01T08:00:00Z")
    assert await dp.secure_keyless_tokens(db, send=True) == 1
    assert len(_rotations(events, phone)) == 1
    assert await _withdrawn(db, phone) == []
    # Idempotent while the rotation runs and after the token arrived.
    assert await dp.secure_keyless_tokens(db) == 0
    await _deliver(db, phone)
    assert await _withdrawn(db, phone) == [None]
    assert await dp.secure_keyless_tokens(db) == 0


async def test_same_second_counts_as_newer(db, events):
    """Audit times are whole seconds: the switch's own rotation of that second
    must not be rotated again."""
    phone = await _device(
        db, name="Sec", profile="adult", minted_at="2026-06-01T08:00:00.400000+00:00"
    )
    kid = await starter_profile(db, "child")
    await _assign_without_effects(db, phone, kid, "2026-06-01T08:00:00Z")
    assert await dp.secure_keyless_tokens(db) == 0


async def test_lost_rotation_after_profile_switch_is_caught_up(db, events):
    p = await api.create_profile(_req(db), api.ProfileWrite(name="Später Kind"))
    phone = await _device(db, name="PS", profile_id=p["id"])
    events["integration"].clear()
    async with db.transaction():
        # What the write stores: parental controls on the hub path.
        await db.execute(
            "UPDATE profiles SET child_protection = 1, rev = rev + 1, "
            "body_json = json_set(body_json, '$.rules.control_path', 'hub') "
            "WHERE id = ?",
            (p["id"],),
        )
        await db.execute(
            "INSERT INTO profile_revisions (profile_id, rev, name, child_protection, "
            "body_json, created_at) SELECT id, rev, name, 1, body_json, ? "
            "FROM profiles WHERE id = ?",
            ("2026-06-01T08:00:00Z", p["id"]),
        )
    assert await dp.secure_keyless_tokens(db, send=True) == 1
    assert len(_rotations(events, phone)) == 1


async def test_rotation_dropped_by_the_ttl_comes_back(db, events, monkeypatch):
    """The integration stayed silent beyond the rotation TTL: the retry loop
    starts the rotation again instead of leaving the app's key alive."""
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
    phone = await _device(db, name="TTL", profile="adult")
    kid = await starter_profile(db, "child")
    await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=kid), dry_run=False
    )
    later = dp._now() + dp.ROTATION_TTL_SECONDS + 60
    monkeypatch.setattr(dp, "_now", lambda: later)
    assert await dp.retry_once(db, connected=True) == 1
    assert len(_rotations(events, phone)) == 1
    assert await dp.rotation_pending(db, phone)
    # Not withdrawn until a rotated token is accepted, then logged once.
    assert await _withdrawn(db, phone) == []
    await _deliver(db, phone)
    assert len(await _withdrawn(db, phone)) == 1


async def test_unknown_mint_time_and_odd_rows_do_not_crash(db, events):
    kid = await starter_profile(db, "child")
    phone = await _device(db, name="NoMint", profile="adult", minted_at=None)
    await _assign_without_effects(db, phone, kid, "not a time")
    # No usable switch time: nothing to compare, nothing rotated, no error.
    assert await dp.secure_keyless_tokens(db) == 0
    other = await _device(db, name="NoMint2", profile="adult", minted_at=None)
    await _assign_without_effects(db, other, kid, "2026-06-01T08:00:00Z")
    assert await dp.secure_keyless_tokens(db) == 1


async def test_inactive_and_deleted_devices_are_left_alone(db, events):
    kid = await starter_profile(db, "child")
    gone = await _device(db, name="Gone", profile="adult", is_active=0)
    held = await _device(
        db, name="Held", profile="adult", pending_deletion_at=Database.now()
    )
    for cid in (gone, held):
        await _assign_without_effects(db, cid, kid, "2026-06-01T08:00:00Z")
    assert await dp.secure_keyless_tokens(db) == 0


def test_minted_before():
    t = datetime(2026, 6, 1, 8, tzinfo=UTC)
    iso = t.isoformat()
    assert dp._minted_before((t - timedelta(seconds=5)).isoformat(), [iso, None])
    assert not dp._minted_before((t + timedelta(seconds=0.5)).isoformat(), [iso])
    assert not dp._minted_before((t + timedelta(seconds=2)).isoformat(), [iso])
    assert not dp._minted_before(None, [None, "garbage"])
    assert dp._minted_before(None, [iso])


async def test_withdrawal_at_access_close_comes_back_after_the_ttl(
    db, events, monkeypatch
):
    """Access closed while the integration was away; the rotation
    TTL dropped the withdrawal. The retry loop starts it again."""
    from services import access

    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
    phone = await _device(
        db,
        name="Sitter",
        profile="adult",
        overrides={"access_hours": {"enabled": True, "windows": []}},
    )
    await access.check_transitions(db)
    row = await db.fetchone(
        "SELECT access_state FROM paired_clients WHERE id = ?", (phone,)
    )
    assert row["access_state"] == "closed"
    assert await dp.rotation_pending(db, phone)
    later = dp._now() + dp.ROTATION_TTL_SECONDS + 60
    monkeypatch.setattr(dp, "_now", lambda: later)
    assert await dp.retry_once(db, connected=True) == 1
    assert len(_rotations(events, phone)) == 1
    # Delivered: the hub holds a token younger than the close, nothing more.
    await _deliver(db, phone)
    assert await dp.secure_closed_tokens(db) == 0


async def _child_with_temporary_remote(db, monkeypatch):
    from api import profiles as papi
    from services.profiles import expiry_task

    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
    now = datetime.now(UTC)
    kid = await _device(
        db,
        name="Kid",
        profile="child",
        minted_at=(now - timedelta(minutes=5)).isoformat(),
    )
    end = (now + timedelta(hours=1)).replace(microsecond=0)
    await papi.put_client_override(
        _req(db),
        kid,
        "remote_access",
        papi.DeviceOverride(value=True, expires_at=end.isoformat()),
        dry_run=False,
    )
    return kid, end, expiry_task


async def test_end_of_remote_access_rotation_comes_back_after_the_ttl(
    db, events, monkeypatch
):
    """The rotation at the end of a temporary remote access was
    dropped by the TTL while the integration was away; the catch-up starts
    it again (once)."""
    kid, end, expiry_task = await _child_with_temporary_remote(db, monkeypatch)
    events["integration"].clear()
    assert await expiry_task.expire_due(db, now=end + timedelta(seconds=1)) == [kid]
    assert await dp.rotation_pending(db, kid)
    later = dp._now() + dp.ROTATION_TTL_SECONDS + 60
    monkeypatch.setattr(dp, "_now", lambda: later)
    assert await dp.retry_once(db, connected=True) == 1
    assert len(_rotations(events, kid)) == 1
    assert await expiry_task.expire_due(db, now=end + timedelta(seconds=2)) == []
    await _deliver(db, kid)
    assert await dp.secure_keyless_tokens(db) == 0


async def test_crash_between_expiry_and_its_effects_is_caught_up(
    db, events, monkeypatch
):
    """The adjustment is gone but the effects never ran (crash): the
    audit row of the same transaction lets the catch-up rotate the key."""
    from services.profiles import changes

    kid, end, expiry_task = await _child_with_temporary_remote(db, monkeypatch)
    events["integration"].clear()

    async def _crash(*_a, **_k):
        raise RuntimeError("hub stopped")

    monkeypatch.setattr(changes, "audit", _crash)
    with pytest.raises(RuntimeError):
        await expiry_task.expire_due(db, now=end + timedelta(seconds=1))
    assert not await dp.rotation_pending(db, kid)
    assert await dp.retry_once(db, connected=True) == 1
    assert len(_rotations(events, kid)) == 1


# ── every way to lose remote access, integration away past the TTL ──


async def _kids_profiles(db):
    """Two kids profiles: with and without remote access (not fixed)."""
    out = {}
    for name, remote in (("Kids away", True), ("Kids home", False)):
        p = await api.create_profile(
            _req(db),
            api.ProfileWrite(
                name=name, child_protection=True, rules={"remote_access": remote}
            ),
        )
        out[remote] = p["id"]
    return out


async def _past_ttl_rotates(db, events, monkeypatch, kid):
    later = dp._now() + dp.ROTATION_TTL_SECONDS + 60
    monkeypatch.setattr(dp, "_now", lambda: later)
    assert await dp.retry_once(db, connected=True) == 1
    assert len(_rotations(events, kid)) == 1
    await _deliver(db, kid)
    assert await dp.secure_keyless_tokens(db) == 0


@pytest.mark.parametrize(
    "how",
    [
        "reassign",
        "delete_profile",
        "edit_profile",
        "adjustment_set",
        "adjustment_removed",
        "adjustment_expired",
    ],
)
async def test_lost_remote_access_is_caught_up_after_the_ttl(
    db, events, monkeypatch, how
):
    from datetime import datetime as _dt

    from services.profiles import expiry_task

    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
    profiles = await _kids_profiles(db)
    start = profiles[False] if how.startswith("adjustment") else profiles[True]
    kid = await _device(db, name=f"Kid-{how}", profile_id=start)
    end = None
    if how == "adjustment_set":
        await api.put_client_override(
            _req(db),
            kid,
            "remote_access",
            api.DeviceOverride(value=True),
            dry_run=False,
        )
    elif how in ("adjustment_removed", "adjustment_expired"):
        end = (_dt.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
        extra = {"expires_at": end.isoformat()} if how == "adjustment_expired" else {}
        await api.put_client_override(
            _req(db),
            kid,
            "remote_access",
            api.DeviceOverride(value=True, **extra),
            dry_run=False,
        )
    # The app got its key for the way away an hour ago.
    await _deliver(db, kid, token="app-token-with-remote")
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_minted_at = ? WHERE id = ?",
            ((_dt.now(UTC) - timedelta(hours=1)).isoformat(), kid),
        )
    assert (await store_rules(db, kid))["remote_access"]["value"] is True
    events["integration"].clear()

    if how == "reassign":
        await api.assign_client_profile(
            _req(db), kid, api.ClientProfile(profile_id=profiles[False]), dry_run=False
        )
    elif how == "delete_profile":
        await api.delete_profile(
            _req(db), profiles[True], replacement=profiles[False], dry_run=False
        )
    elif how == "edit_profile":
        await api.update_profile(
            _req(db),
            profiles[True],
            api.ProfileWrite(rules={"remote_access": False}),
            dry_run=False,
        )
    elif how == "adjustment_set":
        await api.put_client_override(
            _req(db),
            kid,
            "remote_access",
            api.DeviceOverride(value=False),
            dry_run=False,
        )
    elif how == "adjustment_removed":
        await api.delete_client_override(_req(db), kid, "remote_access", dry_run=False)
    else:
        assert await expiry_task.expire_due(db, now=end + timedelta(seconds=1)) == [kid]
    assert (await store_rules(db, kid))["remote_access"]["value"] is False
    await _past_ttl_rotates(db, events, monkeypatch, kid)


async def store_rules(db, client_id):
    from services.profiles import store

    return (await store.resolve_client(db, client_id))[2]
