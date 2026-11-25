"""Home screen head per profile (rule ``home_header``).

``{style, status_chips, greeting, weather}`` in the group "Home screen"
together with ``header``: weather mood with everything on and user's choice by
default, the kids suggestions compact without status capsules; the admin locks
it by taking away "User's choice" (``locked`` in ``client_config.rules``);
phones and tablets only, never on a TV.
"""

import types

import api.websocket as ws
import pytest
from api import profiles as api
from fastapi import HTTPException
from services.profiles import catalog as cat

from .profile_helpers import add_client, starter_profile

WEATHER_MOOD = {
    "style": "weather_mood",
    "status_chips": True,
    "greeting": True,
    "weather": True,
}
COMPACT_KIDS = {
    "style": "compact",
    "status_chips": False,
    "greeting": True,
    "weather": True,
}


def _req(db):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


async def _rule(db, client_id):
    return (await ws._get_client_config(db, client_id))["rules"].get("home_header")


def test_catalog_groups_the_head_with_header():
    rule = cat.rule("home_header")
    assert rule["group"] == "home" and cat.rule("header")["group"] == "home"
    assert [g["id"] for g in cat.catalog()["groups"]].index("home") < [
        g["id"] for g in cat.catalog()["groups"]
    ].index("display")
    assert rule["default"] == WEATHER_MOOD
    assert rule["user_choice_allowed"] and rule["default_user_choice"]
    assert "tvos" not in rule["platforms"] and "android_tv" not in rule["platforms"]


async def test_adult_phone_gets_weather_mood_as_users_choice(db):
    phone = await add_client(db, secret_hash="a" * 64, platform="ios", profile="adult")
    rule = await _rule(db, phone)
    assert rule["value"] == WEATHER_MOOD
    assert rule["locked"] is False and rule["user_choice"] is True


async def test_child_phone_starts_compact_without_capsules(db):
    phone = await add_client(db, secret_hash="b" * 64, platform="ios", profile="child")
    rule = await _rule(db, phone)
    assert rule["value"] == COMPACT_KIDS
    assert rule["locked"] is False


async def test_not_sent_to_a_tv(db):
    tv = await add_client(db, secret_hash="c" * 64, platform="tvos", profile="apple_tv")
    assert await _rule(db, tv) is None


async def test_admin_locks_the_head(db):
    adult = await starter_profile(db, "adult")
    phone = await add_client(db, secret_hash="d" * 64, platform="ios", profile="adult")
    before = await api.get_profile(_req(db), adult)
    uc = [k for k in before["user_choice"] if k != "home_header"]
    compact = {
        "style": "compact",
        "status_chips": True,
        "greeting": False,
        "weather": True,
    }
    await api.update_profile(
        _req(db),
        adult,
        api.ProfileWrite(rules={"home_header": compact}, user_choice=uc),
        dry_run=False,
    )
    rule = await _rule(db, phone)
    assert rule["value"] == compact
    assert rule["locked"] is True and rule["reason"] == "profile"


async def test_invalid_value_is_rejected(db):
    adult = await starter_profile(db, "adult")
    for bad in (
        {"style": "weather_mood", "status_chips": True, "greeting": True},
        {**WEATHER_MOOD, "style": "big"},
        {**WEATHER_MOOD, "clock": True},
    ):
        with pytest.raises(HTTPException) as e:
            await api.update_profile(
                _req(db),
                adult,
                api.ProfileWrite(rules={"home_header": bad}),
                dry_run=False,
            )
        assert e.value.status_code in (400, 422), bad


async def _strip_rule(db, profile_id: str, key: str = "home_header") -> None:
    """Stored body as before the rule existed (no value, not the user's choice)."""
    import json

    row = await db.fetchone(
        "SELECT body_json FROM profiles WHERE id = ?", (profile_id,)
    )
    body = json.loads(row["body_json"])
    body["rules"].pop(key, None)
    body["user_choice"] = [k for k in body.get("user_choice", []) if k != key]
    async with db.transaction():
        await db.execute(
            "UPDATE profiles SET body_json = ? WHERE id = ?",
            (json.dumps(body), profile_id),
        )


async def test_stored_profile_without_the_rule_is_not_locked(db):
    adult = await starter_profile(db, "adult")
    await _strip_rule(db, adult)
    phone = await add_client(db, secret_hash="e" * 64, platform="ios", profile="adult")
    rule = await _rule(db, phone)
    assert rule["value"] == WEATHER_MOOD
    assert rule["locked"] is False and rule["user_choice"] is True
    assert rule["source"] == "default" and rule["reason"] == "user_choice"
    view = await api.get_profile(_req(db), adult)
    assert "home_header" in view["catalog_defaults"]
    assert "home_header" in view["user_choice"]


async def test_stored_child_profile_without_the_rule_gets_the_kids_default(db):
    child = await starter_profile(db, "child")
    await _strip_rule(db, child)
    phone = await add_client(db, secret_hash="f" * 64, platform="ios", profile="child")
    rule = await _rule(db, phone)
    assert rule["value"] == COMPACT_KIDS
    assert rule["locked"] is False


async def test_saving_stores_the_filled_rule_and_an_explicit_lock_stays(db):
    adult = await starter_profile(db, "adult")
    await _strip_rule(db, adult)
    phone = await add_client(db, secret_hash="0" * 64, platform="ios", profile="adult")
    saved = await api.update_profile(
        _req(db), adult, api.ProfileWrite(description="Neu"), dry_run=False
    )
    assert (
        saved["catalog_defaults"] == []
        or "home_header" not in saved["catalog_defaults"]
    )
    rule = await _rule(db, phone)
    assert rule["source"] == "profile" and rule["locked"] is False
    uc = [k for k in saved["user_choice"] if k != "home_header"]
    await api.update_profile(
        _req(db), adult, api.ProfileWrite(user_choice=uc), dry_run=False
    )
    assert (await _rule(db, phone))["locked"] is True
