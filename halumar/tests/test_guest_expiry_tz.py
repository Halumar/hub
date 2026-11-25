"""Guest expiry time zones.

Expiries are compared as aware datetimes, so an expiry with any offset
(e.g. "+01:00") ends on time; a naive stored value is read as UTC and never
breaks GET /api/integration/status; unparseable input is refused with 422.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from .profile_helpers import add_client
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.pairing as pairing
import api.websocket as ws
from models.schemas import ExtendGuestRequest, PairingGenerateRequest
from services.compliance import derive_status
from services.expiry import is_expired, normalize_expiry_input

NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)


class TestNormalizeInput:
    def test_offset_is_normalized_to_utc(self):
        assert (
            normalize_expiry_input("2026-11-18T12:30:45.250+01:00", now=NOW)
            == "2026-11-18T11:30:45+00:00"
        )

    def test_seconds_are_kept(self):
        """No truncation to minutes, so a guest never ends early and a
        value less than a minute ahead is not stored as already past."""
        soon = "2026-11-18T10:00:40+00:00"
        assert normalize_expiry_input(soon, now=NOW) == soon
        assert is_expired(soon, now=NOW) is False
        assert is_expired(soon, now=NOW.replace(second=40)) is True

    def test_z_suffix(self):
        assert (
            normalize_expiry_input("2026-11-18T11:00:00Z", now=NOW)
            == "2026-11-18T11:00:00+00:00"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "2026-11-18T12:00:00",  # naive: ambiguous
            "garbage",
            "",
            "2026-11-18T09:00:00+00:00",  # past
            "2040-01-01T00:00:00+00:00",  # > 10 years
        ],
    )
    def test_rejected(self, value):
        with pytest.raises(ValueError):
            normalize_expiry_input(value, now=NOW)


# A day within one guest grant (at most 90 days ahead).
SOON = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()


class TestSchemas:
    def test_extend_naive_is_422(self):
        with pytest.raises(ValidationError):
            ExtendGuestRequest(expires_at="2034-01-01T10:00:00")

    def test_extend_offset_normalized(self):
        body = ExtendGuestRequest(expires_at=f"{SOON}T10:00:30+01:00")
        assert body.expires_at == f"{SOON}T09:00:30+00:00"

    def test_extend_is_at_most_90_days(self):
        """One guest grant lasts at most guest_access.max_hours (90 days)."""
        with pytest.raises(ValidationError):
            ExtendGuestRequest(expires_at="2099-01-01T10:00:00+00:00")
        with pytest.raises(ValidationError):
            ExtendGuestRequest(hours=2161)
        assert ExtendGuestRequest(hours=2160).hours == 2160

    @pytest.mark.parametrize("hours", [0, -1, 1e12])
    def test_extend_hours_bounds(self, hours):
        with pytest.raises(ValidationError):
            ExtendGuestRequest(hours=hours)

    def test_integration_start_pairing_schema(self):
        with pytest.raises(ValidationError):
            PairingGenerateRequest(is_guest=True, guest_expires_at="2034-01-01 10:00")
        # guest_url_mode is not a field: ignored, never stored.
        req = PairingGenerateRequest(is_guest=True, guest_url_mode="both")
        assert "guest_url_mode" not in req.model_dump()


class TestStoredValues:
    def test_text_order_vs_time_order(self):
        # 10:30+01:00 = 09:30Z is past at 10:00Z although "10:30" > "10:00".
        assert is_expired("2026-11-18T10:30:00+01:00", now=NOW) is True
        assert is_expired("2026-11-18T10:30:00+00:00", now=NOW) is False

    def test_naive_legacy_value_is_utc_not_typeerror(self):
        assert is_expired("2026-11-18T09:59:00", now=NOW) is True
        assert is_expired("2026-11-18T10:01:00", now=NOW) is False

    def test_unparseable_fails_closed(self):
        assert is_expired("garbage", now=NOW) is True

    def test_compliance_never_raises_on_naive(self):
        client = {"is_guest": 1, "is_active": 1, "expires_at": "2026-01-01T00:00:00"}
        assert derive_status(client) == "need_token_update"


def _app(db, auth_type):
    app = FastAPI()
    app.state.db = db
    app.state.ha_client = None

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        request.state.auth_type = auth_type
        return await call_next(request)

    from api.integration import router as integration_router

    app.include_router(pairing.router, prefix="/api")
    app.include_router(integration_router, prefix="/api")
    return app


@pytest.fixture
def clean_pins():
    pairing._active_pins.clear()
    yield
    pairing._active_pins.clear()


class TestEndpoints:
    async def test_generate_naive_is_422_and_keeps_active_pin(self, db, clean_pins):
        pairing._active_pins["ABCD-EFGH"] = {"created_at": 0, "expires_at": 9e12}
        with TestClient(_app(db, "admin")) as c:
            r = c.post(
                "/api/pairing/generate",
                json={"access": "guest", "guest_expires_at": f"{SOON}T10:00:00"},
            )
            assert r.status_code == 422
            r = c.post(
                "/api/pairing/generate",
                json={"access": "guest", "guest_expires_at": "garbage"},
            )
            assert r.status_code == 422
            r = c.post(
                "/api/pairing/generate",
                json={"access": "guest", "guest_hours": 1e12},
            )
            assert r.status_code == 422
            r = c.post(
                "/api/pairing/generate",
                json={"access": "guest", "guest_expires_at": "2099-01-01T10:00:00Z"},
            )
            assert r.status_code == 422
        assert "ABCD-EFGH" in pairing._active_pins

    async def test_generate_offset_is_stored_as_utc(self, db, clean_pins):
        with TestClient(_app(db, "admin")) as c:
            r = c.post(
                "/api/pairing/generate",
                json={
                    "access": "guest",
                    "guest_expires_at": f"{SOON}T10:00:00+01:00",
                },
            )
        assert r.status_code == 200
        (pin_data,) = pairing._active_pins.values()
        assert pin_data["guest_expires_at"] == f"{SOON}T09:00:00+00:00"

    async def test_integration_start_pairing_naive_is_422(self, db, clean_pins):
        with TestClient(_app(db, "integration")) as c:
            r = c.post(
                "/api/integration/start-pairing",
                json={"is_guest": True, "guest_expires_at": f"{SOON}T10:00:00"},
            )
            assert r.status_code == 422
            r = c.post(
                "/api/integration/start-pairing",
                json={
                    "is_guest": True,
                    "guest_expires_at": f"{SOON}T10:00:00+01:00",
                },
            )
            assert r.status_code == 200
        (pin_data,) = pairing._active_pins.values()
        assert pin_data["guest_expires_at"] == f"{SOON}T09:00:00+00:00"

    async def test_integration_status_never_500_with_legacy_naive_expiry(self, db):
        await add_client(
            db,
            name="Legacy",
            secret_hash="a" * 64,
            guest=True,
            expires_at="2034-01-01T10:00:00",
        )
        with TestClient(_app(db, "integration")) as c:
            r = c.get("/api/integration/status")
        assert r.status_code == 200


async def test_expiry_loop_removes_offset_guest_on_time(db, monkeypatch):
    """A '+01:00' guest whose expiry passed is removed on time, although its
    text sorts after "now"."""
    now = datetime.now(timezone.utc)
    past_local = (now - timedelta(minutes=2)).astimezone(timezone(timedelta(hours=1)))
    future = (now + timedelta(hours=1)).isoformat()
    for cid, value in (("gone", past_local.isoformat()), ("stays", future)):
        await add_client(
            db,
            client_id=cid,
            name=cid,
            secret_hash="a" * 64,
            guest=True,
            expires_at=value,
        )

    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError

    async def _noop(*_a, **_k):
        return 0

    monkeypatch.setattr(ws.asyncio, "sleep", _sleep)
    monkeypatch.setattr(ws, "disconnect_client", _noop)
    monkeypatch.setattr(ws, "broadcast", _noop)
    from services import device_provisioning

    sent = []

    async def _sender(message):
        sent.append(message)
        return True

    monkeypatch.setattr(device_provisioning, "_sender", _sender)
    app = SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None))
    with pytest.raises(asyncio.CancelledError):
        await ws.guest_expiry_loop(app)

    ids = {r["id"] for r in await db.fetchall("SELECT id FROM paired_clients")}
    assert ids == {"stays"}
    # The expired guest's HA user is removed by the integration.
    assert sent == [
        {
            "type": "deprovision_client",
            "data": {"client_id": "gone", "reason": "guest_expired"},
        }
    ]
