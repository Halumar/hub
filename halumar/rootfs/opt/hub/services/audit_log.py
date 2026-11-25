"""Security audit logger for Halumar.

Provides a dedicated logger for security-relevant events such as:
- Authentication attempts (success/failure)
- Pairing events
- Service calls
- Token generation/revocation
- Admin actions

All events are logged to a dedicated 'hub.security' logger at INFO level,
making them easy to filter and forward to external SIEM systems.

The admin-relevant events (admin actions, admin requests, failed logins,
pairing failures, rate limits, blocked calls, revoked tokens) are also kept
in a small in-memory ring buffer for the audit view of the admin UI
(``GET /api/admin/audit``). It holds the newest ``AUDIT_BUFFER_SIZE``
entries since the hub started; the hub log keeps the full history.
"""

import itertools
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

# Dedicated security audit logger — separate from application logs
audit = logging.getLogger("hub.security")

AUDIT_BUFFER_SIZE = 500

# Category of an entry in the audit view: what an admin did ("action",
# "change" = admin write request), read it ("read") or a security signal.
CATEGORY_ACTION = "action"
CATEGORY_CHANGE = "change"
CATEGORY_READ = "read"
CATEGORY_SECURITY = "security"
CATEGORIES = (CATEGORY_ACTION, CATEGORY_CHANGE, CATEGORY_READ, CATEGORY_SECURITY)

_recent: deque[dict] = deque(maxlen=AUDIT_BUFFER_SIZE)
_ids = itertools.count(1)

# A device whose client info does not match its pairing is logged at most
# once per this many seconds (it retries with backoff).
CLIENT_INFO_MISMATCH_INTERVAL = 600
_mismatch_logged: dict[str, float] = {}


def _remember(event: str, category: str, level: str, **fields: Any) -> None:
    _recent.append(
        {
            "id": next(_ids),
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "category": category,
            "level": level,
            **{k: (None if v is None else _sanitize(v)) for k, v in fields.items()},
        }
    )


def recent_entries(
    limit: int = 200, categories: set[str] | frozenset[str] | None = None
) -> list[dict]:
    """Newest first, optionally only some categories."""
    out: list[dict] = []
    for entry in reversed(_recent):
        if categories and entry["category"] not in categories:
            continue
        out.append(dict(entry))
        if len(out) >= limit:
            break
    return out


def reset_recent() -> None:
    """Empty the buffer (tests)."""
    _recent.clear()
    _mismatch_logged.clear()


def _sanitize(value: Any) -> str:
    """Sanitize a value for safe log output (prevent log injection)."""
    s = str(value) if value else "-"
    return s.replace("\n", "\\n").replace("\r", "\\r")


def log_auth_success(
    method: str, client_id: str | None = None, ip: str | None = None
) -> None:
    """Log a successful authentication."""
    audit.info(
        "AUTH_SUCCESS method=%s client_id=%s ip=%s",
        _sanitize(method),
        _sanitize(client_id),
        _sanitize(ip),
    )


def log_auth_failure(method: str, reason: str, ip: str | None = None) -> None:
    """Log a failed authentication attempt."""
    audit.warning(
        "AUTH_FAILURE method=%s reason=%s ip=%s",
        _sanitize(method),
        _sanitize(reason),
        _sanitize(ip),
    )
    _remember(
        "auth_failure",
        CATEGORY_SECURITY,
        "warning",
        action=method,
        details=reason,
        ip=ip,
    )


def log_client_info_mismatch(
    client_id: str, field: str, paired: Any, reported: Any, ip: str | None = None
) -> bool:
    """A paired device reported client info that differs from its pairing
    (connection refused). Throttled per device; True when logged."""
    now = time.monotonic()
    last = _mismatch_logged.get(client_id)
    if last is not None and now - last < CLIENT_INFO_MISMATCH_INTERVAL:
        return False
    _mismatch_logged[client_id] = now
    audit.warning(
        "CLIENT_INFO_MISMATCH client_id=%s field=%s paired=%s reported=%s ip=%s",
        _sanitize(client_id),
        _sanitize(field),
        _sanitize(paired),
        _sanitize(reported),
        _sanitize(ip),
    )
    _remember(
        "client_info_mismatch",
        CATEGORY_SECURITY,
        "warning",
        action="ws_secret",
        details=f"{field}: paired {paired}, reported {reported}",
        client_id=client_id,
        ip=ip,
    )
    return True


def log_pairing_start(pairing_type: str, ip: str | None = None) -> None:
    """Log the start of a pairing session."""
    audit.info(
        "PAIRING_START type=%s ip=%s",
        _sanitize(pairing_type),
        _sanitize(ip),
    )


def log_pairing_success(
    client_id: str, client_name: str, is_guest: bool = False
) -> None:
    """Log a successful pairing."""
    audit.info(
        "PAIRING_SUCCESS client_id=%s client_name=%s is_guest=%s",
        _sanitize(client_id),
        _sanitize(client_name),
        is_guest,
    )


def log_pairing_failure(reason: str, ip: str | None = None) -> None:
    """Log a failed pairing attempt."""
    audit.warning(
        "PAIRING_FAILURE reason=%s ip=%s",
        _sanitize(reason),
        _sanitize(ip),
    )
    _remember("pairing_failure", CATEGORY_SECURITY, "warning", details=reason, ip=ip)


def log_pairing_qr_shown(kind: str) -> None:
    """The integration's camera showed the pairing QR (once per window).

    ``kind`` is the kind of the pairing window (guest / device), never the PIN.
    """
    audit.info("PAIRING_QR_SHOWN kind=%s", _sanitize(kind))
    _remember("pairing_qr_shown", CATEGORY_SECURITY, "info", details=kind)


def log_service_call(
    domain: str, service: str, client_id: str | None, target: Any = None
) -> None:
    """Log a service call."""
    audit.info(
        "SERVICE_CALL domain=%s service=%s client_id=%s target=%s",
        _sanitize(domain),
        _sanitize(service),
        _sanitize(client_id),
        _sanitize(target),
    )


def log_service_blocked(
    domain: str, service: str, client_id: str | None, reason: str
) -> None:
    """Log a blocked service call."""
    audit.warning(
        "SERVICE_BLOCKED domain=%s service=%s client_id=%s reason=%s",
        _sanitize(domain),
        _sanitize(service),
        _sanitize(client_id),
        _sanitize(reason),
    )
    _remember(
        "service_blocked",
        CATEGORY_SECURITY,
        "warning",
        action=f"{domain}.{service}",
        details=reason,
        client_id=client_id,
    )


def log_token_generated(token_type: str, ip: str | None = None) -> None:
    """Log token generation (API token, pairing PIN, etc.)."""
    audit.info(
        "TOKEN_GENERATED type=%s ip=%s",
        _sanitize(token_type),
        _sanitize(ip),
    )


def log_token_revoked(token_type: str, target_id: str | None = None) -> None:
    """Log token revocation."""
    audit.info(
        "TOKEN_REVOKED type=%s target_id=%s",
        _sanitize(token_type),
        _sanitize(target_id),
    )
    _remember(
        "token_revoked",
        CATEGORY_SECURITY,
        "info",
        action=token_type,
        client_id=target_id,
    )


def log_client_connected(
    client_id: str, ip: str | None = None, is_guest: bool = False
) -> None:
    """Log a client WebSocket connection."""
    audit.info(
        "CLIENT_CONNECTED client_id=%s ip=%s is_guest=%s",
        _sanitize(client_id),
        _sanitize(ip),
        is_guest,
    )


def log_client_disconnected(client_id: str, reason: str | None = None) -> None:
    """Log a client WebSocket disconnection."""
    audit.info(
        "CLIENT_DISCONNECTED client_id=%s reason=%s",
        _sanitize(client_id),
        _sanitize(reason) if reason else "normal",
    )


def log_rate_limited(action: str, ip: str | None = None) -> None:
    """Log a rate-limited request."""
    audit.warning(
        "RATE_LIMITED action=%s ip=%s",
        _sanitize(action),
        _sanitize(ip),
    )
    _remember("rate_limited", CATEGORY_SECURITY, "warning", action=action, ip=ip)


def log_admin_action(
    action: str, details: str | None = None, ip: str | None = None
) -> None:
    """Log an admin action."""
    audit.info(
        "ADMIN_ACTION action=%s details=%s ip=%s",
        _sanitize(action),
        _sanitize(details),
        _sanitize(ip),
    )
    _remember(
        "admin_action", CATEGORY_ACTION, "info", action=action, details=details, ip=ip
    )


def log_admin_access(
    method: str,
    path: str,
    auth_type: str,
    ip: str | None = None,
    user: str | None = None,
) -> None:
    """Log an admin-level request (Ingress, session, admin token)."""
    audit.info(
        "ADMIN_ACCESS method=%s path=%s auth=%s user=%s ip=%s",
        _sanitize(method),
        _sanitize(path),
        _sanitize(auth_type),
        _sanitize(user),
        _sanitize(ip),
    )
    _remember(
        "admin_access",
        CATEGORY_READ
        if method.upper() in ("GET", "HEAD", "OPTIONS")
        else CATEGORY_CHANGE,
        "info",
        action=f"{method.upper()} {path}",
        auth=auth_type,
        user=user,
        ip=ip,
    )
