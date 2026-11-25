"""The config sensor (sensor.halumar_config) is the client's OFFLINE layout
source. It must project only ACTIVE areas — exactly like the WS path
(_get_areas, WHERE is_active=1) — so a deactivated ("hidden") area does NOT
reappear in the app's sensor fallback when the hub is stopped.
"""

import uuid

from models.database import Database
from services.ha_sensors import sync_config_sensor


class FakeHAClient:
    """Captures the payload POSTed to the HA states API."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []

    async def _post(self, url, payload=None):
        self.posts.append((url, payload))
        return {}


async def _add_area(db, name: str, is_active: bool) -> str:
    aid = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (aid, name, 1 if is_active else 0, now, now),
    )
    await db.commit()
    return aid


class TestConfigSensorActiveOnly:
    async def test_deactivated_area_excluded_from_projection(self, db):
        await _add_area(db, "Visible", is_active=True)
        await _add_area(db, "Hidden", is_active=False)

        ha = FakeHAClient()
        await sync_config_sensor(db, ha)

        cfg = [p for (u, p) in ha.posts if u.endswith("sensor.halumar_config")]
        assert cfg, "config sensor was not synced"
        areas = cfg[-1]["attributes"]["areas"]
        names = {a["name"] for a in areas}

        assert "Visible" in names
        assert "Hidden" not in names  # a deactivated area is not listed
        # State (count) must reflect active areas only.
        assert cfg[-1]["state"] == "1"

    async def test_all_active_areas_present(self, db):
        await _add_area(db, "A", is_active=True)
        await _add_area(db, "B", is_active=True)
        ha = FakeHAClient()
        await sync_config_sensor(db, ha)
        cfg = [p for (u, p) in ha.posts if u.endswith("sensor.halumar_config")][-1]
        assert {a["name"] for a in cfg["attributes"]["areas"]} == {"A", "B"}
        assert cfg["state"] == "2"
