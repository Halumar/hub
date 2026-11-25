"""Compliance channel: crypto contract, derivation, toggle.

Under test:
(a) KNOWN-ANSWER crypto vectors (the v1 contract MUST be byte-identical on
    iOS): given digest32, k_sign and the signatures for "deleted" and
    "need_token_update" must match the pinned hex values exactly.
(b) Status derivation from existing row state, every branch: pending-deletion
    -> deleted; deactivated OR ha_token_status in {revoked, revoke_pending,
    mint_failed} OR guest past expires_at -> need_token_update; else
    compliant. sig present ONLY for the signed (restricting) statuses.
(c) GET /api/integration/status carries a per-client compliance blob plus the
    signed tombstones list (hidden pending rows included) and the
    compliance_endpoint_enabled flag; disabling the toggle omits ALL of it.
    secret_hash never leaks into the payload.
(d) GET/PUT /api/settings/compliance-endpoint: default true, PUT persists and
    broadcasts client_config_updated.
"""

import hashlib
import hmac
import types
from datetime import datetime, timedelta, timezone

import pytest

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
from models.database import Database
from services import compliance

# Client info every app sends with auth/pair.
CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}

# ── Known-answer vectors: contract fixtures/crypto/compliance-vectors ───────
#
# The same file the apps check; without a contract checkout these skip
# locally, CI fails instead (conftest.py).


def _contract_vectors() -> dict | None:
    import json
    import os
    from pathlib import Path

    root = os.environ.get("CONTRACTS_DIR")
    path = Path(root or "/nonexistent") / "fixtures/crypto/compliance-vectors/v1.json"
    return json.loads(path.read_text()) if path.is_file() else None


VECTORS = _contract_vectors()
needs_vectors = pytest.mark.skipif(
    VECTORS is None, reason="contract checkout not found (set CONTRACTS_DIR)"
)
_CASES = (VECTORS or {}).get("cases", [])


def _case(status: str) -> dict:
    return next(c for c in _CASES if c["status"] == status and "digest32" in c)


# The client the status tests sign for: the first case of the vectors.
VECTOR_DIGEST32 = bytes.fromhex(_CASES[0]["digest32"] if _CASES else "00" * 32)
VECTOR_CLIENT_ID = _CASES[0]["client_id"] if _CASES else "c" * 36
VECTOR_TS = _CASES[0]["ts"] if _CASES else 0


@pytest.mark.parametrize(
    "case",
    _CASES or [pytest.param(None, marks=needs_vectors, id="no-contract")],
    ids=lambda c: f"{c['status']}-{c['client_id'][:8]}" if c else "none",
)
def test_contract_compliance_vectors(case):
    # A case gives digest32 or the pairing secret it is the SHA-256 of
    # (the chain the hub stores as secret_hash).
    digest_hex = (
        case.get("digest32") or hashlib.sha256(case["secret"].encode()).hexdigest()
    )
    assert compliance.DERIVATION_LABEL == VECTORS["label"].encode()
    assert (
        compliance.derive_signing_key(bytes.fromhex(digest_hex)).hex()
        == (case["k_sign"])
    )
    assert (
        compliance.sign_status(
            digest_hex, case["client_id"], case["status"], case["ts"]
        )
        == case["sig"]
    )


class TestKnownAnswerVectors:
    def test_secret_to_sig_end_to_end(self):
        """The full chain from a pairing secret matches an independent HMAC."""
        secret = "ab" * 32  # a token_hex(32)-shaped secret string
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        digest32 = hashlib.sha256(secret.encode()).digest()
        assert bytes.fromhex(secret_hash) == digest32

        k_sign = hmac.new(digest32, b"hub/compliance/v1", hashlib.sha256).digest()
        expected = hmac.new(
            k_sign, b"v1|cid-1|deleted|1783130000", hashlib.sha256
        ).hexdigest()
        assert compliance.sign_status(secret_hash, "cid-1", "deleted", 1783130000) == (
            expected
        )


# ── Status derivation (every branch) ─────────────────────────────────────────


def _client_row(**overrides):
    row = {
        "id": VECTOR_CLIENT_ID,
        "secret_hash": VECTOR_DIGEST32.hex(),
        "is_active": 1,
        "is_guest": 0,
        "expires_at": None,
        "ha_token_status": None,
        "pending_deletion_at": None,
    }
    row.update(overrides)
    return row


class TestStatusDerivation:
    def test_active_client_is_compliant(self):
        assert compliance.derive_status(_client_row()) == "compliant"

    def test_pending_deletion_wins_over_everything(self):
        row = _client_row(
            pending_deletion_at=Database.now(),
            is_active=0,
            ha_token_status="revoked",
        )
        assert compliance.derive_status(row) == "deleted"

    def test_inactive_needs_token_update(self):
        assert compliance.derive_status(_client_row(is_active=0)) == "need_token_update"

    def test_device_waiting_for_its_token_is_compliant(self):
        """pending (integration not done yet) is no dead token."""
        row = _client_row(ha_token_status="pending")
        assert compliance.derive_status(row) == "compliant"

    def test_healthy_token_status_stays_compliant(self):
        assert compliance.derive_status(_client_row(ha_token_status="active")) == (
            "compliant"
        )

    def test_expired_guest_needs_token_update(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        row = _client_row(is_guest=1, expires_at=past)
        assert compliance.derive_status(row) == "need_token_update"

    def test_live_guest_is_compliant(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        row = _client_row(is_guest=1, expires_at=future)
        assert compliance.derive_status(row) == "compliant"


class TestBuildCompliance:
    def test_compliant_has_no_sig(self):
        blob = compliance.build_compliance(_client_row())
        assert blob["status"] == "compliant"
        assert "sig" not in blob
        assert isinstance(blob["ts"], int)

    @needs_vectors
    def test_deleted_is_signed_with_vector(self):
        row = _client_row(pending_deletion_at=Database.now())
        blob = compliance.build_compliance(row, now=VECTOR_TS)
        assert blob == {
            "status": "deleted",
            "ts": VECTOR_TS,
            "sig": _case("deleted")["sig"],
        }

    @needs_vectors
    def test_need_token_update_is_signed_with_vector(self):
        row = _client_row(is_active=0)
        blob = compliance.build_compliance(row, now=VECTOR_TS)
        assert blob == {
            "status": "need_token_update",
            "ts": VECTOR_TS,
            "sig": _case("need_token_update")["sig"],
        }

    def test_bad_secret_hash_fails_neutral_without_sig(self):
        row = _client_row(is_active=0, secret_hash="h" * 64)
        blob = compliance.build_compliance(row, now=VECTOR_TS)
        assert blob["status"] == "need_token_update"
        assert "sig" not in blob


# ── Integration status surface ───────────────────────────────────────────────


def _integration_request(db):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
    )


def _admin_request(db):
    request = _integration_request(db)
    request.state.auth_type = "admin"
    return request


@pytest.fixture
def clean_pairing():
    pairing.clear_active_pins()
    yield
    pairing.clear_active_pins()


class TestIntegrationStatusCompliance:
    async def test_compliant_client_has_unsigned_blob(
        self, db_with_client, clean_pairing
    ):
        db, client_id, _secret, _hash = db_with_client
        result = await apiintg.integration_status(_integration_request(db))

        assert result["compliance_endpoint_enabled"] is True
        me = next(c for c in result["clients"] if c["id"] == client_id)
        assert me["compliance"]["status"] == "compliant"
        assert "sig" not in me["compliance"]
        assert result["tombstones"] == []

    async def test_pending_deletion_client_signed_deleted(
        self, db_with_client, clean_pairing
    ):
        db, client_id, _secret, secret_hash = db_with_client
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )
        await db.commit()

        result = await apiintg.integration_status(_integration_request(db))
        me = next(c for c in result["clients"] if c["id"] == client_id)
        blob = me["compliance"]
        assert blob["status"] == "deleted"
        # The sig verifies against the app-side key material (the secret).
        assert blob["sig"] == compliance.sign_status(
            secret_hash, client_id, "deleted", blob["ts"]
        )

        # Also present as a signed tombstone entry.
        assert [t["client_id"] for t in result["tombstones"]] == [client_id]
        t_blob = result["tombstones"][0]["compliance"]
        assert t_blob["status"] == "deleted"
        assert t_blob["sig"] == compliance.sign_status(
            secret_hash, client_id, "deleted", t_blob["ts"]
        )

    async def test_hidden_tombstone_kept_in_tombstones_only(
        self, db_with_client, clean_pairing
    ):
        db, client_id, _secret, _hash = db_with_client
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )
        await db.commit()
        await db.set_pending_hidden(client_id)

        result = await apiintg.integration_status(_integration_request(db))
        assert all(c["id"] != client_id for c in result["clients"])
        assert [t["client_id"] for t in result["tombstones"]] == [client_id]
        assert result["tombstones"][0]["compliance"]["status"] == "deleted"
        assert "sig" in result["tombstones"][0]["compliance"]

    async def test_toggle_off_omits_all_compliance(self, db_with_client, clean_pairing):
        db, client_id, _secret, _hash = db_with_client
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )
        await db.commit()
        await db.set_setting("compliance_endpoint_enabled", "false")

        result = await apiintg.integration_status(_integration_request(db))
        assert result["compliance_endpoint_enabled"] is False
        assert result["tombstones"] == []
        assert all("compliance" not in c for c in result["clients"])

    async def test_secret_hash_never_leaks(self, db_with_client, clean_pairing):
        db, _client_id, _secret, secret_hash = db_with_client
        result = await apiintg.integration_status(_integration_request(db))
        assert secret_hash not in str(result)
        assert all("secret_hash" not in c for c in result["clients"])


# ── Toggle setting endpoints ─────────────────────────────────────────────────


class TestComplianceEndpointSetting:
    async def test_default_is_true(self, db):
        result = await pairing.get_compliance_endpoint_setting(_admin_request(db))
        assert result == {"compliance_endpoint_enabled": True}

    async def test_put_persists_and_broadcasts(self, db, monkeypatch):
        events = []

        async def _capture(message, *a, **k):
            events.append(message)

        monkeypatch.setattr(ws, "broadcast", _capture)

        result = await pairing.update_compliance_endpoint_setting(
            _admin_request(db),
            pairing.ComplianceEndpointUpdate(compliance_endpoint_enabled=False),
        )
        assert result == {"compliance_endpoint_enabled": False}
        assert await db.get_setting("compliance_endpoint_enabled") == "false"
        assert {"type": "client_config_updated", "data": {}} in events

        result = await pairing.get_compliance_endpoint_setting(_admin_request(db))
        assert result == {"compliance_endpoint_enabled": False}


# ── Tombstone lifecycle: retained until wipe-ack, then purged ────────────────


class _FakeWebSocket:
    def __init__(self, db):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None)
        )
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


async def _mark_pending(db, client_id):
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()


class TestTombstoneLifecycle:
    async def test_returning_device_gets_handshake_row_retained(self, db_with_client):
        """Auth with a pending-deletion secret replays client_removed, no purge."""
        db, client_id, secret, _hash = db_with_client
        await _mark_pending(db, client_id)

        fake_ws = _FakeWebSocket(db)
        try:
            authenticated, cid = await ws._handle_message(
                fake_ws, {"type": "auth", "secret": secret, **CLIENT_INFO}, False, None
            )

            assert authenticated is False
            assert cid is None
            # client_removed handshake replayed, socket NOT closed.
            assert {
                "type": "client_removed",
                "client_id": client_id,
                "reason": "deleted",
            } in fake_ws.sent
            assert fake_ws.closed is None
            # Tombstone (client_id + secret_hash) survives until the wipe-ack.
            row = await db.fetchone(
                "SELECT id, secret_hash FROM paired_clients WHERE id = ?",
                (client_id,),
            )
            assert row is not None
            # Connection marked as pending-removal for the unauthenticated ack.
            assert ws._clients[fake_ws]["pending_removal_client_id"] == client_id
        finally:
            ws._clients.pop(fake_ws, None)

    async def test_wipe_ack_purges_tombstone(self, db_with_client, monkeypatch):
        """The unauthenticated client_reset_ack from the handshake purges."""
        db, client_id, secret, _hash = db_with_client
        await _mark_pending(db, client_id)

        async def _noop_broadcast(*a, **k):
            return None

        monkeypatch.setattr(ws, "broadcast", _noop_broadcast)

        fake_ws = _FakeWebSocket(db)
        try:
            await ws._handle_message(
                fake_ws, {"type": "auth", "secret": secret, **CLIENT_INFO}, False, None
            )
            await ws._handle_message(
                fake_ws,
                {"type": "client_reset_ack", "client_id": client_id},
                False,
                None,
            )

            assert {"type": "client_reset_ack_ok"} in fake_ws.sent
            assert fake_ws.closed == (1000, "Client removed")
            assert (
                await db.fetchone(
                    "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
                )
            ) is None
        finally:
            ws._clients.pop(fake_ws, None)

    async def test_unauth_ack_without_handshake_is_rejected(self, db_with_client):
        """A cold, unauthenticated ack (no handshake) must never purge."""
        db, client_id, _secret, _hash = db_with_client
        await _mark_pending(db, client_id)

        fake_ws = _FakeWebSocket(db)
        try:
            await ws._handle_message(
                fake_ws,
                {"type": "client_reset_ack", "client_id": client_id},
                False,
                None,
            )
            assert any(m.get("type") == "error" for m in fake_ws.sent)
            assert (
                await db.fetchone(
                    "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
                )
            ) is not None
        finally:
            ws._clients.pop(fake_ws, None)

    async def test_admin_force_purge(self, db_with_client):
        db, client_id, _secret, _hash = db_with_client

        # Not pending yet -> 400.
        import fastapi

        with pytest.raises(fastapi.HTTPException) as exc:
            await pairing.purge_client_tombstone(_admin_request(db), client_id)
        assert exc.value.status_code == 400

        await _mark_pending(db, client_id)
        await pairing.purge_client_tombstone(_admin_request(db), client_id)
        assert (
            await db.fetchone(
                "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
            )
        ) is None

        # Unknown client -> 404.
        with pytest.raises(fastapi.HTTPException) as exc:
            await pairing.purge_client_tombstone(_admin_request(db), client_id)
        assert exc.value.status_code == 404
