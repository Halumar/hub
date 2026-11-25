"""Audit view of the admin UI (GET /api/admin/audit).

Admin actions, admin requests and security events are kept in a ring buffer
(newest first, categories, no secrets); admin-only.
"""

import types

import fastapi
import pytest

from api.admin import get_audit
from services import audit_log


@pytest.fixture(autouse=True)
def clean_buffer():
    audit_log.reset_recent()
    yield
    audit_log.reset_recent()


def _request(auth="ingress"):
    return types.SimpleNamespace(state=types.SimpleNamespace(auth_type=auth))


async def test_entries_newest_first_with_categories():
    audit_log.log_admin_access(
        "GET", "/api/pairing/clients", "ingress", "172.30.32.2", "Admin One"
    )
    audit_log.log_admin_access(
        "PATCH", "/api/pairing/clients/x", "session", "192.0.2.2", "manager_one"
    )
    audit_log.log_admin_action("clients_bulk_update", "fields=tags", "192.0.2.2")
    audit_log.log_auth_failure("session", "bad_password", "192.0.2.9")
    audit_log.log_pairing_failure("wrong_pin", "192.0.2.9")
    audit_log.log_rate_limited("pair", "192.0.2.9")

    result = await get_audit(_request(), limit=200, category=None)
    assert result.max_entries == audit_log.AUDIT_BUFFER_SIZE
    events = [(e.event, e.category) for e in result.entries]
    assert events == [
        ("rate_limited", "security"),
        ("pairing_failure", "security"),
        ("auth_failure", "security"),
        ("admin_action", "action"),
        ("admin_access", "change"),
        ("admin_access", "read"),
    ]
    change = result.entries[4]
    assert change.action == "PATCH /api/pairing/clients/x"
    assert (change.auth, change.user, change.ip) == (
        "session",
        "manager_one",
        "192.0.2.2",
    )
    assert result.entries[2].level == "warning"


async def test_filter_and_limit():
    for i in range(5):
        audit_log.log_admin_action(f"a{i}")
        audit_log.log_admin_access("GET", f"/api/admin/x{i}", "ingress")
    result = await get_audit(_request(), limit=3, category=["action"])
    assert [e.action for e in result.entries] == ["a4", "a3", "a2"]
    result = await get_audit(_request(), limit=200, category=["action", "change"])
    assert all(e.category == "action" for e in result.entries)


async def test_ring_buffer_is_bounded():
    for i in range(audit_log.AUDIT_BUFFER_SIZE + 25):
        audit_log.log_admin_action(f"a{i}")
    entries = audit_log.recent_entries(limit=10_000)
    assert len(entries) == audit_log.AUDIT_BUFFER_SIZE
    assert entries[0]["action"] == f"a{audit_log.AUDIT_BUFFER_SIZE + 24}"


async def test_values_are_sanitized():
    audit_log.log_admin_action("x", "line1\nFAKE ENTRY")
    assert audit_log.recent_entries()[0]["details"] == "line1\\nFAKE ENTRY"


async def test_noisy_events_are_not_kept():
    audit_log.log_auth_success("secret", "c1", "192.0.2.1")
    audit_log.log_client_connected("c1", "192.0.2.1")
    audit_log.log_client_disconnected("c1")
    audit_log.log_service_call("light", "turn_on", "c1", "light.x")
    assert audit_log.recent_entries() == []


async def test_clients_are_rejected():
    with pytest.raises(fastapi.HTTPException) as e:
        await get_audit(_request("client"), limit=10, category=None)
    assert e.value.status_code == 403


def test_route_is_registered():
    from main import app

    op = app.openapi()["paths"]["/api/admin/audit"]["get"]
    assert {p["name"] for p in op["parameters"]} == {"limit", "category"}
