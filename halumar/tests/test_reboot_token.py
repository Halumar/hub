"""Token replaced at start: refused, but not reported as a failure.

After a simultaneous Core and hub start (host reboot) the integration
presents the token of the previous start once and then takes over the token
announced via discovery. That request is refused like any invalid token, but
it is logged as information and does not count as an authentication failure,
so the Settings page does not show a stale-token warning after every reboot.
"""

import logging
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import api.auth as auth
from api.auth import (
    AuthMiddleware,
    activate_integration_token,
    is_recently_replaced_integration_token,
    rotate_integration_token,
    stage_integration_token,
)
from services import integration_status


async def _passed(request):
    return "PASSED_THROUGH"


async def _dispatch(db, path, token):
    """Run the auth middleware for one request from an untrusted address."""
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
        "client": ("203.0.113.7", 54321),
        "server": ("testserver", 80),
        "scheme": "http",
        "app": SimpleNamespace(state=SimpleNamespace(db=db)),
    }
    return await AuthMiddleware(app=None).dispatch(Request(scope), _passed)


@pytest.fixture(autouse=True)
def _isolate():
    integration_status.reset()
    auth._forget_replaced_integration_token()
    yield
    integration_status.reset()
    auth._forget_replaced_integration_token()


async def _start(db) -> str:
    """One hub start: stage, (announce), activate. Returns the new token."""
    token = await stage_integration_token(db)
    await activate_integration_token(db, token)
    return token


@pytest.mark.asyncio
async def test_previous_token_refused_but_not_counted(db, caplog):
    old = await rotate_integration_token(db)
    new = await _start(db)

    with caplog.at_level(logging.INFO, logger="hub.auth"):
        refused = await _dispatch(db, "/api/integration/status", token=old)
    assert refused.status_code == 401
    assert integration_status.get_status()["auth_failure_count"] == 0
    assert "takes over the announced token" in caplog.text
    assert "invalid/stale" not in caplog.text
    assert old not in caplog.text

    assert await _dispatch(db, "/api/integration/status", token=new) == (
        "PASSED_THROUGH"
    )


@pytest.mark.asyncio
async def test_previous_token_counts_after_the_window(db, monkeypatch):
    old = await rotate_integration_token(db)
    await _start(db)
    monkeypatch.setattr(auth, "REPLACED_TOKEN_NOTICE_SECONDS", 0.0)
    await _start(db)  # window of the newest switch is already over

    assert not is_recently_replaced_integration_token(old)
    r = await _dispatch(db, "/api/integration/status", token=old)
    assert r.status_code == 401
    assert integration_status.get_status()["auth_failure_count"] == 1


@pytest.mark.asyncio
async def test_only_the_directly_replaced_token_is_expected(db):
    first = await rotate_integration_token(db)
    second = await _start(db)
    await _start(db)

    assert is_recently_replaced_integration_token(second)
    assert not is_recently_replaced_integration_token(first)
    assert not is_recently_replaced_integration_token("")
    r = await _dispatch(db, "/api/integration/status", token=first)
    assert r.status_code == 401
    assert integration_status.get_status()["auth_failure_count"] == 1


@pytest.mark.asyncio
async def test_regenerate_revokes_without_notice(db):
    old = await _start(db)
    await rotate_integration_token(db)

    assert not is_recently_replaced_integration_token(old)
    r = await _dispatch(db, "/api/integration/status", token=old)
    assert r.status_code == 401
    assert integration_status.get_status()["auth_failure_count"] == 1


@pytest.mark.asyncio
async def test_first_start_has_no_replaced_token(db):
    await _start(db)
    assert auth._replaced_integration_token is None


@pytest.mark.asyncio
async def test_replaced_token_opens_nothing_else(db):
    old = await rotate_integration_token(db)
    await _start(db)

    r = await _dispatch(db, "/api/areas", token=old)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_new_token_valid_while_the_switch_commits(db, monkeypatch):
    """HA may use the announced token while the Hub switches to it.

    The switch happens right after the check's first database read; the
    check must still accept the new token (no torn read of current and
    pending hash, live 19.11.2026 05:24).
    """
    await rotate_integration_token(db)
    new = await stage_integration_token(db)
    state = {"checking": False, "switched": False}

    def switch_after(read):
        async def wrapper(*args, **kwargs):
            result = await read(*args, **kwargs)
            if state["checking"] and not state["switched"]:
                state["switched"] = True
                await activate_integration_token(db, new)
            return result

        return wrapper

    monkeypatch.setattr(db, "get_setting", switch_after(db.get_setting))
    monkeypatch.setattr(db, "fetchall", switch_after(db.fetchall))
    monkeypatch.setattr(db, "fetchone", switch_after(db.fetchone))

    state["checking"] = True
    assert await auth.integration_token_valid(db, new) is True
    assert state["switched"]
    assert await auth.integration_token_valid(db, new) is True
