"""Auth and session details, error reasons, deleting offline devices, area lists.

* ``auth_error``/``pair_error`` carry a machine-readable reason.
* API docs admin only, local assets, strict CSP with hashes.
* Limits of client_status values.
* Token stored only for active devices, delete before close,
  authz fail closed.
* Integration token only from this host, client-token bound to a
  request or the stored HA user.
* Log range header. No guest_url_mode, guests never get
  the external URL. Logout needs the CSRF token.
* PIN hash only for devices with an active settings lock.
* Removal of an offline device (on_reconnect | immediate).
* ``areas_changed`` to paired apps by visibility.
"""

import asyncio
import hashlib
import json
import os
import types
import uuid

import pytest
from .profile_helpers import add_client
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

import api.integration as apiintg
import api.websocket as ws
from api import auth, authz, pairing
from api.pairing import clients as pairing_clients
from models.database import Database
from models.schemas import ClientTokenDelivery
from services import crypto, csp, integration_source
from services import device_provisioning as dp

CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}
TOKEN = "eyJ.sec2-device-token.FAKE"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    integration_source.reset_cache()
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()
    events: list[dict] = []

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
    dp.reset()


@pytest.fixture
def sent(monkeypatch):
    messages: list[dict] = []

    async def fake_sender(message):
        messages.append(message)
        return True

    monkeypatch.setattr(dp, "_sender", fake_sender)
    return messages


class _WS:
    def __init__(self, db, messages=(), host="192.0.2.10"):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host=host)
        self.url = types.SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []
        self.text = []
        self.closed = None
        self.application_state = WebSocketState.CONNECTING
        self._messages = list(messages)

    async def accept(self):
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, payload):
        self.text.append(json.loads(payload))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED

    async def receive_text(self):
        if self._messages:
            return json.dumps(self._messages.pop(0))
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect()


async def _client(db, *, secret=None, **columns):
    secret = secret or uuid.uuid4().hex
    columns.setdefault("remote_allowed", 0)
    client_id = await add_client(
        db, name="Test device", secret=secret, last_seen=Database.now(), **columns
    )
    return client_id, secret


async def _area(db, tags=(), is_active=1):
    area_id = str(uuid.uuid4())
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, "
            "updated_at) VALUES (?, ?, 0, ?, ?, ?)",
            (area_id, f"A-{area_id[:6]}", is_active, now, now),
        )
        for tag in tags:
            await db.execute(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
            )
    return area_id


def _request(db, auth_type="ingress", ha_client=None):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth_type),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha_client)
        ),
        client=None,
    )


async def _auth(db, secret):
    fake = _WS(db, [{"type": "auth", "secret": secret, **CLIENT_INFO}])
    await ws.websocket_endpoint(fake)
    return fake.sent[1]


# ── auth_error reasons ────────────────────────────────────────


class TestAuthErrorReasons:
    async def test_unknown_secret(self, db):
        reply = await _auth(db, "nope")
        assert reply == {
            "type": "auth_error",
            "reason": "invalid_secret",
            "message": "Invalid client secret",
        }

    async def test_revoked(self, db):
        _cid, secret = await _client(db, is_active=0)
        reply = await _auth(db, secret)
        assert reply["reason"] == "revoked"

    async def test_guest_expired(self, db):
        cid, secret = await _client(
            db, is_guest=1, expires_at="2020-01-01T00:00:00+00:00"
        )
        reply = await _auth(db, secret)
        assert reply["reason"] == "guest_expired"
        assert (
            await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (cid,))
            is None
        )

    async def test_rate_limited(self, db):
        msgs = [{"type": "auth", "secret": "x", **CLIENT_INFO}] * (
            ws.MAX_AUTH_ATTEMPTS + 1
        )
        fake = _WS(db, msgs)
        await ws.websocket_endpoint(fake)
        assert fake.sent[-1]["reason"] == "rate_limited"
        assert {m["reason"] for m in fake.sent[1:-1]} == {"invalid_secret"}

    async def test_valid_secret_still_ok(self, db):
        cid, secret = await _client(db)
        reply = await _auth(db, secret)
        assert reply["type"] == "auth_ok" and reply["client_id"] == cid

    async def test_pair_reasons(self, db):
        fake = _WS(db)
        ws._clients[fake] = {"authenticated": False, "pair_attempts": 0}
        await ws._handle_message(
            fake,
            {"type": "pair", "pin": "", "client_name": "X", **CLIENT_INFO},
            False,
            None,
        )
        assert fake.sent[-1]["reason"] == "invalid_request"
        await ws._handle_message(
            fake,
            {"type": "pair", "pin": "1", "client_name": "X" * 101, **CLIENT_INFO},
            False,
            None,
        )
        assert fake.sent[-1]["reason"] == "invalid_request"
        await ws._handle_message(fake, {"type": "pair"}, True, "c")
        assert fake.sent[-1]["reason"] == "already_authenticated"
        evicted = _WS(db)
        await ws._handle_message(
            evicted, {"type": "pair", "pin": "1", "client_name": "X"}, False, None
        )
        assert evicted.sent[-1]["reason"] == "connection_expired"


# ── client_status limits ────────────────────────────────────────


class TestClientStatusLimits:
    async def test_values_are_cleaned_and_bounded(self, db):
        cid, _ = await _client(db)
        area_id = await _area(db)
        await ws._handle_client_status(
            db,
            cid,
            {
                "device_name": "D\x00\x1b[31m" + "x" * 5000,
                "connection_type": "wifi" * 50,
                "battery_level": 70.4,
                "latitude": 47.3,
                "longitude": 8.5,
                "current_area_id": area_id,
            },
        )
        row = await db.fetchone("SELECT * FROM paired_clients WHERE id = ?", (cid,))
        assert len(row["device_name"]) == 100
        assert "\x00" not in row["device_name"] and "\x1b" not in row["device_name"]
        assert len(row["connection_type"]) == 32
        assert row["battery_level"] == 70
        assert row["current_area_id"] == area_id

    @pytest.mark.parametrize(
        "field,value",
        [
            ("battery_level", 101),
            ("battery_level", True),
            ("battery_level", "50"),
            ("latitude", 91.0),
            ("longitude", -181.0),
            ("latitude", float("nan")),
            ("current_area_id", "not-an-area"),
            ("current_area_id", "x" * 65),
            ("device_name", 42),
        ],
    )
    async def test_invalid_values_are_skipped(self, db, field, value):
        cid, _ = await _client(db)
        await ws._handle_client_status(db, cid, {field: value})
        row = await db.fetchone(
            f"SELECT {field} FROM paired_clients WHERE id = ?", (cid,)
        )
        assert row[field] is None

    async def test_home_marker_is_accepted(self, db):
        cid, _ = await _client(db)
        await ws._handle_client_status(db, cid, {"current_area_id": "__home__"})
        row = await db.fetchone(
            "SELECT current_area_id FROM paired_clients WHERE id = ?", (cid,)
        )
        assert row["current_area_id"] == "__home__"


# ── token delivery ────────────────────────────────────


def _delivery(client_id, *, user="u-1", rt="rt-1", request_id=None):
    return ClientTokenDelivery(
        client_id=client_id,
        ha_user_id=user,
        refresh_token_id=rt,
        token=TOKEN,
        request_id=request_id,
    )


class TestClientTokenBinding:
    async def test_pending_device_accepts_any_user(self, db):
        cid, _ = await _client(db, ha_token_status="pending")
        result = await apiintg.integration_client_token(
            _request(db, "integration"), _delivery(cid)
        )
        assert result["ha_token_status"] == "active"

    async def test_same_user_rotation_unsolicited(self, db):
        cid, _ = await _client(db, ha_token_status="pending")
        await db.store_client_token(
            cid, ha_user_id="u-1", refresh_token_id="rt-0", token="t"
        )
        result = await apiintg.integration_client_token(
            _request(db, "integration"), _delivery(cid, rt="rt-2")
        )
        assert result["unchanged"] is False

    async def test_foreign_user_without_request_is_refused(self, db):
        cid, _ = await _client(db, ha_token_status="pending")
        await db.store_client_token(
            cid, ha_user_id="u-1", refresh_token_id="rt-0", token="t"
        )
        with pytest.raises(HTTPException) as exc:
            await apiintg.integration_client_token(
                _request(db, "integration"), _delivery(cid, user="u-evil", rt="rt-9")
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "request_required"
        row = await db.fetchone(
            "SELECT ha_user_id FROM paired_clients WHERE id = ?", (cid,)
        )
        assert row["ha_user_id"] == "u-1"

    async def test_foreign_user_with_open_request(self, db):
        cid, _ = await _client(db, ha_token_status="pending")
        await db.store_client_token(
            cid, ha_user_id="u-1", refresh_token_id="rt-0", token="t"
        )
        request_id = await dp.start_rotation(db, cid)
        result = await apiintg.integration_client_token(
            _request(db, "integration"),
            _delivery(cid, user="u-2", rt="rt-3", request_id=request_id),
        )
        assert result["ha_token_status"] == "active"

    async def test_store_refuses_inactive_device(self, db):
        """The UPDATE itself checks active + not pending deletion."""
        cid, _ = await _client(db, ha_token_status="pending", is_active=0)
        assert (
            await db.store_client_token(
                cid, ha_user_id="u", refresh_token_id="r", token="t"
            )
            is False
        )
        deleted, _ = await _client(
            db, ha_token_status="pending", pending_deletion_at=Database.now()
        )
        assert (
            await db.store_client_token(
                deleted, ha_user_id="u", refresh_token_id="r", token="t"
            )
            is False
        )

    async def test_revoke_between_check_and_store_is_409(self, db, monkeypatch):
        cid, _ = await _client(db, ha_token_status="pending")
        original = db.store_client_token

        async def revoke_then_store(*a, **k):
            async with db.transaction():
                await db.execute(
                    "UPDATE paired_clients SET is_active = 0 WHERE id = ?", (cid,)
                )
            return await original(*a, **k)

        monkeypatch.setattr(db, "store_client_token", revoke_then_store)
        with pytest.raises(HTTPException) as exc:
            await apiintg.integration_client_token(
                _request(db, "integration"), _delivery(cid)
            )
        assert exc.value.status_code == 409
        row = await db.fetchone(
            "SELECT ha_token_status FROM paired_clients WHERE id = ?", (cid,)
        )
        assert row["ha_token_status"] == "pending"


class TestIntegrationSource:
    def test_not_enforced_outside_the_supervisor(self):
        assert integration_source.source_allowed("203.0.113.9") is True

    def test_only_own_addresses_under_the_supervisor(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
        assert integration_source.source_allowed("127.0.0.1") is True
        assert integration_source.source_allowed("::1") is True
        assert integration_source.source_allowed("203.0.113.9") is False
        assert integration_source.source_allowed("172.30.33.5") is False
        assert integration_source.source_allowed("not-an-ip") is False
        assert integration_source.source_allowed(None) is False

    def test_local_interface_address_is_allowed(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
        monkeypatch.setattr(
            integration_source, "_is_local_address", lambda ip: str(ip) == "10.0.0.5"
        )
        assert integration_source.source_allowed("10.0.0.5") is True
        assert integration_source.source_allowed("10.0.0.6") is False

    def test_dev_flag_disables_the_check(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
        monkeypatch.setenv("HUB_DEV", "1")
        assert integration_source.source_allowed("203.0.113.9") is True

    async def test_middleware_refuses_foreign_integration_token(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "x")
        token = "integration-token-sec2"

        class _DB:
            async def get_setting(self, key):
                if key == "integration_token_hash":
                    return auth.hash_secret(token)
                return None

            async def fetchall(self, sql, params=()):
                return [{"value": auth.hash_secret(token)}]

        from starlette.requests import Request

        def _req(ip):
            return Request(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "method": "GET",
                    "path": "/api/integration/status",
                    "raw_path": b"/api/integration/status",
                    "query_string": b"",
                    "headers": [(b"authorization", f"Bearer {token}".encode())],
                    "client": (ip, 1234),
                    "server": ("hub", 8099),
                    "scheme": "http",
                    "app": types.SimpleNamespace(state=types.SimpleNamespace(db=_DB())),
                }
            )

        async def _next(request):
            return "PASSED"

        mw = auth.AuthMiddleware(app=None)
        refused = await mw.dispatch(_req("203.0.113.9"), _next)
        assert refused.status_code == 403
        assert await mw.dispatch(_req("127.0.0.1"), _next) == "PASSED"


# ── kick order, authz fail closed ───────────────────────────────


class TestFailClosed:
    async def test_missing_or_inactive_client_sees_nothing(self, db):
        await _area(db)
        assert await authz.client_area_scope(db, "no-such-client") == authz.NO_AREAS
        assert await authz.listed_area_ids(db, "no-such-client") == set()
        revoked, _ = await _client(db, is_active=0)
        assert await authz.listed_area_ids(db, revoked) == set()
        member, _ = await _client(db)
        assert len(await authz.listed_area_ids(db, member)) == 1

    async def test_kick_deletes_before_closing(self, db, monkeypatch, sent):
        cid, _ = await _client(db)
        seen = {}

        async def _disconnect(client_id, code, reason):
            seen["row"] = await db.fetchone(
                "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
            )
            return 1

        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        await pairing_clients.kick_client(_request(db), cid)
        assert seen["row"] is None


# ── deleting an offline device ────────────────────────────────────


async def _held(db):
    return await dp.held_user_ids(db)


class TestOfflineDelete:
    async def _delete(self, db, cid, removal=None):
        await pairing_clients.delete_client(_request(db), cid, ha_user_removal=removal)

    async def test_unknown_remote_access_removes_at_once(self, db, sent, monkeypatch):
        """Rules that cannot be resolved: the delete does not fail and takes
        the stronger removal (the HA user goes at once)."""
        from services.profiles import store as profiles

        monkeypatch.setattr(ws, "signal_client_removed", _zero)
        cid, _ = await _client(db, remote_allowed=0)

        async def _broken(*_a, **_k):
            raise ValueError("unknown profile body version 9")

        monkeypatch.setattr(profiles, "remote_allowed_map", _broken)
        await self._delete(db, cid)
        assert cid not in await _held(db)
        assert pairing_clients.recommended_ha_user_removal(None) == "immediate"

    async def test_default_home_only_holds_until_reconnect(self, db, sent, monkeypatch):
        monkeypatch.setattr(ws, "signal_client_removed", _zero)
        cid, _ = await _client(db, remote_allowed=0)
        await self._delete(db, cid)
        assert cid in await _held(db)
        assert sent == []

    async def test_default_remote_device_removes_at_once(self, db, sent, monkeypatch):
        monkeypatch.setattr(ws, "signal_client_removed", _zero)
        cid, _ = await _client(db, remote_allowed=1)
        await self._delete(db, cid)
        assert cid not in await _held(db)
        assert sent[-1] == {
            "type": "deprovision_client",
            "data": {"client_id": cid, "reason": "deleted"},
        }
        row = await db.fetchone(
            "SELECT pending_deletion_at FROM paired_clients WHERE id = ?", (cid,)
        )
        assert row["pending_deletion_at"], "tombstone stays for the app reset"

    @pytest.mark.parametrize("remote", [0, 1])
    async def test_explicit_choice_wins(self, db, sent, monkeypatch, remote):
        monkeypatch.setattr(ws, "signal_client_removed", _zero)
        a, _ = await _client(db, remote_allowed=remote)
        b, _ = await _client(db, remote_allowed=remote)
        await self._delete(db, a, "immediate")
        await self._delete(db, b, "on_reconnect")
        held = await _held(db)
        assert a not in held and b in held

    async def test_online_device_ignores_the_choice(self, db, sent, monkeypatch):
        async def _one(_cid):
            return 1

        spawned = []
        monkeypatch.setattr(ws, "signal_client_removed", _one)
        monkeypatch.setattr(
            dp, "spawn", lambda coro: spawned.append(coro) or coro.close()
        )
        cid, _ = await _client(db, remote_allowed=1)
        await self._delete(db, cid, "immediate")
        assert cid in await _held(db)  # released after the reset ack window
        assert len(spawned) == 1
        assert sent == []

    def test_recommendation(self):
        assert pairing_clients.recommended_ha_user_removal(True) == "immediate"
        assert pairing_clients.recommended_ha_user_removal(False) == "on_reconnect"

    async def test_offline_immediate_stays_listed_as_waiting(
        self, db, sent, monkeypatch
    ):
        """204 for an offline device with ``immediate`` removes the HA
        user now, but the row stays as a listed tombstone (``pending_deletion``)
        until the app returns; dismiss-pending hides it, the tombstone still
        resets the returning app."""
        monkeypatch.setattr(ws, "signal_client_removed", _zero)
        cid, secret = await _client(db, remote_allowed=0)
        await self._delete(db, cid, "immediate")
        assert cid not in await _held(db)
        listed = {
            c.id: c for c in await pairing_clients.list_paired_clients(_request(db))
        }
        assert listed[cid].pending_deletion and not listed[cid].is_active
        await pairing_clients.dismiss_pending_client(_request(db), cid)
        listed = await pairing_clients.list_paired_clients(_request(db))
        assert cid not in {c.id for c in listed}
        assert await ws._match_pending_deletion(db, secret) == cid

    def test_delete_documents_the_listed_tombstone(self):
        from main import app

        op = app.openapi()["paths"]["/api/pairing/clients/{client_id}/permanent"][
            "delete"
        ]
        assert "pending_deletion: true" in op["description"]
        assert "dismiss-pending" in op["description"]

    def test_query_parameter_in_openapi(self):
        from main import app

        op = app.openapi()["paths"]["/api/pairing/clients/{client_id}/permanent"][
            "delete"
        ]
        param = next(p for p in op["parameters"] if p["name"] == "ha_user_removal")
        assert param["in"] == "query"
        schema = json.dumps(param["schema"])
        assert "on_reconnect" in schema and "immediate" in schema


async def _zero(_cid):
    return 0


# ── client_config ────────────────────────────────────


class TestClientConfig:
    async def test_pin_hash_only_where_needed(self, db):
        """The hash only goes to a device whose settings open
        with the remote PIN (rule app_settings = pin); without a PIN the rule
        falls back to hidden (prerequisite)."""
        pin = {"settings_pin_hash": "a" * 64, "settings_pin_salt": "b" * 32}
        pin_rule_no_pin, _ = await _client(db, overrides={"app_settings": "pin"})
        open_with_pin, _ = await _client(db, **pin)
        locked, _ = await _client(db, overrides={"app_settings": "pin"}, **pin)
        hidden, _ = await _client(db, overrides={"app_settings": "hidden"}, **pin)
        cfg = await ws._get_client_config(db, pin_rule_no_pin)
        assert cfg["rules"]["app_settings"]["value"] == "hidden"
        assert cfg["rules"]["app_settings"]["reason"] == "prerequisite_missing"
        assert cfg["settings_pin_hash"] is None
        cfg = await ws._get_client_config(db, open_with_pin)
        assert cfg["rules"]["app_settings"]["value"] == "open"
        assert cfg["settings_pin_hash"] is None
        cfg = await ws._get_client_config(db, locked)
        assert cfg["settings_pin_hash"] == "a" * 64
        assert cfg["settings_pin_salt"] == "b" * 32
        assert cfg["settings_pin_source"] == "device"
        cfg = await ws._get_client_config(db, hidden)
        assert cfg["settings_pin_hash"] is None and cfg["settings_pin_salt"] is None

    async def test_list_carries_adjustments(self, db):
        """The clients list counts the device adjustments."""
        cid, _ = await _client(db, overrides={"search_filter": False})
        rows = await pairing_clients.list_paired_clients(_request(db))
        mine = next(r for r in rows if r.id == cid)
        assert mine.override_count == 2  # remote_access (helper) + search_filter

    async def test_external_url_follows_remote_access(self, db):
        """Guests too may have remote access when their profile says so."""
        await db.set_setting("ha_url", "https://ha.example.com")
        guest, _ = await _client(db, is_guest=1, expires_at="2099-01-01T00:00:00+00:00")
        guest_remote, _ = await _client(
            db, is_guest=1, expires_at="2099-01-01T00:00:00+00:00", remote_allowed=1
        )
        member, _ = await _client(db, remote_allowed=1)
        assert (await ws._get_client_config(db, guest))["ha_url"] is None
        assert (await ws._get_client_config(db, guest_remote))["ha_url"] == (
            "https://ha.example.com"
        )
        assert (await ws._get_client_config(db, member))["ha_url"] == (
            "https://ha.example.com"
        )


# ── docs and CSP ──────────────────────────────────────


class _DocsDB:
    async def get_setting(self, key):
        return None

    async def fetchone(self, *a, **k):
        return None


@pytest.fixture
def docs_client(monkeypatch, direct_admin_on):
    import main

    monkeypatch.setattr(main, "_read_addon_options", lambda: {})
    monkeypatch.setattr(main.app.state, "db", _DocsDB(), raising=False)
    monkeypatch.setattr(main.app.state, "startup_error", None, raising=False)
    client = TestClient(main.app, base_url="https://hub.local:8100")
    yield client, main
    auth._sessions.clear()


def _session_headers():
    token = auth._create_session("admin", "pytest-ua")
    return {
        "cookie": f"{auth.SESSION_COOKIE_NAME}={token}",
        "user-agent": "pytest-ua",
    }


class TestDocs:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/docs",
            "/api/redoc",
            "/api/openapi.json",
            "/api/docs/assets/swagger-ui-bundle.js",
        ],
    )
    def test_no_public_docs(self, docs_client, path):
        client, _ = docs_client
        assert client.get(path).status_code == 401

    def test_admin_gets_local_docs_with_strict_csp(self, docs_client):
        client, _ = docs_client
        headers = _session_headers()
        page = client.get("/api/docs", headers=headers)
        assert page.status_code == 200
        assert "cdn." not in page.text and "<script>" not in page.text
        policy = page.headers["content-security-policy"]
        assert "unsafe-inline" not in policy.split("script-src")[1].split(";")[0]
        assert "jsdelivr" not in policy
        redoc = client.get("/api/redoc", headers=headers)
        assert redoc.status_code == 200 and "cdn." not in redoc.text
        asset = client.get("/api/docs/assets/swagger-ui-bundle.js", headers=headers)
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("text/javascript")
        assert (
            client.get("/api/docs/assets/..%2Fmain.py", headers=headers).status_code
            == 404
        )
        assert client.get("/api/openapi.json", headers=headers).json()["openapi"]

    def test_option_off_answers_404(self, docs_client, monkeypatch):
        client, main = docs_client
        monkeypatch.setattr(main, "_read_addon_options", lambda: {"api_docs": False})
        headers = _session_headers()
        for path in ("/api/docs", "/api/redoc", "/api/openapi.json"):
            assert client.get(path, headers=headers).status_code == 404

    def test_vendored_assets_match_checksums(self):
        import main

        sums = open(os.path.join(main.DOCS_ASSETS_DIR, "SHA256SUMS")).read().split("\n")
        for line in filter(None, sums):
            digest, name = line.split()
            with open(os.path.join(main.DOCS_ASSETS_DIR, name), "rb") as f:
                assert hashlib.sha256(f.read()).hexdigest() == digest


class TestCsp:
    def test_inline_script_hashes(self):
        html = b'<script src="/x.js"></script><script>\n\tboot();\n</script><script type="module">a()</script>'
        hashes = csp.inline_script_hashes(html)
        assert len(hashes) == 2
        import base64

        expected = base64.b64encode(hashlib.sha256(b"\n\tboot();\n").digest()).decode()
        assert f"'sha256-{expected}'" in hashes

    def test_page_policy_has_no_inline_script_and_own_websocket(self):
        policy = csp.page_policy(
            ["'sha256-abc'"], csp.websocket_sources(["hub.local:8100"])
        )
        script = next(p for p in policy.split("; ") if p.startswith("script-src"))
        assert "'unsafe-inline'" not in script and "'sha256-abc'" in script
        connect = next(p for p in policy.split("; ") if p.startswith("connect-src"))
        assert connect == "connect-src 'self' ws://hub.local:8100 wss://hub.local:8100"

    def test_hostile_host_header_is_dropped(self):
        assert csp.websocket_sources(["evil.com; script-src *", "a b", None]) == []

    def test_html_page_gets_hashed_policy(self, docs_client):
        client, _ = docs_client
        page = client.get("/", headers=_session_headers())
        if page.status_code != 200 or "text/html" not in page.headers.get(
            "content-type", ""
        ):
            pytest.skip("frontend bundle not present")
        policy = page.headers["content-security-policy"]
        script = next(p for p in policy.split("; ") if p.startswith("script-src"))
        assert "'unsafe-inline'" not in script
        for source in csp.inline_script_hashes(page.content):
            assert source in script
        assert "wss://hub.local:8100" in policy


# ── logout and logs ─────────────────────────────────────────────────────


class TestLogoutAndLogs:
    @pytest.mark.usefixtures("direct_admin_on")
    async def test_logout_needs_csrf(self):
        token = auth._create_session("admin", "ua")
        req = types.SimpleNamespace(
            cookies={auth.SESSION_COOKIE_NAME: token},
            url=types.SimpleNamespace(scheme="https"),
            headers={"user-agent": "ua"},
        )
        with pytest.raises(HTTPException) as exc:
            await auth.logout(req)
        assert exc.value.status_code == 403
        assert token in auth._sessions
        req.headers["x-csrf-token"] = auth._sessions[token]["csrf_token"]
        await auth.logout(req)
        assert token not in auth._sessions

    async def test_logs_request_the_wanted_range(self):
        from services.ha_client import HAClient

        seen = {}

        class _Resp:
            text = "\n".join(f"line {i}" for i in range(600))

            def raise_for_status(self):
                return None

        class _Http:
            async def get(self, url, headers=None):
                seen["headers"] = headers
                return _Resp()

        client = HAClient.__new__(HAClient)
        client._client = _Http()
        text = await client.get_addon_logs(500)
        assert seen["headers"] == {"Range": "entries=:-499:500"}
        assert len(text.splitlines()) == 500


# ── areas_changed ─────────────────────────────────────────────


class TestAreasChanged:
    def _session(self, db, client_id, **extra):
        fake = _WS(db)
        ws._clients[fake] = {"authenticated": True, "client_id": client_id, **extra}
        return fake

    async def test_only_apps_that_can_see_the_area(self, db):
        member, _ = await _client(db, tags='["og"]')
        other, _ = await _client(db, tags='["eg"]')
        guest_none, _ = await _client(db, is_guest=1, tags="[]")
        guest_all, _ = await _client(db, is_guest=1, tags='["*"]')
        removed, _ = await _client(db)
        sessions = {
            "member": self._session(db, member),
            "other": self._session(db, other),
            "guest_none": self._session(db, guest_none),
            "guest_all": self._session(db, guest_all),
            "removed": self._session(db, removed, reset_signaled=True),
            "admin": self._session(db, "ingress"),
            "integration": self._session(db, "integration"),
        }
        count = await ws.notify_areas_changed(db, {"is_active": True, "tags": ["og"]})
        got = {k for k, s in sessions.items() if s.text}
        assert got == {"member", "guest_all"}
        assert count == 2
        assert sessions["member"].text == [{"type": "areas_changed", "data": {}}]

    async def test_pinned_area_reaches_the_pinned_app(self, db):
        pinned, _ = await _client(db, tags='["area:a1"]')
        other, _ = await _client(db, tags='["area:a2"]')
        a = self._session(db, pinned)
        b = self._session(db, other)
        area = {"id": "a1", "is_active": True, "tags": []}
        assert await ws.notify_areas_changed(db, area) == 1
        assert a.text and not b.text

    async def test_inactive_area_notifies_nobody(self, db):
        member, _ = await _client(db)
        s = self._session(db, member)
        assert await ws.notify_areas_changed(db, {"is_active": False, "tags": []}) == 0
        assert s.text == []

    async def test_bulk_change_reaches_every_app(self, db):
        member, _ = await _client(db, tags='["og"]')
        guest, _ = await _client(db, is_guest=1, tags="[]")
        a = self._session(db, member)
        b = self._session(db, guest)
        admin = self._session(db, "ingress")
        assert await ws.notify_areas_changed(db) == 2
        assert a.text and b.text and not admin.text


class TestOneVisibilityRule:
    """Assignment helper == list rule, member/guest x tags."""

    @pytest.mark.parametrize(
        "is_guest,client_tags,area_tags,visible",
        [
            (0, [], [], True),
            (0, [], ["og"], True),
            (0, ["og"], ["og"], True),
            (0, ["og"], [], False),
            (0, ["og"], ["eg"], False),
            (0, ["*"], ["eg"], True),
            (1, [], [], False),
            (1, [], ["og"], False),
            (1, ["og"], ["og"], True),
            (1, ["og"], [], False),
            (1, ["*"], [], True),
        ],
    )
    async def test_rule(self, db, is_guest, client_tags, area_tags, visible):
        cid, _ = await _client(
            db,
            is_guest=is_guest,
            tags=json.dumps(client_tags),
            expires_at="2099-01-01T00:00:00+00:00" if is_guest else None,
        )
        area_id = await _area(db, area_tags)
        assert await ws._area_visible_for_client(db, cid, area_id) is visible
        assert (area_id in await authz.listed_area_ids(db, cid)) is visible
        scope = await authz.client_area_scope(db, cid)
        listed = {a["id"] for a in await ws._get_areas(db, scope)}
        assert (area_id in listed) is visible


async def test_concurrency_smoke(db):
    """notify_areas_changed tolerates a socket that fails."""

    class _Broken(_WS):
        async def send_text(self, payload):
            raise RuntimeError("gone")

    member, _ = await _client(db)
    broken = _Broken(db)
    ws._clients[broken] = {"authenticated": True, "client_id": member}
    assert await asyncio.wait_for(ws.notify_areas_changed(db), 2) == 0
    assert broken not in ws._clients


async def test_ingress_rewrite_skips_api_docs():
    """No injected inline script on the docs pages under Ingress."""
    import main
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    def req(path):
        r = Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"x-ingress-path", b"/api/hassio_ingress/abc")],
                "client": ("172.30.32.2", 1),
                "server": ("hub", 8099),
                "scheme": "http",
            }
        )
        r.state.auth_type = "ingress"
        return r

    async def page(_request):
        async def body():
            yield b"<html><head></head><body>x</body></html>"

        return StreamingResponse(body(), media_type="text/html")

    mw = main.IngressMiddleware(app=None)
    docs = await mw.dispatch(req("/api/docs"), page)
    ui = await mw.dispatch(req("/"), page)
    assert isinstance(docs, StreamingResponse)  # untouched
    assert b"__INGRESS_PATH" in ui.body
