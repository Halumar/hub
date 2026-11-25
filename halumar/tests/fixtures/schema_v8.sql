-- Hub schema v8 (before the profile model): fixture of the v9
-- migration tests. Frozen copy, never edit.
CREATE TABLE IF NOT EXISTS areas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    icon TEXT,
    description TEXT,
    color TEXT,
    card_image_path TEXT,
    card_image_url TEXT,
    bg_light_path TEXT,
    bg_light_url TEXT,
    bg_dark_path TEXT,
    bg_dark_url TEXT,
    grid_cols INTEGER DEFAULT 2,
    dense_section_placement INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT 1,
    control_mode TEXT,
    ha_area_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS area_sections (
    id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    title TEXT DEFAULT '',
    icon TEXT,
    sort_order INTEGER DEFAULT 0,
    column_span INTEGER DEFAULT 1,
    columns INTEGER DEFAULT 2,
    is_collapsed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_area_sections_area_id ON area_sections(area_id);

CREATE TABLE IF NOT EXISTS area_entities (
    id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    section_id TEXT NOT NULL REFERENCES area_sections(id) ON DELETE CASCADE,
    display_name TEXT,
    columns INTEGER DEFAULT 1,
    rows INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    is_visible BOOLEAN DEFAULT 1,
    control_mode TEXT,
    tile TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(area_id, entity_id)
);

CREATE TABLE IF NOT EXISTS area_widgets (
    id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    section_id TEXT NOT NULL REFERENCES area_sections(id) ON DELETE CASCADE,
    widget_type TEXT NOT NULL,
    config TEXT NOT NULL DEFAULT '{}',
    columns INTEGER DEFAULT 1,
    rows INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS area_tags (
    area_id TEXT NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (area_id, tag)
);

CREATE TABLE IF NOT EXISTS paired_clients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    ha_token_encrypted TEXT,
    tags TEXT DEFAULT '[]',
    is_active BOOLEAN DEFAULT 1,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    device_name TEXT,
    app_version TEXT,
    app_build TEXT,
    platform TEXT,
    device_model TEXT,
    protocol_version INTEGER,
    battery_level INTEGER,
    connection_type TEXT,
    current_area_id TEXT,
    latitude REAL,
    longitude REAL,
    is_guest INTEGER DEFAULT 0,
    expires_at TEXT,
    url_mode TEXT DEFAULT 'both',
    ha_user_id TEXT,
    ha_refresh_token_id TEXT,
    ha_token_minted_at TEXT,
    ha_token_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (ha_token_status IN ('pending', 'active')),
    ha_token_pending_since TEXT,
    owner_label TEXT,
    remote_allowed INTEGER NOT NULL DEFAULT 0 CHECK (remote_allowed IN (0, 1)),
    can_add_devices INTEGER NOT NULL DEFAULT 0
        CHECK (can_add_devices IN (0, 1) AND NOT (is_guest = 1 AND can_add_devices = 1)),
    can_invite_guests INTEGER NOT NULL DEFAULT 0
        CHECK (can_invite_guests IN (0, 1) AND NOT (is_guest = 1 AND can_invite_guests = 1)),
    invited_by_client_id TEXT,
    invited_by_name TEXT,
    pending_deletion_at TEXT,
    pending_hidden_at TEXT,
    ha_user_released_at TEXT,
    notification_stacked_style TEXT,
    app_config_overrides TEXT,
    config_template_id TEXT,
    settings_hidden INTEGER DEFAULT 0,
    settings_lock_enabled INTEGER DEFAULT 0,
    settings_require_biometrics INTEGER DEFAULT 0,
    settings_pin_hash TEXT,
    settings_pin_salt TEXT,
    profile_id TEXT CHECK (NOT (is_guest = 1 AND profile_id IS NOT NULL)),
    kids_override_until TEXT,
    CHECK (NOT (is_guest = 1 AND remote_allowed = 1))
);
CREATE INDEX IF NOT EXISTS idx_paired_clients_pending
    ON paired_clients(ha_token_status) WHERE ha_token_status = 'pending';

CREATE TABLE IF NOT EXISTS token_requests (
    client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    token_action TEXT NOT NULL CHECK (token_action IN ('ensure', 'rotate')),
    reason TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    next_attempt_at TEXT,
    retry_delay REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS client_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_activity_client ON client_activity(client_id, id);

CREATE TABLE IF NOT EXISTS client_notifications (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    notification_type TEXT NOT NULL DEFAULT 'default',
    icon TEXT,
    icon_svg TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    dismissed_at TEXT,
    popup INTEGER DEFAULT 0,
    popup_mode TEXT DEFAULT 'none',
    scheduled_at TEXT,
    priority INTEGER DEFAULT 50,
    color TEXT DEFAULT 'none',
    action_type TEXT DEFAULT 'none',
    action_target TEXT,
    show_time INTEGER DEFAULT 1,
    delivered_via TEXT
);

CREATE TABLE IF NOT EXISTS kids_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL DEFAULT 'child' CHECK (kind IN ('child')),
    rules TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kids_profile_rules (
    profile_id TEXT NOT NULL REFERENCES kids_profiles(id) ON DELETE CASCADE,
    scope TEXT NOT NULL CHECK (scope IN ('area', 'entity')),
    target TEXT NOT NULL,
    rules TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, scope, target)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quick_access (
    entity_id TEXT PRIMARY KEY,
    level TEXT NOT NULL CHECK (level IN ('off', 'status', 'control')),
    critical_ack INTEGER NOT NULL DEFAULT 0 CHECK (critical_ack IN (0, 1)),
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS area_excluded_entities (
    area_id TEXT NOT NULL REFERENCES areas(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL,
    excluded_at TEXT NOT NULL,
    PRIMARY KEY (area_id, entity_id)
);

CREATE TABLE IF NOT EXISTS config_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    platform TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_area_entities_area_id ON area_entities(area_id);
CREATE INDEX IF NOT EXISTS idx_area_entities_entity_id ON area_entities(entity_id);
CREATE INDEX IF NOT EXISTS idx_area_entities_section_id ON area_entities(section_id);
CREATE INDEX IF NOT EXISTS idx_area_widgets_area_id ON area_widgets(area_id);
CREATE INDEX IF NOT EXISTS idx_area_widgets_section_id ON area_widgets(section_id);
CREATE INDEX IF NOT EXISTS idx_areas_ha_area_id ON areas(ha_area_id);
CREATE INDEX IF NOT EXISTS idx_areas_is_active ON areas(is_active);
CREATE INDEX IF NOT EXISTS idx_paired_clients_secret_hash ON paired_clients(secret_hash);
CREATE INDEX IF NOT EXISTS idx_paired_clients_active ON paired_clients(is_active);
CREATE INDEX IF NOT EXISTS idx_client_notifications_client_id ON client_notifications(client_id);
CREATE INDEX IF NOT EXISTS idx_client_notifications_status ON client_notifications(status);
CREATE INDEX IF NOT EXISTS idx_client_notifications_client_status ON client_notifications(client_id, status);
CREATE INDEX IF NOT EXISTS idx_area_excluded_entities_area_id ON area_excluded_entities(area_id);
CREATE INDEX IF NOT EXISTS idx_config_templates_platform ON config_templates(platform);
