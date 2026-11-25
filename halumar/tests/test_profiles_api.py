"""Profile model: admin API, preview, adjustments, client_config.

Covers the endpoints of api/profiles.py against the contract
(openapi/hub.openapi.json, tag profiles): starter profiles of a fresh hub,
profile CRUD with validation and limits, delete with replacement, duplicate,
suggestions with versions, profile from device, default profiles, device
adjustments (permanent, temporary, boundaries), own area, remote PIN in device
and profile, bulk, preview (dry run), audit, the expiry planner and the
delivery in client_config (contract schema when CONTRACTS_DIR is set).
"""

import json
import os
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import api.websocket as ws
import pytest
from brand import BRAND_NAME
from api import authz
from api import profiles as api
from fastapi import HTTPException
from models.database import Database
from services import crypto, hub_control, kids_mode
from services import device_provisioning as dp
from services.profiles import catalog as cat
from services.profiles import expiry_task, store

from .profile_helpers import add_client, starter_profile

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    kids_mode.reset()
    yield
    crypto.reset_cache()


@pytest.fixture
def events(monkeypatch):
    log = {"to": [], "broadcast": [], "integration": []}

    async def _send(client_id, message):
        log["to"].append((client_id, message))
        return True

    async def _broadcast(message, *a, **k):
        log["broadcast"].append(message)

    async def _sender(message):
        log["integration"].append(message)
        return True

    monkeypatch.setattr(ws, "send_to_client", _send)
    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(dp, "_sender", _sender)
    return log


def _req(db, auth="ingress", actor="Admin One"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type=auth, actor=actor),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


async def _area(db, name, tags=()):
    import uuid

    area_id = str(uuid.uuid4())
    now = Database.now()
    async with db.transaction():
        await db.execute(
            "INSERT INTO areas (id, name, sort_order, is_active, created_at, updated_at) "
            "VALUES (?, ?, 0, 1, ?, ?)",
            (area_id, name, now, now),
        )
        for tag in tags:
            await db.execute(
                "INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", (area_id, tag)
            )
    return area_id


async def _audit(db, client_id=None, profile_id=None, limit=100):
    return await api.get_profile_audit(
        _req(db), client_id=client_id, profile_id=profile_id, limit=limit
    )


async def _rules(db, client_id):
    return (await store.resolve_client(db, client_id))[2]


async def _override(db, client_id, rule, value, dry_run=False, **extra):
    return await api.put_client_override(
        _req(db),
        client_id,
        rule,
        api.DeviceOverride(value=value, **extra),
        dry_run=dry_run,
    )


# ── fresh hub ───────────────────────────────────────────────────────────────


async def test_fresh_hub_has_the_starters_and_default_profiles(db):
    listed = await api.list_profiles(_req(db))
    names = sorted(p["name"] for p in listed["profiles"])
    assert names == ["Erwachsene Person", "Fernseher", "Gast", "Kind", "Wandpanel"]
    assert listed["max_profiles"] == 50
    settings = await api.get_profile_settings(_req(db))
    assert settings["guest_default_hours"] == 24
    defaults = settings["default_profiles"]
    assert defaults["ios"] == defaults["watchos"] == defaults["android"]
    assert defaults["android"] == defaults["wearos"]
    assert defaults["ios"] == await starter_profile(db, "adult")
    assert defaults["ipados"] == defaults["android_tablet"]
    assert defaults["ipados"] == await starter_profile(db, "wall_panel")
    assert defaults["tvos"] == defaults["android_tv"]
    assert defaults["tvos"] == await starter_profile(db, "apple_tv")
    assert defaults["guest"] == await starter_profile(db, "guest")
    adult = next(p for p in listed["profiles"] if p["name"] == "Erwachsene Person")
    assert adult["origin"] == {"suggestion_id": "adult", "version": 5}
    assert adult["suggestion_update"] is None and adult["catalog_defaults"] == []
    # Sensors in HA on, no control from HA.
    assert adult["rules"]["panel_remote"] is False
    assert adult["rules"]["panel_sensors"] == [
        "battery",
        "charging",
        "screen",
        "brightness",
    ]
    assert set(adult["default_for"]) == {"ios", "watchos", "android", "wearos"}
    assert adult["rules"]["remote_access"] is True
    assert set(adult["rules"]) == {r["key"] for r in cat.catalog()["rules"]}


async def test_catalog_and_gallery(db):
    rules = await api.get_profile_rules(_req(db))
    assert rules == cat.catalog()
    gallery = await api.list_profile_suggestions(_req(db))
    assert len(gallery["suggestions"]) == 14
    parent = next(s for s in gallery["suggestions"] if s["id"] == "parent")
    assert parent["profile_ids"] == []
    adult = next(s for s in gallery["suggestions"] if s["id"] == "adult")
    assert adult["profile_ids"] == [await starter_profile(db, "adult")]
    teen = next(s for s in gallery["suggestions"] if s["id"] == "teen")
    assert teen["profile_ids"] == []


async def test_admin_only(db):
    for call in (
        api.list_profiles(_req(db, auth="client")),
        api.get_profile_rules(_req(db, auth="client")),
        api.get_profile_settings(_req(db, auth="integration")),
    ):
        with pytest.raises(HTTPException) as e:
            await call
        assert e.value.status_code == 403


# ── profiles ────────────────────────────────────────────────────────────────


async def test_create_validates_and_audits(db, events):
    p = await api.create_profile(
        _req(db),
        api.ProfileWrite(
            name="  Oma  ",
            icon="mdi:human-cane",
            rules={"control_mode_max": "basic", "remote_access": True},
            fixed=["remote_access"],
        ),
    )
    assert p["name"] == "Oma" and p["icon"] == "mdi:human-cane" and p["rev"] == 1
    assert p["rules"]["control_mode_max"] == "basic"
    assert "remote_access" in p["fixed"] and "kids_exceptions" in p["fixed"]
    assert p["origin"] is None and p["device_count"] == 0
    assert p["updated_by"] == "Admin One"
    for body, code in (
        (api.ProfileWrite(name="oma"), "name.duplicate"),
        (api.ProfileWrite(name="X", rules={"columns": 9}), "rules.columns"),
        (api.ProfileWrite(name="X", rules={"nope": 1}), "rules.nope"),
        (api.ProfileWrite(name="X", fixed=["nope"]), "fixed.nope"),
        (
            api.ProfileWrite(name="X", user_choice=["remote_access"]),
            "user_choice.remote_access",
        ),
        (api.ProfileWrite(name=" "), "name"),
    ):
        with pytest.raises(HTTPException) as e:
            await api.create_profile(_req(db), body)
        assert e.value.status_code == 422 and e.value.detail["code"] == code
    audit = await _audit(db, profile_id=p["id"])
    assert audit["entries"][0]["action"] == "profile_created"
    assert audit["entries"][0]["actor"] == "Admin One"


async def test_setup_devices_may_allow_every_profile_kind(db, events):
    """All 7 kinds incl. babysitter and person are accepted (the value
    schema allows every kind, no 422 "too many items")."""
    kinds = [k["id"] for k in cat.catalog()["profile_kinds"]]
    assert len(kinds) == 7 and "babysitter" in kinds
    p = await api.create_profile(
        _req(db),
        api.ProfileWrite(
            name="Alle Arten",
            rules={"setup_devices": {"enabled": True, "kinds": kinds}},
        ),
    )
    assert p["rules"]["setup_devices"] == {"enabled": True, "kinds": kinds}
    with pytest.raises(HTTPException) as e:
        await api.create_profile(
            _req(db),
            api.ProfileWrite(
                name="Doppelt",
                rules={"setup_devices": {"enabled": True, "kinds": ["guest", "guest"]}},
            ),
        )
    assert e.value.status_code == 422
    assert e.value.detail["code"] == "rules.setup_devices"


async def test_description_is_written_and_cleared(db, events):
    p = await api.create_profile(
        _req(db), api.ProfileWrite(name="Opa", description="  Sieht  alles ")
    )
    assert p["description"] == "Sieht alles"
    p = await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(description="Neu"), dry_run=False
    )
    assert p["description"] == "Neu"
    p = await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(rules={"columns": 3}), dry_run=False
    )
    assert p["description"] == "Neu"
    p = await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(description=""), dry_run=False
    )
    assert p["description"] is None


async def test_profile_limit(db, monkeypatch):
    monkeypatch.setitem(cat.catalog()["limits"], "max_profiles", 6)
    await api.create_profile(_req(db), api.ProfileWrite(name="Sechs"))
    with pytest.raises(HTTPException) as e:
        await api.create_profile(_req(db), api.ProfileWrite(name="Sieben"))
    assert e.value.detail["code"] == "profile.limit"


async def test_patch_merges_rules_fixes_and_drops_adjustments(db, events):
    adult = await starter_profile(db, "adult")
    phone = await add_client(db, name="Phone", overrides={"camera_live": False})
    other = await add_client(db, name="Other", overrides={"camera_live": False})
    assert (await _rules(db, phone))["camera_live"]["source"] == "device"
    preview = await api.update_profile(
        _req(db), adult, api.ProfileWrite(fixed=["camera_live"]), dry_run=True
    )
    assert preview.status_code == 202
    body = json.loads(preview.body)
    assert body["dry_run"] is True and body["affected_count"] == 2
    dev = next(d for d in body["devices"] if d["client_id"] == phone)
    assert dev["overrides_removed"] == ["camera_live"]
    change = next(c for c in dev["changes"] if c["rule"] == "camera_live")
    assert change["before"]["value"] is False and change["after"]["value"] is True
    assert change["loosens"] is True and "camera_live" in body["loosened_rules"]
    # Nothing was saved.
    assert (await _rules(db, phone))["camera_live"]["source"] == "device"
    p = await api.update_profile(
        _req(db),
        adult,
        api.ProfileWrite(rules={"columns": 3}, fixed=["camera_live"]),
        dry_run=False,
    )
    assert p["rev"] == 2 and p["rules"]["columns"] == 3
    assert p["rules"]["remote_access"] is True  # merged, not replaced
    assert (await _rules(db, phone))["camera_live"]["source"] == "profile"
    assert await store.overrides_of(db, other) == []
    assert (phone, {"type": "client_config_updated", "data": {}}) in events["to"]
    audit = await _audit(db, profile_id=adult)
    entry = audit["entries"][0]
    assert entry["action"] == "profile_updated"
    assert "camera_live" in entry["after"]["fixed"]
    assert "camera_live" not in entry["before"]["fixed"]
    revs = await db.fetchall(
        "SELECT rev FROM profile_revisions WHERE profile_id = ? ORDER BY rev", (adult,)
    )
    assert [r["rev"] for r in revs] == [1, 2]


async def test_adjustment_equal_to_the_profile_value_is_removed(db, events):
    adult = await starter_profile(db, "adult")
    phone = await add_client(db, overrides={"camera_live": False, "columns": 3})
    await api.update_profile(
        _req(db),
        adult,
        api.ProfileWrite(rules={"camera_live": False, "columns": 3}),
        dry_run=False,
    )
    # columns stays: the adjustment locks it, the profile leaves it to the user.
    assert [o["rule_key"] for o in await store.overrides_of(db, phone)] == ["columns"]


async def test_child_protection_switch_and_guest_boundary(db, events, monkeypatch):
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    p = await api.create_profile(_req(db), api.ProfileWrite(name="Familie"))
    device = await add_client(
        db, profile_id=p["id"], ha_token_status="active", remote=False
    )
    # Remote access stays a normal rule with parental controls;
    # a device with it keeps its (read-only) key.
    away = await add_client(
        db, profile_id=p["id"], ha_token_status="active", remote=True
    )
    preview = json.loads(
        (
            await api.update_profile(
                _req(db), p["id"], api.ProfileWrite(child_protection=True), dry_run=True
            )
        ).body
    )
    devs = {d["client_id"]: d for d in preview["devices"]}
    assert devs[device]["ha_key"] == "removed"
    assert (
        devs[away]["ha_key"] == "unchanged"
        and devs[away]["remote_access"] == "unchanged"
    )
    await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(child_protection=True), dry_run=False
    )
    rotated = [
        m["data"]["client_id"]
        for m in events["integration"]
        if m["type"] == "provision_client" and m["data"].get("token_action") == "rotate"
    ]
    assert rotated == [device]
    guest = await add_client(db, guest=True)
    assert guest
    p2 = await api.create_profile(_req(db), api.ProfileWrite(name="Gäste lang"))
    await api.assign_client_profile(
        _req(db), guest, api.ClientProfile(profile_id=p2["id"]), dry_run=False
    )
    with pytest.raises(HTTPException) as e:
        await api.update_profile(
            _req(db), p2["id"], api.ProfileWrite(child_protection=True), dry_run=False
        )
    assert e.value.detail["code"] == "profile.guest_child_protection"
    assert device


async def test_delete_needs_replacement_and_protects_defaults(db, events):
    adult = await starter_profile(db, "adult")
    with pytest.raises(HTTPException) as e:
        await api.delete_profile(_req(db), adult, replacement=None, dry_run=False)
    assert e.value.status_code == 409 and e.value.detail["code"] == "profile_is_default"
    p = await api.create_profile(_req(db), api.ProfileWrite(name="Alt"))
    kid = await starter_profile(db, "child")
    member = await add_client(db, profile_id=p["id"], overrides={"columns": 1})
    guest = await add_client(db, guest=True, profile_id=p["id"])
    with pytest.raises(HTTPException) as e:
        await api.delete_profile(_req(db), p["id"], replacement=None, dry_run=False)
    assert e.value.detail["code"] == "profile_in_use"
    with pytest.raises(HTTPException) as e:
        await api.delete_profile(_req(db), p["id"], replacement=kid, dry_run=False)
    assert e.value.detail["code"] == "profile.guest_child_protection"
    preview = await api.delete_profile(
        _req(db), p["id"], replacement=adult, dry_run=True
    )
    assert preview.status_code == 202
    assert await store.profile_row(db, p["id"])
    await api.delete_profile(_req(db), p["id"], replacement=adult, dry_run=False)
    assert await store.profile_row(db, p["id"]) is None
    for cid in (member, guest):
        row = await store.client_row(db, cid)
        assert row["profile_id"] == adult
    assert len(await store.overrides_of(db, member)) == 1


async def test_last_profile_cannot_be_deleted(db, events):
    async with db.transaction():
        await db.execute("DELETE FROM settings WHERE key LIKE 'default_profile_%'")
        await db.execute("DELETE FROM profiles WHERE origin_suggestion != 'adult'")
    with pytest.raises(HTTPException) as e:
        await api.delete_profile(
            _req(db),
            await starter_profile(db, "adult"),
            replacement=None,
            dry_run=False,
        )
    assert e.value.detail["code"] == "last_profile"


async def test_duplicate_and_suggestion_copies(db, events):
    adult = await starter_profile(db, "adult")
    copy_ = await api.duplicate_profile(_req(db), adult, None)
    assert copy_["name"] == "Kopie von Erwachsene Person" and copy_["origin"] is None
    assert copy_["rules"] == (await api.get_profile(_req(db), adult))["rules"]
    again = await api.add_profile_suggestion(_req(db), "adult", None)
    assert again["name"] == "Erwachsene Person (2)"
    assert again["origin"] == {"suggestion_id": "adult", "version": 5}
    teen = await api.add_profile_suggestion(
        _req(db), "teen", api.ProfileName(name="Teen One")
    )
    assert teen["name"] == "Teen One" and teen["child_protection"] is True
    with pytest.raises(HTTPException) as e:
        await api.add_profile_suggestion(_req(db), "nope", None)
    assert e.value.status_code == 404


async def test_new_suggestion_version_is_only_a_hint(db, events, monkeypatch):
    """Add-on updates never change profiles: a newer version is offered."""
    adult = await starter_profile(db, "adult")
    phone = await add_client(db)
    before = await api.get_profile(_req(db), adult)
    with pytest.raises(HTTPException) as e:
        await api.apply_profile_suggestion(_req(db), adult, dry_run=False)
    assert e.value.detail["code"] == "no_suggestion_update"
    sug = cat.suggestion("adult")
    monkeypatch.setitem(sug, "version", 6)
    monkeypatch.setitem(sug, "rules", {**sug["rules"], "camera_live": False})
    hinted = await api.get_profile(_req(db), adult)
    assert hinted["suggestion_update"] == {"version": 6}
    assert hinted["rules"] == before["rules"]  # unchanged until applied
    preview = json.loads(
        (await api.apply_profile_suggestion(_req(db), adult, dry_run=True)).body
    )
    assert preview["devices"][0]["client_id"] == phone
    applied = await api.apply_profile_suggestion(_req(db), adult, dry_run=False)
    assert applied["origin"]["version"] == 6 and applied["suggestion_update"] is None
    assert applied["rules"]["camera_live"] is False
    assert applied["name"] == "Erwachsene Person"


async def test_new_catalog_rule_uses_the_default_until_saved(db, events):
    adult = await starter_profile(db, "adult")
    row = await store.profile_row(db, adult)
    body = json.loads(row["body_json"])
    del body["rules"]["notification_level"]
    async with db.transaction():
        await db.execute(
            "UPDATE profiles SET body_json = ? WHERE id = ?", (json.dumps(body), adult)
        )
    view = await api.get_profile(_req(db), adult)
    assert view["catalog_defaults"] == ["notification_level"]
    phone = await add_client(db)
    rule = (await _rules(db, phone))["notification_level"]
    assert rule["source"] == "default" and rule["reason"] == "user_choice"


async def test_profile_from_device(db, events):
    phone = await add_client(
        db, name="Manager One", overrides={"camera_live": False, "remote_access": False}
    )
    p = await api.create_profile_from_device(
        _req(db), api.FromDevice(client_id=phone, name="Wie Manager One")
    )
    assert p["rules"]["camera_live"] is False and p["rules"]["remote_access"] is False
    assert p["device_count"] == 1
    assert await store.overrides_of(db, phone) == []
    rules = await _rules(db, phone)
    assert rules["camera_live"]["value"] is False
    assert rules["camera_live"]["source"] == "profile"
    keep = await add_client(db, overrides={"columns": 1})
    p2 = await api.create_profile_from_device(
        _req(db), api.FromDevice(client_id=keep, name="Kopie", assign=False)
    )
    assert p2["rules"]["columns"] == 1 and p2["device_count"] == 0
    assert len(await store.overrides_of(db, keep)) == 1


async def test_default_profiles_settings(db, events):
    settings = await api.get_profile_settings(_req(db))
    kid = await starter_profile(db, "child")
    bad = dict(settings["default_profiles"], guest=kid)
    with pytest.raises(HTTPException) as e:
        await api.put_profile_settings(
            _req(db), api.ProfileSettings(default_profiles=bad, guest_default_hours=24)
        )
    assert e.value.detail["code"] == "profile.guest_child_protection"
    good = dict(
        settings["default_profiles"], ipados=settings["default_profiles"]["ios"]
    )
    out = await api.put_profile_settings(
        _req(db), api.ProfileSettings(default_profiles=good, guest_default_hours=72)
    )
    assert out["guest_default_hours"] == 72
    assert await store.default_profile_for(db, "ipados", False) == good["ios"]
    with pytest.raises(Exception):
        api.ProfileSettings(default_profiles=good, guest_default_hours=2161)


# ── device adjustments ──────────────────────────────────────────────────────


async def test_adjustment_rules_and_errors(db, events):
    phone = await add_client(db, platform="ios")
    eff = await _override(db, phone, "camera_live", False, note="Kinder zu Besuch")
    assert eff["rules"]["camera_live"]["source"] == "device"
    assert eff["rules"]["camera_live"]["profile_value"] is True
    assert eff["overrides"][0]["note"] == "Kinder zu Besuch"
    for rule, value, extra, code in (
        ("kids_volume_max", 10, {}, "override.not_applicable"),
        ("kiosk", True, {}, "override.not_applicable"),  # iPhone
        ("columns", 9, {}, "override.value"),
        ("remote_access", True, {"user_choice": True}, "override.user_choice"),
        (
            "camera_live",
            True,
            {"expires": "1h", "expires_at": "2099-01-01T00:00:00Z"},
            "override.expires_at",
        ),
        (
            "camera_live",
            True,
            {"expires_at": "2026-01-01T00:00:00"},
            "override.expires_at",
        ),
    ):
        with pytest.raises(HTTPException) as e:
            await _override(db, phone, rule, value, **extra)
        assert e.value.status_code == 422 and e.value.detail["code"] == code, rule
    adult = await starter_profile(db, "adult")
    await api.update_profile(
        _req(db), adult, api.ProfileWrite(fixed=["columns"]), dry_run=False
    )
    with pytest.raises(HTTPException) as e:
        await _override(db, phone, "columns", 1)
    assert e.value.detail["code"] == "override.fixed"
    guest = await add_client(db, guest=True)
    with pytest.raises(HTTPException) as e:
        await _override(db, guest, "app_settings", "open")
    assert e.value.detail["code"] == "override.boundary"
    with pytest.raises(HTTPException) as e:
        await _override(db, "nope", "camera_live", True)
    assert e.value.status_code == 404


async def test_user_choice_adjustment(db, events):
    phone = await add_client(db)
    eff = await _override(db, phone, "card_style", "color", user_choice=False)
    rule = eff["rules"]["card_style"]
    assert rule["value"] == "color" and rule["locked"] is True
    assert rule["user_choice"] is False and rule["reason"] == "device"


async def test_temporary_adjustment_and_expiry_planner(db, events, monkeypatch):
    # A member with the profile "Gast" (remote access off, not fixed).
    phone = await add_client(db, profile="guest", ha_token_status="active")
    await db.set_setting("ha_url", "https://ha.example.com")
    end = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
    eff = await _override(db, phone, "remote_access", True, expires_at=end.isoformat())
    rule = eff["rules"]["remote_access"]
    assert rule["reason"] == "device_temporary" and rule["then"] is False
    assert rule["expires_at"] == end.isoformat().replace("+00:00", "Z")
    cfg = await ws._get_client_config(db, phone)
    assert cfg["ha_url"] == "https://ha.example.com"
    assert cfg["rules"]["remote_access"]["then"] is False
    assert any(m["type"] == "update_client_identity" for m in events["integration"])
    events["integration"].clear()
    events["to"].clear()
    # Nothing to end yet.
    assert await expiry_task.expire_due(db) == []
    ended = await expiry_task.expire_due(db, now=end + timedelta(seconds=1))
    assert ended == [phone]
    assert await store.overrides_of(db, phone) == []
    identity = [
        m for m in events["integration"] if m["type"] == "update_client_identity"
    ]
    assert identity and identity[0]["data"]["remote_allowed"] is False
    assert (phone, {"type": "client_config_updated", "data": {}}) in events["to"]
    audit = await _audit(db, client_id=phone)
    assert audit["entries"][0]["action"] == "override_expired"
    assert audit["entries"][0]["actor"] == BRAND_NAME


async def test_expiry_of_remote_access_rotates_the_key_of_a_child(
    db, events, monkeypatch
):
    """The end of a temporary remote access of a device with parental
    controls withdraws the HA key like the admin's delete does."""
    monkeypatch.setattr(ws, "get_integration_push_connected", lambda: True)
    child = await starter_profile(db, "child")
    a = await add_client(db, profile_id=child, ha_token_status="active", name="A")
    b = await add_client(db, profile_id=child, ha_token_status="active", name="B")
    end = (datetime.now(UTC) + timedelta(hours=2)).replace(microsecond=0)
    for cid in (a, b):
        await _override(db, cid, "remote_access", True, expires_at=end.isoformat())
    events["integration"].clear()

    def rotated():
        return [
            m["data"]["client_id"]
            for m in events["integration"]
            if m["type"] == "provision_client"
            and m["data"].get("token_action") == "rotate"
        ]

    await api.delete_client_override(_req(db), a, "remote_access", dry_run=False)
    assert rotated() == [a]
    events["integration"].clear()
    events["to"].clear()
    assert await expiry_task.expire_due(db, now=end + timedelta(seconds=1)) == [b]
    assert (await _rules(db, b))["remote_access"]["value"] is False
    assert rotated() == [b]
    assert (b, {"type": "client_config_updated", "data": {}}) in events["to"]


async def test_expiry_presets_use_the_hub_time_zone(db, events, monkeypatch):
    async def _tz(_ha):
        return "Europe/Zurich"

    monkeypatch.setattr(kids_mode, "hub_time_zone", _tz)
    phone = await add_client(db)
    eff = await _override(db, phone, "camera_live", False, expires="tomorrow_morning")
    end = datetime.fromisoformat(
        eff["rules"]["camera_live"]["expires_at"].replace("Z", "+00:00")
    )
    local = end.astimezone(__import__("zoneinfo").ZoneInfo("Europe/Zurich"))
    assert (local.hour, local.minute) == (7, 0)


async def test_delete_adjustment_and_dry_run(db, events):
    phone = await add_client(db, overrides={"camera_live": False})
    preview = await api.delete_client_override(
        _req(db), phone, "camera_live", dry_run=True
    )
    assert preview.status_code == 202
    assert len(await store.overrides_of(db, phone)) == 1
    eff = await api.delete_client_override(
        _req(db), phone, "camera_live", dry_run=False
    )
    assert eff["rules"]["camera_live"]["source"] == "profile"
    with pytest.raises(HTTPException) as e:
        await api.delete_client_override(_req(db), phone, "camera_live", dry_run=False)
    assert e.value.status_code == 404


async def test_assign_with_and_without_discarding(db, events):
    phone = await add_client(db, overrides={"columns": 1, "remote_access": False})
    panel = await starter_profile(db, "wall_panel")
    result = await api.assign_client_profile(
        _req(db), phone, api.ClientProfile(profile_id=panel), dry_run=False
    )
    assert result.profile_id == panel and result.profile_name == "Wandpanel"
    # The wall panel fixes remote_access: that adjustment is gone, columns stays.
    assert [o["rule_key"] for o in await store.overrides_of(db, phone)] == ["columns"]
    adult = await starter_profile(db, "adult")
    await api.assign_client_profile(
        _req(db),
        phone,
        api.ClientProfile(profile_id=adult, discard_overrides=True),
        dry_run=False,
    )
    assert await store.overrides_of(db, phone) == []


async def test_own_area_feeds_areas_and_panel_start(db, events):
    kids_room = await _area(db, "Kinderzimmer")
    await _area(db, "Wohnzimmer")
    guest = await add_client(db, guest=True)
    assert await authz.listed_area_ids(db, guest) == set()  # no own area: nothing
    eff = await api.put_client_own_area(
        _req(db), guest, api.OwnArea(area_id=kids_room), dry_run=False
    )
    assert eff["own_area_id"] == kids_room
    assert eff["rules"]["areas"]["value"]["area_ids"] == [kids_room]
    assert await authz.listed_area_ids(db, guest) == {kids_room}
    panel = await add_client(db, platform="ipados", own_area_id=kids_room)
    assert (await _rules(db, panel))["panel_start"]["value"] == {
        "mode": "area",
        "area_id": kids_room,
    }
    # A deleted area clears the own area (fail closed).
    async with db.transaction():
        await db.execute("DELETE FROM areas WHERE id = ?", (kids_room,))
    assert (await store.client_row(db, guest))["own_area_id"] is None
    with pytest.raises(HTTPException) as e:
        await api.put_client_own_area(
            _req(db), guest, api.OwnArea(area_id="nope"), dry_run=False
        )
    assert e.value.status_code == 404


async def test_remote_pin_of_device_and_profile(db, events):
    phone = await add_client(db, overrides={"app_settings": "pin"})
    assert (await _rules(db, phone))["app_settings"]["value"] == "hidden"
    eff = await api.put_client_remote_pin(
        _req(db), phone, api.RemotePin(pin="12345678")
    )
    assert eff["has_remote_pin"] is True and eff["remote_pin_source"] == "device"
    assert eff["rules"]["app_settings"]["value"] == "pin"
    cfg = await ws._get_client_config(db, phone)
    assert cfg["settings_pin_source"] == "device" and cfg["settings_pin_hash"]
    row = await db.fetchone("SELECT * FROM paired_clients WHERE id = ?", (phone,))
    assert "12345678" not in json.dumps(row)
    # Profile PIN: a device PIN wins unless the profile PIN is fixed.
    adult = await starter_profile(db, "adult")
    preview = await api.update_profile(
        _req(db), adult, api.ProfileWrite(remote_pin="87654321"), dry_run=True
    )
    assert "87654321" not in preview.body.decode()
    view = await api.update_profile(
        _req(db), adult, api.ProfileWrite(remote_pin="87654321"), dry_run=False
    )
    assert view["has_remote_pin"] is True and view["remote_pin_fixed"] is False
    assert (await ws._get_client_config(db, phone))["settings_pin_source"] == "device"
    other = await add_client(db, overrides={"app_settings": "pin"})
    cfg = await ws._get_client_config(db, other)
    assert cfg["rules"]["app_settings"]["value"] == "pin"
    assert cfg["settings_pin_source"] == "profile"
    await api.update_profile(
        _req(db), adult, api.ProfileWrite(remote_pin_fixed=True), dry_run=False
    )
    assert (await ws._get_client_config(db, phone))["settings_pin_source"] == "profile"
    with pytest.raises(HTTPException) as e:
        await api.put_client_remote_pin(_req(db), phone, api.RemotePin(pin="11112222"))
    assert e.value.detail["code"] == "remote_pin.fixed"
    await api.put_client_remote_pin(_req(db), phone, api.RemotePin(pin=None))
    audit = await _audit(db, limit=500)
    text = json.dumps(audit)
    assert "87654321" not in text and "12345678" not in text
    assert any(e["action"] == "profile_remote_pin_set" for e in audit["entries"])


def _pushed(events, cid):
    return [
        m for c, m in events["to"] if c == cid and m["type"] == "client_config_updated"
    ]


async def test_profile_pin_and_pin_fixed_are_pushed(db, events):
    """A new profile PIN or remote_pin_fixed changes the PIN the
    device's settings open with; the device is told."""
    p = await api.create_profile(
        _req(db),
        api.ProfileWrite(
            name="Pin", rules={"app_settings": "pin"}, remote_pin="11111111"
        ),
    )
    phone = await add_client(db, profile_id=p["id"])
    cfg1 = await ws._get_client_config(db, phone)
    events["to"].clear()
    await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(remote_pin="22222222"), dry_run=False
    )
    cfg2 = await ws._get_client_config(db, phone)
    assert cfg2["settings_pin_hash"] != cfg1["settings_pin_hash"]
    assert _pushed(events, phone)
    # A device PIN wins until the profile PIN is fixed.
    await api.put_client_remote_pin(_req(db), phone, api.RemotePin(pin="33333333"))
    assert (await ws._get_client_config(db, phone))["settings_pin_source"] == "device"
    events["to"].clear()
    await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(remote_pin_fixed=True), dry_run=False
    )
    assert (await ws._get_client_config(db, phone))["settings_pin_source"] == "profile"
    assert _pushed(events, phone)
    # The preview never mentions the PIN.
    preview = await api.update_profile(
        _req(db), p["id"], api.ProfileWrite(remote_pin="44444444"), dry_run=True
    )
    assert json.loads(preview.body)["affected_count"] == 0


async def test_fixed_flag_and_new_expiry_are_pushed(db, events):
    """Fixed and expires_at are part of client_config.rules and
    config_hash; a change of either is pushed."""
    phone = await add_client(db, profile="adult")
    end1 = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    await _override(db, phone, "camera_live", True, expires_at=end1.isoformat())
    cfg1 = await ws._get_client_config(db, phone)
    events["to"].clear()
    await _override(
        db,
        phone,
        "camera_live",
        True,
        expires_at=(end1 + timedelta(days=6)).isoformat(),
    )
    cfg2 = await ws._get_client_config(db, phone)
    assert cfg1["config_hash"] != cfg2["config_hash"]
    assert _pushed(events, phone)

    adult = await starter_profile(db, "adult")
    body = store.body_of(await store.profile_row(db, adult))
    assert "quick_access" not in body["fixed"]
    events["to"].clear()
    await api.update_profile(
        _req(db),
        adult,
        api.ProfileWrite(fixed=[*body["fixed"], "quick_access"]),
        dry_run=False,
    )
    cfg3 = await ws._get_client_config(db, phone)
    assert cfg3["rules"]["quick_access"]["fixed"] is True
    assert _pushed(events, phone)


async def test_bulk_actions_and_skips(db, events):
    a = await add_client(db, name="A")
    b = await add_client(db, name="B")
    g = await add_client(db, name="G", guest=True)
    kid = await starter_profile(db, "child")
    out = await api.bulk_update_clients(
        _req(db),
        api.Bulk(client_ids=[a, g, "nope"], action="assign_profile", profile_id=kid),
    )
    by = {r["client_id"]: r for r in out["results"]}
    assert by[a]["status"] == "changed"
    assert by[g] == {
        "client_id": g,
        "status": "skipped",
        "reason": "guest_child_protection",
    }
    assert by["nope"]["reason"] == "not_found"
    dry = await api.bulk_update_clients(
        _req(db),
        api.Bulk(
            client_ids=[b, g],
            action="set_override",
            rule="setup_devices",
            value={"enabled": True, "kinds": ["guest"]},
            dry_run=True,
        ),
    )
    assert dry["dry_run"] is True and dry["preview"]["affected_count"] == 1
    # The guest's profile is of kind guest: setup_devices does not apply.
    assert {r["client_id"]: r["reason"] for r in dry["results"]}[g] in (
        "boundary",
        "not_applicable",
    )
    assert await store.overrides_of(db, b) == []
    out = await api.bulk_update_clients(
        _req(db),
        api.Bulk(
            client_ids=[b],
            action="set_override",
            rule="setup_devices",
            value={"enabled": True, "kinds": ["guest"]},
        ),
    )
    assert out["results"][0]["status"] == "changed"
    assert (await _rules(db, b))["setup_devices"]["value"] == {
        "enabled": True,
        "kinds": ["guest"],
    }
    out = await api.bulk_update_clients(
        _req(db),
        api.Bulk(client_ids=[b], action="remove_override", rule="setup_devices"),
    )
    assert (await _rules(db, b))["setup_devices"]["value"]["enabled"] is False


# ── enforcement ─────────────────────────────────────────────────────────────


async def test_kids_control_follows_the_listed_areas_only(db, events, monkeypatch):
    """Open point of the spec: the hub enforces the area scope of a device with
    parental controls - an area a notification only links to is readable but
    not controllable."""
    from services.notifications import create_notification

    calls = []

    async def _caller(_ha, token, entity_id, action, data, grant):
        calls.append(entity_id)
        return {"entity_id": entity_id, "ok": True, "adjusted": None}

    monkeypatch.setattr(hub_control, "ha_caller", _caller)
    own = await _area(db, "Kinderzimmer")
    other = await _area(db, "Schlafzimmer Eltern")
    now = Database.now()
    async with db.transaction():
        for area_id, eid in ((own, "light.kids"), (other, "light.parents")):
            await db.execute(
                "INSERT INTO area_sections (id, area_id, title) VALUES (?, ?, 'S')",
                (area_id + "-s", area_id),
            )
            await db.execute(
                "INSERT INTO area_entities (id, area_id, section_id, entity_id, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (area_id + "-e", area_id, area_id + "-s", eid, now),
            )
    body = cat.materialize("child")
    body["rules"].update(
        {
            "areas": {"mode": "selected", "area_ids": [], "tags": [], "own_area": True},
            "kids_quiet_hours": [],
            "kids_rate_limit": None,
        }
    )
    from services.profiles import rows

    async with db.transaction():
        pid = await rows.insert_profile(
            db, name="Child One", body=body, now=now, actor=None
        )
    child = await add_client(
        db,
        profile_id=pid,
        own_area_id=own,
        ha_token_status="active",
        ha_token_encrypted=await crypto.encrypt_token(db, "tok"),
    )
    await create_notification(
        db,
        client_id=child,
        message="Schau mal",
        action_type="area",
        action_target=other,
    )
    assert await authz.can_read_area(db, child, other) is True
    ok = await hub_control.control(
        db, None, child, {"entity_id": "light.kids", "service": "turn_on"}
    )
    assert ok["type"] == "control_ok"
    denied = await hub_control.control(
        db, None, child, {"entity_id": "light.parents", "service": "turn_on"}
    )
    assert denied == {
        "type": "control_error",
        "request_id": None,
        "reason": "not_visible",
    }
    assert calls == ["light.kids"]


async def test_control_mode_is_clamped(db, events):
    await db.set_setting("default_control_mode", "full")
    area = await _area(db, "Wohnzimmer")
    async with db.transaction():
        await db.execute("UPDATE areas SET control_mode = 'full' WHERE id = ?", (area,))
    phone = await add_client(db, overrides={"control_mode_max": "basic"})
    cfg = await ws._get_client_config(db, phone)
    assert cfg["control_mode"] == "basic"
    areas = await ws._get_areas(
        db, await authz.client_area_scope(db, phone), control_mode_max="basic"
    )
    assert areas[0]["control_mode"] == "basic"


async def test_expired_guest_is_strict_everywhere(db, events):
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    guest = await add_client(db, guest=True, expires_at=past, tags=["*"], remote=True)
    rules = await _rules(db, guest)
    assert rules["remote_access"]["value"] is False
    assert rules["remote_access"]["reason"] == "access_expired"
    assert await authz.listed_area_ids(db, guest) == set()


# ── client_config shape ─────────────────────────────────────────────────────


def _contract_validator(schema: str = "client-config.json"):
    root = os.environ.get("CONTRACTS_DIR")
    if not root:
        root = str(
            Path(__file__).resolve().parents[4] / ".worktrees" / "contracts-profiles"
        )
    schema_path = Path(root) / "schemas" / schema
    if not schema_path.exists():
        return None
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")

    def retrieve(uri):
        path = Path(uri.replace("file://", ""))
        return referencing.Resource.from_contents(json.loads(path.read_text()))

    registry = referencing.Registry(retrieve=retrieve)
    schema = json.loads(schema_path.read_text())
    schema["$id"] = schema_path.as_uri()
    return jsonschema.Draft202012Validator(schema, registry=registry)


async def test_client_config_matches_the_contract(db, events):
    validator = _contract_validator()
    if validator is None:
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    room = await _area(db, "Kinderzimmer")
    devices = [
        await add_client(db, name="Phone", platform="ios"),
        await add_client(db, name="Panel", platform="ipados", own_area_id=room),
        await add_client(db, name="TV", platform="tvos"),
        await add_client(db, name="Watch", platform="watchos"),
        await add_client(db, name="Gast", guest=True, remote=True),
        await add_client(db, name="Kind", profile="child"),
        await add_client(
            db,
            name="Pin",
            overrides={"app_settings": "pin"},
            settings_pin_hash="a" * 64,
            settings_pin_salt="b" * 32,
        ),
    ]
    end = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    await _override(db, devices[0], "camera_live", False, expires_at=end)
    for cid in devices + ["ingress"]:
        cfg = await ws._get_client_config(db, cid)
        errors = sorted(validator.iter_errors(cfg), key=str)
        assert errors == [], (cid, [e.message for e in errors[:3]])


async def test_admin_answers_and_broadcast_match_the_contract(db, events):
    """effective, profile-audit, profiles_changed and the guest grant follow the
    contract (summary gone, profile instead of template_name)."""
    from services import guest_invites

    checks = {
        name: _contract_validator(name)
        for name in (
            "rest/client-effective.response.json",
            "rest/profile-audit.response.json",
            "ws/broadcast/profiles_changed.json",
            "common/defs.json",
        )
    }
    if None in checks.values():
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    room = await _area(db, "Kinderzimmer")
    phone = await add_client(db, name="Phone", platform="ios")
    await api.put_client_own_area(
        _req(db), phone, api.OwnArea(area_id=room), dry_run=False
    )
    await _override(db, phone, "camera_live", False, note="Ferien")
    eff = await api.get_client_effective(_req(db), phone)
    assert "summary" not in eff
    audit = await _audit(db, client_id=phone)
    pushes = [m for m in events["broadcast"] if m["type"] == "profiles_changed"]
    assert pushes and all(m["data"]["client_ids"] == [phone] for m in pushes)
    assert not [
        m
        for m in events["broadcast"]
        if m["type"] == "client_config_updated" and m.get("data")
    ]
    for name, doc in (
        ("rest/client-effective.response.json", eff),
        ("rest/profile-audit.response.json", audit),
        *(("ws/broadcast/profiles_changed.json", m) for m in pushes),
    ):
        errors = sorted(checks[name].iter_errors(doc), key=str)
        assert errors == [], (name, [e.message for e in errors[:3]])
    assert {e["action"] for e in audit["entries"]} >= {"own_area_set", "override_set"}
    own = next(e for e in audit["entries"] if e["action"] == "own_area_set")
    assert own["after"] == {"own_area_id": room}
    guest_profile = await store.profile_row(
        db, await store.default_profile_for(db, None, True)
    )
    grant = await guest_invites.grant(
        db, guest_invites.Choice(guest_profile, None, None, None)
    )
    assert "template_name" not in grant and grant["profile"]["name"] == "Gast"
    defs = json.loads(
        (
            Path(checks["common/defs.json"].schema["$id"].replace("file://", ""))
        ).read_text()
    )
    grant_schema = {**defs["$defs"]["guest_invite_grant"], "$defs": defs["$defs"]}
    import jsonschema

    assert list(jsonschema.Draft202012Validator(grant_schema).iter_errors(grant)) == []


# ── profile edits, adjustments and expiry ────────────────────────────────────────────


async def test_guest_default_never_gets_parental_controls(db, events):
    """The profile edit refuses parental controls on the guest default
    like the settings do; guest pairing keeps working."""
    guest = await starter_profile(db, "guest")
    settings = await api.get_profile_settings(_req(db))
    assert settings["default_profiles"]["guest"] == guest
    with pytest.raises(HTTPException) as e:
        await api.update_profile(
            _req(db), guest, api.ProfileWrite(child_protection=True), dry_run=False
        )
    assert e.value.status_code == 422
    assert e.value.detail["code"] == "profile.guest_child_protection"
    assert (await store.profile_row(db, guest))["child_protection"] == 0
    assert await add_client(db, guest=True)


async def test_profile_from_device_keeps_temporary_adjustments_temporary(db, events):
    """Only permanent adjustments go into the new profile; temporary
    ones stay temporary on the device, ended ones are not taken."""
    phone = await add_client(db, profile="guest")
    await _override(db, phone, "card_style", "color")
    end = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    await _override(db, phone, "remote_access", True, expires_at=end.isoformat())
    async with db.transaction():
        await db.execute(
            "INSERT INTO device_overrides (client_id, rule_key, value_json, "
            "user_choice, expires_at, note, created_by, created_at) VALUES "
            "(?, 'camera_live', 'true', 0, '2020-01-01T00:00:00Z', NULL, 't', "
            "'2020-01-01T00:00:00Z')",
            (phone,),
        )
    p = await api.create_profile_from_device(
        _req(db), api.FromDevice(client_id=phone, name="Neu")
    )
    assert p["rules"]["card_style"] == "color"
    assert p["rules"]["remote_access"] is False
    assert p["rules"]["camera_live"] is False
    eff = await _rules(db, phone)
    assert eff["remote_access"]["value"] is True
    assert eff["remote_access"]["expires_at"] is not None
    assert eff["card_style"]["value"] == "color"
    assert {o["rule_key"] for o in await store.overrides_of(db, phone)} >= {
        "remote_access"
    }
    assert "card_style" not in {
        o["rule_key"] for o in await store.overrides_of(db, phone)
    }


def test_loosens_sees_unordered_critical_rules():
    """remote_control, kids_blocked_types and access_hours."""
    strict = {"default": "hidden", "domains": {}, "entities": {}}
    loose = {"default": "control", "domains": {}, "entities": {}}
    assert cat.loosens("remote_control", strict, loose) is True
    assert cat.loosens("remote_control", loose, strict) is False
    locked = {"default": "control", "domains": {"lock": "view"}, "entities": {}}
    entity_open = {**locked, "entities": {"lock.front": "control"}}
    assert cat.loosens("remote_control", locked, entity_open) is True
    assert cat.loosens("remote_control", entity_open, locked) is False
    assert cat.loosens("kids_blocked_types", ["lock", "camera"], ["lock"]) is True
    assert cat.loosens("kids_blocked_types", ["lock"], ["lock", "camera"]) is False
    closed = {"enabled": True, "windows": []}
    wider = {
        "enabled": True,
        "windows": [{"days": ["mon"], "start": "08:00", "end": "20:00"}],
    }
    assert cat.loosens("access_hours", closed, {"enabled": False, "windows": []})
    assert cat.loosens("access_hours", closed, wider) is True
    assert cat.loosens("access_hours", wider, closed) is False


def test_loosens_sees_the_kids_rules():
    """Bedtime, both rate limits and the exceptions."""
    school = [{"days": ["mon", "tue"], "start": "20:00", "end": "06:30"}]
    later = [{"days": ["mon", "tue"], "start": "21:00", "end": "06:30"}]
    earlier = [{"days": ["mon", "tue"], "start": "19:30", "end": "07:00"}]
    split = [
        {"days": ["mon"], "start": "20:00", "end": "06:30"},
        {"days": ["tue"], "start": "19:00", "end": "07:00"},
    ]
    q = "kids_quiet_hours"
    assert cat.loosens(q, school, []) is True
    assert cat.loosens(q, school, later) is True
    assert cat.loosens(q, school, earlier) is False
    assert cat.loosens(q, school, split) is False
    assert cat.loosens(q, [], school) is False
    whole = [{"days": ["sat"], "start": "00:00", "end": "00:00"}]
    assert cat.loosens(q, whole, [{"days": ["sat"], "start": "08:00", "end": "07:00"}])

    rate = {"max_actions": 6, "window_seconds": 60, "cooldown_seconds": 300}
    assert cat.loosens("kids_rate_limit", rate, None) is True
    assert cat.loosens("kids_rate_limit", None, rate) is False
    assert cat.loosens("kids_rate_limit", rate, {**rate, "max_actions": 10}) is True
    assert cat.loosens("kids_rate_limit", rate, {**rate, "window_seconds": 30}) is True
    assert cat.loosens("kids_rate_limit", rate, {**rate, "cooldown_seconds": 0}) is True
    assert cat.loosens("kids_rate_limit", rate, {**rate, "max_actions": 3}) is False

    every = {
        "enabled": True,
        "max_actions": 12,
        "max_entities": 4,
        "window_seconds": 60,
        "cooldown_seconds": 300,
    }
    a = "kids_rate_limit_all"
    assert cat.loosens(a, every, {**every, "enabled": False}) is True
    assert cat.loosens(a, {**every, "enabled": False}, every) is False
    assert cat.loosens(a, every, {**every, "max_entities": 8}) is True
    assert cat.loosens(a, every, {**every, "cooldown_seconds": 600}) is False

    e = "kids_exceptions"
    room = {"area_id": "8c7d0f3e-1111-4c1e-9a55-000000000001"}
    lamp = {"entity_id": "light.night"}
    blocked = [{"target": room, "rules": {"blocked": True}}]
    allowed = [{"target": room, "rules": {"blocked": False}}]
    assert cat.loosens(e, [], blocked) is False
    assert cat.loosens(e, [], allowed) is True
    assert cat.loosens(e, blocked, allowed) is True
    assert cat.loosens(e, blocked, []) is True
    assert cat.loosens(e, allowed, []) is False
    night = [{"target": lamp, "rules": {"quiet_hours": []}}]
    assert cat.loosens(e, [], night) is True
    capped = [{"target": lamp, "rules": {"volume_max": 30}}]
    assert cat.loosens(e, capped, [{"target": lamp, "rules": {"volume_max": 50}}])
    assert not cat.loosens(e, capped, [{"target": lamp, "rules": {"volume_max": 20}}])
    assert cat.loosens(e, capped, [{"target": lamp, "rules": {"volume_max": None}}])
    types_ = [{"target": room, "rules": {"blocked_types": ["lock", "camera"]}}]
    fewer = [{"target": room, "rules": {"blocked_types": ["lock"]}}]
    assert cat.loosens(e, types_, fewer) is True
    assert cat.loosens(e, fewer, types_) is False
    assert cat.loosens(e, blocked, blocked) is False


LOOSENS_CASES = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "e2e"
    / "fixtures"
    / "loosens-cases.json"
)


def test_loosens_agrees_with_the_shared_table():
    """The admin UI checks the same table (frontend/e2e/loosens.spec.js), so
    the warning before saving and the hub's preview never disagree."""
    table = json.loads(LOOSENS_CASES.read_text())["cases"]
    wrong = [
        c["name"]
        for c in table
        if cat.loosens(c["rule"], c["before"], c["after"]) is not c["loosens"]
    ]
    assert wrong == []
    assert all(
        cat.rule(c["rule"])["security"] in ("critical", "sensitive") for c in table
    )


async def test_garbage_expires_at_is_a_422(db, events):
    """An unparseable expires_at is a validation error, not a 500."""
    phone = await add_client(db, profile="adult")
    for bad in ("tomorrow", "2026-13-45T00:00:00Z"):
        with pytest.raises(HTTPException) as e:
            await _override(db, phone, "camera_live", True, expires_at=bad)
        assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await api.bulk_update_clients(
            _req(db),
            api.Bulk(
                client_ids=[phone],
                action="set_override",
                rule="camera_live",
                value=True,
                expires_at="2026-13-45T00:00:00Z",
            ),
        )
    assert e.value.status_code == 422


def test_rule_values_strict_numbers_and_patterns():
    """40.0, NaN and a trailing newline do not pass."""
    from services.profiles import schema as pschema

    assert pschema.rule_value_errors("kids_volume_max", 40) == []
    assert pschema.rule_value_errors("kids_volume_max", 40.0)
    assert pschema.rule_value_errors("columns", 2.0)
    assert pschema.rule_value_errors("panel_dim", float("nan"))
    night = {"mode": "off", "start": "22:00\n", "end": "06:00", "brightness": 20}
    assert pschema.rule_value_errors("panel_night", night)
    assert pschema.rule_value_errors(
        "kids_quiet_hours", [{"days": ["mon"], "start": "20:00\n", "end": "07:00"}]
    )
    ok = [{"days": ["mon"], "start": "20:00", "end": "07:00"}]
    assert pschema.rule_value_errors("kids_quiet_hours", ok) == []
