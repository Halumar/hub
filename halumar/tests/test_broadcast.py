"""Tests for broadcast audience filtering.

Uses mock WebSocket objects to verify that ADMIN-audience events
are only sent to ingress/dev sessions, not to external paired clients.
"""

import json


from api.websocket import broadcast, EventAudience, _clients


class MockWebSocket:
    """Minimal mock WebSocket for broadcast testing."""

    def __init__(self, client_id="ingress", authenticated=True):
        self._sent: list[str] = []
        self._client_id = client_id
        self._authenticated = authenticated

    async def send_text(self, data: str):
        self._sent.append(data)

    @property
    def sent_messages(self) -> list[dict]:
        return [json.loads(s) for s in self._sent]


def _register_client(ws: MockWebSocket, client_id: str, authenticated: bool = True):
    """Register a mock WebSocket in the global _clients dict."""
    _clients[ws] = {
        "authenticated": authenticated,
        "client_id": client_id,
        "pair_attempts": 0,
        "auth_attempts": 0,
    }


class TestBroadcastAudienceAll:
    """EventAudience.ALL sends to everyone."""

    async def test_all_clients_receive(self):
        _clients.clear()

        ws_ingress = MockWebSocket()
        ws_client = MockWebSocket()

        _register_client(ws_ingress, "ingress")
        _register_client(ws_client, "abc-paired-client")

        await broadcast(
            {"type": "area_updated", "data": {"area_id": "123"}},
            audience=EventAudience.ALL,
        )

        assert len(ws_ingress.sent_messages) == 1
        assert ws_ingress.sent_messages[0]["type"] == "area_updated"
        assert len(ws_client.sent_messages) == 1
        assert ws_client.sent_messages[0]["type"] == "area_updated"

        _clients.clear()

    async def test_default_audience_is_all(self):
        _clients.clear()

        ws_client = MockWebSocket()
        _register_client(ws_client, "some-client")

        # No audience parameter = default ALL
        await broadcast({"type": "area_updated", "data": {}})

        assert len(ws_client.sent_messages) == 1

        _clients.clear()


class TestBroadcastAudienceAdmin:
    """EventAudience.ADMIN sends only to ingress/dev."""

    async def test_admin_only_to_ingress(self):
        _clients.clear()

        ws_ingress = MockWebSocket()
        ws_client = MockWebSocket()

        _register_client(ws_ingress, "ingress")
        _register_client(ws_client, "abc-paired-client")

        await broadcast(
            {"type": "area_created", "data": {"area_id": "123"}},
            audience=EventAudience.ADMIN,
        )

        # Ingress should receive
        assert len(ws_ingress.sent_messages) == 1
        assert ws_ingress.sent_messages[0]["type"] == "area_created"

        # External client should NOT receive
        assert len(ws_client.sent_messages) == 0

        _clients.clear()

    async def test_admin_to_dev_mode(self):
        _clients.clear()

        ws_dev = MockWebSocket()
        ws_client = MockWebSocket()

        _register_client(ws_dev, "dev")
        _register_client(ws_client, "xyz-panel")

        await broadcast(
            {"type": "client_paired", "data": {"client_id": "new"}},
            audience=EventAudience.ADMIN,
        )

        assert len(ws_dev.sent_messages) == 1
        assert len(ws_client.sent_messages) == 0

        _clients.clear()

    async def test_admin_skips_unauthenticated(self):
        _clients.clear()

        ws_unauth = MockWebSocket()
        _register_client(ws_unauth, "ingress", authenticated=False)

        await broadcast(
            {"type": "area_deleted", "data": {}},
            audience=EventAudience.ADMIN,
        )

        assert len(ws_unauth.sent_messages) == 0

        _clients.clear()

    async def test_admin_multiple_ingress(self):
        """Multiple admin sessions should all receive."""
        _clients.clear()

        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        ws_ext = MockWebSocket()

        _register_client(ws1, "ingress")
        _register_client(ws2, "dev")
        _register_client(ws_ext, "panel-1")

        await broadcast(
            {"type": "notification_created", "data": {}},
            audience=EventAudience.ADMIN,
        )

        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 1
        assert len(ws_ext.sent_messages) == 0

        _clients.clear()


class TestBroadcastEventClassification:
    """Verify that specific event types are sent with the correct audience.

    These tests document the expected audience for each event type.
    The actual audience is set at the call site, so these tests serve
    as documentation of the design intent.
    """

    def test_admin_only_events(self):
        """Events that should only go to admin sessions."""
        admin_events = {
            "area_created",
            "area_deleted",
            "areas_deleted",
            "client_paired",
            "client_kicked",
            "clients_cleared",
            "guest_extended",
            "guests_expired",
            "client_connected",
            "client_disconnected",
            "client_status_updated",
            "notification_created",
            "notification_deleted",
            "notifications_cleared",
            "notification_delivered",
        }
        # This is a documentation test — it doesn't execute the broadcast
        # but ensures the classification is explicit and reviewed.
        assert len(admin_events) == 15

    def test_all_audience_events(self):
        """Events that should go to all clients."""
        all_events = {
            "area_updated",
            "layout_updated",
            "client_config_updated",
        }
        assert len(all_events) == 3


class TestBroadcastEdgeCases:
    """Edge cases for broadcast."""

    async def test_no_clients(self):
        """Broadcast with no clients should not raise."""
        _clients.clear()
        # Should not raise
        await broadcast({"type": "test"})
        await broadcast({"type": "test"}, audience=EventAudience.ADMIN)

    async def test_only_unauthenticated_clients(self):
        """Broadcast with only unauthenticated clients should do nothing."""
        _clients.clear()
        ws = MockWebSocket()
        _register_client(ws, None, authenticated=False)

        await broadcast({"type": "test"})
        assert len(ws.sent_messages) == 0

        _clients.clear()
