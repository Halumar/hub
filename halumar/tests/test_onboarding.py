"""Setting up a device from an already paired device.

* HPKE seal (RFC 9180 base mode, X25519/HKDF-SHA256/ChaCha20Poly1305): RFC
  vector A.2.1 and the contract vector onboard-v1.
* ``onboard_start`` (TV, TLS only) -> ``onboard_session``; limits; the
  connection stays open while the session runs.
* ``onboard_lookup`` / ``onboard_confirm`` / ``onboard_decline`` (paired
  device): permission (admin release or hub option, guests never), key and
  hub binding, single use, failure budget.
* Confirm creates the device (member, scope of the confirming device, local
  only, no right to add devices) and delivers the sealed secret; the TV
  connection is authenticated as the new device.
* Admin REST: running sessions, cancel; ``client_config.rules.setup_devices``
  (profile rule).
* Kinds and profiles (never stronger than the confirming device), the
  6-digit code, guests, children with family managers, wall panels, “Set up
  by me”.
"""

import base64
import hashlib
import json
import time
import types
import uuid

import api.websocket as ws
import pytest

from .profile_helpers import add_client, starter_profile
from services.profiles import store
from api import pairing
from api.pairing import onboarding as onboarding_api
from api.ws import onboarding as onboarding_ws
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import HTTPException
from models.database import Database
from services import client_activity, crypto, onboard_crypto, onboarding
from starlette.websockets import WebSocketState

HUB_ID = "8b7e3b10-2c4d-4e5f-9a0b-1c2d3e4f5a6b"
TV_INFO = {
    "protocol_version": 1,
    "platform": "tvos",
    "app_version": "2026.12.0",
    "app_build": "7",
    "device_model": "AppleTV14,1",
}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    monkeypatch.setattr(onboarding_ws, "own_hub_id", lambda: HUB_ID)
    crypto.reset_cache()
    ws._clients.clear()
    onboarding.reset()
    pairing.clear_active_pins()
    events = []

    async def _capture(message, *a, **k):
        events.append(message)

    monkeypatch.setattr(ws, "broadcast", _capture)
    yield events
    ws._clients.clear()
    onboarding.reset()
    crypto.reset_cache()


class _WS:
    """Fake WebSocket registered in the connection registry."""

    def __init__(
        self,
        db,
        *,
        host="192.0.2.10",
        scheme="wss",
        authenticated=False,
        client_id=None,
    ):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host=host)
        self.url = types.SimpleNamespace(hostname="hub", port=8100, scheme=scheme)
        self.sent = []
        self.closed = None
        self.application_state = WebSocketState.CONNECTED
        ws._clients[self] = {
            "authenticated": authenticated,
            "client_id": client_id,
            "pair_attempts": 0,
            "auth_attempts": 0,
            "connected_at": time.time(),
            "ip": host,
        }

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def last(self, type_=None):
        msgs = [m for m in self.sent if type_ is None or m["type"] == type_]
        return msgs[-1] if msgs else None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _tv_key():
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return priv, _b64(pub)


async def _client(db, *, secret=None, **columns):
    secret = secret or uuid.uuid4().hex
    return await add_client(
        db, name="iPhone", secret=secret, last_seen=Database.now(), **columns
    )


def _request(db, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


async def _start(db, *, host="192.0.2.10", name="Apple TV Wohnzimmer", info=None):
    tv = _WS(db, host=host)
    priv, pub = _tv_key()
    await ws._handle_message(
        tv,
        {
            "type": "onboard_start",
            "public_key": pub,
            "client_name": name,
            **(info or TV_INFO),
        },
        False,
        None,
    )
    return tv, priv, pub


ALL_KINDS = ["guest", "child", "panel", "tv", "house_sitter", "babysitter", "person"]
SETUP_ALL = {"enabled": True, "kinds": ALL_KINDS}
# Kind a test confirms when it does not name one (by the waiting platform).
DEFAULT_KIND = {
    "tvos": "tv",
    "android_tv": "tv",
    "ipados": "panel",
    "android_tablet": "panel",
    "ios": "person",
    "android": "person",
}


async def _phone(db, **columns):
    overrides = {"setup_devices": SETUP_ALL, **columns.pop("overrides", {})}
    cid = await _client(db, overrides=overrides, **columns)
    return _WS(db, host="192.0.2.20", authenticated=True, client_id=cid), cid


async def _send(phone, db, msg):
    cid = ws._clients[phone]["client_id"]
    if msg["type"] == "onboard_confirm" and "kind" not in msg:
        msg = {**msg, **(await _pick(db, cid, msg.get("session_id")))}
    await ws._handle_client_message(phone, db, msg, msg["type"], cid)


async def _pick(db, cid, session_id) -> dict:
    """kind and profile_id a test confirms with: the kind of the platform and
    the default profile of the platform when offered, else the first."""
    session = onboarding.find(session_id) if isinstance(session_id, str) else None
    if session is None or cid in (None, "ingress"):
        return {"kind": "tv", "profile_id": "x"}
    try:
        grant = await onboarding.grant_for(db, cid, session.info.platform)
    except onboarding.SetupError:
        return {"kind": "tv", "profile_id": "x"}
    if not grant["kinds"]:
        return {"kind": "tv", "profile_id": "x"}
    by_kind = {k["kind"]: k["profiles"] for k in grant["kinds"]}
    kind = DEFAULT_KIND.get(session.info.platform)
    if kind not in by_kind:
        kind = grant["kinds"][0]["kind"]
    default = await store.default_profile_for(db, session.info.platform, False)
    profiles = by_kind[kind]
    pid = next((p["id"] for p in profiles if p["id"] == default), profiles[0]["id"])
    return {"kind": kind, "profile_id": pid}


def _ask(session_id, pub, **extra):
    return {"session_id": session_id, "public_key": pub, "hub_id": HUB_ID, **extra}


# ── Crypto ───────────────────────────────────────────────────────────────


class TestSeal:
    def test_rfc9180_a21_vector(self):
        sk_e = bytes.fromhex(
            "f4ec9b33b792c372c1d2c2063507b684ef925b8c75a42dbcbf57d63ccd381600"
        )
        pk_r = bytes.fromhex(
            "4310ee97d88cc1f088a5576c77ab0cf5c3ac797f3d95139c6c84b5429c59662a"
        )
        sk_r = bytes.fromhex(
            "8057991eef8f1f1af18f4a9491d16a1ce333f695d4db8e38da75975c4478e0fb"
        )
        info = bytes.fromhex("4f6465206f6e2061204772656369616e2055726e")
        pt = bytes.fromhex("4265617574792069732074727574682c20747275746820626561757479")
        aad = bytes.fromhex("436f756e742d30")
        enc, ct = onboard_crypto.seal(pk_r, pt, info=info, aad=aad, sk_e=sk_e)
        assert enc.hex() == (
            "1afa08d3dec047a643885163f1180476fa7ddb54c6a8029ea33f95796bf2ac4a"
        )
        assert ct.hex() == (
            "1c5250d8034ec2b784ba2cfd69dbdb8af406cfe3ff938e131f0def8c8b60b4db"
            "21993c62ce81883d2dd1b51a28"
        )
        assert onboard_crypto.open_sealed(sk_r, enc, ct, info=info, aad=aad) == pt

    def test_contract_vector_onboard_v1(self):
        sk_r = hashlib.sha256(b"onboard-vector-tv-key").digest()
        sk_e = hashlib.sha256(b"onboard-vector-hub-ephemeral").digest()
        pk_r = (
            X25519PrivateKey.from_private_bytes(sk_r)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        plain = json.dumps(
            {
                "client_id": "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b",
                "secret": "0123456789abcdef" * 4,
            },
            separators=(",", ":"),
        ).encode()
        enc, ct = onboard_crypto.seal(
            pk_r, plain, aad=b"q8Zr3vT1yL0pXw5nB2cD4e", sk_e=sk_e
        )
        assert _b64(enc) == "_gkKHeYt1_uzYA2_tZaDa4cmi2DvV21qVeiTJoF_TSE"
        assert _b64(ct).startswith("2ZFPicCd8ehI5KDc7QnWl5DDubBs6iVWD82Wz0_Rz3SK")

    def test_public_key_parsing_is_strict(self):
        _, pub = _tv_key()
        assert onboard_crypto.parse_public_key(pub) is not None
        assert onboard_crypto.parse_public_key(pub + "=") is None
        assert onboard_crypto.parse_public_key(pub[:-1]) is None
        assert onboard_crypto.parse_public_key("x" * 43 + "+") is None
        assert onboard_crypto.parse_public_key(None) is None


# ── Waiting device ───────────────────────────────────────────────────────


class TestStart:
    async def test_session_is_opened_over_tls(self, db, _clean_state):
        tv, _, _ = await _start(db)
        msg = tv.last()
        assert msg["type"] == "onboard_session"
        assert msg["hub_id"] == HUB_ID and msg["expires_in"] == 300
        assert len(msg["session_id"]) == 22
        started = [e for e in _clean_state if e["type"] == "onboarding_started"]
        assert started and started[0]["data"]["client_name"] == "Apple TV Wohnzimmer"
        assert "session_id" not in json.dumps(started[0])
        # The connection may stay open until the session ends (not 30 s).
        assert onboarding_ws.deadline(tv, time.time() + 30) > time.time() + 290

    @pytest.mark.parametrize("model", ["AppleTV14,1", None])
    async def test_started_broadcast_matches_the_contract(
        self, db, _clean_state, model
    ):
        """device_model is optional in onboarding_started, never null."""
        from .test_profiles_api import _contract_validator

        info = {k: v for k, v in TV_INFO.items() if k != "device_model"}
        if model:
            info["device_model"] = model
        await _start(db, info=info)
        started = [e for e in _clean_state if e["type"] == "onboarding_started"][0]
        assert started["data"].get("device_model") == model
        assert ("device_model" in started["data"]) is bool(model)
        validator = _contract_validator("ws/broadcast/onboarding_started.json")
        if validator is None:
            pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
        assert [e.message for e in validator.iter_errors(started)] == []

    async def test_cleartext_port_is_refused(self, db):
        tv = _WS(db, scheme="ws")
        _, pub = _tv_key()
        await ws._handle_message(
            tv,
            {
                "type": "onboard_start",
                "public_key": pub,
                "client_name": "TV",
                **TV_INFO,
            },
            False,
            None,
        )
        assert tv.last()["reason"] == "tls_required"
        assert onboarding.sessions() == []

    @pytest.mark.parametrize(
        "patch",
        [
            {"public_key": "short"},
            {"public_key": None},
            # Low-order X25519 points: nothing can be sealed to them.
            {"public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
            {"public_key": "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
            {"public_key": "4Ot6fDtBuK4WVuP68Z_EatoJjeucMrH9hmIFFl9JuAA"},
            {"client_name": ""},
            {"client_name": "x" * 101},
        ],
    )
    async def test_invalid_request(self, db, patch):
        tv = _WS(db)
        _, pub = _tv_key()
        msg = {
            "type": "onboard_start",
            "public_key": pub,
            "client_name": "TV",
            **TV_INFO,
        }
        msg.update(patch)
        await ws._handle_message(tv, msg, False, None)
        assert tv.last()["reason"] == "invalid_request"

    async def test_old_app_gets_upgrade_required(self, db):
        tv = _WS(db)
        _, pub = _tv_key()
        await ws._handle_message(
            tv,
            {"type": "onboard_start", "public_key": pub, "client_name": "TV"},
            False,
            None,
        )
        assert tv.last()["type"] == "upgrade_required"
        assert tv.closed[0] == 4426

    async def test_authenticated_connection_is_refused(self, db):
        phone, _ = await _phone(db)
        _, pub = _tv_key()
        await ws._handle_message(
            phone,
            {
                "type": "onboard_start",
                "public_key": pub,
                "client_name": "TV",
                **TV_INFO,
            },
            True,
            ws._clients[phone]["client_id"],
        )
        assert phone.last()["reason"] == "already_authenticated"

    async def test_new_code_replaces_the_session(self, db, _clean_state):
        tv, _, _ = await _start(db)
        first = tv.last()["session_id"]
        _, pub = _tv_key()
        await ws._handle_message(
            tv,
            {
                "type": "onboard_start",
                "public_key": pub,
                "client_name": "TV",
                **TV_INFO,
            },
            False,
            None,
        )
        assert tv.last()["session_id"] != first
        assert len(onboarding.sessions()) == 1
        ended = [e for e in _clean_state if e["type"] == "onboarding_ended"]
        assert ended[-1]["data"]["result"] == "replaced"

    async def test_limits_per_ip_and_connection(self, db):
        for _ in range(onboarding.MAX_OPEN_PER_IP):
            await _start(db)
        tv, _, _ = await _start(db)
        assert tv.last()["reason"] == "rate_limited"
        # Another address is not affected.
        other, _, _ = await _start(db, host="192.0.2.99")
        assert other.last()["type"] == "onboard_session"
        # Per connection at most MAX_START_ATTEMPTS starts.
        for _ in range(onboarding_ws.MAX_START_ATTEMPTS):
            _, pub = _tv_key()
            await ws._handle_message(
                other,
                {
                    "type": "onboard_start",
                    "public_key": pub,
                    "client_name": "T",
                    **TV_INFO,
                },
                False,
                None,
            )
        assert other.last()["reason"] == "rate_limited"

    async def test_starts_per_ip_window(self, db, monkeypatch):
        monkeypatch.setattr(onboarding, "MAX_STARTS_PER_IP", 2)
        a, _, _ = await _start(db)
        await onboarding_ws.on_disconnect(a)
        b, _, _ = await _start(db)
        await onboarding_ws.on_disconnect(b)
        c, _, _ = await _start(db)
        assert c.last()["reason"] == "rate_limited"

    async def test_disconnect_ends_the_session(self, db, _clean_state):
        tv, _, _ = await _start(db)
        await onboarding_ws.on_disconnect(tv)
        assert onboarding.sessions() == []
        assert _clean_state[-1]["data"]["result"] == "disconnected"

    async def test_expiry_tells_the_tv(self, db, monkeypatch):
        tv, _, _ = await _start(db)
        session = onboarding.session_of(tv)
        session.expires_at = time.time() - 1
        assert await onboarding_ws.expire(tv)
        assert tv.last()["reason"] == "session_expired"
        assert onboarding.session_of(tv) is None


# ── Confirming device ────────────────────────────────────────────────────


class TestLookupAndConfirm:
    async def test_member_without_release_may_not(self, db):
        tv, _, pub = await _start(db)
        cid = await _client(db)
        phone = _WS(db, authenticated=True, client_id=cid)
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        assert phone.last()["reason"] == "not_permitted"

    async def test_profile_rule_allows_members_but_never_guests(self, db):
        """The rule setup_devices; the guest boundary keeps it off."""
        tv, _, pub = await _start(db)
        sid = tv.last()["session_id"]
        member = _WS(
            db,
            authenticated=True,
            client_id=await _client(db, overrides={"setup_devices": SETUP_ALL}),
        )
        await _send(member, db, {"type": "onboard_lookup", **_ask(sid, pub)})
        assert member.last()["type"] == "onboard_info"
        guest = _WS(
            db,
            authenticated=True,
            client_id=await _client(
                db,
                is_guest=1,
                expires_at="2099-01-01T00:00:00+00:00",
                overrides={"setup_devices": SETUP_ALL},
            ),
        )
        await _send(guest, db, {"type": "onboard_lookup", **_ask(sid, pub)})
        assert guest.last()["reason"] == "not_permitted"

    async def test_child_device_may_not_even_with_release(self, db):
        # Parental controls: boundary child_protection on setup_devices (and a
        # child profile is not of kind person).
        tv, _, pub = await _start(db)
        phone, _ = await _phone(db, profile="child")
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        assert phone.last()["reason"] == "not_permitted"

    async def test_admin_sessions_cannot_confirm(self, db):
        tv, _, pub = await _start(db)
        admin = _WS(db, authenticated=True, client_id="ingress")
        await _send(
            admin, db, {"type": "onboard_confirm", **_ask(tv.last()["session_id"], pub)}
        )
        assert admin.last()["reason"] == "not_permitted"

    async def test_lookup_shows_name_device_and_grant(self, db, db_with_area):
        tv, _, pub = await _start(db)
        phone, _ = await _phone(db)
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        info = phone.last()
        assert info["type"] == "onboard_info"
        assert info["client_name"] == "Apple TV Wohnzimmer"
        assert info["platform"] == "tvos" and info["device_model"] == "AppleTV14,1"
        assert info["public_key"] == pub and info["hub_id"] == HUB_ID
        grant = info["grant"]
        assert grant["all_areas"] is True and grant["tags"] == ["*"]
        # A TV can only be set up as a TV; the TV profile: home only.
        assert [k["kind"] for k in grant["kinds"]] == ["tv"]
        (tv_profile,) = grant["kinds"][0]["profiles"]
        assert tv_profile["name"] == "Fernseher" and tv_profile["kind"] == "tv"
        assert tv_profile["remote_access"] is False
        assert tv_profile["remote_choice"] is False
        assert tv_profile["area_choice"] is True
        assert grant["own_watch"] is False
        assert grant["areas"] and grant["areas"][0]["id"]  # the test area

    async def test_grant_follows_the_scope_of_the_confirming_device(self, db):
        now = Database.now()
        async with db.transaction():
            for aid, name, tag in (("a1", "Wohnzimmer", "eg"), ("a2", "Bad", "og")):
                await db.execute(
                    "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (aid, name, now, now),
                )
                await db.execute(
                    "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (aid, tag)
                )
        tv, _, pub = await _start(db)
        phone, _ = await _phone(db, tags='["og"]')
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        grant = phone.last()["grant"]
        assert grant["tags"] == ["og"] and grant["all_areas"] is False
        assert grant["areas"] == [{"id": "a2", "name": "Bad", "icon": None}]

    async def test_unknown_session_and_failure_budget(self, db):
        phone, _ = await _phone(db)
        _, pub = _tv_key()
        for _ in range(onboarding.MAX_FAILURES_PER_DEVICE):
            await _send(phone, db, {"type": "onboard_lookup", **_ask("A" * 22, pub)})
            assert phone.last()["reason"] == "session_not_found"
        await _send(phone, db, {"type": "onboard_lookup", **_ask("A" * 22, pub)})
        assert phone.last()["reason"] == "rate_limited"

    async def test_hub_mismatch(self, db):
        tv, _, pub = await _start(db)
        phone, _ = await _phone(db)
        msg = _ask(tv.last()["session_id"], pub)
        msg["hub_id"] = str(uuid.uuid4())
        await _send(phone, db, {"type": "onboard_lookup", **msg})
        assert phone.last()["reason"] == "hub_mismatch"
        assert onboarding.session_of(tv) is not None

    async def test_key_mismatch_invalidates_the_session(self, db, _clean_state):
        tv, _, _ = await _start(db)
        sid = tv.last()["session_id"]
        phone, _ = await _phone(db)
        _, other = _tv_key()
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, other)})
        assert phone.last()["reason"] == "session_invalidated"
        assert tv.last()["reason"] == "session_invalidated"
        assert onboarding.sessions() == []
        assert _clean_state[-1]["data"]["result"] == "invalidated"
        rows = await db.fetchall("SELECT id FROM paired_clients")
        assert len(rows) == 1  # only the phone

    async def test_expired_session(self, db):
        tv, _, pub = await _start(db)
        sid = tv.last()["session_id"]
        onboarding.session_of(tv).expires_at = time.time() - 1
        phone, _ = await _phone(db)
        await _send(phone, db, {"type": "onboard_lookup", **_ask(sid, pub)})
        assert phone.last()["reason"] == "session_expired"
        assert tv.last()["reason"] == "session_expired"

    async def test_decline(self, db, _clean_state):
        tv, _, pub = await _start(db)
        phone, cid = await _phone(db)
        await _send(
            phone,
            db,
            {"type": "onboard_decline", "session_id": tv.last()["session_id"]},
        )
        assert tv.last()["reason"] == "declined"
        assert _clean_state[-1]["data"] == {
            "id": _clean_state[-1]["data"]["id"],
            "result": "declined",
            "confirmed_by": cid,
        }
        await _send(phone, db, {"type": "onboard_decline", "session_id": "A" * 22})
        assert phone.last()["reason"] == "session_not_found"

    async def test_confirm_creates_the_device_and_seals_the_secret(
        self, db, _clean_state
    ):
        tv, priv, pub = await _start(db)
        sid = tv.last()["session_id"]
        phone, cid = await _phone(db, tags='["og"]')
        await _send(
            phone,
            db,
            {"type": "onboard_confirm", **_ask(sid, pub, client_name="TV Küche")},
        )

        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok" and ok["client_name"] == "TV Küche"
        new_id = ok["client_id"]

        delivered = tv.last()
        assert delivered["type"] == "onboard_ok" and delivered["client_id"] == new_id
        assert "secret" not in delivered
        assert delivered["protocol_version"] >= 1 and "min_app_version" in delivered
        sealed = delivered["sealed"]
        assert sealed["suite"] == "X25519-SHA256-ChaCha20Poly1305"
        sk = priv.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        plain = json.loads(
            onboard_crypto.open_sealed(
                sk,
                onboard_crypto.b64url_decode(sealed["enc"]),
                onboard_crypto.b64url_decode(sealed["ciphertext"]),
                aad=sid.encode(),
            )
        )
        assert plain["client_id"] == new_id

        row = await db.fetchone("SELECT * FROM paired_clients WHERE id = ?", (new_id,))
        assert (
            row["secret_hash"] == hashlib.sha256(plain["secret"].encode()).hexdigest()
        )
        assert row["name"] == "TV Küche" and row["access"] == "member"
        assert row["platform"] == "tvos" and row["ha_token_status"] == "pending"
        # Default profile of its type; the confirmer's areas as an adjustment.
        assert row["profile_id"] == await starter_profile(db, "apple_tv")
        effective = (await store.resolve_client(db, new_id))[2]
        assert effective["areas"]["source"] == "device"
        assert effective["areas"]["value"]["tags"] == ["og"]
        assert effective["remote_access"]["value"] is False
        assert effective["setup_devices"]["source"] == "not_applicable"  # TV
        assert row["set_up_by_client_id"] == cid and row["set_up_method"] == "onboard"
        assert row["set_up_by_name"] == "iPhone"

        # The TV connection is the new device now.
        assert ws._clients[tv]["authenticated"] is True
        assert ws._clients[tv]["client_id"] == new_id

        events = {e["event"]: e for e in await client_activity.list_events(db, new_id)}
        assert events["paired"]["detail"]["method"] == "onboard"
        assert events["paired"]["detail"]["confirmed_by"] == cid
        mine = await client_activity.list_events(db, cid)
        assert mine[0]["event"] == "device_added"
        assert mine[0]["detail"]["added_client_id"] == new_id
        assert mine[0]["detail"]["kind"] == "tv"
        assert ok["kind"] == "tv" and ok["profile"]["name"] == "Fernseher"

        types_ = [e["type"] for e in _clean_state]
        assert "client_paired" in types_
        ended = [e for e in _clean_state if e["type"] == "onboarding_ended"][-1]
        assert ended["data"]["result"] == "confirmed"
        assert ended["data"]["client_id"] == new_id

        # Single use.
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub)})
        assert phone.last()["reason"] == "session_not_found"

    async def test_a_key_that_cannot_be_sealed_to_leaves_no_device(
        self, db, monkeypatch
    ):
        """Sealing runs before the device is inserted."""
        from services import onboard_crypto

        tv, _, pub = await _start(db)
        sid = tv.last()["session_id"]
        phone, _ = await _phone(db)
        before = await db.fetchone("SELECT COUNT(*) AS n FROM paired_clients")

        def _fail(*_a, **_k):
            raise ValueError("low-order point")

        monkeypatch.setattr(onboard_crypto, "seal_credentials", _fail)
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub)})
        assert phone.last()["type"] == "onboard_error"
        assert tv.last()["reason"] == "cancelled"
        after = await db.fetchone("SELECT COUNT(*) AS n FROM paired_clients")
        assert after["n"] == before["n"]

    async def test_confirm_after_the_tv_left(self, db):
        tv, _, pub = await _start(db)
        sid = tv.last()["session_id"]
        await onboarding_ws.on_disconnect(tv)
        phone, _ = await _phone(db)
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub)})
        assert phone.last()["reason"] == "session_not_found"

    async def test_invalid_confirm_name(self, db):
        tv, _, pub = await _start(db)
        phone, _ = await _phone(db)
        await _send(
            phone,
            db,
            {
                "type": "onboard_confirm",
                **_ask(tv.last()["session_id"], pub, client_name=""),
            },
        )
        assert phone.last()["reason"] == "invalid_request"
        assert onboarding.session_of(tv) is not None


# ── Admin REST and client_config ─────────────────────────────────────────


class TestAdmin:
    async def test_list_and_cancel(self, db):
        tv, _, pub = await _start(db)
        listed = await onboarding_api.list_onboarding_sessions(_request(db))
        dumped = listed.model_dump()
        assert "members_can_add_devices" not in dumped
        assert dumped["expires_in"] == 300
        entry = dumped["sessions"][0]
        assert entry["client_name"] == "Apple TV Wohnzimmer"
        assert entry["source_ip"] == "192.0.2.10"
        text = json.dumps(dumped)
        assert tv.last()["session_id"] not in text and pub not in text

        await onboarding_api.cancel_onboarding_session(_request(db), entry["id"])
        assert tv.last()["reason"] == "cancelled"
        with pytest.raises(HTTPException) as e:
            await onboarding_api.cancel_onboarding_session(_request(db), entry["id"])
        assert e.value.status_code == 404

    async def test_admin_only(self, db):
        with pytest.raises(HTTPException) as e:
            await onboarding_api.list_onboarding_sessions(_request(db, auth="client"))
        assert e.value.status_code == 403
        assert not hasattr(onboarding_api, "update_device_onboarding_settings")

    async def test_rule_in_client_config(self, db, _clean_state):
        """client_config.rules.setup_devices (guests never: boundary);
        a guest profile is of kind guest, the rule does not apply there."""
        member = await _client(db)
        released = await _client(db, overrides={"setup_devices": SETUP_ALL})
        guest = await _client(
            db,
            is_guest=1,
            expires_at="2099-01-01T00:00:00+00:00",
            profile="adult",
            overrides={"setup_devices": SETUP_ALL},
        )
        guest_kind = await _client(
            db, is_guest=1, expires_at="2099-01-01T00:00:00+00:00"
        )
        cfg = ws._get_client_config
        off = {"enabled": False, "kinds": []}
        assert (await cfg(db, member))["rules"]["setup_devices"]["value"] == off
        assert (await cfg(db, released))["rules"]["setup_devices"]["value"] == SETUP_ALL
        rule = (await cfg(db, guest))["rules"]["setup_devices"]
        assert rule["value"] == off and rule["reason"] == "boundary_guest"
        assert "setup_devices" not in (await cfg(db, guest_kind))["rules"]
        assert (await cfg(db, member))["profile"]["kind"] == "person"
        assert (await cfg(db, guest_kind))["profile"]["kind"] == "guest"
        assert (await cfg(db, "ingress"))["rules"] == {}

    async def test_apple_watch_needs_the_watch_rule(self, db):
        """An Apple Watch is added by an iPhone whose rule watch is on."""
        info = {**TV_INFO, "platform": "watchos"}
        tv, _, pub = await _start(db, info=info)
        phone, _ = await _phone(db, overrides={"watch": False})
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        assert phone.last()["reason"] == "not_permitted"
        phone, _ = await _phone(db)
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        assert phone.last()["type"] == "onboard_info"

    async def test_wear_os_needs_the_watch_rule(self, db):
        """A Wear OS watch is added by an Android phone whose rule watch
        is on, like an Apple Watch."""
        info = {**TV_INFO, "platform": "wearos", "device_model": "Google Pixel Watch 4"}
        tv, _, pub = await _start(db, info=info)
        phone, _ = await _phone(db, platform="android", overrides={"watch": False})
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        assert phone.last()["reason"] == "not_permitted"
        phone, _ = await _phone(db, platform="android")
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        assert phone.last()["type"] == "onboard_info"

    @pytest.mark.parametrize(
        ("platform", "phone_platform", "suggestion"),
        [
            ("android_tv", "ios", "apple_tv"),
            ("tvos", "android", "apple_tv"),
            ("android_tablet", "android", "wall_panel"),
        ],
    )
    async def test_cross_vendor_confirm_gives_the_default_profile_of_the_type(
        self, db, platform, phone_platform, suggestion
    ):
        """An iPhone confirms an Android TV and an Android phone an
        Apple TV; the new device gets the default profile of its own type."""
        info = {**TV_INFO, "platform": platform, "device_model": "Google TV Streamer"}
        tv, _, pub = await _start(db, name="Fernseher", info=info)
        sid = tv.last()["session_id"]
        phone, _ = await _phone(db, platform=phone_platform)
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub)})
        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok"
        row = await db.fetchone(
            "SELECT platform, profile_id FROM paired_clients WHERE id = ?",
            (ok["client_id"],),
        )
        assert row["platform"] == platform
        assert row["profile_id"] == await starter_profile(db, suggestion)
        effective = (await store.resolve_client(db, ok["client_id"]))[2]
        if suggestion == "apple_tv":
            assert effective["remote_access"]["value"] is False
            assert effective["control_path"]["value"] == "checked"
            assert effective["setup_devices"]["source"] == "not_applicable"


# ── Endpoint loop ────────────────────────────────────────────────────────


class _LiveWS(_WS):
    """Fake socket for websocket_endpoint with a message queue."""

    def __init__(self, db):
        super().__init__(db)
        ws._clients.pop(self, None)  # the endpoint registers it
        self.application_state = WebSocketState.CONNECTING
        import asyncio

        self.queue = asyncio.Queue()

    async def accept(self):
        self.application_state = WebSocketState.CONNECTED

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED

    async def receive_text(self):
        item = await self.queue.get()
        if item is None:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return json.dumps(item)


class TestEndpoint:
    async def test_tv_waits_beyond_auth_timeout_and_is_onboarded(self, db, monkeypatch):
        import asyncio

        from api.ws import handshake

        monkeypatch.setattr(handshake, "UNAUTH_TIMEOUT_SECONDS", 0.1)
        tv = _LiveWS(db)
        task = asyncio.create_task(ws.websocket_endpoint(tv))
        priv, pub = _tv_key()
        await tv.queue.put(
            {"type": "onboard_start", "public_key": pub, "client_name": "TV", **TV_INFO}
        )
        for _ in range(50):
            await asyncio.sleep(0.01)
            if tv.last("onboard_session"):
                break
        sid = tv.last("onboard_session")["session_id"]
        await asyncio.sleep(0.3)  # longer than the auth timeout
        assert tv.closed is None

        phone, _ = await _phone(db)
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub)})
        assert tv.last("onboard_ok")
        await tv.queue.put({"type": "get_client_config"})
        for _ in range(50):
            await asyncio.sleep(0.01)
            if tv.last("client_config"):
                break
        # setup_devices does not apply to a TV: not in its rules.
        assert "setup_devices" not in tv.last("client_config")["data"]["rules"]
        await tv.queue.put(None)
        await asyncio.wait_for(task, 2)

    async def test_session_expiry_closes_4002(self, db, monkeypatch):
        import asyncio

        from api.ws import handshake

        monkeypatch.setattr(handshake, "UNAUTH_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(onboarding, "SESSION_SECONDS", 0.2)
        tv = _LiveWS(db)
        task = asyncio.create_task(ws.websocket_endpoint(tv))
        _, pub = _tv_key()
        await tv.queue.put(
            {"type": "onboard_start", "public_key": pub, "client_name": "TV", **TV_INFO}
        )
        await asyncio.wait_for(task, 3)
        assert tv.last("onboard_error")["reason"] == "session_expired"
        assert tv.closed[0] == 4002
        assert onboarding.sessions() == []


# ── Platforms and area choice ─────────────────────────────────────────────


IPAD_INFO = {**TV_INFO, "platform": "ipados", "device_model": "iPad16,3"}


async def _areas(db, rows):
    """Insert ``(id, name, tag|None, active)`` areas in display order."""
    now = Database.now()
    async with db.transaction():
        for order, (aid, name, tag, active) in enumerate(rows):
            await db.execute(
                "INSERT INTO areas (id, name, sort_order, is_active, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (aid, name, order, 1 if active else 0, now, now),
            )
            if tag:
                await db.execute(
                    "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (aid, tag)
                )


HOUSE = [
    ("a1", "Wohnzimmer", "eg", True),
    ("a2", "Küche", "eg", True),
    ("a3", "Bad", "og", True),
    ("a4", "Keller", None, False),
]


class TestPlatforms:
    async def test_ipad_may_wait(self, db):
        tv, _, _ = await _start(db, name="iPad Flur", info=IPAD_INFO)
        assert tv.last()["type"] == "onboard_session"
        assert onboarding.sessions()[0].info.platform == "ipados"

    @pytest.mark.parametrize("platform", ["ios", "android"])
    async def test_phones_may_wait(self, db, platform):
        """A phone waits like every other device (child's iPhone)."""
        tv, _, _ = await _start(db, info={**TV_INFO, "platform": platform})
        assert tv.last()["type"] == "onboard_session"
        assert len(tv.last()["code"]) == 6 and tv.last()["code"].isdigit()

    async def test_web_pairs_with_the_pin(self, db):
        tv, _, _ = await _start(db, info={**TV_INFO, "platform": "web"})
        assert tv.last()["reason"] == "platform_not_supported"
        assert onboarding.sessions() == []

    @pytest.mark.parametrize("platform", ["android_tv", "android_tablet", "wearos"])
    async def test_android_counterparts_may_wait(self, db, platform):
        """Android TV, Android tablet and Wear OS wait like their Apple
        counterparts."""
        tv, _, _ = await _start(db, info={**TV_INFO, "platform": platform})
        assert tv.last()["type"] == "onboard_session"
        assert onboarding.sessions()[0].info.platform == platform

    async def test_apple_watch_may_wait(self, db):
        watch_info = {**TV_INFO, "platform": "watchos", "device_model": "Watch7,9"}
        tv, _, _ = await _start(db, name="Apple Watch", info=watch_info)
        assert tv.last()["type"] == "onboard_session"
        assert onboarding.sessions()[0].info.platform == "watchos"


class TestAreaChoice:
    async def _confirm(self, db, phone_tags="[]", **choice):
        await _areas(db, HOUSE)
        tv, _, pub = await _start(db, name="iPad Flur", info=IPAD_INFO)
        sid = tv.last()["session_id"]
        phone, cid = await _phone(db, tags=phone_tags)
        await _send(phone, db, {"type": "onboard_confirm", **_ask(sid, pub, **choice)})
        return tv, phone, sid, pub

    async def _tags(self, db, phone):
        """The new device's areas in the client-tag form (rule areas)."""
        from api import authz

        new_id = phone.last()["client_id"]
        row = await db.fetchone(
            "SELECT owner_label, platform FROM paired_clients WHERE id = ?",
            (new_id,),
        )
        scope = await authz.client_area_scope(db, new_id)
        return new_id, scope.as_tags(), row

    async def test_lookup_offers_the_own_active_areas_in_order(self, db):
        await _areas(db, HOUSE)
        tv, _, pub = await _start(db, info=IPAD_INFO)
        phone, _ = await _phone(db, tags='["eg"]')
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        grant = phone.last()["grant"]
        assert [a["id"] for a in grant["areas"]] == ["a1", "a2"]
        assert grant["all_areas"] is False

    async def test_without_choice_the_new_device_gets_the_confirmer_scope(self, db):
        _, phone, _, _ = await self._confirm(db, phone_tags='["eg"]')
        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok"
        assert ok["area_names"] == ["Wohnzimmer", "Küche"] and not ok["all_areas"]
        _, tags, row = await self._tags(db, phone)
        assert tags == ["eg"]
        assert row["owner_label"] is None and row["platform"] == "ipados"

    async def test_subset_becomes_area_pins(self, db):
        _, phone, _, _ = await self._confirm(db, area_ids=["a3", "a1"])
        ok = phone.last()
        assert ok["area_names"] == ["Wohnzimmer", "Bad"] and ok["all_areas"] is False
        new_id, tags, _ = await self._tags(db, phone)
        assert tags == ["area:a1", "area:a3"]
        from api import authz

        assert await authz.listed_area_ids(db, new_id) == {"a1", "a3"}
        areas = await ws._get_areas(db, await authz.client_area_scope(db, new_id))
        assert [a["id"] for a in areas] == ["a1", "a3"]

    async def test_whole_own_list_keeps_the_tags(self, db):
        _, phone, _, _ = await self._confirm(
            db, phone_tags='["eg"]', area_ids=["a2", "a1"]
        )
        _, tags, _ = await self._tags(db, phone)
        assert tags == ["eg"]

    async def test_all_areas_only_when_the_confirmer_sees_all(self, db):
        _, phone, _, _ = await self._confirm(db, all_areas=True)
        assert phone.last()["all_areas"] is True
        _, tags, _ = await self._tags(db, phone)
        assert tags == ["*"]

    async def test_all_areas_refused_for_a_restricted_confirmer(self, db, _clean_state):
        tv, phone, sid, pub = await self._confirm(
            db, phone_tags='["eg"]', all_areas=True
        )
        err = phone.last()
        assert err["reason"] == "areas_not_allowed" and err["session_id"] == sid
        # The session stays open: choose again.
        assert onboarding.session_of(tv) is not None
        await _send(
            phone, db, {"type": "onboard_confirm", **_ask(sid, pub, area_ids=["a2"])}
        )
        assert phone.last()["type"] == "onboard_confirm_ok"
        _, tags, _ = await self._tags(db, phone)
        assert tags == ["area:a2"]

    @pytest.mark.parametrize(
        "choice",
        [
            {"area_ids": ["a3"]},  # not visible for the confirmer (og)
            {"area_ids": ["a4"]},  # inactive
            {"area_ids": ["nope"]},  # unknown
            {"area_ids": []},  # empty would mean "all" for a member
            {"area_ids": ["a1", "a1"]},
            {"area_ids": "a1"},
            {"area_ids": [1]},
            {"all_areas": "yes"},
        ],
    )
    async def test_never_more_than_the_confirmer_sees(self, db, choice):
        tv, phone, _, _ = await self._confirm(db, phone_tags='["eg"]', **choice)
        assert phone.last()["reason"] == "areas_not_allowed"
        assert onboarding.session_of(tv) is not None
        rows = await db.fetchall("SELECT id FROM paired_clients")
        assert len(rows) == 1  # only the phone

    async def test_refusals_count_against_the_failure_budget(self, db):
        tv, phone, sid, pub = await self._confirm(
            db, phone_tags='["eg"]', area_ids=["a3"]
        )
        for _ in range(onboarding.MAX_FAILURES_PER_DEVICE - 1):
            await _send(
                phone,
                db,
                {"type": "onboard_confirm", **_ask(sid, pub, area_ids=["a3"])},
            )
        await _send(
            phone, db, {"type": "onboard_confirm", **_ask(sid, pub, area_ids=["a1"])}
        )
        assert phone.last()["reason"] == "rate_limited"

    async def test_pins_of_a_pinned_confirmer_and_deleted_areas(self, db):
        _, phone, _, _ = await self._confirm(db, area_ids=["a1"])
        new_id, tags, _ = await self._tags(db, phone)
        from api import authz

        async with db.transaction():
            await db.execute("DELETE FROM areas WHERE id = 'a1'")
        # A pin of a deleted area matches nothing: never "all areas".
        assert await authz.listed_area_ids(db, new_id) == set()
        assert (await authz.client_area_scope(db, new_id)).all is False

    async def test_audit_names_the_choice(self, db, caplog):
        caplog.set_level("INFO", logger="hub.security")
        await self._confirm(db, area_ids=["a2"])
        assert any(
            "onboarding_confirmed" in r.getMessage()
            and "areas=subset:1" in r.getMessage()
            for r in caplog.records
        )


# ── kinds, profiles, code, guests, children, panels, “Set up by me” ──


PHONE_INFO = {**TV_INFO, "platform": "ios", "device_model": "iPhone17,3"}


async def _profile_of(db, suggestion):
    return await starter_profile(db, suggestion)


async def _gallery(db, suggestion):
    """Profile copied from a non-starter suggestion (e.g. child_6_9, parent)."""
    from api import profiles as profiles_api

    req = types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", user=None),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
        headers={},
    )
    out = await profiles_api.add_profile_suggestion(req, suggestion)
    return out["id"]


def _kinds(grant):
    return {k["kind"]: [p["name"] for p in k["profiles"]] for k in grant["kinds"]}


class TestSetUp:
    async def _lookup(self, db, phone, tv, pub):
        await _send(
            phone, db, {"type": "onboard_lookup", **_ask(tv.last()["session_id"], pub)}
        )
        return phone.last()

    async def test_phone_waits_and_is_set_up_by_code(self, db, _clean_state):
        tv, priv, pub = await _start(db, name="iPhone von Child One", info=PHONE_INFO)
        session = tv.last()
        code = session["code"]
        phone, cid = await _phone(db)
        await _send(phone, db, {"type": "onboard_lookup", "code": code})
        info = phone.last()
        assert info["type"] == "onboard_info"
        assert info["session_id"] == session["session_id"]
        assert info["public_key"] == pub and info["hub_id"] == HUB_ID
        # A phone can be a guest, child, house sitter or person, not a TV/panel.
        assert set(_kinds(info["grant"])) <= {
            "guest",
            "child",
            "house_sitter",
            "person",
        }
        assert "person" in _kinds(info["grant"])
        # The waiting device learns who sets it up; admins too.
        claimed = tv.last()
        assert claimed == {
            "type": "onboard_claimed",
            "session_id": session["session_id"],
            "by_name": "iPhone",
        }
        listed = await onboarding_api.list_onboarding_sessions(_request(db))
        assert listed.model_dump()["sessions"][0]["claimed_by"] == {
            "client_id": cid,
            "name": "iPhone",
        }
        assert code not in json.dumps(listed.model_dump())
        # The code is used up; the session goes on with its id.
        await _send(phone, db, {"type": "onboard_lookup", "code": code})
        assert phone.last()["reason"] == "session_not_found"
        await _send(
            phone,
            db,
            {
                "type": "onboard_confirm",
                **_ask(
                    info["session_id"],
                    pub,
                    kind="person",
                    profile_id=await _profile_of(db, "adult"),
                    client_name="Child iPhone",
                    owner_label="Child One",
                ),
            },
        )
        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok" and ok["kind"] == "person"
        assert ok["owner_label"] == "Child One" and ok["expires_at"] is None
        assert tv.last()["type"] == "onboard_ok"

    async def test_wrong_codes_are_limited_per_device_and_in_total(
        self, db, monkeypatch
    ):
        await _start(db, info=PHONE_INFO)
        phone, _ = await _phone(db)
        for _ in range(onboarding.MAX_CODE_FAILURES_PER_DEVICE):
            await _send(phone, db, {"type": "onboard_lookup", "code": "000000"})
            assert phone.last()["reason"] in ("session_not_found",)
        await _send(phone, db, {"type": "onboard_lookup", "code": "000001"})
        assert phone.last()["reason"] == "too_many_attempts"
        # The budget of the hub stops other devices too.
        monkeypatch.setattr(onboarding, "MAX_CODE_FAILURES_TOTAL", 6)
        other, _ = await _phone(db)
        await _send(other, db, {"type": "onboard_lookup", "code": "000002"})
        assert other.last()["reason"] == "session_not_found"
        await _send(other, db, {"type": "onboard_lookup", "code": "000003"})
        assert other.last()["reason"] == "too_many_attempts"

    async def test_code_needs_the_right_and_a_proper_form(self, db):
        tv, _, _ = await _start(db, info=PHONE_INFO)
        code = tv.last()["code"]
        member = _WS(db, authenticated=True, client_id=await _client(db))
        await _send(member, db, {"type": "onboard_lookup", "code": code})
        assert member.last()["reason"] == "not_permitted"
        phone, _ = await _phone(db)
        for bad in ("12345", "abcdef", 123456):
            await _send(phone, db, {"type": "onboard_lookup", "code": bad})
            assert phone.last()["reason"] in ("invalid_request", "session_not_found")
        # Mixing the code with QR values is refused.
        await _send(
            phone,
            db,
            {"type": "onboard_lookup", "code": code, "session_id": "A" * 22},
        )
        assert phone.last()["reason"] == "invalid_request"
        # The code was not used by any of these.
        await _send(phone, db, {"type": "onboard_lookup", "code": code})
        assert phone.last()["type"] == "onboard_info"

    async def test_kind_must_be_allowed_and_fit_the_platform(self, db):
        tv, _, pub = await _start(db)
        phone, _ = await _phone(
            db, overrides={"setup_devices": {"enabled": True, "kinds": ["guest"]}}
        )
        assert (await self._lookup(db, phone, tv, pub))["reason"] == "kind_not_allowed"
        tv2, _, pub2 = await _start(db, info=PHONE_INFO, host="192.0.2.11")
        info = await self._lookup(db, phone, tv2, pub2)
        assert list(_kinds(info["grant"])) == ["guest"]
        await _send(
            phone,
            db,
            {
                "type": "onboard_confirm",
                **_ask(
                    info["session_id"],
                    pub2,
                    kind="person",
                    profile_id=await _profile_of(db, "adult"),
                ),
            },
        )
        assert phone.last()["reason"] == "kind_not_allowed"
        # A profile of another kind under an allowed kind is refused.
        await _send(
            phone,
            db,
            {
                "type": "onboard_confirm",
                **_ask(
                    info["session_id"],
                    pub2,
                    kind="guest",
                    profile_id=await _profile_of(db, "adult"),
                    expires_at="2099-01-01T00:00:00Z",
                ),
            },
        )
        assert phone.last()["reason"] == "profile_not_allowed"
        assert onboarding.session_of(tv2) is not None
        assert len(await db.fetchall("SELECT id FROM paired_clients")) == 1

    async def test_no_profile_stronger_than_the_confirming_device(self, db):
        """Rights escalation: a checked device with fewer kinds and no family
        manager never gives Parents (direct, family manager, more kinds) or
        remote access; Adult (checked) it may give."""
        parent = await _gallery(db, "parent")
        await _gallery(db, "easy_mode")
        tv, _, pub = await _start(db, info=PHONE_INFO)
        phone, _ = await _phone(
            db,
            overrides={
                "control_path": "checked",
                "setup_devices": {"enabled": True, "kinds": ["person", "guest"]},
            },
        )
        info = await self._lookup(db, phone, tv, pub)
        names = _kinds(info["grant"])
        assert names["person"] == ["Einfach bedienen", "Erwachsene Person"]
        assert info["grant"]["managers_choice"] is False
        await _send(
            phone,
            db,
            {
                "type": "onboard_confirm",
                **_ask(info["session_id"], pub, kind="person", profile_id=parent),
            },
        )
        assert phone.last()["reason"] == "profile_not_allowed"
        # The full adult may give Adult, but not Parents (family manager).
        full, _ = await _phone(db)
        tv2, _, pub2 = await _start(db, info=PHONE_INFO, host="192.0.2.12")
        names = _kinds((await self._lookup(db, full, tv2, pub2))["grant"])
        assert (
            "Erwachsene Person" in names["person"] and "Eltern" not in names["person"]
        )
        # A parent gives Parents (same rights, same kinds).
        dad = _WS(
            db, authenticated=True, client_id=await _client(db, profile_id=parent)
        )
        tv3, _, pub3 = await _start(db, info=PHONE_INFO, host="192.0.2.13")
        grant = (await self._lookup(db, dad, tv3, pub3))["grant"]
        assert "person" not in _kinds(grant)  # Parents may not set up people
        assert "Kind" in _kinds(grant)["child"]

    async def test_guest_needs_an_expiry_of_at_most_90_days(self, db, _clean_state):
        tv, _, pub = await _start(db, info=PHONE_INFO)
        sid = tv.last()["session_id"]
        phone, cid = await _phone(db, overrides={"remote_access": False})
        guest = await _profile_of(db, "guest")
        base = {
            "type": "onboard_confirm",
            **_ask(sid, pub, kind="guest", profile_id=guest),
        }
        await _send(phone, db, base)
        assert phone.last()["reason"] == "expiry_not_allowed"
        await _send(phone, db, {**base, "expires_at": "2099-01-01T00:00:00Z"})
        assert phone.last()["reason"] == "expiry_not_allowed"
        await _send(phone, db, {**base, "expires_at": "2020-01-01T00:00:00Z"})
        assert phone.last()["reason"] == "expiry_not_allowed"
        from datetime import datetime, timedelta, timezone

        end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        # Remote access only when the confirming device has it.
        await _send(phone, db, {**base, "expires_at": end, "remote": True})
        assert phone.last()["reason"] == "profile_not_allowed"
        await _send(
            phone,
            db,
            {
                **base,
                "expires_at": end,
                "owner_label": "Guest One",
                "client_name": "Gast Guest One",
            },
        )
        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok" and ok["expires_at"]
        row = await db.fetchone(
            "SELECT access, expires_at, owner_label, set_up_by_client_id "
            "FROM paired_clients WHERE id = ?",
            (ok["client_id"],),
        )
        assert row["access"] == "guest" and row["owner_label"] == "Guest One"
        assert row["set_up_by_client_id"] == cid
        entries = await db.fetchall(
            "SELECT kind, actor, detail_json FROM event_log WHERE kind = 'device_set_up'"
        )
        assert entries and entries[-1]["actor"] == "iPhone"
        assert json.loads(entries[-1]["detail_json"])["kind"] == "guest"

    async def test_guest_remote_within_the_frame(self, db):
        tv, _, pub = await _start(db, info=PHONE_INFO)
        phone, _ = await _phone(db)
        info = await self._lookup(db, phone, tv, pub)
        (g,) = [p for p in info["grant"]["kinds"][0]["profiles"] if p["name"] == "Gast"]
        assert g["remote_choice"] is True and g["remote_access"] is False
        from datetime import datetime, timedelta, timezone

        end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        await _send(
            phone,
            db,
            {
                "type": "onboard_confirm",
                **_ask(
                    info["session_id"],
                    pub,
                    kind="guest",
                    profile_id=g["id"],
                    expires_at=end,
                    remote=True,
                ),
            },
        )
        ok = phone.last()
        assert ok["remote_allowed"] is True
        eff = (await store.resolve_client(db, ok["client_id"]))[2]
        assert eff["remote_access"]["source"] == "device"

    async def test_babysitter_with_access_windows(self, db, _clean_state):
        """Access hours chosen when setting up, stored as an adjustment;
        refused when the profile fixes them."""
        from datetime import datetime, timedelta, timezone

        sitter = await _gallery(db, "babysitter")
        tv, _, pub = await _start(db, info=PHONE_INFO)
        phone, _ = await _phone(db)
        info = await self._lookup(db, phone, tv, pub)
        (bs,) = [k for k in info["grant"]["kinds"] if k["kind"] == "babysitter"]
        entry = next(p for p in bs["profiles"] if p["id"] == sitter)
        assert entry["access_hours_choice"] is True
        assert entry["access_hours"]["enabled"] is True
        end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        windows = [{"days": ["fri", "sat"], "start": "18:00", "end": "01:00"}]
        base = {
            "type": "onboard_confirm",
            **_ask(info["session_id"], pub, kind="babysitter", profile_id=sitter),
            "expires_at": end,
        }
        await _send(
            phone,
            db,
            {**base, "access_windows": [{"days": [], "start": "1", "end": "2"}]},
        )
        assert phone.last()["reason"] == "invalid_request"
        await _send(phone, db, {**base, "access_windows": windows})
        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok", ok
        assert ok["access_hours"] == {"enabled": True, "windows": windows}
        eff = (await store.resolve_client(db, ok["client_id"]))[2]["access_hours"]
        assert eff["source"] == "device" and eff["value"]["windows"] == windows
        row = await db.fetchone(
            "SELECT access FROM paired_clients WHERE id = ?", (ok["client_id"],)
        )
        assert row["access"] == "guest"

        # Fixed in the profile: no choice, and the field is refused.
        from api import profiles as profiles_api

        await profiles_api.update_profile(
            _request(db),
            sitter,
            profiles_api.ProfileWrite(
                fixed=["access_hours", "remote_access", "control_path"]
            ),
            dry_run=False,
        )
        tv, _, pub = await _start(db, info=PHONE_INFO)
        info = await self._lookup(db, phone, tv, pub)
        (bs,) = [k for k in info["grant"]["kinds"] if k["kind"] == "babysitter"]
        entry = next(p for p in bs["profiles"] if p["id"] == sitter)
        assert entry["access_hours_choice"] is False
        base = {
            "type": "onboard_confirm",
            **_ask(info["session_id"], pub, kind="babysitter", profile_id=sitter),
            "expires_at": end,
        }
        await _send(phone, db, {**base, "access_windows": windows})
        assert phone.last()["reason"] == "access_hours_not_allowed"
        await _send(phone, db, base)
        assert phone.last()["type"] == "onboard_confirm_ok"

    async def test_own_access_hours_bound_the_new_device(self, db, _clean_state):
        """A device with access hours sets up nothing that has
        access outside them (no profile without hours, no wider windows)."""
        from datetime import datetime, timedelta, timezone

        from services import kids_mode

        kids_mode.reset()
        kids_mode.remember_time_zone("UTC")  # the windows below are in UTC
        sitter = await _gallery(db, "babysitter")
        now = datetime.now(timezone.utc)
        all_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        # Every day but one hour that is not now (and not in the babysitter
        # suggestion's 17:00-01:00).
        gap = 10 if now.hour == 3 else 3
        mine = [
            {"days": all_days, "start": f"{gap + 1:02d}:00", "end": f"{gap:02d}:00"}
        ]
        tv, _, pub = await _start(db, info=PHONE_INFO)
        phone, _ = await _phone(
            db, overrides={"access_hours": {"enabled": True, "windows": mine}}
        )
        info = await self._lookup(db, phone, tv, pub)
        assert "grant" in info, info
        kinds = {k["kind"]: k for k in info["grant"]["kinds"]}
        # The default guest profile has no access hours: not offered.
        assert all(
            p["access_hours"]["enabled"]
            for p in kinds.get("guest", {}).get("profiles", [])
        )
        entry = next(p for p in kinds["babysitter"]["profiles"] if p["id"] == sitter)
        assert entry["access_hours_choice"] is True
        end = (now + timedelta(days=3)).isoformat()
        base = {
            "type": "onboard_confirm",
            **_ask(info["session_id"], pub, kind="babysitter", profile_id=sitter),
            "expires_at": end,
        }
        wider = [{"days": all_days, "start": "00:00", "end": "00:00"}]
        await _send(phone, db, {**base, "access_windows": wider})
        assert phone.last()["reason"] == "access_hours_not_allowed"
        inside = [{"days": ["fri"], "start": "18:00", "end": "20:00"}]
        await _send(phone, db, {**base, "access_windows": inside})
        assert phone.last()["type"] == "onboard_confirm_ok", phone.last()

    async def test_child_with_managers(self, db):
        kid = await _gallery(db, "child_6_9")
        tv, _, pub = await _start(db, info=PHONE_INFO)
        sid = tv.last()["session_id"]
        mum, _ = await _phone(db)  # no family manager
        mama = await _client(
            db, overrides={"family_manager": True, "setup_devices": SETUP_ALL}
        )
        from services.family import store as fstore

        await fstore.put_scope(
            db, mama, scope={"all": False, "client_ids": []}, actor="admin"
        )
        # Without the right family_manager: no choice.
        await _send(
            mum,
            db,
            {
                "type": "onboard_confirm",
                **_ask(sid, pub, kind="child", profile_id=kid, managers=[mama]),
            },
        )
        assert mum.last()["reason"] == "managers_not_allowed"
        dad, dad_id = await _phone(
            db, overrides={"family_manager": True, "setup_devices": SETUP_ALL}
        )
        info = await self._lookup(db, dad, tv, pub)
        grant = info["grant"]
        assert grant["managers_choice"] is True
        by_id = {m["client_id"]: m for m in grant["managers"]}
        assert by_id[dad_id]["is_self"] is True and by_id[dad_id]["all_children"]
        assert by_id[mama]["all_children"] is False
        await _send(
            dad,
            db,
            {
                "type": "onboard_confirm",
                **_ask(
                    sid,
                    pub,
                    kind="child",
                    profile_id=kid,
                    managers=[dad_id, mama],
                    owner_label="Child One",
                    client_name="Child iPhone",
                ),
            },
        )
        ok = dad.last()
        assert ok["type"] == "onboard_confirm_ok" and ok["kind"] == "child"
        scope = (await fstore.scope_row(db, mama))["scope"]
        assert scope == {"all": False, "client_ids": [ok["client_id"]]}
        # Parental controls: the device never gets the key.
        cfg = await ws._get_client_config(db, ok["client_id"])
        assert cfg["ha_token"] is None and cfg["profile"]["kind"] == "child"

    async def test_wall_panel_start_area(self, db):
        await _areas(db, HOUSE)
        tv, _, pub = await _start(db, name="iPad", info=IPAD_INFO)
        sid = tv.last()["session_id"]
        phone, _ = await _phone(db, tags='["eg"]')
        panel = await _profile_of(db, "wall_panel")
        base = {
            "type": "onboard_confirm",
            **_ask(sid, pub, kind="panel", profile_id=panel),
        }
        await _send(phone, db, {**base, "start_area": "a3"})  # not visible
        assert phone.last()["reason"] == "areas_not_allowed"
        await _send(phone, db, {**base, "area_ids": ["a1"], "start_area": "a2"})
        assert phone.last()["reason"] == "areas_not_allowed"  # outside the choice
        await _send(
            phone,
            db,
            {
                **base,
                "area_ids": ["a2", "a1"],
                "start_area": "a2",
                "own_area_id": "a2",
                "client_name": "Wandpanel Küche",
            },
        )
        ok = phone.last()
        assert ok["type"] == "onboard_confirm_ok" and ok["kind"] == "panel"
        eff = (await store.resolve_client(db, ok["client_id"]))[2]
        assert eff["panel_start"]["value"] == {"mode": "area", "area_id": "a2"}
        row = await db.fetchone(
            "SELECT own_area_id FROM paired_clients WHERE id = ?", (ok["client_id"],)
        )
        assert row["own_area_id"] == "a2"

    async def test_teen_profile_cannot_set_up(self, db):
        """Kind child (also without parental controls) never sets up devices."""
        tv, _, pub = await _start(db, info=PHONE_INFO)
        teen = await _gallery(db, "teen")
        async with db.transaction():
            await db.execute(
                "UPDATE profiles SET child_protection = 0 WHERE id = ?", (teen,)
            )
        phone, _ = await _phone(db, profile_id=teen)
        assert (await self._lookup(db, phone, tv, pub))["reason"] == "not_permitted"

    async def test_set_up_by_me_list_and_remove(self, db):
        tv, _, pub = await _start(db, info=PHONE_INFO)
        phone, cid = await _phone(db)
        await _send(
            phone, db, {"type": "onboard_confirm", **_ask(tv.last()["session_id"], pub)}
        )
        new_id = phone.last()["client_id"]
        await _send(phone, db, {"type": "get_set_up_devices"})
        listed = phone.last()
        assert listed["type"] == "set_up_devices"
        assert [d["client_id"] for d in listed["devices"]] == [new_id]
        assert listed["devices"][0]["method"] == "onboard"
        assert listed["devices"][0]["profile"]["kind"] == "person"
        other, _ = await _phone(db)
        await _send(other, db, {"type": "set_up_device_remove", "client_id": new_id})
        assert other.last()["reason"] == "device_not_found"
        await _send(other, db, {"type": "get_set_up_devices"})
        assert other.last()["devices"] == []
        await _send(phone, db, {"type": "set_up_device_remove", "client_id": new_id})
        assert phone.last() == {"type": "set_up_device_removed", "client_id": new_id}
        assert not await db.fetchone(
            "SELECT id FROM paired_clients WHERE id = ?", (new_id,)
        )

    async def test_grant_before_scanning(self, db):
        phone, _ = await _phone(db)
        await _send(phone, db, {"type": "get_setup_grant", "platform": "tvos"})
        msg = phone.last()
        assert msg["type"] == "setup_grant" and list(_kinds(msg["grant"])) == ["tv"]
        await _send(phone, db, {"type": "get_setup_grant", "purpose": "invite"})
        assert list(_kinds(phone.last()["grant"])) == ["guest"]
        await _send(phone, db, {"type": "get_setup_grant", "platform": "web"})
        assert phone.last()["reason"] == "invalid_request"
        member = _WS(db, authenticated=True, client_id=await _client(db))
        await _send(member, db, {"type": "get_setup_grant"})
        assert member.last()["reason"] == "not_permitted"

    async def test_own_watch_gets_its_default_profile(self, db):
        tv, _, pub = await _start(
            db, info={**TV_INFO, "platform": "watchos", "device_model": "Watch7,1"}
        )
        phone, _ = await _phone(
            db, overrides={"setup_devices": {"enabled": True, "kinds": []}}
        )
        info = await self._lookup(db, phone, tv, pub)
        assert info["grant"]["own_watch"] is True
        assert _kinds(info["grant"]) == {"person": ["Erwachsene Person"]}

    async def test_own_watch_with_a_stronger_default_profile_is_refused(self, db):
        """The watch's default profile is Direct, the phone is Checked: the
        watch never gets more than its phone, so the setup is refused with
        profile_not_allowed (not the generic not_permitted)."""
        from services.profiles import catalog as cat
        from services.profiles import rows as profile_rows

        body = cat.materialize("adult")
        body["rules"].update(control_path="direct")
        async with db.transaction():
            direct = await profile_rows.insert_profile(
                db.connection, name="Direkt", body=body, now=Database.now(), actor=None
            )
        await db.set_setting(profile_rows.default_setting_key("watchos"), direct)
        tv, _, pub = await _start(
            db, info={**TV_INFO, "platform": "watchos", "device_model": "Watch7,1"}
        )
        phone, _ = await _phone(
            db,
            overrides={
                "setup_devices": {"enabled": True, "kinds": []},
                "control_path": "checked",
            },
        )
        answer = await self._lookup(db, phone, tv, pub)
        assert answer["reason"] == "profile_not_allowed"
        # Without the rule watch it stays not_permitted.
        tv2, _, pub2 = await _start(
            db,
            info={**TV_INFO, "platform": "watchos", "device_model": "Watch7,1"},
            host="192.0.2.14",
        )
        phone2, _ = await _phone(
            db,
            overrides={
                "setup_devices": {"enabled": True, "kinds": []},
                "watch": False,
            },
        )
        answer = await self._lookup(db, phone2, tv2, pub2)
        assert answer["reason"] == "not_permitted"


def test_hours_wider():
    every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    evening = {
        "enabled": True,
        "windows": [{"days": every_day, "start": "17:00", "end": "01:00"}],
    }
    off = {"enabled": False, "windows": []}
    assert not onboarding.hours_wider(off, off)
    assert not onboarding.hours_wider(evening, off)
    assert onboarding.hours_wider(off, evening)
    assert not onboarding.hours_wider(evening, evening)
    sat_night = {
        "enabled": True,
        "windows": [{"days": ["sat"], "start": "23:00", "end": "00:30"}],
    }
    assert not onboarding.hours_wider(sat_night, evening)
    longer = {
        "enabled": True,
        "windows": [{"days": ["sun"], "start": "23:00", "end": "02:00"}],
    }
    assert onboarding.hours_wider(longer, evening)
    whole_day = {
        "enabled": True,
        "windows": [{"days": ["sun"], "start": "08:00", "end": "08:00"}],
    }
    assert onboarding.hours_wider(whole_day, evening)
    # Enabled without any window = no access at all: nothing is wider than it.
    nothing = {"enabled": True, "windows": []}
    assert not onboarding.hours_wider(nothing, evening)
    assert onboarding.hours_wider(evening, nothing)
