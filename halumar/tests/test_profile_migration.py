"""Schema v9: carrying the old model over to profiles with a self check.

A v8 database (fixtures/schema_v8.sql, the frozen v8 schema) with
templates, kids profiles and their area/entity rules, guest defaults, the
hub options and devices of every kind is migrated by ``Database.initialize``.
The self check compares the effective values before (old model) and after
(resolver); a difference stops the migration with nothing changed.
"""

import json
import logging
import re
import sqlite3
from pathlib import Path

import pytest
from models.database import SCHEMA_VERSION, Database, DatabaseUnavailableError
from services import db_migrations
from services.profiles import migration_v9
from services.profiles import store

V8 = (Path(__file__).parent / "fixtures" / "schema_v8.sql").read_text()
NOW = "2026-11-19T10:00:00+00:00"
FUTURE = "2099-01-01T00:00:00+00:00"
KIDS_RULES = {
    "blocked_types": ["lock", "camera"],
    "quiet_hours": [{"days": ["mon"], "start": "20:00", "end": "06:30"}],
    "rate_limit": {"max_actions": 6, "window_seconds": 60, "cooldown_seconds": 300},
    "color_lock": True,
    "volume_max": 30,
}


def _v8(path: str, rows) -> None:
    conn = sqlite3.connect(path)
    for stmt in db_migrations.split_sql(V8):
        conn.execute(stmt)
    for sql, params in rows:
        conn.execute(sql, params)
    conn.execute("PRAGMA user_version = 8")
    conn.commit()
    conn.close()


def _client(cid, **cols):
    base = {
        "id": cid,
        "name": cid,
        "secret_hash": "h" * 64,
        "created_at": NOW,
        "tags": "[]",
        "platform": "ios",
    }
    base.update(cols)
    return (
        f"INSERT INTO paired_clients ({', '.join(base)}) "
        f"VALUES ({', '.join('?' for _ in base)})",
        tuple(base.values()),
    )


def _house():
    template = {
        "show_search_bar": False,
        "kiosk_mode": True,
        "panel_screensaver_after": 10,
        "card_style": "color",
        "settings_hidden": True,
    }
    return [
        (
            "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("a1", "Kinderzimmer", NOW, NOW),
        ),
        (
            "INSERT INTO areas (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("a2", "Küche", NOW, NOW),
        ),
        ("INSERT INTO area_tags (area_id, tag) VALUES (?, ?)", ("a2", "gast")),
        (
            "INSERT INTO config_templates (id, name, settings_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("t1", "Gast", json.dumps(template), NOW),
        ),
        (
            "INSERT INTO kids_profiles (id, name, rules, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("k1", "Kids", json.dumps(KIDS_RULES), NOW, NOW),
        ),
        (
            "INSERT INTO kids_profile_rules (profile_id, scope, target, rules, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            ("k1", "area", "a1", json.dumps({"volume_max": 20}), NOW),
        ),
        (
            "INSERT INTO kids_profile_rules (profile_id, scope, target, rules, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            ("k1", "entity", "light.night", json.dumps({"blocked": False}), NOW),
        ),
        (
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            (
                "guest_invite_defaults",
                json.dumps(
                    {"all_areas": False, "area_ids": ["a2"], "expires_hours": 48}
                ),
            ),
        ),
        (
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("notification_stacking", "true"),
        ),
        (
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            ("members_can_add_devices", "false"),
        ),
        _client(
            "phone",
            remote_allowed=1,
            can_invite_guests=1,
            app_config_overrides=json.dumps(
                {
                    "show_filter_button": False,
                    "camera_live_preview": False,
                    "location_tracking_enabled": True,
                    "area_card_tint_enabled": False,
                }
            ),
        ),
        _client(
            "local",
            remote_allowed=0,
            notification_stacked_style="carousel",
            app_config_overrides=json.dumps({"card_style": "calm"}),
        ),
        _client("tv", platform="tvos", config_template_id="t1"),
        _client(
            "panel",
            platform="ipados",
            config_template_id="t1",
            tags=json.dumps(["area:a1"]),
            settings_pin_hash="a" * 64,
            settings_pin_salt="b" * 32,
        ),
        _client("kid", profile_id="k1", tags=json.dumps(["area:a1"])),
        _client("kid_tpl", profile_id="k1", config_template_id="t1"),
        _client("guest", is_guest=1, expires_at=FUTURE, tags=json.dumps(["gast"])),
        _client("guest_none", is_guest=1, expires_at=FUTURE),
        _client("gone", is_active=0, pending_deletion_at=NOW),
    ]


async def _migrate(path):
    db = Database(path)
    await db.initialize()
    return db


async def test_v8_house_is_carried_over_with_a_clean_self_check(tmp_path, caplog):
    path = str(tmp_path / "hub.db")
    _v8(path, _house())
    with caplog.at_level(logging.WARNING):
        db = await _migrate(path)
    # The fixed log line of the self check (contracts docs/profiles.md).
    assert re.search(
        r"Migration v9 self-check passed: \d+ devices, 0 deviations, \d+ profiles, "
        r"\d+ adjustments, \d+ boundary changes",
        caplog.text,
    ), caplog.text
    try:
        assert (await db.fetchone("PRAGMA user_version"))[
            "user_version"
        ] == SCHEMA_VERSION
        assert await db_migrations.missing_schema_parts(db.connection) == []
        names = {r["name"] for r in await db.fetchall("SELECT name FROM profiles")}
        # Starters, the template (name taken -> "(2)"), the kids profile and
        # the used combination kids profile + template.
        assert names == {
            "Erwachsene Person",
            "Kind",
            "Gast",
            "Wandpanel",
            "Fernseher",
            "Gast (2)",
            "Kids",
            "Kids + Gast",
        }
        for table in ("config_templates", "kids_profiles", "kids_profile_rules"):
            assert not await db.fetchone(
                "SELECT name FROM sqlite_master WHERE name = ?", (table,)
            )
        cols = await db_migrations.table_columns(db.connection, "paired_clients")
        for gone in ("is_guest", "tags", "remote_allowed", "app_config_overrides"):
            assert gone not in cols
        for key in (
            "guest_invite_defaults",
            "members_can_add_devices",
            "notification_stacking",
        ):
            assert await db.get_setting(key) is None
        assert await db.get_setting("guest_default_hours") == "48"

        rules = {}
        for cid in (
            "phone",
            "local",
            "tv",
            "panel",
            "kid",
            "kid_tpl",
            "guest",
            "guest_none",
        ):
            rules[cid] = (await store.resolve_client(db, cid))[2]
        assert rules["phone"]["remote_access"]["value"] is True
        assert rules["phone"]["setup_devices"]["value"] == {
            "enabled": True,
            "kinds": ["guest", "house_sitter"],
        }
        assert rules["phone"]["search_filter"]["value"] is False
        assert rules["phone"]["search_filter"]["locked"] is True
        assert rules["phone"]["camera_live"]["value"] is False
        assert rules["phone"]["location"]["value"] == "background"
        assert rules["local"]["remote_access"]["value"] is False
        assert rules["local"]["notification_stack"]["value"] == "carousel"
        assert rules["local"]["card_style"]["value"] == "calm"
        assert (
            rules["local"]["card_style"]["user_choice"] is True
        )  # preset without lock
        assert rules["tv"]["kiosk"]["value"] is True
        assert rules["tv"]["app_settings"]["value"] == "hidden"
        assert rules["tv"]["panel_screensaver"]["value"] == 10
        assert rules["panel"]["areas"]["value"]["area_ids"] == ["a1"]
        assert rules["kid"]["kids_volume_max"]["value"] == 30
        assert rules["kid"]["kids_exceptions"]["value"] == [
            {"target": {"area_id": "a1"}, "rules": {"volume_max": 20}},
            {"target": {"entity_id": "light.night"}, "rules": {"blocked": False}},
        ]
        # Remote access is a normal rule with parental controls too;
        # the migrated kid stays at home.
        assert rules["kid"]["remote_access"]["value"] is False
        assert rules["kid_tpl"]["kids_color_lock"]["value"] is True
        assert rules["guest"]["areas"]["value"]["tags"] == ["gast"]
        # The guest defaults went into the profile "Gast"; a guest that saw
        # nothing before keeps seeing nothing (adjustment).
        gast = await db.fetchone("SELECT body_json FROM profiles WHERE name = 'Gast'")
        assert json.loads(gast["body_json"])["rules"]["areas"]["area_ids"] == ["a2"]
        assert rules["guest_none"]["areas"]["value"]["area_ids"] == []
        assert rules["guest_none"]["areas"]["source"] == "device"

        rows = {
            r["id"]: r
            for r in await db.fetchall(
                "SELECT c.id, c.access, c.expires_at, c.own_area_id, p.name AS profile, "
                "p.child_protection FROM paired_clients c JOIN profiles p "
                "ON p.id = c.profile_id"
            )
        }
        assert rows["phone"]["profile"] == "Erwachsene Person"
        assert rows["tv"]["profile"] == "Gast (2)"
        assert rows["kid"]["profile"] == "Kids" and rows["kid"]["child_protection"] == 1
        assert rows["kid_tpl"]["profile"] == "Kids + Gast"
        assert rows["guest"]["access"] == "guest" and rows["guest"]["profile"] == "Gast"
        assert rows["guest"]["expires_at"] == FUTURE
        assert (
            rows["phone"]["expires_at"] is None and rows["phone"]["own_area_id"] is None
        )
        assert rows["gone"]["profile"] == "Erwachsene Person"
        notes = {
            r["note"] for r in await db.fetchall("SELECT note FROM device_overrides")
        }
        assert notes == {"Migration v9"}
        audit = await db.fetchone(
            "SELECT * FROM profile_audit WHERE action = 'migration_v9'"
        )
        assert json.loads(audit["after_json"])["devices"] == 9
    finally:
        await db.close()


async def test_a_difference_stops_the_migration_and_changes_nothing(tmp_path):
    """A kids profile that blocks everything has no rule in the new model."""
    path = str(tmp_path / "hub.db")
    rows = _house() + [
        (
            "INSERT INTO kids_profiles (id, name, rules, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("k2", "Alles zu", json.dumps({"blocked": True}), NOW, NOW),
        ),
        _client("blocked", profile_id="k2"),
    ]
    _v8(path, rows)
    before = Path(path).read_bytes()
    with pytest.raises(DatabaseUnavailableError) as e:
        await _migrate(path)
    assert "blocked" in e.value.reason and "Nothing was changed" in e.value.reason
    assert re.search(
        r"Migration v9 self-check failed: [1-9]\d* deviations", e.value.reason
    )
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert conn.execute("SELECT COUNT(*) FROM kids_profiles").fetchone()[0] == 2
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'profiles'"
        ).fetchone()
    finally:
        conn.close()
    assert len(Path(path).read_bytes()) == len(before)


async def test_reset_option_falls_back_to_a_fresh_setup(tmp_path):
    path = str(tmp_path / "hub.db")
    _v8(
        path,
        _house()
        + [
            (
                "INSERT INTO kids_profiles (id, name, rules, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("k2", "Alles zu", json.dumps({"blocked": True}), NOW, NOW),
            ),
            _client("blocked", profile_id="k2"),
        ],
    )
    db = Database(path, reset_on_corruption=True)
    await db.initialize()
    try:
        assert db.recovery_notice and "EMPTY" in db.recovery_notice
        assert (await db.fetchone("PRAGMA user_version"))[
            "user_version"
        ] == SCHEMA_VERSION
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM profiles"))["n"] == 5
        assert list(tmp_path.glob("hub.db.migration-*"))
    finally:
        await db.close()


def test_before_snapshot_maps_the_old_keys():
    snap = migration_v9.before_snapshot(
        {
            "platform": "ios",
            "app_config_overrides": json.dumps(
                {
                    "location_tracking_enabled": True,
                    "location_background_enabled": False,
                    "power_button_domains": ["light", "lock", "fan"],
                    "quick_access_max": "status",
                    "panel_night_mode": "sun",
                }
            ),
            "tags": "[]",
        },
        None,
        None,
        {"notification_stacking": "false"},
    )
    assert snap["location"] == ("foreground", True)
    assert snap["power_button"] == (
        {"enabled": False, "domains": ["light", "fan"]},
        True,
    )
    assert snap["quick_access"] == ("status", True)
    assert snap["panel_night"][0]["mode"] == "sun"
    assert snap["notification_stack"] == ("off", True)
    assert snap["areas"][0]["mode"] == "all"
    assert snap["header"] is migration_v9.DEVICE
