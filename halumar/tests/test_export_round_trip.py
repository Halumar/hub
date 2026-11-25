"""Whatever a writer accepts exports and imports again, without loss.

The import checks with the models of the REST endpoints, and every writer
(create, update, layout, sync, import) keeps the same bounds, for example
``columns`` in ``PUT .../layout`` and the length of a ``display_name`` stored
by the HA sync. Property: random operations through every writer, valid or not;
what was accepted must import again (overwrite) with nothing failed or
skipped, and export the same.
"""

import asyncio
import json
import os
import tempfile
import types
from unittest import mock

from fastapi import HTTPException
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import api.areas as areas
import api.export as export
import api.sync as sync
from api import ws as api_ws
from models.database import Database
from models.schemas import (
    AreaCreate,
    AreaEntityCreate,
    AreaEntityUpdate,
    AreaUpdate,
    AreaWidgetCreate,
    AreaWidgetUpdate,
    ImportRequest,
    SectionCreate,
    SectionLayoutUpdate,
    SectionUpdate,
)


class _HA:
    def __init__(self, ha_areas=(), entities=()):
        self.ha_areas = list(ha_areas)
        self.entities = list(entities)

    async def get_state(self, _entity_id):
        return None

    async def get_areas(self):
        return self.ha_areas

    async def get_entity_registry(self):
        return self.entities

    async def get_devices(self):
        return []


def _req(db, ha):
    state = types.SimpleNamespace(db=db, ha_client=ha, auth_type="ingress")
    return types.SimpleNamespace(
        state=state,
        app=types.SimpleNamespace(state=state),
        client=types.SimpleNamespace(host="127.0.0.1"),
    )


async def _noop(*_a, **_k):
    return None


INTS = st.one_of(st.integers(-2, 5), st.integers(-(2**40), 2**40))
TEXT = st.one_of(
    st.text(max_size=8),
    st.text(alphabet="ab \t\x01ä", min_size=90, max_size=220),
)
NAMES = st.text(alphabet="abc XYZ", min_size=1, max_size=12)
ENTITY_IDS = st.sampled_from([f"light.l{i}" for i in range(6)])
OPTIONAL = st.one_of(st.none(), INTS)


def _norm(exported):
    out = json.loads(json.dumps(exported, default=str))
    for a in out:
        for k in ("id", "created_at", "updated_at"):
            a.pop(k, None)
        for s in a.get("sections", []):
            for k in ("id", "created_at", "area_id"):
                s.pop(k, None)
            for item in s.get("entities", []) + s.get("widgets", []):
                for k in ("id", "created_at", "area_id", "section_id"):
                    item.pop(k, None)
    return sorted(out, key=lambda a: a["name"])


async def _apply(data, db, req, ha):
    """Random writes; rejected ones (422/400/404 or a model error) are fine."""
    area = await areas.create_area(req, AreaCreate(name="Raum"))
    aid = area.id
    sections = [
        (await db.fetchone("SELECT id FROM area_sections WHERE area_id = ?", (aid,)))[
            "id"
        ]
    ]
    widgets: list[str] = []

    async def attempt(fn):
        try:
            return await fn()
        except (ValidationError, HTTPException):
            return None

    for _ in range(data.draw(st.integers(1, 12), label="ops")):
        op = data.draw(
            st.sampled_from(
                [
                    "update_area",
                    "create_section",
                    "update_section",
                    "add_entity",
                    "update_entity",
                    "create_widget",
                    "update_widget",
                    "layout",
                    "sync",
                ]
            ),
            label="op",
        )
        sid = data.draw(st.sampled_from(sections), label="section")
        if op == "update_area":
            r = await attempt(
                lambda: areas.update_area(
                    req,
                    aid,
                    AreaUpdate(
                        icon=data.draw(st.one_of(st.none(), TEXT)),
                        description=data.draw(st.one_of(st.none(), TEXT)),
                        grid_cols=data.draw(OPTIONAL),
                        sort_order=data.draw(OPTIONAL),
                    ),
                )
            )
        elif op == "create_section":
            r = await attempt(
                lambda: areas.create_section(
                    req,
                    aid,
                    SectionCreate(
                        title=data.draw(TEXT),
                        sort_order=data.draw(INTS),
                        column_span=data.draw(INTS),
                        columns=data.draw(INTS),
                    ),
                )
            )
            if r is not None:
                sections.append(r.id)
        elif op == "update_section":
            await attempt(
                lambda: areas.update_section(
                    req,
                    aid,
                    sid,
                    SectionUpdate(
                        title=data.draw(st.one_of(st.none(), TEXT)),
                        columns=data.draw(OPTIONAL),
                        column_span=data.draw(OPTIONAL),
                        is_collapsed=data.draw(st.one_of(st.none(), st.booleans())),
                    ),
                )
            )
        elif op == "add_entity":
            await attempt(
                lambda: areas.add_entity_to_area(
                    req,
                    aid,
                    AreaEntityCreate(
                        entity_id=data.draw(ENTITY_IDS),
                        section_id=sid,
                        display_name=data.draw(st.one_of(st.none(), TEXT)),
                        columns=data.draw(INTS),
                        rows=data.draw(INTS),
                        sort_order=data.draw(INTS),
                        control_mode=data.draw(
                            st.sampled_from([None, "basic", "full", "x"])
                        ),
                    ),
                )
            )
        elif op == "update_entity":
            await attempt(
                lambda: areas.update_area_entity(
                    req,
                    aid,
                    data.draw(ENTITY_IDS),
                    AreaEntityUpdate.model_validate(
                        {
                            "section_id": sid,
                            "display_name": data.draw(st.one_of(st.none(), TEXT)),
                            "columns": data.draw(INTS),
                            "rows": data.draw(INTS),
                        }
                    ),
                )
            )
        elif op == "create_widget":
            r = await attempt(
                lambda: areas.create_widget(
                    req,
                    aid,
                    AreaWidgetCreate(
                        section_id=sid,
                        widget_type=data.draw(st.sampled_from(["text", "clock", "x"])),
                        config=data.draw(
                            st.dictionaries(
                                st.text(max_size=4),
                                st.text(
                                    max_size=data.draw(st.sampled_from([5, 12000]))
                                ),
                                max_size=3,
                            )
                        ),
                        columns=data.draw(INTS),
                        rows=data.draw(INTS),
                    ),
                )
            )
            if r is not None:
                widgets.append(r.id)
        elif op == "update_widget" and widgets:
            await attempt(
                lambda: areas.update_widget(
                    req,
                    aid,
                    data.draw(st.sampled_from(widgets)),
                    AreaWidgetUpdate(
                        section_id=sid,
                        columns=data.draw(OPTIONAL),
                        rows=data.draw(OPTIONAL),
                        config=data.draw(
                            st.one_of(
                                st.none(),
                                st.just({"text": "x" * 11000}),
                                st.just({"text": "ok"}),
                            )
                        ),
                    ),
                )
            )
        elif op == "layout":
            items = [
                {
                    "entity_id": data.draw(ENTITY_IDS),
                    "section_id": sid,
                    "sort_order": data.draw(INTS),
                    "columns": data.draw(OPTIONAL),
                    "rows": data.draw(OPTIONAL),
                }
            ]
            await attempt(
                lambda: areas.update_layout(
                    req, aid, SectionLayoutUpdate.model_validate({"items": items})
                )
            )
        elif op == "sync":
            ha.ha_areas = [
                {
                    "area_id": "ha_room",
                    "name": data.draw(TEXT),
                    "icon": data.draw(st.one_of(st.none(), TEXT)),
                }
            ]
            ha.entities = [
                {
                    "entity_id": f"switch.s{i}",
                    "area_id": "ha_room",
                    "name": data.draw(TEXT),
                }
                for i in range(data.draw(st.sampled_from([1, 3, 55])))
            ]
            await attempt(
                lambda: sync.sync_from_ha(
                    req, sync.SyncFromHARequest(include_entities=True)
                )
            )


async def _round_trip(data):
    path = os.path.join(tempfile.mkdtemp(prefix="hub-rt-"), "t.db")
    db = Database(path)
    await db.initialize()
    try:
        ha = _HA()
        req = _req(db, ha)
        await _apply(data, db, req, ha)
        first = (await export.export_all_areas(req)).areas
        placed = sum(
            len(sec["entities"]) + len(sec["widgets"])
            for a in first
            for sec in a["sections"]
        )
        event(
            f"areas={len(first)} items={'0' if not placed else '1-9' if placed < 10 else '10+'}"
        )
        result = await export.import_areas(
            req, ImportRequest(areas=first, overwrite=True)
        )
        assert result["failed"] == 0 and result["errors"] == [], result
        assert result["imported"] == len(first)
        again = (await export.export_all_areas(req)).areas
        assert _norm(again) == _norm(first)
    finally:
        await db.close()


@settings(
    max_examples=60,
    deadline=None,
    # No example database: it would be written into the repo tree.
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(st.data())
def test_every_accepted_write_exports_and_imports_again(data):
    with (
        mock.patch.object(areas, "_sync_sensors", _noop),
        mock.patch.object(export, "sync_all_sensors", _noop),
        mock.patch.object(sync, "sync_all_sensors", _noop),
        mock.patch.object(api_ws, "broadcast", _noop),
        mock.patch.object(api_ws, "broadcast_area_scoped", _noop),
        mock.patch.object(api_ws, "notify_areas_changed", _noop),
        mock.patch.object(areas, "sync_config_sensor", _noop),
    ):
        asyncio.run(_round_trip(data))


# ── Two cases spelled out ──────────────────────────


def test_layout_refuses_what_the_models_refuse():
    for bad in (0, 3):
        for field in ("columns", "rows"):
            try:
                SectionLayoutUpdate.model_validate(
                    {
                        "items": [
                            {
                                "entity_id": "light.a",
                                "section_id": "s",
                                "sort_order": 0,
                                field: bad,
                            }
                        ]
                    }
                )
            except ValidationError:
                continue
            raise AssertionError(f"{field}={bad} accepted")


def test_sync_name_fits_the_display_name_bounds():
    from models.schemas import AREA_DISPLAY_NAME_MAX, clip_area_display_name

    clipped = clip_area_display_name("  L\x01" + "x" * 150)
    assert len(clipped) == AREA_DISPLAY_NAME_MAX and "\x01" not in clipped
    assert clip_area_display_name(" \t ") is None
    assert AreaEntityCreate(entity_id="light.a", section_id="s", display_name=clipped)


def _run(coro_fn):
    async def go():
        path = os.path.join(tempfile.mkdtemp(prefix="hub-rt-"), "t.db")
        db = Database(path)
        await db.initialize()
        try:
            await coro_fn(db, _req(db, _HA()))
        finally:
            await db.close()

    with (
        mock.patch.object(areas, "_sync_sensors", _noop),
        mock.patch.object(export, "sync_all_sensors", _noop),
        mock.patch.object(api_ws, "broadcast", _noop),
        mock.patch.object(api_ws, "notify_areas_changed", _noop),
    ):
        asyncio.run(go())


def test_a_full_hub_exports_and_imports_again():
    """At most MAX_AREAS areas (create, copy, sync, import alike): the
    import took 100 while create had no limit, so 101 areas never imported."""

    async def check(db, req):
        for i in range(areas.MAX_AREAS):
            await areas.create_area(req, AreaCreate(name=f"R{i}"))
        try:
            await areas.create_area(req, AreaCreate(name="one more"))
            raise AssertionError("101st area created")
        except HTTPException as e:
            assert e.status_code == 400
        first = (await db.fetchone("SELECT id FROM areas"))["id"]
        try:
            await areas.copy_area(req, first)
            raise AssertionError("101st area copied")
        except HTTPException as e:
            assert e.status_code == 400
        exported = (await export.export_all_areas(req)).areas
        result = await export.import_areas(
            req, ImportRequest(areas=exported, overwrite=True)
        )
        assert (result["imported"], result["failed"]) == (areas.MAX_AREAS, 0)

    _run(check)


def test_copy_name_stays_within_the_name_bound():
    async def check(db, req):
        source = await areas.create_area(req, AreaCreate(name="N" * 100))
        copy = await areas.copy_area(req, source.id)
        again = await areas.copy_area(req, source.id)
        assert len(copy.name) <= 100 and copy.name.endswith(" (Copy)")
        assert len(again.name) <= 100 and again.name.endswith(" (Copy 2)")
        exported = (await export.export_all_areas(req)).areas
        result = await export.import_areas(
            req, ImportRequest(areas=exported, overwrite=True)
        )
        assert result["failed"] == 0, result

    _run(check)
