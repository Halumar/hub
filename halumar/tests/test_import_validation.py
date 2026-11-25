"""Import validates like the REST endpoints and tells everyone.

``POST /api/import/areas`` reports and does not store values the readers
cannot handle (a widget config that is no JSON object, NULL integers, an
unknown ``control_mode``), and announces created and replaced areas, so the
admin UI, the integration and the apps see the import without a reload.
"""

from types import SimpleNamespace

import pytest

import api.export as export
from api import ws as api_ws
from api.export import export_all_areas, import_areas
from models.schemas import ImportRequest


async def _noop(*_a, **_k):
    return None


@pytest.fixture
def req(db, monkeypatch):
    monkeypatch.setattr(export, "sync_all_sensors", _noop)
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="admin"),
        app=SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None)),
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def _broadcast(message, audience=api_ws.EventAudience.ALL):
        calls.append(("broadcast", message["type"], audience))

    async def _areas_changed(db, area=None):
        calls.append(("areas_changed", area, None))
        return 1

    async def _scoped(db, area_id, message):
        calls.append(("scoped", message["type"], area_id))
        return 1

    monkeypatch.setattr(api_ws, "broadcast", _broadcast)
    monkeypatch.setattr(api_ws, "notify_areas_changed", _areas_changed)
    monkeypatch.setattr(api_ws, "broadcast_area_scoped", _scoped)
    return calls


class _HA:
    async def get_state(self, _entity_id):
        return None


async def _readers_work(db, req):
    """Admin export and the app's area detail read every stored row."""
    from api.ws.client import _get_area_detail

    await export_all_areas(req)
    for row in await db.fetchall("SELECT id FROM areas"):
        await _get_area_detail(db, _HA(), row["id"])


def _area(name, **section):
    return {"name": name, "sections": [{"title": "s", **section}]}


@pytest.mark.parametrize(
    "bad",
    [
        _area("Null sort", sort_order=None),
        _area("Null columns", columns=None),
        _area("Section too wide", columns=3),
        _area("Entity null", entities=[{"entity_id": "light.a", "columns": None}]),
        _area(
            "Entity mode", entities=[{"entity_id": "light.a", "control_mode": "bogus"}]
        ),
        _area("Entity id", entities=[{"entity_id": "not an id"}]),
        _area("Entity visible", entities=[{"entity_id": "light.a", "is_visible": []}]),
        _area("Widget null", widgets=[{"widget_type": "text", "rows": None}]),
    ],
    ids=lambda a: a["name"],
)
async def test_invalid_entry_is_reported_not_stored(db, req, sent, bad):
    result = await import_areas(req, ImportRequest(areas=[{"name": "Good"}, bad]))
    assert result["imported"] == 1
    assert result["failed"] == 1
    assert bad["name"] in result["errors"][0]
    names = {r["name"] for r in await db.fetchall("SELECT name FROM areas")}
    assert names == {"Good"}
    await _readers_work(db, req)


async def test_widget_config_must_be_an_object(db, req, sent):
    data = ImportRequest(
        areas=[_area("A", widgets=[{"widget_type": "text", "config": "not json"}])]
    )
    result = await import_areas(req, data)
    assert result["imported"] == 1
    assert any("invalid config type" in e for e in result["errors"])
    assert await db.fetchall("SELECT id FROM area_widgets") == []
    await _readers_work(db, req)


async def test_export_round_trip_keeps_values(db, req, sent):
    data = ImportRequest(
        areas=[
            _area(
                "Kitchen",
                sort_order=3,
                column_span=2,
                is_collapsed=1,
                entities=[
                    {
                        "entity_id": "light.a",
                        "display_name": "  Lamp ",
                        "columns": 2,
                        "rows": 1,
                        "is_visible": 0,
                        "control_mode": "full",
                    }
                ],
                widgets=[{"widget_type": "text", "config": {"text": "hi"}, "rows": 2}],
            )
        ]
    )
    assert (await import_areas(req, data))["imported"] == 1
    exported = (await export_all_areas(req)).areas
    section = exported[0]["sections"][0]
    assert (section["sort_order"], section["column_span"], section["is_collapsed"]) == (
        3,
        2,
        1,
    )
    entity = section["entities"][0]
    assert entity["display_name"] == "Lamp"  # trimmed like the REST endpoint
    assert (entity["columns"], entity["is_visible"], entity["control_mode"]) == (
        2,
        0,
        "full",
    )
    assert section["widgets"][0]["config"] == {"text": "hi"}
    # The export imports again unchanged (overwrite).
    again = await import_areas(req, ImportRequest(areas=exported, overwrite=True))
    assert again["imported"] == 1 and again["errors"] == []


async def test_import_announces_created_and_replaced_areas(db, req, sent):
    await import_areas(req, ImportRequest(areas=[{"name": "A"}]))
    area_id = (await db.fetchone("SELECT id FROM areas"))["id"]
    assert sent == [
        ("broadcast", "area_created", api_ws.EventAudience.ADMIN),
        ("areas_changed", None, None),
    ]
    sent.clear()
    await import_areas(req, ImportRequest(areas=[{"name": "A"}], overwrite=True))
    assert sent == [("scoped", "area_updated", area_id), ("areas_changed", None, None)]


async def test_skipped_import_announces_nothing(db, req, sent):
    await import_areas(req, ImportRequest(areas=[{"name": "A"}]))
    sent.clear()
    result = await import_areas(req, ImportRequest(areas=[{"name": "A"}]))
    assert result["skipped"] == 1
    assert sent == []
