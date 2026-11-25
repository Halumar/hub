"""Location (contracts docs/location.md).

* rule ``location`` only for kinds person and child, ``location_required``
  only while location may be shared (resolver);
* hub way for devices with parental controls: WS ``location_report`` and REST
  ``POST /api/client/location`` – rule, background, age, rate limit, ordering,
  devices with a Home Assistant key get ``not_hub_controlled``;
* the fix goes to the integration (``client_location``), the hub keeps no
  position and logs no coordinates;
* ``location_shared`` / ``location_at`` from client_status, hub reports and
  the integration's metadata (``POST /api/integration/location-status``);
* snapshot field ``location``; paired-client location fields.
"""

import json
import logging
import types
from datetime import datetime, timedelta, timezone

import pytest
from api import profiles as profiles_api
from api.auth import CLIENT_SECRET_PATHS
from api.pairing import clients as clients_api
from api.ws import client as ws_client
from services import enforcement, location
from services.profiles import store as pstore

from .profile_helpers import add_client


def _now(minutes: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def _fix(**extra):
    body = {
        "latitude": 47.3769,
        "longitude": 8.5417,
        "accuracy": 12.0,
        "altitude": 408.0,
        "speed": 0.0,
        "course": 90.0,
        "battery_level": 80,
        "background": False,
        "ts": _now(),
    }
    body.update(extra)
    return body


@pytest.fixture(autouse=True)
def _clean():
    location.reset()
    yield
    location.reset()


@pytest.fixture
def sent(monkeypatch):
    """Messages to the integration and admin broadcasts."""
    import sys

    from api.ws.broadcast import send_to_integration  # noqa: F401 - loads the module

    bc = sys.modules["api.ws.broadcast"]
    out = {"integration": [], "broadcast": []}

    async def _int(message):
        out["integration"].append(message)
        return True

    async def _bc(message, audience=None):
        out["broadcast"].append(message)

    monkeypatch.setattr(bc, "send_to_integration", _int)
    from api import ws as api_ws

    monkeypatch.setattr(api_ws, "broadcast", _bc)
    return out


async def _child(db, **overrides):
    return await add_client(
        db, platform="ios", profile="child", overrides=overrides or None
    )


# ── rules ───────────────────────────────────────────────────────────────────


async def test_rules_by_kind(db):
    adult = await add_client(db, platform="ios", profile="adult")
    child = await _child(db)
    guest = await add_client(db, platform="ios", guest=True)
    eff = {
        cid: (await pstore.resolve_client(db, cid))[2] for cid in (adult, child, guest)
    }
    assert eff[adult]["location"]["value"] == "background"
    assert eff[adult]["location_required"]["value"] is False
    assert eff[child]["location"]["value"] == "foreground"
    assert eff[child]["location"]["enforcement"] == "hub"
    assert eff[guest]["location"]["reason"] == "requires_profile_kind"
    assert eff[guest]["location_required"]["reason"] == "requires_profile_kind"


async def test_required_needs_sharing(db):
    off = await add_client(
        db,
        platform="ios",
        profile="adult",
        overrides={"location": "off", "location_required": True},
    )
    on = await add_client(
        db, platform="ios", profile="adult", overrides={"location_required": True}
    )
    eff_off = (await pstore.resolve_client(db, off))[2]
    eff_on = (await pstore.resolve_client(db, on))[2]
    assert eff_off["location_required"]["reason"] == "requires_location"
    assert eff_off["location_required"]["value"] is None
    assert eff_on["location_required"]["value"] is True


async def test_snapshot_carries_location(db):
    adult = await add_client(db, platform="ios", profile="adult", ha_user_id="a" * 32)
    guest = await add_client(db, platform="ios", guest=True, ha_user_id="b" * 32)
    snaps = {s["client_id"]: s for s in await enforcement.build_all(db, None)}
    assert snaps[adult]["location"] == "background"
    assert snaps[guest]["location"] == "off"


# ── hub way ─────────────────────────────────────────────────────────────────


async def test_child_report_goes_to_integration_without_storing(db, sent, caplog):
    child = await _child(db)
    caplog.set_level(logging.DEBUG)
    answer = await location.report(db, child, _fix(), via="ws")
    assert answer == {"type": "location_report_ok", "applied": True}
    (msg,) = sent["integration"]
    assert msg["type"] == "client_location"
    assert msg["data"]["client_id"] == child
    assert msg["data"]["location"]["latitude"] == 47.3769
    assert "battery_level" not in msg["data"]["location"]
    # No position anywhere in the log; the battery went to the database.
    assert "47.37" not in caplog.text and "8.54" not in caplog.text
    row = await db.fetchone(
        "SELECT battery_level, latitude, longitude FROM paired_clients WHERE id = ?",
        (child,),
    )
    assert row == {"battery_level": 80, "latitude": None, "longitude": None}
    status = location.status_of(child)
    assert status["location_shared"] == "foreground"
    assert status["location_at"]
    assert sent["broadcast"][-1]["type"] == "client_status_updated"
    assert "latitude" not in sent["broadcast"][-1]["data"]
    assert sent["broadcast"][-1]["data"]["location_shared"] == "foreground"


async def test_device_with_key_uses_the_action(db, sent):
    adult = await add_client(db, platform="ios", profile="adult")
    with pytest.raises(location.LocationError) as err:
        await location.report(db, adult, _fix(), via="ws")
    assert err.value.reason == "not_hub_controlled"
    assert sent["integration"] == []


async def test_rule_background_and_off(db, sent):
    child = await _child(db)
    with pytest.raises(location.LocationError) as err:
        await location.report(db, child, _fix(background=True), via="rest")
    assert err.value.reason == "location_background_not_allowed"
    off = await _child(db, location="off")
    with pytest.raises(location.LocationError) as err:
        await location.report(db, off, _fix(), via="rest")
    assert err.value.reason == "location_not_allowed"
    bg = await _child(db, location="background")
    ok = await location.report(db, bg, _fix(background=True), via="rest")
    assert ok["applied"] is True
    assert location.status_of(bg)["location_shared"] == "background"
    assert len(sent["integration"]) == 1


async def test_age_rate_limit_and_order(db, sent):
    child = await _child(db)
    for ts in (_now(-16), _now(6)):
        with pytest.raises(location.LocationError) as err:
            await location.report(db, child, _fix(ts=ts), via="ws")
        assert err.value.reason == "location_stale"
    await location.report(db, child, _fix(ts=_now(-1)), via="ws")
    with pytest.raises(location.LocationError) as err:
        await location.report(db, child, _fix(), via="ws")
    assert err.value.reason == "rate_limited" and 1 <= err.value.retry_after <= 5
    location._accepted_at[child] -= 10
    older = await location.report(db, child, _fix(ts=_now(-2)), via="ws")
    assert older == {"type": "location_report_ok", "applied": False}
    assert len(sent["integration"]) == 1


@pytest.mark.parametrize(
    "body",
    [
        _fix(latitude=91),
        _fix(accuracy=-1),
        _fix(background="no"),
        _fix(ts="yesterday"),
        _fix(ts="2026-11-21T18:00:00"),
        _fix(location_shared="always"),
        _fix(battery_level=101),
        _fix(type="location_report"),
        {k: v for k, v in _fix().items() if k != "longitude"},
        "not an object",
    ],
)
async def test_invalid_reports(db, sent, body):
    child = await _child(db)
    with pytest.raises(location.LocationError) as err:
        await location.report(db, child, body, via="ws")
    assert err.value.reason == "invalid_request"
    assert sent["integration"] == []


async def test_ws_message(db, sent):
    child = await _child(db)
    answers = []
    socket = types.SimpleNamespace(send_json=lambda m: _append(answers, m))
    await ws_client._handle_client_message(
        socket, db, {"type": "location_report", **_fix()}, "location_report", child
    )
    await ws_client._handle_client_message(
        socket, db, {"type": "location_report", **_fix()}, "location_report", child
    )
    assert answers[0] == {"type": "location_report_ok", "applied": True}
    assert answers[1]["type"] == "location_report_error"
    assert answers[1]["reason"] == "rate_limited"
    await ws_client._handle_client_message(
        socket, db, {"type": "location_report", **_fix()}, "location_report", "admin"
    )
    assert answers[2]["type"] == "error"


async def _append(out, message):
    out.append(message)


def _request(db, client_id, body, auth_type="client"):
    from starlette.requests import Request

    payload = json.dumps(body).encode()
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/client/location",
        "raw_path": b"/api/client/location",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("192.0.2.30", 5555),
        "server": ("hub", 8100),
        "scheme": "https",
        "app": types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
    }

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    req = Request(scope, receive)
    req.state.auth_type = auth_type
    req.state.client_id = client_id
    return req


async def test_rest_twin(db, sent):
    assert "/api/client/location" in CLIENT_SECRET_PATHS
    child = await _child(db)
    ok = await profiles_api.client_location(_request(db, child, _fix()))
    assert ok == {"type": "location_report_ok", "applied": True}
    limited = await profiles_api.client_location(_request(db, child, _fix()))
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert json.loads(limited.body)["reason"] == "rate_limited"
    adult = await add_client(db, platform="ios", profile="adult")
    refused = await profiles_api.client_location(_request(db, adult, _fix()))
    assert refused.status_code == 403
    assert json.loads(refused.body) == {
        "type": "location_report_error",
        "reason": "not_hub_controlled",
    }
    bad = await profiles_api.client_location(_request(db, child, {"latitude": 1}))
    assert bad.status_code == 422
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await profiles_api.client_location(_request(db, None, _fix(), "admin"))


# ── status ──────────────────────────────────────────────────────────────────


async def test_client_status_location_shared(db, sent):
    adult = await add_client(db, platform="ios", profile="adult")
    await ws_client._handle_client_status(db, adult, {"location_shared": "none"})
    assert location.status_of(adult)["location_shared"] == "none"
    assert sent["broadcast"][-1]["data"]["location_shared"] == "none"
    count = len(sent["broadcast"])
    await ws_client._handle_client_status(db, adult, {"location_shared": "always"})
    await ws_client._handle_client_status(
        db, adult, {"latitude": 1.0, "longitude": 2.0}
    )
    assert len(sent["broadcast"]) == count
    assert location.status_of(adult)["location_shared"] == "none"


async def test_integration_metadata(db, sent, monkeypatch):
    from api import integration as integ

    adult = await add_client(db, platform="ios", profile="adult")
    ts = _now(-1)
    answer = await location.integration_status(
        db,
        adult,
        background=True,
        ts=location.parse_ts(ts),
        shared=None,
        battery_level=55,
    )
    assert answer["location_shared"] == "background"
    assert answer["location_at"]
    row = await db.fetchone(
        "SELECT battery_level FROM paired_clients WHERE id = ?", (adult,)
    )
    assert row["battery_level"] == 55
    assert sent["integration"] == []
    body = integ.LocationStatusRequest(
        client_id=adult, background=False, ts=_now(), location_shared="foreground"
    )
    req = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db)),
        state=types.SimpleNamespace(auth_type="integration"),
        headers={},
    )
    monkeypatch.setattr(integ, "require_integration", lambda request: None)
    answer = await integ.integration_location_status(req, body)
    assert answer["location_shared"] == "foreground"


async def test_paired_client_fields(db, sent):
    child = await _child(db, location_required=True)
    panel = await add_client(db, platform="ipados", profile="wall_panel")
    location.set_shared(child, "none")
    views = {v.id: v for v in await clients_api.client_views(db)}
    assert views[child].location_rule == "foreground"
    assert views[child].location_required is True
    assert views[child].location_shared == "none"
    assert views[child].location_at is None
    assert views[panel].location_rule is None
    assert views[panel].location_required is False


async def test_report_without_integration_is_not_applied(db, monkeypatch):
    """Nobody took the fix: applied false, and the same fix counts
    as new when the app sends it again."""
    import sys

    from api.ws.broadcast import send_to_integration  # noqa: F401 - loads the module

    bc = sys.modules["api.ws.broadcast"]
    delivered = {"ok": False}

    async def _int(message):
        return delivered["ok"]

    async def _bc(message, audience=None):
        pass

    from api import ws as api_ws

    monkeypatch.setattr(bc, "send_to_integration", _int)
    monkeypatch.setattr(api_ws, "broadcast", _bc)
    child = await _child(db)
    body = _fix()
    assert await location.report(db, child, body, via="ws") == {
        "type": "location_report_ok",
        "applied": False,
    }
    location._accepted_at.clear()  # past the 5 s rate limit
    delivered["ok"] = True
    assert await location.report(db, child, body, via="ws") == {
        "type": "location_report_ok",
        "applied": True,
    }
