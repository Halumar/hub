"""Progress of the admin setup assistant (GET/PUT /api/admin/setup-assistant)."""

import types

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from api.admin import (
    SETUP_ASSISTANT_SETTING,
    SetupAssistantState,
    get_setup_assistant,
    update_setup_assistant,
)


def _request(db, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
    )


async def test_fresh_hub_is_new(db):
    state = await get_setup_assistant(_request(db))
    assert state.model_dump() == {"status": "new", "step": "integration", "skipped": []}


async def test_progress_is_stored(db):
    saved = await update_setup_assistant(
        _request(db),
        SetupAssistantState(
            status="in_progress", step="area", skipped=["urls", "urls"]
        ),
    )
    assert saved.skipped == ["urls"]
    state = await get_setup_assistant(_request(db, auth="session"))
    assert state.model_dump() == {
        "status": "in_progress",
        "step": "area",
        "skipped": ["urls"],
    }


@pytest.mark.parametrize("status", ["dismissed", "completed"])
async def test_closed_states_persist(db, status):
    await update_setup_assistant(
        _request(db), SetupAssistantState(status=status, step="done")
    )
    assert (await get_setup_assistant(_request(db))).status == status


async def test_clients_are_rejected(db):
    with pytest.raises(HTTPException) as e:
        await get_setup_assistant(_request(db, auth="client"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await update_setup_assistant(
            _request(db, auth="integration"), SetupAssistantState()
        )
    assert e.value.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"status": "unknown"},
        {"step": "guests"},
        {"skipped": ["nope"]},
        {"status": "new", "extra": 1},
    ],
)
def test_invalid_bodies_are_rejected(body):
    with pytest.raises(ValidationError):
        SetupAssistantState.model_validate(body)


async def test_broken_stored_value_starts_over(db):
    await db.set_setting(SETUP_ASSISTANT_SETTING, "{not json")
    state = await get_setup_assistant(_request(db))
    assert state.status == "new"


def test_routes_are_registered():
    from main import app

    paths = app.openapi()["paths"]
    assert {"get", "put"} <= set(paths["/api/admin/setup-assistant"])
