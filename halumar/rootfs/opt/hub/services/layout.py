"""Area layout loading shared by the REST area endpoints and WS get_area."""

import json

from models.database import Database


async def load_sections_with_items(db: Database, area_id: str) -> list[dict]:
    """Get all sections for an area, each with its entities and widgets."""
    sections = await db.fetchall(
        "SELECT * FROM area_sections WHERE area_id = ? ORDER BY sort_order",
        (area_id,),
    )

    result = []
    for section in sections:
        section_id = section["id"]

        # Fetch entities for this section
        entities = await db.fetchall(
            "SELECT * FROM area_entities WHERE section_id = ? ORDER BY sort_order",
            (section_id,),
        )

        # Fetch widgets for this section
        widgets_raw = await db.fetchall(
            "SELECT * FROM area_widgets WHERE section_id = ? ORDER BY sort_order",
            (section_id,),
        )
        widgets = []
        for w in widgets_raw:
            w_dict = dict(w)
            if isinstance(w_dict.get("config"), str):
                w_dict["config"] = json.loads(w_dict["config"])
            widgets.append(w_dict)

        result.append(
            {
                **dict(section),
                "entities": [dict(e) for e in entities],
                "widgets": widgets,
            }
        )

    return result
