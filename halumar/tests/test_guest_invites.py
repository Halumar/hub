"""Guest invitations by member devices.

* ``setup_devices`` with the kind guest: per profile or device (never
  guests), in ``client_config`` and the admin client; removing it ends the
  open invitation.
* ``guest_invite_create`` -> ``guest_invite`` (own one-time PIN next to the
  admin PIN, pairing link, grant = the member's choice within its frame:
  guest profile, end, remote access); limits; replace.
* The guest pairs with the normal ``pair`` and gets that choice;
  ``set_up_by``, activity, audit; the member gets
  ``guest_invite_ended {paired}``.
* Cancel, expiry, revoke, invalidation after failed PINs.
* “Set up by me”: only own guests.
* Admin REST: tag catalog, open invitations.
"""

import asyncio
import json
import time
import types
import uuid
from datetime import datetime, timezone

import api.websocket as ws
import pytest

from .profile_helpers import add_client, starter_profile
from api import authz
from api import profiles as profiles_api
from api import pairing
from api.pairing import clients as clients_api
from api.pairing import guests as guests_api
from fastapi import HTTPException
from models.database import Database
from services import client_activity, crypto, guest_invites, pairing_link
from services import device_provisioning as dp
from starlette.websockets import WebSocketState

CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    ws._clients.clear()
    guest_invites.reset()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()

    async def _sender(_message):
        return True

    monkeypatch.setattr(dp, "_sender", _sender)

    async def _base_url(_app, _db):
        return "https://192.0.2.1:8100"

    monkeypatch.setattr(pairing_link, "resolve_lan_base_url", _base_url)
    yield
    ws._clients.clear()
    guest_invites.reset()
    pairing.clear_active_pins()
    crypto.reset_cache()
    dp.reset()


@pytest.fixture
def events(monkeypatch):
    """Broadcasts and targeted messages (client id, message)."""
    log: dict[str, list] = {"broadcast": [], "to": []}

    async def _broadcast(message, *a, **k):
        log["broadcast"].append(message)

    async def _send_to(client_id, message):
        log["to"].append((client_id, message))
        return True

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(ws, "send_to_client", _send_to)
    return log


class _WS:
    def __init__(self, db, *, host="192.0.2.20", client_id=None):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url="")
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host=host)
        self.url = types.SimpleNamespace(hostname="hub", port=8100, scheme="wss")
        self.sent = []
        self.closed = None
        self.application_state = WebSocketState.CONNECTED
        ws._clients[self] = {
            "authenticated": client_id is not None,
            "client_id": client_id,
            "pair_attempts": 0,
            "auth_attempts": 0,
            "connected_at": time.time(),
            "ip": host,
        }

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def last(self, type_=None):
        msgs = [m for m in self.sent if type_ is None or m["type"] == type_]
        return msgs[-1] if msgs else None


async def _client(db, **columns):
    columns.setdefault("name", "iPhone Manager One")
    return await add_client(db, last_seen=Database.now(), **columns)


async def _area(db, name, sort_order=0, tags=()):
    area_id = str(uuid.uuid4())
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, "
            "updated_at) VALUES (?, ?, ?, 1, ?, ?)",
            (area_id, name, sort_order, now, now),
        )
        for tag in tags:
            await db.execute(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
            )
    return area_id


def _request(db, auth="ingress"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


async def _defaults(db, *, all_areas=False, tags=(), area_ids=(), expires_hours=24):
    """Guest default profile (rule areas) and guest default duration."""
    guest_profile = await starter_profile(db, "guest")
    areas = (
        {"mode": "all", "area_ids": [], "tags": [], "own_area": False}
        if all_areas
        else {
            "mode": "selected",
            "area_ids": list(area_ids),
            "tags": list(tags),
            "own_area": False,
        }
    )
    await profiles_api.update_profile(
        _request(db),
        guest_profile,
        profiles_api.ProfileWrite(rules={"areas": areas}),
        dry_run=False,
    )
    settings = await profiles_api.get_profile_settings(_request(db))
    await profiles_api.put_profile_settings(
        _request(db),
        profiles_api.ProfileSettings(
            default_profiles=settings["default_profiles"],
            guest_default_hours=expires_hours,
        ),
    )


GUESTS = {"enabled": True, "kinds": ["guest"]}


async def _member(db, **columns):
    overrides = {"setup_devices": GUESTS, **columns.pop("overrides", {})}
    cid = await _client(db, overrides=overrides, **columns)
    return _WS(db, client_id=cid), cid


async def _send(conn, db, msg):
    await ws._handle_client_message(
        conn, db, msg, msg["type"], ws._clients[conn]["client_id"]
    )


async def _invite(db, conn, **choice):
    await _send(conn, db, {"type": "guest_invite_create", **choice})
    return conn.last()


async def _pair(db, pin, name="Gast-Handy", host="198.51.100.7"):
    guest = _WS(db, host=host)
    ok, client_id = await ws._handle_pair(
        guest, db, {"pin": pin, "client_name": name, **CLIENT_INFO}
    )
    return ok, client_id, guest


def _ended(events, client_id):
    return [
        m
        for cid, m in events["to"]
        if cid == client_id and m["type"] == "guest_invite_ended"
    ]


# ── Right ───────────────────────────────────────────────────────────────────


class TestRight:
    async def test_client_config_follows_the_profile_rule(self, db, events):
        member = await _client(db)
        guest = await _client(
            db, is_guest=1, profile="adult", overrides={"setup_devices": GUESTS}
        )
        rules = (await ws._get_client_config(db, member))["rules"]
        assert rules["setup_devices"]["value"] == {"enabled": False, "kinds": []}
        await profiles_api.put_client_override(
            _request(db),
            member,
            "setup_devices",
            profiles_api.DeviceOverride(value=GUESTS),
            dry_run=False,
        )
        rules = (await ws._get_client_config(db, member))["rules"]
        assert rules["setup_devices"] == {
            "value": GUESTS,
            "source": "device",
            "reason": "device",
            "locked": True,
            "fixed": False,
            "enforcement": "hub",
            "set_by": "admin",
        }
        assert (member, {"type": "client_config_updated", "data": {}}) in events["to"]
        rule = (await ws._get_client_config(db, guest))["rules"]["setup_devices"]
        assert rule["value"]["enabled"] is False and rule["reason"] == "boundary_guest"
        assert (await ws._get_client_config(db, "ingress"))["rules"] == {}

    async def test_guests_never_get_the_right(self, db, events):
        for profile in ("guest", "adult"):
            guest = await _client(db, is_guest=1, profile=profile)
            with pytest.raises(HTTPException) as exc:
                await profiles_api.put_client_override(
                    _request(db),
                    guest,
                    "setup_devices",
                    profiles_api.DeviceOverride(value=GUESTS),
                    dry_run=False,
                )
            assert exc.value.status_code == 422

    async def test_admin_client_shows_set_up_by(self, db, events):
        member = await _client(db, overrides={"setup_devices": GUESTS})
        guest = await _client(
            db,
            is_guest=1,
            set_up_by_client_id=member,
            set_up_by_name="iPhone Manager One",
            set_up_method="invite",
        )
        m = await clients_api.get_client(_request(db), member)
        g = await clients_api.get_client(_request(db), guest)
        assert m.set_up_by is None and m.access == "member"
        assert m.profile_kind == "person"
        assert g.access == "guest" and g.profile_kind == "guest"
        assert g.set_up_by == {
            "client_id": member,
            "name": "iPhone Manager One",
            "method": "invite",
        }

    async def test_removing_the_right_stops_the_open_invitation(self, db, events):
        conn, member = await _member(db)
        await _defaults(db, all_areas=True)
        invite = await _invite(db, conn)
        assert invite["type"] == "guest_invite"
        await profiles_api.delete_client_override(
            _request(db), member, "setup_devices", dry_run=False
        )
        # The open invitation ends at once ...
        assert _ended(events, member)[-1]["result"] == "revoked"
        assert guest_invites.open_invites() == []
        # ... and the PIN does not pair any more (fail closed).
        ok, _cid, guest = await _pair(db, invite["pin"])
        assert not ok


# ── Create ──────────────────────────────────────────────────────────────────


class TestCreate:
    async def test_without_right_guest_or_admin_session_not_permitted(self, db, events):
        await _defaults(db, all_areas=True)
        plain = _WS(db, client_id=await _client(db))
        await _send(plain, db, {"type": "guest_invite_create"})
        assert plain.last()["reason"] == "not_permitted"
        guest = _WS(db, client_id=await _client(db, is_guest=1))
        await _send(guest, db, {"type": "guest_invite_create"})
        assert guest.last()["reason"] == "not_permitted"
        admin = _WS(db, client_id="ingress")
        await _send(admin, db, {"type": "guest_invite_create"})
        assert admin.last() == {
            "type": "guest_invite_error",
            "reason": "not_permitted",
            "message": guest_invites.MESSAGES["not_permitted"],
        }
        assert guest_invites.open_invites() == []

    async def test_child_device_may_not_invite_even_with_release(self, db, events):
        # Kids mode: a child device never invites guests.
        await _defaults(db, all_areas=True)
        child, _ = await _member(db, profile="child")
        await _send(child, db, {"type": "guest_invite_create"})
        assert child.last()["reason"] == "not_permitted"
        assert guest_invites.open_invites() == []

    async def test_guest_defaults_without_area_refuse(self, db, events):
        await _area(db, "Wohnzimmer")
        conn, _ = await _member(db)
        assert (await _invite(db, conn))["reason"] == "no_guest_areas"
        # A tag that matches no area gives the guest nothing either.
        await _defaults(db, tags=["gast"])
        assert (await _invite(db, conn))["reason"] == "no_guest_areas"

    async def test_invitation_carries_pin_link_and_the_guest_defaults(self, db, events):
        living = await _area(db, "Wohnzimmer", 0, tags=("gast",))
        kitchen = await _area(db, "Küche", 1)
        await _area(db, "Büro", 2)
        await _defaults(db, tags=["gast"], area_ids=[kitchen], expires_hours=48)
        conn, member = await _member(db)
        msg = await _invite(db, conn)
        assert msg["type"] == "guest_invite"
        pin = msg["pin"]
        assert len(pin) == 9 and pin[4] == "-"
        assert msg["deep_link"].startswith("halumar://pair?url=https%3A%2F%2F192.0.2.1")
        assert f"&pin={pin}" in msg["deep_link"]
        assert msg["url"] == "https://192.0.2.1:8100"
        assert msg["expires_in"] == 300
        guest_profile = await starter_profile(db, "guest")
        assert msg["grant"] == {
            "all_areas": False,
            "area_names": ["Wohnzimmer", "Küche"],
            "expires_hours": 48,
            "expires_at": None,
            "profile": {"id": guest_profile, "name": "Gast", "kind": "guest"},
            "remote_allowed": False,
            "access_hours": {"enabled": False, "windows": []},
            "owner_label": None,
        }
        assert living
        opened = [m for m in events["broadcast"] if m["type"] == "guest_invite_opened"]
        assert opened[0]["data"]["invited_by"] == {
            "client_id": member,
            "name": "iPhone Manager One",
        }
        assert "pin" not in json.dumps(opened)
        listing = await guests_api.list_guest_invites(_request(db))
        assert [i.id for i in listing.invites] == [msg["invite_id"]]
        assert pin not in listing.model_dump_json()

    async def test_new_invitation_replaces_the_own_one(self, db, events):
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        first = await _invite(db, conn)
        second = await _invite(db, conn)
        assert first["invite_id"] != second["invite_id"]
        assert _ended(events, member)[-1] == {
            "type": "guest_invite_ended",
            "invite_id": first["invite_id"],
            "result": "replaced",
        }
        assert [i.id for i in guest_invites.open_invites()] == [second["invite_id"]]

    async def test_rate_limit_per_device_and_in_total(self, db, events, monkeypatch):
        await _defaults(db, all_areas=True)
        conn, _ = await _member(db)
        for _ in range(guest_invites.MAX_INVITES_PER_DEVICE):
            assert (await _invite(db, conn))["type"] == "guest_invite"
        assert (await _invite(db, conn))["reason"] == "rate_limited"
        monkeypatch.setattr(guest_invites, "MAX_OPEN_TOTAL", 1)
        other, _ = await _member(db)
        assert (await _invite(db, other))["reason"] == "rate_limited"

    async def test_admin_pin_and_invitation_work_side_by_side(self, db, events):
        await _defaults(db, all_areas=True)
        conn, _ = await _member(db)
        invite = await _invite(db, conn)
        pairing.set_active_pin("1234-5678", {"created_at": 0, "expires_at": 9e12})
        ok, _, _ = await _pair(db, invite["pin"])
        assert ok
        ok, admin_device, _ = await _pair(db, "1234-5678", name="TV")
        assert ok
        row = await db.fetchone(
            "SELECT access, set_up_by_client_id FROM paired_clients WHERE id = ?",
            (admin_device,),
        )
        assert row == {"access": "member", "set_up_by_client_id": None}


# ── Pairing of the guest ────────────────────────────────────────────────────


class TestPairing:
    async def test_guest_gets_the_defaults_and_the_member_sees_it(self, db, events):
        kitchen = await _area(db, "Küche")
        await _defaults(db, tags=["gast"], area_ids=[kitchen], expires_hours=6)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        before = datetime.now(timezone.utc)
        ok, guest_id, guest = await _pair(db, invite["pin"])
        assert ok
        assert guest.last()["type"] == "pair_ok" and guest.last()["is_guest"] is True
        row = await db.fetchone(
            "SELECT * FROM paired_clients WHERE id = ?", (guest_id,)
        )
        assert row["access"] == "guest"
        assert row["profile_id"] == await starter_profile(db, "guest")
        scope = await authz.client_area_scope(db, guest_id)
        assert scope.as_tags() == ["gast", f"area:{kitchen}"]
        rules = (await ws._get_client_config(db, guest_id))["rules"]
        assert rules["remote_access"]["value"] is False
        assert row["set_up_by_client_id"] == member
        assert row["set_up_by_name"] == "iPhone Manager One"
        assert row["set_up_method"] == "invite"
        hours = (
            datetime.fromisoformat(row["expires_at"]) - before
        ).total_seconds() / 3600
        assert 5.9 < hours <= 6.0
        assert _ended(events, member)[-1] == {
            "type": "guest_invite_ended",
            "invite_id": invite["invite_id"],
            "result": "paired",
            "client_id": guest_id,
            "client_name": "Gast-Handy",
        }
        member_events = await client_activity.list_events(db, member)
        assert member_events[0]["event"] == "guest_invited"
        assert member_events[0]["detail"]["guest_client_id"] == guest_id
        guest_events = await client_activity.list_events(db, guest_id)
        assert guest_events[0]["detail"]["method"] == "invite"
        assert guest_events[0]["detail"]["invited_by"] == member
        # Single use.
        ok, _, again = await _pair(db, invite["pin"], host="198.51.100.8")
        assert not ok and again.last()["reason"] == "pin_invalid"
        assert guest_invites.open_invites() == []

    async def test_all_areas_default_gives_the_marker(self, db, events):
        await _area(db, "Garten")
        await _defaults(db, all_areas=True, tags=["gast"])
        conn, _ = await _member(db)
        invite = await _invite(db, conn)
        assert invite["grant"]["all_areas"] is True
        ok, guest_id, _ = await _pair(db, invite["pin"])
        assert ok
        assert (await authz.client_area_scope(db, guest_id)).as_tags() == ["*"]

    async def test_member_without_right_at_pairing_time_fails_closed(self, db, events):
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        async with db.transaction():
            await db.execute(
                "DELETE FROM device_overrides WHERE client_id = ? "
                "AND rule_key = 'setup_devices'",
                (member,),
            )
        ok, _, guest = await _pair(db, invite["pin"])
        assert not ok and guest.last()["reason"] == "pin_invalid"
        assert _ended(events, member)[-1]["result"] == "revoked"
        count = await db.fetchone(
            "SELECT COUNT(*) AS n FROM paired_clients WHERE access = 'guest'"
        )
        assert count["n"] == 0

    async def test_cancelled_and_expired_invitations_stop_working(self, db, events):
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        other, _ = await _member(db)
        await _send(
            other, db, {"type": "guest_invite_cancel", "invite_id": invite["invite_id"]}
        )
        assert other.last()["reason"] == "invite_not_found"
        await _send(conn, db, {"type": "guest_invite_cancel"})
        assert conn.last()["reason"] == "invalid_request"
        await _send(
            conn, db, {"type": "guest_invite_cancel", "invite_id": invite["invite_id"]}
        )
        assert _ended(events, member)[-1]["result"] == "cancelled"
        ok, _, guest = await _pair(db, invite["pin"])
        assert not ok and guest.last()["reason"] == "pin_expired"

        invite = await _invite(db, conn)
        live = guest_invites.by_id(invite["invite_id"])
        live.expires_at = time.time() - 1
        ok, _, guest = await _pair(db, invite["pin"], host="198.51.100.9")
        assert not ok and guest.last()["reason"] == "pin_invalid"

    async def test_expiry_task_tells_the_member(self, db, events, monkeypatch):
        await _defaults(db, all_areas=True)
        monkeypatch.setattr(guest_invites, "INVITE_SECONDS", 0.05)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        await asyncio.sleep(0.2)
        assert _ended(events, member)[-1] == {
            "type": "guest_invite_ended",
            "invite_id": invite["invite_id"],
            "result": "expired",
        }
        closed = [m for m in events["broadcast"] if m["type"] == "guest_invite_closed"]
        assert closed[-1]["data"]["result"] == "expired"

    async def test_wrong_pins_from_three_addresses_invalidate(
        self, db, events, monkeypatch
    ):
        monkeypatch.setattr(pairing, "PAIR_IP_LIMIT_MAX", 1000)
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        wrong = "0000-0000" if invite["pin"] != "0000-0000" else "1111-1111"
        for i in range(guest_invites.MAX_PIN_FAILURES):
            await _pair(db, wrong, host=f"198.51.100.{i % 3 + 10}")
        assert _ended(events, member)[-1]["result"] == "invalidated"
        ok, _, guest = await _pair(db, invite["pin"], host="203.0.113.5")
        assert not ok and guest.last()["reason"] == "too_many_failures"


# ── Own guests ──────────────────────────────────────────────────────────────


class TestOwnGuests:
    async def test_member_lists_and_removes_only_its_guests(self, db, events):
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        _, mine, guest_conn = await _pair(db, invite["pin"])
        ws._clients[guest_conn].update({"authenticated": True, "client_id": mine})
        stranger = await _client(db, is_guest=1)
        other_member = await _client(db, can_invite_guests=1)
        theirs = await _client(
            db,
            is_guest=1,
            set_up_by_client_id=other_member,
            set_up_by_name="x",
            set_up_method="invite",
        )

        await _send(conn, db, {"type": "get_set_up_devices"})
        listing = conn.last()
        assert listing["type"] == "set_up_devices"
        assert [g["client_id"] for g in listing["devices"]] == [mine]
        assert listing["devices"][0]["name"] == "Gast-Handy"
        assert listing["devices"][0]["method"] == "invite"
        assert listing["devices"][0]["access"] == "guest"

        for target in (stranger, theirs, member):
            await _send(conn, db, {"type": "set_up_device_remove", "client_id": target})
            assert conn.last()["reason"] == "device_not_found"
        await _send(conn, db, {"type": "set_up_device_remove", "client_id": mine})
        assert conn.last() == {"type": "set_up_device_removed", "client_id": mine}
        assert (
            await db.fetchone("SELECT id FROM paired_clients WHERE id = ?", (mine,))
            is None
        )
        assert guest_conn.closed[0] == 4003
        kicked = [m for m in events["broadcast"] if m["type"] == "client_kicked"]
        assert kicked[-1]["data"] == {
            "client_id": mine,
            "is_guest": True,
            "reason": "kicked",
        }

    async def test_guests_cannot_list_or_remove(self, db, events):
        guest = _WS(db, client_id=await _client(db, is_guest=1))
        await _send(guest, db, {"type": "get_set_up_devices"})
        assert guest.last()["reason"] == "not_permitted"
        await _send(
            guest, db, {"type": "set_up_device_remove", "client_id": str(uuid.uuid4())}
        )
        assert guest.last()["reason"] == "not_permitted"
        await _send(guest, db, {"type": "set_up_device_remove"})
        assert guest.last()["reason"] == "invalid_request"


class TestChoice:
    """The member chooses profile, end and remote access within its frame."""

    async def test_chosen_end_remote_and_name(self, db, events):
        from datetime import timedelta

        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        end = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        msg = await _invite(
            db, conn, expires_at=end, remote=True, owner_label="Guest One"
        )
        assert msg["type"] == "guest_invite"
        assert msg["grant"]["remote_allowed"] is True
        assert msg["grant"]["owner_label"] == "Guest One"
        ok, guest_id, _ = await _pair(db, msg["pin"])
        assert ok
        row = await db.fetchone(
            "SELECT expires_at, owner_label FROM paired_clients WHERE id = ?",
            (guest_id,),
        )
        assert row["owner_label"] == "Guest One"
        assert abs(
            datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(end)
        ) < timedelta(seconds=2)
        eff = (await ws._get_client_config(db, guest_id))["rules"]
        assert eff["remote_access"]["value"] is True
        entries = await db.fetchall(
            "SELECT actor, detail_json FROM event_log WHERE kind = 'device_set_up'"
        )
        assert entries and entries[-1]["actor"] == "iPhone Manager One"

    async def test_frame_is_enforced(self, db, events):
        from datetime import timedelta

        await _defaults(db, all_areas=True)
        conn, _ = await _member(db, overrides={"remote_access": False})
        far = (datetime.now(timezone.utc) + timedelta(days=120)).isoformat()
        assert (await _invite(db, conn, expires_at=far))["reason"] == (
            "expiry_not_allowed"
        )
        assert (await _invite(db, conn, remote=True))["reason"] == "expiry_not_allowed"
        adult = await starter_profile(db, "adult")
        assert (await _invite(db, conn, profile_id=adult))["reason"] == (
            "profile_not_allowed"
        )
        # Kinds without guest: no invitation at all.
        other, _ = await _member(
            db, overrides={"setup_devices": {"enabled": True, "kinds": ["child"]}}
        )
        assert (await _invite(db, other))["reason"] == "not_permitted"

    async def test_access_windows_become_an_adjustment(self, db, events):
        """The member chooses access hours unless the profile fixes them."""
        await _defaults(db, all_areas=True)
        conn, _ = await _member(db)
        windows = [{"days": ["sat", "sun"], "start": "10:00", "end": "18:00"}]
        msg = await _invite(db, conn, access_windows=windows)
        assert msg["type"] == "guest_invite"
        assert msg["grant"]["access_hours"] == {"enabled": True, "windows": windows}
        ok, guest_id, _ = await _pair(db, msg["pin"])
        assert ok
        eff = (await ws._get_client_config(db, guest_id))["rules"]["access_hours"]
        assert eff["value"] == {"enabled": True, "windows": windows}
        assert eff["source"] == "device"
        bad = await _invite(
            db, conn, access_windows=[{"days": [], "start": "10:00", "end": "18:00"}]
        )
        assert bad["reason"] == "invalid_request"
        assert (await _invite(db, conn, access_windows=[]))[
            "reason"
        ] == "invalid_request"
        guest_profile = await starter_profile(db, "guest")
        await profiles_api.update_profile(
            _request(db),
            guest_profile,
            profiles_api.ProfileWrite(fixed=["access_hours", "control_path"]),
            dry_run=False,
        )
        assert (await _invite(db, conn, access_windows=windows))["reason"] == (
            "access_hours_not_allowed"
        )
        msg = await _invite(db, conn)
        assert msg["type"] == "guest_invite"

    async def test_own_access_hours_bound_the_invitation(self, db, events):
        """A member with access hours invites nobody with access
        outside them."""
        from datetime import datetime, timezone

        from services import kids_mode

        kids_mode.reset()
        kids_mode.remember_time_zone("UTC")  # the windows below are in UTC
        await _defaults(db, all_areas=True)
        gap = 10 if datetime.now(timezone.utc).hour == 3 else 3
        every_day = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        mine = {
            "enabled": True,
            "windows": [
                {
                    "days": every_day,
                    "start": f"{gap + 1:02d}:00",
                    "end": f"{gap:02d}:00",
                }
            ],
        }
        conn, _ = await _member(db, overrides={"access_hours": mine})
        # The guest profile has no access hours: more than the member has.
        assert (await _invite(db, conn))["reason"] == "profile_not_allowed"
        guest_profile = await starter_profile(db, "guest")
        await profiles_api.update_profile(
            _request(db),
            guest_profile,
            profiles_api.ProfileWrite(rules={"access_hours": mine}),
            dry_run=False,
        )
        wider = [{"days": every_day, "start": "00:00", "end": "00:00"}]
        assert (await _invite(db, conn, access_windows=wider))["reason"] == (
            "access_hours_not_allowed"
        )
        inside = [{"days": ["sat"], "start": "10:00", "end": "18:00"}]
        assert (await _invite(db, conn, access_windows=inside))["type"] == (
            "guest_invite"
        )


# ── Admin ───────────────────────────────────────────────────────────────────


class TestAdmin:
    async def test_tag_catalog_lists_area_tags(self, db, events):
        await _area(db, "Küche", tags=("gast",))
        tags = await guests_api.get_all_tags(_request(db))
        assert tags == {"tags": ["gast"]}

    async def test_admin_cancels_an_invitation(self, db, events):
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        invite = await _invite(db, conn)
        await guests_api.cancel_guest_invite(_request(db), invite["invite_id"])
        assert _ended(events, member)[-1]["result"] == "revoked"
        with pytest.raises(HTTPException) as exc:
            await guests_api.cancel_guest_invite(_request(db), invite["invite_id"])
        assert exc.value.status_code == 404

    async def test_kicking_the_member_ends_its_invitation(self, db, events):
        await _defaults(db, all_areas=True)
        conn, member = await _member(db)
        await _invite(db, conn)
        await clients_api.kick_client(_request(db), member)
        assert _ended(events, member)[-1]["result"] == "revoked"
        assert guest_invites.open_invites() == []

    async def test_admin_only(self, db, events):
        with pytest.raises(HTTPException):
            await guests_api.list_guest_invites(_request(db, auth="client"))
        assert not hasattr(guests_api, "get_guest_defaults")


async def test_activity_names_the_device_by_its_current_name(db):
    """A renamed guest (or added device) shows its current name in the
    activity of the inviting device; a deleted one keeps the stored name."""
    member = await _client(db, name="iPhone Manager One")
    guest = await _client(db, name="Gast-Handy", is_guest=1)
    gone = str(uuid.uuid4())
    await client_activity.insert(
        db,
        member,
        client_activity.GUEST_INVITED,
        {"guest_client_id": guest, "guest_client_name": "Gast-Handy"},
    )
    await client_activity.insert(
        db,
        member,
        client_activity.DEVICE_ADDED,
        {"added_client_id": gone, "added_client_name": "Altes TV"},
    )
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET name = ? WHERE id = ?", ("Guest iPhone", guest)
        )
    events = await client_activity.list_events(db, member)
    assert events[0]["detail"]["added_client_name"] == "Altes TV"
    assert events[1]["detail"] == {
        "guest_client_id": guest,
        "guest_client_name": "Guest iPhone",
    }
    stored = await db.fetchone(
        "SELECT detail FROM client_activity WHERE event = 'guest_invited'"
    )
    assert json.loads(stored["detail"])["guest_client_name"] == "Gast-Handy"


async def test_no_part_of_the_pin_in_the_log(db, events, caplog, monkeypatch):
    """The log line of an opened invitation wrote the first half of
    the guest PIN."""
    # Letters no id or timestamp in a log line contains.
    monkeypatch.setattr(pairing, "_generate_pin", lambda: "QQQQ-WWWW")
    conn, _member_id = await _member(db)
    await _defaults(db, all_areas=True)
    with caplog.at_level("DEBUG"):
        invite = await _invite(db, conn)
    assert invite["type"] == "guest_invite"
    assert invite["pin"] == "QQQQ-WWWW"
    halves = ("QQQQ", "WWWW")
    assert any("Guest invitation" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        for half in halves:
            assert half not in record.getMessage()


class TestPlatform:
    async def test_an_invitation_on_a_tv_is_refused_and_stays_open(self, db, events):
        """A guest is offered only on the platforms of its kind: a device
        that reports a TV or a watch gets pair_error platform_not_allowed, and
        the invitation still pairs the guest's phone."""
        await _defaults(db, all_areas=True)
        conn, _ = await _member(db)
        invite = await _invite(db, conn)
        for platform in ("tvos", "android_tv", "watchos", "wearos"):
            guest = _WS(db, host="198.51.100.9")
            ok, _cid = await ws._handle_pair(
                guest,
                db,
                {
                    "pin": invite["pin"],
                    "client_name": "Gast-TV",
                    **{**CLIENT_INFO, "platform": platform},
                },
            )
            assert not ok
            assert guest.last()["reason"] == "platform_not_allowed"
        ok, _cid, _guest = await _pair(db, invite["pin"])
        assert ok

    async def test_a_guest_pin_on_a_tv_is_refused(self, db, events):
        pairing.set_active_pin(
            "1234-5678",
            {
                "created_at": 0,
                "expires_at": 9e12,
                "is_guest": True,
                "guest_expires_at": "2099-01-01T00:00:00+00:00",
            },
        )
        guest = _WS(db, host="198.51.100.10")
        ok, _cid = await ws._handle_pair(
            guest,
            db,
            {
                "pin": "1234-5678",
                "client_name": "Gast-TV",
                **{**CLIENT_INFO, "platform": "tvos"},
            },
        )
        assert not ok and guest.last()["reason"] == "platform_not_allowed"
        # The attempt counts against the limits like a wrong PIN.
        assert pairing.get_active_pin()[1]["failed_attempts"] == 1
        # The PIN is not used up: the guest's phone still pairs with it.
        ok, _cid, _phone = await _pair(db, "1234-5678")
        assert ok
