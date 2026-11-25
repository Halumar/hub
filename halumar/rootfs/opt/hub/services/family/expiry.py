"""Planner of the family manager: releases end and requests time out
on time; old activity is pruned. Catches up at once after a restart."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from models.database import Database
from services.family import service
from services.family import store as fstore

logger = logging.getLogger("hub.family.expiry")

MAX_SLEEP_SECONDS = 30.0
PRUNE_EVERY_SECONDS = 3600.0


async def run_once(db: Database, ha_client) -> None:
    await service.expire_requests(db, ha_client)
    await service.expire_releases(db, ha_client)


async def _next_due(db: Database) -> datetime | None:
    times = []
    row = await db.fetchone("SELECT MIN(until) AS t FROM kids_releases")
    if row and row["t"]:
        times.append(fstore.parse(row["t"]))
    row = await db.fetchone(
        "SELECT MIN(expires_at) AS t FROM kids_requests WHERE status = 'open'"
    )
    if row and row["t"]:
        times.append(fstore.parse(row["t"]))
    times = [t for t in times if t is not None]
    return min(times) if times else None


async def expiry_loop(app) -> None:
    """Supervised task."""
    db: Database = app.state.db
    last_prune = 0.0
    loop = asyncio.get_running_loop()
    while True:
        ha_client = getattr(app.state, "ha_client", None)
        nxt = None
        try:
            await run_once(db, ha_client)
            if loop.time() - last_prune > PRUNE_EVERY_SECONDS:
                async with db.transaction():
                    await fstore.prune(db)
                last_prune = loop.time()
            nxt = await _next_due(db)
        except Exception as e:  # noqa: BLE001 - the supervisor restarts on crash only
            logger.error("Family planner failed: %s", e)
        delay = MAX_SLEEP_SECONDS
        if nxt is not None:
            delay = min(
                MAX_SLEEP_SECONDS,
                max(0.5, (nxt - fstore.utcnow()).total_seconds()),
            )
        await asyncio.sleep(delay)
