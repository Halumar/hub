"""Integration pairing controls: the pairing toggle and navigate HOME.

Under test:
(a) allow_pairing_via_integration setting: default true, GET/PUT
    /api/settings/pairing-via-integration (admin-only, PUT broadcasts
    client_config_updated), surfaced in GET /api/integration/status.
(b) POST /api/integration/start-pairing returns 403 when the setting is
    "false" — for BOTH user and guest starts; cancel stays available.
(c) POST /api/integration/clients/{id}/navigate_to_area accepts the HOME
    sentinel "__home__" without an area-existence 404; real unknown areas
    still 404.
"""

import json
import types
import uuid

import fastapi
import pytest

from .profile_helpers import add_client

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
from models.database import Database
from models.schemas import NavigateToAreaRequest


def _integration_request(db, body=None):
    """Integration-authed request; optional JSON body for start-pairing."""
    raw = json.dumps(body).encode() if body is not None else b""

    async def _body():
        return raw

    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url="")
        ),
        headers={"content-length": str(len(raw))},
        body=_body,
    )


def _admin_request(db):
    request = _integration_request(db)
    request.state.auth_type = "admin"
    return request


@pytest.fixture
def clean_pairing():
    pairing.clear_active_pins()
    yield
    pairing.clear_active_pins()


@pytest.fixture
def _quiet_ws(monkeypatch):
    async def _noop_broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "broadcast", _noop_broadcast)


# ── (a) setting endpoints + status surface ───────────────────────────────────


class TestPairingViaIntegrationSetting:
    async def test_default_is_true(self, db):
        result = await pairing.get_pairing_via_integration_setting(_admin_request(db))
        assert result == {"allow_pairing_via_integration": True}

    async def test_put_persists_and_broadcasts(self, db, monkeypatch):
        events = []

        async def _capture(message, *a, **k):
            events.append(message)

        monkeypatch.setattr(ws, "broadcast", _capture)

        result = await pairing.update_pairing_via_integration_setting(
            _admin_request(db),
            pairing.PairingViaIntegrationUpdate(allow_pairing_via_integration=False),
        )
        assert result == {"allow_pairing_via_integration": False}
        assert await db.get_setting("allow_pairing_via_integration") == "false"
        assert {"type": "client_config_updated", "data": {}} in events

        result = await pairing.get_pairing_via_integration_setting(_admin_request(db))
        assert result == {"allow_pairing_via_integration": False}

    async def test_surfaced_in_integration_status(self, db, clean_pairing, _quiet_ws):
        result = await apiintg.integration_status(_integration_request(db))
        assert result["allow_pairing_via_integration"] is True

        await db.set_setting("allow_pairing_via_integration", "false")
        result = await apiintg.integration_status(_integration_request(db))
        assert result["allow_pairing_via_integration"] is False


# ── (b) start-pairing enforcement ────────────────────────────────────────────


class TestStartPairingToggle:
    async def test_user_start_blocked_when_disabled(self, db, clean_pairing, _quiet_ws):
        await db.set_setting("allow_pairing_via_integration", "false")

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_start_pairing(_integration_request(db))
        assert exc.value.status_code == 403
        assert (
            exc.value.detail == "Pairing via the integration is disabled in the add-on."
        )
        assert pairing.get_active_pin() is None

    async def test_guest_start_blocked_when_disabled(
        self, db, clean_pairing, _quiet_ws
    ):
        await db.set_setting("allow_pairing_via_integration", "false")

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_start_pairing(
                _integration_request(db, {"is_guest": True})
            )
        assert exc.value.status_code == 403
        assert pairing.get_active_pin() is None

    async def test_start_allowed_by_default(self, db, clean_pairing, _quiet_ws):
        result = await apiintg.integration_start_pairing(_integration_request(db))
        assert result["pin"]
        assert pairing.get_active_pin() is not None

    async def test_cancel_stays_available_when_disabled(
        self, db, clean_pairing, _quiet_ws
    ):
        import time

        await db.set_setting("allow_pairing_via_integration", "false")
        pairing.set_active_pin(
            "1234-5678",
            {"created_at": time.time(), "expires_at": time.time() + 300},
        )
        result = await apiintg.integration_cancel_pairing(_integration_request(db))
        assert result == {"success": True, "cancelled": True}


# ── (c) navigate_to_area HOME sentinel ───────────────────────────────────────


class TestNavigateHome:
    async def _make_client(self, db):
        client_id = str(uuid.uuid4())
        now = Database.now()
        await add_client(
            db, client_id=client_id, name="Panel", secret_hash="h" * 64, last_seen=now
        )
        return client_id

    async def test_home_sentinel_skips_area_check(self, db, monkeypatch):
        client_id = await self._make_client(db)

        sent = []

        async def _capture_send(cid, message):
            sent.append((cid, message))
            return True

        monkeypatch.setattr(ws, "send_to_client", _capture_send)

        result = await apiintg.integration_navigate_to_area(
            _integration_request(db),
            client_id,
            NavigateToAreaRequest(area_id="__home__"),
        )
        assert result["success"] is True
        assert sent == [
            (
                client_id,
                {
                    "type": "command",
                    "command": "navigate_to_area",
                    "area_id": "__home__",
                },
            )
        ]

    async def test_unknown_real_area_still_404(self, db, monkeypatch):
        client_id = await self._make_client(db)

        async def _never_send(cid, message):  # pragma: no cover
            raise AssertionError("must not send for unknown area")

        monkeypatch.setattr(ws, "send_to_client", _never_send)

        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_navigate_to_area(
                _integration_request(db),
                client_id,
                NavigateToAreaRequest(area_id=str(uuid.uuid4())),
            )
        assert exc.value.status_code == 404
