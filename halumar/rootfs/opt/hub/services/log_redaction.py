"""Logging setup without secrets.

The hub log is readable over ``/api/admin/logs``, in the HA UI and ends up
in bug reports, so it must never carry credentials:

* uvicorn logs WebSocket handshakes with the full query string even at
  ``info`` (``"WebSocket /api/ws?token=…" [accepted]``): every query string
  in a uvicorn record is replaced.
* Generic patterns (``Bearer …``, ``token=…``, ``"secret": "…"``, JWTs) are
  masked in every record of every logger.
* Libraries that dump raw protocol data at DEBUG (``websockets``: handshake
  headers incl. ``Authorization`` and frame excerpts incl. the
  ``SUPERVISOR_TOKEN``; ``aiosqlite``: SQL parameters) never log below INFO,
  whatever ``log_level`` the hub runs with.

Redaction runs in a log-record factory, so it applies regardless of which
handler (ours or uvicorn's own ``dictConfig``) finally writes the record.
"""

from __future__ import annotations

import logging
import re
import sys

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

REDACTED = "***"

# Loggers that dump raw protocol/SQL data at DEBUG.
_QUIET_AT_DEBUG = ("websockets", "aiosqlite")
# Chatty HTTP client loggers (every Supervisor poll at INFO).
_WARNING_ONLY = ("httpx", "httpcore")

_SECRET_KEYS = (
    r"access_token|refresh_token|api_token|integration_token|ha_token|"
    r"client_secret|secret|password|token|pin"
)

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization headers in any form.
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 " + REDACTED),
    # key=value in query strings and form-like text.
    (
        re.compile(rf"(?i)([?&;\s](?:{_SECRET_KEYS})=)[^&\s\"'#]+"),
        r"\1" + REDACTED,
    ),
    # "key": "value" / 'key': 'value' / key: value in JSON and dict reprs.
    (
        re.compile(
            rf"(?i)([\"']?\b(?:{_SECRET_KEYS})[\"']?\s*:\s*[\"'])[^\"']+([\"'])"
        ),
        r"\1" + REDACTED + r"\2",
    ),
    # JSON Web Tokens (HA LLATs) wherever they appear.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+"),
        REDACTED,
    ),
)

# Any query string in a request line/path logged by uvicorn.
_UVICORN_QUERY = re.compile(r"(/[^\s\"?]*)\?[^\s\"]*")

_installed = False


def redact(text: str, *, strip_query: bool = False) -> str:
    """Return ``text`` with credentials masked."""
    if strip_query:
        text = _UVICORN_QUERY.sub(r"\1?" + REDACTED, text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_record(record: logging.LogRecord) -> None:
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a broken format must not kill logging
        return
    clean = redact(message, strip_query=record.name.startswith("uvicorn"))
    if clean != message:
        record.msg = clean
        record.args = None


def install_record_redaction() -> None:
    """Wrap the log-record factory once so every record is redacted."""
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        _redact_record(record)
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def configure_logging(level_name: str) -> None:
    """Root logging for the hub process (called once from ``main()``)."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stdout)
    for name in _WARNING_ONLY:
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in _QUIET_AT_DEBUG:
        logging.getLogger(name).setLevel(max(level, logging.INFO))
    install_record_redaction()
