"""Sensor contract: full-content notification sensors, ids, WS channel.

This suite pins the shared hub <-> app contract:

1. sensor.halumar_client_{slug}_{uid8}_notifications — FULL content BY
   DESIGN (it is the REMOTE delivery path: the hub is LAN-only, so away
   from home the app reaches only Home Assistant and this sensor is its only
   notification source; any HA user/token can read it — accepted, content is
   treated as not confidential). state = str(count); attributes = EXACTLY
   friendly_name, icon, count, notifications (newest first), updated_at.
   Each notifications item is serialized by notification_to_push_payload —
   the SAME shape as the get_notifications WS response items /
   display_notification push (ids live INSIDE the items as notification_id;
   there is no separate notification_ids attribute). The entity id is derived
   by client_notification_sensor_id (uid8 = first 8 chars of the lowercased,
   dash-stripped client UUID, slug capped at 48) and delivered to the client
   via client_config (notification_sensor) — the SAME helper on both sides,
   so they cannot drift. The sync only posts; it deletes no other entity ids.

2. sensor.halumar_guests — per guest ONLY client_id, is_connected,
   expires_at (no names, tags, device names, activity timestamps).

3. get_notifications is THE single WS content channel (there is no
   get_pending_notifications): {"type": "notifications", "notifications":
   [...]} where each item mirrors the display_notification push payload
   (notification_id + content fields, plus status). Paired clients get ONLY
   their own active notifications; ingress/dev and unauthenticated sockets
   are rejected.
"""

import types

from api.websocket import _get_client_config, _handle_message
from models.database import Database

from .profile_helpers import add_client
from services.ha_sensors import (
    client_notification_sensor_id,
    sync_client_notification_sensors,
    sync_guests_sensor,
)
from services.notifications import create_notification


class RecordingHAClient:
    """Records _post and _delete calls (url, payload) for shape assertions."""

    def __init__(self):
        self.posts = []
        self.deletes = []

    async def _post(self, url, payload):
        self.posts.append((url, payload))

    async def _delete(self, url):
        self.deletes.append(url)


class FakeWebSocket:
    """Minimal WebSocket stand-in exposing app.state and send_json."""

    def __init__(self, db, ha_client=None):
        self.sent = []
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha_client)
        )

    async def send_json(self, message):
        self.sent.append(message)


async def _make_client(db, name="Panel", *, is_guest=False, expires_at=None):
    """Insert a paired client; returns its id."""
    return await add_client(
        db,
        name=name,
        secret_hash="h" * 64,
        last_seen=Database.now(),
        guest=is_guest,
        expires_at=expires_at,
        device_name="iPad Pro",
    )


# ── notification sensor: full-content shape + id derivation ─────────────────


class TestClientNotificationSensor:
    async def test_state_and_exact_attribute_set(self, db_with_client):
        db, client_id, _, _ = db_with_client
        n1 = await create_notification(
            db, client_id=client_id, title="Door open", message="Front door"
        )
        n2 = await create_notification(
            db, client_id=client_id, title="Second", message="More detail"
        )

        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)

        assert len(ha.posts) == 1
        url, payload = ha.posts[0]
        expected_id = client_notification_sensor_id("Test Client", client_id)
        assert url == f"http://supervisor/core/api/states/{expected_id}"

        assert payload["state"] == "2"
        attrs = payload["attributes"]
        # EXACT attribute set; there is no notification_ids attribute (ids
        # live inside the items).
        assert set(attrs) == {
            "friendly_name",
            "icon",
            "count",
            "notifications",
            "updated_at",
        }
        assert attrs["count"] == 2
        # Newest first (n2 was created after n1), ids inside the items.
        ids = [item["notification_id"] for item in attrs["notifications"]]
        assert ids == [n2["id"], n1["id"]]
        assert "notification_ids" not in attrs
        # FULL content in the sensor — it is the remote delivery path, and
        # its world-readability for HA users is accepted by design.
        assert attrs["notifications"][0]["title"] == "Second"
        assert attrs["notifications"][0]["message"] == "More detail"
        assert attrs["notifications"][1]["title"] == "Door open"
        assert attrs["notifications"][1]["message"] == "Front door"

    async def test_items_are_push_shaped(self, db_with_client):
        """Sensor items carry EXACTLY the display_notification push shape
        (same keys, same casing — no DB-row 'id' key), including status."""
        db, client_id, _, _ = db_with_client
        n = await create_notification(
            db,
            client_id=client_id,
            title="Door open",
            message="Front door",
            icon="mdi:door",
            icon_svg="M1 2",
            popup_mode="popup",
            priority=80,
            color="red",
            action_type="area",
            action_target="area-1",
            show_time=False,
        )

        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)

        _, payload = ha.posts[0]
        item = payload["attributes"]["notifications"][0]
        assert set(item) == {
            "notification_id",
            "title",
            "message",
            "notification_type",
            "popup",
            "popup_mode",
            "scheduled_at",
            "priority",
            "color",
            "action_type",
            "action_target",
            "show_time",
            "created_at",
            "status",
            "icon",
            "icon_svg",
        }
        assert item["notification_id"] == n["id"]
        assert "id" not in item
        assert item["title"] == "Door open"
        assert item["message"] == "Front door"
        assert item["notification_type"] == "default"
        assert item["icon"] == "mdi:door"
        assert item["icon_svg"] == "M1 2"
        assert item["popup"] is True
        assert item["popup_mode"] == "popup"
        assert item["scheduled_at"] is None
        assert item["priority"] == 80
        assert item["color"] == "red"
        assert item["action_type"] == "area"
        assert item["action_target"] == "area-1"
        assert item["show_time"] is False
        assert item["created_at"] == n["created_at"]
        assert item["status"] == "pending"

    async def test_items_equal_ws_get_notifications_items(self, db_with_client):
        """Drift guard: the sensor array and the WS content channel are the
        SAME serialization (notification_to_push_payload) — byte-for-byte."""
        db, client_id, _, _ = db_with_client
        await create_notification(
            db, client_id=client_id, title="A", message="a", icon="mdi:door"
        )
        await create_notification(db, client_id=client_id, title="B", message="b")

        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)
        _, payload = ha.posts[0]
        sensor_items = payload["attributes"]["notifications"]

        ws = FakeWebSocket(db)
        await _handle_message(
            ws, {"type": "get_notifications"}, authenticated=True, client_id=client_id
        )
        ws_items = ws.sent[-1]["notifications"]

        assert sensor_items == ws_items

    async def test_icon_keys_omitted_when_unset(self, db_with_client):
        """icon/icon_svg are conditional keys, matching the push behavior."""
        db, client_id, _, _ = db_with_client
        await create_notification(db, client_id=client_id, title="t", message="m")

        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)
        _, payload = ha.posts[0]
        item = payload["attributes"]["notifications"][0]
        assert "icon" not in item
        assert "icon_svg" not in item

    async def test_empty_state_and_icon(self, db_with_client):
        db, client_id, _, _ = db_with_client
        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)

        _, payload = ha.posts[0]
        assert payload["state"] == "0"
        assert payload["attributes"]["count"] == 0
        assert payload["attributes"]["notifications"] == []
        assert payload["attributes"]["icon"] == "mdi:bell-outline"

    async def test_entity_id_derivation(self):
        cid = "ABC12345-6789-0DEF-1111-222233334444"
        # uid8 = first 8 chars of lowercase(id) with dashes removed.
        assert (
            client_notification_sensor_id("Kitchen Panel", cid)
            == "sensor.halumar_client_kitchen_panel_abc12345_notifications"
        )

    async def test_same_name_clients_get_distinct_sensors(self, db):
        c1 = await _make_client(db, "Panel")
        c2 = await _make_client(db, "Panel")
        assert client_notification_sensor_id(
            "Panel", c1
        ) != client_notification_sensor_id("Panel", c2)

        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)
        urls = {url for url, _ in ha.posts}
        assert len(urls) == 2

    async def test_sync_deletes_nothing(self, db_with_client):
        """The sync only posts sensors; it deletes nothing."""
        db, _, _, _ = db_with_client
        ha = RecordingHAClient()
        await sync_client_notification_sensors(db, ha)
        assert ha.deletes == []

    async def test_client_config_delivers_same_entity_id(self, db_with_client):
        """client_config.notification_sensor uses the SAME derivation helper."""
        db, client_id, _, _ = db_with_client
        cfg = await _get_client_config(db, client_id)
        assert cfg["notification_sensor"] == client_notification_sensor_id(
            "Test Client", client_id
        )
        # uid8 (not just the slug) must be part of the delivered id.
        uid8 = client_id.lower().replace("-", "")[:8]
        assert f"_{uid8}_notifications" in cfg["notification_sensor"]


# ── guests sensor: stripped attributes ───────────────────────────────────────


class TestGuestsSensor:
    async def test_state_and_guest_entry_shape(self, db):
        guest_id = await _make_client(
            db,
            "Manager One's iPhone",
            is_guest=True,
            expires_at="2026-07-04T10:00:00+00:00",
        )
        await _make_client(db, "Wall Panel")  # non-guest, must not appear

        ha = RecordingHAClient()
        await sync_guests_sensor(db, ha)

        assert len(ha.posts) == 1
        url, payload = ha.posts[0]
        assert url == "http://supervisor/core/api/states/sensor.halumar_guests"
        assert payload["state"] == "1"

        attrs = payload["attributes"]
        assert set(attrs) == {
            "friendly_name",
            "icon",
            "guests",
            "updated_at",
            "unit_of_measurement",
        }
        assert len(attrs["guests"]) == 1
        entry = attrs["guests"][0]
        # ONLY client_id, is_connected, expires_at — no name/tags/device/PII.
        assert set(entry) == {"client_id", "is_connected", "expires_at"}
        assert entry["client_id"] == guest_id
        assert entry["is_connected"] is False
        assert entry["expires_at"] == "2026-07-04T10:00:00+00:00"
        # The guest's name must not leak anywhere in the payload.
        assert "Manager One" not in str(payload)

    async def test_no_guests(self, db):
        ha = RecordingHAClient()
        await sync_guests_sensor(db, ha)
        _, payload = ha.posts[0]
        assert payload["state"] == "0"
        assert payload["attributes"]["guests"] == []


# ── get_pending_notifications is not a message type ─────────────────────────


class TestGetPendingNotificationsUnknown:
    async def test_rejected_as_unknown_message(self, db):
        """get_notifications is the single content channel."""
        mine = await _make_client(db, "Mine")
        await create_notification(db, client_id=mine, title="t", message="m")

        ws = FakeWebSocket(db)
        await _handle_message(
            ws,
            {"type": "get_pending_notifications"},
            authenticated=True,
            client_id=mine,
        )
        reply = ws.sent[-1]
        assert reply == {"type": "error", "message": "Unknown message type"}


# ── WS content channel: get_notifications (single, push-shaped items) ───────


class TestGetNotificationsChannel:
    async def test_response_type_and_push_shaped_items(self, db):
        mine = await _make_client(db, "Mine")
        n = await create_notification(
            db,
            client_id=mine,
            title="Door open",
            message="Front door",
            icon="mdi:door",
            icon_svg="M1 2",
            popup_mode="popup",
            priority=80,
            color="red",
            action_type="area",
            action_target="area-1",
            show_time=False,
        )

        ws = FakeWebSocket(db)
        await _handle_message(
            ws, {"type": "get_notifications"}, authenticated=True, client_id=mine
        )

        reply = ws.sent[-1]
        assert reply["type"] == "notifications"
        assert len(reply["notifications"]) == 1
        item = reply["notifications"][0]
        # Push naming: notification_id (not the DB-row "id" key).
        assert item["notification_id"] == n["id"]
        assert "id" not in item
        # Same content fields as the display_notification push, plus status.
        assert item["title"] == "Door open"
        assert item["message"] == "Front door"
        assert item["notification_type"] == "default"
        assert item["icon"] == "mdi:door"
        assert item["icon_svg"] == "M1 2"
        assert item["popup"] is True
        assert item["popup_mode"] == "popup"
        assert item["scheduled_at"] is None
        assert item["priority"] == 80
        assert item["color"] == "red"
        assert item["action_type"] == "area"
        assert item["action_target"] == "area-1"
        assert item["show_time"] is False
        assert item["created_at"] == n["created_at"]
        assert item["status"] == "pending"

    async def test_icon_omitted_when_unset(self, db):
        """icon/icon_svg are conditional keys, matching the push behavior."""
        mine = await _make_client(db, "Mine")
        await create_notification(db, client_id=mine, title="t", message="m")

        ws = FakeWebSocket(db)
        await _handle_message(
            ws, {"type": "get_notifications"}, authenticated=True, client_id=mine
        )
        item = ws.sent[-1]["notifications"][0]
        assert "icon" not in item
        assert "icon_svg" not in item

    async def test_returns_only_own_notifications_newest_first(self, db):
        mine = await _make_client(db, "Mine")
        other = await _make_client(db, "Other")
        n1 = await create_notification(db, client_id=mine, title="First", message="")
        n2 = await create_notification(db, client_id=mine, title="Second", message="")
        await create_notification(
            db, client_id=other, title="Not yours", message="private"
        )

        ws = FakeWebSocket(db)
        await _handle_message(
            ws, {"type": "get_notifications"}, authenticated=True, client_id=mine
        )
        reply = ws.sent[-1]
        ids = [n["notification_id"] for n in reply["notifications"]]
        assert ids == [n2["id"], n1["id"]]
        assert "Not yours" not in str(reply)

    async def test_dismissed_not_included(self, db):
        mine = await _make_client(db, "Mine")
        n = await create_notification(db, client_id=mine, title="t", message="m")
        from services.notifications import dismiss_notification

        await dismiss_notification(db, n["id"])

        ws = FakeWebSocket(db)
        await _handle_message(
            ws, {"type": "get_notifications"}, authenticated=True, client_id=mine
        )
        assert ws.sent[-1]["notifications"] == []

    async def test_rejected_for_ingress_and_dev(self, db):
        for admin_id in ("ingress", "dev"):
            ws = FakeWebSocket(db)
            await _handle_message(
                ws,
                {"type": "get_notifications"},
                authenticated=True,
                client_id=admin_id,
            )
            reply = ws.sent[-1]
            assert reply["type"] == "error"
            assert "only for paired clients" in reply["message"]

    async def test_requires_authentication(self, db):
        ws = FakeWebSocket(db)
        await _handle_message(
            ws,
            {"type": "get_notifications"},
            authenticated=False,
            client_id=None,
        )
        reply = ws.sent[-1]
        assert reply["type"] == "auth_required"
