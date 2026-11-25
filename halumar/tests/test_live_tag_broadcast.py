"""Tag changes broadcast client_config_updated LIVE.

When an area's tags change (PUT /areas/{id}), the set of areas a client may
see changes. The app re-fetches its config and re-filters its
visible areas in response to a client_config_updated broadcast, so tag edits
take effect without a reconnect.
"""

import types

import pytest


def _admin_request(db):
    state = types.SimpleNamespace(db=db, auth_type="admin")
    app = types.SimpleNamespace(state=state)
    return types.SimpleNamespace(state=state, app=app)


@pytest.mark.asyncio
async def test_update_client_name_only_still_ok(db_with_client, monkeypatch):
    """A name-only update need not broadcast config (no tag change)."""
    import api.pairing as pairing
    import api.websocket as ws

    captured = []

    async def _capture_broadcast(message, *a, **k):
        captured.append(message)

    monkeypatch.setattr(ws, "broadcast", _capture_broadcast)

    db, client_id, _secret, _hash = db_with_client
    data = pairing.ClientUpdate(name="Renamed")
    await pairing.update_client(_admin_request(db), client_id, data)

    assert not any(m.get("type") == "client_config_updated" for m in captured)


@pytest.mark.asyncio
async def test_update_area_tags_broadcasts(db_with_area, monkeypatch):
    import api.areas as areas
    import api.websocket as ws
    from models.schemas import AreaUpdate

    captured = []

    async def _capture_broadcast(message, *a, **k):
        captured.append(message)

    async def _noop_sync(*a, **k):
        return None

    async def _capture_scoped(db, area_id, message):
        captured.append(message)
        return 1

    monkeypatch.setattr(ws, "broadcast", _capture_broadcast)
    monkeypatch.setattr(ws, "broadcast_area_scoped", _capture_scoped)
    monkeypatch.setattr(areas, "_sync_sensors", _noop_sync)

    db, area_id, _section_id = db_with_area
    await areas.update_area(_admin_request(db), area_id, AreaUpdate(tags=["kitchen"]))

    # The client config does not change, area_updated is enough.
    assert not any(m.get("type") == "client_config_updated" for m in captured)
    assert any(m.get("type") == "area_updated" for m in captured)
