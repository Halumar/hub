"""Guests fail closed on areas.

A guest without tags sees NO area (list, read scope, visibility helper). Only
the explicit marker tag ``authz.ALL_AREAS_TAG`` ("*") gives a guest every
active area. Members/household devices without tags keep seeing all active
areas. The marker is reserved: never an area tag or a predefined guest tag.
"""

import uuid
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from pydantic import ValidationError

from api import authz
from api.websocket import _area_visible_for_client, _handle_message
from models.database import Database
from models.schemas import AreaCreate, AreaUpdate


class _FakeWS:
    def __init__(self, db):
        self.app = SimpleNamespace(
            state=SimpleNamespace(db=db, ha_client=None, app_url="http://test")
        )
        self.url = SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def _client(db, tags, *, is_guest):
    return await add_client(
        db,
        name="C",
        secret_hash="h" * 64,
        last_seen=Database.now(),
        guest=is_guest,
        tags=list(tags),
    )


async def _area(db, tags, *, is_active=1):
    area_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (area_id, area_id[:8], is_active, now, now),
    )
    for tag in tags:
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
        )
    await db.commit()
    return area_id


async def _listed(db, client_id):
    ws = _FakeWS(db)
    await _handle_message(
        ws, {"type": "get_areas"}, authenticated=True, client_id=client_id
    )
    return {a["id"] for a in ws.sent[-1]["data"]}


@pytest.fixture
async def areas(db):
    return {
        "gast": await _area(db, ["gast"]),
        "og": await _area(db, ["og"]),
        "untagged": await _area(db, []),
        "inactive": await _area(db, ["gast"], is_active=0),
    }


class TestGuestFailClosed:
    async def test_guest_without_tags_sees_nothing(self, db, areas):
        guest = await _client(db, [], is_guest=True)
        assert await authz.listed_area_ids(db, guest) == set()
        assert await _listed(db, guest) == set()
        for area_id in (areas["gast"], areas["untagged"]):
            assert await authz.can_read_area(db, guest, area_id) is False
            assert await _area_visible_for_client(db, guest, area_id) is False

    async def test_guest_with_all_marker_sees_all_active(self, db, areas):
        guest = await _client(db, [authz.ALL_AREAS_TAG], is_guest=True)
        expected = {areas["gast"], areas["og"], areas["untagged"]}
        assert await authz.listed_area_ids(db, guest) == expected
        assert await _listed(db, guest) == expected
        assert await authz.can_read_area(db, guest, areas["inactive"]) is False
        assert await _area_visible_for_client(db, guest, areas["untagged"]) is True

    async def test_guest_with_tags_sees_matching_only(self, db, areas):
        guest = await _client(db, ["gast"], is_guest=True)
        assert await authz.listed_area_ids(db, guest) == {areas["gast"]}
        assert await _listed(db, guest) == {areas["gast"]}
        assert await _area_visible_for_client(db, guest, areas["untagged"]) is False
        assert await _area_visible_for_client(db, guest, areas["gast"]) is True

    async def test_member_without_tags_sees_all_active(self, db, areas):
        member = await _client(db, [], is_guest=False)
        expected = {areas["gast"], areas["og"], areas["untagged"]}
        assert await authz.listed_area_ids(db, member) == expected
        assert await _listed(db, member) == expected
        assert await _area_visible_for_client(db, member, areas["og"]) is True

    async def test_member_with_tags_unchanged(self, db, areas):
        member = await _client(db, ["og"], is_guest=False)
        assert await _listed(db, member) == {areas["og"]}
        # One rule for list and assignment helper: an untagged
        # area is not in a tagged member's list.
        assert await _area_visible_for_client(db, member, areas["untagged"]) is False
        assert await _area_visible_for_client(db, member, areas["og"]) is True

    async def test_admin_session_unrestricted(self, db, areas):
        assert await authz.client_area_scope(db, "ingress") == authz.ALL_AREAS
        assert await _listed(db, "ingress") == {
            areas["gast"],
            areas["og"],
            areas["untagged"],
        }


class TestReservedMarker:
    def test_area_tag_star_rejected(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="A", tags=["*"])
        with pytest.raises(ValidationError):
            AreaUpdate(tags=["gast", "*"])

    def test_pure_rule(self):
        def scope(tags, guest):
            from services.profiles import store

            return authz.scope_from_rule(store.areas_from_tags(tags, guest=guest))

        assert authz.area_in_scope(scope([], True), []) is False
        assert authz.area_in_scope(scope(["*"], True), []) is True
        assert authz.area_in_scope(scope([], False), ["x"]) is True
        assert authz.area_in_scope(scope(["gast"], True), ["gast", "og"]) is True
        assert authz.area_in_scope(scope(["gast"], True), ["og"]) is False
        assert authz.area_in_scope(authz.scope_from_rule(None), ["x"]) is False


class TestAreaPins:
    """Area choice when set up from a device: ``area:<id>`` pins exactly that area."""

    async def test_member_with_pins_sees_the_pinned_areas_only(self, db, areas):
        member = await _client(
            db, [authz.area_pin(areas["untagged"]), "og"], is_guest=False
        )
        expected = {areas["untagged"], areas["og"]}
        assert await authz.listed_area_ids(db, member) == expected
        assert await _listed(db, member) == expected
        assert await _area_visible_for_client(db, member, areas["untagged"]) is True
        assert await _area_visible_for_client(db, member, areas["gast"]) is False
        assert await authz.can_read_area(db, member, areas["gast"]) is False

    async def test_pin_of_an_inactive_area_is_not_listed(self, db, areas):
        member = await _client(db, [authz.area_pin(areas["inactive"])], is_guest=False)
        assert await authz.listed_area_ids(db, member) == set()
        assert await _listed(db, member) == set()

    async def test_guest_with_pins_sees_them(self, db, areas):
        guest = await _client(db, [authz.area_pin(areas["og"])], is_guest=True)
        assert await _listed(db, guest) == {areas["og"]}

    def test_area_pin_prefix_reserved_as_area_tag(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="A", tags=["area:x"])
        with pytest.raises(ValidationError):
            AreaUpdate(tags=["area:x"])
        assert authz.is_reserved_area_tag("area:x") and authz.is_reserved_area_tag("*")
        assert not authz.is_reserved_area_tag("garden")

    def test_pure_rule_with_area_id(self):
        from services.profiles import store

        pinned = authz.scope_from_rule(store.areas_from_tags(["area:a1"], guest=False))
        assert authz.area_in_scope(pinned, [], "a1") is True
        assert authz.area_in_scope(pinned, [], "a2") is False
        assert authz.area_in_scope(pinned, ["x"]) is False
        assert pinned.all is False
