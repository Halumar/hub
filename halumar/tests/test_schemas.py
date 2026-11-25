"""Tests for Pydantic schemas — validation, constraints, edge cases."""

import pytest
from pydantic import ValidationError

from models.schemas import (
    AreaCreate,
    AreaUpdate,
    AreaEntityCreate,
    AreaWidgetCreate,
    AreaWidgetUpdate,
    ImportRequest,
    SectionCreate,
    SectionOrderUpdate,
    NavigateToAreaRequest,
    ShowEntityRequest,
    PairingGenerateRequest,
    SendNotificationRequest,
    SectionLayoutItem,
    SectionLayoutUpdate,
)


class TestAreaCreate:
    """AreaCreate schema validation."""

    def test_valid_area(self):
        area = AreaCreate(name="Living Room")
        assert area.name == "Living Room"
        assert area.tags == []
        assert area.sort_order == 0
        assert area.is_active is True

    def test_name_required(self):
        with pytest.raises(ValidationError):
            AreaCreate()

    def test_name_min_length(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="")

    def test_name_max_length(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="x" * 101)

    def test_name_exactly_100_chars(self):
        area = AreaCreate(name="x" * 100)
        assert len(area.name) == 100

    def test_description_max_length(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", description="x" * 2001)

    def test_description_at_limit(self):
        area = AreaCreate(name="Test", description="x" * 2000)
        assert len(area.description) == 2000

    def test_color_valid_hex(self):
        area = AreaCreate(name="Test", color="#FF5500")
        assert area.color == "#FF5500"

    def test_color_invalid_format(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", color="red")

    def test_color_invalid_short_hex(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", color="#FFF")

    def test_grid_cols_range(self):
        area = AreaCreate(name="Test", grid_cols=2)
        assert area.grid_cols == 2

        area = AreaCreate(name="Test", grid_cols=4)
        assert area.grid_cols == 4

    def test_grid_cols_out_of_range(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", grid_cols=1)
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", grid_cols=5)

    def test_tags_max_50(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", tags=["tag"] * 51)

    def test_tag_max_length_100(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", tags=["x" * 101])

    def test_tag_at_limit(self):
        area = AreaCreate(name="Test", tags=["x" * 100])
        assert len(area.tags[0]) == 100

    def test_control_mode_valid(self):
        for mode in ("basic", "advanced", "full"):
            area = AreaCreate(name="Test", control_mode=mode)
            assert area.control_mode == mode

    def test_control_mode_invalid(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", control_mode="invalid")

    def test_control_mode_none_allowed(self):
        area = AreaCreate(name="Test", control_mode=None)
        assert area.control_mode is None

    def test_image_url_max_length(self):
        with pytest.raises(ValidationError):
            AreaCreate(name="Test", card_image_url="x" * 2001)


class TestAreaUpdate:
    """AreaUpdate schema validation."""

    def test_all_fields_optional(self):
        update = AreaUpdate()
        assert update.name is None
        assert update.icon is None

    def test_partial_update(self):
        update = AreaUpdate(name="New Name", icon="mdi:star")
        assert update.name == "New Name"
        assert update.icon == "mdi:star"

    def test_color_valid_hex(self):
        assert AreaUpdate(color="#FF5500").color == "#FF5500"

    def test_color_invalid_format_rejected(self):
        # Same hex pattern as AreaCreate.color.
        with pytest.raises(ValidationError):
            AreaUpdate(color="red")

    def test_color_short_hex_rejected(self):
        with pytest.raises(ValidationError):
            AreaUpdate(color="#FFF")

    def test_color_none_allowed(self):
        assert AreaUpdate(color=None).color is None


class TestAreaEntityCreate:
    """AreaEntityCreate schema validation."""

    def test_valid_entity(self):
        entity = AreaEntityCreate(entity_id="light.living_room", section_id="abc")
        assert entity.entity_id == "light.living_room"
        assert entity.columns == 1
        assert entity.rows == 1

    def test_entity_id_required(self):
        with pytest.raises(ValidationError):
            AreaEntityCreate(section_id="abc")

    def test_entity_id_max_length(self):
        with pytest.raises(ValidationError):
            AreaEntityCreate(entity_id="x" * 256, section_id="abc")

    def test_entity_id_at_limit(self):
        entity_id = "light." + "x" * 249
        entity = AreaEntityCreate(entity_id=entity_id, section_id="abc")
        assert len(entity.entity_id) == 255

    def test_display_name_max_length(self):
        with pytest.raises(ValidationError):
            AreaEntityCreate(
                entity_id="light.test",
                section_id="abc",
                display_name="x" * 201,
            )

    def test_columns_range(self):
        entity = AreaEntityCreate(entity_id="light.test", section_id="abc", columns=2)
        assert entity.columns == 2

        with pytest.raises(ValidationError):
            AreaEntityCreate(entity_id="light.test", section_id="abc", columns=3)

    def test_control_mode_validation(self):
        with pytest.raises(ValidationError):
            AreaEntityCreate(
                entity_id="light.test",
                section_id="abc",
                control_mode="invalid",
            )


class TestAreaWidgetCreate:
    """AreaWidgetCreate schema validation."""

    def test_valid_widget(self):
        widget = AreaWidgetCreate(section_id="abc", widget_type="clock")
        assert widget.widget_type == "clock"
        assert widget.config == {}

    def test_widget_type_max_length(self):
        with pytest.raises(ValidationError):
            AreaWidgetCreate(section_id="abc", widget_type="x" * 51)

    def test_widget_type_min_length(self):
        with pytest.raises(ValidationError):
            AreaWidgetCreate(section_id="abc", widget_type="")


class TestSectionCreate:
    """SectionCreate schema validation."""

    def test_defaults(self):
        section = SectionCreate()
        assert section.title == ""
        assert section.column_span == 1
        assert section.columns == 2
        assert section.sort_order == 0

    def test_title_max_length(self):
        with pytest.raises(ValidationError):
            SectionCreate(title="x" * 201)

    def test_column_span_range(self):
        SectionCreate(column_span=1)
        SectionCreate(column_span=12)
        with pytest.raises(ValidationError):
            SectionCreate(column_span=0)
        with pytest.raises(ValidationError):
            SectionCreate(column_span=13)

    def test_columns_range(self):
        SectionCreate(columns=1)
        SectionCreate(columns=2)
        with pytest.raises(ValidationError):
            SectionCreate(columns=0)
        with pytest.raises(ValidationError):
            SectionCreate(columns=3)


class TestIntegrationSchemas:
    """Integration command schemas."""

    def test_navigate_to_area_valid(self):
        req = NavigateToAreaRequest(area_id="abc123")
        assert req.area_id == "abc123"

    def test_navigate_to_area_empty(self):
        with pytest.raises(ValidationError):
            NavigateToAreaRequest(area_id="")

    def test_navigate_to_area_max_length(self):
        with pytest.raises(ValidationError):
            NavigateToAreaRequest(area_id="x" * 101)

    def test_show_entity_valid(self):
        req = ShowEntityRequest(entity_id="light.living_room")
        assert req.entity_id == "light.living_room"

    def test_show_entity_max_length(self):
        with pytest.raises(ValidationError):
            ShowEntityRequest(entity_id="x" * 256)

    def test_show_entity_control_mode(self):
        req = ShowEntityRequest(entity_id="light.test", control_mode="basic")
        assert req.control_mode == "basic"

        with pytest.raises(ValidationError):
            ShowEntityRequest(entity_id="light.test", control_mode="invalid")

    def test_pairing_generate_defaults(self):
        req = PairingGenerateRequest()
        assert req.is_guest is False
        # None = the guest default duration of the house settings.
        assert req.guest_expires_hours is None
        assert req.profile is None and req.guest_tags is None

    def test_pairing_generate_hours_range(self):
        PairingGenerateRequest(guest_expires_hours=1)
        PairingGenerateRequest(guest_expires_hours=2160)
        with pytest.raises(ValidationError):
            PairingGenerateRequest(guest_expires_hours=0)
        with pytest.raises(ValidationError):
            PairingGenerateRequest(guest_expires_hours=2161)


class TestSendNotificationRequest:
    """REST API notification schema."""

    def test_valid_notification(self):
        req = SendNotificationRequest(
            client_id="abc123",
            title="Alert",
            message="Something happened",
        )
        assert req.client_id == "abc123"
        assert req.notification_type == "default"

    def test_client_id_required(self):
        with pytest.raises(ValidationError):
            SendNotificationRequest(title="Alert")

    def test_client_id_max_length(self):
        with pytest.raises(ValidationError):
            SendNotificationRequest(client_id="x" * 101, title="Alert")

    def test_notification_type_validation(self):
        with pytest.raises(ValidationError):
            SendNotificationRequest(
                client_id="abc",
                notification_type="invalid",
            )

    def test_icon_max_length(self):
        with pytest.raises(ValidationError):
            SendNotificationRequest(
                client_id="abc",
                icon="x" * 201,
            )

    def test_title_and_message_max_lengths(self):
        with pytest.raises(ValidationError):
            SendNotificationRequest(client_id="abc", title="x" * 501)
        with pytest.raises(ValidationError):
            SendNotificationRequest(client_id="abc", message="x" * 5001)

    def test_valid_notification_types(self):
        for t in ("normal", "sticky", "permanent"):
            req = SendNotificationRequest(client_id="abc", notification_type=t)
            assert req.notification_type == t


class TestListAndConfigCaps:
    """List and dict fields carry max_length caps."""

    def test_section_order_within_cap(self):
        assert len(SectionOrderUpdate(section_ids=["s"] * 500).section_ids) == 500

    def test_section_order_over_cap_rejected(self):
        with pytest.raises(ValidationError):
            SectionOrderUpdate(section_ids=["s"] * 501)

    def test_layout_items_within_cap(self):
        items = [SectionLayoutItem(section_id="s", sort_order=0) for _ in range(10)]
        assert len(SectionLayoutUpdate(items=items).items) == 10

    def test_layout_items_over_cap_rejected(self):
        items = [SectionLayoutItem(section_id="s", sort_order=0) for _ in range(2001)]
        with pytest.raises(ValidationError):
            SectionLayoutUpdate(items=items)

    def test_import_areas_within_cap(self):
        assert len(ImportRequest(areas=[{"name": "a"}] * 500).areas) == 500

    def test_import_areas_over_cap_rejected(self):
        with pytest.raises(ValidationError):
            ImportRequest(areas=[{"name": "a"}] * 501)

    def test_widget_create_config_within_cap(self):
        w = AreaWidgetCreate(section_id="s", widget_type="clock", config={"a": 1})
        assert w.config == {"a": 1}

    def test_widget_create_config_over_cap_rejected(self):
        big = {str(i): i for i in range(101)}
        with pytest.raises(ValidationError):
            AreaWidgetCreate(section_id="s", widget_type="clock", config=big)

    def test_widget_update_config_over_cap_rejected(self):
        big = {str(i): i for i in range(101)}
        with pytest.raises(ValidationError):
            AreaWidgetUpdate(config=big)
