"""Tests for WebSocket module — broadcast filtering, helpers, constants."""

from api.websocket import (
    EventAudience,
    _is_admin_client,
    _hash_secret,
    get_connected_client_ids,
    get_connected_count,
    MAX_WS_CONNECTIONS_AUTH,
    MAX_WS_CONNECTIONS_UNAUTH,
    MAX_WS_UNAUTH_PER_IP,
    MAX_PAIR_ATTEMPTS,
    MAX_AUTH_ATTEMPTS,
    MAX_WS_MESSAGE_SIZE,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    _clients,
)


class TestEventAudience:
    """EventAudience enum."""

    def test_audience_values(self):
        assert EventAudience.ALL.value == "all"
        assert EventAudience.ADMIN.value == "admin"

    def test_audience_is_string_enum(self):
        assert isinstance(EventAudience.ALL, str)
        assert isinstance(EventAudience.ADMIN, str)


class TestIsAdminClient:
    """_is_admin_client helper."""

    def test_ingress_is_admin(self):
        assert _is_admin_client("ingress") is True

    def test_dev_is_admin(self):
        assert _is_admin_client("dev") is True

    def test_regular_client_not_admin(self):
        assert _is_admin_client("abc-123-def") is False

    def test_none_not_admin(self):
        assert _is_admin_client(None) is False

    def test_empty_string_not_admin(self):
        assert _is_admin_client("") is False


class TestHashSecret:
    """_hash_secret helper."""

    def test_returns_hex_string(self):
        h = _hash_secret("test-secret")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex

    def test_deterministic(self):
        assert _hash_secret("abc") == _hash_secret("abc")

    def test_different_inputs_different_hashes(self):
        assert _hash_secret("secret1") != _hash_secret("secret2")


class TestSecurityConstants:
    """Security constants are reasonable."""

    def test_connection_limits(self):
        assert MAX_WS_CONNECTIONS_AUTH == 100
        assert MAX_WS_CONNECTIONS_UNAUTH == 200
        assert MAX_WS_UNAUTH_PER_IP == 5

    def test_attempt_limits(self):
        assert MAX_PAIR_ATTEMPTS == 5
        assert MAX_AUTH_ATTEMPTS == 5

    def test_message_size_limit(self):
        assert MAX_WS_MESSAGE_SIZE == 64 * 1024  # 64 KB

    def test_ping_config(self):
        assert WS_PING_INTERVAL == 30
        assert WS_PING_TIMEOUT == 10
        assert WS_PING_TIMEOUT < WS_PING_INTERVAL


class TestGetConnectedClientIds:
    """get_connected_client_ids and get_connected_count."""

    def test_empty_clients(self):
        """Should return empty set when no clients connected."""
        # Clear any state
        _clients.clear()
        assert get_connected_client_ids() == set()
        assert get_connected_count() == 0
