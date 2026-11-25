"""The platform of a paired device is fixed at pairing.

* ``auth`` never changes the stored platform; a valid platform that differs
  from it is refused with ``auth_error`` ``invalid_client_info`` and nothing
  is stored. The one exception: ``android`` and ``android_tablet`` may stand
  for each other (the Android app tells them apart by the current screen
  width); the session is accepted and the stored value stays.
* A malformed platform stays ``invalid_client_info``.
* The mismatch is written to the admin audit view once per device and
  interval.
* App version, build, model and protocol version keep updating; the
  app-version floor is the one of the paired platform.
* A child's watch that authenticates as an iPhone still resolves as its
  phone's watch (parental controls, checked control path, read-only HA user).
"""

import types
import uuid

import api.websocket as ws
import pytest
from api import pairing
from api.ws import client as ws_client
from fastapi import WebSocketDisconnect
from models.database import Database
from services import (
    audit_log,
    client_activity,
    crypto,
    enforcement,
    kids_mode,
    protocol,
    watch_access,
)
from services.profiles import store
from starlette.websockets import WebSocketState

from .profile_helpers import add_client
from .test_child_watch import _child_phone, _pair
from .test_enforcement import STATES, FakeHA, _home

INFO = {
    "protocol_version": 1,
    "app_version": "2026.12.0",
    "app_build": "6",
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()
    audit_log.reset_recent()
    enforcement.reset_state()
    kids_mode.reset()
    watch_access.reset_state()

    async def _quiet(*a, **k):
        return False

    monkeypatch.setattr(ws, "broadcast", _quiet)
    monkeypatch.setattr(ws, "send_to_client", _quiet)
    yield
    ws._clients.clear()
    pairing.clear_active_pins()
    audit_log.reset_recent()
    enforcement.reset_state()
    watch_access.reset_state()
    crypto.reset_cache()


class _WS:
    def __init__(self, db, messages=(), host="192.0.2.10"):
        import json

        self._json = json
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host=host)
        self.url = types.SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []
        self.closed = None
        self.application_state = WebSocketState.CONNECTING
        self._messages = list(messages)

    async def accept(self):
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED

    async def receive_text(self):
        if self._messages:
            return self._json.dumps(self._messages.pop(0))
        raise WebSocketDisconnect()


async def _auth(db, secret, **info):
    """Answer to one ``auth`` (the message after ``auth_required``)."""
    fake = _WS(db, [{"type": "auth", "secret": secret, **INFO, **info}])
    await ws.websocket_endpoint(fake)
    return fake.sent[1]


async def _device(db, platform, **columns):
    secret = uuid.uuid4().hex
    client_id = await add_client(
        db, platform=platform, secret=secret, last_seen=Database.now(), **columns
    )
    return client_id, secret


async def _platform(db, client_id):
    row = await db.fetchone(
        "SELECT platform FROM paired_clients WHERE id = ?", (client_id,)
    )
    return row["platform"]


def _mismatches():
    return [
        e for e in audit_log.recent_entries() if e["event"] == "client_info_mismatch"
    ]


def _refused(answer):
    return answer == {
        "type": "auth_error",
        "reason": "invalid_client_info",
        "message": "Client info does not match this device's pairing",
    }


# ── a child's watch that reports another platform ───────────────────────────


async def _watch_view(db, watch):
    row, prof, rules = await store.resolve_client(db, watch)
    snaps = {s["client_id"]: s for s in await enforcement.build_all(db, FakeHA(STATES))}
    snap = snaps[watch]
    return {
        "platform": row["platform"],
        "profile_id": prof["id"],
        "child_protection": bool(prof["child_protection"]),
        "kids_subject": kids_mode.kids_subject(row),
        "control_path": rules["control_path"]["value"],
        "snap_child_protection": snap["child_protection"],
        "snap_control_path": snap["control_path"],
        "snap_kids": snap.get("kids"),
    }


async def test_childs_watch_authenticating_as_iphone_stays_the_phones_watch(db):
    await _home(db)
    pid, phone = await _child_phone(db)
    secret = uuid.uuid4().hex
    watch = await _pair(db, phone, secret=secret)
    before = await _watch_view(db, watch)
    assert before["platform"] == "watchos" and before["profile_id"] == pid
    assert before["kids_subject"] == phone and before["control_path"] == "checked"
    # checked = the device's HA user stays read-only (integration's group).
    assert before["snap_control_path"] == "checked"

    answer = await _auth(db, secret, platform="ios", device_model="iPhone18,1")

    assert await _watch_view(db, watch) == before
    assert _refused(answer), answer
    row = await db.fetchone(
        "SELECT device_model FROM paired_clients WHERE id = ?", (watch,)
    )
    assert row["device_model"] is None  # nothing of the refused info stored
    assert not any(e["event"] == "connected" for e in await _events(db, watch))
    entries = _mismatches()
    assert len(entries) == 1
    assert entries[0]["client_id"] == watch
    assert entries[0]["details"] == "platform: paired watchos, reported ios"
    assert entries[0]["category"] == "security"


async def _events(db, client_id):
    return await client_activity.list_events(db, client_id)


async def test_watch_reporting_its_own_platform_is_accepted(db):
    await _home(db)
    _pid, phone = await _child_phone(db)
    secret = uuid.uuid4().hex
    watch = await _pair(db, phone, secret=secret)
    answer = await _auth(db, secret, platform="watchos", device_model="Watch7,9")
    assert answer["type"] == "auth_ok" and answer["client_id"] == watch
    assert await _platform(db, watch) == "watchos"
    assert _mismatches() == []


# ── every direction ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "paired, reported",
    [
        ("ios", "watchos"),  # phone -> watch
        ("android", "wearos"),
        ("ios", "tvos"),  # phone -> TV
        ("ios", "ipados"),  # phone -> tablet / panel
        ("android", "android_tv"),
        ("tvos", "ios"),  # TV -> phone
        ("android_tv", "android"),
        ("android_tv", "android_tablet"),
        ("ipados", "ios"),  # panel -> phone
        ("watchos", "ios"),  # away from a watch
        ("watchos", "android"),
        ("wearos", "android"),
        ("wearos", "watchos"),
        ("android", "ios"),  # across vendors
        ("ios", "android"),  # an iPhone has no floor as android
        ("ios", "web"),
        ("android_tablet", "ipados"),
    ],
)
async def test_another_platform_is_refused_and_not_stored(db, paired, reported):
    client_id, secret = await _device(db, paired)
    answer = await _auth(db, secret, platform=reported)
    assert _refused(answer), answer
    assert await _platform(db, client_id) == paired
    assert len(_mismatches()) == 1


@pytest.mark.parametrize(
    "reported",
    [None, "", 7, ["ios"], {"p": "ios"}, "visionos", "IOS", "ios ", "i" * 5000],
    ids=["null", "empty", "int", "list", "object", "unknown", "case", "space", "long"],
)
async def test_malformed_platform_stays_invalid_client_info(db, reported):
    client_id, secret = await _device(db, "watchos")
    message = {"platform": reported}
    answer = await _auth(db, secret, **message)
    assert answer == {
        "type": "auth_error",
        "reason": "invalid_client_info",
        "message": "Invalid client info: platform",
    }
    assert await _platform(db, client_id) == "watchos"


async def test_missing_platform_stays_invalid_client_info(db):
    client_id, secret = await _device(db, "watchos")
    fake = _WS(db, [{"type": "auth", "secret": secret, **INFO}])
    await ws.websocket_endpoint(fake)
    assert fake.sent[1]["reason"] == "invalid_client_info"
    assert await _platform(db, client_id) == "watchos"


async def test_old_app_with_another_platform_stores_nothing(db):
    """A refused platform comes before upgrade_required: no version stored."""
    client_id, secret = await _device(db, "watchos", app_version="2026.12.0")
    answer = await _auth(db, secret, platform="ios", app_version="2026.1.0")
    assert _refused(answer), answer
    row = await db.fetchone(
        "SELECT platform, app_version FROM paired_clients WHERE id = ?", (client_id,)
    )
    assert (row["platform"], row["app_version"]) == ("watchos", "2026.12.0")


async def test_upgrade_required_keeps_the_platform(db):
    client_id, secret = await _device(db, "tvos")
    fake = _WS(db, [{"type": "auth", "secret": secret, "platform": "tvos"}])
    await ws.websocket_endpoint(fake)
    assert fake.sent[1]["type"] == "upgrade_required"
    assert await _platform(db, client_id) == "tvos"


# ── the Android phone / tablet pair ─────────────────────────────────────────


@pytest.mark.parametrize(
    "paired, reported",
    [("android", "android_tablet"), ("android_tablet", "android")],
)
async def test_android_phone_and_tablet_are_accepted_stored_one_stays(
    db, paired, reported
):
    client_id, secret = await _device(db, paired)
    answer = await _auth(db, secret, platform=reported, app_version="2026.12.5")
    assert answer["type"] == "auth_ok"
    row = await db.fetchone(
        "SELECT platform, app_version FROM paired_clients WHERE id = ?", (client_id,)
    )
    assert (row["platform"], row["app_version"]) == (paired, "2026.12.5")
    assert _mismatches() == []


# ── audit trail ─────────────────────────────────────────────────────────────


async def test_mismatch_is_logged_once_per_interval(db, monkeypatch):
    client_id, secret = await _device(db, "watchos")
    other_id, other_secret = await _device(db, "tvos")
    for _ in range(3):
        assert _refused(await _auth(db, secret, platform="ios"))
    assert [e["client_id"] for e in _mismatches()] == [client_id]
    # Another device has its own interval.
    assert _refused(await _auth(db, other_secret, platform="ios"))
    assert [e["client_id"] for e in _mismatches()] == [other_id, client_id]
    # After the interval the next one is logged again.
    now = audit_log.time.monotonic()
    monkeypatch.setattr(
        audit_log.time,
        "monotonic",
        lambda: now + audit_log.CLIENT_INFO_MISMATCH_INTERVAL + 1,
    )
    assert _refused(await _auth(db, secret, platform="ios"))
    assert len(_mismatches()) == 3


async def test_mismatch_entry_carries_no_secret(db):
    _client_id, secret = await _device(db, "watchos")
    await _auth(db, secret, platform="ios")
    assert secret not in repr(_mismatches())


# ── legitimate updates ──────────────────────────────────────────────────────


async def test_version_build_model_keep_updating(db):
    client_id, secret = await _device(
        db, "ios", app_version="2026.12.0", app_build="5", device_model="iPhone17,1"
    )
    answer = await _auth(
        db,
        secret,
        platform="ios",
        app_version="2026.12.1",
        app_build="7",
        device_model="iPhone18,1",
    )
    assert answer["type"] == "auth_ok"
    row = await db.fetchone(
        "SELECT platform, app_version, app_build, device_model "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert dict(row) == {
        "platform": "ios",
        "app_version": "2026.12.1",
        "app_build": "7",
        "device_model": "iPhone18,1",
    }
    assert "app_updated" in [e["event"] for e in await _events(db, client_id)]


async def test_floor_of_the_paired_platform_applies(db, monkeypatch):
    monkeypatch.setitem(protocol.MIN_APP_VERSION, "android_tablet", "2026.12.0")
    _client_id, secret = await _device(db, "android_tablet")
    answer = await _auth(db, secret, platform="android", app_version="2026.1.0")
    assert answer["type"] == "upgrade_required"
    assert answer["reason"] == "app_version"


def test_same_device_platform_rule():
    for p in protocol.PLATFORMS:
        assert protocol.same_device_platform(p, p)
        assert protocol.same_device_platform(None, p)
    allowed = {
        (a, b)
        for a in protocol.PLATFORMS
        for b in protocol.PLATFORMS
        if a != b and protocol.same_device_platform(a, b)
    }
    assert allowed == {("android", "android_tablet"), ("android_tablet", "android")}


# ── other writers: none of them takes a platform from the device ────────────


async def test_client_status_never_writes_identity_columns(db):
    client_id, _secret = await _device(db, "watchos", name="Apple Watch")
    await ws_client._handle_client_status(
        db,
        client_id,
        {
            "platform": "ios",
            "name": "Mum's iPhone",
            "device_model": "iPhone18,1",
            "access": "member",
            "set_up_by_client_id": "x",
            "profile_id": "x",
            "device_name": "Watch",
        },
    )
    row = await db.fetchone(
        "SELECT platform, name, device_model, device_name "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert dict(row) == {
        "platform": "watchos",
        "name": "Apple Watch",
        "device_model": None,
        "device_name": "Watch",
    }


async def test_auth_never_changes_name_guest_flag_or_set_up_by(db):
    phone, _ = await _device(db, "ios", name="iPhone Child One")
    client_id, secret = await _device(
        db,
        "watchos",
        name="Apple Watch",
        set_up_by_client_id=phone,
        set_up_by_name="iPhone Child One",
        set_up_method="onboard",
    )
    answer = await _auth(
        db,
        secret,
        platform="watchos",
        client_id=phone,
        client_name="Mum's iPhone",
        name="Mum's iPhone",
        is_guest=True,
        access="guest",
        set_up_by="x",
        set_up_by_client_id="x",
    )
    assert answer["type"] == "auth_ok" and answer["client_id"] == client_id
    row = await db.fetchone(
        "SELECT name, access, set_up_by_client_id, set_up_by_name "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert dict(row) == {
        "name": "Apple Watch",
        "access": "member",
        "set_up_by_client_id": phone,
        "set_up_by_name": "iPhone Child One",
    }
