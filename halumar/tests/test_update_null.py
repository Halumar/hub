"""Explicit ``null`` in an update body.

In ``PUT /api/areas/{id}`` and the section, entity and widget updates, an
explicit ``null`` for a field that cannot be NULL (``grid_cols``,
``is_active`` ...) is rejected (422), so a stored area always reads back; a
null that means something (icon, color, control_mode, display_name, tile)
goes through.
"""

import pytest
from pydantic import ValidationError

from models.schemas import (
    AreaEntityUpdate,
    AreaUpdate,
    AreaWidgetUpdate,
    SectionUpdate,
)

NOT_NULL = (
    [
        (AreaUpdate, f)
        for f in (
            "name",
            "grid_cols",
            "dense_section_placement",
            "sort_order",
            "is_active",
        )
    ]
    + [
        (SectionUpdate, f)
        for f in ("title", "sort_order", "column_span", "columns", "is_collapsed")
    ]
    + [
        (AreaEntityUpdate, f)
        for f in ("section_id", "columns", "rows", "sort_order", "is_visible")
    ]
    + [
        (AreaWidgetUpdate, f)
        for f in (
            "section_id",
            "widget_type",
            "config",
            "columns",
            "rows",
            "sort_order",
        )
    ]
)

NULL_MEANS_SOMETHING = [
    (AreaUpdate, "icon"),
    (AreaUpdate, "description"),
    (AreaUpdate, "color"),
    (AreaUpdate, "control_mode"),
    (AreaUpdate, "card_image_url"),
    (SectionUpdate, "icon"),
    (AreaEntityUpdate, "display_name"),
    (AreaEntityUpdate, "control_mode"),
    (AreaEntityUpdate, "tile"),
]


@pytest.mark.parametrize(
    "model,field", NOT_NULL, ids=lambda x: getattr(x, "__name__", x)
)
def test_null_for_not_null_column_is_rejected(model, field):
    with pytest.raises(ValidationError) as exc:
        model.model_validate({field: None})
    err = exc.value.errors()[0]
    assert err["type"] == "null_not_allowed"
    assert err["loc"] == (field,)


@pytest.mark.parametrize(
    "model,field", NULL_MEANS_SOMETHING, ids=lambda x: getattr(x, "__name__", x)
)
def test_meaningful_null_is_kept(model, field):
    m = model.model_validate({field: None})
    assert field in m.model_fields_set
    assert getattr(m, field) is None


def test_unsent_fields_stay_unset():
    assert AreaUpdate.model_validate({}).model_dump(exclude_unset=True) == {}


async def test_area_list_survives_rejected_null(db_with_area):
    """End to end over HTTP: 422, nothing stored, the list still loads."""
    import httpx

    import main

    db, area_id, _name = db_with_area
    main.app.state.db = db
    main.app.state.ha_client = None
    main.app.state.startup_error = None
    transport = httpx.ASGITransport(app=main.app, client=("172.30.32.2", 40000))
    headers = {"X-Ingress-Path": "/x"}
    async with httpx.AsyncClient(transport=transport, base_url="http://hub") as c:
        r = await c.put(
            f"/api/areas/{area_id}",
            json={"grid_cols": None, "is_active": None},
            headers=headers,
        )
        assert r.status_code == 422, r.text
        listed = await c.get("/api/areas", headers=headers)
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()] == [area_id]
    row = await db.fetchone(
        "SELECT grid_cols, is_active FROM areas WHERE id = ?", (area_id,)
    )
    assert row["is_active"] == 1
