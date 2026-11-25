"""Hub identity, access fields of client_config and layout span bounds.

- the integration status names this hub (``hub.instance_id``): the
  same stable id as Bonjour and onboarding, so the integration can tell a
  different hub from this one before it reconciles anything;
- a device's own ``client_config.access.set_up_by`` carries only name and
  method, never the client_id of the device that set it up; the
  admin's view keeps the id;
- ``client_config.access.time_zone`` is the hub's zone for the access-hour
  edges, ``UTC`` while none or an unknown one is known;
- the layout batch takes 1-2 spans with plain field bounds.
"""

import time
import types

import pytest
from pydantic import ValidationError

from api import integration as apiintg
from api.pairing import clients as pairing_clients
from api.ws import onboarding as ws_onboarding
from api.ws.client import _get_client_config
from models.schemas import SectionLayoutItem
from services import kids_mode, mdns

from .profile_helpers import add_client

MANAGER = "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b"
_EVENINGS = {
    "enabled": True,
    "windows": [{"days": ["mon", "tue"], "start": "18:00", "end": "21:00"}],
}


def _integration_request(db):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
    )


@pytest.fixture
def zone(monkeypatch):
    def set_zone(name):
        monkeypatch.setattr(kids_mode, "_tz_cache", (time.monotonic(), name))
        monkeypatch.setattr(kids_mode, "_tz_last_known", None)

    return set_zone


async def test_status_names_this_hub_with_its_stable_instance_id(db):
    first = await apiintg.integration_status(_integration_request(db))
    second = await apiintg.integration_status(_integration_request(db))
    iid = first["hub"]["instance_id"]
    assert iid == second["hub"]["instance_id"]
    # One identity: Bonjour TXT iid and the hub_id of onboarding.
    assert iid == mdns.load_instance_id(mdns.INSTANCE_ID_PATH)
    assert iid == ws_onboarding.own_hub_id()


async def test_device_sees_who_set_it_up_without_that_devices_id(db, zone):
    zone("Europe/Zurich")
    await add_client(db, client_id=MANAGER, name="iPhone Papa", platform="ios")
    child = await add_client(
        db,
        name="Child iPad",
        platform="ipados",
        profile="child",
        set_up_by_client_id=MANAGER,
        set_up_by_name="iPhone Papa",
        set_up_method="onboard",
    )
    cfg = await _get_client_config(db, child)
    assert cfg["access"]["set_up_by"] == {"name": "iPhone Papa", "method": "onboard"}
    assert MANAGER not in str(cfg)
    # The admin still sees which device it was.
    view = await pairing_clients.client_view(db, child)
    assert view.set_up_by["client_id"] == MANAGER


async def test_client_effective_view_carries_the_device_shape(db, zone):
    from services.profiles import store

    zone("Europe/Zurich")
    child = await add_client(
        db,
        name="Child iPad",
        platform="ipados",
        set_up_by_client_id=MANAGER,
        set_up_by_name="iPhone Papa",
        set_up_method="onboard",
    )
    effective = await store.effective_view(db, child)
    assert effective["access"]["set_up_by"] == {
        "name": "iPhone Papa",
        "method": "onboard",
    }
    assert effective["access"]["time_zone"] == "Europe/Zurich"


@pytest.mark.parametrize(
    ("ha_zone", "sent"),
    [
        ("Europe/Zurich", "Europe/Zurich"),
        ("America/New_York", "America/New_York"),
        ("Mars/Olympus_Mons", "UTC"),
        (None, "UTC"),
    ],
)
async def test_every_device_gets_the_hub_time_zone(db, zone, ha_zone, sent):
    zone(ha_zone)
    phone = await add_client(db, name="Phone", platform="ios")
    sitter = await add_client(
        db, name="Sitter", platform="ios", overrides={"access_hours": _EVENINGS}
    )
    for cid in (phone, sitter, "ingress"):
        cfg = await _get_client_config(db, cid)
        assert cfg["access"]["time_zone"] == sent, cid


def test_layout_spans_are_plain_field_bounds():
    for bad in (0, 3):
        with pytest.raises(ValidationError):
            SectionLayoutItem(section_id="s", sort_order=0, columns=bad)
        with pytest.raises(ValidationError):
            SectionLayoutItem(section_id="s", sort_order=0, rows=bad)
    item = SectionLayoutItem(section_id="s", sort_order=0, columns=2, rows=1)
    assert (item.columns, item.rows) == (2, 1)
    spans = SectionLayoutItem.model_json_schema()["properties"]
    for field in ("columns", "rows"):
        bounded = [s for s in spans[field]["anyOf"] if s.get("type") == "integer"]
        assert bounded == [{"type": "integer", "minimum": 1, "maximum": 2}]
