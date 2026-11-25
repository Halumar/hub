"""client_leave: a device signs itself off before it resets.

The hub removes the device at once like an admin kick (row, HA user, open
invitations; other connections close), without a tombstone, answers
``client_leave_ok`` on the asking socket and closes it with 1000. Admin UIs get
``client_kicked`` with ``reason: left``, the audit log ``client_left``.
"""

import json
from types import SimpleNamespace

import pytest

import api.websocket as ws
from api.ws import registry
from services import audit_log
from services import device_provisioning as dp


class _FakeWebSocket:
    def __init__(self, db):
        self.app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
        self.client = SimpleNamespace(host="192.0.2.3")
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


@pytest.fixture
def recorded(monkeypatch):
    out = {"broadcasts": [], "integration": [], "disconnects": []}

    async def fake_broadcast(message, audience=None):
        out["broadcasts"].append((message, audience))

    async def fake_sender(message):
        out["integration"].append(message)
        return True

    async def fake_disconnect(client_id, code=4003, reason=""):
        out["disconnects"].append((client_id, code))
        return 1

    monkeypatch.setattr(ws, "broadcast", fake_broadcast)
    monkeypatch.setattr(ws, "disconnect_client", fake_disconnect)
    monkeypatch.setattr(dp, "_sender", fake_sender)
    audit_log.reset_recent()
    return out


async def _row(db, client_id):
    return await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (client_id,))


@pytest.mark.asyncio
async def test_leave_removes_the_device_and_its_ha_user(db_with_client, recorded):
    db, client_id, _secret, _hash = db_with_client
    fake_ws = _FakeWebSocket(db)
    registry._clients[fake_ws] = {"client_id": client_id, "authenticated": True}
    try:
        authenticated, cid = await ws._handle_message(
            fake_ws, {"type": "client_leave", "client_id": client_id}, True, client_id
        )
    finally:
        registry._clients.pop(fake_ws, None)

    assert (authenticated, cid) == (True, client_id)
    assert await _row(db, client_id) is None, "no tombstone: the device wiped itself"
    assert fake_ws.sent[-1] == {"type": "client_leave_ok"}
    assert fake_ws.closed == (1000, "Client left")
    assert fake_ws not in registry._clients
    # The HA user goes at once.
    assert recorded["integration"] == [
        {
            "type": "deprovision_client",
            "data": {"client_id": client_id, "reason": "deleted"},
        }
    ]
    # Other connections of the device are closed; admin UIs are told why.
    assert recorded["disconnects"] == [(client_id, 4001)]
    kicked = [m for m, aud in recorded["broadcasts"] if m["type"] == "client_kicked"]
    assert kicked == [
        {
            "type": "client_kicked",
            "data": {"client_id": client_id, "is_guest": False, "reason": "left"},
        }
    ]
    entry = audit_log.recent_entries(5)[0]
    assert entry["action"] == "client_left"
    assert client_id in entry["details"] and "Test Client" in entry["details"]
    assert entry["ip"] == "192.0.2.3"


@pytest.mark.asyncio
async def test_leave_of_a_guest(db_with_client, recorded):
    db, client_id, _secret, _hash = db_with_client
    await db.execute(
        "UPDATE paired_clients SET access = 'guest', "
        "expires_at = '2099-01-01T00:00:00+00:00', profile_id = (SELECT id FROM "
        "profiles WHERE origin_suggestion = 'guest') WHERE id = ?",
        (client_id,),
    )
    await db.commit()
    fake_ws = _FakeWebSocket(db)
    await ws._handle_message(
        fake_ws, {"type": "client_leave", "client_id": client_id}, True, client_id
    )
    assert await _row(db, client_id) is None
    assert recorded["disconnects"] == [(client_id, 4003)]
    assert fake_ws.sent[-1] == {"type": "client_leave_ok"}


@pytest.mark.asyncio
async def test_leave_with_another_id_changes_nothing(db_with_client, recorded):
    db, client_id, _secret, _hash = db_with_client
    fake_ws = _FakeWebSocket(db)
    await ws._handle_message(
        fake_ws,
        {"type": "client_leave", "client_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7"},
        True,
        client_id,
    )
    assert await _row(db, client_id) is not None
    assert fake_ws.closed is None
    assert fake_ws.sent == [
        {"type": "error", "message": "client_leave client_id mismatch"}
    ]
    assert recorded["integration"] == [] and recorded["broadcasts"] == []


@pytest.mark.asyncio
async def test_leave_is_only_for_paired_clients(db, recorded):
    fake_ws = _FakeWebSocket(db)
    await ws._handle_message(
        fake_ws, {"type": "client_leave", "client_id": "admin"}, True, "admin"
    )
    assert fake_ws.sent == [
        {"type": "error", "message": "client_leave is only for paired clients"}
    ]
    assert fake_ws.closed is None


@pytest.mark.asyncio
async def test_leave_needs_authentication(db, recorded):
    fake_ws = _FakeWebSocket(db)
    authenticated, _cid = await ws._handle_message(
        fake_ws, {"type": "client_leave", "client_id": "x"}, False, None
    )
    assert authenticated is False
    assert fake_ws.sent[-1]["type"] == "auth_required"


@pytest.mark.asyncio
async def test_failed_removal_keeps_the_socket_registered(
    db_with_client, recorded, monkeypatch
):
    db, client_id, _secret, _hash = db_with_client
    fake_ws = _FakeWebSocket(db)
    info = {"client_id": client_id, "authenticated": True}
    registry._clients[fake_ws] = info

    async def broken(*_a, **_k):
        raise RuntimeError("db gone")

    import api.pairing.clients as clients

    monkeypatch.setattr(clients, "end_client_access", broken)
    try:
        await ws._handle_message(
            fake_ws, {"type": "client_leave", "client_id": client_id}, True, client_id
        )
        assert registry._clients.get(fake_ws) is info
    finally:
        registry._clients.pop(fake_ws, None)
    assert fake_ws.sent == [
        {"type": "error", "message": "Failed to remove this device"}
    ]
    assert fake_ws.closed is None
