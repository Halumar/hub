"""Tests for _slugify, _area_slug and client_notification_sensor_id — edge cases, unicode, length caps.

ALL entity slugs are capped at 48 chars (SLUG_MAX_LEN): the worst-case
Halumar entity id stays under 100 chars.
"""

import re
import uuid

from namespace import CLIENT_SENSOR_PREFIX
from services.ha_sensors import (
    SLUG_MAX_LEN,
    _area_slug,
    _slugify,
    client_notification_sensor_id,
    client_quick_access_sensor_id,
)


class TestSlugify:
    """_slugify edge cases."""

    def test_basic_lowercase(self):
        assert _slugify("HelloWorld") == "helloworld"

    def test_spaces_to_underscores(self):
        assert _slugify("Living Room") == "living_room"

    def test_german_umlauts(self):
        """Umlauts should be normalized (ü→u, ö→o, ä→a)."""
        assert _slugify("Büro") == "buro"
        assert _slugify("Küche") == "kuche"
        assert _slugify("Gästezimmer") == "gastezimmer"

    def test_special_characters_removed(self):
        assert _slugify("Room #1 (2nd Floor)") == "room_1_2nd_floor"

    def test_consecutive_specials_collapsed(self):
        assert _slugify("a---b___c   d") == "a_b_c_d"

    def test_leading_trailing_stripped(self):
        assert _slugify("  Room  ") == "room"
        assert _slugify("---Room---") == "room"

    def test_empty_string(self):
        assert _slugify("") == "unknown"

    def test_only_special_chars(self):
        assert _slugify("!@#$%") == "unknown"

    def test_numbers_preserved(self):
        assert _slugify("Room 42") == "room_42"

    def test_already_valid_slug(self):
        assert _slugify("living_room") == "living_room"

    def test_mixed_unicode(self):
        """Various unicode chars should be transliterated or removed."""
        assert _slugify("Café") == "cafe"
        assert _slugify("naïve") == "naive"

    def test_long_input(self):
        """Slugify should handle very long strings."""
        long_name = "a" * 500
        result = _slugify(long_name)
        assert result == "a" * 500  # _slugify itself doesn't cap


class TestAreaSlug:
    """_area_slug collision-free, rename-stable slugs."""

    def test_basic_slug_with_id(self):
        area_id = "abc12345-6789-0def-ghij-klmnopqrstuv"
        slug = _area_slug("Wohnzimmer", area_id)
        assert slug == "wohnzimmer_abc12345"

    def test_uuid_prefix_no_dashes(self):
        """First 8 chars of UUID with dashes removed."""
        area_id = "a1b2c3d4-e5f6-7890-abcd-ef0123456789"
        slug = _area_slug("Test", area_id)
        assert slug.endswith("_a1b2c3d4")

    def test_collision_prevention(self):
        """'Büro' and 'Buro' with different IDs should produce different slugs."""
        id1 = str(uuid.uuid4())
        id2 = str(uuid.uuid4())
        slug1 = _area_slug("Büro", id1)
        slug2 = _area_slug("Buro", id2)
        assert slug1 != slug2

    def test_rename_stability(self):
        """Slug should include the area ID, so renaming doesn't change it much."""
        area_id = "abc12345-6789-0def-ghij-klmnopqrstuv"
        slug1 = _area_slug("Living Room", area_id)
        slug2 = _area_slug("Lounge", area_id)
        # Both end with the same ID suffix
        assert slug1.endswith("_abc12345")
        assert slug2.endswith("_abc12345")

    def test_length_cap_48(self):
        """Name portion is capped at SLUG_MAX_LEN (48) chars."""
        long_name = "a" * 500
        area_id = "abc12345-xxxx"
        slug = _area_slug(long_name, area_id)
        # Slug = name_slug[:48] + "_" + short_id[:8]
        name_part = slug.rsplit("_", 1)[0]
        assert len(name_part) <= 48

    def test_total_slug_length(self):
        """Total slug is at most 48 + 1 + 8 = 57 chars."""
        long_name = "a" * 500
        area_id = "abcdefgh-1234-5678"
        slug = _area_slug(long_name, area_id)
        assert len(slug) <= 57

    def test_worst_case_area_entity_ids_under_100(self):
        """Worst-case area entity ids stay under 100 chars (both prefixes)."""
        slug = _area_slug("a" * 500, str(uuid.uuid4()))
        assert len(f"sensor.halumar_area_{slug}") <= 100
        assert len(f"input_boolean.halumar_area_{slug}") <= 100

    def test_empty_name(self):
        """Empty name should use 'unknown' as fallback."""
        area_id = "abc12345-xxxx"
        slug = _area_slug("", area_id)
        assert slug.startswith("unknown_")

    def test_unicode_name(self):
        area_id = "abc12345-xxxx"
        slug = _area_slug("Gästezimmer", area_id)
        assert slug.startswith("gastezimmer_")


class TestClientNotificationSensorId:
    """client_notification_sensor_id — the single source of truth for the
    per-client notification sensor entity id (slug + uid8)."""

    def test_formula(self):
        cid = "abc12345-6789-0def-1111-222233334444"
        assert (
            client_notification_sensor_id("Kitchen Panel", cid)
            == "sensor.halumar_client_kitchen_panel_abc12345_notifications"
        )

    def test_uid8_is_lowercased(self):
        """uid8 = first 8 chars AFTER lowercasing and removing dashes."""
        cid = "ABC12345-6789-0DEF-1111-222233334444"
        sensor = client_notification_sensor_id("Panel", cid)
        assert sensor.endswith("_abc12345_notifications")

    def test_uid8_strips_all_dashes_before_slicing(self):
        # Dashes removed FIRST, then the first 8 chars are taken:
        # "ab-c1-23-45-..." -> "abc12345..."[:8] == "abc12345"
        cid = "ab-c1-23-45-6789"
        sensor = client_notification_sensor_id("Panel", cid)
        assert sensor == "sensor.halumar_client_panel_abc12345_notifications"

    def test_same_name_different_id_no_collision(self):
        id1, id2 = str(uuid.uuid4()), str(uuid.uuid4())
        assert client_notification_sensor_id(
            "Panel", id1
        ) != client_notification_sensor_id("Panel", id2)

    def test_name_slug_capped_at_48(self):
        cid = str(uuid.uuid4())
        uid8 = cid.lower().replace("-", "")[:8]
        sensor = client_notification_sensor_id("x" * 500, cid)
        # "sensor.halumar_client_" + slug(<=48) + "_" + uid8 + "_notifications"
        assert sensor == f"sensor.halumar_client_{'x' * 48}_{uid8}_notifications"

    def test_worst_case_entity_id_under_100(self):
        """The longest possible notification-sensor entity id is <= 100 chars
        (prefix + 48 slug + 1 + 8 uid8 + 14 suffix; 93 with the current namespace)."""
        cid = str(uuid.uuid4())
        sensor = client_notification_sensor_id("x" * 500, cid)
        expected = (
            len(CLIENT_SENSOR_PREFIX) + SLUG_MAX_LEN + 1 + 8 + len("_notifications")
        )
        assert len(sensor) == expected
        assert len(sensor) <= 100

    def test_empty_name_falls_back_to_client(self):
        cid = "abc12345-6789"
        assert (
            client_notification_sensor_id("", cid)
            == "sensor.halumar_client_client_abc12345_notifications"
        )

    def test_unicode_name(self):
        cid = "abc12345-6789"
        assert client_notification_sensor_id("Büro iPad", cid).startswith(
            "sensor.halumar_client_buro_ipad_"
        )


class TestAreaSlugSync:
    """api/integration.py must reuse ha_sensors' slug helpers (ONE source).

    The integration module imports _slugify/_area_slug from
    services.ha_sensors, so the two can never drift.
    """

    def test_integration_reuses_ha_sensors_helpers(self):
        from services.ha_sensors import _area_slug as slug_sensors
        from services.ha_sensors import _slugify as slugify_sensors
        from api.integration import _area_slug as slug_integration
        from api.integration import _slugify as slugify_integration

        assert slug_integration is slug_sensors
        assert slugify_integration is slugify_sensors


# HA's core.valid_entity_id: no "__", no "_" at either end of domain or object id.
_HA_ENTITY_ID = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")


class TestHaValidEntityIds:
    """A cap right after a separator left a trailing "_" in the
    slug, the entity id got "__" and HA refused the sensor with 400."""

    # Slug "a"*47 + "_b": the cap at 48 lands right after the separator.
    CUT_AT_SEPARATOR = "a" * 47 + " b"

    def test_cap_drops_trailing_separator(self):
        assert _slugify(self.CUT_AT_SEPARATOR, max_len=SLUG_MAX_LEN) == "a" * 47
        assert _slugify("Wohnzimmer Licht", max_len=11) == "wohnzimmer"

    def test_cap_never_leaves_separator_at_either_end(self):
        for n in range(1, 60):
            for name in ("ab " * 30, " - x y" * 20, "Büro / Küche " * 9):
                slug = _slugify(name, max_len=n)
                assert slug and not slug.startswith("_") and not slug.endswith("_")
                assert "__" not in slug and len(slug) <= n

    def test_uncapped_unchanged(self):
        assert _slugify("a" * 500) == "a" * 500

    def test_empty_after_cap_falls_back(self):
        assert _slugify("!!!", fallback="client", max_len=SLUG_MAX_LEN) == "client"

    def test_like_ha_slugify(self):
        """Letters NFKD keeps as they are are spelled like HA's slugify."""
        assert _slugify("Straße") == "strasse"
        assert _slugify("Ærø Œuvre") == "aero_oeuvre"
        assert _slugify("Łódź Þór Đ") == "lodz_thor_d"
        assert _slugify("Parent's iPhone – Teen") == "parent_s_iphone_teen"
        assert _slugify("Länge 1,5 m") == "lange_15_m"

    def test_all_sensor_ids_valid_for_ha(self):
        cid = str(uuid.uuid4())
        names = [
            self.CUT_AT_SEPARATOR,
            "TEST-SUITE remote adjustment remote adjustment remote 1",
            "",
            "___",
            "ß",
            "Ärger",
            "-x-",
            "a" * 300,
        ]
        for name in names:
            for entity_id in (
                client_notification_sensor_id(name, cid),
                client_quick_access_sensor_id(name, cid),
                f"sensor.halumar_area_{_area_slug(name, cid)}",
                f"input_boolean.halumar_area_{_area_slug(name, cid)}",
            ):
                assert _HA_ENTITY_ID.match(entity_id), entity_id

    def test_uid8_only_alphanumerics(self):
        assert _area_slug("x", "ab_cd-ef_gh12") == "x_abcdefgh"
        assert _area_slug("x", "---") == "x_0"
