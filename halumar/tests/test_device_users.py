"""One HA user per paired device, hub side.

The hub never creates, rotates or revokes HA tokens. It asks the integration
(provision_client / update_client_identity / deprovision_client over the
integration push session, provisioning[] in the status snapshot as fallback)
and stores the token the integration delivers to
POST /api/integration/client-token. Contract: contracts
``docs/per-device-users.md``.

The integration sender, ``send_to_client`` and ``broadcast`` are mocked.
"""

import asyncio
import logging
import sqlite3
import types
import uuid
from datetime import datetime, timedelta, timezone

import fastapi
import pydantic
import pytest

from .profile_helpers import add_client, starter_profile
from api import profiles as profiles_api

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
import services.crypto as crypto
from api.pairing import tokens as pairing_tokens
from models.database import Database
from models.schemas import ClientTokenDelivery, PairingGenerateRequest
from services import device_provisioning as dp
from services import integration_status

# Client info every app sends with auth/pair.
CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}

TOKEN = "eyJ.device-user-access-token.SECRET"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    keyfile = str(tmp_path / ".hub_keyfile")
    monkeypatch.setattr(crypto, "KEYFILE_PATH", keyfile)
    crypto.reset_cache()
    dp.reset()
    integration_status.reset()
    pairing.clear_active_pins()
    yield
    crypto.reset_cache()
    dp.reset()
    integration_status.reset()
    pairing.clear_active_pins()


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
    """Messages to apps (send_to_client) and broadcasts."""
    messages: list[tuple[str, dict]] = []

    async def fake_send(client_id, message):
        messages.append((client_id, message))
        return False

    async def fake_broadcast(*_a, **_k):
        return None

    monkeypatch.setattr(ws, "send_to_client", fake_send)
    monkeypatch.setattr(ws, "broadcast", fake_broadcast)
    return messages


def _request(db, auth="ingress", ha_client=None):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha_client)
        ),
        client=None,
    )


async def _client(
    db,
    name="iPhone",
    *,
    is_guest=False,
    expires_at=None,
    status="pending",
    owner_label=None,
    remote_allowed=0,
    is_active=1,
):
    now = Database.now()
    return await add_client(
        db,
        name=name,
        secret_hash="a" * 64,
        guest=is_guest,
        expires_at=expires_at,
        remote=bool(remote_allowed),
        is_active=is_active,
        owner_label=owner_label,
        ha_token_status=status,
        ha_token_pending_since=now if status == "pending" else None,
    )


async def _active_client(db, **kwargs):
    client_id = await _client(db, **kwargs)
    await db.store_client_token(
        client_id, ha_user_id="u-1", refresh_token_id="rt-old", token="old-token"
    )
    return client_id


async def _row(db, client_id):
    return await db.fetchone("SELECT * FROM paired_clients WHERE id = ?", (client_id,))


def _future(hours=24):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _delivery(client_id, refresh_token_id="rt-1", ha_user_id="u-1", token=TOKEN):
    return ClientTokenDelivery(
        client_id=client_id,
        ha_user_id=ha_user_id,
        refresh_token_id=refresh_token_id,
        token=token,
    )


# ── Schema ───────────────────────────────────────────────────────────────


class TestSchema:
    async def test_new_device_columns(self, db):
        client_id = await _client(db)
        row = await _row(db, client_id)
        for column in (
            "ha_user_id",
            "owner_label",
            "access",
            "profile_id",
            "own_area_id",
            "ha_token_pending_since",
        ):
            assert column in row
        for gone in ("ha_token_client_name", "ha_token_expires_at", "remote_allowed"):
            assert gone not in row
        assert row["ha_token_status"] == "pending"

    async def test_status_is_pending_or_active_only(self, db):
        client_id = await _client(db)
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction():
                await db.execute(
                    "UPDATE paired_clients SET ha_token_status = 'revoked' WHERE id = ?",
                    (client_id,),
                )

    async def test_guest_always_has_an_expiry(self, db):
        guest_id = await _client(db, is_guest=True, expires_at=_future())
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction():
                await db.execute(
                    "UPDATE paired_clients SET expires_at = NULL WHERE id = ?",
                    (guest_id,),
                )

    async def test_no_owner_token_storage(self, db):
        assert not hasattr(db, "get_owner_ha_token")
        assert not hasattr(db, "set_client_ha_token_info")


# ── Pairing ──────────────────────────────────────────────────────────────


class _PairWS:
    def __init__(self, db, log):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        )
        self.log = log

    async def send_json(self, payload):
        self.log.append(("app", payload["type"]))


class TestPairing:
    async def test_pairing_creates_a_pending_device_and_asks_the_integration(
        self, db, sent, app_messages
    ):
        log: list = []

        async def recording_sender(message):
            log.append(("integration", message["type"]))
            sent.append(message)
            return True

        dp.set_sender(recording_sender)
        try:
            pairing.set_active_pin(
                "1234-5678",
                {
                    "created_at": 0,
                    "expires_at": 9e12,
                    "owner_label": "Admin One",
                },
            )
            ok, client_id = await ws._handle_pair(
                _PairWS(db, log),
                db,
                {"pin": "1234-5678", "client_name": "iPhone", **CLIENT_INFO},
            )
        finally:
            dp.set_sender(ws.send_to_integration)
        assert ok
        # pair_ok first, then the request to the integration (after the commit).
        assert log == [("app", "pair_ok"), ("integration", "provision_client")]
        row = await _row(db, client_id)
        assert row["ha_token_status"] == "pending"
        assert row["ha_token_pending_since"]
        assert row["ha_token_encrypted"] is None
        assert row["owner_label"] == "Admin One"
        # Default profile of an iPhone: Erwachsene Person, remote access on.
        assert row["access"] == "member"

        data = sent[0]["data"]
        assert data["client_id"] == client_id
        assert data["display_name"] == "iPhone"
        assert data["owner_label"] == "Admin One"
        assert data["remote_allowed"] is True
        assert data["is_guest"] is False
        assert data["guest_expires_at"] is None
        assert data["token_action"] == "ensure"
        assert data["reason"] == "pair"
        uuid.UUID(data["request_id"])

    async def test_guest_pin_follows_the_guest_profile(self, db, sent, app_messages):
        """A guest gets the guest default profile (Gast: remote off)."""
        expiry = _future()
        pairing.set_active_pin(
            "1111-2222",
            {
                "created_at": 0,
                "expires_at": 9e12,
                "is_guest": True,
                "guest_expires_at": expiry,
            },
        )
        ok, client_id = await ws._handle_pair(
            _PairWS(db, []),
            db,
            {"pin": "1111-2222", "client_name": "Gast", **CLIENT_INFO},
        )
        assert ok
        assert (await _row(db, client_id))["access"] == "guest"
        data = sent[0]["data"]
        assert data["is_guest"] is True
        assert data["remote_allowed"] is False
        assert data["guest_expires_at"] == expiry

    async def test_admin_pin_carries_owner_and_remote_access(
        self, db, app_messages, monkeypatch
    ):
        async def _base_url(_request, _db):
            return "https://hub.example"

        import services.pairing_link as pairing_link

        monkeypatch.setattr(pairing_link, "resolve_base_url", _base_url)
        profile_id = await starter_profile(db, "wall_panel")
        await pairing.generate_pairing_pin(
            _request(db),
            pairing.GeneratePinRequest(
                owner_label="  Manager One  ", profile_id=profile_id
            ),
        )
        _pin, data = pairing.get_active_pin()
        assert data["owner_label"] == "Manager One"
        assert data["profile_id"] == profile_id

    def test_pin_request_has_no_rights_fields(self):
        """Rights and remote access are profile rules, not PIN fields."""
        with pytest.raises(pydantic.ValidationError):
            pairing.GeneratePinRequest(remote_allowed=True)
        with pytest.raises(pydantic.ValidationError):
            pairing.GeneratePinRequest(access="guest", template_id="x")
        with pytest.raises(pydantic.ValidationError):
            pairing.GeneratePinRequest(owner_label="x" * 51)
        with pytest.raises(pydantic.ValidationError):
            pairing.GeneratePinRequest(guest_hours=24)  # members have no duration
        with pytest.raises(pydantic.ValidationError):
            pairing.GeneratePinRequest(access="guest", guest_hours=2161)
        assert PairingGenerateRequest(remote_allowed=True)  # ignored extra key

    async def test_integration_start_pairing_carries_owner(self, db, app_messages):
        request = _request(db, auth="integration")
        body = b'{"owner_label": "Admin One", "profile": "wandpanel"}'
        request.headers = {"content-length": str(len(body))}

        async def _body():
            return body

        request.body = _body
        await apiintg.integration_start_pairing(request)
        _pin, data = pairing.get_active_pin()
        assert data["owner_label"] == "Admin One"
        assert data["profile_id"] == await starter_profile(db, "wall_panel")


# ── POST /api/integration/client-token ───────────────────────────────────


class TestClientToken:
    async def test_first_token_is_stored_encrypted_and_announced(
        self, db, app_messages
    ):
        client_id = await _client(db)
        result = await apiintg.integration_client_token(
            _request(db, "integration"), _delivery(client_id)
        )
        assert result == {
            "success": True,
            "client_id": client_id,
            "ha_token_status": "active",
            "unchanged": False,
            "delivered": False,
        }
        row = await _row(db, client_id)
        assert row["ha_token_status"] == "active"
        assert row["ha_user_id"] == "u-1"
        assert row["ha_refresh_token_id"] == "rt-1"
        assert row["ha_token_pending_since"] is None
        assert row["ha_token_minted_at"]
        assert TOKEN not in row["ha_token_encrypted"]
        assert (await ws._get_client_config(db, client_id))["ha_token"] == TOKEN
        # A first token: the app is told right away, no waiting.
        assert app_messages == [
            (client_id, {"type": "client_config_updated", "data": {}})
        ]

    async def test_repeated_delivery_is_idempotent(self, db, app_messages):
        client_id = await _client(db)
        request = _request(db, "integration")
        await apiintg.integration_client_token(request, _delivery(client_id))
        minted = (await _row(db, client_id))["ha_token_minted_at"]
        app_messages.clear()

        result = await apiintg.integration_client_token(request, _delivery(client_id))
        assert result["unchanged"] is True
        assert result["delivered"] is False
        assert app_messages == []
        assert (await _row(db, client_id))["ha_token_minted_at"] == minted

    async def test_new_token_waits_for_the_app_to_switch(self, db, monkeypatch):
        """Answer (= integration removes the old token) only after the
        app fetched its new config plus the switch grace."""
        client_id = await _active_client(db)
        monkeypatch.setattr(ws, "CONFIG_SWITCH_GRACE_SECONDS", 0.2)
        loop = asyncio.get_running_loop()
        times = {}

        async def _send(cid, message):
            times["notified"] = loop.time()
            loop.call_soon(ws._notify_config_fetched, cid)
            return True

        monkeypatch.setattr(ws, "send_to_client", _send)
        result = await apiintg.integration_client_token(
            _request(db, "integration"), _delivery(client_id, refresh_token_id="rt-2")
        )
        assert result["delivered"] is True
        assert loop.time() - times["notified"] >= 0.2
        assert ws._config_fetch_waiters == {}
        assert (await ws._get_client_config(db, client_id))["ha_token"] == TOKEN

    async def test_wait_is_bounded_when_the_app_never_fetches(self, db, monkeypatch):
        client_id = await _active_client(db)

        async def _send(_cid, _message):
            return True

        monkeypatch.setattr(ws, "send_to_client", _send)
        monkeypatch.setattr(ws, "CONFIG_FETCH_GRACE_SECONDS", 0.05)
        result = await asyncio.wait_for(
            apiintg.integration_client_token(
                _request(db, "integration"),
                _delivery(client_id, refresh_token_id="rt-2"),
            ),
            2,
        )
        assert result["delivered"] is True

    async def test_unknown_client_is_404(self, db, app_messages):
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_client_token(
                _request(db, "integration"), _delivery(str(uuid.uuid4()))
            )
        assert exc.value.status_code == 404
        assert exc.value.detail["code"] == "client_not_found"

    @pytest.mark.parametrize("case", ["revoked", "pending_deletion", "expired_guest"])
    async def test_inactive_client_is_409(self, db, app_messages, case):
        if case == "revoked":
            client_id = await _client(db, is_active=0)
        elif case == "expired_guest":
            client_id = await _client(db, is_guest=True, expires_at=_future(-1))
        else:
            client_id = await _client(db)
            async with db.transaction():
                await db.execute(
                    "UPDATE paired_clients SET pending_deletion_at = ? WHERE id = ?",
                    (Database.now(), client_id),
                )
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_client_token(
                _request(db, "integration"), _delivery(client_id)
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "client_inactive"
        assert (await _row(db, client_id))["ha_token_encrypted"] is None

    async def test_database_error_is_503(self, db, app_messages, monkeypatch):
        client_id = await _client(db)

        async def _broken(*_a, **_k):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(db, "store_client_token", _broken)
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_client_token(
                _request(db, "integration"), _delivery(client_id)
            )
        assert exc.value.status_code == 503
        assert exc.value.detail["code"] == "db_unavailable"

    async def test_only_the_integration_may_deliver(self, db):
        client_id = await _client(db)
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_client_token(_request(db), _delivery(client_id))
        assert exc.value.status_code == 403

    async def test_the_token_is_never_logged(self, db, app_messages, caplog):
        client_id = await _client(db)
        with caplog.at_level(logging.DEBUG):
            await apiintg.integration_client_token(
                _request(db, "integration"), _delivery(client_id)
            )
        assert TOKEN not in caplog.text
        assert "rt-1" in caplog.text

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            ClientTokenDelivery(
                client_id=str(uuid.uuid4()),
                ha_user_id="u",
                refresh_token_id="rt",
                token="t",
                owner_token="x",
            )


# ── GET /api/integration/status ──────────────────────────────────────────


class TestStatus:
    async def test_snapshot_lists_devices_and_outstanding_requests(
        self, db, app_messages
    ):
        pending = await _client(db, "iPad", owner_label="Admin One")
        active = await _active_client(db, name="TV", remote_allowed=1)
        guest_expiry = _future()
        guest = await _client(db, "Gast", is_guest=True, expires_at=guest_expiry)
        await _client(db, "Old", is_active=0)

        status = await apiintg.integration_status(_request(db, "integration"))
        assert "owner_token_configured" not in status["hub"]
        by_id = {c["id"]: c for c in status["clients"]}
        assert by_id[pending]["ha_token_status"] == "pending"
        assert by_id[pending]["ha_user_id"] is None
        assert by_id[pending]["owner_label"] == "Admin One"
        assert by_id[active]["ha_token_status"] == "active"
        assert by_id[active]["ha_user_id"] == "u-1"
        assert by_id[active]["remote_allowed"] is True
        assert by_id[guest]["remote_allowed"] is False

        items = {i["client_id"]: i for i in status["provisioning"]}
        assert set(items) == {pending, guest}
        assert items[pending]["token_action"] == "ensure"
        assert items[pending]["reason"] == "pair"
        assert items[pending]["display_name"] == "iPad"
        assert items[guest]["guest_expires_at"] == guest_expiry

    async def test_open_rotation_and_reprovision_are_listed(self, db, app_messages):
        rotating = await _active_client(db)
        request_id = await dp.start_rotation(db, rotating)
        waiter = dp.rotation_waiter(rotating)
        lost = await _active_client(db, name="Restored")
        await db.mark_client_pending(lost)

        items = {i["client_id"]: i for i in await dp.outstanding_requests(db)}
        assert items[rotating]["token_action"] == "rotate"
        assert items[rotating]["reason"] == "rotate"
        assert items[rotating]["request_id"] == request_id
        assert items[lost]["reason"] == "reprovision"

        # The delivered token answers the rotation.
        await apiintg.integration_client_token(
            _request(db, "integration"), _delivery(rotating, refresh_token_id="rt-2")
        )
        assert waiter.is_set()
        assert rotating not in {
            i["client_id"] for i in await dp.outstanding_requests(db)
        }
        assert not await dp.rotation_pending(db, rotating)

    async def test_deleted_devices_are_held_in_the_snapshot(self, db, app_messages):
        """ha_user_holds lists deleted devices (hidden ones too) until
        they are released; they are never in provisioning[]."""
        kept = await _active_client(db)
        deleted = await _active_client(db, name="Gone")
        hidden = await _active_client(db, name="Hidden")
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
                "WHERE id IN (?, ?)",
                (Database.now(), deleted, hidden),
            )
        await db.set_pending_hidden(hidden)

        status = await apiintg.integration_status(_request(db, "integration"))
        assert sorted(status["ha_user_holds"]) == sorted([deleted, hidden])
        assert kept not in status["ha_user_holds"]
        assert status["provisioning"] == []

        assert await dp.release_user(db, deleted)
        assert not await dp.release_user(db, deleted)  # only once
        status = await apiintg.integration_status(_request(db, "integration"))
        assert status["ha_user_holds"] == [hidden]


# ── Identity changes ─────────────────────────────────────────────────────


class TestIdentity:
    async def test_rename_owner_and_remote_access_are_reported(
        self, db, sent, app_messages
    ):
        client_id = await _active_client(db)
        result = await pairing.update_client(
            _request(db),
            client_id,
            pairing.ClientUpdate(name="Admin iPhone", owner_label="Admin One"),
        )
        assert result.owner_label == "Admin One"
        assert result.remote_allowed is False
        assert result.ha_token_status == "active"
        identity = {
            "client_id": client_id,
            "display_name": "Admin iPhone",
            "owner_label": "Admin One",
            "is_guest": False,
            "remote_allowed": False,
            "guest_expires_at": None,
        }
        assert sent == [{"type": "update_client_identity", "data": identity}]
        # Remote access is the profile rule remote_access: an
        # adjustment switches it and is reported the same way.
        sent.clear()
        await profiles_api.put_client_override(
            _request(db),
            client_id,
            "remote_access",
            profiles_api.DeviceOverride(value=True),
            dry_run=False,
        )
        assert {
            "type": "update_client_identity",
            "data": {**identity, "remote_allowed": True},
        } in sent

    async def test_empty_owner_label_clears_it(self, db, sent, app_messages):
        client_id = await _active_client(db, owner_label="Manager One")
        await pairing.update_client(
            _request(db), client_id, pairing.ClientUpdate(owner_label="")
        )
        assert (await _row(db, client_id))["owner_label"] is None
        assert sent[0]["data"]["owner_label"] is None

    async def test_nothing_changed_sends_nothing(self, db, sent, app_messages):
        client_id = await _active_client(db, owner_label="Manager One")
        await pairing.update_client(
            _request(db),
            client_id,
            pairing.ClientUpdate(name="iPhone", owner_label="Manager One"),
        )
        assert sent == []

    async def test_pending_device_is_not_updated_separately(
        self, db, sent, app_messages
    ):
        """The running provisioning reads the current identity anyway."""
        client_id = await _client(db)
        await pairing.update_client(
            _request(db), client_id, pairing.ClientUpdate(owner_label="Manager One")
        )
        assert sent == []
        items = await dp.outstanding_requests(db)
        assert items[0]["owner_label"] == "Manager One"

    async def test_guest_remote_access_by_adjustment(self, db, sent, app_messages):
        """A guest may have remote access (normal rule, not a boundary)."""
        guest_id = await _active_client(db, is_guest=True, expires_at=_future())
        with pytest.raises(pydantic.ValidationError):
            pairing.ClientUpdate(remote_allowed=True)
        await profiles_api.put_client_override(
            _request(db),
            guest_id,
            "remote_access",
            profiles_api.DeviceOverride(value=True),
            dry_run=False,
        )
        identity = next(m for m in sent if m["type"] == "update_client_identity")
        assert identity["data"]["is_guest"] is True
        assert identity["data"]["remote_allowed"] is True

    async def test_guest_extension_is_reported(self, db, sent, app_messages):
        guest_id = await _active_client(db, is_guest=True, expires_at=_future(2))
        result = await pairing.extend_guest_expiry(db, None, guest_id, hours=48)
        assert sent[0]["type"] == "update_client_identity"
        assert sent[0]["data"]["guest_expires_at"] == result["expires_at"]
        assert sent[0]["data"]["remote_allowed"] is False

    def test_client_update_rejects_unknown_fields(self):
        with pytest.raises(pydantic.ValidationError):
            pairing.ClientUpdate(ha_token="x")


# ── Rotation (admin) ─────────────────────────────────────────────────────


class TestRotation:
    @pytest.fixture
    def push_connected(self, monkeypatch):
        state = {"connected": True}
        monkeypatch.setattr(
            pairing_tokens.api_ws,
            "get_integration_push_connected",
            lambda: state["connected"],
        )
        return state

    async def test_rotation_asks_the_integration_and_returns_the_new_token(
        self, db, sent, app_messages, push_connected
    ):
        client_id = await _active_client(db)

        async def deliver_soon():
            while not sent:
                await asyncio.sleep(0.01)
            await apiintg.integration_client_token(
                _request(db, "integration"),
                _delivery(client_id, refresh_token_id="rt-2"),
            )

        task = asyncio.create_task(deliver_soon())
        result = await pairing.rotate_client_token(_request(db), client_id)
        await task
        assert sent[0]["type"] == "provision_client"
        assert sent[0]["data"]["token_action"] == "rotate"
        assert sent[0]["data"]["reason"] == "rotate"
        assert result.ha_token_status == "active"
        assert result.ha_token_minted_at
        assert (await _row(db, client_id))["ha_refresh_token_id"] == "rt-2"
        assert not await dp.rotation_pending(db, client_id)

    async def test_rotation_still_running_is_202(
        self, db, sent, monkeypatch, push_connected
    ):
        client_id = await _active_client(db)
        monkeypatch.setattr(pairing_tokens, "ROTATE_WAIT_SECONDS", 0.05)
        response = await pairing.rotate_client_token(_request(db), client_id)
        assert response.status_code == 202
        assert b"rotation_requested" in response.body
        assert await dp.rotation_pending(db, client_id)

    async def test_no_integration_answers_at_once_with_pending(
        self, db, sent, push_connected
    ):
        """Without the integration the request neither blocks nor
        fails; the rotation is stored and runs once the integration is back."""
        push_connected["connected"] = False
        client_id = await _active_client(db)
        loop = asyncio.get_running_loop()
        started = loop.time()
        response = await pairing.rotate_client_token(_request(db), client_id)
        assert loop.time() - started < 1
        assert response.status_code == 202
        assert b'"status":"pending"' in response.body
        assert sent == []
        items = await dp.outstanding_requests(db)
        assert [(i["client_id"], i["token_action"]) for i in items] == [
            (client_id, "rotate")
        ]

        # The integration connects: the retry loop pushes the same request.
        assert await dp.retry_once(db, connected=True) == 1
        assert sent[0]["data"]["token_action"] == "rotate"
        assert sent[0]["data"]["request_id"] == items[0]["request_id"]

    async def test_open_rotation_survives_a_hub_restart(self, db, sent, push_connected):
        """Rotation and backoff live in the database."""
        push_connected["connected"] = False
        client_id = await _active_client(db)
        await pairing.rotate_client_token(_request(db), client_id)
        before = await dp.outstanding_requests(db)

        dp.reset()  # memory of the old process is gone
        restarted = Database(db.db_path)
        await restarted.initialize()
        try:
            after = await dp.outstanding_requests(restarted)
            assert after == before
            assert await dp.retry_once(restarted, connected=True) == 1
            assert sent[-1]["data"]["request_id"] == before[0]["request_id"]
            # Backoff persisted: no second send right away.
            assert await dp.retry_once(restarted, connected=True) == 0
        finally:
            await restarted.close()

    async def test_same_rotation_is_reused_until_answered(self, db, push_connected):
        push_connected["connected"] = False
        client_id = await _active_client(db)
        first = await dp.start_rotation(db, client_id)
        assert await dp.start_rotation(db, client_id) == first

    async def test_unanswered_rotation_expires(self, db, monkeypatch):
        client_id = await _active_client(db)
        clock = {"now": 2_000_000_000.0}
        monkeypatch.setattr(dp, "_now", lambda: clock["now"])
        await dp.start_rotation(db, client_id)
        clock["now"] += dp.ROTATION_TTL_SECONDS + 1
        assert await dp.outstanding_requests(db) == []
        await dp.retry_once(db, connected=False)
        assert await db.fetchall("SELECT * FROM token_requests") == []

    async def test_device_without_token_is_409(self, db, sent, push_connected):
        client_id = await _client(db)
        with pytest.raises(fastapi.HTTPException) as exc:
            await pairing.rotate_client_token(_request(db), client_id)
        assert exc.value.status_code == 409

    async def test_admin_only(self, db):
        client_id = await _active_client(db)
        with pytest.raises(fastapi.HTTPException) as exc:
            await pairing.rotate_client_token(_request(db, "integration"), client_id)
        assert exc.value.status_code == 403


# ── Removal ──────────────────────────────────────────────────────────────


class TestRemoval:
    async def test_revoke_closes_then_deprovisions(
        self, db, sent, app_messages, monkeypatch
    ):
        client_id = await _active_client(db)
        order = []

        async def _disconnect(cid, code=4003, reason=""):
            order.append(("close", code))
            return 1

        async def _sender(message):
            order.append(("integration", message["data"]["reason"]))
            return True

        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        monkeypatch.setattr(dp, "_sender", _sender)
        await pairing.revoke_client(_request(db), client_id)
        assert order == [("close", 4001), ("integration", "revoked")]

    async def test_kick_deletes_then_deprovisions(
        self, db, sent, app_messages, monkeypatch
    ):
        client_id = await _active_client(db)

        async def _disconnect(*_a, **_k):
            return 1

        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        await pairing.kick_client(_request(db), client_id)
        assert await _row(db, client_id) is None
        assert sent == [
            {
                "type": "deprovision_client",
                "data": {"client_id": client_id, "reason": "deleted"},
            }
        ]

    async def test_clear_all_deprovisions_every_device(
        self, db, sent, app_messages, monkeypatch
    ):
        ids = {await _active_client(db), await _client(db)}

        async def _disconnect(*_a, **_k):
            return 0

        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        await pairing.delete_all_clients(_request(db))
        assert {m["data"]["client_id"] for m in sent} == ids
        assert {m["data"]["reason"] for m in sent} == {"cleared"}

    async def test_expired_guest_rejected_at_auth_loses_its_user(self, db, sent):
        import hashlib

        guest_id = await _active_client(db, is_guest=True, expires_at=_future(-1))
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET secret_hash = ? WHERE id = ?",
                (hashlib.sha256(b"guest-secret").hexdigest(), guest_id),
            )
        assert await ws._verify_client_secret(db, "guest-secret") is None
        assert await _row(db, guest_id) is None
        assert sent == [
            {
                "type": "deprovision_client",
                "data": {"client_id": guest_id, "reason": "guest_expired"},
            }
        ]

    async def test_removal_forgets_outstanding_rotation(self, db, sent, monkeypatch):
        client_id = await _active_client(db)
        await dp.start_rotation(db, client_id)
        waiter = dp.rotation_waiter(client_id)

        async def _disconnect(*_a, **_k):
            return 0

        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        await pairing.kick_client(_request(db), client_id)
        assert waiter.is_set()
        assert not await dp.rotation_pending(db, client_id)
        assert await db.fetchall("SELECT * FROM token_requests") == []

    async def test_revoked_device_drops_its_requests(self, db, sent, monkeypatch):
        client_id = await _active_client(db)
        await dp.start_rotation(db, client_id)

        async def _disconnect(*_a, **_k):
            return 0

        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        await pairing.revoke_client(_request(db), client_id)
        await dp.retry_once(db, connected=True)
        assert await db.fetchall("SELECT * FROM token_requests") == []


# ── Deleted devices keep their HA user until the reset ───────────


class TestDeleteHold:
    async def test_offline_device_is_released_after_the_hold(
        self, db, sent, monkeypatch
    ):
        client_id = await _active_client(db)
        await pairing.delete_client(_request(db), client_id)
        await asyncio.sleep(0.05)
        assert sent == []
        assert await dp.held_user_ids(db) == [client_id]

        # Within the hold nothing happens, also not across retries.
        await dp.retry_once(db, connected=True)
        assert sent == []

        later = dp._now() + dp.DELETE_HOLD_SECONDS + 1
        monkeypatch.setattr(dp, "_now", lambda: later)
        await dp.retry_once(db, connected=False)
        assert sent == [
            {
                "type": "deprovision_client",
                "data": {"client_id": client_id, "reason": "deleted"},
            }
        ]
        assert await dp.held_user_ids(db) == []
        # The tombstone stays for the returning app.
        assert (await _row(db, client_id))["pending_deletion_at"]

    async def test_returning_app_ack_releases_the_user(self, db, sent):
        from api.ws.client import _purge_tombstone_on_ack

        client_id = await _active_client(db)
        await pairing.delete_client(_request(db), client_id)
        assert sent == []

        class _Sock:
            async def send_json(self, _data):
                pass

            async def close(self, **_k):
                pass

        await _purge_tombstone_on_ack(_Sock(), db, client_id)
        assert await _row(db, client_id) is None
        assert sent == [
            {
                "type": "deprovision_client",
                "data": {"client_id": client_id, "reason": "deleted"},
            }
        ]

    async def test_forced_purge_releases_the_user(self, db, sent):
        client_id = await _active_client(db)
        await pairing.delete_client(_request(db), client_id)
        await pairing.purge_client_tombstone(_request(db), client_id)
        assert [m["data"]["client_id"] for m in sent] == [client_id]


# ── Lost token, retry ────────────────────────────────────────────────────


class TestRecovery:
    async def test_undecryptable_token_asks_for_it_again(self, db, sent, monkeypatch):
        client_id = await _active_client(db)
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET ha_token_encrypted = 'not-fernet' WHERE id = ?",
                (client_id,),
            )
        cfg = await ws._get_client_config(db, client_id)
        assert cfg["ha_token"] is None
        await asyncio.sleep(0.05)
        row = await _row(db, client_id)
        assert row["ha_token_status"] == "pending"
        assert row["ha_token_encrypted"] is None
        assert sent[0]["type"] == "provision_client"
        assert sent[0]["data"]["reason"] == "reprovision"

    async def test_retry_backs_off(self, db, sent, monkeypatch):
        client_id = await _client(db)
        clock = {"now": 2_000_000_000.0}
        monkeypatch.setattr(dp, "_now", lambda: clock["now"])

        assert await dp.retry_once(db, connected=False) == 0
        assert await dp.retry_once(db, connected=True) == 1
        assert sent[-1]["data"]["reason"] == "retry"
        first_id = sent[-1]["data"]["request_id"]
        assert await dp.retry_once(db, connected=True) == 0  # backoff 60 s

        clock["now"] += 61
        assert await dp.retry_once(db, connected=True) == 1
        clock["now"] += 61
        assert await dp.retry_once(db, connected=True) == 0  # now 120 s
        clock["now"] += 60
        assert await dp.retry_once(db, connected=True) == 1
        assert sent[-1]["data"]["client_id"] == client_id
        assert sent[-1]["data"]["request_id"] != first_id

        # The token arrives: nothing more to repeat.
        await db.store_client_token(
            client_id, ha_user_id="u", refresh_token_id="rt", token="t"
        )
        dp.complete_rotation(client_id)
        clock["now"] += 1000
        assert await dp.retry_once(db, connected=True) == 0

    async def test_pending_device_stays_compliant(self, db):
        from services.compliance import derive_status

        client_id = await _client(db)
        assert derive_status(await _row(db, client_id)) == "compliant"


# ── Removed surface ──────────────────────────────────────────────────────


def test_owner_token_and_manual_token_endpoints_are_gone():
    from .test_route_resolution import _all_http_routes

    paths = {path for path, _route in _all_http_routes()}
    assert "/api/integration/client-token" in paths
    assert "/api/clients/{client_id}/rotate-token" in paths
    for gone in (
        "/api/integration/owner-token",
        "/api/settings/ha-owner-token",
        "/api/settings/ha-owner-token/status",
        "/api/pairing/clients/{client_id}/ha-config",
    ):
        assert gone not in paths


async def test_get_client_config_releases_the_waiter(db_with_client):
    db, client_id, _s, _h = db_with_client

    class _WS:
        def __init__(self):
            self.sent = []
            self.app = types.SimpleNamespace(state=types.SimpleNamespace(db=db))

        async def send_json(self, data):
            self.sent.append(data)

    event = asyncio.Event()
    ws._config_fetch_waiters[client_id] = [event]
    try:
        sock = _WS()
        await ws._handle_message(
            sock, {"type": "get_client_config"}, authenticated=True, client_id=client_id
        )
        assert sock.sent and sock.sent[0]["type"] == "client_config"
        assert event.is_set()
    finally:
        ws._config_fetch_waiters.pop(client_id, None)
