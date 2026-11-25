"""Robustness of the hub.

- services.tasks: supervised loops restart after a crash, shutdown is bounded;
- services.ha_ws: persistent HA connection with reconnect + backoff, state
  mirror, registry cache with invalidation by events;
- services.ha_sensors: echo guard for the area switch, writes
  only on change, live resync after an HA restart,
  orphan clean-up and rename;
- api.ws send queues: overflow and failed sends CLOSE the connection,
  a slow client does not hold up the others, the ping loop
  survives errors;
- discovery: the new integration token only replaces the current one after
  a successful announce;
- lifespan: start < 2 s without HA, clean shutdown.
"""

import asyncio
import json
import time
import types
import uuid

import pytest

from .profile_helpers import add_client

import namespace
from api import ws as api_ws
from api.ws import registry as ws_registry
from models.database import Database
from services import ha_sensors, tasks
from services.ha_sensors import _area_slug
from services.ha_ws import RECONNECT_MAX_SECONDS as HA_RECONNECT_MAX
from services.ha_ws import HAAuthError, HAUnavailable, HAWebSocket


# ── Task registry ────────────────────────────────────────────────────────────


class TestTaskRegistry:
    async def test_supervised_loop_restarts_after_crash(self):
        reg = tasks.TaskRegistry()
        runs = []

        async def loop():
            runs.append(1)
            if len(runs) < 3:
                raise RuntimeError("boom")
            await asyncio.sleep(3600)

        reg.supervise("crashy", loop, restart_initial=0.01, restart_max=0.02)
        for _ in range(100):
            if len(runs) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(runs) == 3
        assert reg.names() == ["crashy"]
        assert await reg.shutdown(1.0) == []
        assert reg.names() == []

    async def test_shutdown_is_bounded_and_reports_stuck_tasks(self):
        reg = tasks.TaskRegistry()

        async def stubborn():
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.5)  # ignores the first cancel a while

        reg.spawn(stubborn(), name="stubborn")
        await asyncio.sleep(0)
        t0 = time.monotonic()
        stuck = await reg.shutdown(0.05)
        assert stuck == ["stubborn"]
        assert time.monotonic() - t0 < 0.4
        assert reg.closing is False

    async def test_spawn_keeps_reference_and_logs_failure(self, caplog):
        reg = tasks.TaskRegistry()

        async def fail():
            raise ValueError("lost revoke")

        task = reg.spawn(fail(), name="revoke")
        with pytest.raises(ValueError):
            await task
        await asyncio.sleep(0)
        assert "Background task revoke failed" in caplog.text

    async def test_no_new_tasks_during_shutdown(self):
        reg = tasks.TaskRegistry()
        reg._closing = True

        async def noop():
            return None

        with pytest.raises(RuntimeError):
            reg.spawn(noop(), name="late")

    def test_backoff_has_jitter_and_cap(self):
        values = [tasks.backoff_delay(10, 1.0, 15.0) for _ in range(200)]
        assert all(7.5 <= v <= 15.0 for v in values)
        assert all(
            2.5 <= tasks.backoff_delay(10, 1.0, HA_RECONNECT_MAX) <= 5.0
            for _ in range(50)
        )
        assert len({round(v, 3) for v in values}) > 50
        assert 0.5 <= tasks.backoff_delay(0, 1.0, 15.0) <= 1.0
        assert 2.0 <= tasks.backoff_delay(2, 1.0, 15.0) <= 4.0


# ── Fake HA WebSocket server ─────────────────────────────────────────────────


class FakeConn:
    """One HA WebSocket connection driven by FakeHA."""

    def __init__(self, ha: "FakeHA"):
        self.ha = ha
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False
        self.inbox.put_nowait({"type": "auth_required"})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def recv(self):
        msg = await self.inbox.get()
        if msg is None:
            raise ConnectionError("closed")
        return json.dumps(msg)

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = await self.inbox.get()
        if msg is None:
            raise StopAsyncIteration
        return json.dumps(msg)

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("type") == "auth":
            ok = msg.get("access_token") == self.ha.token
            self.inbox.put_nowait(
                {"type": "auth_ok", "ha_version": "2026.11.2"}
                if ok
                else {"type": "auth_invalid", "message": "Invalid access token"}
            )
            return
        mid = msg.get("id")
        t = msg.get("type")
        if t == "subscribe_events":
            self.ha.subscriptions[msg["event_type"]] = (self, mid)
            result = None
        elif t == "get_states":
            result = list(self.ha.states.values())
        elif t.startswith("config/") and t.endswith("_registry/list"):
            kind = t.split("/")[1].removesuffix("_registry")
            self.ha.registry_calls.append(kind)
            result = list(self.ha.registries.get(kind, []))
        elif t in self.ha.hang:
            return
        else:
            result = {"echo": t}
        self.inbox.put_nowait(
            {"id": mid, "type": "result", "success": True, "result": result}
        )

    def drop(self):
        self.inbox.put_nowait(None)


class FakeHA:
    def __init__(self, token="tok"):
        self.token = token
        self.states: dict[str, dict] = {}
        self.registries: dict[str, list] = {"area": [], "device": [], "entity": []}
        self.registry_calls: list[str] = []
        self.subscriptions: dict[str, tuple[FakeConn, int]] = {}
        self.conns: list[FakeConn] = []
        self.refuse = 0
        self.hang: set[str] = set()

    def connect(self, url):
        if self.refuse > 0:
            self.refuse -= 1
            raise OSError("502 Bad Gateway (Core restarting)")
        conn = FakeConn(self)
        self.conns.append(conn)
        return conn

    def fire(self, event_type, data):
        conn, mid = self.subscriptions[event_type]
        conn.inbox.put_nowait(
            {
                "id": mid,
                "type": "event",
                "event": {"event_type": event_type, "data": data},
            }
        )

    def set_state(
        self, entity_id, state, attributes=None, ts="2026-11-19T10:00:00+00:00"
    ):
        old = self.states.get(entity_id)
        new = {
            "entity_id": entity_id,
            "state": state,
            "attributes": attributes or {},
            "last_updated": ts,
        }
        self.states[entity_id] = new
        if "state_changed" in self.subscriptions:
            self.fire(
                "state_changed",
                {"entity_id": entity_id, "old_state": old, "new_state": new},
            )
        return new

    def restart_core(self):
        """Core restart: connection drops, states-API entities are gone."""
        self.states = {
            k: v
            for k, v in self.states.items()
            if not k.split(".", 1)[1].startswith("halumar_")
        }
        self.subscriptions.clear()
        for conn in self.conns:
            if not conn.closed:
                conn.drop()


async def _until(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


@pytest.fixture
async def registry():
    tasks.registry.reset()
    yield tasks.registry
    await tasks.registry.shutdown(1.0)


def _ws(ha: FakeHA, **kw) -> HAWebSocket:
    return HAWebSocket(
        ha.token,
        connect=ha.connect,
        reconnect_initial=kw.pop("initial", 0.01),
        reconnect_max=kw.pop("cap", 0.05),
        **kw,
    )


class TestHAWebSocket:
    async def test_connect_subscribes_and_mirrors_states(self, registry):
        ha = FakeHA()
        ha.set_state("light.a", "on")
        conn = _ws(ha)
        registry.supervise("ha", conn.run)
        assert await conn.wait_connected(2)
        assert conn.states_ready
        assert set(ha.subscriptions) == {
            "state_changed",
            "homeassistant_started",
            "area_registry_updated",
            "device_registry_updated",
            "entity_registry_updated",
            "call_service",
            "mobile_app_notification_action",
        }
        assert conn.get_state("light.a")["state"] == "on"

        ha.set_state("light.a", "off", ts="2026-11-19T10:00:01+00:00")
        assert await _until(lambda: conn.get_state("light.a")["state"] == "off")
        # An older snapshot never overwrites a newer state.
        conn.apply_state(
            {
                "entity_id": "light.a",
                "state": "on",
                "last_updated": "2026-11-19T09:00:00+00:00",
            }
        )
        assert conn.get_state("light.a")["state"] == "off"
        # Removed entity
        ha.fire("state_changed", {"entity_id": "light.a", "new_state": None})
        assert await _until(lambda: not conn.has_state("light.a"))

    async def test_reconnects_with_backoff_after_refusals(self, registry, monkeypatch):
        ha = FakeHA()
        ha.refuse = 3
        delays = []
        real_backoff = tasks.backoff_delay

        def recording(attempt, initial, cap):
            value = real_backoff(attempt, initial, cap)
            delays.append((attempt, value))
            return value

        monkeypatch.setattr("services.ha_ws.backoff_delay", recording)
        conn = _ws(ha, initial=0.01, cap=0.04)
        registry.supervise("ha", conn.run)
        assert await conn.wait_connected(2)
        assert conn.attempts == 4 and conn.connects == 1
        assert [a for a, _ in delays] == [0, 1, 2]
        assert all(v <= 0.04 for _, v in delays)

    async def test_core_restart_reconnects_and_calls_listeners(self, registry):
        ha = FakeHA()
        conn = _ws(ha)
        connects = []

        async def on_connect():
            connects.append(conn.connects)

        conn.add_connect_listener(on_connect)
        registry.supervise("ha", conn.run)
        assert await conn.wait_connected(2)
        assert await _until(lambda: connects == [1])

        ha.refuse = 2
        ha.restart_core()
        assert await _until(lambda: not conn.connected)
        assert await _until(lambda: connects == [1, 2])
        assert conn.connected and conn.states_ready
        assert len(ha.conns) == 2

    async def test_pending_command_fails_when_connection_drops(self, registry):
        ha = FakeHA()
        ha.hang.add("slow/command")
        conn = _ws(ha)
        registry.supervise("ha", conn.run)
        assert await conn.wait_connected(2)
        task = asyncio.ensure_future(conn.command({"type": "slow/command"}))
        await asyncio.sleep(0.01)
        ha.conns[0].drop()
        with pytest.raises(HAUnavailable):
            await asyncio.wait_for(task, 1)
        # After the reconnect commands work again.
        assert await conn.wait_connected(2)
        assert await conn.command({"type": "x"}) == {"echo": "x"}

    async def test_command_without_connection_fails_fast(self):
        conn = HAWebSocket("tok", connect=FakeHA().connect)
        with pytest.raises(HAUnavailable):
            await conn.command({"type": "x"})

    async def test_registry_cache_invalidated_by_event(self, registry):
        ha = FakeHA()
        ha.registries["area"] = [{"area_id": "kitchen"}]
        conn = _ws(ha)
        registry.supervise("ha", conn.run)
        assert await conn.wait_connected(2)
        assert await conn.registry("area") == [{"area_id": "kitchen"}]
        assert await conn.registry("area") == [{"area_id": "kitchen"}]
        assert ha.registry_calls == ["area"]

        ha.registries["area"] = [{"area_id": "kitchen"}, {"area_id": "garden"}]
        ha.fire("area_registry_updated", {"action": "create", "area_id": "garden"})
        assert await _until(lambda: conn._registry["area"] is None)
        assert len(await conn.registry("area")) == 2
        assert ha.registry_calls == ["area", "area"]

    async def test_invalid_token_keeps_retrying_quietly(self, registry, caplog):
        ha = FakeHA(token="right")
        conn = HAWebSocket(
            "wrong", connect=ha.connect, reconnect_initial=0.01, reconnect_max=0.02
        )
        registry.supervise("ha", conn.run)
        assert await _until(lambda: conn.attempts >= 3)
        assert not conn.connected
        assert "rejected the hub's token" in caplog.text
        assert isinstance(HAAuthError("x"), RuntimeError)


# ── HAClient on top of the live connection ───────────────────────────────────


class TestHAClientMirror:
    async def test_reads_come_from_the_mirror(self, registry):
        from services.ha_client import HAClient

        ha = FakeHA()
        for i in range(50):
            ha.set_state(f"light.l{i}", "on")
        conn = _ws(ha)
        client = HAClient("tok")
        client.attach_ws(conn)
        registry.supervise("ha", conn.run)
        assert await conn.wait_connected(2)

        async def no_http(url):
            raise AssertionError(f"HTTP call {url}")

        client._get = no_http
        t0 = time.perf_counter()
        states = [await client.get_state(f"light.l{i}") for i in range(50)]
        assert time.perf_counter() - t0 < 0.05
        assert all(s["state"] == "on" for s in states)
        assert await client.get_state("light.missing") is None
        assert len(await client.get_states()) == 50
        assert client.state_known("light.l1") is True
        assert client.state_known("light.missing") is False

        assert await client.get_areas() == []
        await client.get_areas()
        assert ha.registry_calls == ["area"]
        await client.update_area("kitchen", name="Küche")
        await client.get_areas()
        assert ha.registry_calls == ["area", "area"]
        await client.close()


# ── Sensors: echo guard, change detection, live resync, orphans ──────────────


class StatesHA:
    """HAClient stand-in with a states store, optionally a live mirror."""

    def __init__(self, mirror=True):
        self.states: dict[str, dict] = {}
        self.posts: list[str] = []
        self.deletes: list[str] = []
        self.registry: list[dict] = []
        self.mirror = mirror
        self.ws = self if mirror else None
        self.connected = True

    # mirror surface (HAClient + HAWebSocket)
    @property
    def mirror_ready(self):
        return self.mirror

    def state_known(self, entity_id):
        return (entity_id in self.states) if self.mirror else None

    def entity_ids(self):
        return list(self.states)

    def invalidate_state_cache(self, entity_id=None):
        pass

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def get_entity_registry(self):
        return self.registry

    async def _post(self, url, data=None):
        entity_id = url.rsplit("/", 1)[-1]
        self.posts.append(entity_id)
        self.states[entity_id] = {"entity_id": entity_id, **data}
        return self.states[entity_id]

    async def _delete(self, url):
        entity_id = url.rsplit("/", 1)[-1]
        self.deletes.append(entity_id)
        self.states.pop(entity_id, None)


async def _area(db, name="Wohnzimmer", active=True):
    area_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, is_active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (area_id, name, 1 if active else 0, now, now),
    )
    await db.commit()
    return area_id


def _switch(name, area_id):
    return namespace.area_switch_id(_area_slug(name, area_id))


async def _is_active(db, area_id):
    row = await db.fetchone("SELECT is_active FROM areas WHERE id = ?", (area_id,))
    return bool(row["is_active"])


class TestAreaSwitchEchoGuard:
    async def test_own_echo_never_flips_the_area_back(self, db):
        """Admin hides the area; the late echo of the hub's earlier
        "on" must not re-activate it."""
        area_id = await _area(db)
        ha = StatesHA()
        await ha_sensors.sync_area_switch(db, ha, area_id)
        eid = _switch("Wohnzimmer", area_id)
        stale_on = dict(ha.states[eid])

        await db.execute("UPDATE areas SET is_active = 0 WHERE id = ?", (area_id,))
        await db.commit()
        # Echo of the old write arrives before the new switch state is written.
        assert await ha_sensors.handle_area_switch_state(db, ha, stale_on) is False
        assert await _is_active(db, area_id) is False

        await ha_sensors.sync_area_switch(db, ha, area_id)
        assert ha.states[eid]["state"] == "off"
        # Echo of the new write: nothing to do.
        assert (
            await ha_sensors.handle_area_switch_state(db, ha, ha.states[eid]) is False
        )

    async def test_change_in_ha_applies(self, db, monkeypatch):
        area_id = await _area(db)
        ha = StatesHA()
        await ha_sensors.sync_all_sensors(db, ha)
        eid = _switch("Wohnzimmer", area_id)
        sent = []

        async def fake_announce(_db, announced_id, is_active):
            sent.append((announced_id, is_active))

        # Area-scoped announcement, as for the integration switch.
        monkeypatch.setattr(api_ws, "announce_area_active", fake_announce)
        changed = {**ha.states[eid], "state": "off"}
        assert await ha_sensors.handle_area_switch_state(db, ha, changed) is True
        assert await _is_active(db, area_id) is False
        assert sent == [(area_id, False)]
        # The area sensor of the hidden area is gone, the config sensor updated.
        assert namespace.area_sensor_id(_area_slug("Wohnzimmer", area_id)) in ha.deletes
        assert ha.states[namespace.CONFIG_SENSOR_ID]["state"] == "0"

    async def test_hub_write_after_a_change_in_ha_is_not_skipped(self, db, monkeypatch):
        """Hub hides, HA turns on, hub hides again: the write must
        reach HA, or the next poll re-activates the hidden area."""

        async def fake_broadcast(message, audience=None):
            pass

        monkeypatch.setattr(api_ws, "broadcast", fake_broadcast)
        area_id = await _area(db, "Keller", active=False)
        ha = StatesHA()
        await ha_sensors.sync_area_switch(db, ha, area_id)
        eid = _switch("Keller", area_id)
        assert ha.states[eid]["state"] == "off"
        ha.states[eid] = {**ha.states[eid], "state": "on"}
        assert await ha_sensors.handle_area_switch_state(db, ha, ha.states[eid])
        assert await _is_active(db, area_id) is True

        await db.execute("UPDATE areas SET is_active = 0 WHERE id = ?", (area_id,))
        await db.commit()
        await ha_sensors.sync_area_switch(db, ha, area_id)
        assert ha.states[eid]["state"] == "off"
        await ha_sensors.poll_area_switches(db, ha)
        assert await _is_active(db, area_id) is False

    async def test_foreign_entity_with_our_prefix_is_ignored(self, db):
        area_id = await _area(db)
        ha = StatesHA()
        forged = {
            "entity_id": _switch("Other", area_id),
            "state": "off",
            "attributes": {namespace.AREA_SWITCH_ID_ATTRIBUTE: area_id},
        }
        assert await ha_sensors.handle_area_switch_state(db, ha, forged) is False
        assert await _is_active(db, area_id) is True


class TestWritesOnlyOnChange:
    async def test_unchanged_sensor_is_not_written_again(self, db):
        await _area(db)
        ha = StatesHA()
        await ha_sensors.sync_config_sensor(db, ha)
        await ha_sensors.sync_config_sensor(db, ha)
        assert ha.posts.count(namespace.CONFIG_SENSOR_ID) == 1

        await _area(db, "Küche")
        await ha_sensors.sync_config_sensor(db, ha)
        assert ha.posts.count(namespace.CONFIG_SENSOR_ID) == 2

    async def test_sensor_lost_in_ha_is_written_again(self, db):
        await _area(db)
        ha = StatesHA()
        await ha_sensors.sync_config_sensor(db, ha)
        ha.states.clear()  # HA restart
        await ha_sensors.sync_config_sensor(db, ha)
        assert ha.posts.count(namespace.CONFIG_SENSOR_ID) == 2

    async def test_notification_change_writes_one_sensor_not_all(self, db):
        now = Database.now()
        for i in range(5):
            await add_client(
                db,
                client_id=f"c{i}aaaaaaa",
                name=f"Panel {i}",
                secret_hash="x",
                last_seen=now,
            )
        ha = StatesHA()
        await ha_sensors.sync_client_notification_sensors(db, ha)
        assert len(ha.posts) == 5
        await ha_sensors.sync_client_notification_sensors(db, ha)
        assert len(ha.posts) == 5

    async def test_concurrent_syncs_never_write_an_older_read_last(self, db):
        area_id = await _area(db)
        ha = StatesHA()
        gate = asyncio.Event()
        original = ha._post

        async def slow_first(url, data=None):
            if not gate.is_set():
                gate.set()
                await asyncio.sleep(0.05)
            return await original(url, data)

        ha._post = slow_first
        first = asyncio.ensure_future(ha_sensors.sync_area_switch(db, ha, area_id))
        await gate.wait()
        await db.execute("UPDATE areas SET is_active = 0 WHERE id = ?", (area_id,))
        await db.commit()
        await ha_sensors.sync_area_switch(db, ha, area_id)
        await first
        assert ha.states[_switch("Wohnzimmer", area_id)]["state"] == "off"


class TestLiveResync:
    async def test_connect_and_core_restart_resync(self, db, registry):
        from services.ha_client import HAClient

        area_id = await _area(db)
        fake = FakeHA()
        conn = _ws(fake)
        client = HAClient("tok")
        client.attach_ws(conn)

        async def post(url, data=None):
            entity_id = url.rsplit("/", 1)[-1]
            state = fake.set_state(entity_id, data["state"], data["attributes"])
            client._mirror_write(url, state)
            return state

        async def delete(url):
            fake.states.pop(url.rsplit("/", 1)[-1], None)
            client._mirror_write(url, None)

        client._post = post
        client._delete = delete
        ha_sensors.attach_live_updates(db, client, conn)
        registry.supervise("ha", conn.run)

        assert await _until(lambda: namespace.CONFIG_SENSOR_ID in fake.states)
        switch = _switch("Wohnzimmer", area_id)
        assert await _until(lambda: switch in fake.states)

        t0 = time.monotonic()
        fake.restart_core()
        assert namespace.CONFIG_SENSOR_ID not in fake.states
        assert await _until(lambda: namespace.CONFIG_SENSOR_ID in fake.states, 3)
        assert await _until(lambda: switch in fake.states)
        assert time.monotonic() - t0 < 2

        # A change of the switch in HA arrives as an event, no polling.
        fake.set_state(
            switch,
            "off",
            {namespace.AREA_SWITCH_ID_ATTRIBUTE: area_id},
            ts="2026-11-19T11:00:00+00:00",
        )
        assert await _until(lambda: fake.states[switch]["state"] == "off")
        for _ in range(200):
            if not await _is_active(db, area_id):
                break
            await asyncio.sleep(0.01)
        assert await _is_active(db, area_id) is False
        await client.close()

    async def test_reconnect_without_restart_applies_switch_changes(self, db, registry):
        from services.ha_client import HAClient

        area_id = await _area(db)
        fake = FakeHA()
        conn = _ws(fake)
        client = HAClient("tok")
        client.attach_ws(conn)

        async def post(url, data=None):
            state = fake.set_state(
                url.rsplit("/", 1)[-1], data["state"], data["attributes"]
            )
            client._mirror_write(url, state)
            return state

        client._post = post
        ha_sensors.attach_live_updates(db, client, conn)
        registry.supervise("ha", conn.run)
        switch = _switch("Wohnzimmer", area_id)
        assert await _until(lambda: switch in fake.states)

        # Link drops (HA keeps running); the switch is changed meanwhile.
        fake.subscriptions.clear()
        fake.conns[0].drop()
        assert await _until(lambda: not conn.connected)
        fake.set_state(
            switch,
            "off",
            {namespace.AREA_SWITCH_ID_ATTRIBUTE: area_id},
            ts="2026-11-19T12:00:00+00:00",
        )
        for _ in range(300):
            if not await _is_active(db, area_id):
                break
            await asyncio.sleep(0.01)
        assert await _is_active(db, area_id) is False
        await client.close()

    async def test_homeassistant_started_triggers_check(self, db, registry):
        ha = StatesHA()
        events = []

        class Conn:
            def add_connect_listener(self, fn):
                events.append(("connect", fn))

            def add_event_listener(self, fn):
                events.append(("event", fn))

        ha_sensors.attach_live_updates(db, ha, Conn())
        listener = [fn for kind, fn in events if kind == "event"][0]
        listener("homeassistant_started", {})
        assert await _until(lambda: namespace.CONFIG_SENSOR_ID in ha.states)


class TestOrphans:
    async def test_full_sync_removes_orphans_but_not_registry_entities(self, db):
        await _area(db)
        ha = StatesHA()
        ha.states["sensor.halumar_area_alt_12345678"] = {"state": "1"}
        ha.states["input_boolean.halumar_area_alt_12345678"] = {"state": "on"}
        ha.states["sensor.halumar_client_old_abcdef12_notifications"] = {"state": "0"}
        ha.states["sensor.halumar_area_from_integration"] = {"state": "1"}
        ha.states["light.kitchen"] = {"state": "on"}
        ha.registry = [{"entity_id": "sensor.halumar_area_from_integration"}]

        await ha_sensors.sync_all_sensors(db, ha)
        assert sorted(ha.deletes) == [
            "input_boolean.halumar_area_alt_12345678",
            "sensor.halumar_area_alt_12345678",
            "sensor.halumar_client_old_abcdef12_notifications",
        ]
        assert "sensor.halumar_area_from_integration" in ha.states
        assert "light.kitchen" in ha.states

    async def test_without_mirror_nothing_is_removed(self, db):
        ha = StatesHA(mirror=False)
        ha.states["sensor.halumar_area_alt_12345678"] = {"state": "1"}
        assert await ha_sensors.remove_orphan_entities(db, ha) == 0

    async def test_rename_removes_old_entities(self, db):
        area_id = await _area(db, "Büro")
        ha = StatesHA()
        await ha_sensors.sync_all_sensors(db, ha)
        old_sensor = namespace.area_sensor_id(_area_slug("Büro", area_id))
        old_switch = _switch("Büro", area_id)
        await db.execute("UPDATE areas SET name = 'Arbeit' WHERE id = ?", (area_id,))
        await db.commit()
        await ha_sensors.sync_area_sensor(db, ha, area_id)
        await ha_sensors.sync_area_switch(db, ha, area_id)
        await ha_sensors.remove_renamed_area_entities(ha, "Büro", "Arbeit", area_id)
        assert old_sensor not in ha.states and old_switch not in ha.states
        assert _switch("Arbeit", area_id) in ha.states


# ── /api/ws send queues ──────────────────────────────────────────────────────


class QueueWS:
    def __init__(self, *, block: asyncio.Event | None = None, fail=False):
        self.sent: list[str] = []
        self.block = block
        self.fail = fail
        self.closed: tuple[int, str] | None = None

    async def send_text(self, data):
        if self.fail:
            raise RuntimeError("connection reset")
        if self.block is not None:
            await self.block.wait()
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


@pytest.fixture
def clients(registry):
    ws_registry._clients.clear()
    yield ws_registry._clients
    for ws in list(ws_registry._clients):
        ws_registry.discard_sender(ws)
    ws_registry._clients.clear()


def _bc():
    import importlib

    return importlib.import_module("api.ws.broadcast")


def _register(ws, client_id="panel"):
    ws_registry._clients[ws] = {"authenticated": True, "client_id": client_id}


class TestSendQueues:
    async def test_slow_client_does_not_hold_up_others(self, clients):
        gate = asyncio.Event()
        slow, fast = QueueWS(block=gate), QueueWS()
        _register(slow, "slow")
        _register(fast, "fast")
        t0 = time.monotonic()
        await api_ws.broadcast({"type": "area_updated", "data": {}})
        assert time.monotonic() - t0 < 0.1
        assert len(fast.sent) == 1 and slow.sent == []
        gate.set()
        assert await _until(lambda: len(slow.sent) == 1)
        assert slow.closed is None

    async def test_overflow_closes_instead_of_silently_dropping(
        self, clients, monkeypatch
    ):
        monkeypatch.setattr(ws_registry, "SEND_QUEUE_MAX", 3)
        stuck = QueueWS(block=asyncio.Event())
        _register(stuck, "wall-panel")
        for i in range(6):
            await api_ws.broadcast({"type": "area_updated", "data": {"n": i}})
        assert stuck not in ws_registry._clients
        assert await _until(lambda: stuck.closed is not None)
        assert stuck.closed == (1000, "Send queue overflow")

    async def test_failed_send_closes_the_connection(self, clients):
        broken = QueueWS(fail=True)
        _register(broken, "tv")
        assert await api_ws.send_to_client("tv", {"type": "ping"}) is False
        assert broken not in ws_registry._clients
        assert await _until(lambda: broken.closed == (1000, "Send failed"))

    async def test_stuck_send_times_out_and_closes(self, clients, monkeypatch):
        monkeypatch.setattr(ws_registry, "SEND_TIMEOUT_SECONDS", 0.05)
        stuck = QueueWS(block=asyncio.Event())
        _register(stuck, "ipad")
        await api_ws.broadcast({"type": "area_updated", "data": {}})
        assert await _until(lambda: stuck.closed == (1000, "Send failed"))

    async def test_ping_cycle_drops_dead_connection(self, clients):
        dead, alive = QueueWS(), QueueWS()
        _register(dead, "dead")
        _register(alive, "alive")
        now = time.time()
        ws_registry._clients[dead].update(last_ping=now - 60, last_pong=now - 120)
        assert await _bc().ping_once(now) == 1
        assert dead not in ws_registry._clients
        assert await _until(lambda: dead.closed == (1000, "Ping timeout"))
        assert json.loads(alive.sent[0]) == {"type": "ping"}

    async def test_ping_loop_survives_an_error(self, clients, monkeypatch):
        bc = _bc()
        calls = []

        async def flaky(now=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return 0

        monkeypatch.setattr(bc, "WS_PING_INTERVAL", 0.01)
        monkeypatch.setattr(bc, "ping_once", flaky)
        task = asyncio.ensure_future(bc.ws_ping_loop())
        assert await _until(lambda: len(calls) >= 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ── Discovery token ─────────────────────────────────────────────


class TestDiscoveryToken:
    class Supervisor:
        def __init__(self, db, fail=0):
            self.db = db
            self.fail = fail
            self.calls = 0
            self.valid_during_announce = None

        async def get_addon_info(self):
            return {"data": {"hostname": "local-halumar"}}

        async def send_discovery(self, service, config):
            from api.auth import integration_token_valid

            self.calls += 1
            # HA may use the new token before the announce call returns.
            self.valid_during_announce = await integration_token_valid(
                self.db, config["api_token"]
            )
            self.token = config["api_token"]
            if self.calls <= self.fail:
                raise OSError("Supervisor busy")
            return {"result": "ok"}

    async def test_failed_announce_keeps_current_token(self, db):
        from api.auth import integration_token_valid, rotate_integration_token
        from services.discovery import announce_integration_discovery

        current = await rotate_integration_token(db)
        sup = self.Supervisor(db, fail=1)
        with pytest.raises(OSError):
            await announce_integration_discovery(db, sup)
        assert await integration_token_valid(db, current) is True
        assert await integration_token_valid(db, sup.token) is False

    async def test_success_activates_new_and_ends_old(self, db):
        from api.auth import integration_token_valid, rotate_integration_token
        from services.discovery import announce_integration_discovery

        current = await rotate_integration_token(db)
        sup = self.Supervisor(db)
        assert await announce_integration_discovery(db, sup) is True
        assert sup.valid_during_announce is True
        assert await integration_token_valid(db, sup.token) is True
        assert await integration_token_valid(db, current) is False

    async def test_announce_is_retried_until_accepted(self, db, monkeypatch):
        from api.auth import integration_token_valid
        from services import discovery

        monkeypatch.setattr(discovery, "RETRY_INITIAL_SECONDS", 0.01)
        sup = self.Supervisor(db, fail=2)
        await asyncio.wait_for(discovery.announce_until_done(db, sup), 2)
        assert sup.calls == 3
        assert await integration_token_valid(db, sup.token) is True


# ── Lifespan: start time and shutdown ────────────────────────────────────────


def test_lifespan_starts_fast_without_ha_and_shuts_down_cleanly(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main

    monkeypatch.setenv("HUB_DB_PATH", str(tmp_path / "hub.db"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-token")
    monkeypatch.setattr(main, "_read_addon_options", lambda: {"mdns_enabled": False})
    real_makedirs = main.os.makedirs
    monkeypatch.setattr(
        main.os,
        "makedirs",
        lambda p, *a, **k: None if p.startswith("/data") else real_makedirs(p, *a, **k),
    )

    unreachable = FakeHA()
    unreachable.refuse = 10**6

    class UnreachableHA(HAWebSocket):
        def __init__(self, token):
            super().__init__(token, connect=unreachable.connect)

    monkeypatch.setattr(main, "HAWebSocket", UnreachableHA)

    async def supervisor_down(*args, **kwargs):
        raise OSError("Supervisor not reachable")

    from services.ha_client import HAClient

    monkeypatch.setattr(HAClient, "_get", supervisor_down)
    monkeypatch.setattr(HAClient, "_post", supervisor_down)

    t0 = time.monotonic()
    with TestClient(main.app) as client:
        started = time.monotonic() - t0
        assert client.get("/api/health").status_code == 200
        names = set(tasks.registry.names())
        assert {
            "ha-websocket",
            "sensor-reconcile",
            "ws-ping",
            "guest-expiry",
            "notification-cleanup",
            "db-snapshot",
            "device-provisioning",
        } <= names
    stopped = time.monotonic() - t0 - started
    assert started < 2.0
    assert stopped < 3.0
    assert tasks.registry.names() == []
    assert unreachable.refuse < 10**6  # it kept trying in the background


class TestRemovedDeviceNotificationSensor:
    async def test_sensor_of_a_removed_device_goes(self, db, monkeypatch):
        """The notification texts of a removed device do not stay in
        HA: the removal deletes its sensor, the next sync any left over."""
        from api.pairing import clients as clients_api
        from services import device_provisioning as dp
        from services.notifications import create_notification

        async def _none(*_a, **_k):
            return 0

        async def _true(*_a, **_k):
            return True

        monkeypatch.setattr(api_ws, "broadcast", _none)
        monkeypatch.setattr(api_ws, "disconnect_client", _none)
        monkeypatch.setattr(api_ws, "signal_client_removed", _none)
        monkeypatch.setattr(dp, "_sender", _true)
        ha = StatesHA()
        guest = await add_client(db, name="Guest Phone", guest=True)
        other = await add_client(db, name="Other Phone")
        for cid in (guest, other):
            await create_notification(db, cid, title="Alarm code", message="1234")
        await ha_sensors.sync_client_notification_sensors(db, ha)
        s_guest = ha_sensors.client_notification_sensor_id("Guest Phone", guest)
        s_other = ha_sensors.client_notification_sensor_id("Other Phone", other)
        assert s_guest in ha.states and s_other in ha.states

        request = types.SimpleNamespace(
            state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
            client=None,
        )
        await clients_api.delete_client(request, guest, ha_user_removal=None)
        assert s_guest not in ha.states

        # Gone some other way (row deleted): the next sync removes it.
        async with db.transaction():
            await db.execute("DELETE FROM paired_clients WHERE id = ?", (other,))
        await ha_sensors.sync_client_notification_sensors(db, ha)
        assert s_other not in ha.states

    @pytest.mark.parametrize("how", ["delete", "revoke"])
    async def test_next_sync_does_not_bring_it_back(self, db, monkeypatch, how):
        """A tombstoned or revoked row keeps no sensor and is not in
        the guests sensor; the next sync does not rebuild them."""
        from api.pairing import clients as clients_api
        from services import device_provisioning as dp
        from services.notifications import create_notification

        async def _none(*_a, **_k):
            return 0

        async def _true(*_a, **_k):
            return True

        monkeypatch.setattr(api_ws, "broadcast", _none)
        monkeypatch.setattr(api_ws, "disconnect_client", _none)
        monkeypatch.setattr(api_ws, "signal_client_removed", _none)
        monkeypatch.setattr(dp, "_sender", _true)
        ha = StatesHA()
        cid = await add_client(db, name="Stolen Phone", guest=True)
        await create_notification(db, cid, title="Alarm code", message="1234")
        await ha_sensors.sync_client_notification_sensors(db, ha)
        await ha_sensors.sync_guests_sensor(db, ha)
        sid = ha_sensors.client_notification_sensor_id("Stolen Phone", cid)
        assert sid in ha.states
        request = types.SimpleNamespace(
            state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
            app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=ha)),
            client=None,
        )
        if how == "delete":
            await clients_api.delete_client(request, cid, ha_user_removal=None)
        else:
            await clients_api.revoke_client(request, cid)
        await ha_sensors.sync_client_notification_sensors(db, ha)
        assert sid not in ha.states
        guests = ha.states[namespace.GUESTS_SENSOR_ID]["attributes"]["guests"]
        assert cid not in {g["client_id"] for g in guests}
        # A row left over from before (e.g. revoked before this fix) also goes.
        ha.states[sid] = {"entity_id": sid, "state": "1", "attributes": {}}
        await ha_sensors.sync_client_notification_sensors(db, ha)
        assert sid not in ha.states
