"""In-memory connection telemetry for the companion HA integration.

Every REST request on ``/api/integration/*`` that authenticates with the
dedicated integration token is recorded here by the auth middleware
(``api/auth.py``), including the ``X-Hub-Integration-Version`` header the
integration sends on each request. Failed authentications on integration
paths are counted separately so the admin UI can surface a stale/rotated
token.

State is deliberately in-memory only: a reset on hub restart is
semantically correct ("connected" means "polled recently in THIS process").
``GET /api/admin/integration-status`` (``api/admin.py``) exposes it to the
web UI's System tab.
"""

import time

# Header the integration sends on every REST request (lowercased for lookup).
INTEGRATION_VERSION_HEADER = "x-hub-integration-version"

# "connected" = a successfully authenticated integration request within this
# window. 3x the integration's 30s DataUpdateCoordinator poll interval.
CONNECTED_WINDOW_SECONDS = 90

_last_request_ts: float | None = None
_request_count: int = 0
_auth_failure_count: int = 0
_integration_version: str | None = None


def record_request(integration_version: str | None = None) -> None:
    """Record a successfully authenticated integration request.

    ``integration_version`` is the raw ``X-Hub-Integration-Version``
    header value (None/"" when the header is absent — the last seen value is
    kept in that case).
    """
    global _last_request_ts, _request_count, _integration_version
    _last_request_ts = time.time()
    _request_count += 1
    if integration_version:
        _integration_version = integration_version


def record_auth_failure() -> None:
    """Record a failed authentication on an integration path."""
    global _auth_failure_count
    _auth_failure_count += 1


def get_status() -> dict:
    """Return the current telemetry snapshot.

    ``last_request_ts`` is Unix epoch seconds (None = never seen);
    ``connected`` is True when the last authenticated request is younger than
    ``CONNECTED_WINDOW_SECONDS``.
    """
    connected = (
        _last_request_ts is not None
        and (time.time() - _last_request_ts) < CONNECTED_WINDOW_SECONDS
    )
    return {
        "connected": connected,
        "last_request_ts": _last_request_ts,
        "request_count": _request_count,
        "auth_failure_count": _auth_failure_count,
        "integration_version": _integration_version,
    }


def reset() -> None:
    """Reset all counters (test helper)."""
    global _last_request_ts, _request_count, _auth_failure_count
    global _integration_version
    _last_request_ts = None
    _request_count = 0
    _auth_failure_count = 0
    _integration_version = None
