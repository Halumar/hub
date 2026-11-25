"""Messages from the companion integration's read-only push session."""

import time

from fastapi import WebSocket

from api.ws.registry import _clients
from services.integration_status import record_request


async def _handle_integration_message(websocket: WebSocket, msg_type: str) -> None:
    """Integration push session: READ-ONLY.

    Only ping/pong are accepted inbound; anything else — including pair,
    auth and every data/command message — is rejected. There is no
    subscribe handshake either: auth_ok implicitly subscribes to all
    events (redacted via _integration_view in broadcast()).
    """
    if msg_type == "ping":
        await websocket.send_json({"type": "pong"})
    elif msg_type == "pong":
        info = _clients.get(websocket)
        if info:
            info["last_pong"] = time.time()
            # Telemetry: each pong (30s server ping cadence < the 90s
            # connected window) keeps the integration "connected" in the
            # web UI even when WS push replaces most REST polls.
            record_request(info.get("integration_version"))
    else:
        await websocket.send_json(
            {"type": "error", "message": "integration session is read-only"}
        )
