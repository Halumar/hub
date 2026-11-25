"""Notifications while the hub was unreachable.

The integration writes a notification straight into the client's sensor when
the hub does not answer, queues it with its own UUID and hands it over once
the hub is back:

(a) POST /api/integration/notify with ``notification_id`` is idempotent
    (same client: existing one, ``duplicate``, no second push; another
    client: 409); with "all" the id is a broadcast id, each device gets
    UUIDv5(broadcast id, client id) and a retry skips the devices that have
    theirs.
(b) POST /api/integration/notifications/handover creates queued items with
    their id and created_at, stores app-confirmed ones as delivered via
    'remote' without a push, reports duplicates and skips unknown clients,
    foreign ids, expired and invalid items.
(c) The hub's own sensor write keeps items listed in ``pending_handover``
    until they are handed over (the app treats the sensor as the removal
    authority) and rewrites the sensors after a handover.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone

import fastapi
import pytest

from .profile_helpers import add_client
from pydantic import ValidationError

import api.integration as apiintg
import api.websocket as ws
import namespace
import services.ha_sensors as ha_sensors
from models.database import Database
from models.schemas import (
    HandoverNotification,
    IntegrationNotifyRequest,
    NotificationHandoverRequest,
)
from services.notifications import create_notification, get_notification


class MirrorHA:
    """HAClient stand-in: states store with a live mirror (sync get_state)."""

    def __init__(self):
        self.states: dict[str, dict] = {}
        self.posts: list[str] = []
        self.ws = self

    @property
    def mirror_ready(self):
        return True

    def state_known(self, entity_id):
        return entity_id in self.states

    async def get_state(self, entity_id):
        return self.states.get(entity_id)

    async def _post(self, url, data=None):
        entity_id = url.rsplit("/", 1)[-1]
        self.posts.append(entity_id)
        self.states[entity_id] = {"entity_id": entity_id, **data}
        return self.states[entity_id]


def _request(db, ha_client=None):
    state = types.SimpleNamespace(db=db, ha_client=ha_client)
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(state=state),
    )


async def _client(db, name="Panel"):
    return await add_client(
        db, name=name, secret_hash="h" * 64, last_seen=Database.now()
    )


def _sensor(name, client_id):
    return ha_sensors.client_notification_sensor_id(name, client_id)


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def _send(client_id, message):
        sent.append((client_id, message))
        return True

    async def _broadcast(*a, **k):
        return None

    monkeypatch.setattr(ws, "send_to_client", _send)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return sent


def _ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# ── (a) idempotent notify ────────────────────────────────────────────────────


class TestNotifyWithId:
    async def test_same_id_twice_creates_one(self, db, pushes):
        client_id = await _client(db)
        nid = str(uuid.uuid4())
        body = IntegrationNotifyRequest(
            client_id=client_id, title="Hi", notification_id=nid
        )
        first = await apiintg.integration_notify(_request(db), body)
        assert first["notification"]["id"] == nid
        assert first["ws_sent"] is True
        second = await apiintg.integration_notify(_request(db), body)
        assert second["duplicate"] is True
        assert second["ws_sent"] is False
        assert second["notification"]["id"] == nid
        assert len(pushes) == 1
        rows = await db.fetchall("SELECT id FROM client_notifications")
        assert [r["id"] for r in rows] == [nid]

    async def test_id_of_another_client_is_409(self, db, pushes):
        a = await _client(db, "A")
        b = await _client(db, "B")
        nid = str(uuid.uuid4())
        await apiintg.integration_notify(
            _request(db),
            IntegrationNotifyRequest(client_id=a, title="x", notification_id=nid),
        )
        with pytest.raises(fastapi.HTTPException) as exc:
            await apiintg.integration_notify(
                _request(db),
                IntegrationNotifyRequest(client_id=b, title="x", notification_id=nid),
            )
        assert exc.value.status_code == 409

    def test_schema(self):
        # A broadcast id is allowed.
        bid = str(uuid.uuid4())
        assert (
            IntegrationNotifyRequest(
                client_id="all", title="x", notification_id=bid
            ).notification_id
            == bid
        )
        with pytest.raises(ValidationError):
            IntegrationNotifyRequest(client_id="c", title="x", notification_id="n-1")
        # Without an id nothing changes.
        assert (
            IntegrationNotifyRequest(client_id="all", title="x").notification_id is None
        )


class TestBroadcastWithId:
    def test_derived_id_matches_the_contract_example(self):
        from services.notifications import broadcast_notification_id

        assert (
            broadcast_notification_id(
                "6f1c2a4e-8b3d-4c5e-9f70-1a2b3c4d5e6f",
                "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b",
            )
            == "e58ba18c-8fa7-5d79-87b1-4e45eb94b7b9"
        )

    async def test_retry_after_timeout_creates_nothing_twice(self, db, pushes):
        from services.notifications import broadcast_notification_id

        a = await _client(db, "A")
        b = await _client(db, "B")
        bid = str(uuid.uuid4())
        body = IntegrationNotifyRequest(
            client_id="all", title="Hi", notification_id=bid
        )
        first = await apiintg.integration_notify(_request(db), body)
        assert first == {"sent": 2, "duplicates": 0}
        rows = await db.fetchall("SELECT id, client_id FROM client_notifications")
        assert {(r["client_id"], r["id"]) for r in rows} == {
            (a, broadcast_notification_id(bid, a)),
            (b, broadcast_notification_id(bid, b)),
        }
        # A device paired in between gets its notification on the retry.
        c = await _client(db, "C")
        second = await apiintg.integration_notify(_request(db), body)
        assert second == {"sent": 1, "duplicates": 2}
        assert len(pushes) == 3
        assert {p[0] for p in pushes} == {a, b, c}

    async def test_handover_of_a_derived_id_is_a_duplicate(self, db, pushes):
        from services.notifications import broadcast_notification_id

        a = await _client(db, "A")
        bid = str(uuid.uuid4())
        await apiintg.integration_notify(
            _request(db),
            IntegrationNotifyRequest(client_id="all", title="Hi", notification_id=bid),
        )
        result = await apiintg.integration_notifications_handover(
            _request(db, MirrorHA()),
            NotificationHandoverRequest(
                notifications=[
                    HandoverNotification(
                        client_id=a,
                        notification_id=broadcast_notification_id(bid, a),
                        created_at=_ago(seconds=5),
                        title="Hi",
                    )
                ]
            ),
        )
        assert result["created"] == []
        assert result["duplicates"] == [broadcast_notification_id(bid, a)]
        assert len(pushes) == 1

    async def test_broadcast_without_id_is_unchanged(self, db, pushes):
        await _client(db, "A")
        result = await apiintg.integration_notify(
            _request(db), IntegrationNotifyRequest(client_id="all", title="Hi")
        )
        assert result == {"sent": 1}


# ── (b) handover ─────────────────────────────────────────────────────────────


class TestHandover:
    async def test_creates_delivers_and_dedupes(self, db, pushes):
        client_id = await _client(db)
        ha = MirrorHA()
        pending_id, delivered_id = str(uuid.uuid4()), str(uuid.uuid4())
        created_at = _ago(minutes=5)
        body = NotificationHandoverRequest(
            notifications=[
                HandoverNotification(
                    notification_id=pending_id,
                    client_id=client_id,
                    created_at=created_at,
                    title="Waschmaschine",
                    message="fertig",
                    color="green",
                ),
                HandoverNotification(
                    notification_id=delivered_id,
                    client_id=client_id,
                    created_at=_ago(minutes=4),
                    title="Gelesen",
                    status="delivered",
                ),
            ]
        )
        result = await apiintg.integration_notifications_handover(
            _request(db, ha), body
        )
        assert result == {
            "success": True,
            "created": [pending_id, delivered_id],
            "duplicates": [],
            "skipped": [],
        }
        pending = await get_notification(db, pending_id)
        assert pending["status"] == "pending"
        assert pending["created_at"] == created_at
        assert pending["color"] == "green"
        delivered = await get_notification(db, delivered_id)
        assert delivered["status"] == "delivered"
        assert delivered["delivered_via"] == "remote"
        # Only the pending one is pushed to the app.
        assert [m["notification_id"] for _, m in pushes] == [pending_id]
        # Sensor written by the hub, no carry-over attribute.
        sensor = ha.states[_sensor("Panel", client_id)]
        assert sensor["state"] == "2"
        assert "pending_handover" not in sensor["attributes"]

        again = await apiintg.integration_notifications_handover(_request(db, ha), body)
        assert again["created"] == []
        assert again["duplicates"] == [pending_id, delivered_id]
        assert len(pushes) == 1

    async def test_skips(self, db, pushes):
        a = await _client(db, "A")
        b = await _client(db, "B")
        foreign = await create_notification(db, client_id=b, title="b")
        ids = [str(uuid.uuid4()) for _ in range(3)]
        body = NotificationHandoverRequest(
            notifications=[
                HandoverNotification(
                    notification_id=ids[0],
                    client_id=str(uuid.uuid4()),
                    created_at=_ago(minutes=1),
                    title="x",
                ),
                HandoverNotification(
                    notification_id=foreign["id"],
                    client_id=a,
                    created_at=_ago(minutes=1),
                    title="x",
                ),
                HandoverNotification(
                    notification_id=ids[1],
                    client_id=a,
                    created_at=_ago(hours=73),
                    title="x",
                ),
                HandoverNotification(
                    notification_id=ids[2],
                    client_id=a,
                    created_at=_ago(minutes=1),
                    title="",
                ),
            ]
        )
        result = await apiintg.integration_notifications_handover(
            _request(db, MirrorHA()), body
        )
        assert result["created"] == []
        assert result["skipped"] == [
            {"notification_id": ids[0], "reason": "client_not_found"},
            {"notification_id": foreign["id"], "reason": "id_conflict"},
            {"notification_id": ids[1], "reason": "expired"},
            {"notification_id": ids[2], "reason": "invalid"},
        ]
        assert pushes == []

    def test_schema(self):
        nid = str(uuid.uuid4())
        with pytest.raises(ValidationError):
            HandoverNotification(
                notification_id=nid, client_id="all", created_at=_ago(), title="x"
            )
        with pytest.raises(ValidationError):
            HandoverNotification(
                notification_id=nid,
                client_id="c",
                created_at="2026-11-19T10:00:00",  # no time zone
                title="x",
            )
        with pytest.raises(ValidationError):
            HandoverNotification(
                notification_id=nid,
                client_id="c",
                created_at=_ago(),
                title="x",
                status="dismissed",
            )
        with pytest.raises(ValidationError):
            NotificationHandoverRequest(notifications=[])
        item = HandoverNotification(
            notification_id=nid,
            client_id="c",
            created_at="2026-11-19T11:00:00+01:00",
            title="x",
        )
        assert item.created_at == "2026-11-19T10:00:00+00:00"


# ── (c) sensor carry-over ────────────────────────────────────────────────────


def _item(nid, created_at, title="queued"):
    return {
        "notification_id": nid,
        "title": title,
        "message": "",
        "notification_type": "default",
        "popup": False,
        "popup_mode": "none",
        "scheduled_at": None,
        "priority": 50,
        "color": "none",
        "action_type": "none",
        "action_target": None,
        "show_time": True,
        "created_at": created_at,
        "status": "pending",
    }


class TestSensorCarryOver:
    async def test_hub_write_keeps_queued_items_until_handover(self, db, pushes):
        client_id = await _client(db)
        sensor_id = _sensor("Panel", client_id)
        ha = MirrorHA()
        hub_own = await create_notification(db, client_id=client_id, title="hub")
        queued_id, stale_id = str(uuid.uuid4()), str(uuid.uuid4())
        queued = _item(queued_id, _ago(minutes=1))
        stale = _item(stale_id, _ago(hours=80))
        # Written by the integration while the hub was down.
        ha.states[sensor_id] = {
            "entity_id": sensor_id,
            "state": "2",
            "attributes": {
                "count": 2,
                "notifications": [queued, stale],
                "pending_handover": [queued_id, stale_id],
            },
        }

        await ha_sensors.sync_client_notification_sensors(db, ha)
        attributes = ha.states[sensor_id]["attributes"]
        assert [n["notification_id"] for n in attributes["notifications"]] == [
            hub_own["id"],
            queued_id,
        ]
        assert attributes["pending_handover"] == [queued_id]
        assert attributes["count"] == 2

        # Handover: the hub now owns it, the attribute goes away.
        await apiintg.integration_notifications_handover(
            _request(db, ha),
            NotificationHandoverRequest(
                notifications=[
                    HandoverNotification(
                        notification_id=queued_id,
                        client_id=client_id,
                        created_at=queued["created_at"],
                        title="queued",
                    )
                ]
            ),
        )
        attributes = ha.states[sensor_id]["attributes"]
        assert "pending_handover" not in attributes
        ids = [n["notification_id"] for n in attributes["notifications"]]
        assert sorted(ids) == sorted([queued_id, hub_own["id"]])

    async def test_refused_items_are_not_carried(self, db, pushes):
        client_id = await _client(db)
        sensor_id = _sensor("Panel", client_id)
        ha = MirrorHA()
        nid = str(uuid.uuid4())
        ha.states[sensor_id] = {
            "entity_id": sensor_id,
            "state": "1",
            "attributes": {
                "notifications": [_item(nid, _ago(minutes=1), title="")],
                "pending_handover": [nid],
            },
        }
        result = await apiintg.integration_notifications_handover(
            _request(db, ha),
            NotificationHandoverRequest(
                notifications=[
                    HandoverNotification(
                        notification_id=nid,
                        client_id=client_id,
                        created_at=_ago(minutes=1),
                        title="",
                    )
                ]
            ),
        )
        assert result["skipped"] == [{"notification_id": nid, "reason": "invalid"}]
        attributes = ha.states[sensor_id]["attributes"]
        assert attributes["notifications"] == []
        assert "pending_handover" not in attributes

    async def test_handover_rewrites_an_unchanged_sensor(self, db, pushes):
        """The integration overwrote the sensor; the hub's digest is stale."""
        client_id = await _client(db)
        sensor_id = _sensor("Panel", client_id)
        ha = MirrorHA()
        nid = str(uuid.uuid4())
        await apiintg.integration_notify(
            _request(db, ha),
            IntegrationNotifyRequest(
                client_id=client_id, title="x", notification_id=nid
            ),
        )
        posts = ha.posts.count(sensor_id)
        ha.states[sensor_id] = {"entity_id": sensor_id, "state": "0", "attributes": {}}
        result = await apiintg.integration_notifications_handover(
            _request(db, ha),
            NotificationHandoverRequest(
                notifications=[
                    HandoverNotification(
                        notification_id=nid,
                        client_id=client_id,
                        created_at=_ago(minutes=1),
                        title="x",
                    )
                ]
            ),
        )
        assert result["duplicates"] == [nid]
        assert ha.posts.count(sensor_id) == posts + 1
        assert ha.states[sensor_id]["state"] == "1"

    def test_prefix_matches_namespace(self):
        assert _sensor("Panel", str(uuid.uuid4())).startswith(
            namespace.CLIENT_SENSOR_PREFIX
        )
