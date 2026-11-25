"""Supervisor discovery announce for the companion HA integration.

On every startup under the Supervisor (SUPERVISOR_TOKEN present) the hub:

1. stages a new dedicated integration API token (only the hash is stored;
   it replaces the current token once the announce succeeded — see
   api.auth.stage_integration_token), and
2. announces service "halumar" to the Supervisor discovery API
   (POST /discovery) with config keys EXACTLY
   {"host", "port", "api_token", "version"}.

Home Assistant then starts the integration's ``hassio`` config-flow: first
install shows a one-click "Discovered" card; an already configured entry is
updated in place (self-healing token rotation) — the token never appears on
screen or in the clipboard.

The Supervisor deduplicates discovery messages per addon+service (a repeat
announce updates the stored config in place, keeping the same uuid), so
re-announcing on every start is safe and no uuid bookkeeping is needed.
Requires ``discovery: [halumar]`` in config.yaml — without the declaration
the Supervisor rejects the POST (403).

Never log the token plaintext.
"""

import asyncio
import logging

from models.database import Database
from namespace import DISCOVERY_SERVICE
from version import VERSION

logger = logging.getLogger("hub.services.discovery")

# DISCOVERY_SERVICE: name announced to the Supervisor; the integration's hassio
# config-flow is matched on this name (must equal the integration domain).

# The integration talks to the hub over the plain HTTP port: HA core and
# the hub share the internal Docker network, where :8099 is reachable via
# the add-on hostname (TLS/:8100 is for external, fingerprint-pinning clients).
API_PORT = 8099


async def announce_integration_discovery(db: Database, ha_client) -> bool:
    """Rotate the integration token and announce it via Supervisor discovery.

    Resolves the add-on hostname from the Supervisor self-info FIRST, so a
    failure there does not needlessly invalidate the current token. Returns
    True when the announce was posted. Raises nothing token-shaped into logs.
    """
    addon_info = await ha_client.get_addon_info()
    hostname = (addon_info.get("data") or {}).get("hostname") or ""
    if not hostname:
        logger.warning(
            "Supervisor self-info returned no hostname — skipping discovery announce"
        )
        return False

    # Stage the new token AFTER the hostname is known; it replaces the
    # current one only once the Supervisor accepted the announce.
    # The plaintext lives only in this payload (the Supervisor exposes it
    # exclusively to HA core).
    from api.auth import (
        activate_integration_token,
        discard_staged_integration_token,
        stage_integration_token,
    )

    token = await stage_integration_token(db)

    config = {
        "host": hostname,
        "port": API_PORT,
        "api_token": token,
        "version": VERSION,
    }
    try:
        await ha_client.send_discovery(DISCOVERY_SERVICE, config)
    except BaseException:
        await discard_staged_integration_token(db, token)
        raise
    await activate_integration_token(db, token)
    logger.info(
        "Announced discovery service '%s' to the Supervisor (host=%s, port=%d, version=%s)",
        DISCOVERY_SERVICE,
        hostname,
        API_PORT,
        VERSION,
    )
    return True


# Retry of a failed announce (Supervisor busy at boot): backoff with jitter.
RETRY_INITIAL_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0


async def announce_until_done(db: Database, ha_client) -> None:
    """Announce, retrying with backoff until the Supervisor accepted it.

    Runs as a background task on start, so a slow or not yet ready
    Supervisor neither delays the start nor leaves the integration without a
    token. A missing hostname ends the attempts (logged above).
    """
    from services.tasks import backoff_delay

    attempt = 0
    while True:
        try:
            await announce_integration_discovery(db, ha_client)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - retried with backoff
            delay = backoff_delay(attempt, RETRY_INITIAL_SECONDS, RETRY_MAX_SECONDS)
            attempt += 1
            logger.warning(
                "Supervisor discovery announce failed (%s); retrying in %.0fs",
                e,
                delay,
            )
            await asyncio.sleep(delay)
