"""Sync and app URL changes reach connected sessions.

``POST /api/sync/from-ha`` and ``/api/sync/area/{id}/entities`` announce the
areas they add or change to the admin UI, the integration and the apps.
``PUT /api/settings/app-url`` changes ``image_base_url`` of the area list and
tells the apps to reload it.
"""

from types import SimpleNamespace

import pytest

import api.pairing as pairing
import api.sync as sync
from api import ws as api_ws


class _FakeHA:
    def __init__(self, ha_areas, entities=()):
        self._areas = list(ha_areas)
        self._entities = list(entities)

    async def get_areas(self):
        return self._areas

    async def get_entity_registry(self):
        return self._entities

    async def get_devices(self):
        return []


def _request(db, ha_client=None):
    state = SimpleNamespace(db=db, ha_client=ha_client, app_url="")
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="admin"), app=SimpleNamespace(state=state)
    )


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def _broadcast(message, audience=api_ws.EventAudience.ALL):
        calls.append(("broadcast", message, audience))

    async def _areas_changed(db, area=None):
        calls.append(("areas_changed", area, None))
        return 1

    async def _scoped(db, area_id, message):
        calls.append(("scoped", message, area_id))
        return 1

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(api_ws, "broadcast", _broadcast)
    monkeypatch.setattr(api_ws, "notify_areas_changed", _areas_changed)
    monkeypatch.setattr(api_ws, "broadcast_area_scoped", _scoped)
    monkeypatch.setattr(sync, "sync_all_sensors", _noop)
    return calls


async def test_sync_from_ha_announces_new_areas(db, sent):
    ha = _FakeHA([{"area_id": "office", "name": "Büro"}])
    result = await sync.sync_from_ha(_request(db, ha), None)
    assert result.created == 1
    created = [c for c in sent if c[0] == "broadcast"]
    assert len(created) == 1
    assert created[0][1]["type"] == "area_created"
    assert created[0][2] == api_ws.EventAudience.ADMIN
    assert ("areas_changed", {"is_active": True, "tags": []}, None) in sent


async def test_sync_from_ha_announces_changed_areas_only(db, sent):
    ha = _FakeHA(
        [{"area_id": "office", "name": "Büro"}, {"area_id": "hall", "name": "Flur"}]
    )
    await sync.sync_from_ha(_request(db, ha), None)
    sent.clear()

    # Same names: nothing to tell.
    await sync.sync_from_ha(_request(db, ha), None)
    assert sent == []

    # The office gets an icon in HA: the area has none, the sync fills it.
    with_icon = _FakeHA(
        [
            {"area_id": "office", "name": "Büro", "icon": "mdi:desk"},
            {"area_id": "hall", "name": "Flur"},
        ]
    )
    result = await sync.sync_from_ha(_request(db, with_icon), None)
    office = await db.fetchone("SELECT id FROM areas WHERE ha_area_id = 'office'")
    assert result.updated == 1
    assert sent == [
        (
            "scoped",
            {"type": "area_updated", "data": {"area_id": office["id"]}},
            office["id"],
        )
    ]


async def test_sync_from_ha_keeps_name_and_icon_of_existing_areas(db, sent):
    """The admin's name and icon survive every later sync."""
    ha = _FakeHA([{"area_id": "office", "name": "Büro", "icon": "mdi:desk"}])
    await sync.sync_from_ha(_request(db, ha), None)
    office = await db.fetchone("SELECT id FROM areas WHERE ha_area_id = 'office'")
    await db.execute(
        "UPDATE areas SET name = 'Arbeitszimmer', icon = 'mdi:laptop' WHERE id = ?",
        (office["id"],),
    )
    await db.commit()
    sent.clear()

    renamed = _FakeHA([{"area_id": "office", "name": "Office", "icon": "mdi:chair"}])
    result = await sync.sync_from_ha(_request(db, renamed), None)
    row = await db.fetchone(
        "SELECT name, icon FROM areas WHERE id = ?", (office["id"],)
    )
    assert (row["name"], row["icon"]) == ("Arbeitszimmer", "mdi:laptop")
    assert (result.synced, result.created, result.updated) == (1, 0, 0)
    assert sent == []


async def test_entities_of_a_deleted_section_stay_out_of_the_sync(
    db, sent, monkeypatch
):
    """Deleting a section is like removing its entities."""
    import api.areas as areas
    from models.schemas import SectionCreate

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(areas, "_sync_sensors", _noop)
    ha = _FakeHA(
        [{"area_id": "office", "name": "Büro"}],
        entities=[
            {"entity_id": "light.desk", "area_id": "office"},
            {"entity_id": "light.shelf", "area_id": "office"},
        ],
    )
    body = sync.SyncFromHARequest(include_entities=True)
    await sync.sync_from_ha(_request(db, ha), body)
    office = await db.fetchone("SELECT id FROM areas WHERE ha_area_id = 'office'")
    req = _request(db, ha)
    req.client = SimpleNamespace(host="127.0.0.1")
    extra = await areas.create_section(req, office["id"], SectionCreate(title="Desk"))
    await db.execute(
        "UPDATE area_entities SET section_id = ? WHERE entity_id = 'light.desk'",
        (extra.id,),
    )
    await db.commit()
    await areas.delete_section(req, office["id"], extra.id)

    result = await sync.sync_from_ha(_request(db, ha), body)
    assert result.entities_added == 0
    rows = await db.fetchall(
        "SELECT entity_id FROM area_entities WHERE area_id = ?", (office["id"],)
    )
    assert [r["entity_id"] for r in rows] == ["light.shelf"]
    # The per-area sync respects it too.
    again = await sync.sync_area_entities_from_ha(_request(db, ha), office["id"], None)
    assert again.entities_added == 0


async def test_sync_area_entities_announces_added_entities(db, sent):
    ha = _FakeHA(
        [{"area_id": "office", "name": "Büro"}],
        entities=[{"entity_id": "light.desk", "area_id": "office"}],
    )
    await sync.sync_from_ha(_request(db, ha), None)
    office = await db.fetchone("SELECT id FROM areas WHERE ha_area_id = 'office'")
    sent.clear()

    result = await sync.sync_area_entities_from_ha(_request(db, ha), office["id"], None)
    assert result.entities_added == 1
    assert sent == [
        (
            "scoped",
            {"type": "area_updated", "data": {"area_id": office["id"]}},
            office["id"],
        )
    ]

    sent.clear()
    result = await sync.sync_area_entities_from_ha(_request(db, ha), office["id"], None)
    assert result.entities_added == 0
    assert sent == []


async def test_app_url_change_refreshes_the_area_lists(db, sent):
    await pairing.update_app_url(
        _request(db), pairing.AppUrlUpdate(app_url="https://hub.example.com/")
    )
    assert ("areas_changed", None, None) in sent
    sent.clear()
    await pairing.update_app_url(_request(db), pairing.AppUrlUpdate(app_url=""))
    assert ("areas_changed", None, None) in sent
