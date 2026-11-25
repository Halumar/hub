"""Sensors come back after an HA Core restart.

States-API entities (sensor.halumar_*, input_boolean.halumar_*) are not
persisted by HA and are gone after `ha core restart`. The poll loop checks
sensor.halumar_config and runs a full sync when it is missing (404),
debounced, and leaves HA alone while it is unreachable; a startup sync
before HA is ready is retried the same way.
"""

import httpx
import pytest

import services.ha_sensors as ha_sensors
from services.ha_sensors import CONFIG_SENSOR_ID, resync_if_missing


class FakeHA:
    """Minimal HAClient stand-in that keeps posted states like HA does."""

    def __init__(self):
        self.states: dict[str, dict] = {}
        self.down = False
        self.posts: list[str] = []

    def invalidate_state_cache(self, entity_id=None):
        pass

    async def get_state(self, entity_id):
        if self.down:
            raise httpx.ConnectError("HA restarting")
        return self.states.get(entity_id)

    async def _post(self, url, data=None):
        if self.down:
            raise httpx.ConnectError("HA restarting")
        entity_id = url.rsplit("/", 1)[-1]
        self.posts.append(entity_id)
        self.states[entity_id] = data
        return data

    async def _delete(self, url):
        self.states.pop(url.rsplit("/", 1)[-1], None)

    def restart_core(self):
        """HA Core restart: every states-API entity is gone."""
        self.states.clear()


@pytest.fixture(autouse=True)
def reset_debounce(monkeypatch):
    monkeypatch.setattr(ha_sensors, "_last_resync_attempt", None)


async def _seed_area(db):
    from models.database import Database

    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, is_active, created_at, updated_at) "
        "VALUES ('a1', 'Wohnzimmer', 1, ?, ?)",
        (now, now),
    )
    await db.commit()


async def test_core_restart_triggers_full_resync(db):
    await _seed_area(db)
    ha = FakeHA()
    await ha_sensors.sync_all_sensors(db, ha)
    before = set(ha.states)
    assert CONFIG_SENSOR_ID in before
    assert any(e.startswith("input_boolean.halumar_area_") for e in before)

    # Sensors present: nothing to do.
    assert await resync_if_missing(db, ha) is False

    ha.restart_core()
    assert await resync_if_missing(db, ha) is True
    assert set(ha.states) == before


async def test_ha_unreachable_does_nothing(db):
    ha = FakeHA()
    ha.down = True
    assert await resync_if_missing(db, ha) is False
    assert ha.posts == []


async def test_boot_before_ha_ready_is_retried_with_debounce(db, monkeypatch):
    await _seed_area(db)
    ha = FakeHA()
    # Startup sync while HA is down fails silently (sync_* swallow errors).
    ha.down = True
    await ha_sensors.sync_all_sensors(db, ha)
    assert CONFIG_SENSOR_ID not in ha.states

    ha.down = False
    clock = {"t": 1000.0}

    class _Loop:
        def time(self):
            return clock["t"]

    monkeypatch.setattr(ha_sensors.asyncio, "get_running_loop", lambda: _Loop())

    # First cycle: resync. Pretend it failed again (states wiped) -> the next
    # cycles within the debounce window do not hammer HA ...
    assert await resync_if_missing(db, ha) is True
    ha.restart_core()
    clock["t"] += 5
    assert await resync_if_missing(db, ha) is False
    # ... but after the debounce window the retry happens.
    clock["t"] += ha_sensors.RESYNC_DEBOUNCE_SECONDS
    assert await resync_if_missing(db, ha) is True
    assert CONFIG_SENSOR_ID in ha.states
