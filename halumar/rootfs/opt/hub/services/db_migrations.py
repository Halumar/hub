"""Schema and versioned migrations.

Rules:
- ``PRAGMA user_version`` holds the schema version (no version table).
- A fresh install creates :data:`SCHEMA_SQL` (the current schema) in one
  transaction and starts at the newest version: no migration steps run.
- An existing database runs every migration above its version, each in its
  own ``BEGIN IMMEDIATE`` transaction together with the version bump. A
  failing step rolls back completely and stops the start; the database stays
  at the last good version. A snapshot is taken before the first step.
- Migrations never swallow errors. Additive columns are compared against
  ``PRAGMA table_info`` (:func:`add_missing_columns`) instead of "ALTER and
  ignore the error".
- A schema change = a new ``@migration(N)`` function here AND the same change
  in :data:`SCHEMA_SQL`; ``tests/test_migrations.py`` shows the pattern.

Version history:
  1  baseline (pre-release schemas have no upgrade path). Includes one
     HA user per device: paired_clients.ha_user_id, owner_label,
     remote_allowed, ha_token_status pending|active, ha_token_pending_since;
     no owner token, no hub-minted token metadata.
  2  token_requests (outstanding rotations and the retry backoff of
     provisioning requests survive a restart);
     paired_clients.ha_user_released_at (a deleted device keeps its HA user
     until its app confirmed the reset or the grace period ran out).
  3  protocol version: client info per device
     (paired_clients.platform, app_build, device_model, protocol_version;
     app_version existed) and the activity feed client_activity.
  4  tile settings per area entity: area_entities.tile, canonical JSON
     of the writable tile fields, NULL = default.
  5  paired_clients.can_add_devices (onboarding by QR code).
  6  guest invitations by members: paired_clients.can_invite_guests
     (admin release, never guests), invited_by_client_id / invited_by_name
     (who invited a guest; the name survives the inviting device).
  7  quick access (widgets, controls, Siri): quick_access (level per
     entity, critical_ack).
  8  profiles and kids mode: kids_profiles (child profiles with their
     profile-level rules), kids_profile_rules (area/entity rules),
     paired_clients.profile_id (NULL = adult, never guests) and
     kids_override_until (parent release).
  9  profile model: profiles (one per device, body with the
     value per catalog rule, fixed and user's choice, origin suggestion and
     version, optional profile remote PIN), profile_revisions, device_overrides
     (permanent or until expires_at), profile_audit (before/after);
     paired_clients.access member|guest, profile_id NOT NULL, own_area_id;
     the per-device rights, templates, managed settings, tags and kids
     profile tables are dropped. Existing data is carried over with a
     self check (services/profiles/migration_v9.py).
  10 persistent log: event_log (activity, changes and alarms with
     their retention, read marks of alarms) and log_notify_marks (which
     notification of an expiring guest/adjustment or an outage went out).
  11 family manager: family_scopes (children and HA notify targets of a
     family manager device), kids_releases (running releases of a child
     device), kids_requests and family_answer_tokens (requests of children,
     hashed answer tokens of HA notifications); device_overrides.origin and
     created_by_client_id (who set an adjustment). The activity of the
     children is a read view of event_log (v10).
  12 Android default profile slots.
  13 setting up devices: profiles.kind (and profile_revisions.kind),
     derived from the origin suggestion, the guest default slot or parental
     controls, else person; the rules add_devices and invite_guests in
     profile bodies and device adjustments become setup_devices;
     paired_clients.set_up_by_client_id / set_up_by_name / set_up_method
     (who set the device up; taken from the invited_by_* columns, plus the devices
     confirmed by QR code from their paired activity).
  14 access hours and babysitter: paired_clients.access_switch (access
     switch, default on) and access_state (last logged access, open|closed);
     profiles.kind also babysitter (table rebuild), profiles copied from the
     Babysitter suggestion become kind babysitter.
  15 watches: paired_clients.seal_key (the X25519 public key a
     watch paired with; the hub seals the watch's HA access to it for its
     phone, watch_access).
  16 tags: scan_tags.
  17 early access: paired_clients.access_early_json (running early
     access of a device: until, by, request_id, duration, at), access_requests
     (requests of devices outside their access hours) and
     access_answer_tokens (hashed answer tokens of HA notifications).
  18 room climate: areas.room_temperature_entity_id / room_humidity_entity_id.
  19 paired_clients.remote_lost_at (when the effective remote access of a
     device last went from on to off; the HA key catch-up rotates a child's
     token minted before it).
  20 Hub only: paired_clients.key_lost_at (renamed from remote_lost_at:
     when a device last stopped holding a Home Assistant key); profiles with
     parental controls and of the kinds guest, babysitter and house sitter
     get control_path hub (the guest kinds fixed), control_path adjustments
     on their devices are removed, each changed profile gets a new revision
     and devices of the guest kinds that hold no key get key_lost_at
     (services/profiles/migration_v20.py).
"""

import logging
import sqlite3
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiosqlite

logger = logging.getLogger("hub.migrations")

# Version a fresh install starts from; migrations are numbered above it.
BASELINE_VERSION = 1

PAIRED_CLIENTS_SQL = """CREATE TABLE IF NOT EXISTS paired_clients (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    ha_token_encrypted TEXT,
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
    access TEXT NOT NULL DEFAULT 'member' CHECK (access IN ('member', 'guest')),
    expires_at TEXT,
    ha_user_id TEXT,
    ha_refresh_token_id TEXT,
    ha_token_minted_at TEXT,
    ha_token_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (ha_token_status IN ('pending', 'active')),
    ha_token_pending_since TEXT,
    owner_label TEXT,
    set_up_by_client_id TEXT,
    set_up_by_name TEXT,
    set_up_method TEXT CHECK (set_up_method IN ('onboard', 'invite')),
    pending_deletion_at TEXT,
    pending_hidden_at TEXT,
    ha_user_released_at TEXT,
    settings_pin_hash TEXT,
    settings_pin_salt TEXT,
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE RESTRICT,
    own_area_id TEXT REFERENCES areas(id) ON DELETE SET NULL,
    kids_override_until TEXT,
    access_switch INTEGER NOT NULL DEFAULT 1 CHECK (access_switch IN (0, 1)),
    access_state TEXT CHECK (access_state IN ('open', 'closed')),
    seal_key TEXT,
    access_early_json TEXT,
    key_lost_at TEXT,
    CHECK ((access = 'guest') = (expires_at IS NOT NULL))
);"""

# Profile kinds of the contract ($defs/profile_kind).
PROFILE_KINDS = (
    "guest",
    "child",
    "panel",
    "tv",
    "house_sitter",
    "babysitter",
    "person",
)

PROFILE_TABLES_SQL = """CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    icon TEXT,
    color TEXT,
    kind TEXT NOT NULL DEFAULT 'person' CHECK (kind IN (__PROFILE_KINDS__)),
    child_protection INTEGER NOT NULL DEFAULT 0 CHECK (child_protection IN (0, 1)),
    body_json TEXT NOT NULL,
    origin_suggestion TEXT,
    origin_version INTEGER,
    remote_pin_hash TEXT,
    remote_pin_salt TEXT,
    remote_pin_fixed INTEGER NOT NULL DEFAULT 0 CHECK (remote_pin_fixed IN (0, 1)),
    rev INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS profile_revisions (
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    rev INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'person',
    child_protection INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    PRIMARY KEY (profile_id, rev)
);

CREATE TABLE IF NOT EXISTS device_overrides (
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    rule_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    user_choice INTEGER NOT NULL DEFAULT 0 CHECK (user_choice IN (0, 1)),
    expires_at TEXT,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'admin'
        CHECK (origin IN ('admin', 'family_manager', 'system')),
    created_by_client_id TEXT,
    PRIMARY KEY (client_id, rule_key)
);

CREATE TABLE IF NOT EXISTS profile_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT,
    profile_id TEXT,
    client_id TEXT,
    rule_key TEXT,
    before_json TEXT,
    after_json TEXT,
    note TEXT
);""".replace(
    "__PROFILE_KINDS__",
    ", ".join(f"'{k}'" for k in PROFILE_KINDS),
)

PROFILE_TRIGGERS_SQL = """CREATE TRIGGER IF NOT EXISTS trg_guest_no_child_protection_insert
BEFORE INSERT ON paired_clients
WHEN NEW.access = 'guest'
    AND (SELECT child_protection FROM profiles WHERE id = NEW.profile_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'profile.guest_child_protection');
END;

CREATE TRIGGER IF NOT EXISTS trg_guest_no_child_protection_update
BEFORE UPDATE OF access, profile_id ON paired_clients
WHEN NEW.access = 'guest'
    AND (SELECT child_protection FROM profiles WHERE id = NEW.profile_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'profile.guest_child_protection');
END;

CREATE TRIGGER IF NOT EXISTS trg_child_protection_no_guest
BEFORE UPDATE OF child_protection ON profiles
WHEN NEW.child_protection = 1
    AND EXISTS (SELECT 1 FROM paired_clients WHERE profile_id = NEW.id AND access = 'guest')
BEGIN
    SELECT RAISE(ABORT, 'profile.guest_child_protection');
END;"""

PROFILE_INDEXES_SQL = """CREATE INDEX IF NOT EXISTS idx_paired_clients_profile ON paired_clients(profile_id);
CREATE INDEX IF NOT EXISTS idx_device_overrides_expires
    ON device_overrides(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_profile_audit_client ON profile_audit(client_id, id);
CREATE INDEX IF NOT EXISTS idx_profile_audit_profile ON profile_audit(profile_id, id);"""

FAMILY_TABLES_SQL = """CREATE TABLE IF NOT EXISTS family_scopes (
    client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
    all_children INTEGER NOT NULL DEFAULT 1 CHECK (all_children IN (0, 1)),
    child_ids_json TEXT NOT NULL DEFAULT '[]',
    notify_targets_json TEXT NOT NULL DEFAULT '[]',
    notify_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS kids_releases (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('bedtime', 'pause', 'entity')),
    entity_id TEXT NOT NULL DEFAULT '',
    until TEXT NOT NULL,
    by_json TEXT NOT NULL,
    request_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (client_id, kind, entity_id)
);

CREATE TABLE IF NOT EXISTS kids_requests (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    entity_id TEXT,
    note TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('open', 'approved', 'declined', 'expired', 'cancelled')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    answer_json TEXT,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS family_answer_tokens (
    token_hash TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES kids_requests(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    manager_client_id TEXT,
    notify_service TEXT,
    expires_at TEXT NOT NULL
);"""

FAMILY_INDEXES_SQL = """CREATE INDEX IF NOT EXISTS idx_kids_requests_client ON kids_requests(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kids_requests_status ON kids_requests(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_kids_releases_until ON kids_releases(until);"""

EVENT_LOG_SQL = """CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE,
    ts TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('activity', 'change', 'alarm')),
    kind TEXT NOT NULL,
    event_kind TEXT,
    source TEXT NOT NULL,
    client_id TEXT,
    device_name TEXT,
    owner_label TEXT,
    profile_id TEXT,
    profile_name TEXT,
    area_id TEXT,
    entity_id TEXT,
    action TEXT,
    route TEXT,
    origin TEXT,
    control_path TEXT,
    result TEXT,
    reason TEXT,
    count INTEGER,
    since TEXT,
    until TEXT,
    actor TEXT,
    detail_json TEXT,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts);
CREATE INDEX IF NOT EXISTS idx_event_log_client ON event_log(client_id, ts);
CREATE INDEX IF NOT EXISTS idx_event_log_alarm ON event_log(category, read_at);

CREATE TABLE IF NOT EXISTS log_notify_marks (
    key TEXT PRIMARY KEY,
    ts TEXT NOT NULL
);"""

# NFC/QR tags registered in the hub (contract common/scan-tag.json).
SCAN_TAGS_SQL = """CREATE TABLE IF NOT EXISTS scan_tags (
    tag_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    action_kind TEXT NOT NULL
        CHECK (action_kind IN ('show', 'toggle', 'turn_on', 'turn_off', 'run')),
    entity_id TEXT,
    area_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_scanned_at TEXT
)"""

# Early access requests (contract common/access-request.json).
ACCESS_REQUESTS_SQL = """CREATE TABLE IF NOT EXISTS access_requests (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    note TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('open', 'approved', 'declined', 'expired', 'cancelled')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    opens_at TEXT,
    recipients_json TEXT NOT NULL DEFAULT '[]',
    answer_json TEXT,
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS access_answer_tokens (
    token_hash TEXT PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES access_requests(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    recipient_client_id TEXT NOT NULL,
    notify_service TEXT,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_requests_client ON access_requests(client_id, created_at);
CREATE INDEX IF NOT EXISTS idx_access_requests_status ON access_requests(status, expires_at)"""


SCHEMA_SQL = (
    """
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
    room_temperature_entity_id TEXT,
    room_humidity_entity_id TEXT,
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

__PAIRED_CLIENTS__
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

__PROFILE_TABLES__
__FAMILY_TABLES__
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

__SCAN_TAGS__

__ACCESS_REQUESTS__

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
__PROFILE_INDEXES__
__EVENT_LOG__
""".replace("__PAIRED_CLIENTS__", PAIRED_CLIENTS_SQL)
    .replace("__PROFILE_TABLES__", PROFILE_TABLES_SQL)
    .replace("__PROFILE_INDEXES__", PROFILE_INDEXES_SQL + "\n" + PROFILE_TRIGGERS_SQL)
    .replace("__FAMILY_TABLES__", FAMILY_TABLES_SQL + "\n" + FAMILY_INDEXES_SQL)
    .replace("__EVENT_LOG__", EVENT_LOG_SQL)
    .replace("__SCAN_TAGS__", SCAN_TAGS_SQL + ";")
    .replace("__ACCESS_REQUESTS__", ACCESS_REQUESTS_SQL + ";")
)


def split_sql(script: str) -> list[str]:
    """Split a script into single statements (``executescript`` would COMMIT
    the surrounding transaction first)."""
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        if not buf and (not line.strip() or line.strip().startswith("--")):
            continue
        buf.append(line)
        candidate = "\n".join(buf)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            buf = []
    if buf and "\n".join(buf).strip():
        raise ValueError(f"incomplete SQL statement: {' '.join(buf)[:80]}")
    return statements


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: Callable[[aiosqlite.Connection], Awaitable[None]]
    # Rebuilds a table that others reference: runs with PRAGMA foreign_keys
    # off (only possible outside the transaction) and ends with
    # PRAGMA foreign_key_check (SQLite's documented table rebuild).
    foreign_keys_off: bool = False


MIGRATIONS: dict[int, Migration] = {}


def migration(version: int, description: str, *, foreign_keys_off: bool = False):
    """Register ``fn(conn)`` as the step to ``version`` (runs inside a transaction)."""

    def deco(fn):
        if version in MIGRATIONS:
            raise ValueError(f"duplicate migration v{version}")
        if version <= BASELINE_VERSION:
            raise ValueError(f"migration v{version} is not above the baseline")
        MIGRATIONS[version] = Migration(version, description, fn, foreign_keys_off)
        return fn

    return deco


def latest_version() -> int:
    return max(MIGRATIONS, default=BASELINE_VERSION)


def steps(from_version: int, to_version: int) -> list[Migration]:
    """Migrations to run from ``from_version`` to ``to_version``; raises on a gap."""
    if from_version < BASELINE_VERSION:
        raise RuntimeError(f"unknown schema version {from_version}")
    start = from_version
    missing = [v for v in range(start + 1, to_version + 1) if v not in MIGRATIONS]
    if missing:
        raise RuntimeError(
            f"no migration registered for schema version(s) {missing} "
            f"(database v{from_version}, add-on v{to_version})"
        )
    return [MIGRATIONS[v] for v in range(start + 1, to_version + 1)]


async def table_columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        return [row[1] for row in await cursor.fetchall()]


async def add_missing_columns(
    conn: aiosqlite.Connection, table: str, columns: list[tuple[str, str]]
) -> list[str]:
    """``ALTER TABLE ADD COLUMN`` for every column not in ``table`` yet.

    Existing columns are read from ``PRAGMA table_info``; any error of the
    ALTER itself (locked, I/O, disk full) propagates.
    """
    existing = set(await table_columns(conn, table))
    if not existing:
        raise sqlite3.OperationalError(f"no such table: {table}")
    added = []
    for name, typedef in columns:
        if name in existing:
            continue
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typedef}")
        added.append(f"{table}.{name}")
    return added


TOKEN_REQUESTS_SQL = """
CREATE TABLE IF NOT EXISTS token_requests (
    client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    token_action TEXT NOT NULL CHECK (token_action IN ('ensure', 'rotate')),
    reason TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    next_attempt_at TEXT,
    retry_delay REAL NOT NULL DEFAULT 0
)
"""


@migration(2, "token_requests, paired_clients.ha_user_released_at")
async def _v2_token_requests_and_user_hold(conn: aiosqlite.Connection) -> None:
    await conn.execute(TOKEN_REQUESTS_SQL)
    await add_missing_columns(conn, "paired_clients", [("ha_user_released_at", "TEXT")])


# Client info columns of the version check (app_version already exists in v1).
CLIENT_INFO_COLUMNS = [
    ("app_build", "TEXT"),
    ("platform", "TEXT"),
    ("device_model", "TEXT"),
    ("protocol_version", "INTEGER"),
]


@migration(3, "client info per device and activity feed")
async def _v3_client_info_and_activity(conn: aiosqlite.Connection) -> None:
    await add_missing_columns(conn, "paired_clients", CLIENT_INFO_COLUMNS)
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS client_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL REFERENCES paired_clients(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT
)"""
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_client_activity_client "
        "ON client_activity(client_id, id)"
    )


@migration(4, "area_entities.tile (tile settings)")
async def _v4_tile_settings(conn: aiosqlite.Connection) -> None:
    await add_missing_columns(conn, "area_entities", [("tile", "TEXT")])


CAN_ADD_DEVICES_COLUMN = (
    "can_add_devices",
    "INTEGER NOT NULL DEFAULT 0 CHECK (can_add_devices IN (0, 1) "
    "AND NOT (is_guest = 1 AND can_add_devices = 1))",
)


@migration(5, "paired_clients.can_add_devices (onboarding by QR code)")
async def _v5_can_add_devices(conn: aiosqlite.Connection) -> None:
    await add_missing_columns(conn, "paired_clients", [CAN_ADD_DEVICES_COLUMN])


GUEST_INVITE_COLUMNS = [
    (
        "can_invite_guests",
        "INTEGER NOT NULL DEFAULT 0 CHECK (can_invite_guests IN (0, 1) "
        "AND NOT (is_guest = 1 AND can_invite_guests = 1))",
    ),
    ("invited_by_client_id", "TEXT"),
    ("invited_by_name", "TEXT"),
]


@migration(6, "guest invitations by members")
async def _v6_guest_invites(conn: aiosqlite.Connection) -> None:
    await add_missing_columns(conn, "paired_clients", GUEST_INVITE_COLUMNS)


QUICK_ACCESS_TABLE = """CREATE TABLE IF NOT EXISTS quick_access (
    entity_id TEXT PRIMARY KEY,
    level TEXT NOT NULL CHECK (level IN ('off', 'status', 'control')),
    critical_ack INTEGER NOT NULL DEFAULT 0 CHECK (critical_ack IN (0, 1)),
    updated_at TEXT NOT NULL,
    updated_by TEXT
)"""


@migration(7, "quick_access (widgets, controls, Siri per entity)")
async def _v7_quick_access(conn: aiosqlite.Connection) -> None:
    await conn.execute(QUICK_ACCESS_TABLE)


KIDS_TABLES_SQL = """
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
"""

KIDS_CLIENT_COLUMNS = [
    ("profile_id", "TEXT CHECK (NOT (is_guest = 1 AND profile_id IS NOT NULL))"),
    ("kids_override_until", "TEXT"),
]


@migration(8, "profiles and kids mode")
async def _v8_kids_mode(conn: aiosqlite.Connection) -> None:
    for stmt in split_sql(KIDS_TABLES_SQL):
        await conn.execute(stmt)
    await add_missing_columns(conn, "paired_clients", KIDS_CLIENT_COLUMNS)


@migration(9, "profile model", foreign_keys_off=True)
async def _v9_profiles(conn: aiosqlite.Connection) -> None:
    from services.profiles import migration_v9

    report = await migration_v9.run(conn)
    logger.warning(
        "%s passed: %d devices, 0 deviations, %d profiles, %d adjustments, "
        "%d boundary changes",
        migration_v9.SELF_CHECK_PREFIX,
        len(report["devices"]),
        len(report["profiles"]),
        sum(len(d["overrides"]) for d in report["devices"]),
        len(report["boundary_changes"]),
    )


@migration(10, "persistent log")
async def _v10_event_log(conn: aiosqlite.Connection) -> None:
    for stmt in split_sql(EVENT_LOG_SQL):
        await conn.execute(stmt)


FAMILY_OVERRIDE_COLUMNS = [
    (
        "origin",
        "TEXT NOT NULL DEFAULT 'admin' "
        "CHECK (origin IN ('admin', 'family_manager', 'system'))",
    ),
    ("created_by_client_id", "TEXT"),
]


@migration(11, "family manager")
async def _v11_family(conn: aiosqlite.Connection) -> None:
    for stmt in split_sql(FAMILY_TABLES_SQL + "\n" + FAMILY_INDEXES_SQL):
        await conn.execute(stmt)
    await add_missing_columns(conn, "device_overrides", FAMILY_OVERRIDE_COLUMNS)


@migration(12, "Android default profile slots")
async def _v12_android_slots(conn: aiosqlite.Connection) -> None:
    """New slots android_tablet, android_tv, wearos (and android, if missing)
    start with the default profile of their Apple counterpart; no profile or
    device changes."""
    from services.profiles import catalog as cat
    from services.profiles import rows

    for android, apple in cat.ANDROID_COUNTERPART.items():
        await conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) "
            "SELECT ?, value FROM settings WHERE key = ?",
            (rows.default_setting_key(android), rows.default_setting_key(apple)),
        )


@migration(13, "setting up devices")
async def _v13_setup_devices(conn: aiosqlite.Connection) -> None:
    from services.profiles import migration_v13

    await migration_v13.run(conn)


ACCESS_CLIENT_COLUMNS = [
    (
        "access_switch",
        "INTEGER NOT NULL DEFAULT 1 CHECK (access_switch IN (0, 1))",
    ),
    ("access_state", "TEXT CHECK (access_state IN ('open', 'closed'))"),
]


@migration(14, "access hours and babysitter", foreign_keys_off=True)
async def _v14_access(conn: aiosqlite.Connection) -> None:
    """paired_clients.access_switch / access_state; profiles.kind accepts
    babysitter (table rebuild: SQLite cannot change a CHECK); profiles copied
    from the Babysitter suggestion become kind babysitter. Rules and bodies
    stay as they are."""
    import re

    await add_missing_columns(conn, "paired_clients", ACCESS_CLIENT_COLUMNS)
    create = re.search(
        r"CREATE TABLE IF NOT EXISTS profiles \(.*?\n\);", SCHEMA_SQL, re.S
    )
    if create is None:  # pragma: no cover - schema text changed
        raise RuntimeError("profiles table not found in the schema")
    cols = ", ".join(await table_columns(conn, "profiles"))
    # Triggers of other tables name profiles: drop them during the rebuild.
    for name in re.findall(r"CREATE TRIGGER IF NOT EXISTS (\w+)", PROFILE_TRIGGERS_SQL):
        await conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    await conn.execute("DROP TABLE IF EXISTS profiles_v14")
    await conn.execute(
        create.group(0).replace(
            "CREATE TABLE IF NOT EXISTS profiles (", "CREATE TABLE profiles_v14 ("
        )
    )
    await conn.execute(
        f"INSERT INTO profiles_v14 ({cols}) SELECT {cols} FROM profiles"  # noqa: S608 - own columns
    )
    await conn.execute("DROP TABLE profiles")
    await conn.execute("ALTER TABLE profiles_v14 RENAME TO profiles")
    for stmt in split_sql(PROFILE_INDEXES_SQL + "\n" + PROFILE_TRIGGERS_SQL):
        await conn.execute(stmt)
    await conn.execute(
        "UPDATE profiles SET kind = 'babysitter' WHERE origin_suggestion = 'babysitter'"
    )
    await conn.execute(
        "UPDATE profile_revisions SET kind = 'babysitter' WHERE profile_id IN "
        "(SELECT id FROM profiles WHERE kind = 'babysitter')"
    )


WATCH_SEAL_COLUMNS = [("seal_key", "TEXT")]


@migration(15, "paired_clients.seal_key (watch access)")
async def _v15_watch_seal_key(conn: aiosqlite.Connection) -> None:
    await add_missing_columns(conn, "paired_clients", WATCH_SEAL_COLUMNS)


@migration(16, "scan_tags")
async def _v16_scan_tags(conn: aiosqlite.Connection) -> None:
    """Table scan_tags for NFC/QR tags."""
    await conn.execute(SCAN_TAGS_SQL)


EARLY_ACCESS_COLUMNS = [("access_early_json", "TEXT")]


@migration(17, "early access requests")
async def _v17_access_requests(conn: aiosqlite.Connection) -> None:
    """paired_clients.access_early_json, tables access_requests and
    access_answer_tokens (additive)."""
    await add_missing_columns(conn, "paired_clients", EARLY_ACCESS_COLUMNS)
    for stmt in split_sql(ACCESS_REQUESTS_SQL + ";"):
        await conn.execute(stmt)


ROOM_CLIMATE_COLUMNS = [
    ("room_temperature_entity_id", "TEXT"),
    ("room_humidity_entity_id", "TEXT"),
]


@migration(18, "areas.room_*_entity_id (room climate sensors)")
async def _v18_room_climate(conn: aiosqlite.Connection) -> None:
    """The admin's room climate sensors per area (null = automatic)."""
    await add_missing_columns(conn, "areas", ROOM_CLIMATE_COLUMNS)


REMOTE_LOST_COLUMNS = [("remote_lost_at", "TEXT")]


@migration(19, "paired_clients.remote_lost_at (HA key catch-up)")
async def _v19_remote_lost_at(conn: aiosqlite.Connection) -> None:
    """When a device's effective remote access last went from on to off."""
    await add_missing_columns(conn, "paired_clients", REMOTE_LOST_COLUMNS)


@migration(20, "Hub only (control_path hub, paired_clients.key_lost_at)")
async def _v20_hub_only(conn: aiosqlite.Connection) -> None:
    from services.profiles import migration_v20

    await migration_v20.run(conn)


async def seed_fresh(conn: aiosqlite.Connection) -> None:
    """Data a fresh database needs: the starter profiles."""
    from datetime import datetime, timezone

    from services.profiles import rows

    await rows.create_starters(
        conn, now=datetime.now(timezone.utc).isoformat(), actor=None
    )


def expected_columns() -> dict[str, set[str]]:
    """Tables and columns of :data:`SCHEMA_SQL` (built in a scratch database)."""
    conn = sqlite3.connect(":memory:")
    try:
        for stmt in split_sql(SCHEMA_SQL):
            conn.execute(stmt)
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        return {
            table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for table in tables
        }
    finally:
        conn.close()


async def missing_schema_parts(conn: aiosqlite.Connection) -> list[str]:
    """Tables/columns of the current schema the database lacks.

    A database at the current version must have all of them; one that does
    not was written by a different (pre-release) build with the same version
    number. Extra tables or columns are ignored.
    """
    missing: list[str] = []
    for table, columns in sorted(expected_columns().items()):
        actual = set(await table_columns(conn, table))
        if not actual:
            missing.append(table)
            continue
        missing.extend(f"{table}.{col}" for col in sorted(columns - actual))
    return missing
