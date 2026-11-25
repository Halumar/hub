# `services/` Backend service layer

This folder contains the backend service modules of the hub. Each module encapsulates one concern that the API layer (FastAPI routes, WebSocket handlers) and startup code depend on. The modules are mostly function collections (plus classes such as `HAClient` and `HAWebSocket`); they receive their `Database` and HA-client dependencies as arguments. Process-wide state is limited to caches and registries named in the module docstrings (for example the key cache in `crypto`, the zeroconf handles in `mdns`, the served-certificate fingerprint in `tls`, the in-memory telemetry in `integration_status`).

Every module starts with a docstring that describes its contract; the table is an index.

## Files

### Home Assistant and the integration

| File | Purpose |
|------|---------|
| `ha_client.py` | `HAClient`: REST and WebSocket calls to HA Core and the Supervisor API through the Supervisor proxy; reads use the live state mirror of `ha_ws` while it is up. |
| `ha_ws.py` | The hub's persistent WebSocket connection to HA Core: live state mirror, registry cache, event subscriptions, reconnect. |
| `ha_sensors.py` | Mirrors areas, guests and per-device notifications into HA sensor entities and the area activation `input_boolean` switches; writes only on content changes; slug helpers (`SLUG_MAX_LEN` 48). |
| `ha_url.py` | The internal and external HA URLs handed to apps (internal from `GET /core/info` when HA has no `internal_url`). |
| `device_provisioning.py` | One HA user per paired device: `provision_client`, `update_client_identity` and `deprovision_client` requests to the integration, rotations, retry with backoff, `provisioning[]` of the status snapshot. |
| `discovery.py` | Supervisor discovery announce: rotates the integration token and announces `{host, port, api_token, version}` on every start. |
| `integration_status.py` | In-memory connection telemetry of the integration (`GET /api/admin/integration-status`). |
| `integration_source.py` | Where the integration token is accepted from. |
| `integration_events.py` | Events the integration reports for the log tab: intake and hand-over. |
| `enforcement.py` | Rule snapshots per device that the integration enforces in Home Assistant. |
| `panel.py` | Wall panel as a Home Assistant device: status, commands, reserved actions. |
| `pairing_qr.py` | Pairing QR in the integration's camera, governed by the app option `pairing_qr_camera` (`off`, `guests`, `all`). |

### Devices, pairing and access

| File | Purpose |
|------|---------|
| `client_pairing.py` | Creates a paired device; shared by PIN pairing, onboarding and guest invitations. |
| `onboarding.py` | Setting up a device from an already paired device (sessions in memory). |
| `onboard_crypto.py` | Seal of the onboarding credentials (HPKE base mode, RFC 9180). |
| `guest_invites.py` | Guest invitations created by member devices (in memory). |
| `watch_access.py` | Home Assistant access of watches, sealed to the watch's key; a child's watch follows its phone. |
| `pairing_link.py` | Canonical `halumar://pair` deep link and LAN base-URL resolution (single source for every QR and link). |
| `pin_hash.py` | PBKDF2-HMAC-SHA256 hash of the remote settings PIN (100 000 iterations). |
| `protocol.py` | Protocol version, `MIN_APP_VERSION` per platform, check of the client info in `auth`/`pair`, the platform fixed at pairing. |
| `access.py` | Access hours and access switch of a device. |
| `access_requests.py` | Early access requests and their answers. |
| `home_networks.py` | Further networks that count as at home. |
| `location.py` | Location of devices with the effective rule `location`. |
| `compliance.py` | PII-free per-client compliance status, HMAC-signed for the restricting statuses. |
| `client_activity.py` | Activity feed per device (ring buffer in `client_activity`, broadcast `device_activity`). |
| `expiry.py` | Guest expiry timestamps: one parser, one storage format, datetime compares. |
| `ingress.py` | Single source of truth for HA Ingress trust (REST and WebSocket). |

### Profiles, kids mode and family

| File | Purpose |
|------|---------|
| `profiles/catalog.py` | Rule catalog and suggestions of the profile model (contract data). |
| `profiles/resolver.py` | Profile resolver: effective rules of a device (port of the contract's reference, shared vectors). |
| `profiles/store.py` | Profiles, device adjustments and effective rules on the hub database. |
| `profiles/changes.py` | Profile writes with preview, audit and side effects. |
| `profiles/rows.py` | Low-level profile rows on a raw `aiosqlite` connection. |
| `profiles/schema.py` | Small JSON Schema (2020-12 subset) validator for the rule values. |
| `profiles/expiry_task.py` | Temporary device adjustments end on time. |
| `profiles/migration_v9.py`, `profiles/migration_v13.py`, `profiles/migration_v20.py` | Data conversions of the schema migrations 9 (profile model, with a self check), 13 (setting up devices) and 20 (Hub only: parental controls and the guest kinds on the hub path, `key_lost_at`). |
| `kids_rules.py` | Pure rule engine of the kids mode: layers, bedtime windows across midnight and DST, rate limit and cooldown, color lock, volume cap. |
| `hub_control.py` | The hub path (parental controls or "Hub only"): which device is on it, its scope, the read limits, WS `control` / REST `POST /api/client/control` and the call to HA Core with the device's token (a grant only for children). |
| `kids_mode.py` | The kids rules on the hub path: decision, counters, pauses, releases, kids status. |
| `hub_read.py` | History and weather forecast for devices on the hub path. |
| `hub_images.py` | Camera still images and media artwork for devices on the hub path, read with the hub's own access (scope first, limits, no copy kept). |
| `state_relay.py` | Live entity states for devices on the hub path (the whole scope in the first snapshot). |
| `scene_targets.py` | Scenes and scripts whose targets a profile or device may not switch. |
| `family/` | Family manager: permission check (`check.py`, port of the contract's reference), operations (`service.py`), tables (`store.py`), contract data (`data.py`), log events (`events.py`), HA notifications (`notify.py`), planner for releases and requests (`expiry.py`). |

### Areas, tiles and notifications

| File | Purpose |
|------|---------|
| `layout.py` | Loads an area's sections with their entities and widgets; shared by the REST area endpoints and WS `get_area`. |
| `tiles.py` | Tile settings per area entity. |
| `quick_access.py` | Quick access level per entity (widgets, controls, Siri). |
| `room_climate.py` | Which entities give an area's room temperature and humidity. |
| `scan_tags.py` | NFC and QR tags registered in the hub. |
| `notifications.py` | Per-client notifications in `client_notifications`: lifecycle (`pending`, `delivered`, `dismissed`), the shared push shape, clean-up. |
| `event_log.py` | Persistent log tab: activity, changes and alarms in `event_log` with retention, reading and filters. |
| `event_notify.py` | Notifications at events: rules, thresholds, quiet hours, bundling, HA `notify.*` and device targets. |

### Storage, security and runtime

| File | Purpose |
|------|---------|
| `db_migrations.py` | Schema SQL and the numbered migration registry (`@migration(N)`, `add_missing_columns`). |
| `db_snapshots.py` | `VACUUM INTO` snapshots in `/data/backups` (daily, before migrations, for HA backups), verification, restore candidates. |
| `atomic_io.py` | Crash-safe file writes: temp file, fsync, `os.replace`, directory fsync. |
| `image_store.py` | Crash-safe image replacement and start-up clean-up of leftover image files. |
| `crypto.py` | Fernet encryption of HA tokens with a key file (atomic, backup copy, start-up check); `derived_key` for other purposes (HKDF with a fixed label). |
| `tls.py` | Self-signed TLS certificate for port 8100, SHA-256 fingerprint for pinning, TLS 1.3 floor. |
| `csp.py` | Content-Security-Policy of the HTML pages. |
| `audit_log.py` | Security audit logger (`hub.security`) and the in-memory audit buffer of `GET /api/admin/audit`. |
| `log_redaction.py` | Logging setup that masks secrets. |
| `mdns.py` | mDNS/Bonjour announcement (`_halumar._tcp.local.`) with the hub's instance id. |
| `static_compress.py` | On-the-fly gzip of the admin UI's immutable assets. |
| `tasks.py` | Background task registry: every task has an owner and ends on shutdown. |

## Patterns

- Dependency injection: most functions take `db` (`Database`) and/or `ha_client` (`HAClient`) as arguments.
- The Supervisor proxy is the only way to HA. All HA REST, WebSocket and Supervisor traffic goes through `http://supervisor` / `ws://supervisor` with the Supervisor token. Device tokens come from the integration (`device_provisioning`); the hub never mints HA tokens.
- HA entities as a remote-read path: `ha_sensors` projects the layout and notifications into HA sensors, so apps can read them over Home Assistant when the hub's own WebSocket is unreachable. Tokens are never put into sensor attributes.
- Security at rest and in logs: tokens are Fernet-encrypted with an on-disk-only key (`crypto`); remote PINs are PBKDF2-hashed (`pin_hash`); security events go to an isolated, CR/LF-sanitized audit logger (`audit_log`); `log_redaction` masks secrets in every log line.
- Lazy imports inside functions break circular dependencies (for example `ha_sensors` and `api.ws`).
