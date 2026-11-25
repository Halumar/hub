"""Area entity and layout validation.

POST /api/areas/{id}/entities: ``entity_id`` must be ``domain.object_id``
(schema, always) and known to Home Assistant (state mirror or entity
registry of the persistent connection). Without a complete live mirror the
existence check is skipped. PUT /api/areas/{id}/layout: every item's section
must belong to the area. Both answer 422 in the HTTPValidationError shape the
contract documents for these operations.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import api.areas as areas
from models.database import Database
from models.schemas import AreaEntityCreate, SectionLayoutItem, SectionLayoutUpdate
from services.ha_client import HAClient


class _Mirror:
    """Stand-in for services.ha_ws.HAWebSocket (mirror + registry cache)."""

    def __init__(self, states=(), registry=(), *, ready=True, registry_error=None):
        self.states = set(states)
        self.entries = [{"entity_id": e} for e in registry]
        self.connected = ready
        self.states_ready = ready
        self.registry_error = registry_error
        self.registry_calls = 0

    def has_state(self, entity_id):
        return entity_id in self.states

    async def registry(self, kind):
        assert kind == "entity"
        self.registry_calls += 1
        if self.registry_error:
            raise self.registry_error
        return list(self.entries)


class _FakeHA:
    def __init__(self, known):
        self.known = known
        self.asked = []

    async def entity_known(self, entity_id):
        self.asked.append(entity_id)
        return self.known

    async def get_state(self, entity_id):
        return None

    async def get_states(self):
        return []

    async def get_entity_registry(self):
        return []


def _request(db, ha):
    state = SimpleNamespace(db=db, ha_client=ha, auth_type="ingress")
    return SimpleNamespace(
        state=state,
        app=SimpleNamespace(state=state),
        client=SimpleNamespace(host="127.0.0.1"),
    )


async def _area(db, *, sections=1):
    area_id = str(uuid.uuid4())
    now = Database.now()
    await db.execute(
        "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
        "VALUES (?, ?, 0, 1, ?, ?)",
        (area_id, area_id[:8], now, now),
    )
    section_ids = []
    for i in range(sections):
        section_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO area_sections (id, area_id, title, sort_order, column_span, "
            "columns) VALUES (?, ?, 'S', ?, 1, 2)",
            (section_id, area_id, i),
        )
        section_ids.append(section_id)
    await db.commit()
    return area_id, section_ids


@pytest.fixture
def quiet(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(areas.api_ws, "broadcast", _noop)
    monkeypatch.setattr(areas, "_sync_sensors", _noop)


# ── entity_id format (schema) ────────────────────────────────────────────────


class TestEntityIdFormat:
    @pytest.mark.parametrize(
        "entity_id",
        [
            "TEST not an entity",
            "light",
            "light.",
            ".bed",
            "Light.Bed",
            "light.bed.extra",
            "light.bed/../x",
            "light.bed light",
        ],
    )
    def test_rejected(self, entity_id):
        with pytest.raises(ValidationError) as exc:
            AreaEntityCreate(entity_id=entity_id, section_id="s")
        (error,) = exc.value.errors()
        assert error["loc"] == ("entity_id",)
        assert error["type"] == "entity_id.invalid_format"

    @pytest.mark.parametrize(
        "entity_id", ["light.bed_light", "sensor.halumar_area_1", "input_button.x2"]
    )
    def test_accepted(self, entity_id):
        assert AreaEntityCreate(entity_id=entity_id, section_id="s").entity_id == (
            entity_id
        )


# ── existence (HAClient.entity_known) ────────────────────────────────────────


class TestEntityKnown:
    async def _known(self, ws, entity_id="light.bed"):
        ha = HAClient("tok")
        try:
            if ws is not None:
                ha.attach_ws(ws)
            return await ha.entity_known(entity_id)
        finally:
            await ha.close()

    async def test_without_connection_undecided(self):
        assert await self._known(None) is None

    async def test_mirror_not_ready_undecided(self):
        ws = _Mirror(ready=False)
        assert await self._known(ws) is None
        assert ws.registry_calls == 0

    async def test_state_in_mirror(self):
        ws = _Mirror(states={"light.bed"})
        assert await self._known(ws) is True
        assert ws.registry_calls == 0

    async def test_registered_without_state(self):
        assert await self._known(_Mirror(registry={"light.bed"})) is True

    async def test_unknown(self):
        assert (
            await self._known(_Mirror(states={"light.x"}, registry={"light.y"}))
            is False
        )

    async def test_registry_error_undecided(self):
        ws = _Mirror(registry_error=RuntimeError("HA gone"))
        assert await self._known(ws) is None


# ── POST /api/areas/{id}/entities ────────────────────────────────────────────


class TestAddEntity:
    async def _add(self, db, ha, entity_id="light.bed_light"):
        area_id, (section_id,) = await _area(db)
        res = await areas.add_entity_to_area(
            _request(db, ha),
            area_id,
            AreaEntityCreate(entity_id=entity_id, section_id=section_id),
        )
        return area_id, res

    async def test_unknown_entity_is_422_and_stores_nothing(self, db, quiet):
        ha = _FakeHA(False)
        with pytest.raises(HTTPException) as exc:
            await self._add(db, ha, "light.does_not_exist")
        assert exc.value.status_code == 422
        assert exc.value.detail == [
            {
                "loc": ["body", "entity_id"],
                "msg": "light.does_not_exist does not exist in Home Assistant",
                "type": "entity_not_found",
            }
        ]
        assert ha.asked == ["light.does_not_exist"]
        assert await db.fetchall("SELECT * FROM area_entities") == []

    async def test_known_entity_is_added(self, db, quiet):
        area_id, res = await self._add(db, _FakeHA(True))
        assert res.entity_id == "light.bed_light"
        assert len(await db.fetchall("SELECT * FROM area_entities")) == 1

    async def test_ha_not_connected_checks_format_only(self, db, quiet):
        area_id, res = await self._add(db, _FakeHA(None), "light.not_loaded_yet")
        assert res.entity_id == "light.not_loaded_yet"


# ── PUT /api/areas/{id}/layout ───────────────────────────────────────────────


class TestLayoutSections:
    async def _setup(self, db):
        area_id, (s1, s2) = await _area(db, sections=2)
        other_area, (foreign,) = await _area(db)
        now = Database.now()
        await db.execute(
            "INSERT INTO area_entities (id, area_id, entity_id, section_id, "
            "sort_order, created_at) VALUES (?, ?, 'light.a', ?, 0, ?)",
            (str(uuid.uuid4()), area_id, s1, now),
        )
        widget_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO area_widgets (id, area_id, section_id, widget_type, config, "
            "sort_order, created_at) VALUES (?, ?, ?, 'spacer', '{}', 1, ?)",
            (widget_id, area_id, s1, now),
        )
        await db.commit()
        return area_id, s1, s2, foreign, widget_id

    async def _sections(self, db, area_id):
        entity = await db.fetchone(
            "SELECT section_id, sort_order FROM area_entities WHERE area_id = ?",
            (area_id,),
        )
        widget = await db.fetchone(
            "SELECT section_id, sort_order FROM area_widgets WHERE area_id = ?",
            (area_id,),
        )
        return (entity["section_id"], entity["sort_order"]), (
            widget["section_id"],
            widget["sort_order"],
        )

    async def test_own_sections_are_applied(self, db, quiet):
        area_id, s1, s2, _, widget_id = await self._setup(db)
        body = SectionLayoutUpdate(
            items=[
                SectionLayoutItem(entity_id="light.a", section_id=s2, sort_order=3),
                SectionLayoutItem(widget_id=widget_id, section_id=s2, sort_order=4),
            ]
        )
        assert await areas.update_layout(
            _request(db, _FakeHA(True)), area_id, body
        ) == {"status": "ok"}
        assert await self._sections(db, area_id) == ((s2, 3), (s2, 4))

    async def test_foreign_section_is_422_and_changes_nothing(self, db, quiet):
        area_id, s1, s2, foreign, widget_id = await self._setup(db)
        body = SectionLayoutUpdate(
            items=[
                SectionLayoutItem(entity_id="light.a", section_id=s2, sort_order=3),
                SectionLayoutItem(
                    widget_id=widget_id, section_id=foreign, sort_order=4
                ),
                SectionLayoutItem(widget_id=widget_id, section_id="nope", sort_order=5),
            ]
        )
        with pytest.raises(HTTPException) as exc:
            await areas.update_layout(_request(db, _FakeHA(True)), area_id, body)
        assert exc.value.status_code == 422
        assert [(e["loc"], e["type"]) for e in exc.value.detail] == [
            (["body", "items", 1, "section_id"], "section_not_in_area"),
            (["body", "items", 2, "section_id"], "section_not_in_area"),
        ]
        # all or nothing: the valid first item was not applied either
        assert await self._sections(db, area_id) == ((s1, 0), (s1, 1))

    async def test_unknown_area_stays_404(self, db, quiet):
        body = SectionLayoutUpdate(
            items=[SectionLayoutItem(entity_id="light.a", section_id="x", sort_order=0)]
        )
        with pytest.raises(HTTPException) as exc:
            await areas.update_layout(_request(db, _FakeHA(True)), "missing", body)
        assert exc.value.status_code == 404


# ── contract: 422 is the documented error of both operations ─────────────────


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/areas/{area_id}/entities", "post"),
        ("/api/areas/{area_id}/layout", "put"),
    ],
)
def test_contract_documents_422(path, method):
    from main import app

    responses = app.openapi()["paths"][path][method]["responses"]
    assert "422" in responses
