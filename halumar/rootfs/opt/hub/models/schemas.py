"""Pydantic schemas for Halumar API.

Schema v3: Sections-based layout model.
"""

import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from services.expiry import MAX_GUEST_HOURS, normalize_expiry_input
from services.ha_client import is_valid_entity_id
from version import VERSION

# Owner label of a paired device: optional, trimmed, max 50 chars;
# an empty value clears it.
OWNER_LABEL_MAX = 50


def normalize_owner_label(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if len(v) > OWNER_LABEL_MAX:
        raise ValueError(f"owner_label must be at most {OWNER_LABEL_MAX} characters")
    return v or None


# Notification types accepted on input: default and sticky; the input also
# accepts normal and permanent and stores their canonical value.
_ACCEPTED_NOTIFICATION_TYPES = ("default", "sticky", "normal", "permanent")


def _validate_notification_type(v: str) -> str:
    if v not in _ACCEPTED_NOTIFICATION_TYPES:
        raise ValueError(
            'notification_type must be one of "default", "sticky" '
            "(also accepts normal and permanent and stores their canonical value)"
        )
    return v


# Notification presentation extension (shared addon<->app contract).
_NOTIFICATION_COLORS = ("none", "red", "blue", "green", "yellow", "orange")
_NOTIFICATION_ACTION_TYPES = ("none", "area", "entity", "url")
# Popup display modes: none=inline banner only, popup=centered modal only,
# both=inline banner + centered modal (extra attention), direct=immediately
# open the linked area/entity/url (one-time, no card/popup; requires an action).
_NOTIFICATION_POPUP_MODES = ("none", "popup", "both", "direct")


def _validate_popup_mode(v: str | None) -> str | None:
    # None means "not specified" -> create_notification derives the mode from
    # the popup bool (true -> "popup", else "none").
    if v is None:
        return None
    if v not in _NOTIFICATION_POPUP_MODES:
        raise ValueError(
            "popup_mode must be one of "
            + ", ".join(f'"{m}"' for m in _NOTIFICATION_POPUP_MODES)
        )
    return v


def _validate_color(v: str) -> str:
    if v not in _NOTIFICATION_COLORS:
        raise ValueError(
            "color must be one of " + ", ".join(f'"{c}"' for c in _NOTIFICATION_COLORS)
        )
    return v


def _validate_action_type(v: str) -> str:
    if v not in _NOTIFICATION_ACTION_TYPES:
        raise ValueError(
            "action_type must be one of "
            + ", ".join(f'"{a}"' for a in _NOTIFICATION_ACTION_TYPES)
        )
    return v


def _validate_scheduled_at(v: str | None) -> str | None:
    """Validate an optional ISO-8601 timestamp string (UTC)."""
    if v is None or v == "":
        return None
    try:
        # Accept trailing 'Z' as UTC.
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_at must be an ISO-8601 datetime string") from exc
    return v


def _reject_null(v, info):
    """Explicit ``null`` for a column that cannot be NULL: 422, not SQL NULL.

    The update models keep ``None`` as the "not sent" default; only a field
    that was actually sent as ``null`` reaches this validator.
    """
    if v is None:
        raise PydanticCustomError(
            "null_not_allowed", "{field} must not be null", {"field": info.field_name}
        )
    return v


# ── Sections ─────────────────────────────────────────────────────────────────


class SectionCreate(BaseModel):
    """Schema for creating a section within an area."""

    title: str = Field("", max_length=200)
    icon: str | None = Field(None, max_length=100)
    sort_order: int = 0
    column_span: int = Field(1, ge=1, le=12)
    columns: int = Field(2, ge=1, le=2)


class SectionUpdate(BaseModel):
    """Schema for updating a section."""

    title: str | None = Field(None, max_length=200)
    icon: str | None = Field(None, max_length=100)
    sort_order: int | None = None
    column_span: int | None = Field(None, ge=1, le=12)
    columns: int | None = Field(None, ge=1, le=2)
    is_collapsed: bool | None = None

    check_not_null = field_validator(
        "title", "sort_order", "column_span", "columns", "is_collapsed"
    )(_reject_null)


class SectionResponse(BaseModel):
    """Schema for section response (with nested entities and widgets)."""

    id: str
    area_id: str
    title: str = ""
    icon: str | None = None
    sort_order: int = 0
    column_span: int = 1
    columns: int = 2
    is_collapsed: int = 0
    created_at: str | None = None
    entities: list["AreaEntityResponse"] = []
    widgets: list["AreaWidgetResponse"] = []


class AreaOrderUpdate(BaseModel):
    """Schema for reordering all areas at once (batch reorder)."""

    model_config = {"extra": "forbid"}

    area_ids: list[str] = Field(..., min_length=1, max_length=500)


class AreaOrderResponse(BaseModel):
    """Result of a batch reorder: number of areas whose position changed."""

    status: Literal["ok"] = "ok"
    moved: int


class SectionOrderUpdate(BaseModel):
    """Schema for reordering sections."""

    # Bounded so an untrusted body can't force an unbounded reorder loop
    # (an area realistically holds <= 20 sections; 500 is a generous backstop).
    section_ids: list[str] = Field(..., max_length=500)


# ── Areas ────────────────────────────────────────────────────────────────────


def _dedupe_tags(tags: list[str]) -> list[str]:
    """Length check and duplicates removed (first occurrence wins).

    ``area_tags`` has PRIMARY KEY (area_id, tag), so a duplicate would raise
    an IntegrityError halfway through the update.
    """
    for tag in tags:
        if len(tag) > 100:
            raise ValueError("Each tag must be 100 characters or less")
        if tag == "*":
            raise ValueError('"*" is reserved (client marker for all areas)')
        if tag.startswith("area:"):
            raise ValueError('"area:" is reserved (client area pins)')
    return list(dict.fromkeys(tags))


# Bounds of an area's name and of every icon field (also for names and icons
# taken from Home Assistant by the sync).
AREA_NAME_MAX = 100
ICON_MAX = 100


class AreaCreate(BaseModel):
    """Schema for creating an area."""

    name: str = Field(..., min_length=1, max_length=AREA_NAME_MAX)
    icon: str | None = Field(None, max_length=ICON_MAX)
    description: str | None = Field(None, max_length=2000)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    card_image_url: str | None = Field(None, max_length=2000)
    bg_light_url: str | None = Field(None, max_length=2000)
    bg_dark_url: str | None = Field(None, max_length=2000)
    grid_cols: int = Field(2, ge=2, le=4)
    dense_section_placement: bool = True
    sort_order: int = 0
    is_active: bool = True
    control_mode: str | None = None  # "basic", "advanced", "full", or None (disabled)
    tags: list[str] = Field(default=[], max_length=50)

    @field_validator("control_mode")
    @classmethod
    def validate_control_mode(cls, v):
        if v is not None and v not in ("basic", "advanced", "full"):
            raise ValueError('control_mode must be "basic", "advanced", or "full"')
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v):
        return _dedupe_tags(v)


class AreaUpdate(BaseModel):
    """Schema for updating an area."""

    name: str | None = Field(None, min_length=1, max_length=AREA_NAME_MAX)
    icon: str | None = Field(None, max_length=ICON_MAX)
    description: str | None = Field(None, max_length=2000)
    # Match AreaCreate.color: only a 6-digit hex color (or null to skip).
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$", max_length=7)
    card_image_url: str | None = Field(None, max_length=2000)
    bg_light_url: str | None = Field(None, max_length=2000)
    bg_dark_url: str | None = Field(None, max_length=2000)
    grid_cols: int | None = Field(None, ge=2, le=4)
    dense_section_placement: bool | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    control_mode: str | None = None  # "basic", "advanced", "full", or None to skip
    tags: list[str] | None = Field(None, max_length=50)

    check_not_null = field_validator(
        "name", "grid_cols", "dense_section_placement", "sort_order", "is_active"
    )(_reject_null)

    @field_validator("control_mode")
    @classmethod
    def validate_control_mode(cls, v):
        if v is not None and v not in ("basic", "advanced", "full"):
            raise ValueError('control_mode must be "basic", "advanced", or "full"')
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v):
        return None if v is None else _dedupe_tags(v)


class AreaResponse(BaseModel):
    """Schema for area response."""

    id: str
    name: str
    icon: str | None = None
    description: str | None = None
    color: str | None = None
    card_image_path: str | None = None
    card_image_url: str | None = None
    card_image_src: str | None = None  # Computed: servable URL for uploaded card image
    bg_light_path: str | None = None
    bg_light_url: str | None = None
    bg_light_src: str | None = None  # Computed: servable URL for uploaded light bg
    bg_dark_path: str | None = None
    bg_dark_url: str | None = None
    bg_dark_src: str | None = None  # Computed: servable URL for uploaded dark bg
    grid_cols: int = 4
    dense_section_placement: int = 1
    sort_order: int = 0
    is_active: bool = True
    control_mode: str | None = None  # null = disabled/inherit from default
    ha_area_id: str | None = None
    tags: list[str] = []
    entity_count: int = 0
    section_count: int = 0
    created_at: str
    updated_at: str


class AreaDetailResponse(AreaResponse):
    """Schema for detailed area response with sections."""

    sections: list[SectionResponse] = []


# ── Tile settings (contracts common/tile-settings.json) ─────────────────

# Display name of a placement (contracts $defs/area_display_name): trimmed,
# 1-100 characters, no control characters; empty = null (HA name).
AREA_DISPLAY_NAME_MAX = 100
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def normalize_area_display_name(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if _CONTROL_CHARS.search(v):
        raise ValueError("display_name must not contain control characters")
    if len(v) > AREA_DISPLAY_NAME_MAX:
        raise ValueError(
            f"display_name must be at most {AREA_DISPLAY_NAME_MAX} characters"
        )
    return v or None


def clip_area_display_name(v: str | None) -> str | None:
    """A name from Home Assistant made to fit ``area_display_name``: control
    characters dropped, trimmed, cut to the maximum; empty = None."""
    if not v:
        return None
    return _CONTROL_CHARS.sub("", v).strip()[:AREA_DISPLAY_NAME_MAX].strip() or None


TileVariant = Literal["compact", "large", "slider", "action"]
TileAccent = Literal[
    "accent", "light", "heat", "safe", "sea", "cool", "media", "neutral"
]
TileActionKind = Literal["default", "toggle", "more_info", "run", "none"]
TileHoldActionKind = Literal["default", "more_info", "none"]
TileFormFactor = Literal["phone", "tablet", "tv"]
TileSecondarySource = Literal["state", "last_changed", "attribute", "entity", "none"]

TILE_ICON_PATTERN = r"^(mdi:[a-z0-9]+(-[a-z0-9]+)*|sf:[a-z0-9]+(\.[a-z0-9]+)*)$"
TILE_RUN_TARGET = re.compile(r"^(scene|script|button|input_button)\.[a-z0-9_]+$")
TILE_SECONDARY_ENTITY = re.compile(r"^(sensor|binary_sensor)\.[a-z0-9_]+$")


class _TileModel(BaseModel):
    # Unknown keys are a 422, never silently dropped.
    model_config = ConfigDict(extra="forbid")


class TileSecondary(_TileModel):
    source: TileSecondarySource
    attribute: str | None = Field(None, pattern=r"^[a-z0-9_]{1,64}$")
    entity_id: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _source_fields(self):
        if self.source == "attribute" and not self.attribute:
            raise PydanticCustomError(
                "tile.attribute_required", "source=attribute needs attribute"
            )
        if self.source != "attribute" and self.attribute is not None:
            raise PydanticCustomError(
                "tile.field_not_allowed", "attribute only with source=attribute"
            )
        if self.source == "entity":
            if not self.entity_id:
                raise PydanticCustomError(
                    "tile.entity_required", "source=entity needs entity_id"
                )
            if not TILE_SECONDARY_ENTITY.match(self.entity_id):
                raise PydanticCustomError(
                    "tile.entity_domain",
                    "entity_id must be a sensor or binary_sensor",
                )
        elif self.entity_id is not None:
            raise PydanticCustomError(
                "tile.field_not_allowed", "entity_id only with source=entity"
            )
        return self


class TileTapAction(_TileModel):
    action: TileActionKind
    target_entity_id: str | None = Field(None, max_length=255)

    @model_validator(mode="after")
    def _target(self):
        if self.action == "run":
            if not self.target_entity_id:
                raise PydanticCustomError(
                    "tile.target_required", "run needs target_entity_id"
                )
            if not TILE_RUN_TARGET.match(self.target_entity_id):
                raise PydanticCustomError(
                    "tile.target_domain",
                    "target_entity_id must be a scene, script, button or input_button",
                )
        elif self.target_entity_id is not None:
            raise PydanticCustomError(
                "tile.field_not_allowed", "target_entity_id only with action=run"
            )
        return self


class TileHoldAction(_TileModel):
    action: TileHoldActionKind


class TileSettings(_TileModel):
    """Writable tile settings ($defs/tile_settings_base). None = default."""

    icon: str | None = Field(None, max_length=100, pattern=TILE_ICON_PATTERN)
    variant: TileVariant | None = None
    secondary: TileSecondary | None = None
    tap_action: TileTapAction | None = None
    hold_action: TileHoldAction | None = None
    confirm: bool | None = None
    accent: TileAccent | None = None
    hidden_on: list[TileFormFactor] | None = None

    @field_validator("hidden_on")
    @classmethod
    def _hidden_on(cls, v):
        if v is None:
            return v
        if len(set(v)) != len(v):
            raise PydanticCustomError(
                "tile.hidden_duplicate", "hidden_on must be unique"
            )
        if len(v) >= 3:
            raise PydanticCustomError(
                "tile.hidden_everywhere",
                "to hide the tile everywhere use is_visible = false",
            )
        if not v:
            raise PydanticCustomError(
                "tile.hidden_empty", "hidden_on needs 1-2 entries or null"
            )
        return v


# ── Entities ─────────────────────────────────────────────────────────────────


class AreaEntityCreate(BaseModel):
    """Schema for adding an entity to an area."""

    entity_id: str = Field(..., min_length=1, max_length=255)
    section_id: str
    display_name: str | None = Field(None, max_length=200)
    columns: int = Field(1, ge=1, le=2)
    rows: int = Field(1, ge=1, le=2)
    sort_order: int = 0
    is_visible: bool = True
    control_mode: str | None = None  # null=inherit, "basic"/"advanced"/"full"=override
    tile: TileSettings | None = None

    @field_validator("entity_id")
    @classmethod
    def validate_entity_id(cls, v):
        # Existence in Home Assistant is checked by the endpoint (needs HA).
        if not is_valid_entity_id(v):
            raise PydanticCustomError(
                "entity_id.invalid_format", "entity_id must be domain.object_id"
            )
        return v

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v):
        return normalize_area_display_name(v)

    @field_validator("control_mode")
    @classmethod
    def validate_control_mode(cls, v):
        if v is not None and v not in ("basic", "advanced", "full"):
            raise ValueError('control_mode must be "basic", "advanced", or "full"')
        return v


class AreaEntityUpdate(BaseModel):
    """Schema for updating an area entity (contracts rest/area-entity-update.request).

    Partial update; ``tile`` replaces the whole object, ``tile: null`` resets it.
    Unknown keys are rejected (422).
    """

    model_config = ConfigDict(extra="forbid")

    section_id: str | None = None
    display_name: str | None = Field(None, max_length=AREA_DISPLAY_NAME_MAX + 50)
    columns: int | None = Field(None, ge=1, le=2)
    rows: int | None = Field(None, ge=1, le=2)
    sort_order: int | None = None
    is_visible: bool | None = None
    control_mode: str | None = None  # null=inherit, "basic"/"advanced"/"full"=override
    tile: TileSettings | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v):
        return normalize_area_display_name(v)

    check_not_null = field_validator(
        "section_id", "columns", "rows", "sort_order", "is_visible"
    )(_reject_null)

    @field_validator("control_mode")
    @classmethod
    def validate_control_mode(cls, v):
        if v is not None and v not in ("basic", "advanced", "full"):
            raise ValueError('control_mode must be "basic", "advanced", or "full"')
        return v


class AreaEntityResponse(BaseModel):
    """Schema for area entity response."""

    id: str
    area_id: str
    entity_id: str
    section_id: str
    display_name: str | None = None
    columns: int = 1
    rows: int = 1
    sort_order: int = 0
    is_visible: int = 1
    control_mode: str | None = None  # null=inherit, "basic"/"advanced"/"full"=override
    tile: dict = Field(
        default_factory=dict,
        description="Tile settings (contracts common/tile-settings.json); {} = default.",
    )
    state: dict | None = None  # Current HA state (populated on request)
    created_at: str


# ── Widgets ──────────────────────────────────────────────────────────────────

# Serialized size of a widget config (bytes of its JSON). One bound for every
# writer (create, update, import) so that an export always imports again.
WIDGET_CONFIG_MAX_BYTES = 10_000


def _check_widget_config(v: dict | None) -> dict | None:
    if v is not None and len(json.dumps(v)) > WIDGET_CONFIG_MAX_BYTES:
        raise PydanticCustomError(
            "widget.config_too_large",
            "config must be at most {max} bytes as JSON",
            {"max": WIDGET_CONFIG_MAX_BYTES},
        )
    return v


class AreaWidgetCreate(BaseModel):
    """Schema for creating a widget."""

    section_id: str
    widget_type: str = Field(..., min_length=1, max_length=50)
    # Cap the number of keys so an untrusted body can't carry an enormous dict
    # (widget configs are tiny: clock/text/spacer). 100 is a generous backstop.
    config: dict = Field(default={}, max_length=100)
    columns: int = Field(1, ge=1, le=2)
    rows: int = Field(1, ge=1, le=2)
    sort_order: int = 0

    check_config = field_validator("config")(_check_widget_config)


class AreaWidgetUpdate(BaseModel):
    """Schema for updating a widget."""

    section_id: str | None = None
    widget_type: str | None = None
    config: dict | None = Field(None, max_length=100)
    columns: int | None = Field(None, ge=1, le=2)
    rows: int | None = Field(None, ge=1, le=2)
    sort_order: int | None = None

    check_not_null = field_validator(
        "section_id", "widget_type", "config", "columns", "rows", "sort_order"
    )(_reject_null)
    check_config = field_validator("config")(_check_widget_config)


class AreaWidgetResponse(BaseModel):
    """Schema for widget response."""

    id: str
    area_id: str
    section_id: str
    widget_type: str
    config: dict = {}
    columns: int = 1
    rows: int = 1
    sort_order: int = 0
    created_at: str


# ── Batch Layout ─────────────────────────────────────────────────────────────


class SectionLayoutItem(BaseModel):
    """Single item in a batch layout update."""

    entity_id: str | None = None
    widget_id: str | None = None
    section_id: str
    sort_order: int
    # Same bounds as the entity and widget models (contract common/area-*.json).
    columns: int | None = Field(None, ge=1, le=2)
    rows: int | None = Field(None, ge=1, le=2)


class SectionLayoutUpdate(BaseModel):
    """Schema for batch-updating layout positions (entities + widgets)."""

    # Bounded backstop against an oversized body. A per-area layout tops out
    # around 50 items x 20 sections = 1000; 2000 leaves comfortable headroom.
    items: list[SectionLayoutItem] = Field(..., max_length=2000)


# ── HA Integration ───────────────────────────────────────────────────────────


class HAEntityResponse(BaseModel):
    """Schema for HA entity."""

    entity_id: str
    state: str
    attributes: dict = {}
    last_changed: str | None = None
    last_updated: str | None = None


class HAAreaResponse(BaseModel):
    """Schema for HA area."""

    area_id: str
    name: str
    aliases: list[str] = []
    icon: str | None = None
    picture: str | None = None


class SyncFromHARequest(BaseModel):
    """Schema for selective HA area import."""

    area_ids: list[str] | None = None  # If None, import all areas
    include_entities: bool = (
        False  # If True, also import entities assigned to each area
    )
    domains: list[str] | None = None  # If None, import all domains


class SyncAreaEntitiesRequest(BaseModel):
    """Schema for syncing entities of a single area."""

    domains: list[str] | None = None  # If None, import all domains


class SyncResult(BaseModel):
    """Schema for sync result."""

    synced: int = 0
    created: int = 0
    updated: int = 0
    entities_added: int = 0
    errors: list[str] = []


class ExportData(BaseModel):
    """Schema for export data."""

    format_version: int = 2
    version: str = VERSION
    exported_at: str
    areas: list[dict]


class ImportRequest(BaseModel):
    """Schema for import request."""

    # Schema-level backstop; the endpoint further enforces MAX_IMPORT_AREAS.
    areas: list[dict] = Field(..., max_length=500)
    overwrite: bool = False


# ── Integration command schemas ──────────────────────────────────────────


class NavigateToAreaRequest(BaseModel):
    """Schema for navigate_to_area integration command."""

    area_id: str = Field(..., min_length=1, max_length=100)


class ShowEntityRequest(BaseModel):
    """Schema for show_entity integration command."""

    entity_id: str = Field(..., min_length=1, max_length=255)
    control_mode: str | None = None

    @field_validator("control_mode")
    @classmethod
    def validate_control_mode(cls, v):
        if v is not None and v not in ("basic", "advanced", "full"):
            raise ValueError('control_mode must be "basic", "advanced", or "full"')
        return v


class ClientTokenDelivery(BaseModel):
    """POST /api/integration/client-token.

    The integration delivers the token of the device's own HA user. The
    bearer is stored encrypted and never logged; only the ids are.
    """

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(
        ...,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    )
    ha_user_id: str = Field(..., min_length=1, max_length=128)
    refresh_token_id: str = Field(..., min_length=1, max_length=128)
    token: str = Field(..., min_length=1, max_length=4096)
    request_id: str | None = Field(None, max_length=64)


class HAUrlReport(BaseModel):
    """POST /api/integration/ha-url.

    The external URL Home Assistant itself reports (``get_url`` without
    internal URLs: its ``external_url``, else the Home Assistant Cloud
    address); null when it has none.
    """

    model_config = ConfigDict(extra="forbid")

    external_url: str | None = Field(..., max_length=2048)
    ha_instance_id: str | None = Field(
        None,
        pattern=r"^[0-9a-f]{32}$",
        description=(
            "Home Assistant's instance ID (also the uuid of its Bonjour "
            "TXT record); handed to the devices as client_config.ha_instance_id."
        ),
    )


class HAUrlReportResponse(BaseModel):
    """Answer of POST /api/integration/ha-url."""

    success: bool
    changed: bool = Field(
        ..., description="The URL devices with remote access get changed."
    )


class ClientTagsRequest(BaseModel):
    """Schema for replacing a client's tag list via integration.

    The integration's manage_client_tags service computes add/remove against
    its coordinator data and always POSTs the resulting FULL list.
    """

    tags: list[str] = Field(
        default=[],
        max_length=50,
        description="Full new tag list. Empty list = all areas for members, none for "
        'guests (fail closed); ["*"] = all areas (also for guests).',
    )


class ExtendGuestRequest(BaseModel):
    """Schema for extending a guest client's session.

    Shared by the admin PATCH /clients/{id}/extend and the integration
    POST /integration/clients/{id}/extend. Provide exactly one of the fields.
    """

    hours: float | None = Field(
        None, gt=0, le=MAX_GUEST_HOURS, description="Add N hours from now"
    )
    expires_at: str | None = Field(
        None,
        max_length=100,
        description="Set explicit new expiry (ISO 8601 with offset, e.g. "
        "2026-11-18T18:00:00+01:00); stored as UTC",
    )
    dry_run: bool = Field(
        False,
        description="Write nothing; answer the new end and whether the guest is "
        "(hub_remote_now) or later becomes (hub_remote_at) Hub only with remote "
        "access after the extension",
    )

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, v):
        return normalize_expiry_input(v) if v else v


class AckNotificationsRequest(BaseModel):
    """Schema for the integration's remote notification ack (fallback path).

    POST /api/integration/notifications/ack — when a client app reaches only
    Home Assistant (not the hub WebSocket), it calls the
    ``halumar.ack_notifications`` HA service after displaying notifications
    from its HA notification sensor; the integration forwards the batch here.
    1-50 ids per call; each id max 100 chars.
    """

    client_id: str = Field(..., min_length=1, max_length=100)
    notification_ids: list[str] = Field(..., min_length=1, max_length=50)

    @field_validator("notification_ids")
    @classmethod
    def validate_notification_ids(cls, v):
        for nid in v:
            if not nid or len(nid) > 100:
                raise ValueError("each notification id must be 1-100 characters long")
        return v


class PairingGenerateRequest(BaseModel):
    """Body of POST /api/integration/start-pairing."""

    is_guest: bool = False
    guest_expires_hours: int | None = Field(None, ge=1, le=MAX_GUEST_HOURS)
    guest_expires_at: str | None = Field(None, max_length=100)
    # Guests: area scope as a device adjustment of the rule areas; None = the
    # profile decides.
    guest_tags: list[str] | None = Field(None, max_length=50)

    @field_validator("guest_expires_at")
    @classmethod
    def validate_guest_expires_at(cls, v):
        return normalize_expiry_input(v) if v else v

    # Profile NAME (case-insensitive); None = default profile of the device type.
    profile: str | None = Field(None, max_length=40)

    # Owner label of the paired device.
    owner_label: str | None = Field(None, max_length=200)

    @field_validator("owner_label")
    @classmethod
    def validate_owner_label(cls, v):
        return normalize_owner_label(v)


class SendNotificationRequest(BaseModel):
    """Schema for REST API notification sending."""

    client_id: str = Field(..., min_length=1, max_length=100)
    title: str = Field("", max_length=500)
    # message is optional: only title is required. None/"" both mean "no body".
    message: str | None = Field(None, max_length=5000)
    notification_type: str = "default"
    icon: str | None = Field(None, max_length=200)
    icon_svg: str | None = Field(None, max_length=10000)
    # popup (derived from popup_mode): true -> popup_mode "popup". popup_mode is
    # the canonical field; popup is accepted and emitted alongside.
    # popup_mode None => not specified (derived from popup).
    popup: bool = False
    popup_mode: str | None = None
    scheduled_at: str | None = Field(None, max_length=100)
    priority: int = Field(50, ge=0, le=100)
    color: str = "none"
    action_type: str = "none"
    action_target: str | None = Field(None, max_length=2000)
    # Per-notification "show send time" flag (default on).
    show_time: bool = True

    @field_validator("notification_type")
    @classmethod
    def validate_notification_type(cls, v):
        return _validate_notification_type(v)

    @field_validator("scheduled_at")
    @classmethod
    def validate_scheduled_at(cls, v):
        return _validate_scheduled_at(v)

    @field_validator("color")
    @classmethod
    def validate_color(cls, v):
        return _validate_color(v)

    @field_validator("action_type")
    @classmethod
    def validate_action_type(cls, v):
        return _validate_action_type(v)

    @field_validator("popup_mode")
    @classmethod
    def validate_popup_mode(cls, v):
        return _validate_popup_mode(v)


_NOTIFICATION_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Queued notifications older than this are not taken over (same bound as the
# integration's queue and the hub's clean-up of dismissed notifications).
NOTIFICATION_HANDOVER_MAX_AGE_HOURS = 72


class IntegrationNotifyRequest(SendNotificationRequest):
    """POST /api/integration/notify: the notification model plus a stable id.

    ``notification_id`` (lowercase UUID, chosen by the integration) makes the
    call idempotent: a retry after a timeout returns the existing notification
    instead of creating a second one. With client_id "all" it is a broadcast
    id: each device's notification gets the id UUIDv5(broadcast id, client
    id), and devices that already have it are skipped.
    """

    notification_id: str | None = Field(None, pattern=_NOTIFICATION_ID_PATTERN)


class HandoverNotification(SendNotificationRequest):
    """A notification the integration queued while the hub was unreachable."""

    notification_id: str = Field(..., pattern=_NOTIFICATION_ID_PATTERN)
    created_at: str = Field(..., max_length=100)
    # "delivered": the app already confirmed it through Home Assistant
    # (ack_notifications) during the outage.
    status: Literal["pending", "delivered"] = "pending"

    @field_validator("client_id")
    @classmethod
    def validate_single_client(cls, v):
        if v == "all":
            raise ValueError('client_id "all" is not allowed in a handover')
        return v

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, v):
        try:
            parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("created_at must be an ISO-8601 datetime string") from exc
        if parsed.tzinfo is None:
            raise ValueError("created_at needs a time zone")
        return parsed.astimezone(timezone.utc).isoformat()


class NotificationHandoverRequest(BaseModel):
    """POST /api/integration/notifications/handover: 1-50 queued notifications,
    oldest first."""

    notifications: list[HandoverNotification] = Field(..., min_length=1, max_length=50)


# ── enforcement in Home Assistant ───────────────────────────────

_UUID_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ACTION_PATTERN = r"^[a-z_]{1,64}$"
_ENTITY_PATTERN = r"^[a-z0-9_]+\.[a-z0-9_]+$"


def _contract_enum(name: str) -> frozenset[str]:
    import json
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "schemas",
        "defs.json",
    )
    with open(path, encoding="utf-8") as f:
        return frozenset(json.load(f)["$defs"][name]["enum"])


EVENT_TYPES = _contract_enum("enforcement_event_type")
DENIAL_REASONS = _contract_enum("control_denial_reason")


class KidsCheckRequest(BaseModel):
    """POST /api/integration/kids-check: kids counter and pause."""

    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(..., pattern=_UUID_PATTERN)
    entity_id: str = Field(..., max_length=255, pattern=_ENTITY_PATTERN)
    action: str = Field(..., pattern=_ACTION_PATTERN)
    grant: str | None = Field(None, min_length=16, max_length=128)


class KidsCheckResponse(BaseModel):
    """Answer of POST /api/integration/kids-check."""

    allowed: bool
    reason: (
        Literal["rate_limited", "cooldown", "blocked", "quiet_hours", "paused_all"]
        | None
    )
    until: str | None


class IntegrationEvent(BaseModel):
    """One integration event for the hub log (contract enforcement-event.json)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., pattern=_UUID_PATTERN)
    type: str = Field(..., max_length=64)
    at: str = Field(..., max_length=64)
    client_id: str | None = Field(None, pattern=_UUID_PATTERN)
    ha_user_id: str | None = Field(None, pattern=r"^[0-9a-f]{32}$")
    entity_id: str | None = Field(None, max_length=255, pattern=_ENTITY_PATTERN)
    action: str | None = Field(None, pattern=_ACTION_PATTERN)
    route: Literal["local", "external", "unknown"] | None = None
    origin: Literal["app", "quick_access"] | None = None
    reason: str | None = Field(None, max_length=64)
    count: int | None = Field(None, ge=1, le=100000)
    since: str | None = Field(None, max_length=64)
    ha_group: Literal["read_only", "users"] | None = None
    rules_hash: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    reliability: Literal["exact", "best_effort"] | None = None
    version: str | None = Field(None, max_length=32)

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v not in EVENT_TYPES:
            raise ValueError("unknown event type")
        return v

    @field_validator("at", "since")
    @classmethod
    def _timestamp(cls, v: str | None) -> str | None:
        # The log sorts and filters by these strings: a date and a time.
        if v is not None:
            try:
                datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("not an ISO-8601 timestamp") from exc
            if "T" not in v and " " not in v:
                raise ValueError("not an ISO-8601 timestamp")
        return v

    @field_validator("reason")
    @classmethod
    def _known_reason(cls, v: str | None) -> str | None:
        if v is not None and v not in DENIAL_REASONS:
            raise ValueError("unknown reason")
        return v

    @model_validator(mode="after")
    def _required_by_type(self):
        if self.type in ("control", "denied") and not (
            self.client_id and self.entity_id and self.action and self.route
        ):
            raise ValueError("control/denied need client_id, entity_id, action, route")
        if self.type == "denied" and not self.reason:
            raise ValueError("denied needs a reason")
        if (
            self.type
            in ("access_group_restored", "direct_access_attempt", "rules_applied")
            and not self.client_id
        ):
            raise ValueError(f"{self.type} needs client_id")
        if self.type == "integration_outage" and not self.since:
            raise ValueError("integration_outage needs since")
        return self


class IntegrationEventsRequest(BaseModel):
    """POST /api/integration/events: batch of integration events."""

    model_config = ConfigDict(extra="forbid")

    events: list[IntegrationEvent] = Field(..., min_length=1, max_length=100)
    dropped: int = Field(..., ge=0)


class IntegrationEventsResponse(BaseModel):
    """Answer of POST /api/integration/events."""

    accepted: int = Field(..., ge=0)
    duplicates: int = Field(..., ge=0)


class PanelActor(BaseModel):
    """Who triggered a panel command, for the log."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ha_admin", "automation", "admin_ui"]
    ha_user_id: str | None = Field(None, min_length=1, max_length=128)
    name: str | None = Field(None, max_length=100)


class PanelCommandRequest(BaseModel):
    """POST /api/pairing/clients/{id}/panel (admin UI test buttons).

    action, level and mode are checked by services/panel.py so that every
    rejection answers with a contract code (unsupported, invalid).
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., min_length=1, max_length=64)
    level: float | None = None
    mode: str | None = Field(None, max_length=16)
    # announce and show_camera (actions reserved in the contract).
    text: str | None = Field(None, max_length=300)
    volume: int | None = None
    chime: bool | None = None
    entity_id: str | None = Field(None, max_length=255)

    def args(self) -> dict:
        """The action arguments that were sent (services/panel.py checks them)."""
        return {
            k: v
            for k in ("level", "mode", "text", "volume", "chime", "entity_id")
            if (v := getattr(self, k)) is not None
        }


class IntegrationPanelCommandRequest(PanelCommandRequest):
    """POST /api/integration/clients/{id}/panel (Home Assistant)."""

    actor: PanelActor | None = None


class PanelCommandResponse(BaseModel):
    """Answer of a panel command: the app executed it."""

    success: Literal[True]
    client_id: str
    action: str
    panel: dict | None
