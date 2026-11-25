"""A dry run is never carried out for real (api/dry_run.py).

Every write of the REST API either declares a dry run (query parameter or
body field ``dry_run``) or refuses ``dry_run`` true with 422 before it runs.
"""

import httpx
import pytest
from fastapi.routing import APIRoute

from api import auth
from api.dry_run import reject_unsupported_dry_run, supports_dry_run

WRITES = {"POST", "PUT", "PATCH", "DELETE"}
TOKEN = "tok-dry-run"


@pytest.fixture
async def app(db, monkeypatch):
    import main

    monkeypatch.delenv("HUB_DEV", raising=False)
    monkeypatch.setattr(main.app.state, "db", db, raising=False)
    monkeypatch.setattr(main.app.state, "ha_client", None, raising=False)
    monkeypatch.setattr(main.app.state, "startup_error", None, raising=False)
    auth._sessions.clear()
    auth._rate_limits.clear()
    yield main.app
    auth._sessions.clear()


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.7", 50000)),
        base_url="https://hub.local:8100",
        headers={"authorization": f"Bearer {TOKEN}", "user-agent": "pytest"},
    )


async def _admin_token(db):
    await db.set_setting("api_token_hash", auth.hash_secret(TOKEN))


def _write_routes():
    """(method set, path, endpoint, guarded) of every REST write: the routes
    of every router the app includes, with that inclusion's dependencies."""
    import main

    out = []
    for item in main.app.routes:
        if isinstance(item, APIRoute):
            if item.methods & WRITES:
                guards = [d.call for d in item.dependant.dependencies]
                out.append(
                    (
                        item.methods,
                        item.path,
                        item.endpoint,
                        reject_unsupported_dry_run in guards,
                    )
                )
            continue
        router = getattr(item, "original_router", None)
        if router is None:
            continue
        context = item.include_context
        guarded = any(
            d.dependency is reject_unsupported_dry_run for d in context.dependencies
        )
        for r in router.routes:
            if isinstance(r, APIRoute) and r.methods & WRITES:
                out.append((r.methods, context.prefix + r.path, r.endpoint, guarded))
    return out


def test_every_write_has_a_dry_run_or_refuses_one():
    routes = _write_routes()
    assert len(routes) > 100
    unguarded = [
        f"{sorted(m)} {path}"
        for m, path, endpoint, guarded in routes
        if not guarded and not supports_dry_run(endpoint)
    ]
    assert unguarded == []
    supported = sorted(
        f"{','.join(sorted(m & WRITES))} {path}"
        for m, path, endpoint, _g in routes
        if supports_dry_run(endpoint)
    )
    assert "POST /api/profiles" not in supported
    assert "PATCH /api/profiles/{profile_id}" in supported
    assert "PATCH /api/clients/{client_id}/extend" in supported
    assert "POST /api/integration/clients/{client_id}/extend" in supported
    # A body field dry_run (postponed annotations resolved).
    assert "POST /api/pairing/clients/bulk" in supported


async def test_a_dry_run_on_profile_creation_is_refused(app, db):
    await _admin_token(db)
    before = await db.fetchone("SELECT COUNT(*) AS n FROM profiles")
    async with _client(app) as c:
        query = await c.post("/api/profiles?dry_run=true", json={"name": "Probe A"})
        body = await c.post("/api/profiles", json={"name": "Probe B", "dry_run": True})
        real = await c.post("/api/profiles", json={"name": "Probe C"})
    assert query.status_code == 422 and body.status_code == 422
    assert query.json()["detail"]["code"] == "dry_run_not_supported"
    assert real.status_code == 201
    after = await db.fetchone("SELECT COUNT(*) AS n FROM profiles")
    assert after["n"] == before["n"] + 1


async def test_a_supported_dry_run_changes_nothing(app, db):
    await _admin_token(db)
    async with _client(app) as c:
        created = (await c.post("/api/profiles", json={"name": "Probe"})).json()
        preview = await c.patch(
            f"/api/profiles/{created['id']}?dry_run=true", json={"name": "Renamed"}
        )
        again = (await c.get(f"/api/profiles/{created['id']}")).json()
    assert preview.status_code == 202 and preview.json()["dry_run"] is True
    assert again["name"] == "Probe"


async def test_dry_run_false_runs_normally(app, db):
    await _admin_token(db)
    async with _client(app) as c:
        r = await c.post("/api/profiles?dry_run=false", json={"name": "Probe F"})
    assert r.status_code == 201
