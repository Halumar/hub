"""Tests for the extended notification backend.

Covers:
- canonical notification_type (default/sticky) persisted + normalized from normal/permanent
- popup stored + surfaced
- scheduled_at round-trip + presence in payloads
- curated icons endpoint + constant
- manual mdi:* name accepted by the create schema
"""

import types

import pytest

from data.notification_icons import CURATED_NOTIFICATION_ICONS
from models.schemas import SendNotificationRequest
from services.notifications import (
    canonical_notification_type,
    create_notification,
    get_client_notifications,
    get_notification,
    normalize_notification_row,
)
from api.notifications import list_notification_icons
from api.auth import require_admin


# ── canonical type mapping ──────────────────────────────────────────────────


def test_canonical_type_mapping():
    assert canonical_notification_type("normal") == "default"
    assert canonical_notification_type("default") == "default"
    assert canonical_notification_type("permanent") == "sticky"
    assert canonical_notification_type("sticky") == "sticky"
    assert canonical_notification_type(None) == "default"
    assert canonical_notification_type("garbage") == "default"
    assert canonical_notification_type("STICKY") == "sticky"


# ── create + persistence + normalization ────────────────────────────────────


@pytest.mark.asyncio
async def test_create_default_persisted(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(db, client_id=client_id, message="hi")
    assert n["notification_type"] == "default"
    assert n["popup"] is False
    assert n["scheduled_at"] is None

    fetched = await get_notification(db, n["id"])
    assert fetched["notification_type"] == "default"
    assert fetched["popup"] is False
    assert fetched["scheduled_at"] is None


@pytest.mark.asyncio
async def test_create_sticky_and_popup(db_with_client):
    db, client_id, _, _ = db_with_client
    n = await create_notification(
        db,
        client_id=client_id,
        message="stay",
        notification_type="sticky",
        popup=True,
    )
    assert n["notification_type"] == "sticky"
    assert n["popup"] is True

    fetched = await get_notification(db, n["id"])
    assert fetched["notification_type"] == "sticky"
    assert fetched["popup"] is True


@pytest.mark.asyncio
async def test_legacy_types_normalized_on_write(db_with_client):
    db, client_id, _, _ = db_with_client
    n_normal = await create_notification(
        db, client_id=client_id, message="a", notification_type="normal"
    )
    n_perm = await create_notification(
        db, client_id=client_id, message="b", notification_type="permanent"
    )
    assert n_normal["notification_type"] == "default"
    assert n_perm["notification_type"] == "sticky"

    # Persisted canonical value in the DB column.
    row = await db.fetchone(
        "SELECT notification_type FROM client_notifications WHERE id = ?",
        (n_perm["id"],),
    )
    assert row["notification_type"] == "sticky"


@pytest.mark.asyncio
async def test_legacy_value_normalized_on_read(db_with_client):
    """A row with normal or permanent in the DB reads back canonical."""
    db, client_id, _, _ = db_with_client
    now = db.now()
    await db.execute(
        """INSERT INTO client_notifications
           (id, client_id, title, message, notification_type, status, created_at)
           VALUES ('legacy-1', ?, '', 'old', 'permanent', 'pending', ?)""",
        (client_id, now),
    )
    await db.commit()
    fetched = await get_notification(db, "legacy-1")
    assert fetched["notification_type"] == "sticky"
    assert fetched["popup"] is False


@pytest.mark.asyncio
async def test_scheduled_at_round_trip_and_in_pending_payload(db_with_client):
    db, client_id, _, _ = db_with_client
    when = "2026-07-01T08:30:00+00:00"
    n = await create_notification(
        db,
        client_id=client_id,
        message="later",
        scheduled_at=when,
    )
    assert n["scheduled_at"] == when

    # The pending-notification payload the app polls carries all three fields.
    pending = await get_client_notifications(db, client_id, active_only=True)
    assert len(pending) == 1
    item = pending[0]
    assert item["scheduled_at"] == when
    assert item["notification_type"] == "default"
    assert item["popup"] is False
    assert set(["notification_type", "popup", "scheduled_at"]).issubset(item.keys())


def test_normalize_notification_row_coercions():
    row = {"notification_type": "normal", "popup": 1, "scheduled_at": None}
    out = normalize_notification_row(row)
    assert out["notification_type"] == "default"
    assert out["popup"] is True
    assert out["scheduled_at"] is None


# ── schema validation ───────────────────────────────────────────────────────


def test_send_schema_defaults_and_canonical_input():
    req = SendNotificationRequest(client_id="c1", message="x")
    assert req.notification_type == "default"
    assert req.popup is False
    assert req.scheduled_at is None

    sticky = SendNotificationRequest(
        client_id="c1", message="x", notification_type="sticky", popup=True
    )
    assert sticky.notification_type == "sticky"
    assert sticky.popup is True


def test_send_schema_accepts_legacy_type():
    for legacy in ("normal", "permanent", "sticky"):
        req = SendNotificationRequest(
            client_id="c1", message="x", notification_type=legacy
        )
        assert req.notification_type == legacy  # stored canonical at write time


def test_send_schema_rejects_unknown_type():
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", message="x", notification_type="bogus")


def test_send_schema_accepts_manual_mdi_icon():
    req = SendNotificationRequest(
        client_id="c1", message="x", icon="mdi:some-obscure-custom-icon"
    )
    assert req.icon == "mdi:some-obscure-custom-icon"


def test_send_schema_scheduled_at_validation():
    ok = SendNotificationRequest(
        client_id="c1", message="x", scheduled_at="2026-07-01T08:30:00Z"
    )
    assert ok.scheduled_at == "2026-07-01T08:30:00Z"
    with pytest.raises(ValueError):
        SendNotificationRequest(client_id="c1", message="x", scheduled_at="not-a-date")


# ── curated icons ───────────────────────────────────────────────────────────


def test_curated_icon_constant():
    assert 40 <= len(CURATED_NOTIFICATION_ICONS) <= 80
    assert all(name.startswith("mdi:") for name in CURATED_NOTIFICATION_ICONS)
    assert "mdi:bell" in CURATED_NOTIFICATION_ICONS
    assert "mdi:alert" in CURATED_NOTIFICATION_ICONS
    # No duplicates.
    assert len(set(CURATED_NOTIFICATION_ICONS)) == len(CURATED_NOTIFICATION_ICONS)


def _fake_request(auth_type):
    state = types.SimpleNamespace(auth_type=auth_type)
    return types.SimpleNamespace(state=state)


@pytest.mark.asyncio
async def test_icons_endpoint_returns_curated_list():
    req = _fake_request("admin")
    result = await list_notification_icons(req)
    assert result["icons"] == CURATED_NOTIFICATION_ICONS


def test_icons_endpoint_requires_admin():
    import fastapi

    req = _fake_request("client")
    with pytest.raises(fastapi.HTTPException):
        require_admin(req)
