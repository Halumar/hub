# Halumar architecture

This document describes how the Halumar Hub, a Home Assistant app (add-on), is built and how its parts
fit together. It is derived from the code in this repository and is meant for contributors
and integrators. For end-user installation and the API reference, see
[`../README.md`](../README.md), [`DOCS.md`](DOCS.md) (users) and [`../docs/DEVELOPER.md`](../docs/DEVELOPER.md) (API).

## 1. System overview

The hub maintains its own database of areas with extended
layout metadata (sections, entity placements, widgets, images, tags) that is independent of
Home Assistant's built-in area registry. Data flows one way, from Home Assistant into
Halumar, and the same backend serves three audiences:

- the admin web UI (Home Assistant sidebar via Ingress, or external login),
- the companion Home Assistant integration (real Home Assistant devices and entities), and
- paired clients (the apps on iPhone, iPad, Apple TV, Apple Watch, Android phone and tablet
  (wall panel) and Android TV) over REST and WebSocket.

The hub sits between the companion integration and the apps. It
owns all persistent Halumar state and never switches entities with its own rights. It ships
layout, tokens, config and notifications. A device with a key calls Home Assistant directly with
its own token; a device on Hub only switches through the hub, which has the integration carry
out each command with the device's own read-only Home Assistant user (section 3).

The runtime is a single Python process:

- Backend: Python 3.12, FastAPI on Uvicorn, SQLite via `aiosqlite`, `httpx` for Home Assistant REST, `websockets` for Home Assistant WebSocket, `cryptography` (Fernet) for token encryption, `zeroconf` (AsyncZeroconf) for mDNS.
- Frontend: Svelte 5 and SvelteKit (static adapter) and TailwindCSS v4, pre-built into static files and served by the same FastAPI app.
- Packaging: Alpine-based Home Assistant app base image, multi-arch (`amd64`, `aarch64`), published to GHCR.

Source of truth for version and build: `rootfs/opt/hub/version.py`
(`VERSION = 2026.12.0`, CalVer; `BUILD = 2026-11-26`), mirrored in `config.yaml` and the git
tag. Hub and integration of the same version belong together.

### Component map

```
halumar/                          # the app (this directory)
|-- config.yaml                    # app manifest (ingress, ports, perms, discovery, image:)
|-- Dockerfile                     # base image (FROM), labels, Python deps, rootfs (+ prebuilt static)
|-- apparmor.txt                   # AppArmor profile (S6 top level, child profile "hub" for Python)
|-- README.md / DOCS.md / CHANGELOG.md  # store intro, user docs (Documentation tab), changes
|-- translations/{en,de}.yaml      # option and port texts of the Configuration tab
`-- rootfs/opt/hub/           # application root (copied to / in the image)
    |-- main.py                    # FastAPI app: lifespan, middleware, routing, static serving, dual-server TLS
    |-- version.py                 # single source of truth for VERSION / BUILD
    |-- requirements.txt           # runtime Python deps, all pinned incl. transitive (wheels only)
    |-- api/                       # FastAPI routers + WebSocket handler (external interface)
    |   |-- auth.py                # AuthMiddleware, five auth types, sessions, CSRF, token mgmt
    |   |-- areas.py               # CRUD: areas/sections/entities/widgets, layout, images
    |   |-- entities.py            # read-only HA entity/area/device browsing (admin)
    |   |-- sync.py                # one-way import of HA areas/entities
    |   |-- export.py              # JSON export/import of the layout DB
    |   |-- pairing/               # PINs, paired clients, guests, onboarding, settings, tokens
    |   |-- notifications.py       # admin CRUD for client notifications
    |   |-- integration.py         # whole /api/integration/* surface (status, commands, QR, client-token)
    |   |-- profiles.py            # profile model: profiles, suggestions, adjustments, kids actions
    |   |-- admin.py               # app logs, self-restart, integration status, TLS admin
    |   |-- ws/                    # /api/ws: handshake, client, admin, broadcast, registry
    |   `-- websocket.py           # alias of api.ws (existing imports)
    |-- services/                  # cross-cutting backend services
    |   |-- ha_client.py           # HAClient: REST to HA Core + Supervisor, reads via ha_ws
    |   |-- ha_ws.py               # persistent HA WebSocket: reconnect, events, state mirror, registry cache
    |   |-- tasks.py               # background task registry: supervised loops, bounded shutdown
    |   |-- device_provisioning.py # one HA user per device: requests to the integration
    |   |-- ha_sensors.py          # project layout into HA sensors + activation switches
    |   |-- compliance.py          # signed, PII-free per-client compliance status (HMAC)
    |   |-- pairing_link.py        # canonical halumar:// deep link + base URL resolution
    |   |-- crypto.py              # Fernet encryption of HA tokens (on-disk keyfile)
    |   |-- tls.py                 # self-signed leaf cert lifecycle + SHA-256 fingerprint
    |   |-- mdns.py                # _halumar._tcp.local. service announcement
    |   |-- discovery.py           # Supervisor discovery announce (new integration token after success)
    |   |-- profiles/              # profile model: catalog, resolver, store, changes, migrations
    |   |-- layout.py              # loads an area's sections with entities + widgets (REST + WS)
    |   |-- integration_status.py  # in-memory integration connection observability
    |   |-- notifications.py       # client_notifications table lifecycle + push serializer
    |   |-- audit_log.py           # security audit trail (hub.security logger)
    |   `-- pin_hash.py            # PIN hashing helper
    |-- models/                    # data layer
    |   |-- database.py            # async SQLite wrapper, transactions, start-up recovery
    |   `-- schemas.py             # Pydantic request/response + integration command models
    |-- data/                      # lookup tables (mdi_paths.py, notification_icons.py) and contract data copies
    `-- static/                    # PREBUILT frontend (output of the SvelteKit build)

frontend/                         # SvelteKit admin web UI (source)
|-- svelte.config.js              # adapter-static -> ../halumar/rootfs/opt/hub/static
|-- src/lib/                      # api.js (REST), ws.js (WebSocket), stores, components, data
|-- src/routes/                   # pages: dashboard, areas, devices, profiles, family, guests,
|                                 # notifications, log, settings, setup
`-- e2e/                          # Playwright smoke tests (mocked API/WS)

halumar/tests/                   # pytest suite (auth, crypto, db, ws, schemas, compliance, ...)
.github/workflows/                # ci.yml (pytest), frontend.yml (build+e2e), build.yaml (image)
```

## 2. Request and authentication model

All authentication is enforced centrally by `AuthMiddleware` (`api/auth.py`), which runs on
every request and sets `request.state.auth_type` to one of five values:

| `auth_type` | Source | Granted to admin endpoints? |
|---|---|---|
| `ingress` | Validated Home Assistant Ingress request | yes |
| `admin` | Bearer = admin API token (SHA-256 hash match) | yes |
| `session` | Valid cookie session (web UI login, only while direct admin access is on) | yes |
| `client` | Bearer = a paired client's secret (hash match), only on `CLIENT_SECRET_PATHS` | no (`POST /api/client/control`, `/api/client/leave`, `/api/client/location`; the WebSocket authenticates inside its protocol) |
| `integration` | Bearer = the dedicated integration token | only `/api/integration/*` |
| `None` | unauthenticated | no |

The middleware decides per request in this order:

1. Ingress: `_is_trusted_ingress` requires both the `X-Ingress-Path` header (added by the Home Assistant Supervisor proxy) and a source IP (`request.client.host`, never `X-Forwarded-For`) equal to the Supervisor Ingress proxy `172.30.32.2` (`services/ingress.py`, shared with the WebSocket check `_is_ingress`). Other apps and host processes are not trusted; loopback and the rest of the hassio network only with the dev flag `HUB_DEV`. A spoofed header is ignored, also by `IngressMiddleware` (no HTML rewrite).
2. Public paths: `PUBLIC_PATHS` (`/api/health`, the head icons and the web manifest in `HEAD_ASSET_PATHS`) and `PUBLIC_PREFIXES` (`/api/auth/login|logout|session`, `/_app/`, `/images/`, `/favicon`). These carry their own auth (credentials) or are static. The API docs (`/api/docs`, `/api/redoc`, `/api/openapi.json`) need admin authentication.
3. WebSocket: only a genuine ASGI websocket scope is passed through (an `Upgrade` header on a plain HTTP request is not); `/api/ws` authenticates inside the protocol (see section 3).
4. `CLIENT_SECRET_PATHS` (`/api/client/control`, `/api/client/leave`, `/api/client/location`): only a Bearer paired-client secret, checked against the hashes; anything else is 401.
5. Otherwise: require a Bearer token (`_validate_token` checks, in order, the hashed admin API token, the hashed integration token, then in-memory sessions; a paired-client secret is not accepted) or a session cookie. An admin token or session that does not arrive over TLS (:8100) is refused with 403. An `integration` token is scoped by the middleware to `/api/integration/*` and counts only from this host.

### Sessions, RBAC, CSRF

- Direct admin access (setting `admin_direct_access`, cached in `api/auth.py`) is off by default: then `/api/auth/login` answers 403 `admin_ingress_only` and `_validate_session` accepts no session. Only `require_ingress(request)` callers may switch it (`PUT /api/settings/admin-direct-access`); switching off clears `_sessions` and closes the session sockets.
- Sessions are in-memory (`_sessions`), created on `/api/auth/login` after the credentials are validated against the Home Assistant Supervisor Auth API. They are bound to the User-Agent, expire after `SESSION_LIFETIME = 86400s` (24 hours), and are capped per-user and globally. Lookups use constant-time comparison.
- Cookies: `hub_session` (HttpOnly session token) and `hub_csrf` (readable by JS so the SPA can echo it back).
- CSRF: for cookie-authenticated requests with a state-changing method (`CSRF_METHODS` = POST, PUT, PATCH, DELETE), the `x-csrf-token` header must match the session's stored `csrf_token` (constant-time). Bearer-token clients are exempt (no ambient cookie credentials).
- RBAC: `require_admin(request)` is the authorization gate used by almost every endpoint. It allows only `_ADMIN_AUTH_TYPES = {ingress, admin, session}` and rejects paired `client` callers with 403. Paired clients are therefore limited to the WebSocket protocol and the three `CLIENT_SECRET_PATHS`. `/api/integration/*` uses `require_integration(request)` instead: those endpoints accept exactly the dedicated integration token.
- Rate limiting: login allows 5 attempts per IP in 60 s (`_rate_limit_check`, LRU-bounded) and blocks an account after 5 failures from one IP, or 30 from at least 5 IPs, in 15 minutes. WebSocket `pair` allows 5 attempts per connection and 10 failed attempts per IP in 10 minutes (`api/pairing/pin.py`); see docs/DEVELOPER.md, Rate limiting. The client IP comes from the TCP connection, not headers.

### Secrets at rest

- The admin API token (`secrets.token_hex(32)`) is stored only as a SHA-256 hash (`api_token_hash`) and never logged; regenerate it in **Settings > Advanced** to see one.
- The integration token (64-hex, `integration_token_hash`) is rotated on every hub start under the Supervisor and handed to the companion integration via the Supervisor discovery API (`services/discovery.py`); without discovery (the integration added by hand, or a failed announce) an administrator generates it in the web UI. Only the hash is stored, and the middleware scopes it to `/api/integration/*`.
- Paired-client secrets are stored as SHA-256 hashes (`paired_clients.secret_hash`).
- Home Assistant long-lived tokens handed to clients are Fernet-encrypted (`services/crypto.py`) with a key kept in a separate `0600` keyfile (`/data/.hub_keyfile`), never in the database. The hub holds no other Home Assistant credential than the Supervisor token.

### One Home Assistant user per device

Every paired device gets its own Home Assistant user with exactly one long-lived token
(device, Home Assistant user and token are 1:1:1). The companion integration creates the user and
token inside Home Assistant; the hub never mints, rotates or revokes Home Assistant tokens and holds no owner
token. Contract: `contracts/docs/per-device-users.md`.

- Requests to the integration (`services/device_provisioning.py`) go over the integration's
  WebSocket push session only (`send_to_integration`): `provision_client` (pairing,
  admin rotation, unreadable token), `update_client_identity` (name, owner label, remote
  access, guest expiry of an active device) and `deprovision_client` (revoke, delete, kick,
  clear all, guest expiry; always after the removal is committed and the app was closed or
  told `client_removed`). A lost message is harmless: `GET /api/integration/status`
  lists outstanding requests in `provisioning[]`, and the device list is the desired state
  the integration reconciles against. While the push session is up a retry loop repeats
  outstanding requests (60 s, doubling to 15 min). Open rotations and the backoff live in
  the table `token_requests` (one row per device, removed with the delivered token, with
  the device, or when a rotation got no token within 1 h), so a hub restart neither loses
  a rotation nor resets the backoff. `POST /api/clients/{id}/rotate-token` stores
  the rotation first; without the integration's push session it answers 202 `pending` at
  once and the rotation runs once the integration is back.
- Deleted devices keep their Home Assistant user until the app confirmed its reset (`client_reset_ack`,
  also when an offline device comes back and the hub replays `client_removed`) or a grace
  period ran out: the reset-ack window when the app was connected, `DELETE_HOLD_SECONDS`
  (24 h) when it was offline. Meanwhile the status snapshot lists the device in
  `ha_user_holds` and the integration does not remove the user; the release
  (`paired_clients.ha_user_released_at`) sends `deprovision_client`. Revoke removes the
  user at once (lost or stolen device).
- `POST /api/integration/client-token` stores the delivered token encrypted and sets
  `ha_token_status` to `active` (idempotent per `refresh_token_id`). The app learns about it
  through `client_config_updated`; for a replaced token the answer waits until the app
  fetched its config, because the integration removes the old token right after it.
- `paired_clients`: `ha_user_id`, `owner_label`, `access` `member` | `guest` (guests always
  with `expires_at`, CHECK), `profile_id` (NOT NULL), `own_area_id`, `ha_token_status`
  `pending` | `active`, `ha_token_pending_since`. Remote access is the effective profile rule
  `remote_access`, reported to the integration as `remote_allowed`.
  A new device is `pending` until its token arrives; it gets `ha_token: null` meanwhile.
- Keys on the hub path: a device holds its key only when `resolver.holds_key` is true
  (Checked or Direct, or Hub only with remote access); otherwise `client_config.ha_token`,
  `ha_instance_id` and `ha_enforcement` are null and the hub keeps the token for its own calls
  of `<ns>.control`. When a device stops holding a key (control path to Hub only, remote access
  lost, the end of a temporary adjustment, the migration) or its access closes, the hub asks
  for a rotation (`provision_client` with `token_action rotate`). `paired_clients.key_lost_at`
  records the loss in the write's transaction, so `secure_keyless_tokens` repeats the rotation
  at start and in the retry loop until a new token was accepted. The old key keeps working
  until the hub has received the replacement and the integration has removed the old token,
  so while the integration cannot be reached it can keep working for a long time. The hub
  keeps retrying (backoff up to 15 min, a new rotation after 1 h without an answer), the device
  counts as holding the key (`has_ha_token` true), and the log entry `ha_key_withdrawn` is
  written when the hub accepts the new token. The end of a temporary adjustment is compared
  with the state just before that end, also when the hub was down at the end time (timed ends
  are applied by the hub: while it is stopped, a key whose temporary value has ended keeps
  working until it starts again), and every
  profile write first processes the adjustments whose end has passed. A watch holds a key only
  while its phone's effective rule `watch` is on; a phone on Hub only without remote access
  carries none (its watch is removed with reason `hub_only`). At every start the hub removes
  a watch its phone's rules no longer allow (`watch_access.remove_disallowed`), so none
  survives an upgrade or a missed expiry.
- Fail-closed guests: there is no shared guest token. A guest's user is `local_only` unless
  its profile gives it remote access, its token is bounded to the guest expiry by the
  integration, and the user is removed when the guest expires.
- The Home Assistant version shown in the admin UI comes from `GET /api/config` through the
  Supervisor proxy (`GET /api/admin/integration-status`, `ha_version`).

### Middleware stack (outer to inner, as executed)

`main.py` registers middleware so that the last added runs first:

```
request -> BodySizeLimitMiddleware   (413/408 for bodies over their limit or too slow, chunked too)
        -> StartupErrorMiddleware    (503 with reason while the hub is in an error state)
        -> DynamicCORSMiddleware     (dynamic allowed origins from app_url + HA URLs)
        -> AuthMiddleware            (classify auth_type / reject)
        -> SecurityHeadersMiddleware (CSP, X-Frame-Options: SAMEORIGIN)
        -> IngressMiddleware         (rewrite HTML asset paths for the Ingress subpath)
        -> route handlers
```

`IngressMiddleware` rewrites served HTML when `X-Ingress-Path` is present: it injects
`window.__INGRESS_PATH`, fixes absolute `/_app/` asset URLs, and patches SvelteKit's `base`
so the SPA works under `/api/hassio_ingress/<token>/`.

## 3. Dual-channel design (REST + WebSocket)

Every feature is reachable through two complementary channels exposed under `/api`:

- REST (request/response): CRUD for areas/sections/entities/widgets, settings, pairing administration, export/import, notifications, image upload, admin/observability. Used primarily by the admin web UI and the companion integration. Interactive docs at `/api/docs` (Swagger) and `/api/redoc`.
- WebSocket (`/api/ws`): the primary channel for paired clients and the broadcast bus for realtime updates. The protocol (`api/ws/`) handles `pair` and `auth` for unauthenticated sockets, then `subscribe`, `get_areas`/`get_area`, `get_client_config`, notification ack/dismiss, `get_notifications` (the single WS notification content channel; remotely the Home Assistant notification sensor carries the same push-shaped items), `client_status`, `client_reset_ack` (the authenticated wipe-ack that clears a delete tombstone) and `client_leave` (the device signs itself off before a reset). It enforces a 30s unauthenticated timeout, a 64 KB message cap, and separate auth/unauth connection limits.

Entity control is not proxied with the hub's own rights. A device reaches Home Assistant on one
of three paths, set by the profile rule `control_path` ("Home Assistant access"):

| Path | Key on the device | Reads | Switches |
|---|---|---|---|
| `direct` | full key (`system-users`) | Home Assistant | directly |
| `checked` | read-only key (`system-read-only`) | Home Assistant | `<ns>.control`, checked by the integration |
| `hub` ("Hub only") | none; read-only with remote access | the hub | the hub, which calls `<ns>.control` with the device's token |

A device with parental controls is on the hub path as well, also on Checked. A TV or a watch
never is (`hub` resolves to `checked` there). On the hub path (`services/hub_control.py`) the hub
checks that the device is paired, active, not a watch and on the hub path, that its access is
open and that the entity is in its scope; for children it applies the kids rules
(`services/kids_mode.py`) and passes a single-use grant. Then it calls `<ns>.control` with the
device's own token, never with the Supervisor token; the integration checks the call again
(allowlist, scope, run releases, group members, access hours, rate limit). Reads on the hub path
(`state_subscribe`, `history_get`, `forecast_get`, `camera_image_get`, `media_image_get`) are
served with the hub's own access, only for the device's scope and limited per device. The rule
snapshot of a hub-path device carries `checked`; its Home Assistant user stays read-only.
States sent to paired devices (`get_area`, the `state_subscribe` snapshot, `entity_state`) carry
no picture link of media players and cameras (those links hold the entity's access token):
`api/authz.py` replaces it with `attributes.hub_picture_id`, an HMAC over the link without its
token, keyed with a key derived from the hub's key file (`crypto.derived_key`, purpose
`hub picture marker v1`). The relay sends a change only when what the device sees changed, so a
renewal of only the token goes nowhere; the apps ask `media_image_get`/`camera_image_get` when
the marker appears or changes.

Write-then-fan-out is the core pattern: a mutating REST endpoint writes to SQLite, commits,
then broadcasts a typed event over WebSocket and best-effort re-syncs Home Assistant sensors. REST
routers call `broadcast` and `send_to_client` on the `api.ws` package (looked up at call
time). `EventAudience` filters events: `ALL` reaches every authenticated client,
`ADMIN` reaches only Ingress/dev admin sockets, so admin-only events never leak to paired
clients.

```
            +------------ admin web UI (Ingress / login) -----------+
            |                                                        |
  REST  ----+  POST /api/areas  --> writes SQLite --> commit        |
            |                          |                            |
            |                          +-> broadcast(area_updated) --+--> WS clients
            |                          `-> sync HA sensors (best effort)
            |                                                        |
  WS    ----+  /api/ws  <-- realtime data / notifications / events -+
            |  (paired clients: data; Hub only also control, else direct to HA)
            +--------------------------------------------------------+
```

## 4. TLS dual server and client trust

When the `tls_enabled` option is on (the default), `main.py` serves the same
FastAPI app on two ports at once:

- Port 8099: plain HTTP/WS, used by Home Assistant Ingress (internal Docker network) and the integration on this system.
- Port 8100: TLS 1.3 https/wss, used by the apps. They connect only here and pin the certificate; even the first contact before pairing reads the certificate over TLS, and an address typed with port 8099 is taken as 8100.

`services/tls.py` maintains a self-signed leaf certificate and exposes its SHA-256
fingerprint. Apps pin that fingerprint on a trust-on-first-use basis (it is not CA-validated).
The fingerprint is distributed through three channels so a client can learn and verify it
before connecting: `GET /api/health` (`tls_fingerprint`), the mDNS TXT record (`tls`,
`tls_port`, `fp`), and the canonical pairing deep link (`services/pairing_link.py`,
`halumar://pair?url=<enc>&pin=XXXX-XXXX[&tls_port&fp]`). Regenerating the certificate
(`POST /api/settings/tls/regenerate`) changes the fingerprint, which forces pinned clients
to re-pair and requires a restart.

The Home Assistant leg is different: devices with a key reach Home Assistant with their own token and use
TOFU there too, but CA-valid Home Assistant Cloud certificates validate normally and are never pinned.

### Compliance channel and delete tombstone

`services/compliance.py` derives a small, PII-free compliance status per paired client
(`need_token_update`, `deleted`, or a neutral unsigned status) and signs the meaningful ones
with HMAC-SHA256. The signing key is derived from the client's own secret hash
(`HMAC(key=digest32, "hub/compliance/v1")`), so only that client can verify its own
status. Every other case returns an identical neutral no-oracle body. A locked-out app can
therefore learn why (need a fresh token, or it was deleted) without any bearer, and it acts
on `need_token_update` or `deleted` only after verifying the HMAC against its own pairing
secret.

Client deletion is a two-phase tombstone: `DELETE /api/pairing/clients/{id}/permanent` marks the row
pending-deletion and persists a signed `deleted` status until the device confirms with an
authenticated LAN wipe-ack (`client_reset_ack` over `/api/ws`), which the hub then hard
deletes (`DELETE /api/pairing/clients/{id}/tombstone`). `DELETE /api/pairing/clients/{id}` revokes a
device at once (lost or stolen device). Guests are hard-deleted at expiry.

## 5. SQLite data model

The data layer is `models/database.py`, a single async `aiosqlite` connection with
`journal_mode=WAL`, `foreign_keys=ON`, and a 5s busy timeout. The on-disk DB lives at
`/data/hub.db` (`HUB_DB_PATH` overrides it for dev).

Schema (`services/db_migrations.py`, baseline v1 in `PRAGMA user_version`, "sections-based layout model"):

```
areas --+--< area_sections --+--< area_entities
        |                     `--< area_widgets        (config stored as JSON text)
        +--< area_tags
        `--< area_excluded_entities                    (suppresses HA re-import)

profiles --< paired_clients --< client_notifications
         |                 `--< device_overrides     (adjustments, optional expires_at)
         `--< profile_revisions
profile_audit    (before/after of every profile, adjustment and default change)
settings         (key/value store: ha_url, app_url, control_mode, home_*,
                  default_profile_<type>, guest_default_hours, ...)
```

Foreign keys cascade from `areas` to its sections/entities/widgets, and from
`paired_clients` to its notifications. The hierarchy mirrors the nested API response shape
(`AreaDetailResponse -> SectionResponse -> entities/widgets`).

Lifecycle and resilience (`initialize()`):

- restored Home Assistant backup (marker from the `backup_pre` hook) -> start from the consistent
  snapshot `backups/hub-ha-backup.db`; database file missing -> newest snapshot;
- fresh DB: `SCHEMA_SQL` and the version in one transaction (no migration steps);
- older version: snapshot (`VACUUM INTO`), then one transaction per `@migration(N)`;
  newer version (downgrade): left unchanged;
- `PRAGMA integrity_check`; damaged -> newest valid snapshot from `/data/backups` (bad file
  kept as `<path>.corrupt-<time>`); without one `DatabaseUnavailableError`: the app starts in
  an error state (health/API 503 with reason and action), files untouched, unless the option
  `reset_corrupt_database` is on;
- current (or just migrated) version: every table and column of `SCHEMA_SQL` must exist. A
  database of a different pre-release build with the same version number stops the start the
  same way (`DatabaseUnavailableError`, reason lists the missing columns); with
  `reset_corrupt_database` it is kept as `<path>.mismatch-<time>` and an empty database is
  created;
- `_ensure_api_token()`. A daily snapshot (newest 7) runs in the background.

Writes that belong together use `async with db.transaction()` (global write lock,
`BEGIN IMMEDIATE`, commit/rollback; nested = savepoint). Plain `commit()` waits for a running
transaction and is a no-op inside one. Handlers do no Home Assistant or WebSocket calls inside a
transaction (fetch from Home Assistant first, broadcast after the commit) and touch files around it:
images are written before and deleted after the commit (`services/image_store.py`).
`scripts/check_db_commits.py --strict` (blocking CI step) fails on transaction control outside
`models/database.py`; its allowlist (`scripts/db_commit_allowlist.txt`) is empty.

Rows are returned as plain dicts (via `aiosqlite.Row`); routes feed them into the matching
Pydantic `*Response` model and validate input with `*Create` and `*Update`. Schema
management is forward-only through numbered migrations.

### Home Assistant sensor projection (remote delivery path)

`services/ha_sensors.py` projects the SQLite layout into Home Assistant entities so remote clients (for
example over Home Assistant Cloud) can read Halumar via Home Assistant's own WebSocket when the hub's port is
unreachable. The hub is LAN-only, so away from home this projection is the client's only
data source:

- `sensor.halumar_config`: overview of all active areas (`is_active = 1` filtered), Home Assistant URLs, version.
- `sensor.halumar_area_<slug>`: per-area detail (active areas only; deactivating an area deletes its sensor, while the `input_boolean` remains as the way back).
- `sensor.halumar_guests`: guest overview; per guest only `client_id`, `is_connected`, `expires_at` (PII-free).
- `sensor.halumar_client_<slug>_<uid8>_notifications`: full notification content (state = active count; `notifications` attribute serialized by the same `notification_to_push_payload` helper that feeds the `get_notifications` WS reply and the `display_notification` push, so the shapes cannot drift). The entity id is delivered to the client via `client_config.notification_sensor`.
- `input_boolean.halumar_area_<slug>`: per-area activation switch; a state set in Home Assistant reaches the hub as a `state_changed` event and updates `areas.is_active`. The hub remembers the state it wrote itself, so the echo of its own write never flips an area back.

Tokens are never written into sensor attributes. Stale sensors are removed via
`DELETE /core/api/states/<entity_id>`; a sensor is only written when its content (without
`updated_at`) changed or Home Assistant lost it, and two syncs of the same sensor never interleave. Every
full sync (start, Home Assistant Core restart) also removes orphaned `halumar_*` states-API entities of
renamed or deleted areas and devices (never an entity in Home Assistant's entity registry).

### Home Assistant connection and background tasks

`services/ha_ws.py` keeps one authenticated WebSocket to Home Assistant Core through the Supervisor proxy.
It reconnects on its own (exponential backoff 1-5 s with jitter), subscribes to
`state_changed`, `homeassistant_started` and the area/device/entity registry events, keeps a
live mirror of all states (`get_states` on connect, then events) and caches the registries
until a registry event or reconnect. `HAClient` reads states and registries from it while it
is up and falls back to REST / a one-shot WebSocket otherwise. On every (re)connect the hub
detects Home Assistant's URLs and checks the sensors: full sync on the first connect, afterwards only if
Home Assistant lost them (Core restart); `homeassistant_started` and a deleted `sensor.halumar_config`
trigger the same check; after a reconnect without Home Assistant restart the area switches are compared
once (changes made while the connection was down). A 30 s reconcile loop is the safety net.

All background work runs through `services/tasks.py`: loops are supervised (a crash is logged
and the loop restarts with backoff), one-shot tasks keep a reference, and shutdown cancels
everything with a time limit per step. The start does not wait for Home Assistant or the Supervisor
(URL detection, sensor sync and the discovery announce run in the background and retry), so
the hub answers within about a second. Pushes on `/api/ws` go through a bounded queue per
connection (256 messages); a full queue, a failed send or a send stuck for 10 s closes the
connection (close 1000) instead of silently dropping it from the registry.

Sensor attributes are readable by any Home Assistant user or token (Home Assistant has no per-entity ACL). For the
notification sensor this is accepted by design: it is the remote delivery path, and
notification content is treated as not confidential (see DOCS.md "What other Home Assistant users can see"). Only
the guests sensor and the integration REST payload are PII-stripped.

## 6. Frontend build to static pipeline

The frontend is a separate SvelteKit project (`frontend/`) that is built ahead of time into
the hub's `static/` directory and shipped inside the Docker image. The running container
never builds the UI.

- It is a fully client-rendered SPA: routes set `prerender = true`, `ssr = false` (`src/routes/+layout.js`), built with Svelte 5 runes.
- `svelte.config.js` uses `adapter-static` with `pages`/`assets = '../halumar/rootfs/opt/hub/static'`, `fallback: 'index.html'`, and `paths.relative = true` (so assets resolve correctly under the Ingress subpath). Output: `index.html` (SPA fallback), prerendered `areas.html`/`clients.html`/`notifications.html`/`settings.html`, and content-hashed `_app/immutable/*` assets.
- Two shared clients in `src/lib`: `api.js` (REST, with auth headers, CSRF, method-aware retry/backoff, Ingress-aware base path, `session-expired` event) and `ws.js` (auto-reconnecting WebSocket with backoff and auth-loop guard). Both honor `window.__INGRESS_PATH` injected by `IngressMiddleware`.

Serving (`main.py`):

- `/_app/immutable/{...}`: long-term immutable cache headers (`max-age=31536000, immutable`), with path-traversal validation.
- `/images/...`: uploaded images mounted from `/data/images`.
- catch-all `GET /{full_path}`: exact file, then `<path>.html` variant, then `index.html` SPA fallback; HTML is always `no-cache` so it re-fetches the latest hashed assets. All paths are validated to stay within `static/`.

Local development: `npm run dev` proxies the API to `:8099`; the Python backend runs from
`rootfs/opt/hub` with `HUB_DB_PATH=./test.db`. `npm run build` regenerates
`static/`; release images build it themselves (`build.yaml`, job `bundle`).

## 7. CI/CD and release

Three GitHub Actions workflows under `.github/workflows/`:

| Workflow | Trigger | Does |
|---|---|---|
| `ci.yml` | push to `develop`, PRs, by hand, called by `build.yaml` | ruff, `pytest` with coverage gate, transaction lint, `pip-audit`, contract and brand checks, licenses; job `image` (by hand with "image", PRs into `main`, Dependabot PRs for the base image or Python pins) builds both images without pushing and starts the hub from them |
| `frontend.yml` | push to `develop`, PRs, by hand, called by `build.yaml` | `npm audit`, audit of the packages in the bundle, `svelte-check`, build, Playwright e2e (Chromium) |
| `build.yaml` | push to `main`, push of a tag `*.*.*` | runs both gates above, then builds, signs and publishes the images when the version in `config.yaml` has no tag yet |

All jobs run on GitHub-hosted runners. The image jobs of `build.yaml` and the `image` job of
`ci.yml` use one native runner per architecture (`ubuntu-24.04`, `ubuntu-24.04-arm`), because
the `home-assistant/builder` action builds for the runner's own architecture.

Release flow (CalVer): a release is a `version:` in `halumar/config.yaml` on `main` that
has no tag yet. Set it, add a `## <version>` section to `halumar/CHANGELOG.md` and merge to
`main`; the push starts `build.yaml`. The tag is created by CI afterwards.

Home Assistant reads `version:` on `main` at once, before the images exist: between the merge
and the end of `build.yaml` an update or new install of that version fails. The tag is created
only after the images are pushed; wait for the tag before updating. See
[../docs/release-versioning.md](../docs/release-versioning.md). Pushing a tag by hand publishes
that version; that is how an existing version gets rebuilt. A pushed tag also moves `latest` and
creates no GitHub release (`tag-release` runs only on `main`).

The `build.yaml` pipeline:

1. In parallel: `tests` and `frontend`, the full gates (`ci.yml`, `frontend.yml`), and `release-gate`: determines the version (`version:` from `config.yaml` on `main`; on a tag the tag name, which must match `config.yaml`), stops for pre-release versions and for other refs, requires a matching `## <version>` section in `halumar/CHANGELOG.md`, and checks through the API whether that tag already exists. Decides whether this run publishes.
2. `init` (only when publishing; waits for all three): the lowercased registry prefix (`ghcr.io/halumar`) and the arch matrix (`prepare-multi-arch-matrix`).
3. `bundle`: builds the Admin UI from `frontend/` once; both images get these files instead of the checked-in `static/`.
4. `build`: per arch (`amd64`, `aarch64`) on its native runner via the official `home-assistant/builder` action, using `halumar/Dockerfile` with build context `halumar/`, tagged with the version and `latest`, signed with cosign, pushed to GHCR, with an SBOM and, in a public repository, provenance and SBOM attestations.
5. `publish`: assembles and signs the multi-arch manifest `ghcr.io/halumar/hub`.
6. `tag-release` (only on `main`): creates the tag and the GitHub release — a receipt stating that images exist, not a trigger. If the build fails, no tag appears and the next push to `main` retries.

How Home Assistant pulls the image: `config.yaml` declares
`image: "ghcr.io/halumar/hub"`, the multi-arch manifest. Because a prebuilt image exists,
the Supervisor pulls the entry for its architecture instead of building on the device, so
installation is fast. The `version` field in `config.yaml` (`2026.12.0`) selects the image
tag and must match the published tag and `version.py`. The packages must be public on
GHCR; the Supervisor pulls without credentials.

Dockerfile (`halumar/Dockerfile`): starts from `ghcr.io/hassio-addons/base:20.2.0`
(Alpine 3.23, Python 3.12; pinned by digest, fixed `FROM`), installs `python3`, `py3-pip`, and `libffi` (pinned to their versions with `~`),
`pip install`s `requirements.txt` as wheels only (`--only-binary=:all:`, no on-device
compilation), then copies `rootfs/`, which contains the `static/` frontend, into the image.

## 8. App configuration (`config.yaml`)

Key manifest settings that shape the architecture:

- `ingress: true`, `ingress_port: 8099`, `ingress_stream: true`: the UI is embedded in the Home Assistant sidebar with no separate login.
- `host_network: true`, `ports: 8099/tcp` and `8100/tcp`: port 8099 is plain HTTP for Ingress and the integration (administrators cannot sign in over it from the network), and port 8100 serves TLS for fingerprint-pinning clients when `tls_enabled` is on.
- `discovery: [halumar]`: on every start the hub rotates its integration token and announces `{host, port, api_token, version}` via the Supervisor discovery API so the companion integration configures itself.
- `homeassistant_api`, `hassio_api`, `auth_api`: grant access to Home Assistant Core, the Supervisor (logs/restart/app info, `core/info`, discovery), and the Supervisor Auth API used for the direct web UI login (only while direct admin access is on). There is deliberately no `hassio_role` (default role): every endpoint the hub uses is either allowed for any app (`/addons/self/*`, `/info`, `/discovery`, `/auth`), a `*/info` endpoint of the default role, or the Home Assistant Core proxy. `tests/test_supervisor_role.py` checks this against the Supervisor's access patterns.
- `startup: application`, `init: false`: runs as the main application process.
- Options: `log_level` (enum; `trace` logs the same as `debug`), `mdns_enabled`, `tls_enabled` and `api_docs` (bool, default `true`), `reset_corrupt_database` and `reset_encryption_key` (bool, default `false`), `pairing_qr_camera` (`off`, `guests`, `all`; default `off`), read from `/data/options.json`; see [../docs/configuration.md](../docs/configuration.md).
