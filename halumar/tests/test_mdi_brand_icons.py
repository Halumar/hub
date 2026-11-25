"""Brand/logo icons are not shipped (Pictogrammers Free License).

The MDI tables (hub ``data/mdi_paths.py`` and the Admin UI catalog
``frontend/src/lib/data/mdi-icons.js``) come from ``frontend/scripts/gen-mdi-icons.mjs``,
which leaves out every icon tagged "Brand / Logo" (and every deprecated icon) in
@mdi/svg meta.json. Stored values that still name such an icon (areas, tiles,
notifications) resolve to the neutral FALLBACK_MDI_ICON; new tile settings reject them.
"""

import json
import re
from pathlib import Path

from data.mdi_paths import (
    FALLBACK_MDI_ICON,
    MDI_PATHS,
    REMOVED_MDI_ICONS,
    get_mdi_path,
)
from models.schemas import TileSettings
from services import tiles

from api.websocket import _handle_message

from .test_tiles import _area, _entities, _FakeHA, _FakeWS, _get_area

FRONTEND_ICONS = (
    Path(__file__).resolve().parents[2] / "frontend/src/lib/data/mdi-icons.js"
)
FALLBACK = MDI_PATHS[FALLBACK_MDI_ICON]


def _frontend_names() -> list[str]:
    text = FRONTEND_ICONS.read_text(encoding="utf-8")
    return [
        json.loads(m.group(0))[0]
        for m in re.finditer(r"^\[.*\](?=,$)", text, re.MULTILINE)
    ]


class TestTables:
    def test_brand_icons_are_not_shipped(self):
        for name in ("home-assistant", "apple", "google", "spotify", "netflix"):
            assert name in REMOVED_MDI_ICONS
            assert name not in MDI_PATHS
        assert not REMOVED_MDI_ICONS & MDI_PATHS.keys()

    def test_hub_and_admin_ui_ship_the_same_set(self):
        names = _frontend_names()
        assert names == sorted(MDI_PATHS)
        assert not REMOVED_MDI_ICONS & set(names)

    def test_official_names(self):
        # @mdi/svg meta.json names (HA uses mdi:battery-10, not battery10).
        assert "battery-10" in MDI_PATHS and "battery10" not in MDI_PATHS
        assert "home-floor-1" in MDI_PATHS


class TestLookup:
    def test_removed_icon_falls_back_to_neutral_icon(self):
        assert get_mdi_path("home-assistant") == FALLBACK
        assert get_mdi_path("mdi:home-assistant") == FALLBACK
        assert get_mdi_path("home-assistant", fallback=False) is None

    def test_unknown_and_known_icons_unchanged(self):
        assert get_mdi_path("does-not-exist-anywhere") is None
        assert get_mdi_path("mdi:floor-lamp") == MDI_PATHS["floor-lamp"]
        assert get_mdi_path("") is None


def test_tile_setting_with_brand_icon_is_rejected():
    errors = tiles.capability_errors("light.x", TileSettings(icon="mdi:home-assistant"))
    assert [e["type"] for e in errors] == ["tile.icon_unknown"]


async def test_stored_tile_icon_falls_back(db):
    area_id, _ = await _area(db)
    await db.execute(
        "UPDATE area_entities SET tile = ? WHERE area_id = ?",
        (json.dumps({"icon": "mdi:home-assistant"}), area_id),
    )
    await db.commit()
    reply = await _get_area(db, _FakeHA(), "ingress", area_id)
    tile = _entities(reply)["light.stehlampe"]["tile"]
    assert tile["icon"] == "mdi:home-assistant"
    assert tile["icon_svg"] == FALLBACK


async def test_stored_area_icon_falls_back(db):
    area_id, _ = await _area(db)
    await db.execute(
        "UPDATE areas SET icon = 'mdi:home-assistant' WHERE id = ?", (area_id,)
    )
    await db.commit()
    detail = await _get_area(db, _FakeHA(), "ingress", area_id)
    assert detail["data"]["icon_svg"] == FALLBACK

    ws = _FakeWS(db, _FakeHA())
    await _handle_message(
        ws, {"type": "get_areas"}, authenticated=True, client_id="ingress"
    )
    listed = {a["id"]: a for a in ws.sent[-1]["data"]}
    assert listed[area_id]["icon_svg"] == FALLBACK
