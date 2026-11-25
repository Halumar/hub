# `models/` SQLite data layer and Pydantic schemas

This folder is the hub's data layer. It contains two independent concerns: `database.py`, an async SQLite wrapper (`aiosqlite`) that owns the database lifecycle (create / migrate / recover), transactions, low-level query helpers, and a key/value settings store; and `schemas.py`, the Pydantic request/response models that define the shape of the REST API and the HA integration commands. The two files do not import each other: the database deals in raw `dict` rows while schemas validate and serialize API payloads. Higher-level service and route code wires them together.

## Files

| File | Purpose |
| --- | --- |
| `__init__.py` | Empty package marker (makes `models` an importable package). |
| `database.py` | Async SQLite wrapper: connection lifecycle (restore, migrations, corruption recovery), `transaction()` unit of work with write lock, query helpers, settings store, API-token bootstrap, and helpers for the per-device HA token, identity and remote PIN. Schema and migrations live in `services/db_migrations.py`, snapshots in `services/db_snapshots.py`. |
| `schemas.py` | Pydantic models for the REST API and HA integration: areas, sections, entities, widgets, tiles, layout batches, HA sync, import/export, integration commands, kids check, events and panel commands. Further request models live next to their routers. |

## `database.py`

### Constants

- `SCHEMA_VERSION`, the newest migration (`services/db_migrations.latest_version()`), stored in `PRAGMA user_version`. Baseline is 1.
- `SCHEMA_SQL` (re-exported from `services/db_migrations.py`), the full `CREATE TABLE`/`CREATE INDEX` script. Tables: `areas`, `area_sections`, `area_entities`, `area_widgets`, `area_tags`, `area_excluded_entities`, `quick_access`, `paired_clients`, `client_activity`, `client_notifications`, `token_requests`, `settings`, the profile tables (`profiles`, `profile_revisions`, `device_overrides`, `profile_audit`), the family and kids tables (`family_scopes`, `kids_releases`, `kids_requests`, `family_answer_tokens`), `access_requests`/`access_answer_tokens`, `scan_tags`, and `event_log`/`log_notify_marks` of the log tab.
- `HA_TOKEN_PENDING` / `HA_TOKEN_ACTIVE`, the two values of `paired_clients.ha_token_status`.

### `class Database`

- `Database(db_path, reset_on_corruption=False)`; `initialize()` restores a pending HA-backup snapshot or a missing file, opens the DB, reads `user_version`, runs the integrity check (damaged -> newest valid snapshot, otherwise `DatabaseUnavailableError` or, with `reset_on_corruption`, archive + empty DB), creates the schema (fresh) or runs migrations (snapshot first, one transaction each), then `_ensure_api_token()`. `recovery_notice` describes what was restored.
- `transaction()`: async context manager, global write lock, `BEGIN IMMEDIATE`, commit/rollback (incl. cancellation), nested = `SAVEPOINT`. Writes and `commit()`/`rollback()` from other coroutines wait while it runs; `commit()` inside it is a no-op.
- `probe()` for `/api/health`; `has_encrypted_tokens()` for the key-file check.
- Query helpers: `connection` (property, asserts initialized), `execute()`, `executemany()`, `fetchone()` / `fetchall()` (return `dict` / `list[dict]` via the `aiosqlite.Row` row factory), `commit()`, `rollback()`, `close()`.
- Settings store: `get_setting(key)` / `set_setting(key, value)` over the `settings` table.
- Per-device HA user helpers: `store_client_token()` (encrypts the delivered token, sets `ha_user_id`, `ha_refresh_token_id`, `active`, one transaction), `mark_client_pending()` (drops an unusable token), `set_client_identity()` (name, `owner_label`), `set_client_remote_pin()` (PBKDF2 hash and salt), `set_pending_hidden()` (hides a tombstone from the clients list).
- `_ensure_api_token()` generates `secrets.token_hex(32)` on first start, stores only its SHA-256 hash under `api_token_hash`; the plaintext is neither logged nor persisted (Settings > API token > Regenerate shows a new one).
- `now()` (static) returns the current UTC time as an ISO-8601 string, used for `created_at`/`updated_at` values.

## `schemas.py`

All Pydantic `BaseModel` classes, grouped by domain. The recurring pattern is a triplet per resource: `*Create` (input, with `Field` constraints), `*Update` (all-optional input for PATCH), `*Response` (output). `control_mode` is validated by a shared `field_validator` to one of `"basic"`/`"advanced"`/`"full"` (or `None` for inherit/disabled). `notification_type` accepts the canonical `"default"`/`"sticky"` plus the aliases `"normal"`/`"permanent"` and persists the canonical value. Imports `VERSION` from the top-level `version` module.

- Sections: `SectionCreate`, `SectionUpdate`, `SectionResponse` (nests `entities: list[AreaEntityResponse]` and `widgets: list[AreaWidgetResponse]`), `SectionOrderUpdate` (reorder by `section_ids`); area order: `AreaOrderUpdate`, `AreaOrderResponse`.
- Areas: `AreaCreate` (validates `color` as `#RRGGBB`, `control_mode`, `tags`), `AreaUpdate`, `AreaResponse` (includes computed `*_src` servable-image URLs plus `entity_count`/`section_count`), `AreaDetailResponse` (extends `AreaResponse` with `sections: list[SectionResponse]`).
- Entities: `AreaEntityCreate`, `AreaEntityUpdate`, `AreaEntityResponse` (carries an optional `state: dict` populated from live HA state on request).
- Widgets: `AreaWidgetCreate`, `AreaWidgetUpdate`, `AreaWidgetResponse` (`config` is a free-form `dict`, stored as JSON text in the `area_widgets.config` column).
- Batch layout: `SectionLayoutItem` (one entity or widget placement) and `SectionLayoutUpdate` (`items: list[...]`) for drag-and-drop reordering in one call.
- HA integration: `HAEntityResponse`, `HAAreaResponse`, `SyncFromHARequest` / `SyncAreaEntitiesRequest` (selective-import filters), `SyncResult` (counts plus `errors`).
- Import/export: `ExportData` (`format_version`, `version=VERSION`, `areas`), `ImportRequest` (`areas`, `overwrite`).
- Tiles: `TileSettings` with `TileSecondary`, `TileTapAction`, `TileHoldAction`.
- Integration commands (actions in Home Assistant): `NavigateToAreaRequest`, `ShowEntityRequest`, `ClientTokenDelivery`, `HAUrlReport`, `ClientTagsRequest`, `ExtendGuestRequest`, `AckNotificationsRequest`, `PairingGenerateRequest` (guest-PIN options), `SendNotificationRequest`, `IntegrationNotifyRequest`, `NotificationHandoverRequest`.
- Kids check, events and panel: `KidsCheckRequest`/`KidsCheckResponse`, `IntegrationEventsRequest`/`IntegrationEventsResponse`, `PanelCommandRequest`, `IntegrationPanelCommandRequest`, `PanelCommandResponse`.

## Data flow and patterns

- Forward-only schema management through numbered migrations (`@migration(N)` in `services/db_migrations.py`), each in one transaction; no upgrade path from pre-release schemas.
- Resilience first: integrity check on every start; damaged data is restored from a snapshot (`/data/backups`) or the start stops in an error state, never silently with an empty database. WAL mode plus a busy timeout are set on every connection.
- Rows as dicts: the database layer never returns ORM objects; `fetchone`/`fetchall` yield plain dicts. API routes feed those dicts into the matching `*Response` Pydantic model for serialization and validate incoming bodies with `*Create`/`*Update`.
- Layout hierarchy: `areas` contain `area_sections`, which contain `area_entities` or `area_widgets`, enforced by cascading foreign keys. This mirrors the nested `AreaDetailResponse` to `SectionResponse` to entities/widgets response shape.
- Security at rest: API tokens, the integration token, and paired-client secrets are stored only as SHA-256 hashes; per-device HA tokens are Fernet-encrypted (never plaintext); remote settings PINs are stored as a PBKDF2-HMAC-SHA256 hash plus salt.
