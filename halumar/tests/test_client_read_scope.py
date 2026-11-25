"""What a paired client (and a guest) may read.

A client reads exactly the areas of its own ``get_areas`` list (plus areas its
own notifications link to) via ``get_area``; tokens, token-bearing pictures, GPS
of people/trackers and server file paths never reach a client. Admin sessions
are unrestricted. ``get_entity_state`` is gone and paired clients have no REST
access (REST area/tag reads are admin-only).
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from fastapi import HTTPException

import api.areas as areas
import api.pairing as pairing
from api import authz
from api.websocket import _handle_message
from models.database import Database
from services.notifications import create_notification

CAMERA_TOKEN = "cam-access-token-secret"


class _FakeHA:
    def __init__(self):
        self.calls = []
        self.states = {
            "light.kitchen": {"state": "on", "attributes": {"friendly_name": "K"}},
            "camera.door": {
                "state": "idle",
                "attributes": {
                    "access_token": CAMERA_TOKEN,
                    "entity_picture": f"/api/camera_proxy/camera.door?token={CAMERA_TOKEN}",
                    "friendly_name": "Door",
                },
            },
            "person.manager_one": {
                "state": "home",
                "attributes": {
                    "latitude": 47.1,
                    "longitude": 8.5,
                    "gps_accuracy": 5,
                    "entity_picture": "/api/image/serve/abc/512x512",
                    "friendly_name": "Manager One",
                },
            },
            "device_tracker.phone": {
                "state": "home",
                "attributes": {
                    "latitude": 47.1,
                    "longitude": 8.5,
                    "source_type": "gps",
                },
            },
        }

    async def get_state(self, entity_id):
        self.calls.append(entity_id)
        state = self.states.get(entity_id)
        if state is None:
            return None
        return {**state, "last_changed": "2026-11-18T10:00:00+00:00"}


class _FakeWS:
    def __init__(self, db, ha):
        self.app = SimpleNamespace(
            state=SimpleNamespace(db=db, ha_client=ha, app_url="http://test")
        )
        self.url = SimpleNamespace(hostname="test", port=8099, scheme="ws")
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


async def _client(db, tags, *, is_guest=False):
    return await add_client(
        db, name="C", last_seen=Database.now(), guest=is_guest, tags=list(tags)
    )


async def _area(db, tags, entities=(), *, is_active=1, name=None):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, card_image_path, "
        "created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?, ?)",
        (area_id, name or area_id[:8], is_active, "/data/images/x.png", now, now),
    )
    await db.execute(
        "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, "
        "columns) VALUES (?, ?, 'S', 0, 1, 2)",
        (section_id, area_id),
    )
    for i, entity_id in enumerate(entities):
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, "
            "sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), area_id, entity_id, section_id, i, now),
        )
    for tag in tags:
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
        )
    await db.commit()
    return area_id


async def _ws(db, ha, client_id, message):
    ws = _FakeWS(db, ha)
    await _handle_message(ws, message, authenticated=True, client_id=client_id)
    return ws.sent[-1]


def _request(db, ha, client_id=None):
    auth_type = "ingress" if client_id is None else "client"
    return SimpleNamespace(
        state=SimpleNamespace(auth_type=auth_type, client_id=client_id),
        app=SimpleNamespace(state=SimpleNamespace(db=db, ha_client=ha)),
    )


class TestWsGetArea:
    async def test_guest_without_visible_area_reads_nothing(self, db):
        guest = await _client(db, ["gast"], is_guest=True)
        hidden = await _area(db, [], ["light.kitchen"])
        ha = _FakeHA()

        assert (await _ws(db, ha, guest, {"type": "get_areas"}))["data"] == []
        reply = await _ws(db, ha, guest, {"type": "get_area", "area_id": hidden})
        assert reply == {"type": "error", "message": "Area not found"}
        assert ha.calls == [], "no HA state is fetched for a hidden area"

    async def test_tagged_area_readable_and_redacted(self, db):
        guest = await _client(db, ["gast"], is_guest=True)
        area_id = await _area(db, ["gast"], ["camera.door", "person.manager_one"])

        reply = await _ws(
            db, _FakeHA(), guest, {"type": "get_area", "area_id": area_id}
        )
        assert reply["type"] == "area"
        data = reply["data"]
        assert "card_image_path" not in data
        assert data["card_image_src"] == "/images/x.png"
        states = {
            e["entity_id"]: e["state"]["attributes"]
            for s in data["sections"]
            for e in s["entities"]
        }
        assert "access_token" not in states["camera.door"]
        assert "entity_picture" not in states["camera.door"]
        assert "latitude" not in states["person.manager_one"]
        assert "gps_accuracy" not in states["person.manager_one"]
        assert (
            states["person.manager_one"]["entity_picture"]
            == "/api/image/serve/abc/512x512"
        )
        assert CAMERA_TOKEN not in json.dumps(reply)

    async def test_inactive_area_denied(self, db):
        client = await _client(db, [])
        area_id = await _area(db, [], ["light.kitchen"], is_active=0)
        reply = await _ws(
            db, _FakeHA(), client, {"type": "get_area", "area_id": area_id}
        )
        assert reply["type"] == "error"

    async def test_foreign_tag_denied_for_tagged_client(self, db):
        client = await _client(db, ["oben"])
        area_id = await _area(db, ["unten"], ["light.kitchen"])
        reply = await _ws(
            db, _FakeHA(), client, {"type": "get_area", "area_id": area_id}
        )
        assert reply["type"] == "error"

    async def test_ingress_unrestricted_and_unredacted(self, db):
        area_id = await _area(db, ["unten"], ["camera.door"], is_active=0)
        reply = await _ws(
            db, _FakeHA(), "ingress", {"type": "get_area", "area_id": area_id}
        )
        assert reply["type"] == "area"
        assert reply["data"]["card_image_path"] == "/data/images/x.png"
        attrs = reply["data"]["sections"][0]["entities"][0]["state"]["attributes"]
        assert attrs["access_token"] == CAMERA_TOKEN

    async def test_get_areas_strips_server_paths_for_clients_only(self, db):
        client = await _client(db, [])
        await _area(db, [])
        reply = await _ws(db, _FakeHA(), client, {"type": "get_areas"})
        assert reply["data"] and "card_image_path" not in reply["data"][0]
        reply = await _ws(db, _FakeHA(), "ingress", {"type": "get_areas"})
        assert reply["data"][0]["card_image_path"] == "/data/images/x.png"


class TestRemovedEntityState:
    async def test_get_entity_state_is_gone(self, db):
        """No app used it; it read any HA state with the Supervisor token."""
        ha = _FakeHA()
        for client_id in (await _client(db, []), "ingress"):
            reply = await _ws(
                db,
                ha,
                client_id,
                {"type": "get_entity_state", "entity_id": "person.manager_one"},
            )
            assert reply == {"type": "error", "message": "Unknown message type"}
        assert ha.calls == []

    async def test_dismissed_notification_link_grants_nothing(self, db):
        client = await _client(db, ["oben"])
        area_id = await _area(db, ["unten"], ["light.kitchen"])
        n = await create_notification(
            db, client, "t", "m", action_type="area", action_target=area_id
        )
        assert await authz.can_read_area(db, client, area_id)
        await db.execute(
            "UPDATE client_notifications SET status = 'dismissed' WHERE id = ?",
            (n["id"],),
        )
        await db.commit()
        assert not await authz.can_read_area(db, client, area_id)

    async def test_other_clients_notification_grants_nothing(self, db):
        client = await _client(db, ["oben"])
        other = await _client(db, [])
        area_id = await _area(db, ["unten"])
        await create_notification(
            db, other, "t", "m", action_type="area", action_target=area_id
        )
        assert not await authz.can_read_area(db, client, area_id)


class TestRestReadsAdminOnly:
    """Paired-client secrets are no REST credential; area reads are admin-only."""

    async def test_client_secret_rejected_on_rest(self, db):
        from starlette.requests import Request

        from api.auth import AuthMiddleware, hash_secret

        secret = "client-secret-value"
        await add_client(
            db, client_id="c1", name="C", secret_hash=hash_secret(secret), last_seen="x"
        )

        async def _passed(request):
            return "PASSED"

        for path in ("/api/areas", "/api/tags", "/api/areas/x/sections"):
            scope = {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": [(b"authorization", f"Bearer {secret}".encode())],
                "client": ("192.0.2.5", 1),
                "server": ("t", 8100),
                "scheme": "https",
                "app": SimpleNamespace(state=SimpleNamespace(db=db)),
            }
            res = await AuthMiddleware(app=None).dispatch(Request(scope), _passed)
            assert res.status_code == 401, path

    async def test_read_handlers_require_admin(self, db):
        area_id = await _area(db, [], ["light.kitchen"])
        req = _request(db, _FakeHA(), "c1")
        for call in (areas.get_area, areas.list_sections, areas.list_widgets):
            with pytest.raises(HTTPException) as exc:
                await call(req, area_id)
            assert exc.value.status_code == 403
        for call in (areas.list_areas, pairing.get_all_tags):
            with pytest.raises(HTTPException) as exc:
                await call(req)
            assert exc.value.status_code == 403
        assert len(await areas.list_areas(_request(db, _FakeHA()))) == 1


class TestSanitizer:
    def test_input_not_mutated(self):
        attrs = {"access_token": "x", "latitude": 1}
        state = {"state": "home", "attributes": attrs}
        clean = authz.sanitize_state("person.p", state)
        assert clean["attributes"] == {}
        assert attrs == {"access_token": "x", "latitude": 1}

    def test_other_domains_keep_coordinates(self):
        attrs = {"latitude": 1, "longitude": 2}
        assert authz.sanitize_attributes("zone.home", attrs) == attrs
