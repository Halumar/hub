"""Watches: access through the phone.

* Onboarding of a watch: the hub keeps the watch's public key, sends
  ``onboard_ok`` and closes the phone-held connection without authenticating
  it; ``client_config`` of a watch never carries its HA token in clear.
* ``watch_access_get`` → ``watch_access``: pending / ready (the watch's
  ``client_config`` sealed to its key) / gone; only for the phone that set it
  up. Pushed to that phone after every ``client_config_updated`` of the watch
  (e.g. a token rotation).
* A rotation of a watch with remote access waits for its phone.
* ``POST /api/client/leave`` with the device secret (the watch signs off).
"""

import asyncio
import json
import types

import api.websocket as ws
import pytest
from api import pairing
from api.auth import AuthMiddleware
from api.pairing import clients as clients_api
from api.pairing import tokens as tokens_api
from api.ws.client import _get_client_config
from cryptography.hazmat.primitives import serialization
from services import audit_log, device_provisioning, onboard_crypto, watch_access

from .test_onboarding import (  # noqa: F401 - _clean_state is an autouse fixture
    TV_INFO,
    _ask,
    _clean_state,
    _phone,
    _send,
    _start,
)

WATCH_INFO = {**TV_INFO, "platform": "watchos", "device_model": "Watch7,9"}


def _raw(priv) -> bytes:
    return priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


async def _paired_watch(db, *, phone_overrides=None):
    """A watch set up by a phone; returns (phone ws, phone id, watch id, watch key)."""
    watch, priv, pub = await _start(db, name="Apple Watch", info=WATCH_INFO)
    sid = watch.last()["session_id"]
    phone, phone_id = await _phone(db, overrides=phone_overrides or {})
    await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub)})
    ok = phone.last()
    assert ok["type"] == "onboard_confirm_ok", ok
    return watch, phone, phone_id, ok["client_id"], priv


def _open(priv, message: dict) -> dict:
    sealed = message["sealed"]
    assert sealed["suite"] == onboard_crypto.SUITE
    return json.loads(
        onboard_crypto.open_sealed(
            _raw(priv),
            onboard_crypto.b64url_decode(sealed["enc"]),
            onboard_crypto.b64url_decode(sealed["ciphertext"]),
            info=watch_access.INFO,
            aad=message["client_id"].encode(),
        )
    )


async def _ask_access(phone, db, watch_id):
    await _send(phone, db, {"type": "watch_access_get", "client_id": watch_id})
    return phone.last()


class TestOnboarding:
    async def test_watch_connection_is_closed_not_authenticated(self, db):
        watch, phone, _phone_id, watch_id, priv = await _paired_watch(db)
        delivered = watch.last()
        assert delivered["type"] == "onboard_ok" and delivered["client_id"] == watch_id
        assert watch.closed == (1000, "Watch onboarded")
        assert ws._clients[watch]["authenticated"] is False
        assert ws._clients[watch]["client_id"] is None
        row = await db.fetchone(
            "SELECT seal_key, platform FROM paired_clients WHERE id = ?", (watch_id,)
        )
        pub = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        assert row == {"seal_key": onboard_crypto.b64url(pub), "platform": "watchos"}

    async def test_other_devices_keep_no_seal_key(self, db):
        tv, _priv, pub = await _start(db)
        phone, _ = await _phone(db)
        await _send(
            phone, db, {"type": "onboard_confirm", **_ask(tv.last()["session_id"], pub)}
        )
        new_id = phone.last()["client_id"]
        row = await db.fetchone(
            "SELECT seal_key FROM paired_clients WHERE id = ?", (new_id,)
        )
        assert row["seal_key"] is None
        assert ws._clients[tv]["authenticated"] is True and tv.closed is None

    async def test_client_config_of_a_watch_has_no_token_in_clear(self, db):
        _w, _p, _pid, watch_id, _priv = await _paired_watch(db)
        await db.store_client_token(
            watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="watch-token"
        )
        assert (await _get_client_config(db, watch_id))["ha_token"] is None
        sealed = await _get_client_config(db, watch_id, sealed_for_watch=True)
        assert sealed["ha_token"] == "watch-token"


class TestWatchAccess:
    async def test_pending_then_ready_with_the_sealed_config(self, db):
        _w, phone, _pid, watch_id, priv = await _paired_watch(db)
        assert await _ask_access(phone, db, watch_id) == {
            "type": "watch_access",
            "client_id": watch_id,
            "status": "pending",
        }
        await db.store_client_token(
            watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="watch-token"
        )
        answer = await _ask_access(phone, db, watch_id)
        assert answer["status"] == "ready"
        assert "watch-token" not in json.dumps(answer)
        plain = _open(priv, answer)
        assert plain["client_id"] == watch_id
        assert plain["issued_at"].endswith("Z")
        assert plain["config"]["ha_token"] == "watch-token"
        assert plain["config"]["profile"]["kind"] == "person"

    async def test_pending_while_the_token_is_held_back(self, db):
        """The hub holds a new token back until the integration has the
        watch's rules (client_config.token_error pending): pending, not a
        ready access without a token."""
        from services import enforcement

        _w, phone, _pid, watch_id, _priv = await _paired_watch(db)
        await db.store_client_token(
            watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="watch-token"
        )
        with enforcement.token_held(watch_id):
            assert (await _ask_access(phone, db, watch_id))["status"] == "pending"
        assert (await _ask_access(phone, db, watch_id))["status"] == "ready"

    async def test_only_the_phone_that_set_it_up(self, db):
        _w, _phone_ws, _pid, watch_id, _priv = await _paired_watch(db)
        other, _ = await _phone(db)
        assert (await _ask_access(other, db, watch_id))["status"] == "gone"
        assert (await _ask_access(other, db, "nope"))["status"] == "gone"

    async def test_not_a_watch_and_removed_watches_are_gone(self, db):
        tv, _priv, pub = await _start(db)
        phone, _ = await _phone(db)
        await _send(
            phone, db, {"type": "onboard_confirm", **_ask(tv.last()["session_id"], pub)}
        )
        assert (await _ask_access(phone, db, phone.last()["client_id"]))[
            "status"
        ] == "gone"
        _w, phone2, _pid, watch_id, _priv = await _paired_watch(db)
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET is_active = 0 WHERE id = ?", (watch_id,)
            )
        assert (await _ask_access(phone2, db, watch_id))["status"] == "gone"

    async def test_admin_sessions_get_an_error(self, db):
        admin = ws._clients
        _w, _p, _pid, watch_id, _priv = await _paired_watch(db)
        from .test_onboarding import _WS

        session = _WS(db, authenticated=True, client_id="ingress")
        await ws._handle_client_message(
            session,
            db,
            {"type": "watch_access_get", "client_id": watch_id},
            "watch_access_get",
            "ingress",
        )
        assert session.last()["type"] == "error"
        assert admin  # registry untouched

    async def test_push_after_a_config_change_of_the_watch(self, db, monkeypatch):
        _w, phone, _pid, watch_id, priv = await _paired_watch(db)
        await db.store_client_token(
            watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="t-1"
        )
        monkeypatch.setattr(watch_access, "PUSH_DEBOUNCE_SECONDS", 0)
        sent = []

        async def _send_to(client_id, message):
            sent.append((client_id, message))
            return True

        monkeypatch.setattr(ws, "send_to_client", _send_to)

        async def _settled():
            for _ in range(200):
                await asyncio.sleep(0.01)
                if not watch_access._dirty and (
                    watch_access._push_task is None or watch_access._push_task.done()
                ):
                    return

        watch_access.reset_state()
        watch_access.register(db, None)
        try:
            watch_access.notify(watch_id)
            await _settled()
            assert [(c, m["status"]) for c, m in sent] == [(_pid, "ready")]
            assert _open(priv, sent[0][1])["config"]["ha_token"] == "t-1"
            # A change for everyone reaches the watch as well; other devices
            # (the phone itself) are not pushed.
            sent.clear()
            watch_access.notify(None)
            watch_access.notify(_pid)
            await _settled()
            assert [c for c, _m in sent] == [_pid]
        finally:
            watch_access.reset_state()

    async def test_client_config_updated_for_the_watch_triggers_the_push(
        self, db, monkeypatch
    ):
        seen = []
        monkeypatch.setattr(watch_access, "notify", lambda cid=None: seen.append(cid))
        import importlib

        broadcast = importlib.import_module("api.ws.broadcast")
        await broadcast.send_to_client("w-1", {"type": "client_config_updated"})
        await broadcast.send_to_client("w-1", {"type": "areas_changed"})
        assert seen == ["w-1"]

    async def test_push_skips_disconnected_phones(self, db, monkeypatch):
        _w, phone, _pid, watch_id, _priv = await _paired_watch(db)
        await db.store_client_token(
            watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="t-1"
        )
        ws._clients.pop(phone)
        assert await watch_access.push(db, None, {watch_id}) == 0

    async def test_phone_connect_releases_deferred_rotations(self, db, monkeypatch):
        _w, _phone_ws, phone_id, watch_id, _priv = await _paired_watch(db)
        released, notified = [], []

        async def _release(_db, client_id, **_k):
            released.append(client_id)
            return True

        monkeypatch.setattr(device_provisioning, "release_deferred_rotation", _release)
        monkeypatch.setattr(
            watch_access, "notify", lambda cid=None: notified.append(cid)
        )
        await watch_access.phone_connected(db, phone_id)
        assert released == [watch_id] and notified == [watch_id]


class TestUnusableKeys:
    """A low-order key is no key; one broken watch never stops
    the push to the others."""

    def test_low_order_key_is_rejected_and_seal_gives_none(self):
        zero = onboard_crypto.b64url(bytes(32))
        assert onboard_crypto.parse_public_key(zero) is None
        assert watch_access.seal(zero, "watch-id", {"ha_token": "x"}) is None

    async def test_push_continues_after_a_failing_watch(self, db, monkeypatch):
        from .profile_helpers import add_client

        phone = await add_client(db, name="Phone", platform="ios")
        watches = []
        for name in ("W1", "W2"):
            watches.append(
                await add_client(
                    db,
                    name=name,
                    platform="watchos",
                    seal_key=onboard_crypto.b64url(b"\x09" * 32),
                    set_up_by_client_id=phone,
                    set_up_method="onboard",
                )
            )
        sent = []

        async def _message(_db, _ha, watch_id, _phone):
            if watch_id == watches[0]:
                raise ValueError("broken")
            return {"type": "watch_access", "client_id": watch_id, "status": "ready"}

        async def _send_to(client_id, message):
            sent.append(message["client_id"])
            return True

        monkeypatch.setattr(watch_access, "message", _message)
        monkeypatch.setattr(ws, "get_connected_client_ids", lambda: {phone})
        monkeypatch.setattr(ws, "send_to_client", _send_to)
        assert await watch_access.push(db, None, None) == 1
        assert sent == [watches[1]]


class TestRotation:
    async def test_rotation_of_a_watch_with_remote_access_waits_for_its_phone(
        self, db, monkeypatch
    ):
        _w, phone, phone_id, watch_id, _priv = await _paired_watch(db)
        await db.store_client_token(
            watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="t-1"
        )
        from services.profiles import store as profiles

        async def _remote(_db, ids):
            return {i: True for i in ids}

        monkeypatch.setattr(profiles, "remote_allowed_map", _remote)
        deferred = []

        async def _defer(_db, client_id):
            deferred.append(client_id)
            return "r-1"

        monkeypatch.setattr(device_provisioning, "defer_rotation", _defer)
        request = types.SimpleNamespace(
            state=types.SimpleNamespace(auth_type="ingress"),
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
            client=None,
            headers={},
        )
        # Phone away: the rotation waits.
        ws._clients.pop(phone)
        answer = await tokens_api.rotate_client_token(request, watch_id)
        assert answer.status_code == 202 and deferred == [watch_id]
        # Phone at home: it runs now (the watch itself is never connected).
        ws._clients[phone] = {"authenticated": True, "client_id": phone_id}
        monkeypatch.setattr(ws, "get_integration_push_connected", lambda: False)
        answer = await tokens_api.rotate_client_token(request, watch_id)
        assert answer.status_code == 202 and deferred == [watch_id]
        assert json.loads(answer.body)["status"] == "pending"
        assert await device_provisioning.rotation_pending(db, watch_id)


def _leave_request(db, secret, body):
    from starlette.requests import Request

    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json")]
    if secret:
        headers.append((b"authorization", f"Bearer {secret}".encode()))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/client/leave",
        "raw_path": b"/api/client/leave",
        "query_string": b"",
        "headers": headers,
        "client": ("192.0.2.40", 5555),
        "server": ("hub", 8100),
        "scheme": "https",
        "app": types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        ),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


class TestLeave:
    @pytest.fixture
    def recorded(self, monkeypatch):
        out = {"integration": [], "kicked": []}

        async def _broadcast(message, audience=None):
            if message.get("type") == "client_kicked":
                out["kicked"].append(message["data"])

        async def _sender(message):
            out["integration"].append(message)
            return True

        async def _disconnect(client_id, code=4003, reason=""):
            return 0

        monkeypatch.setattr(ws, "broadcast", _broadcast)
        monkeypatch.setattr(ws, "disconnect_client", _disconnect)
        monkeypatch.setattr(device_provisioning, "_sender", _sender)
        audit_log.reset_recent()
        return out

    async def _watch_with_secret(self, db):
        from .profile_helpers import add_client

        return await add_client(
            db, name="Apple Watch", secret="w" * 64, platform="watchos"
        ), "w" * 64

    async def test_watch_signs_itself_off(self, db, recorded):
        watch_id, secret = await self._watch_with_secret(db)
        mw = AuthMiddleware(app=None)
        request = _leave_request(db, secret, {"client_id": watch_id})

        async def _next(req):
            return await clients_api.client_leave(req)

        assert await mw.dispatch(request, _next) == {"type": "client_leave_ok"}
        assert (
            await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (watch_id,))
            is None
        )
        assert recorded["kicked"] == [
            {"client_id": watch_id, "is_guest": False, "reason": "left"}
        ]
        assert any(
            m["type"] == "deprovision_client" and m["data"]["client_id"] == watch_id
            for m in recorded["integration"]
        )
        entry = audit_log.recent_entries(5)[0]
        assert entry["action"] == "client_left" and watch_id in entry["details"]
        assert entry["ip"] == "192.0.2.40"
        # The secret is gone with the device.
        again = await mw.dispatch(
            _leave_request(db, secret, {"client_id": watch_id}), _next
        )
        assert again.status_code == 401

    async def test_wrong_secret_or_id(self, db, recorded):
        watch_id, secret = await self._watch_with_secret(db)
        mw = AuthMiddleware(app=None)

        async def _next(req):
            return await clients_api.client_leave(req)

        bad = await mw.dispatch(
            _leave_request(db, "x" * 64, {"client_id": watch_id}), _next
        )
        assert bad.status_code == 401
        with pytest.raises(Exception) as err:
            await mw.dispatch(_leave_request(db, secret, {"client_id": "other"}), _next)
        assert getattr(err.value, "status_code", None) == 422
        assert await db.fetchone(
            "SELECT id FROM paired_clients WHERE id = ?", (watch_id,)
        )

    async def test_route_is_registered(self):
        paths = {(r.path, tuple(sorted(r.methods))) for r in pairing.router.routes}
        assert ("/client/leave", ("POST",)) in paths


class TestRemovalOfThePhone:
    """The watches a phone carries go with it; devices it set
    up stay and are listed in the log entry of the removal."""

    @pytest.fixture
    def recorded(self, monkeypatch):
        out = {"integration": []}

        async def _broadcast(message, audience=None):
            pass

        async def _sender(message):
            out["integration"].append(message)
            return True

        async def _none(*_a, **_k):
            return 0

        monkeypatch.setattr(ws, "broadcast", _broadcast)
        monkeypatch.setattr(ws, "disconnect_client", _none)
        monkeypatch.setattr(ws, "signal_client_removed", _none)
        monkeypatch.setattr(device_provisioning, "_sender", _sender)
        return out

    async def _family(self, db):
        from .profile_helpers import add_client

        phone = await add_client(db, name="Mum's iPhone", platform="ios")
        watch = await add_client(
            db,
            name="Mum's Watch",
            platform="watchos",
            seal_key=onboard_crypto.b64url(b"\x09" * 32),
            set_up_by_client_id=phone,
            set_up_by_name="Mum's iPhone",
            set_up_method="onboard",
        )
        tv = await add_client(
            db,
            name="Living room TV",
            platform="tvos",
            set_up_by_client_id=phone,
            set_up_by_name="Mum's iPhone",
            set_up_method="onboard",
        )
        return phone, watch, tv

    def _req(self, db):
        return types.SimpleNamespace(
            state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(db=db, ha_client=None)
            ),
            client=None,
        )

    async def _gone(self, db, client_id):
        row = await db.fetchone(
            "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
        )
        return row is None

    def _deprovisioned(self, recorded, client_id):
        return any(
            m["type"] == "deprovision_client" and m["data"]["client_id"] == client_id
            for m in recorded["integration"]
        )

    @pytest.mark.parametrize("how", ["delete", "revoke", "kick"])
    async def test_watch_goes_with_its_phone(self, db, recorded, how):
        phone, watch, tv = await self._family(db)
        request = self._req(db)
        if how == "delete":
            await clients_api.delete_client(request, phone, ha_user_removal=None)
        elif how == "revoke":
            await clients_api.revoke_client(request, phone)
        else:
            await clients_api.kick_client(request, phone)
        assert await self._gone(db, watch)
        assert self._deprovisioned(recorded, watch)
        # The TV it set up stays paired and is named in the log entry.
        assert not await self._gone(db, tv)
        rows = await db.fetchall(
            "SELECT client_id, detail_json FROM event_log "
            "WHERE kind IN ('device_removed', 'device_revoked') ORDER BY id"
        )
        by_client = {r["client_id"]: json.loads(r["detail_json"]) for r in rows}
        assert by_client[phone]["set_up_devices"] == [
            {"client_id": tv, "name": "Living room TV"}
        ]
        assert "set_up_devices" not in by_client[watch]
        # The log says the watch went with its phone (not "kicked" by the admin).
        assert by_client[watch]["how"] == "with_phone"
        assert by_client[watch]["with_device"] == {
            "client_id": phone,
            "name": "Mum's iPhone",
        }
        assert "with_device" not in by_client[phone]

    async def test_set_up_device_remove_takes_the_phones_watch(self, db, recorded):
        """A member removing a phone it set up also ends that phone's watch,
        which the member did not set up: without its phone the watch gets no
        access (contracts protocol.md) and set_up_device_remove removes "like
        the admin's Remove". The log names the phone."""
        from api.ws import onboarding as onboarding_ws

        from .profile_helpers import add_client

        dad = await add_client(db, name="Dad's iPhone", platform="ios")
        phone, watch, _tv = await self._family(db)
        await db.execute(
            "UPDATE paired_clients SET set_up_by_client_id = ?, set_up_by_name = ?, "
            "set_up_method = 'onboard' WHERE id = ?",
            (dad, "Dad's iPhone", phone),
        )
        await db.commit()

        class _Sock:
            app = types.SimpleNamespace(state=types.SimpleNamespace(ha_client=None))
            client = types.SimpleNamespace(host="192.0.2.20")
            headers = {}
            sent = []

            async def send_json(self, message):
                self.sent.append(message)

        sock = _Sock()
        await onboarding_ws.handle_remove_set_up(sock, db, {"client_id": phone}, dad)
        assert sock.sent[-1] == {"type": "set_up_device_removed", "client_id": phone}
        assert await self._gone(db, watch)
        row = await db.fetchone(
            "SELECT actor, detail_json FROM event_log WHERE client_id = ? "
            "AND kind = 'device_removed'",
            (watch,),
        )
        detail = json.loads(row["detail_json"])
        assert detail["how"] == "with_phone"
        assert detail["with_device"]["client_id"] == phone
        assert row["actor"] == "Dad's iPhone"


async def test_contract_shapes(db):
    from .test_profiles_api import _contract_validator

    validator = _contract_validator("ws/server/watch_access.json")
    plain = _contract_validator("crypto/watch-access-plaintext.json")
    if validator is None or plain is None:
        pytest.skip("contract checkout with watch_access not found (set CONTRACTS_DIR)")
    _w, phone, _pid, watch_id, priv = await _paired_watch(db)
    pending = await _ask_access(phone, db, watch_id)
    await db.store_client_token(
        watch_id, ha_user_id="u-w", refresh_token_id="rt-1", token="watch-token"
    )
    ready = await _ask_access(phone, db, watch_id)
    gone = await _ask_access(phone, db, "00000000-0000-4000-8000-000000000000")
    for message in (pending, ready, gone):
        assert sorted(validator.iter_errors(message), key=str) == [], message
    errors = sorted(plain.iter_errors(_open(priv, ready)), key=str)
    assert errors == [], [e.message for e in errors[:3]]
