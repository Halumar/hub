"""Protocol version, client info per device, activity feed.

* Compatibility rule: the hub supports at least the two previous protocol
  versions (``MIN_PROTOCOL_VERSION <= max(1, PROTOCOL_VERSION - 2)``).
* ``auth``/``pair`` carry the client info; a too old app gets
  ``upgrade_required`` + close 4426 (pairing kept, PIN not consumed), a
  malformed one ``auth_error``/``pair_error``.
* ``auth_ok``, ``pair_ok``, ``/api/health`` and Bonjour TXT publish the
  protocol version.
* The client info is stored per device and shown in the Admin API
  (``update_required``); the activity feed is a ring buffer per device.
"""

import json
import sqlite3
import types
import uuid

import api.integration as apiintg
import api.websocket as ws
import pytest

from .profile_helpers import add_client
from api import pairing
from api.integration import ClientTokenDelivery
from fastapi import HTTPException, WebSocketDisconnect
from models.database import Database
from services import client_activity, crypto, db_migrations, mdns, protocol
from starlette.websockets import WebSocketState

CLIENT_INFO = {
    "protocol_version": 1,
    "platform": "ios",
    "app_version": "2026.12.0",
    "app_build": "6",
    "device_model": "iPhone18,1",
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()
    events = []

    async def _capture(message, *a, **k):
        events.append(message)

    async def _no_send(*a, **k):
        return False

    monkeypatch.setattr(ws, "broadcast", _capture)
    monkeypatch.setattr(ws, "send_to_client", _no_send)
    yield events
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()
    crypto.reset_cache()


class _WS:
    """Fake WebSocket that can drive websocket_endpoint."""

    def __init__(self, db, messages=(), host="192.0.2.10"):
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
            return json.dumps(self._messages.pop(0))
        raise WebSocketDisconnect()


async def _client(db, *, secret=None, **columns):
    secret = secret or uuid.uuid4().hex
    client_id = await add_client(
        db, name="TV", secret=secret, last_seen=Database.now(), **columns
    )
    return client_id, secret


def _request(db, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


async def _events(db, client_id):
    return [e["event"] for e in await client_activity.list_events(db, client_id)]


# ── Rules and parsing ────────────────────────────────────────────────────


class TestRules:
    def test_hub_supports_at_least_two_previous_versions(self):
        pv = protocol.PROTOCOL_VERSION
        assert 1 <= protocol.MIN_PROTOCOL_VERSION <= pv
        assert protocol.SUPPORTED_PREVIOUS_VERSIONS >= 2
        assert protocol.MIN_PROTOCOL_VERSION <= max(
            1, pv - protocol.SUPPORTED_PREVIOUS_VERSIONS
        )

    def test_min_app_versions_are_known_platforms_and_valid(self):
        for platform, version in protocol.MIN_APP_VERSION.items():
            assert platform in protocol.PLATFORMS
            assert protocol.is_valid_app_version(version)

    def test_platforms_match_the_contract(self):
        """Platform list and default profile slots are the contract's."""
        import json
        from pathlib import Path

        from services.profiles import catalog as cat

        defs = json.loads(
            (Path(cat.DATA_DIR) / "schemas" / "defs.json").read_text("utf-8")
        )["$defs"]
        assert list(protocol.PLATFORMS) == defs["platform"]["enum"]
        assert list(cat.DEFAULT_SLOTS) == defs["default_profile_slot"]["enum"]
        for platform in protocol.PLATFORMS:
            assert protocol._STORE_HINT[platform]
        assert "Google Play" in protocol._STORE_HINT["android_tv"]

    def test_fields(self):
        fields = protocol.protocol_fields()
        assert fields == {
            "protocol_version": protocol.PROTOCOL_VERSION,
            "min_protocol_version": protocol.MIN_PROTOCOL_VERSION,
            "min_app_version": protocol.MIN_APP_VERSION,
        }
        fields["min_app_version"]["ios"] = "0"
        assert protocol.MIN_APP_VERSION["ios"] != "0", "a copy, not the constant"
        assert protocol.txt_fields() == {"pv": "1", "pv_min": "1"}

    @pytest.mark.parametrize(
        "a,b",
        [
            ("2026.11.9", "2026.12.0"),
            ("2026.12.0", "2026.12.1"),
            ("2026.9", "2026.10"),
            ("1.9.9", "2026.1"),
        ],
    )
    def test_version_order(self, a, b):
        assert protocol.version_key(a) < protocol.version_key(b)

    def test_version_equal_forms(self):
        assert protocol.version_key("2026.12") == protocol.version_key("2026.12.0")
        assert protocol.version_key("2026.12.0-dev") == protocol.version_key(
            "2026.12.0"
        )


class TestEvaluate:
    def test_complete_info_is_ok(self):
        ev = protocol.evaluate(CLIENT_INFO)
        assert ev.ok
        assert ev.info.columns() == {
            "protocol_version": 1,
            "platform": "ios",
            "app_version": "2026.12.0",
            "app_build": "6",
            "device_model": "iPhone18,1",
        }

    @pytest.mark.parametrize("pv", [None, 0, -1, "1", 1.0, True])
    def test_missing_or_invalid_protocol_version_needs_upgrade(self, pv):
        msg = {k: v for k, v in CLIENT_INFO.items() if k != "protocol_version"}
        if pv is not None:
            msg["protocol_version"] = pv
        ev = protocol.evaluate(msg)
        assert ev.upgrade_reason == "protocol"

    def test_old_app_without_any_info_needs_upgrade(self):
        ev = protocol.evaluate({"type": "auth", "secret": "x"})
        assert ev.upgrade_reason == "protocol"
        assert ev.info.platform is None

    def test_protocol_below_minimum(self, monkeypatch):
        monkeypatch.setattr(protocol, "MIN_PROTOCOL_VERSION", 2)
        assert protocol.evaluate(CLIENT_INFO).upgrade_reason == "protocol"

    def test_newer_protocol_is_accepted(self):
        assert protocol.evaluate({**CLIENT_INFO, "protocol_version": 7}).ok

    def test_app_below_platform_floor(self):
        ev = protocol.evaluate({**CLIENT_INFO, "app_version": "2026.11.3"})
        assert ev.upgrade_reason == "app_version"

    def test_platform_without_floor(self):
        assert protocol.evaluate(
            {**CLIENT_INFO, "platform": "web", "app_version": "1.0-dev"}
        ).ok

    @pytest.mark.parametrize(
        "patch,field",
        [
            ({"platform": "visionos"}, "platform"),
            ({"platform": None}, "platform"),
            ({"app_version": "v2026"}, "app_version"),
            ({"app_version": 2026}, "app_version"),
            ({"app_build": "6 beta"}, "app_build"),
            ({"device_model": "x" * 65}, "device_model"),
            ({"device_model": "iPhone\n"}, "device_model"),
        ],
    )
    def test_malformed_info_is_an_error(self, patch, field):
        ev = protocol.evaluate({**CLIENT_INFO, **patch})
        assert ev.upgrade_reason is None
        assert ev.error and field in ev.error

    def test_upgrade_message(self):
        ev = protocol.evaluate(
            {**CLIENT_INFO, "platform": "tvos", "app_version": "1.0"}
        )
        msg = protocol.upgrade_required_message(ev)
        assert msg["type"] == "upgrade_required"
        assert msg["reason"] == "app_version"
        assert msg["platform"] == "tvos"
        assert "App Store" in msg["message"]
        assert msg["protocol_version"] == protocol.PROTOCOL_VERSION
        assert msg["min_app_version"] == protocol.MIN_APP_VERSION

    def test_update_required_hint(self):
        assert protocol.update_required({}) is True
        assert protocol.update_required(CLIENT_INFO) is False
        assert protocol.update_required({**CLIENT_INFO, "app_version": "2026.1.0"})
        assert not protocol.update_required(
            {**CLIENT_INFO, "platform": "android", "app_version": "1.0"}
        )


# ── WebSocket handshake ──────────────────────────────────────────────────


class TestAuth:
    async def test_auth_ok_carries_protocol_and_stores_info(self, db, _clean_state):
        # The platform comes from pairing; auth stores the rest.
        client_id, secret = await _client(db, platform="ios")
        fake = _WS(db, [{"type": "auth", "secret": secret, **CLIENT_INFO}])
        await ws.websocket_endpoint(fake)
        ok = fake.sent[1]
        assert ok["type"] == "auth_ok" and ok["client_id"] == client_id
        assert ok["protocol_version"] == 1 and ok["min_protocol_version"] == 1
        assert ok["min_app_version"] == protocol.MIN_APP_VERSION
        row = await db.fetchone(
            "SELECT * FROM paired_clients WHERE id = ?", (client_id,)
        )
        assert (row["platform"], row["app_version"], row["app_build"]) == (
            "ios",
            "2026.12.0",
            "6",
        )
        assert (row["device_model"], row["protocol_version"]) == ("iPhone18,1", 1)
        assert await _events(db, client_id) == ["disconnected", "connected"]
        activity = [m for m in _clean_state if m.get("type") == "device_activity"]
        assert [m["data"]["event"] for m in activity] == ["connected", "disconnected"]

    async def test_app_update_is_recorded(self, db):
        client_id, secret = await _client(db, app_version="2026.12.0", platform="ios")
        await ws.websocket_endpoint(
            _WS(
                db,
                [
                    {
                        "type": "auth",
                        "secret": secret,
                        **CLIENT_INFO,
                        "app_version": "2026.12.1",
                    }
                ],
            )
        )
        events = await client_activity.list_events(db, client_id)
        assert [e["event"] for e in events] == [
            "disconnected",
            "connected",
            "app_updated",
        ]
        assert events[2]["detail"] == {
            "previous_app_version": "2026.12.0",
            "app_version": "2026.12.1",
        }

    async def test_old_app_gets_upgrade_required_and_close_4426(self, db, _clean_state):
        client_id, secret = await _client(
            db, platform="tvos", app_version="2026.12.0", protocol_version=1
        )
        fake = _WS(db, [{"type": "auth", "secret": secret}, {"type": "get_areas"}])
        await ws.websocket_endpoint(fake)
        assert [m["type"] for m in fake.sent] == ["auth_required", "upgrade_required"]
        assert fake.sent[1]["reason"] == "protocol"
        assert fake.sent[1]["platform"] is None
        assert fake.closed == (4426, "Upgrade required")
        assert fake not in ws._clients
        assert not any(m.get("type") == "client_connected" for m in _clean_state)
        # pairing kept, what it reported is stored for the Admin UI
        row = await db.fetchone(
            "SELECT * FROM paired_clients WHERE id = ?", (client_id,)
        )
        assert row["is_active"] == 1
        assert row["protocol_version"] is None and row["app_version"] is None
        events = await client_activity.list_events(db, client_id)
        assert [e["event"] for e in events] == ["upgrade_required"]
        assert events[0]["detail"] == {"reason": "protocol"}

    async def test_app_below_floor_gets_upgrade_required(self, db):
        client_id, secret = await _client(db)
        fake = _WS(
            db,
            [
                {
                    "type": "auth",
                    "secret": secret,
                    **CLIENT_INFO,
                    "app_version": "2026.11.0",
                }
            ],
        )
        await ws.websocket_endpoint(fake)
        assert fake.sent[1]["type"] == "upgrade_required"
        assert fake.sent[1]["reason"] == "app_version"
        assert fake.sent[1]["platform"] == "ios"
        assert fake.closed[0] == 4426
        detail = (await client_activity.list_events(db, client_id))[0]["detail"]
        assert detail == {
            "reason": "app_version",
            "app_version": "2026.11.0",
            "protocol_version": 1,
            "platform": "ios",
        }

    async def test_wrong_secret_stays_auth_error(self, db):
        fake = _WS(db, [{"type": "auth", "secret": "nope"}])
        await ws.websocket_endpoint(fake)
        assert fake.sent[1]["type"] == "auth_error"
        assert fake.closed is None

    async def test_malformed_info_is_auth_error(self, db):
        client_id, secret = await _client(db)
        fake = _WS(
            db,
            [{"type": "auth", "secret": secret, **CLIENT_INFO, "platform": "visionos"}],
        )
        await ws.websocket_endpoint(fake)
        assert fake.sent[1] == {
            "type": "auth_error",
            "reason": "invalid_client_info",
            "message": "Invalid client info: platform",
        }
        assert await _events(db, client_id) == []

    async def test_removed_device_still_gets_client_removed(self, db):
        _client_id, secret = await _client(db, pending_deletion_at=Database.now())
        fake = _WS(db, [{"type": "auth", "secret": secret}])
        await ws.websocket_endpoint(fake)
        assert fake.sent[1]["type"] == "client_removed"

    async def test_repeated_auth_carries_protocol(self, db):
        _client_id, secret = await _client(db)
        auth = {"type": "auth", "secret": secret, **CLIENT_INFO}
        fake = _WS(db, [auth, auth])
        await ws.websocket_endpoint(fake)
        assert fake.sent[2]["message"] == "Already authenticated"
        assert fake.sent[2]["protocol_version"] == protocol.PROTOCOL_VERSION


class TestPair:
    def _pin(self):
        pairing.set_active_pin("1234-5678", {"created_at": 0, "expires_at": 9e12})
        return "1234-5678"

    async def test_pair_ok_carries_protocol_and_stores_info(self, db, monkeypatch):
        async def _no_provision(*a, **k):
            return None

        monkeypatch.setattr(ws._device_provisioning, "request_provision", _no_provision)
        pin = self._pin()
        fake = _WS(
            db,
            [
                {
                    "type": "pair",
                    "pin": pin,
                    "client_name": "Panel",
                    **CLIENT_INFO,
                    "platform": "ipados",
                }
            ],
        )
        await ws.websocket_endpoint(fake)
        ok = fake.sent[1]
        assert ok["type"] == "pair_ok"
        assert ok["protocol_version"] == protocol.PROTOCOL_VERSION
        assert ok["min_app_version"] == protocol.MIN_APP_VERSION
        row = await db.fetchone(
            "SELECT * FROM paired_clients WHERE id = ?", (ok["client_id"],)
        )
        assert row["platform"] == "ipados" and row["protocol_version"] == 1
        events = await client_activity.list_events(db, ok["client_id"])
        assert [e["event"] for e in events] == ["disconnected", "paired"]
        assert events[1]["detail"] == {
            "platform": "ipados",
            "app_version": "2026.12.0",
            "method": "pin",
        }

    async def test_old_app_does_not_use_up_the_pin(self, db):
        pin = self._pin()
        fake = _WS(db, [{"type": "pair", "pin": pin, "client_name": "Old TV"}])
        await ws.websocket_endpoint(fake)
        assert fake.sent[1]["type"] == "upgrade_required"
        assert fake.closed[0] == 4426
        assert pairing.get_active_pin() is not None
        assert await db.fetchall("SELECT id FROM paired_clients") == []

    async def test_malformed_info_is_pair_error(self, db):
        pin = self._pin()
        fake = _WS(
            db,
            [
                {
                    "type": "pair",
                    "pin": pin,
                    "client_name": "X",
                    **CLIENT_INFO,
                    "app_version": "new",
                }
            ],
        )
        await ws.websocket_endpoint(fake)
        assert fake.sent[1] == {
            "type": "pair_error",
            "reason": "invalid_client_info",
            "message": "Invalid client info: app_version",
        }
        assert pairing.get_active_pin() is not None


# ── Health, Bonjour ──────────────────────────────────────────────────────


async def test_health_carries_protocol(db):
    from main import health_check

    body = await health_check(_request(db))
    assert body["protocol_version"] == protocol.PROTOCOL_VERSION
    assert body["min_protocol_version"] == protocol.MIN_PROTOCOL_VERSION
    assert body["min_app_version"] == protocol.MIN_APP_VERSION


def test_bonjour_txt_carries_pv(monkeypatch):
    from services import tls

    monkeypatch.setattr(tls, "get_fingerprint", lambda: None)
    txt = mdns.build_properties("iid-1", "2026.12.0")
    assert txt["pv"] == "1" and txt["pv_min"] == "1"
    assert all(isinstance(v, str) for v in txt.values())


# ── Admin API ────────────────────────────────────────────────────────────


class TestAdminApi:
    async def test_client_list_and_detail_show_client_info(self, db):
        fresh, _ = await _client(db, **CLIENT_INFO)
        old, _ = await _client(
            db, platform="tvos", app_version="2026.11.0", protocol_version=1
        )
        never, _ = await _client(db)
        listed = {c.id: c for c in await pairing.list_paired_clients(_request(db))}
        assert listed[fresh].platform == "ios"
        assert listed[fresh].app_version == "2026.12.0"
        assert listed[fresh].app_build == "6"
        assert listed[fresh].device_model == "iPhone18,1"
        assert listed[fresh].protocol_version == 1
        assert listed[fresh].update_required is False
        assert listed[old].update_required is True
        assert listed[never].update_required is True
        assert listed[never].platform is None
        detail = await pairing.get_client(_request(db), old)
        assert detail.platform == "tvos" and detail.update_required is True

    async def test_activity_endpoint(self, db):
        client_id, _ = await _client(db)
        for event in ("paired", "connected", "disconnected"):
            await client_activity.insert(db, client_id, event)
        result = await pairing.get_client_activity(_request(db), client_id, limit=2)
        assert result.client_id == client_id
        assert result.max_events == client_activity.MAX_EVENTS_PER_CLIENT
        assert [e.event for e in result.events] == ["disconnected", "connected"]
        assert result.events[0].id > result.events[1].id

    async def test_activity_unknown_client_is_404(self, db):
        with pytest.raises(HTTPException) as exc:
            await pairing.get_client_activity(_request(db), str(uuid.uuid4()), limit=10)
        assert exc.value.status_code == 404

    async def test_activity_requires_admin(self, db):
        client_id, _ = await _client(db)
        with pytest.raises(HTTPException) as exc:
            await pairing.get_client_activity(
                _request(db, auth="client"), client_id, limit=10
            )
        assert exc.value.status_code in (401, 403)

    async def test_route_is_registered(self):
        paths = {
            (r.path, m)
            for r in pairing.router.routes
            for m in getattr(r, "methods", ())
        }
        assert ("/pairing/clients/{client_id}/activity", "GET") in paths


# ── Activity ring buffer ─────────────────────────────────────────────────


class TestActivity:
    async def test_ring_buffer_keeps_newest(self, db, monkeypatch):
        monkeypatch.setattr(client_activity, "MAX_EVENTS_PER_CLIENT", 5)
        client_id, _ = await _client(db)
        other, _ = await _client(db)
        await client_activity.insert(db, other, "paired")
        for i in range(8):
            await client_activity.insert(
                db, client_id, "connected", {"app_version": f"1.{i}"}
            )
        events = await client_activity.list_events(db, client_id)
        assert [e["detail"]["app_version"] for e in events] == [
            f"1.{i}" for i in (7, 6, 5, 4, 3)
        ]
        assert await _events(db, other) == ["paired"], "other devices untouched"

    async def test_events_go_with_the_device(self, db):
        client_id, _ = await _client(db)
        await client_activity.insert(db, client_id, "paired")
        async with db.transaction():
            await db.execute("DELETE FROM paired_clients WHERE id = ?", (client_id,))
        assert await db.fetchall("SELECT id FROM client_activity") == []

    async def test_unknown_device_or_event(self, db):
        assert await client_activity.record(db, str(uuid.uuid4()), "paired") is None
        client_id, _ = await _client(db)
        with pytest.raises(ValueError):
            await client_activity.insert(db, client_id, "exploded")
        assert await client_activity.record(db, client_id, "exploded") is None

    async def test_none_values_are_dropped_from_detail(self, db):
        client_id, _ = await _client(db)
        entry = await client_activity.insert(
            db, client_id, "connected", {"app_version": "1", "app_build": None}
        )
        assert entry["detail"] == {"app_version": "1"}
        entry = await client_activity.insert(db, client_id, "disconnected", {"x": None})
        assert entry["detail"] is None

    async def test_token_events_from_the_integration(self, db):
        client_id, _ = await _client(db, ha_token_status="pending")
        request = _request(db, "integration")
        await apiintg.integration_client_token(
            request,
            ClientTokenDelivery(
                client_id=client_id,
                ha_user_id="u-1",
                refresh_token_id="rt-1",
                token="t-1",
            ),
        )
        await apiintg.integration_client_token(
            request,
            ClientTokenDelivery(
                client_id=client_id,
                ha_user_id="u-1",
                refresh_token_id="rt-2",
                token="t-2",
            ),
        )
        assert await _events(db, client_id) == ["token_rotated", "token_issued"]


# ── Migration v2 → v3 ────────────────────────────────────────────────────


def _v2_schema() -> str:
    from pathlib import Path

    sql = (Path(__file__).parent / "fixtures" / "schema_v8.sql").read_text()
    start = sql.index("CREATE TABLE IF NOT EXISTS client_activity")
    end = sql.index("CREATE TABLE IF NOT EXISTS client_notifications")
    sql = sql[:start] + sql[end:]
    for line in (
        "    app_build TEXT,\n",
        "    platform TEXT,\n",
        "    device_model TEXT,\n",
        "    protocol_version INTEGER,\n",
        "    tile TEXT,\n",
    ):
        assert line in sql
        sql = sql.replace(line, "", 1)
    return sql


async def test_migration_v2_to_latest_keeps_devices(tmp_path):
    path = str(tmp_path / "hub.db")
    conn = sqlite3.connect(path)
    conn.executescript(_v2_schema())
    conn.execute(
        "INSERT INTO paired_clients (id, name, secret_hash, created_at, app_version) "
        "VALUES ('c1', 'TV', 'h', '2026-11-18T00:00:00+00:00', '2026.12.0')"
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    db = Database(path)
    await db.initialize()
    try:
        assert (await db.fetchone("PRAGMA user_version"))[
            "user_version"
        ] == db_migrations.latest_version()
        row = await db.fetchone("SELECT * FROM paired_clients WHERE id = 'c1'")
        assert row["app_version"] == "2026.12.0" and row["platform"] is None
        await client_activity.insert(db, "c1", "connected")
        assert await _events(db, "c1") == ["connected"]
    finally:
        await db.close()

    fresh = Database(str(tmp_path / "fresh.db"))
    await fresh.initialize()
    await fresh.close()

    def _columns(p, table):
        c = sqlite3.connect(p)
        try:
            return sorted(r[1] for r in c.execute(f"PRAGMA table_info({table})"))
        finally:
            c.close()

    for table in ("paired_clients", "client_activity"):
        assert _columns(path, table) == _columns(str(tmp_path / "fresh.db"), table)
