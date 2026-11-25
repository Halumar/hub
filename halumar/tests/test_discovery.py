"""Supervisor discovery announce: payload contract and token rotation.

announce_integration_discovery must:
1. resolve the add-on hostname from GET /addons/self/info (data.hostname),
2. rotate the dedicated integration token (hash-only storage), and
3. POST service "halumar" with config keys EXACTLY
   {"host", "port", "api_token", "version"} — port 8099, plaintext token.

A missing hostname must SKIP the announce without rotating the token, so a
broken Supervisor call never invalidates a working integration setup.
"""

import pytest

from api.auth import hash_secret
from services.discovery import (
    API_PORT,
    DISCOVERY_SERVICE,
    announce_integration_discovery,
)
from version import VERSION


class FakeSupervisorHAClient:
    """Records the discovery POST; serves a canned /addons/self/info reply."""

    def __init__(self, hostname="local-halumar"):
        self._hostname = hostname
        self.discoveries = []

    async def get_addon_info(self):
        data = {"result": "ok", "data": {}}
        if self._hostname is not None:
            data["data"]["hostname"] = self._hostname
        return data

    async def send_discovery(self, service, config):
        self.discoveries.append((service, config))
        return {"result": "ok", "data": {"uuid": "u" * 32}}


@pytest.mark.asyncio
async def test_announce_posts_exact_payload(db):
    ha = FakeSupervisorHAClient(hostname="local-halumar")

    ok = await announce_integration_discovery(db, ha)
    assert ok is True

    assert len(ha.discoveries) == 1
    service, config = ha.discoveries[0]
    assert service == DISCOVERY_SERVICE == "halumar"
    # Config keys EXACTLY per contract — nothing more, nothing less.
    assert set(config) == {"host", "port", "api_token", "version"}
    assert config["host"] == "local-halumar"
    assert config["port"] == API_PORT == 8099
    assert config["version"] == VERSION

    # The announced plaintext token is the freshly rotated one (64-hex,
    # hash-only storage).
    token = config["api_token"]
    assert len(token) == 64
    int(token, 16)
    assert await db.get_setting("integration_token_hash") == hash_secret(token)


@pytest.mark.asyncio
async def test_announce_rotates_on_every_call(db):
    ha = FakeSupervisorHAClient()
    await announce_integration_discovery(db, ha)
    await announce_integration_discovery(db, ha)

    t1 = ha.discoveries[0][1]["api_token"]
    t2 = ha.discoveries[1][1]["api_token"]
    assert t1 != t2
    # Only the LATEST token validates.
    assert await db.get_setting("integration_token_hash") == hash_secret(t2)


@pytest.mark.asyncio
async def test_missing_hostname_skips_announce_and_keeps_token(db):
    """No hostname -> no announce AND no rotation (fail-safe ordering)."""
    from api.auth import rotate_integration_token

    current = await rotate_integration_token(db)
    ha = FakeSupervisorHAClient(hostname=None)

    ok = await announce_integration_discovery(db, ha)
    assert ok is False
    assert ha.discoveries == []
    # The pre-existing token is still the valid one.
    assert await db.get_setting("integration_token_hash") == hash_secret(current)


@pytest.mark.asyncio
async def test_token_never_logged(db, caplog):
    """The plaintext token must not appear in any log record."""
    import logging

    ha = FakeSupervisorHAClient()
    with caplog.at_level(logging.DEBUG):
        await announce_integration_discovery(db, ha)

    token = ha.discoveries[0][1]["api_token"]
    assert token not in caplog.text
