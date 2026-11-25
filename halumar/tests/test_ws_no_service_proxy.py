"""The WebSocket has no service-call message.

Apps call HA directly with their own HA token; the hub never calls a service on
a client's behalf with the Supervisor token. An incoming call_service message
falls through to the generic "Unknown message type" error and never reaches the
HA client.

services/ha_client.py (used for sensor sync and registry access) validates
domain and service names itself; its traversal guards are covered here too.
"""

import types

import pytest

from api.websocket import _handle_message


class FakeHAClient:
    """Records call_service invocations so we can assert none happen."""

    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, data=None, target=None):
        self.calls.append(
            {"domain": domain, "service": service, "data": data, "target": target}
        )
        return {"ok": True}


class FakeWebSocket:
    """Minimal WebSocket stand-in exposing app.state and send_json."""

    def __init__(self, db, ha_client):
        self.sent = []
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=ha_client)
        )

    async def send_json(self, message):
        self.sent.append(message)


class TestNoServiceCallMessage:
    """call_service is not a known message type."""

    async def test_call_service_is_unknown_message(self, db):
        ha = FakeHAClient()
        ws = FakeWebSocket(db, ha)
        msg = {
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "target": {"entity_id": "light.test"},
            "service_data": {},
        }
        await _handle_message(ws, msg, authenticated=True, client_id="ingress")

        reply = ws.sent[-1]
        assert reply["type"] == "error"
        assert reply["message"] == "Unknown message type"
        # Critically: the HA client must NOT have been invoked.
        assert ha.calls == []

    async def test_call_service_unknown_for_paired_client_too(self, db_with_client):
        """Not just admin sessions — paired clients get the same rejection."""
        db, client_id, _, _ = db_with_client
        ha = FakeHAClient()
        ws = FakeWebSocket(db, ha)
        msg = {"type": "call_service", "domain": "light", "service": "turn_on"}
        await _handle_message(ws, msg, authenticated=True, client_id=client_id)

        reply = ws.sent[-1]
        assert reply["type"] == "error"
        assert reply["message"] == "Unknown message type"
        assert ha.calls == []


class TestHAClientSinkValidation:
    """Defense in depth: the HA client itself rejects path traversal in domain,
    service and entity names, whatever the caller validated."""

    async def test_call_service_rejects_traversal(self):
        from services.ha_client import HAClient

        client = HAClient("fake-token")
        try:
            with pytest.raises(ValueError):
                await client.call_service("light", "../../homeassistant/restart")
            with pytest.raises(ValueError):
                await client.call_service("../../hassio", "turn_on")
            # A normal call passes validation (network layer is not exercised
            # here; the ValueError guard runs before any request is built).
            with pytest.raises(ValueError):
                await client.get_state("../../addons/self/info")
        finally:
            await client.close()

    async def test_validators_accept_real_values(self):
        from services.ha_client import is_valid_ha_name, is_valid_entity_id

        assert is_valid_ha_name("light")
        assert is_valid_ha_name("input_boolean")
        assert is_valid_ha_name("alarm_control_panel")
        assert is_valid_entity_id("light.living_room")
        assert is_valid_entity_id("sensor.halumar_config")
        # Rejections
        assert not is_valid_ha_name("../../x")
        assert not is_valid_ha_name("turn on")
        assert not is_valid_ha_name("")
        assert not is_valid_entity_id("../../addons/self/info")
        assert not is_valid_entity_id("light")  # missing object_id
        assert not is_valid_entity_id("a.b.c")  # too many dots
