"""Unencrypted Home Assistant at home.

The integration reports Home Assistant's instance ID with ``POST
/api/integration/ha-url``; the hub hands it to the devices as
``client_config.ha_instance_id`` (second proof of the home network over
Bonjour). Profile rule ``connection_insecure_http`` offers the switch
"Always allow unencrypted (less secure)": adults yes, TV and wall panel
fixed off, guests and parental controls capped off.
"""

import pytest

import api.integration as apiintg
import api.websocket as ws
from models.schemas import HAUrlReport

from .profile_helpers import add_client
from .test_remote_access import _FakeHA, _request, isolated_state  # noqa: F401

HA_ID = "c4680ececb2b4cbfae862aca0d7bc0a8"


@pytest.fixture
def app_messages(monkeypatch):
    messages: list[dict] = []

    async def fake_broadcast(message, *_a, **_k):
        messages.append(message)

    monkeypatch.setattr(apiintg.api_ws, "broadcast", fake_broadcast)
    return messages


class TestInstanceIdReport:
    async def test_stored_and_devices_are_told(self, db, app_messages):
        request = _request(db, "integration", _FakeHA())
        result = await apiintg.integration_ha_url(
            request, HAUrlReport(external_url=None, ha_instance_id=HA_ID)
        )
        assert result.changed is True
        assert await db.get_setting("ha_instance_id") == HA_ID
        assert {"type": "client_config_updated", "data": {}} in app_messages

        app_messages.clear()
        again = await apiintg.integration_ha_url(
            request, HAUrlReport(external_url=None, ha_instance_id=HA_ID)
        )
        assert again.changed is False
        assert app_messages == []

    async def test_missing_or_null_keeps_the_stored_id(self, db, app_messages):
        await db.set_setting("ha_instance_id", HA_ID)
        request = _request(db, "integration", _FakeHA())
        await apiintg.integration_ha_url(request, HAUrlReport(external_url=None))
        await apiintg.integration_ha_url(
            request, HAUrlReport(external_url=None, ha_instance_id=None)
        )
        assert await db.get_setting("ha_instance_id") == HA_ID

    async def test_stored_even_when_the_url_is_refused(self, db, app_messages):
        """The ID does not depend on the URL check (422 url_not_allowed)."""
        request = _request(
            db, "integration", _FakeHA(external_url="https://ha.example.com")
        )
        with pytest.raises(Exception) as exc:
            await apiintg.integration_ha_url(
                request,
                HAUrlReport(
                    external_url="https://attacker.example.com", ha_instance_id=HA_ID
                ),
            )
        assert getattr(exc.value, "status_code", None) == 422
        assert await db.get_setting("ha_instance_id") == HA_ID
        assert await db.get_setting("ha_url") is None
        assert {"type": "client_config_updated", "data": {}} in app_messages

    @pytest.mark.parametrize(
        "value", ["C4680ECECB2B4CBFAE862ACA0D7BC0A8", "c4680ece-cb2b", "x" * 32, ""]
    )
    def test_only_32_lowercase_hex(self, value):
        with pytest.raises(Exception):
            HAUrlReport(external_url=None, ha_instance_id=value)


class TestClientConfig:
    async def test_members_get_the_id_children_do_not(self, db):
        await db.set_setting("ha_instance_id", HA_ID)
        member = await add_client(
            db, name="TEST-HTTP Phone", secret_hash="a" * 64, platform="ios"
        )
        guest = await add_client(
            db, name="TEST-HTTP Guest", secret_hash="b" * 64, guest=True
        )
        child = await add_client(
            db,
            name="TEST-HTTP Kid",
            secret_hash="c" * 64,
            platform="ios",
            profile="child",
        )
        assert (await ws._get_client_config(db, member))["ha_instance_id"] == HA_ID
        # Guests and children are on Hub only by default: no key, no id.
        assert (await ws._get_client_config(db, guest))["ha_instance_id"] is None
        assert (await ws._get_client_config(db, child))["ha_instance_id"] is None

    async def test_unknown_id_is_null(self, db):
        member = await add_client(
            db, name="TEST-HTTP Phone", secret_hash="a" * 64, platform="ios"
        )
        assert (await ws._get_client_config(db, member))["ha_instance_id"] is None


class TestRule:
    @staticmethod
    async def _rule(db, **kwargs):
        client = await add_client(db, secret_hash="d" * 64, **kwargs)
        return (await ws._get_client_config(db, client))["rules"].get(
            "connection_insecure_http"
        )

    async def test_adult_offers_the_switch(self, db):
        rule = await self._rule(
            db, name="TEST-HTTP Adult", platform="ios", profile="adult"
        )
        assert rule["value"] is True and rule["fixed"] is False

    async def test_apple_tv_and_wall_panel_fixed_off(self, db):
        for platform, profile in (("tvos", "apple_tv"), ("ipados", "wall_panel")):
            rule = await self._rule(
                db, name=f"TEST-HTTP {platform}", platform=platform, profile=profile
            )
            assert rule["value"] is False and rule["fixed"] is True, platform

    async def test_guest_capped_off_even_with_an_adjustment(self, db):
        rule = await self._rule(
            db,
            name="TEST-HTTP Guest",
            guest=True,
            overrides={"connection_insecure_http": True},
        )
        assert rule["value"] is False and rule["reason"] == "boundary_guest"

    async def test_child_capped_off(self, db):
        rule = await self._rule(
            db,
            name="TEST-HTTP Kid",
            platform="ios",
            profile="child",
            overrides={"connection_insecure_http": True},
        )
        assert rule["value"] is False and rule["reason"] == "boundary_child_protection"

    async def test_admin_turns_it_off_per_device(self, db):
        rule = await self._rule(
            db,
            name="TEST-HTTP Adult",
            platform="ios",
            profile="adult",
            overrides={"connection_insecure_http": False},
        )
        assert rule["value"] is False and rule["source"] == "device"

    async def test_android_counterparts(self, db):
        rule = await self._rule(
            db, name="TEST-HTTP Android", platform="android", profile="adult"
        )
        assert rule["value"] is True
        tv = await self._rule(
            db, name="TEST-HTTP Google TV", platform="android_tv", profile="apple_tv"
        )
        assert tv["value"] is False and tv["fixed"] is True
