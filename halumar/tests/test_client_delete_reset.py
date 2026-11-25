"""Tests for the client-delete / reset flow.

Deleting a paired client must:
  * return INSTANTLY — removing the device's HA user (deprovision_client to
    the integration) runs in the background and only after the app
    was told (client_removed) and reset or the grace period ran out,
  * NOT hard-delete the row — instead mark it pending-deletion
    (``pending_deletion_at`` set, ``is_active=0``) so even an offline app is
    forced to reset on its next reconnect,
  * SIGNAL any live connection to reset itself via ``client_removed`` (without
    closing the socket or deleting the row inline).

The explicit ``client_reset_ack`` ("wiped") is the ONLY purge
trigger: on receiving it from the owning client the hub hard-deletes the
row, broadcasts a removal to admin UIs, replies ``client_reset_ack_ok`` and
closes the socket. A signaled connection that disconnects WITHOUT acking keeps
its tombstone (the compliance endpoint must keep signing "deleted").

The OFFLINE path: a deleted (pending) client that was offline keeps its signed
tombstone row INDEFINITELY; when it later reconnects with its old secret the
``client_removed`` handshake is replayed on that socket (no close, no delete)
and only its unauthenticated wipe-ack purges the row (see test_compliance.py
for the handshake tests). Admins can force-purge via
DELETE /pairing/clients/{id}/tombstone.

The integration sender, ``disconnect_client`` and ``broadcast`` are MOCKED — no
live HA and no real WebSocket. Reuses the ``db`` / ``db_with_client`` fixtures.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

import api.pairing as pairing
import api.websocket as ws
from services import device_provisioning
from models.database import Database

# Client info every app sends with auth/pair.
CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}


def _admin_request(db):
    """Build a minimal Request that passes require_admin and carries the db."""
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="ingress"),
        app=SimpleNamespace(state=SimpleNamespace(db=db)),
    )


class _FakeWebSocket:
    """Records send_json / send_text / close calls; exposes .app.state.db."""

    def __init__(self, db):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.sent: list[dict] = []
        self.sent_text: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, data):
        self.sent_text.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class _DrivableWS:
    """A WebSocket fake that can drive ``websocket_endpoint`` end-to-end.

    Feeds a queue of inbound text messages; once exhausted it runs an optional
    ``before_disconnect`` async hook and raises ``WebSocketDisconnect`` so the
    endpoint's finally-block executes.
    """

    def __init__(self, db, messages, before_disconnect=None):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.headers: dict[str, str] = {}
        self.client = SimpleNamespace(host="198.51.100.9")  # not a trusted ingress IP
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self.accepted = False
        self._messages = list(messages)
        self._before_disconnect = before_disconnect

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive_text(self):
        if self._messages:
            return self._messages.pop(0)
        if self._before_disconnect is not None:
            await self._before_disconnect()
        raise WebSocketDisconnect()


async def _async_true(*_a, **_k):
    return True


async def _async_zero(*_a, **_k):
    return 0


async def _async_noop(*_a, **_k):
    return None


@pytest.fixture
def integration_messages(monkeypatch):
    """Messages sent to the integration push session."""
    sent: list[dict] = []

    async def fake_sender(message):
        sent.append(message)
        return True

    monkeypatch.setattr(device_provisioning, "_sender", fake_sender)
    return sent


# ── delete_client is INSTANT: deprovision scheduled in background ────────────


@pytest.mark.asyncio
async def test_offline_delete_is_instant_and_holds_the_ha_user(
    db_with_client, integration_messages
):
    """An offline device keeps its HA user until its app comes back
    and confirms the reset (or the hold runs out): no deprovision now."""
    db, client_id, _secret, _hash = db_with_client

    await pairing.delete_client(_admin_request(db), client_id)

    # Row is NOT hard-deleted — it is marked pending-deletion + deactivated.
    row = await db.fetchone(
        "SELECT is_active, pending_deletion_at, ha_user_released_at "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert row is not None, "row must survive (pending-deletion, not hard-deleted)"
    assert row["is_active"] == 0
    assert row["pending_deletion_at"] is not None
    assert row["ha_user_released_at"] is None

    await asyncio.sleep(0.05)
    assert integration_messages == []
    assert await device_provisioning.held_user_ids(db) == [client_id]


@pytest.mark.asyncio
async def test_delete_removes_the_ha_user_only_after_the_app_reset(
    db_with_client, integration_messages, monkeypatch
):
    """client_removed first, the HA user goes after the app is gone."""
    db, client_id, _secret, _hash = db_with_client
    monkeypatch.setattr(ws, "RESET_ACK_GRACE_SECONDS", 5)

    live = _FakeWebSocket(db)
    ws._clients[live] = {"authenticated": True, "client_id": client_id}
    try:
        await pairing.delete_client(_admin_request(db), client_id)
        assert [m["type"] for m in live.sent_text] == ["client_removed"]

        # The app is still connected (no ack yet): the HA user stays.
        await asyncio.sleep(0.3)
        assert integration_messages == []

        # The app acked and is gone -> now the HA user is removed.
        ws._clients.pop(live, None)
        await asyncio.sleep(0.5)
        assert [m["type"] for m in integration_messages] == ["deprovision_client"]
        assert integration_messages[0]["data"]["reason"] == "deleted"
    finally:
        ws._clients.pop(live, None)


@pytest.mark.asyncio
async def test_delete_signals_live_connection(db_with_client, monkeypatch):
    db, client_id, _secret, _hash = db_with_client

    # Fast, no-op fallback so the test doesn't linger / really disconnect.
    monkeypatch.setattr(ws, "RESET_ACK_GRACE_SECONDS", 0)
    monkeypatch.setattr(ws, "disconnect_client", _async_zero)

    live = _FakeWebSocket(db)
    ws._clients[live] = {"authenticated": True, "client_id": client_id}
    try:
        await pairing.delete_client(_admin_request(db), client_id)

        # client_removed was sent over the live socket with the wire contract shape.
        assert any(
            m.get("type") == "client_removed"
            and m.get("client_id") == client_id
            and m.get("reason") == "deleted"
            for m in live.sent_text
        )
        # The connection is flagged as signaled (so its eventual disconnect can
        # clean the row), but the socket is NOT closed inline.
        assert ws._clients[live].get("reset_signaled") is True
        assert live.closed is None

        # Drain the scheduled fallback task.
        await asyncio.sleep(0.05)
    finally:
        ws._clients.pop(live, None)


@pytest.mark.asyncio
async def test_delete_excluded_from_active_count(db_with_client, monkeypatch):
    db, client_id, _secret, _hash = db_with_client

    await pairing.delete_client(_admin_request(db), client_id)
    await asyncio.sleep(0.05)  # drain background deprovision task

    # pairing/status active_clients uses is_active=1 → excludes pending.
    active = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM paired_clients WHERE is_active = 1"
    )
    assert active["cnt"] == 0


@pytest.mark.asyncio
async def test_delete_unknown_client_404(db):
    import fastapi

    with pytest.raises(fastapi.HTTPException) as exc:
        await pairing.delete_client(_admin_request(db), "does-not-exist")
    assert exc.value.status_code == 404


# ── client_reset_ack: PRIMARY removal trigger from the owning client ─────────


@pytest.mark.asyncio
async def test_client_reset_ack_deletes_and_broadcasts(db_with_client, monkeypatch):
    db, client_id, _secret, _hash = db_with_client

    # The row is pending-deletion (as it would be right after a delete).
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    broadcasts = []

    async def fake_broadcast(message, audience=None):
        broadcasts.append((message, audience))

    monkeypatch.setattr(ws, "broadcast", fake_broadcast)

    fake_ws = _FakeWebSocket(db)
    authenticated, cid = await ws._handle_message(
        fake_ws,
        {"type": "client_reset_ack", "client_id": client_id},
        True,
        client_id,
    )

    # Row hard-deleted.
    assert (
        await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (client_id,))
    ) is None

    # ack_ok replied, then socket closed.
    assert {"type": "client_reset_ack_ok"} in fake_ws.sent
    assert fake_ws.closed is not None

    # Admin UIs told the device is gone (removal/disconnect event).
    assert any(
        m.get("type") == "client_disconnected"
        and m.get("data", {}).get("client_id") == client_id
        and aud == ws.EventAudience.ADMIN
        for m, aud in broadcasts
    )

    # Auth state unchanged in the return tuple.
    assert authenticated is True
    assert cid == client_id


@pytest.mark.asyncio
async def test_client_reset_ack_mismatch_does_not_delete(db_with_client, monkeypatch):
    db, client_id, _secret, _hash = db_with_client

    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    broadcasts = []

    async def fake_broadcast(message, audience=None):
        broadcasts.append((message, audience))

    monkeypatch.setattr(ws, "broadcast", fake_broadcast)

    fake_ws = _FakeWebSocket(db)
    await ws._handle_message(
        fake_ws,
        {"type": "client_reset_ack", "client_id": "some-other-client"},
        True,
        client_id,
    )

    # Mismatched id → row is NOT deleted and socket NOT closed.
    assert (
        await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (client_id,))
    ) is not None
    assert fake_ws.closed is None
    assert any(m.get("type") == "error" for m in fake_ws.sent)
    assert broadcasts == []


@pytest.mark.asyncio
async def test_client_reset_ack_rejected_for_ingress(db_with_client):
    db, _client_id, _secret, _hash = db_with_client

    fake_ws = _FakeWebSocket(db)
    await ws._handle_message(
        fake_ws,
        {"type": "client_reset_ack", "client_id": "ingress"},
        True,
        "ingress",
    )
    assert fake_ws.closed is None
    assert any(m.get("type") == "error" for m in fake_ws.sent)


# ── disconnect finally-block: NEVER purges (wipe-ack is the only trigger) ────


@pytest.mark.asyncio
async def test_finally_keeps_pending_even_when_signaled(db_with_client, monkeypatch):
    """A signaled connection that disconnects WITHOUT acking keeps the row.

    Disconnecting is NOT a wipe confirmation: the signed tombstone must
    persist so the compliance endpoint keeps returning signed "deleted".
    """
    db, client_id, secret, _hash = db_with_client

    monkeypatch.setattr(ws, "_is_ingress", lambda _w: False)
    monkeypatch.setattr(ws, "_auth_disabled", _async_zero)  # returns falsy → auth on
    monkeypatch.setattr(ws, "broadcast", _async_noop)

    async def before_disconnect():
        # Simulate: while connected, admin deleted us AND we were signaled.
        ws._clients[fake]["reset_signaled"] = True
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )
        await db.commit()

    fake = _DrivableWS(
        db,
        messages=[json.dumps({"type": "auth", "secret": secret, **CLIENT_INFO})],
        before_disconnect=before_disconnect,
    )
    await ws.websocket_endpoint(fake)

    # No ack → tombstone survives the signaled disconnect.
    row = await db.fetchone(
        "SELECT id, pending_deletion_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert row is not None, "signaled disconnect without ack must NOT purge"
    assert row["pending_deletion_at"] is not None


@pytest.mark.asyncio
async def test_finally_keeps_pending_when_not_signaled(db_with_client, monkeypatch):
    """A connection that disconnects WITHOUT being signaled never deletes.

    This is the guarantee that an offline-at-delete client (which never had a
    live signaled WS) keeps its tombstone row indefinitely.
    """
    db, client_id, secret, _hash = db_with_client

    monkeypatch.setattr(ws, "_is_ingress", lambda _w: False)
    monkeypatch.setattr(ws, "_auth_disabled", _async_zero)
    monkeypatch.setattr(ws, "broadcast", _async_noop)

    async def before_disconnect():
        # Row becomes pending, but this connection was NEVER signaled.
        await db.execute(
            "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 "
            "WHERE id = ?",
            (Database.now(), client_id),
        )
        await db.commit()

    fake = _DrivableWS(
        db,
        messages=[json.dumps({"type": "auth", "secret": secret, **CLIENT_INFO})],
        before_disconnect=before_disconnect,
    )
    await ws.websocket_endpoint(fake)

    # Not signaled → tombstone row MUST survive.
    row = await db.fetchone(
        "SELECT id, pending_deletion_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert row is not None, "un-signaled pending row must persist"
    assert row["pending_deletion_at"] is not None


# ── WS auth replays the reset handshake for a pending-deletion client ────────


@pytest.mark.asyncio
async def test_pending_deletion_ws_auth_replays_handshake(db_with_client):
    db, client_id, secret, _hash = db_with_client

    # Mark the client pending-deletion (simulating a prior delete).
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    fake_ws = _FakeWebSocket(db)
    try:
        authenticated, cid = await ws._handle_message(
            fake_ws, {"type": "auth", "secret": secret, **CLIENT_INFO}, False, None
        )

        # Auth did not succeed; client_removed replayed instead of closing.
        assert authenticated is False
        assert cid is None
        assert fake_ws.closed is None
        assert {
            "type": "client_removed",
            "client_id": client_id,
            "reason": "deleted",
        } in fake_ws.sent

        # The signed tombstone survives until the wipe-ack.
        row = await db.fetchone(
            "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
        )
        assert row is not None
    finally:
        ws._clients.pop(fake_ws, None)


@pytest.mark.asyncio
async def test_match_pending_deletion_helper(db_with_client):
    db, client_id, secret, _hash = db_with_client

    # Not pending yet → no match.
    assert await ws._match_pending_deletion(db, secret) is None

    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ? WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    # Matches WITHOUT deleting the row.
    assert await ws._match_pending_deletion(db, secret) == client_id
    assert (
        await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (client_id,))
    ) is not None


@pytest.mark.asyncio
async def test_verify_secret_rejects_inactive(db_with_client):
    """A deactivated (pending) client must not authenticate normally."""
    db, client_id, secret, _hash = db_with_client
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    assert await ws._verify_client_secret(db, secret) is None


# ── tombstone persists: no periodic sweep removes offline pending rows ───────


def test_no_periodic_pending_sweep_exists():
    """The time/offline-based sweep is gone — only return-based removal remains."""
    assert not hasattr(ws, "cleanup_pending_deletions")


@pytest.mark.asyncio
async def test_pending_tombstone_persists_indefinitely(db_with_client):
    db, client_id, _secret, _hash = db_with_client

    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    # No live WS connection and no periodic sweep → the tombstone row survives.
    row = await db.fetchone(
        "SELECT id, pending_deletion_at FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert row is not None, "offline pending tombstone must persist indefinitely"
    assert row["pending_deletion_at"] is not None


# ── dismiss-pending hides the tombstone but keeps the background row ──────────


@pytest.mark.asyncio
async def test_dismiss_pending_hides_but_keeps_row(db_with_client):
    db, client_id, _secret, _hash = db_with_client

    # dismiss-pending is only valid for a currently-pending client.
    import fastapi

    with pytest.raises(fastapi.HTTPException) as exc:
        await pairing.dismiss_pending_client(_admin_request(db), client_id)
    assert exc.value.status_code == 400

    # Mark pending-deletion, then dismiss.
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()

    result = await pairing.dismiss_pending_client(_admin_request(db), client_id)
    assert result == {"ok": True}

    # Row is kept; pending_hidden_at is set (background tombstone).
    row = await db.fetchone(
        "SELECT pending_deletion_at, pending_hidden_at "
        "FROM paired_clients WHERE id = ?",
        (client_id,),
    )
    assert row is not None, "dismiss must NOT delete the row"
    assert row["pending_deletion_at"] is not None
    assert row["pending_hidden_at"] is not None


@pytest.mark.asyncio
async def test_dismiss_pending_unknown_client_404(db):
    with pytest.raises(__import__("fastapi").HTTPException) as exc:
        await pairing.dismiss_pending_client(_admin_request(db), "does-not-exist")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_clients_list_flags_and_excludes(db_with_client, monkeypatch):
    import api.integration as integration

    db, client_id, _secret, _hash = db_with_client

    def _integration_request():
        # /api/integration/* accepts only the dedicated integration token.
        return SimpleNamespace(
            state=SimpleNamespace(auth_type="integration"),
            app=SimpleNamespace(state=SimpleNamespace(db=db)),
        )

    async def _clients():
        result = await integration.integration_status(_integration_request())
        return result["clients"]

    # Active client: not flagged pending, present in list.
    clients = await _clients()
    assert any(c["id"] == client_id for c in clients)
    me = next(c for c in clients if c["id"] == client_id)
    assert me["pending_deletion"] is False

    # Mark pending (not hidden) → flagged but still listed.
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()
    clients = await _clients()
    me = next(c for c in clients if c["id"] == client_id)
    assert me["pending_deletion"] is True

    # Hide it → excluded from the list entirely.
    await db.set_pending_hidden(client_id)
    clients = await _clients()
    assert all(c["id"] != client_id for c in clients)


# ── a hidden tombstone still resets a returning app (handshake + ack) ────────


@pytest.mark.asyncio
async def test_hidden_tombstone_still_gets_handshake_and_ack_purges(
    db_with_client, monkeypatch
):
    db, client_id, secret, _hash = db_with_client

    # Pending + hidden (background-only tombstone).
    await db.execute(
        "UPDATE paired_clients SET pending_deletion_at = ?, is_active = 0 WHERE id = ?",
        (Database.now(), client_id),
    )
    await db.commit()
    await db.set_pending_hidden(client_id)

    monkeypatch.setattr(ws, "broadcast", _async_noop)

    fake_ws = _FakeWebSocket(db)
    try:
        authenticated, cid = await ws._handle_message(
            fake_ws, {"type": "auth", "secret": secret, **CLIENT_INFO}, False, None
        )

        # Returning app is still walked through the reset handshake.
        assert authenticated is False
        assert cid is None
        assert any(m.get("type") == "client_removed" for m in fake_ws.sent)
        assert (
            await db.fetchone(
                "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
            )
        ) is not None

        # Its wipe-ack purges the hidden tombstone for good.
        await ws._handle_message(
            fake_ws,
            {"type": "client_reset_ack", "client_id": client_id},
            False,
            None,
        )
        assert (
            await db.fetchone(
                "SELECT id FROM paired_clients WHERE id = ?", (client_id,)
            )
        ) is None
    finally:
        ws._clients.pop(fake_ws, None)
