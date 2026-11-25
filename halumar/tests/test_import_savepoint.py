"""An import keeps the good areas when one area fails.

import_areas wraps each area in its own SAVEPOINT, so one malformed area cannot
undo the good ones, and only persisted areas are counted as imported. A widget
whose config is neither a dict nor a string is skipped; its area stays.

Drives the import_areas endpoint directly with a fake admin request; the HA
sensor sync is mocked.
"""

from types import SimpleNamespace

import api.export as export
from api.export import import_areas
from models.schemas import ImportRequest


async def _noop(*_a, **_k):
    return None


def _admin_request(db):
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="admin"),
        app=SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None)),
    )


async def _names(db):
    rows = await db.fetchall("SELECT name FROM areas")
    return {r["name"] for r in rows}


class TestPerAreaSavepoint:
    async def test_one_bad_area_does_not_wipe_good_areas(self, db, monkeypatch):
        """A malformed area is rolled back alone; good areas persist and count."""
        monkeypatch.setattr(export, "sync_all_sensors", _noop)
        req = _admin_request(db)

        # "tags": 123 is non-iterable → TypeError mid-import for that area only.
        data = ImportRequest(
            areas=[
                {"name": "Good One"},
                {"name": "Bad One", "tags": 123},
                {"name": "Good Two"},
            ]
        )
        result = await import_areas(req, data)

        assert result["imported"] == 2  # count matches what actually persisted
        assert len(result["errors"]) == 1
        names = await _names(db)
        assert "Good One" in names
        assert "Good Two" in names
        assert "Bad One" not in names  # rolled back, not left half-inserted

    async def test_bad_area_first_still_persists_later_good_areas(
        self, db, monkeypatch
    ):
        monkeypatch.setattr(export, "sync_all_sensors", _noop)
        req = _admin_request(db)
        data = ImportRequest(
            areas=[
                {"name": "Bad", "tags": 5},
                {"name": "Good A"},
                {"name": "Good B"},
            ]
        )
        result = await import_areas(req, data)

        assert result["imported"] == 2
        assert await _names(db) == {"Good A", "Good B"}

    async def test_all_good_areas_import(self, db, monkeypatch):
        monkeypatch.setattr(export, "sync_all_sensors", _noop)
        req = _admin_request(db)
        data = ImportRequest(areas=[{"name": "A"}, {"name": "B"}, {"name": "C"}])
        result = await import_areas(req, data)
        assert result["imported"] == 3
        assert result["errors"] == []
        assert await _names(db) == {"A", "B", "C"}


class TestWidgetConfigGuard:
    async def test_non_dict_config_skips_widget_but_keeps_area(self, db, monkeypatch):
        """A widget whose config is a non-dict/str (int) is skipped, not fatal."""
        monkeypatch.setattr(export, "sync_all_sensors", _noop)
        req = _admin_request(db)
        data = ImportRequest(
            areas=[
                {
                    "name": "Area With Bad Widget",
                    "sections": [
                        {
                            "title": "S",
                            "widgets": [{"widget_type": "clock", "config": 123}],
                        }
                    ],
                }
            ]
        )
        result = await import_areas(req, data)

        # Area still imports; only the bad widget is skipped with an error.
        assert result["imported"] == 1
        assert any("invalid config type" in e for e in result["errors"])
        assert "Area With Bad Widget" in await _names(db)
        # No widget row was written for the skipped widget.
        widgets = await db.fetchall("SELECT id FROM area_widgets")
        assert widgets == []
