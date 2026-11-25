"""WebSocket side of the family manager.

``family_*`` messages of member devices and ``kids_request`` /
``kids_request_cancel`` of child devices; the logic lives in
``services/family/service.py``. Admin sessions and the integration session
never speak these messages (the admin uses REST, the integration
``POST /api/integration/family``).
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import WebSocket

from api.ws.registry import PSEUDO_CLIENT_IDS
from models.database import Database
from services.family import service

logger = logging.getLogger("hub.api.websocket")


async def handle(
    websocket: WebSocket,
    db: Database,
    message: dict,
    msg_type: str,
    client_id: str | None,
) -> None:
    ha_client = getattr(websocket.app.state, "ha_client", None)
    try:
        if client_id in PSEUDO_CLIENT_IDS or not client_id:
            answers = [service.FamilyError("not_permitted").message()]
        elif msg_type in service.CHILD_TYPES:
            answers = await service.handle_child_message(
                db, ha_client, client_id, message
            )
        else:
            answers = await service.handle_manager_message(
                db, ha_client, client_id, message, "home"
            )
    except Exception as e:  # noqa: BLE001 - never break the socket
        logger.error("%s failed: %s", msg_type, e, exc_info=True)
        answers = [service.FamilyError("invalid_request").message()]
    for answer in answers:
        with contextlib.suppress(Exception):
            await websocket.send_json(answer)
