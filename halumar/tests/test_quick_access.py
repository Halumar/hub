"""Quick access for widgets, controls and Siri.

Per-entity rule from data/quick_access.json, per-device resolution
(client_config.quick_access), admin API (GET/PUT /api/quick-access) with the
critical-ack rule, managed keys, migration v7 and the check sensor
sensor.halumar_client_<slug>_<uid8>_quick_access.
"""

import re
import sqlite3
import types
import uuid

import fastapi
import pytest

import api.quick_access as qa_api
import api.websocket as ws
import services.crypto as crypto
from api.websocket import _get_client_config
from services.ha_sensors import client_quick_access_sensor_id
from models.database import Database

from .profile_helpers import add_client
from services import db_migrations, quick_access

CONTRACT = "../rootfs/opt/hub/data/quick_access.json"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    quick_access.reset_state()
    yield
    quick_access.reset_state()
    crypto.reset_cache()


@pytest.fixture
def broadcasts(monkeypatch):
    captured: list[dict] = []

    async def _capture(message, *a, **k):
        captured.append(message)

    monkeypatch.setattr(ws, "broadcast", _capture)
    return captured


class FakeHA:
    """States by entity id; records sensor writes and deletes."""

    def __init__(
        self, classes: dict[str, str | None] | None = None, down: bool = False
    ):
        self.classes = classes or {}
        self.down = down
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[str] = []

    async def get_state(self, entity_id):
        if self.down:
            raise RuntimeError("HA down")
        return {
            "entity_id": entity_id,
            "attributes": {"device_class": self.classes.get(entity_id)},
        }

    async def get_states(self):
        if self.down:
            raise RuntimeError("HA down")
        return [
            {
                "entity_id": e,
                "attributes": {"device_class": c, "friendly_name": e.upper()},
            }
            for e, c in self.classes.items()
        ]

    async def _post(self, url, payload):
        self.posts.append((url.rsplit("/", 1)[-1], payload))

    async def _delete(self, url):
        self.deletes.append(url.rsplit("/", 1)[-1])


def _request(db, ha):
    state = types.SimpleNamespace(db=db, ha_client=ha, auth_type="admin")
    app = types.SimpleNamespace(state=state)
    return types.SimpleNamespace(state=state, app=app, client=None)


async def _area(db, name="Wohnzimmer", tags=(), active=1):
    area_id = str(uuid.uuid4())
    section_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, ?, ?, ?)",
        (area_id, name, active, now, now),
    )
    await db.execute(
        "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, columns) "
        "VALUES (?, ?, 'S', 0, 1, 2)",
        (section_id, area_id),
    )
    for tag in tags:
        await db.execute(
            "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
        )
    await db.commit()
    return area_id, section_id


async def _place(db, area, entity_id, visible=1):
    area_id, section_id = area
    await db.execute(
        "INSERT INTO area_entities (id, area_id, entity_id, section_id, is_visible, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), area_id, entity_id, section_id, visible, Database.now()),
    )
    await db.commit()


async def _client(db, name="Parent iPhone", tags=None, guest=False, overrides=None):
    return await add_client(
        db,
        name=name,
        secret_hash="h",
        last_seen=Database.now(),
        guest=guest,
        tags=tags or ([] if not guest else None),
        overrides=overrides,
    )


async def _row(db, client_id):
    return await db.fetchone("SELECT * FROM paired_clients WHERE id = ?", (client_id,))


# ── per-entity rule ──────────────────────────────────────────────────────────


class TestRule:
    def test_data_is_contract_copy(self):
        data = quick_access.data()
        assert data["version"] == 1
        domains = [d for row in data["domains"] for d in row["domains"]]
        assert len(domains) == len(set(domains))
        assert "_other" in domains

    @pytest.mark.parametrize(
        "entity_id, device_class, default, mx, critical",
        [
            ("light.stehlampe", None, "control", "control", False),
            ("scene.movie", None, "control", "control", False),
            ("cover.living_room_window", "window", "control", "control", False),
            ("cover.garage_door", "garage", "status", "control", True),
            ("cover.gate", "gate", "status", "control", True),
            ("lock.front_door", None, "status", "control", True),
            ("alarm_control_panel.security", None, "status", "control", True),
            ("automation.night", None, "status", "control", True),
            ("sensor.temp", "temperature", "status", "status", False),
            ("camera.demo", None, "off", "off", False),
            ("person.dom", None, "off", "off", False),
            ("todo.list", None, "status", "status", False),
        ],
    )
    def test_entity_rule(self, entity_id, device_class, default, mx, critical):
        assert quick_access.entity_rule(entity_id, device_class) == {
            "default": default,
            "max": mx,
            "critical": critical,
        }

    def test_unknown_device_class_fails_closed(self):
        assert quick_access.is_critical("cover.x", None, known=False)
        assert not quick_access.is_critical("light.x", None, known=False)

    def test_effective_level(self):
        eff = quick_access.effective_level
        assert eff("light.a", None, None) == "control"
        assert eff("light.a", None, {"level": "off"}) == "off"
        assert eff("sensor.t", None, {"level": "control"}) == "status"  # capped by max
        assert eff("lock.door", None, None) == "status"
        assert (
            eff("lock.door", None, {"level": "control", "critical_ack": 0}) == "status"
        )
        assert (
            eff("lock.door", None, {"level": "control", "critical_ack": 1}) == "control"
        )
        assert eff("camera.c", None, {"level": "control"}) == "off"

    def test_check_update(self):
        quick_access.check_update("light.a", None, "control", False)
        quick_access.check_update("lock.a", None, "status", False)
        quick_access.check_update("lock.a", None, None, False)
        with pytest.raises(quick_access.QuickAccessError) as exc:
            quick_access.check_update("camera.c", None, "status", False)
        assert exc.value.detail[0]["type"] == "quick_access.not_allowed"
        with pytest.raises(quick_access.QuickAccessError) as exc:
            quick_access.check_update("cover.garage", "garage", "control", False)
        assert exc.value.detail[0]["type"] == "quick_access.critical_ack_required"
        quick_access.check_update("cover.garage", "garage", "control", True)


# ── managed keys ─────────────────────────────────────────────────────────────


# ── per device ───────────────────────────────────────────────────────────────


class TestResolve:
    async def test_member_sees_its_areas_only(self, db):
        wz = await _area(db, "Wohnzimmer", tags=["eg"])
        og = await _area(db, "Büro", tags=["og"])
        off = await _area(db, "Alt", active=0)
        await _place(db, wz, "light.stehlampe")
        await _place(db, wz, "lock.front_door")
        await _place(db, wz, "camera.demo")
        await _place(db, wz, "sensor.hidden", visible=0)
        await _place(db, og, "light.buero")
        await _place(db, off, "light.alt")
        cid = await _client(db, tags=["eg"])
        qa = await quick_access.resolve_for_client(db, FakeHA(), await _row(db, cid))
        assert qa["enabled"] is True and qa["max"] == "control"
        assert qa["entities"] == {
            "light.stehlampe": "control",
            "lock.front_door": "status",
        }
        assert qa["critical"] == ["lock.front_door"]
        assert qa["sensor"].startswith("sensor.halumar_client_parent_iphone_")
        assert qa["sensor"].endswith("_quick_access")

    async def test_admin_levels_and_device_max(self, db):
        wz = await _area(db)
        for e in ("light.a", "lock.door", "cover.garage", "cover.window"):
            await _place(db, wz, e)
        await quick_access.store_level(db, "light.a", "status", False, "admin")
        await quick_access.store_level(db, "lock.door", "control", True, "admin")
        await quick_access.store_level(db, "cover.garage", "control", False, "admin")
        ha = FakeHA({"cover.garage": "garage", "cover.window": "window"})
        member = await _client(db)
        qa = await quick_access.resolve_for_client(db, ha, await _row(db, member))
        assert qa["entities"] == {
            "light.a": "status",
            "lock.door": "control",
            "cover.garage": "status",
            "cover.window": "control",
        }
        assert sorted(qa["critical"]) == ["cover.garage", "lock.door"]
        child = await _client(db, name="Kinder", overrides={"quick_access": "status"})
        qa = await quick_access.resolve_for_client(db, ha, await _row(db, child))
        assert qa["max"] == "status"
        assert set(qa["entities"].values()) == {"status"}

    async def test_disabled_device_and_guest(self, db):
        wz = await _area(db)
        await _place(db, wz, "light.a")
        off = await _client(db, overrides={"quick_access": "off"})
        qa = await quick_access.resolve_for_client(db, FakeHA(), await _row(db, off))
        assert qa["enabled"] is False and qa["entities"] == {} and qa["sensor"]
        guest = await _client(db, name="Gast", tags=["*"], guest=True)
        qa = await quick_access.resolve_for_client(db, FakeHA(), await _row(db, guest))
        assert qa == quick_access.disabled()

    async def test_ha_down_covers_fail_closed(self, db):
        wz = await _area(db)
        await _place(db, wz, "cover.window")
        cid = await _client(db)
        qa = await quick_access.resolve_for_client(
            db, FakeHA(down=True), await _row(db, cid)
        )
        assert qa["entities"] == {"cover.window": "status"}
        assert qa["critical"] == ["cover.window"]

    async def test_client_config_carries_quick_access(self, db):
        wz = await _area(db)
        await _place(db, wz, "scene.movie")
        cid = await _client(db)
        cfg = await _get_client_config(db, cid)
        assert cfg["quick_access"]["enabled"] is True
        assert cfg["quick_access"]["entities"] == {"scene.movie": "control"}
        # The sensor the device reads is the one the hub writes (named after the device;
        # found in a live test: client_config carried sensor.<ns>_client_client_<uid8>_…).
        row = await db.fetchone("SELECT name FROM paired_clients WHERE id = ?", (cid,))
        assert cfg["quick_access"]["sensor"] == client_quick_access_sensor_id(
            row["name"], cid
        )
        assert "_client_client_" not in cfg["quick_access"]["sensor"]
        assert "quick_access" in (await _get_client_config(db, "ingress"))
        assert (await _get_client_config(db, "ingress"))["quick_access"][
            "enabled"
        ] is False


# ── admin API ────────────────────────────────────────────────────────────────


class TestAdminAPI:
    async def test_put_and_list(self, db, broadcasts):
        wz = await _area(db)
        await _place(db, wz, "lock.front_door")
        await _place(db, wz, "light.a")
        ha = FakeHA({"lock.front_door": None, "light.a": None})
        req = _request(db, ha)
        with pytest.raises(fastapi.HTTPException) as exc:
            await qa_api.update_quick_access(
                req, "lock.front_door", qa_api.QuickAccessUpdate(level="control")
            )
        assert exc.value.status_code == 422
        assert exc.value.detail[0]["type"] == "quick_access.critical_ack_required"
        out = await qa_api.update_quick_access(
            req,
            "lock.front_door",
            qa_api.QuickAccessUpdate(level="control", critical_ack=True),
        )
        item = out["items"][0]
        assert item["level"] == "control" and item["effective"] == "control"
        assert item["critical"] and item["critical_ack"] and item["default"] == "status"
        assert broadcasts == [{"type": "client_config_updated", "data": {}}]
        listed = (await qa_api.list_quick_access(req))["items"]
        assert [i["entity_id"] for i in listed] == ["light.a", "lock.front_door"]
        assert listed[0]["name"] == "LIGHT.A" and listed[0]["effective"] == "control"
        out = await qa_api.update_quick_access(
            req, "lock.front_door", qa_api.QuickAccessUpdate(level=None)
        )
        assert (
            out["items"][0]["level"] is None
            and out["items"][0]["effective"] == "status"
        )

    async def test_put_rejects(self, db, broadcasts):
        req = _request(db, FakeHA())
        with pytest.raises(fastapi.HTTPException) as exc:
            await qa_api.update_quick_access(
                req, "camera.demo", qa_api.QuickAccessUpdate(level="status")
            )
        assert exc.value.detail[0]["type"] == "quick_access.not_allowed"
        with pytest.raises(fastapi.HTTPException) as exc:
            await qa_api.update_quick_access(
                req, "../etc", qa_api.QuickAccessUpdate(level="status")
            )
        assert exc.value.status_code == 422
        with pytest.raises(Exception):
            qa_api.QuickAccessUpdate(level="always")
        member = types.SimpleNamespace(
            state=types.SimpleNamespace(auth_type="client"), app=req.app, client=None
        )
        with pytest.raises(fastapi.HTTPException) as exc:
            await qa_api.list_quick_access(member)
        assert exc.value.status_code == 403
        assert broadcasts == []


# ── check sensor ─────────────────────────────────────────────────────────────


class TestSensor:
    async def test_written_on_change_only_and_deleted_on_removal(self, db):
        wz = await _area(db)
        await _place(db, wz, "light.a")
        member = await _client(db)
        await _client(db, name="Gast", tags=["*"], guest=True)
        ha = FakeHA()
        assert await quick_access.sync_sensors(db, ha) == 1
        sensor, payload = ha.posts[0]
        assert sensor.endswith("_quick_access")
        attrs = payload["attributes"]
        assert payload["state"] == "1"
        assert attrs["client_id"] == member and attrs["enabled"] is True
        assert attrs["entities"] == {"light.a": "control"} and attrs["critical"] == []
        # Unchanged: nothing written; forced (HA restart): written again.
        assert await quick_access.sync_sensors(db, ha) == 0
        assert await quick_access.sync_sensors(db, ha, force=True) == 1
        # Revocation by the admin: next sync shows status only.
        await quick_access.store_level(db, "light.a", "status", False, "admin")
        assert await quick_access.sync_sensors(db, ha) == 1
        assert ha.posts[-1][1]["attributes"]["entities"] == {"light.a": "status"}
        assert ha.posts[-1][1]["state"] == "0"
        # Device removed: its sensor goes.
        async with db.transaction():
            await db.execute(
                "UPDATE paired_clients SET is_active = 0 WHERE id = ?", (member,)
            )
        assert await quick_access.sync_sensors(db, ha) == 1
        assert ha.deletes == [sensor]

    async def test_sensor_id_valid_for_ha_and_one_refusal_stops_no_other(self, db):
        """A device name whose slug was cut right after a
        separator gave "…_<uid8>" with "__" (HA 400). The id is valid now, a
        stale bad id from before is replaced on the next sync, and a sensor
        HA still refuses does not keep the other devices' sensors out."""
        valid = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")

        class StrictHA(FakeHA):
            refuse: set[str] = set()

            async def _post(self, url, payload):
                entity_id = url.rsplit("/", 1)[-1]
                if not valid.match(entity_id) or entity_id in self.refuse:
                    raise RuntimeError(f"400 Bad Request for {entity_id}")
                await super()._post(url, payload)

        wz = await _area(db)
        await _place(db, wz, "light.a")
        long_one = await _client(
            db, name="TEST remote adjustment " + "x" * 24 + " remote"
        )
        other = await _client(db, name="Zweites Gerät")
        sensor = client_quick_access_sensor_id(
            "TEST remote adjustment " + "x" * 24 + " remote", long_one
        )
        assert "__" not in sensor
        stale = sensor.replace("_quick_access", "").rsplit("_", 1)
        stale = f"{stale[0]}__{stale[1]}_quick_access"
        quick_access._written[long_one] = (stale, "old")
        ha = StrictHA()
        ha.refuse = {client_quick_access_sensor_id("Zweites Gerät", other)}
        assert await quick_access.sync_sensors(db, ha) == 1
        assert [p[0] for p in ha.posts] == [sensor]
        assert ha.deletes == [stale]
        # The refused one is retried on the next sync and lands once HA takes it.
        ha.refuse = set()
        assert await quick_access.sync_sensors(db, ha) == 1
        assert ha.posts[-1][1]["attributes"]["client_id"] == other

    async def test_notify_change_debounces(self, db, monkeypatch):
        calls = []

        async def fake_sync(db_, ha_, force=False):
            calls.append(force)
            return 0

        monkeypatch.setattr(quick_access, "sync_sensors", fake_sync)
        monkeypatch.setattr(quick_access, "SYNC_DEBOUNCE_SECONDS", 0.01)
        quick_access.notify_change("client_config_updated")  # not registered: ignored
        assert quick_access._sync_task is None
        quick_access.register(db, FakeHA())
        quick_access.notify_change("pong")
        quick_access.notify_change("notification_created")
        assert quick_access._sync_task is None
        quick_access.notify_change("area_updated")
        quick_access.notify_change("client_config_updated")
        await quick_access._sync_task
        assert calls == [False]  # both changes in one debounced sync


# ── migration ────────────────────────────────────────────────────────────────


async def test_v6_database_gets_quick_access_table(tmp_path):
    db_path = str(tmp_path / "hub.db")
    conn = sqlite3.connect(db_path)
    from pathlib import Path

    v6 = (Path(__file__).parent / "fixtures" / "schema_v8.sql").read_text()
    start = v6.index("CREATE TABLE IF NOT EXISTS quick_access (")
    end = v6.index(");", start) + 2
    v6 = v6[:start] + v6[end:]
    for stmt in db_migrations.split_sql(v6):
        conn.execute(stmt)
    conn.execute("PRAGMA user_version = 6")
    conn.commit()
    conn.close()
    db = Database(db_path)
    await db.initialize()
    try:
        assert (await db.fetchone("PRAGMA user_version"))["user_version"] == (
            db_migrations.latest_version()
        )
        assert await db_migrations.missing_schema_parts(db.connection) == []
        await quick_access.store_level(db, "lock.a", "control", True, "admin")
        with pytest.raises(sqlite3.IntegrityError):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO quick_access (entity_id, level, updated_at) VALUES ('x.y', 'always', 'n')"
                )
    finally:
        await db.close()
