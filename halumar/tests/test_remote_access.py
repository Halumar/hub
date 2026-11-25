"""Remote access through Home Assistant.

The external URL comes from HA itself (its ``external_url``, else
the Nabu Casa address the integration reports, POST /api/integration/ha-url);
a URL the admin set in the hub wins.
Only devices with remote access get it (client_config), never
guests, never the config sensor; switching remote access tells the device.
A rotation of a device with remote access that is away waits
until it connects to the hub (bounded, audited); a device holding an outdated
token is told "need_token_update" by the compliance channel.
"""

import asyncio
import json
import types
import uuid

import fastapi
import pytest

from .profile_helpers import add_client
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
import services.crypto as crypto
from api.pairing import tokens as pairing_tokens
from models.database import Database
from models.schemas import ClientTokenDelivery, HAUrlReport
from services import compliance
from services import device_provisioning as dp
from services import ha_sensors, integration_status
from services.ha_url import (
    apply_detected_external_url,
    is_nabu_casa_url,
    normalize_external_url,
)

NABU = "https://abcdef0123456789.ui.nabu.casa"

CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    integration_status.reset()
    yield
    crypto.reset_cache()
    dp.reset()
    integration_status.reset()


@pytest.fixture
def sent(monkeypatch):
    """Messages to the integration push session."""
    messages: list[dict] = []

    async def fake_sender(message):
        messages.append(message)
        return True

    monkeypatch.setattr(dp, "_sender", fake_sender)
    return messages


@pytest.fixture
def app_messages(monkeypatch):
    """(client_id | "*", message) sent to apps."""
    messages: list[tuple[str, dict]] = []

    async def fake_send(client_id, message):
        messages.append((client_id, message))
        return False

    async def fake_broadcast(message, *_a, **_k):
        messages.append(("*", message))

    monkeypatch.setattr(ws, "send_to_client", fake_send)
    monkeypatch.setattr(ws, "broadcast", fake_broadcast)
    return messages


@pytest.fixture
def connected(monkeypatch):
    ids: set[str] = set()
    monkeypatch.setattr(pairing_tokens.api_ws, "get_connected_client_ids", lambda: ids)
    monkeypatch.setattr(
        pairing_tokens.api_ws, "get_integration_push_connected", lambda: True
    )
    monkeypatch.setattr(pairing_tokens, "ROTATE_WAIT_SECONDS", 0.05)
    return ids


@pytest.fixture
def audit(monkeypatch):
    actions: list[tuple[str, str | None]] = []

    def record(action, details=None, ip=None):
        actions.append((action, details))

    monkeypatch.setattr(dp, "log_admin_action", record)
    return actions


class _FakeHA:
    def __init__(self, external_url="", remote_domain=None):
        self.external_url = external_url
        self.remote_domain = remote_domain  # cloud/status; Exception = no cloud
        self.posts: list[tuple[str, dict]] = []

    async def get_config(self):
        return {"external_url": self.external_url}

    async def get_cloud_remote_domain(self):
        if isinstance(self.remote_domain, Exception):
            raise self.remote_domain
        return self.remote_domain

    async def _post(self, url, payload):
        self.posts.append((url, payload))


class _WS:
    """Fake WebSocket driving websocket_endpoint (as test_protocol_version)."""

    def __init__(self, db, messages=()):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host="192.0.2.10")
        self.url = types.SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []
        self.application_state = WebSocketState.CONNECTING
        self._messages = list(messages)

    async def accept(self):
        self.application_state = WebSocketState.CONNECTED

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.application_state = WebSocketState.DISCONNECTED

    async def receive_text(self):
        if self._messages:
            return json.dumps(self._messages.pop(0))
        raise WebSocketDisconnect()


def _request(db, auth="ingress", ha_client=None):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha_client)
        ),
        client=None,
    )


async def _client(db, *, remote_allowed=0, is_guest=False, token=True, **extra):
    if "secret" not in extra:
        extra.setdefault("secret_hash", "a" * 64)
    client_id = await add_client(
        db,
        name="TEST-REMOTE",
        last_seen=Database.now(),
        guest=is_guest,
        remote=bool(remote_allowed),
        **extra,
    )
    if token:
        await db.store_client_token(
            client_id, ha_user_id="u-1", refresh_token_id="rt-old", token="old"
        )
    return client_id


async def _row(db, client_id):
    return await db.fetchone("SELECT * FROM paired_clients WHERE id = ?", (client_id,))


# ── external URL from Home Assistant ──────────────────────────


class TestUrlRules:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (NABU + "/", NABU),
            ("  https://ha.example.com:8443  ", "https://ha.example.com:8443"),
            ("http://1.2.3.4:8123", "http://1.2.3.4:8123"),
            ("ftp://ha.example.com", None),
            ("https://user:pw@ha.example.com", None),
            ("https://ha.example.com/?x=1", None),
            ("https://ha.example.com/#frag", None),
            ("https://ha.example.com:99999", None),
            ("https://", None),
            ("", None),
            (None, None),
            (42, None),
        ],
    )
    def test_normalize(self, value, expected):
        assert normalize_external_url(value) == expected

    @pytest.mark.parametrize(
        "url, expected",
        [
            (NABU, True),
            ("http://abc.ui.nabu.casa", False),
            ("https://abc.ui.nabu.casa:8443", False),
            ("https://abc.ui.nabu.casa/path", False),
            ("https://a.b.ui.nabu.casa", False),
            ("https://ui.nabu.casa", False),
            ("https://abc.ui.nabu.casa.evil.com", False),
            ("https://evil-ui.nabu.casa", False),
        ],
    )
    def test_nabu_casa_form(self, url, expected):
        assert is_nabu_casa_url(url) is expected


class TestDetection:
    async def test_follows_detection_until_the_admin_overrides(self, db):
        assert await apply_detected_external_url(db, NABU) is True
        assert await db.get_setting("ha_url") == NABU
        assert await db.get_setting("ha_url_detected") == NABU
        # unchanged report: nothing to do
        assert await apply_detected_external_url(db, NABU) is False
        # the admin sets another URL in the hub: it wins
        await db.set_setting("ha_url", "https://mine.example.com")
        assert await apply_detected_external_url(db, None) is False
        assert await db.get_setting("ha_url") == "https://mine.example.com"
        assert await db.get_setting("ha_url_detected") is None

    async def test_cloud_off_clears_the_detected_url(self, db):
        await apply_detected_external_url(db, NABU)
        assert await apply_detected_external_url(db, None) is True
        assert await db.get_setting("ha_url") is None

    async def test_ha_external_url_replaces_nabu_casa(self, db):
        await apply_detected_external_url(db, NABU)
        assert await apply_detected_external_url(db, "https://ha.example.com") is True
        assert await db.get_setting("ha_url") == "https://ha.example.com"


class TestReportEndpoint:
    async def test_nabu_casa_is_stored_and_devices_are_told(self, db, app_messages):
        ha = _FakeHA(remote_domain=NABU.removeprefix("https://"))
        request = _request(db, "integration", ha)
        result = await apiintg.integration_ha_url(
            request, HAUrlReport(external_url=NABU + "/")
        )
        assert result.success is True and result.changed is True
        assert await db.get_setting("ha_url") == NABU
        assert request.app.state._cors_ha_url == NABU
        assert ("*", {"type": "client_config_updated", "data": {}}) in app_messages

        app_messages.clear()
        again = await apiintg.integration_ha_url(
            request, HAUrlReport(external_url=NABU)
        )
        assert again.changed is False
        assert app_messages == []

    async def test_ha_external_url_is_accepted(self, db, app_messages):
        ha = _FakeHA(external_url="https://ha.example.com/")
        result = await apiintg.integration_ha_url(
            _request(db, "integration", ha),
            HAUrlReport(external_url="https://ha.example.com"),
        )
        assert result.changed is True
        assert await db.get_setting("ha_url") == "https://ha.example.com"

    @pytest.mark.parametrize(
        "url",
        [
            "https://attacker.example.com",
            "http://abc.ui.nabu.casa",
            "https://abc.ui.nabu.casa.attacker.example",
            "javascript:alert(1)",
        ],
    )
    async def test_other_hosts_are_refused(self, db, app_messages, url):
        """The integration token cannot send the devices' HA token elsewhere."""
        ha = _FakeHA(external_url="https://ha.example.com")
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ha_url(
                _request(db, "integration", ha), HAUrlReport(external_url=url)
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "url_not_allowed"
        assert await db.get_setting("ha_url") is None
        assert app_messages == []

    @pytest.mark.parametrize(
        "remote_domain",
        [
            "otherinstance000000.ui.nabu.casa",  # another instance
            None,  # not logged in to Home Assistant Cloud, or no cloud at all
        ],
    )
    async def test_nabu_casa_only_the_own_instance(
        self, db, app_messages, remote_domain
    ):
        """HA's cloud/status names the instance's own remote UI host."""
        ha = _FakeHA(remote_domain=remote_domain)
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ha_url(
                _request(db, "integration", ha), HAUrlReport(external_url=NABU)
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "url_not_allowed"
        assert await db.get_setting("ha_url") is None
        assert app_messages == []

    async def test_no_answer_from_ha_is_retryable(self, db, app_messages):
        """Right after an HA restart the hub cannot ask cloud/status
        or read external_url. A 422 would be final for the integration
        (external_url.py keeps a refusal), so the hub answers 503."""
        from services.ha_ws import HAUnavailable

        ha = _FakeHA(remote_domain=HAUnavailable("not connected"))
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ha_url(
                _request(db, "integration", ha), HAUrlReport(external_url=NABU)
            )
        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "ha_unavailable"
        assert await db.get_setting("ha_url") is None

        class _NoConfig(_FakeHA):
            async def get_config(self):
                raise HAUnavailable("not connected")

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ha_url(
                _request(db, "integration", _NoConfig()),
                HAUrlReport(external_url="https://ha.example.com"),
            )
        assert exc.value.status_code == 503

        # once HA answers, the same report is accepted
        ha.remote_domain = NABU.removeprefix("https://")
        result = await apiintg.integration_ha_url(
            _request(db, "integration", ha), HAUrlReport(external_url=NABU)
        )
        assert result.changed is True and await db.get_setting("ha_url") == NABU

    async def test_null_clears_the_detection(self, db, app_messages):
        await apply_detected_external_url(db, NABU)
        result = await apiintg.integration_ha_url(
            _request(db, "integration", _FakeHA()), HAUrlReport(external_url=None)
        )
        assert result.changed is True
        assert await db.get_setting("ha_url") is None

    async def test_integration_token_only(self, db):
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_ha_url(
                _request(db, "ingress", _FakeHA()), HAUrlReport(external_url=NABU)
            )
        assert exc.value.status_code in (401, 403)

    def test_schema_forbids_extra_fields(self):
        with pytest.raises(Exception):
            HAUrlReport(external_url=NABU, ha_token="x")


# ── only devices with remote access get the external URL ──────


class TestClientConfig:
    async def test_only_remote_devices_get_the_external_url(self, db):
        await db.set_setting("ha_url", NABU)
        await db.set_setting("ha_internal_url", "http://10.0.0.2")
        local = await _client(db)
        remote = await _client(db, remote_allowed=1)
        guest = await _client(db, is_guest=True)
        configs = {
            name: await ws._get_client_config(db, client_id)
            for name, client_id in (
                ("local", local),
                ("remote", remote),
                ("guest", guest),
            )
        }
        assert configs["remote"]["ha_url"] == NABU
        assert configs["local"]["ha_url"] is None
        assert configs["guest"]["ha_url"] is None
        assert {c["ha_internal_url"] for c in configs.values()} == {"http://10.0.0.2"}

    async def test_config_sensor_never_publishes_the_external_url(self, db):
        """Every HA user (guests too) can read the sensor."""
        await db.set_setting("ha_url", NABU)
        await db.set_setting("ha_internal_url", "http://10.0.0.2")
        ha = _FakeHA()
        await ha_sensors.sync_config_sensor(db, ha)
        attributes = ha.posts[0][1]["attributes"]
        assert attributes["ha_url"] is None
        assert attributes["ha_internal_url"] == "http://10.0.0.2"
        assert NABU not in str(ha.posts)

    @pytest.mark.parametrize("enable", [True, False])
    async def test_switching_remote_access_tells_the_device(
        self, db, sent, app_messages, enable
    ):
        """Remote access is the rule remote_access (device adjustment)."""
        from api import profiles as profiles_api

        client_id = await _client(db, remote_allowed=0 if enable else 1)
        await profiles_api.put_client_override(
            _request(db),
            client_id,
            "remote_access",
            profiles_api.DeviceOverride(value=enable),
            dry_run=False,
        )
        assert sent[0]["type"] == "update_client_identity"
        assert sent[0]["data"]["remote_allowed"] is enable
        assert (client_id, {"type": "client_config_updated", "data": {}}) in (
            app_messages
        )

    @pytest.mark.parametrize(
        "field, value",
        [
            ("ha_url", "javascript:alert(1)"),
            ("ha_url", "https://user:pw@ha.example.com"),
            ("ha_internal_url", "ftp://10.0.0.2"),
        ],
    )
    async def test_admin_urls_are_validated(self, db, app_messages, field, value):
        with pytest.raises(fastapi.HTTPException) as exc:
            await pairing.update_ha_urls(
                _request(db), pairing.HAUrlsUpdate(**{field: value})
            )
        assert exc.value.status_code == 422
        assert await db.get_setting(field) is None
        assert app_messages == []

    async def test_admin_urls_are_normalized(self, db, app_messages):
        result = await pairing.update_ha_urls(
            _request(db),
            pairing.HAUrlsUpdate(ha_url=NABU + "/", ha_internal_url="http://10.0.0.2/"),
        )
        assert result.ha_url == NABU
        assert result.ha_internal_url == "http://10.0.0.2"

    async def test_admin_url_change_tells_connected_apps(self, db, app_messages):
        await pairing.update_ha_urls(
            _request(db), pairing.HAUrlsUpdate(ha_url="https://mine.example.com")
        )
        assert ("*", {"type": "client_config_updated", "data": {}}) in app_messages


# ── rotation must not lock out a device on the road ───────────


class TestDeferredRotation:
    async def test_away_device_with_remote_access_is_deferred(
        self, db, sent, connected, audit
    ):
        client_id = await _client(db, remote_allowed=1)
        response = await pairing.rotate_client_token(_request(db), client_id)
        assert response.status_code == 202
        assert b'"status":"pending"' in response.body
        assert sent == []  # its old token keeps working on the road
        assert await dp.rotation_deferred(db, client_id)
        assert not await dp.rotation_pending(db, client_id)
        assert await dp.outstanding_requests(db) == []
        assert audit[0][0] == "client_token_rotation_deferred"
        # a second request keeps the same deferral
        await pairing.rotate_client_token(_request(db), client_id)
        rows = await db.fetchall("SELECT * FROM token_requests")
        assert len(rows) == 1

    async def test_connected_device_rotates_at_once(self, db, sent, connected):
        client_id = await _client(db, remote_allowed=1)
        connected.add(client_id)
        response = await pairing.rotate_client_token(_request(db), client_id)
        assert response.status_code == 202
        assert b"rotation_requested" in response.body
        assert sent[0]["data"]["token_action"] == "rotate"
        assert not await dp.rotation_deferred(db, client_id)

    async def test_local_only_device_rotates_at_once(self, db, sent, connected):
        """Without remote access the device uses HA only at home anyway."""
        client_id = await _client(db)
        await pairing.rotate_client_token(_request(db), client_id)
        assert sent[0]["data"]["token_action"] == "rotate"

    async def test_connecting_device_releases_the_rotation(
        self, db, sent, connected, audit
    ):
        client_id = await _client(db, remote_allowed=1)
        await pairing.rotate_client_token(_request(db), client_id)
        request_id = (await db.fetchone("SELECT request_id FROM token_requests"))[
            "request_id"
        ]
        assert await dp.release_deferred_rotation(db, client_id) is True
        assert sent[0]["type"] == "provision_client"
        assert sent[0]["data"]["token_action"] == "rotate"
        assert sent[0]["data"]["reason"] == "rotate"
        assert sent[0]["data"]["request_id"] == request_id
        assert await dp.rotation_pending(db, client_id)
        assert [i["request_id"] for i in await dp.outstanding_requests(db)] == [
            request_id
        ]
        assert ("client_token_rotation_released", f"client_id={client_id}") in audit
        # nothing left to release
        assert await dp.release_deferred_rotation(db, client_id) is False

    async def test_handshake_releases_on_auth(self, db, monkeypatch):
        """auth_ok of the device (it is home: the hub is LAN only) starts it."""
        released: list[str] = []

        async def fake_release(_db, client_id, **_k):
            released.append(client_id)
            return True

        async def no_send(*_a, **_k):
            return False

        monkeypatch.setattr(dp, "release_deferred_rotation", fake_release)
        monkeypatch.setattr(ws, "broadcast", no_send)
        monkeypatch.setattr(ws, "send_to_client", no_send)
        monkeypatch.delenv("HUB_DEV", raising=False)
        secret = uuid.uuid4().hex
        client_id = await _client(db, remote_allowed=1, secret=secret)
        fake = _WS(db, [{"type": "auth", "secret": secret, **CLIENT_INFO}])
        ws._clients.clear()
        try:
            await ws.websocket_endpoint(fake)
            for _ in range(50):
                if released:
                    break
                await asyncio.sleep(0.01)
        finally:
            ws._clients.clear()
        assert any(m.get("type") == "auth_ok" for m in fake.sent)
        assert released == [client_id]

    async def test_deferral_is_bounded(self, db, sent, connected, audit, monkeypatch):
        clock = {"now": 2_000_000_000.0}
        monkeypatch.setattr(dp, "_now", lambda: clock["now"])
        client_id = await _client(db, remote_allowed=1)
        await pairing.rotate_client_token(_request(db), client_id)

        # Longer than a normal rotation's TTL: still waiting, not dropped.
        clock["now"] += dp.ROTATION_TTL_SECONDS * 3
        assert await dp.retry_once(db, connected=True) == 0
        assert await dp.rotation_deferred(db, client_id)
        assert sent == []

        clock["now"] += dp.ROTATION_DEFER_SECONDS
        await dp.retry_once(db, connected=True)
        assert sent and sent[0]["data"]["token_action"] == "rotate"
        assert not await dp.rotation_deferred(db, client_id)
        assert any(a == "client_token_rotation_deadline_reached" for a, _ in audit)

    async def test_admin_rotation_after_coming_home_skips_the_wait(
        self, db, sent, connected
    ):
        client_id = await _client(db, remote_allowed=1)
        await pairing.rotate_client_token(_request(db), client_id)
        connected.add(client_id)
        await pairing.rotate_client_token(_request(db), client_id)
        assert not await dp.rotation_deferred(db, client_id)
        assert sent[0]["data"]["token_action"] == "rotate"

    async def test_any_new_token_ends_the_deferral(self, db, sent, connected):
        """E.g. remote access switched off meanwhile: the integration rotates."""
        client_id = await _client(db, remote_allowed=1)
        await pairing.rotate_client_token(_request(db), client_id)
        await db.store_client_token(
            client_id, ha_user_id="u-1", refresh_token_id="rt-new", token="new"
        )
        assert not await dp.rotation_deferred(db, client_id)


class TestComplianceOutdatedToken:
    def _row(self, **kw):
        base = {
            "id": str(uuid.uuid4()),
            "secret_hash": "a" * 64,
            "is_active": 1,
            "ha_token_status": "active",
            "last_seen": "2026-11-19T10:00:00+00:00",
            "ha_token_minted_at": "2026-11-19T09:00:00+00:00",
        }
        base.update(kw)
        return base

    def test_token_fetched_is_compliant(self):
        assert compliance.derive_status(self._row()) == "compliant"

    def test_token_newer_than_last_contact_needs_update(self):
        row = self._row(ha_token_minted_at="2026-11-19T11:00:00+00:00")
        blob = compliance.build_compliance(row, now=1)
        assert blob["status"] == "need_token_update"
        assert "sig" in blob

    def test_pending_device_stays_compliant(self):
        row = self._row(
            ha_token_status="pending", ha_token_minted_at="2026-11-19T11:00:00+00:00"
        )
        assert compliance.derive_status(row) == "compliant"

    def test_unparsable_times_stay_compliant(self):
        assert compliance.derive_status(self._row(ha_token_minted_at="x")) == (
            "compliant"
        )

    async def test_status_snapshot_signals_the_outdated_token(
        self, db, app_messages, monkeypatch
    ):
        monkeypatch.setattr(ws, "get_connected_client_ids", lambda: set())
        client_id = await _client(db, remote_allowed=1)
        # the app was last seen before the token was (re)issued
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET last_seen = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", client_id),
            )
        status = await apiintg.integration_status(_request(db, "integration"))
        entry = next(c for c in status["clients"] if c["id"] == client_id)
        assert entry["compliance"]["status"] == "need_token_update"


async def test_delivery_answers_released_rotation(db, sent, connected, app_messages):
    """End to end: deferred → device connects → integration delivers."""
    client_id = await _client(db, remote_allowed=1)
    await pairing.rotate_client_token(_request(db), client_id)
    await dp.release_deferred_rotation(db, client_id)
    request_id = sent[0]["data"]["request_id"]
    result = await apiintg.integration_client_token(
        _request(db, "integration"),
        ClientTokenDelivery(
            client_id=client_id,
            ha_user_id="u-1",
            refresh_token_id="rt-2",
            token="new-token",
            request_id=request_id,
        ),
    )
    assert result["unchanged"] is False
    assert await db.fetchall("SELECT * FROM token_requests") == []
    assert (await _row(db, client_id))["ha_refresh_token_id"] == "rt-2"


async def test_cloud_remote_domain_from_cloud_status(monkeypatch):
    """The hub reads the instance's remote UI host itself."""
    from services.ha_client import HAClient

    client = HAClient("token")
    answers = [
        {"logged_in": True, "remote_domain": "AbC123.ui.nabu.casa", "email": "x"},
        {"logged_in": False, "cloud": "disconnected"},
        {"logged_in": True, "remote_domain": None},
    ]
    commands = []

    async def fake_command(command):
        commands.append(command)
        return answers.pop(0)

    monkeypatch.setattr(client, "_ws_command", fake_command)
    assert await client.get_cloud_remote_domain() == "abc123.ui.nabu.casa"
    assert await client.get_cloud_remote_domain() is None
    assert await client.get_cloud_remote_domain() is None
    assert commands[0] == {"type": "cloud/status"}

    from services.ha_ws import HACommandError, HAUnavailable

    async def no_cloud(command):
        raise HACommandError("unknown_command: Unknown command.")

    monkeypatch.setattr(client, "_ws_command", no_cloud)
    assert await client.get_cloud_remote_domain() is None  # definite: no cloud

    async def down(command):
        raise HAUnavailable("not connected to Home Assistant")

    monkeypatch.setattr(client, "_ws_command", down)
    with pytest.raises(HAUnavailable):
        await client.get_cloud_remote_domain()
    await client.close()
