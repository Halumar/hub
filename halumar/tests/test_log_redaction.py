"""No credentials in the hub log."""

import logging

import pytest

from services import log_redaction
from services.log_redaction import configure_logging, redact

FP = "5a8e8ee2" + "0" * 51 + "0d073"


@pytest.fixture
def redacting(monkeypatch):
    previous = logging.getLogRecordFactory()
    monkeypatch.setattr(log_redaction, "_installed", False)
    log_redaction.install_record_redaction()
    yield
    logging.setLogRecordFactory(previous)


def _emit(caplog, name, msg, *args, level=logging.INFO):
    caplog.clear()
    logger = logging.getLogger(name)
    with caplog.at_level(logging.DEBUG, logger=name):
        logger.log(level, msg, *args)
    return caplog.records[-1].getMessage()


class TestRecordRedaction:
    def test_uvicorn_ws_query_string_masked(self, redacting, caplog):
        line = _emit(
            caplog,
            "uvicorn.error",
            '%s - "WebSocket %s" [accepted]',
            "192.0.2.1:5000",
            "/api/ws?token=" + "ab" * 32 + "&x=1",
        )
        assert "ab" * 32 not in line
        assert '"WebSocket /api/ws?***" [accepted]' in line

    def test_uvicorn_access_query_masked(self, redacting, caplog):
        line = _emit(
            caplog,
            "uvicorn.access",
            '%s - "%s %s HTTP/%s" %d',
            "192.0.2.1:5000",
            "GET",
            "/api/x?anything=secretvalue",
            "1.1",
            200,
        )
        assert "secretvalue" not in line

    def test_generic_patterns_in_any_logger(self, redacting, caplog):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJhYmMifQ.c2lnbmF0dXJlLXZhbHVl"
        line = _emit(
            caplog,
            "halumar.test",
            "hdr %s body %s jwt %s",
            "Authorization: Bearer abc.def-123",
            '{"type": "pair", "pin": "1234-5678", "secret": "s3cr3t", "access_token": "t0k"}',
            jwt,
        )
        for leaked in ("abc.def-123", "1234-5678", "s3cr3t", "t0k", jwt):
            assert leaked not in line
        assert "Bearer ***" in line

    def test_fingerprint_and_plain_text_untouched(self, redacting, caplog):
        line = _emit(
            caplog, "halumar.test", "TLS fingerprint %s for %s", FP, "secret_hash"
        )
        assert line == f"TLS fingerprint {FP} for secret_hash"

    def test_install_is_idempotent(self, redacting):
        factory = logging.getLogRecordFactory()
        log_redaction.install_record_redaction()
        assert logging.getLogRecordFactory() is factory


def test_redact_helper():
    assert redact("GET /a?token=x HTTP", strip_query=True) == "GET /a?*** HTTP"
    assert redact("url=/p?pin=1234-5678&a=b") == "url=/p?pin=***&a=b"


def test_debug_level_keeps_protocol_loggers_quiet(monkeypatch):
    previous = logging.getLogRecordFactory()
    monkeypatch.setattr(log_redaction, "_installed", False)
    saved = {
        n: logging.getLogger(n).level for n in ("websockets", "aiosqlite", "httpx")
    }
    try:
        configure_logging("debug")
        assert logging.getLogger("websockets").getEffectiveLevel() == logging.INFO
        assert (
            logging.getLogger("websockets.client").getEffectiveLevel() == logging.INFO
        )
        assert logging.getLogger("aiosqlite").getEffectiveLevel() == logging.INFO
        assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    finally:
        logging.setLogRecordFactory(previous)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
