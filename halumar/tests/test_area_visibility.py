"""Tests for per-client area visibility (assignment) checks.

Two layers are covered:

1. The ``_area_visible_for_client`` helper in api.websocket — the canonical
   assignment rule: an area is visible to a client when the area has no tags,
   OR the client has no tags, OR the two tag sets overlap.

2. The admin REST endpoint ``GET /pairing/clients/{client_id}/area-visibility/
   {area_id}`` which exposes that rule to the Web-UI as ``{"visible": bool}``.

These are VIEW/assignment checks only. Entity control is not the hub's
concern: clients call HA directly with their own HA token (the WebSocket has
no service-call message, see test_ws_no_service_proxy).
"""

import uuid
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from fastapi import HTTPException

import api.pairing as pairing
from api.websocket import _area_visible_for_client, _handle_message
from models.database import Database


class _FakeHA:
    async def get_state(self, entity_id):
        return {"state": "on", "attributes": {}, "last_changed": None}


class _FakeWS:
    def __init__(self, db, ha):
        self.app = SimpleNamespace(
            state=SimpleNamespace(db=db, ha_client=ha, app_url="http://test")
        )
        self.url = SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


def _admin_request(db):
    """Minimal Request that passes require_admin and carries the db."""
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="ingress"),
        app=SimpleNamespace(state=SimpleNamespace(db=db)),
    )


def _client_request(db):
    """Minimal Request authenticated as a paired client (non-admin)."""
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="client"),
        app=SimpleNamespace(state=SimpleNamespace(db=db)),
    )


async def _make_client(db, tags):
    return await add_client(
        db, name="C", secret_hash="h" * 64, last_seen=Database.now(), tags=list(tags)
    )


async def _make_area(db, tags, *, is_active=1):
    area_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (area_id, "A", is_active, now, now),
    )
    for t in tags:
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, t)
        )
    await db.commit()
    return area_id


class TestAreaVisibleHelper:
    async def test_untagged_area_not_listed_for_tagged_client(self, db):
        # Same rule as get_areas.
        client_id = await _make_client(db, ["upstairs"])
        area_id = await _make_area(db, [])
        assert await _area_visible_for_client(db, client_id, area_id) is False

    async def test_untagged_client_sees_tagged_area(self, db):
        client_id = await _make_client(db, [])
        area_id = await _make_area(db, ["downstairs"])
        assert await _area_visible_for_client(db, client_id, area_id) is True

    async def test_overlap_visible(self, db):
        client_id = await _make_client(db, ["upstairs", "guest"])
        area_id = await _make_area(db, ["downstairs", "guest"])
        assert await _area_visible_for_client(db, client_id, area_id) is True

    async def test_no_overlap_not_visible(self, db):
        client_id = await _make_client(db, ["upstairs"])
        area_id = await _make_area(db, ["downstairs"])
        assert await _area_visible_for_client(db, client_id, area_id) is False

    async def test_ingress_sees_everything(self, db):
        area_id = await _make_area(db, ["downstairs"])
        assert await _area_visible_for_client(db, "ingress", area_id) is True

    async def test_missing_area_returns_none(self, db):
        client_id = await _make_client(db, ["x"])
        assert await _area_visible_for_client(db, client_id, "nope") is None

    async def test_inactive_area_returns_none(self, db):
        client_id = await _make_client(db, [])
        area_id = await _make_area(db, [], is_active=0)
        assert await _area_visible_for_client(db, client_id, area_id) is None


class TestAreaVisibilityEndpoint:
    async def test_visible_true(self, db):
        client_id = await _make_client(db, ["guest"])
        area_id = await _make_area(db, ["guest"])
        res = await pairing.get_area_visibility(_admin_request(db), client_id, area_id)
        assert res == {"visible": True}

    async def test_visible_false(self, db):
        client_id = await _make_client(db, ["upstairs"])
        area_id = await _make_area(db, ["downstairs"])
        res = await pairing.get_area_visibility(_admin_request(db), client_id, area_id)
        assert res == {"visible": False}

    async def test_unknown_client_404(self, db):
        area_id = await _make_area(db, [])
        with pytest.raises(HTTPException) as exc:
            await pairing.get_area_visibility(_admin_request(db), "missing", area_id)
        assert exc.value.status_code == 404

    async def test_unknown_area_404(self, db):
        client_id = await _make_client(db, [])
        with pytest.raises(HTTPException) as exc:
            await pairing.get_area_visibility(_admin_request(db), client_id, "missing")
        assert exc.value.status_code == 404

    async def test_non_admin_forbidden(self, db):
        client_id = await _make_client(db, [])
        area_id = await _make_area(db, [])
        with pytest.raises(HTTPException) as exc:
            await pairing.get_area_visibility(_client_request(db), client_id, area_id)
        assert exc.value.status_code == 403


class TestDeepLinkOverride:
    """Override A, narrowed: a tag-restricted client may OPEN a
    tag-mismatched area only when one of its own notifications links to it.

    The notification link is admin-authored, so get_area returns the area
    detail although the area is not in the client's list. Without such a link
    the client gets "Area not found" (no read outside its scope).
    """

    async def _get_area(self, db, client_id, area_id):
        ws = _FakeWS(db, _FakeHA())
        await _handle_message(
            ws,
            {"type": "get_area", "area_id": area_id},
            authenticated=True,
            client_id=client_id,
        )
        return ws.sent[-1]

    async def test_restricted_client_can_load_linked_area_detail(self, db):
        from services.notifications import create_notification

        client_id = await _make_client(db, ["upstairs"])
        # Area tagged 'downstairs' -> not visible to this client's list.
        area_id = await _make_area(db, ["downstairs"])
        assert await _area_visible_for_client(db, client_id, area_id) is False
        await create_notification(
            db, client_id, "Look", "here", action_type="area", action_target=area_id
        )

        reply = await self._get_area(db, client_id, area_id)
        assert reply["type"] == "area"
        assert reply["data"]["id"] == area_id

    async def test_restricted_client_cannot_load_unlinked_area(self, db):
        client_id = await _make_client(db, ["upstairs"])
        area_id = await _make_area(db, ["downstairs"])

        reply = await self._get_area(db, client_id, area_id)
        assert reply == {"type": "error", "message": "Area not found"}
