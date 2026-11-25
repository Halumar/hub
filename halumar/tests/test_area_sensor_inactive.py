"""Inactive ("hidden") areas have no per-area sensor.

sensor.halumar_area_<slug> exposes the full layout via the states API, so only
active areas get one. Contract pinned here:

- sync_area_sensor on an inactive area DELETEs the sensor instead of posting.
- sync_all_sensors therefore only creates sensors for active areas (and cleans
  up sensors of areas deactivated while the hub was down).
- Toggling the HA input_boolean switch off (poll_area_switches) deletes the
  sensor; toggling it back on recreates it.
- delete_area_sensor uses DELETE /core/api/states/<id> (real removal).

The input_boolean activation switch intentionally KEEPS existing for inactive
areas — it is the way to re-activate them from HA.
"""

import uuid

from models.database import Database
from services.ha_sensors import (
    _area_slug,
    delete_area_sensor,
    poll_area_switches,
    sync_all_sensors,
    sync_area_sensor,
)


class RecordingHAClient:
    """Records _post/_delete calls; get_state serves scripted switch states."""

    def __init__(self, switch_states=None):
        self.posts = []
        self.deletes = []
        # entity_id -> state string served by get_state ("on"/"off")
        self.switch_states = switch_states or {}

    async def _post(self, url, payload):
        self.posts.append((url, payload))

    async def _delete(self, url):
        self.deletes.append(url)

    async def get_state(self, entity_id):
        state = self.switch_states.get(entity_id)
        return {"state": state} if state is not None else None


async def _make_area(db, name, *, is_active=True):
    """Insert an area (with one section + entity); returns area_id."""
    area_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (area_id, name, 1 if is_active else 0, now, now),
    )
    section_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, columns) "
        "VALUES (?, ?, 'S', 0, 1, 2)",
        (section_id, area_id),
    )
    await db.execute(
        "INSERT INTO area_entities (id, area_id, entity_id, section_id, sort_order, created_at) "
        "VALUES (?, ?, 'light.test', ?, 0, ?)",
        (str(uuid.uuid4()), area_id, section_id, now),
    )
    await db.commit()
    return area_id


def _sensor_url(name, area_id):
    return (
        "http://supervisor/core/api/states/"
        f"sensor.halumar_area_{_area_slug(name, area_id)}"
    )


class TestSyncAreaSensorInactive:
    async def test_active_area_posts_sensor(self, db):
        area_id = await _make_area(db, "Living", is_active=True)
        ha = RecordingHAClient()
        await sync_area_sensor(db, ha, area_id)

        assert [u for u, _ in ha.posts] == [_sensor_url("Living", area_id)]
        assert ha.deletes == []

    async def test_inactive_area_deletes_sensor_and_posts_nothing(self, db):
        area_id = await _make_area(db, "Hidden", is_active=False)
        ha = RecordingHAClient()
        await sync_area_sensor(db, ha, area_id)

        assert ha.posts == []
        assert ha.deletes == [_sensor_url("Hidden", area_id)]

    async def test_inactive_area_layout_not_in_any_post(self, db):
        """Even indirectly, no payload may carry the hidden area's layout."""
        area_id = await _make_area(db, "Vault", is_active=False)
        ha = RecordingHAClient()
        await sync_area_sensor(db, ha, area_id)
        assert "Vault" not in str(ha.posts)


class TestSyncAllSensorsSkipsInactive:
    async def test_only_active_areas_get_sensors(self, db):
        active_id = await _make_area(db, "Active", is_active=True)
        inactive_id = await _make_area(db, "Inactive", is_active=False)

        ha = RecordingHAClient()
        await sync_all_sensors(db, ha)

        posted = [u for u, _ in ha.posts]
        assert _sensor_url("Active", active_id) in posted
        assert _sensor_url("Inactive", inactive_id) not in posted
        # The lingering sensor of the inactive area is cleaned up.
        assert _sensor_url("Inactive", inactive_id) in ha.deletes
        # The activation SWITCH still exists for BOTH areas (re-activation path).
        switch_posts = [u for u in posted if "input_boolean.halumar_area_" in u]
        assert len(switch_posts) == 2

    async def test_config_sensor_omits_inactive_area(self, db):
        await _make_area(db, "Shown", is_active=True)
        await _make_area(db, "NotShown", is_active=False)

        ha = RecordingHAClient()
        await sync_all_sensors(db, ha)

        config_posts = [
            p for u, p in ha.posts if u.endswith("/states/sensor.halumar_config")
        ]
        assert len(config_posts) == 1
        names = [a["name"] for a in config_posts[0]["attributes"]["areas"]]
        assert names == ["Shown"]


class TestSwitchToggleDeletesSensor:
    async def test_toggle_off_deletes_sensor(self, db):
        area_id = await _make_area(db, "Den", is_active=True)
        slug = _area_slug("Den", area_id)
        ha = RecordingHAClient(
            switch_states={f"input_boolean.halumar_area_{slug}": "off"}
        )

        await poll_area_switches(db, ha)

        row = await db.fetchone("SELECT is_active FROM areas WHERE id = ?", (area_id,))
        assert row["is_active"] == 0
        assert _sensor_url("Den", area_id) in ha.deletes

    async def test_toggle_on_recreates_sensor(self, db):
        area_id = await _make_area(db, "Den", is_active=False)
        slug = _area_slug("Den", area_id)
        ha = RecordingHAClient(
            switch_states={f"input_boolean.halumar_area_{slug}": "on"}
        )

        await poll_area_switches(db, ha)

        row = await db.fetchone("SELECT is_active FROM areas WHERE id = ?", (area_id,))
        assert row["is_active"] == 1
        assert _sensor_url("Den", area_id) in [u for u, _ in ha.posts]


class TestDeleteAreaSensor:
    async def test_uses_states_delete(self, db):
        ha = RecordingHAClient()
        await delete_area_sensor(ha, "Gone", "abc12345-6789")
        assert ha.deletes == [_sensor_url("Gone", "abc12345-6789")]
        assert ha.posts == []
