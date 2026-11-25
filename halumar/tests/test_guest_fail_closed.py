"""Guests (and every device) fail closed until their own HA token arrives.

A device only receives an ``ha_token`` once the integration delivered the
token of its own HA user (POST /api/integration/client-token). There
is no shared guest token and no hub-minted token: a device that waits for its
token (``ha_token_status`` pending) gets ``ha_token: None``.

Reuses the ``db`` fixture from conftest.py.
"""

import pytest

from .profile_helpers import add_client

import services.crypto as crypto
from api.websocket import _get_client_config
from models.database import Database


@pytest.fixture(autouse=True)
def isolated_keyfile(tmp_path, monkeypatch):
    """Isolate the Fernet keyfile per-test so encryption works off /data."""
    keyfile = str(tmp_path / ".hub_keyfile")
    monkeypatch.setattr(crypto, "KEYFILE_PATH", keyfile)
    crypto.reset_cache()
    yield
    crypto.reset_cache()


async def _make_client(db, name="Guest", *, is_guest=True, expires_at=None):
    return await add_client(
        db,
        name=name,
        secret_hash="h" * 64,
        last_seen=Database.now(),
        guest=is_guest,
        expires_at=expires_at,
    )


class TestGuestFailsClosed:
    async def test_pending_guest_gets_no_ha_token(self, db):
        guest_id = await _make_client(db)
        row = await db.fetchone(
            "SELECT ha_token_status FROM paired_clients WHERE id = ?", (guest_id,)
        )
        assert row["ha_token_status"] == "pending"
        cfg = await _get_client_config(db, guest_id)
        assert cfg["ha_token"] is None

    async def test_shared_guest_token_is_never_used(self, db):
        """A leftover guest_ha_token setting is ignored."""
        await db.set_setting("guest_ha_token", "legacy-shared-token")
        guest_id = await _make_client(db)
        cfg = await _get_client_config(db, guest_id)
        assert cfg["ha_token"] is None

    async def test_guest_gets_the_token_of_its_own_ha_user(self, db):
        guest_id = await _make_client(db)
        await db.store_client_token(
            guest_id, ha_user_id="u-guest", refresh_token_id="rt-g", token="guest-tok"
        )
        # The guest default profile is Hub only: no key on the device.
        cfg = await _get_client_config(db, guest_id)
        assert cfg["ha_token"] is None and cfg["ha_enforcement"] is None
        # On Checked the guest gets the token of its own HA user.
        async with db.transaction():
            await db.execute(
                "UPDATE profiles SET body_json = json_set(body_json, "
                "'$.rules.control_path', 'checked') WHERE id = "
                "(SELECT profile_id FROM paired_clients WHERE id = ?)",
                (guest_id,),
            )
        cfg = await _get_client_config(db, guest_id)
        assert cfg["ha_token"] == "guest-tok"

    async def test_regular_client_gets_its_delivered_token(self, db):
        client_id = await _make_client(db, "Tablet", is_guest=False)
        await db.store_client_token(
            client_id, ha_user_id="u-tab", refresh_token_id="rt-t", token="tab-tok"
        )
        cfg = await _get_client_config(db, client_id)
        assert cfg["ha_token"] == "tab-tok"
