# `api/` HTTP and WebSocket API surface

This package contains the FastAPI routers and the WebSocket handler that make up the external interface of the hub. Each module exposes an `APIRouter` named `router` that `main.py` mounts under the `/api` prefix.

Routers are thin: they validate input via Pydantic schemas (`models.schemas`), read and write the SQLite `Database` (`request.app.state.db`), call Home Assistant through `request.app.state.ha_client`, mirror state into Home Assistant via `services.ha_sensors`, and push live updates by calling `api.ws.broadcast`. The full route list is in the OpenAPI schema (`/api/docs`); the WebSocket messages are described in the contract.

## Overview

- The hub owns all persistent Halumar state and serves REST plus WebSocket on port 8099 (Home Assistant Ingress and the integration) and, when `tls_enabled`, on the TLS port 8100, which the apps use with a pinned certificate.
- The hub never switches entities with its own rights. It ships layout, per-device Home Assistant tokens, config and notifications. A device with a key calls Home Assistant directly with its own token; a device on the hub path switches through the hub, which has the integration carry out each command with the device's own read-only user.
- The hub path (`profiles.py`, `ws/client.py`, `services/hub_control.py`, `services/kids_mode.py`): a device with parental controls or on the rule `control_path` `hub` ("Hub only"; never a watch or a TV) reads and switches through the hub where it is reachable (WS `control`, REST `POST /api/client/control` with its client secret; besides `POST /api/client/leave` and `POST /api/client/location` the only REST paths that accept it). The hub checks the device, its access and its scope (for children also the kids rules) and calls the integration action `<ns>.control` with the device's own read-only Home Assistant user (logbook unchanged; a grant only for children); the integration checks the call again. Without remote access such a device holds no Home Assistant key; with it a read-only one for use away from home. Live states via `state_subscribe` (`services/state_relay.py`), sensor history `history_get` and weather forecast `forecast_get` (`services/hub_read.py`), camera stills and media artwork `camera_image_get` / `media_image_get` (`services/hub_images.py`), all for visible entities and limited per device; devices off the hub path get `not_hub_controlled`.
- One Home Assistant user per device. The companion integration creates each device's Home Assistant user and token and delivers the token to `POST /api/integration/client-token`; the hub never mints or revokes Home Assistant tokens (`services/device_provisioning.py`). Until the token arrives a device gets `ha_token` null (guests included, fail closed).

## Files

| File | Purpose |
|------|---------|
| `__init__.py` | Empty package marker. |
| `auth.py` | `AuthMiddleware` (per-request auth), session/token/CSRF logic, `require_admin`/`require_integration`, login/logout/session and token management (admin API token, integration token). |
| `authz.py` | Read scope of a paired client (which areas it may read) and `sanitize_state` for every state sent to it (no tokens, no picture links of media players and cameras but the marker `hub_picture_id`, no GPS of people and trackers). |
| `areas.py` | CRUD for areas, sections, entities and widgets; area copy; layout and area order; per-area images. |
| `room_climate.py` | `GET/PUT /areas/{id}/room-climate`: which sensors give an area's temperature and humidity. |
| `tiles.py` | Tile capabilities and per-entity tile suggestions for the admin UI. |
| `quick_access.py` | Quick access level per entity (widgets, controls, Siri). |
| `entities.py` | Read-only browsing of Home Assistant entities, areas, devices and domains (admin-only). |
| `sync.py` | Import Home Assistant areas (and optionally their entities) into hub areas. |
| `export.py` | Export all or one area as JSON and import areas back, with validation and limits. |
| `pairing/` | Package: pairing PIN (`pin.py`), paired clients and the tombstone flow (`clients.py`), guest invitations, tag catalog and guest extension (`guests.py`), device setup sessions started from a paired device (`onboarding.py`), global settings (`settings.py`), per-device Home Assistant token rotation (`tokens.py`). `__init__.py` registers all routes in one ordered table. |
| `profiles.py` | Profile model: rule catalog and suggestions, profiles, default profiles, device profile, adjustments, own area, remote PIN, bulk changes, profile audit, kids rule test, kids status and release; device REST control and location. |
| `family.py` | Family manager: children and notify targets of a device, overview, house settings; the integration's family and answer endpoints. |
| `access_requests.py` | Early access requests: list, answer, end a running early access. |
| `home_networks.py` | `GET/PUT /settings/home-networks`: further networks that count as at home. |
| `scan_tags.py` | NFC and QR tags of the hub. |
| `panel.py` | Panel card of a device in the admin UI. |
| `notifications.py` | Admin CRUD for client notifications (list, send, delete, clear) and the delivery core shared with the integration. |
| `log.py` | Log tab: activity, changes and alarms, export, summary, read marks, retention, notifications at events; the integration's `POST /integration/events`. |
| `integration.py` | Surface of the companion Home Assistant integration (integration token only): status snapshot, enforcement rules, kids check, access switch, device-token delivery, pairing QR camera, start/cancel pairing, device commands, notifications. |
| `admin.py` | App logs, restart, integration telemetry, TLS certificate, setup assistant progress, audit buffer (admin-only). |
| `ws/` | Package: the `/ws` real-time channel (see below). |
| `websocket.py` | Alias of `api.ws` (same module object). |

## Modules

### Authentication (`auth.py`)

`AuthMiddleware.dispatch` runs on every request and sets `request.state.auth_type` to one of `ingress`, `admin`, `session`, `client`, `integration`, or `None`. Order: validated Ingress (`_is_trusted_ingress`, which requires the `X-Ingress-Path` header and a source IP in `TRUSTED_INGRESS_NETWORKS`), the `PUBLIC_PATHS`/`PUBLIC_PREFIXES` allowlist (health, head icons, login, static assets; the API docs need admin), and a genuine ASGI websocket scope. On `CLIENT_SECRET_PATHS` (`/api/client/control`, `/leave`, `/location`) only a paired-client secret counts (`auth_type` `client`). Otherwise it requires a Bearer token (validated by `_validate_token` against the hashed admin API token, the hashed integration token or in-memory sessions; a paired-client secret is not accepted) or a session cookie. Cookie sessions additionally enforce CSRF (`CSRF_HEADER_NAME` versus the session `csrf_token`) on `CSRF_METHODS`.

The integration token is path-scoped by the middleware (valid only on `/api/integration/*`, 403 elsewhere), and every authenticated integration request, plus every 401 on an integration path, feeds the in-memory telemetry in `services.integration_status` (the `X-Hub-Integration-Version` header is parsed).

- `require_admin(request)` raises 403 unless `auth_type` is in `_ADMIN_AUTH_TYPES` (`ingress`, `admin`, `session`), so paired `client` callers are rejected. `require_integration(request)` accepts exactly `auth_type == "integration"`.
- Sessions are in-memory (`_sessions`), User-Agent bound, capped per user and globally.
- `hash_secret` / `verify_secret` (SHA-256 plus `hmac.compare_digest`) are the shared secret-storage primitives; rate limiting protects `/auth/login` and pairing.
- `rotate_integration_token(db)` mints the 64-hex integration token (hash-only storage); it is called by the regenerate endpoint and by `services.discovery` on every start.

### Areas (`areas.py`)

The sections-based layout model over the tables `areas`, `area_sections`, `area_entities`, `area_widgets`, `area_tags` and `area_excluded_entities`. `services.layout.load_sections_with_items` assembles each section with its entities and widgets (shared with WS `get_area`). Dynamic `UPDATE` statements are guarded by column whitelists (`AREA_UPDATE_COLUMNS`, `AREA_ENTITY_UPDATE_COLUMNS`, `WIDGET_UPDATE_COLUMNS`) and `VALID_WIDGET_TYPES`. Removing an entity records it in `area_excluded_entities` so Home Assistant sync will not re-add it. Mutations call `require_admin`, then `broadcast(...)` and a sensor sync.

### Home Assistant browsing and sync (`entities.py`, `sync.py`)

`entities.py` lists Home Assistant states, areas, devices and domains for the admin UI. `sync.py`'s `sync_from_ha` walks Home Assistant areas and upserts hub areas keyed by `ha_area_id`; with `include_entities` it adds each area's entities (skipping disabled and hidden ones, at most `MAX_ITEMS_PER_SECTION` per section and `MAX_SECTIONS_PER_AREA` sections) and respects `area_excluded_entities`. `sync_area_entities_from_ha` does the same for one linked area.

### Export and import (`export.py`)

`export_all_areas` / `export_area` serialize areas, sections, entities, widgets, tags and excluded entities (`format_version=2`). `import_areas` reverses this with overwrite support and the bounds of the REST writers, and returns `{imported, skipped, errors}`.

### Pairing, clients and settings (`pairing/`)

PINs (8 digits, `XXXX-XXXX`) live in the module-level `_active_pins` (one active at a time), exposed via `get_active_pin`/`set_active_pin`/`match_pin`/`remove_pin`/`clear_active_pins` (used by `integration.py` and `ws/handshake.py`). Apps exchange the PIN for a permanent secret (stored as a SHA-256 hash in `paired_clients`) only over the WebSocket (`ws/handshake._handle_pair`); every wrong attempt counts against the active PIN (`register_pin_failure`).

Client management: list, read, update, access switch, revoke (soft), delete (two-phase tombstone), delete all, kick, dismiss pending, extend a guest, and `client_leave` (the device signs itself off with its secret). The two-phase delete keeps the row (`is_active=0`, `pending_deletion_at` set) so a returning device is told to reset; the `tombstone` DELETE removes the row after the device's reset. Every removal sends `deprovision_client` to the integration after the commit.

Global settings under `/settings/*`: `ha-urls`, `app-url`, `control-mode`, `pairing-via-integration`, `compliance-endpoint`, `admin-direct-access`, `home` (plus home image upload/delete), and `/tags`. Settings changes that reach the apps broadcast `client_config_updated`.

`POST /clients/{id}/rotate-token` (`tokens.py`, admin) asks the integration for a new token of the device's Home Assistant user and waits up to 15 s: 200 with `ha_token_minted_at` when the token arrived, 202 `rotation_requested` while it is still running, 409 when the device has no active token, 503 when the integration is not reachable.

### Notifications (`notifications.py`)

Admin CRUD over `client_notifications` (through `services.notifications`). `deliver_notification_request` (shared with `POST /integration/notify`) stores the notification (one client or `"all"`), resolves `mdi:` icons to SVG, pushes it with `send_to_client`, broadcasts `notification_created` to admins and re-syncs the Home Assistant notification sensors.

### Integration API (`integration.py`)

Every endpoint requires the integration token (`require_integration`). `integration_status` returns one combined snapshot (hub info and `instance_id`, areas with slugs, paired clients with live connection state, access state and Home Assistant user/token status, notification ids and counts, `pairing_active` without the PIN, `provisioning[]`, and `pairing_qr_camera`); with the compliance setting on, each client also carries a signed compliance blob (`services.compliance`).

`integration_pairing_qr` returns a status PNG ("PAIRING" / "STANDBY", with a line why no QR is shown) and `X-Pairing-Pin: omitted`. Only when the app option `pairing_qr_camera` allows it (`services/pairing_qr.py`: `guests` = guest windows only, `all` = every window) and the integration sends `X-Pairing-Qr: accept` does it return the QR of the pairing deep link with `X-Pairing-Pin: included`, `X-Pairing-Kind: guest|device` and `X-Pairing-Window`; never cached. `integration_start_pairing` (403 when pairing via the integration is off; optional profile name), `integration_cancel_pairing` and the area `activate`/`deactivate` endpoints back Home Assistant buttons and switches. `integration_client_token` stores the token of a device's Home Assistant user (idempotent per `refresh_token_id`) and, for a replaced token, answers only after the app fetched its new config. The `clients/{id}/...` commands (`navigate_to_area`, `show_entity`, `clear_notifications`, `refresh`, `kick`, `tags`, `extend`) use the same cores as the admin endpoints.

### Admin (`admin.py`)

`GET /admin/logs` and `POST /admin/restart` go through the Supervisor (`/addons/self/*`, allowed for the default role). `GET /admin/integration-status` returns the integration telemetry (`connected` = last authenticated integration request under 90 s ago, `push_connected`, counters, versions) plus `addon_version` and `ha_version`. `GET/PUT /admin/setup-assistant` stores the progress of the setup assistant. `GET /admin/audit` returns the newest entries of the in-memory audit buffer (`services.audit_log`). `/settings/tls` reports and regenerates the TLS certificate.

### WebSocket (`ws/`)

| Module | Purpose |
|--------|---------|
| `handshake.py` | Endpoint `/ws`, connection slots (per-IP unauthenticated limit, global caps), 30 s unauthenticated timeout, 64 KB message cap, `pair`/`auth`/`onboard_start` before authentication, message dispatch, guest expiry loop. |
| `client.py` | Messages of authenticated apps and admin sessions: `subscribe`, `get_areas`/`get_area` (scope from `api/authz.py`), `get_client_config`, notifications (`get_notifications`, ack, dismiss), `client_status`, `client_reset_ack`, `client_leave`, `control`, `state_subscribe`, `history_get`/`forecast_get`, `location_report`, `tag_resolve`, `watch_access_get`, panel replies, access requests, family and onboarding messages, with their data helpers (`_get_areas`, `_get_area_detail`, `_get_client_config`). |
| `onboarding.py` | Setting up a device from a paired device (lookup, confirm, decline). |
| `guest_invites.py` | Guest invitations created by member devices. |
| `family.py` | Family manager messages. |
| `admin.py` | The integration's read-only push session (only `ping`/`pong` inbound). |
| `broadcast.py` | Audiences (`EventAudience.ALL`, `ADMIN`), `broadcast`, `send_to_client`, delivery of a rotated config, `client_removed` signal, ping loop. |
| `registry.py` | Connected sockets (`_clients`), counts, bounded send queue per connection, `disconnect_client`. |

`api.ws` re-exports these names; `broadcast`, `send_to_client`, `disconnect_client`, `_is_ingress`, `_auth_disabled` and the `*_GRACE_SECONDS` timings are looked up through the package at call time, so replacing `api.ws.<name>` (as tests do) takes effect everywhere. There is no general Home Assistant action proxy: devices with a key switch directly with their own token, devices on the hub path only through `control` (`services/hub_control.py`).

## Data flow and patterns

- Auth gating: `AuthMiddleware` classifies the request, then endpoints call `require_admin` for admin-only operations; paired clients have WebSocket access plus the few `/api/client/*` paths.
- Write-then-fan-out: a mutating REST endpoint writes to the `Database`, commits, then broadcasts a typed event over WebSocket and re-syncs Home Assistant via `services.ha_sensors` (best effort; sensor-sync failures are logged, never fail the request).
- REST routers import the package once (`from api import ws as api_ws`) and call `api_ws.broadcast(...)`/`api_ws.send_to_client(...)`; `EventAudience` keeps admin-only events off client sockets.
- Secrets and tokens: client secrets, the admin API token and the integration token are stored only as SHA-256 hashes and compared in constant time; Home Assistant tokens are Fernet-encrypted (`services.crypto`) with an on-disk-only key.
- Injection defense: dynamic `SET`/column writes go through explicit whitelists; uploaded images use random filenames and path validation under `/data/images`, served as `/images/{name}`.
