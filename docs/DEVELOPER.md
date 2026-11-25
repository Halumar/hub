<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/brand/halumar-wordmark-on-dark.svg">
    <img src="images/brand/halumar-wordmark-color.svg" width="280" alt="Halumar">
  </picture>
</p>

<h1 align="center">Halumar Hub – developer reference</h1>

<p align="center">
  Serves Home Assistant areas and devices to the Halumar apps, with a profile per device<br>
  <a href="https://github.com/Halumar/hub">Hub</a> |
  <a href="https://github.com/Halumar/integration">Integration</a> |
  <a href="https://github.com/Halumar/contracts">Contract</a>
</p>

Developer reference of the Halumar Hub app: authentication, security, the REST and
WebSocket APIs, and how to build external clients. The user documentation (Documentation
tab of the app) is [halumar/DOCS.md](../halumar/DOCS.md); the binding contract is the
[Halumar/contracts](https://github.com/Halumar/contracts) repository; what is public contract and
what is internal is explained in [api.md](api.md). Current version: 2026.12.0.

## Quick start

1. Install the app from the Home Assistant app store (repository `https://github.com/Halumar/hub`)
   and start it.
2. Install the [Halumar integration](https://github.com/Halumar/integration) from the
   Marketplace (Home Assistant 2026.11 and newer) or HACS (older versions) as a custom repository
   ([link](https://my.home-assistant.io/redirect/hacs_repository/?owner=Halumar&repository=integration&category=integration)) and add it under **Settings > Devices & services**. It is required: it creates the Home Assistant user of every
   paired device.
3. Open **Halumar** from the Home Assistant sidebar. Ingress needs no extra login.
4. Create an area, set its column grid, and assign entities and widgets.
5. Pair a device under **Devices > Pair device** (see [Client pairing flow](#client-pairing-flow)).

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Access and connectivity](#access-and-connectivity)
- [Authentication](#authentication)
- [Security hardening](#security-hardening)
- [REST API reference](#rest-api-reference)
- [WebSocket protocol](#websocket-protocol)
- [Grid layout system](#grid-layout-system)
- [mDNS discovery](#mdns-discovery)
- [TLS and fingerprint pinning](#tls-and-fingerprint-pinning)
- [Client pairing flow](#client-pairing-flow)
- [Guest access](#guest-access)
- [Export and import format](#export-and-import-format)
- [Home Assistant sensor sync](#home-assistant-sensor-sync)
- [Home Assistant custom integration](#home-assistant-custom-integration)
- [Building external apps](#building-external-apps)
- [Configuration](#configuration)

## Overview

Halumar Hub is a Home Assistant app (add-on) that provides a centralized area, layout, and client
management system. It maintains its own database of areas with extended properties (grid
layout, images, tags, widgets) independent of Home Assistant's built-in areas.

Key design principles:

- API-first: every feature is accessible via REST and WebSocket.
- Own database: Halumar areas are independent of Home Assistant areas (optional one-way import from Home Assistant).
- No export to Home Assistant: data flows only from Home Assistant into Halumar, never the other way.
- Multi-client: designed to serve wall panels, mobile apps, and the integration at the same time.
- Real-time: WebSocket broadcasts keep connected clients in sync.

The hub never switches entities with its own rights. It ships layout, per-device Home Assistant
tokens, config and notifications. A device with a key calls Home Assistant directly with its own
token. A device on Hub only switches through the hub, and the hub has the integration carry out
each command with the device's own read-only Home Assistant user.

## Features

- Area management: create, edit, activate, and deactivate areas with configurable grid layouts (2 to 4 columns).
- Entity assignment: assign Home Assistant entities to sections of an area with custom display names, order and tile size (`columns`, `rows`).
- Widgets: place clock, text, and spacer widgets on area grids (text widgets support Markdown).
- Control modes: three-tier control system (basic, advanced, full) at global, area, and entity level.
- CSS grid layout: drag-and-drop entity and widget placement with resize support.
- Image support: card preview plus light and dark backgrounds per area, via upload or external URL.
- Home Assistant import: one-way import from Home Assistant areas (selective or all, with domain filtering).
- REST API: full CRUD surface with OpenAPI/Swagger documentation.
- WebSocket: real-time broadcasts for all layout changes (the primary channel for paired clients).
- Client pairing: PIN pairing for wall panels and mobile apps, plus a canonical deep link and QR code.
- One Home Assistant user per device: the companion integration creates a non-admin Home Assistant user with one token for every paired device (named after the device and its owner, local only unless remote access is enabled) and removes it when the device is revoked, deleted or a guest expires. Tokens are Fernet-encrypted at rest.
- Guest access: time-limited guest pairing (at most 90 days per grant, extendable) with automatic expiry; guests see only the areas their profile gives them (fail closed).
- Notifications: default and sticky notifications with priority, color, scheduling, popup modes, and MDI icon support.
- Profiles: every device has one profile (rules for access, areas, controls, kids rules, features, display, wall panel, location, when away); suggestions to copy, device adjustments (permanent or temporary), own area, remote PIN, default profile per device type, preview before saving, audit. Three boundaries: guest access, parental controls and "Hub only".
- Home Assistant access per profile (rule `control_path`): Direct (full key), Checked (read-only key, switching through the integration's checked action) or Hub only (no key: the device reads and switches through the hub, at home; with remote access a read-only key). Parental controls, guests and the Kids' room panel start on Hub only; adults on Checked, the suggestion Parents on Direct.
- Authentication: Ingress, admin API token and a Home Assistant administrator session (outside Ingress only over TLS), scoped integration token; paired clients authenticate on the WebSocket and, as Bearer, on `POST /api/client/control`, `/api/client/leave` and `/api/client/location`.
- Security hardening: rate limiting, hashed secret storage, CSRF protection, CSP headers, encryption-key isolation, TLS pinning.
- Home Assistant sensor sync: auto-maintained `sensor.halumar_*` entities mirroring layout data for remote app access via Home Assistant Cloud.
- Home Assistant custom integration: the companion integration creates real Home Assistant devices and entities for each client and area.
- mDNS discovery: Bonjour/mDNS auto-discovery for tablets and mobile apps.
- TLS: fingerprint-pinned https/wss on port 8100 (option `tls_enabled`, on by default).
- Ingress: the admin page opens in the Home Assistant sidebar (no extra login).

## Access and connectivity

| Access method | URL | Auth required |
|---|---|---|
| Home Assistant sidebar (Ingress) | Automatic via Home Assistant UI | No (Home Assistant handles auth) |
| External web UI | `https://<ha-host>:8100` | Off by default; when switched on, Home Assistant login, **Home Assistant administrators only** |
| External REST API | `https://<ha-host>:8100/api/` | Admin API token, or a session while direct access is on |
| External WebSocket | `wss://<ha-host>:8100/api/ws` | Paired client secret, or the admin session cookie |
| Swagger UI | Ingress or `https://<ha-host>:8100/api/docs` | Home Assistant administrators only; off with `api_docs: false` |
| ReDoc | Ingress or `https://<ha-host>:8100/api/redoc` | Home Assistant administrators only; off with `api_docs: false` |
| OpenAPI spec | Ingress or `https://<ha-host>:8100/api/openapi.json` | Home Assistant administrators only; off with `api_docs: false` |

The API documentation is served from files shipped with the hub (no CDN). Under
Ingress it runs in the Home Assistant origin, so it is never public and loads no third-party
script.

Port 8099 is plain HTTP/WS and is used by Home Assistant Ingress and the integration. Port 8100 serves
the same surface over TLS 1.3; the apps connect only there and pin the certificate (an address
typed with port 8099 is taken as 8100). It is active when
`tls_enabled` is on (the default). Admin credentials (Home Assistant login, session, admin API token)
are refused on plain HTTP outside Ingress (403): sign in at `https://<ha-host>:8100` or use
the sidebar panel.

**Direct admin access** (Home Assistant login, session cookie, session as bearer, the admin WebSocket with
the cookie) is **off by default**. It is switched with `PUT /api/settings/admin-direct-access`
`{"admin_direct_access": true|false}`, which only an Ingress request may call (403 for a
session or the admin API token); `GET` reads it (admin). While it is off, `POST /api/auth/login`
answers 403 `{"reason": "admin_ingress_only"}` before the credentials are checked, no session
counts, unauthenticated admin requests answer 401 with the same `reason`, and
`GET /api/auth/session` reports `{"authenticated": false, "direct_access": false, "ha_url": …}`
(Home Assistant's internal URL or `null`); the admin UI then shows a page pointing to Home Assistant.
Switching it off ends every session and closes their sockets (`4401`). Paired apps, the
integration and the admin API token are not affected.

## Authentication

### Authentication methods overview

Halumar classifies every REST request into one of these auth types:

| Method | Use case | Token type | Lifetime |
|---|---|---|---|
| Ingress | Home Assistant sidebar access | None needed | N/A |
| Home Assistant login (admins only, TLS only, direct access on) | External web UI | Session cookie (`Secure`) | 24 hours |
| API token (TLS only) | External REST API | Bearer token (64-char hex) | Permanent |
| Integration token | Halumar integration (`/api/integration/*` only) | Bearer token (64-char hex) | Rotated on every hub start under the Supervisor |

### 1. Ingress (Home Assistant sidebar)

When accessed through the Home Assistant sidebar, the Home Assistant Supervisor sets the `X-Ingress-Path`
header. Halumar's auth middleware recognizes it (and validates the source IP) and needs no
further authentication: no login, no token.

### 2. Home Assistant login (web UI)

For external web UI access on `https://<ha-host>:8100`, only while direct admin access is
switched on (see above):

1. The frontend detects it is not running under Ingress.
2. It checks `GET /api/auth/session`. If not authenticated, it shows the login page (with
   direct access off, the page pointing to Home Assistant instead).
3. The user enters a Home Assistant username and password.
4. `POST /api/auth/login` validates the credentials against the Home Assistant Auth API via Supervisor
   and then checks that the user is a **Home Assistant administrator** (group
   `system-admin` or owner; from the Supervisor answer, otherwise from Home Assistant's user list).
   Non-admins get 403; if the rights cannot be determined the login fails (503). Over
   plain HTTP the login is refused before the password is checked (403).
5. On success, a `Secure` session cookie (`hub_session`) is set with CSRF protection.
6. All subsequent requests carry the cookie and CSRF token automatically. The admin UI's
   WebSocket authenticates with the same cookie (TLS and same `Origin` required); when the
   session ends (expiry, logout) the socket is closed with code `4401`.

Sessions are stored in memory. They are lost on hub restart (the user has to sign in again).
Login attempts are limited per IP and per username (5 per minute each).

### 3. API token (REST API)

For programmatic REST API access:

1. An API token is auto-generated on first database initialization (stored as a SHA-256 hash).
2. Regenerate it via `POST /api/auth/token/regenerate` (requires existing auth).
3. The token is shown only once. It cannot be retrieved again.
4. Use it as a Bearer token in the `Authorization` header: `Authorization: Bearer <64-char-hex-token>`.

### 4. Integration token (Halumar integration)

The companion integration authenticates with a dedicated integration token that is valid only
for `/api/integration/*` (it opens nothing else, and those endpoints accept nothing else):

1. Under the Supervisor, the hub rotates the token on every start and announces `{host, port, api_token, version}` via the Supervisor discovery API. Home Assistant offers or updates the integration automatically, and the token never appears on screen.
2. Without Supervisor discovery (for example when the integration is added by hand or an announce failed), generate it under **Settings > Advanced > Integration token** (`POST /api/auth/integration-token/regenerate`, shown once).
3. Stored as a SHA-256 hash. The hub tracks integration requests in memory and shows the connection state under Settings, System, Integration.

### 5. Paired-client secret (WebSocket only)

For wall panels and mobile apps. See [Client pairing flow](#client-pairing-flow) for the
full process. The client secret (64-char hex) is only accepted as the WebSocket auth
message `{"type": "auth", "secret": "<secret>"}`; it opens no REST endpoint. Over the
WebSocket a client reads only the areas of its own list (`get_areas`, `get_area`; plus
areas its own notifications link to). Camera access tokens, token-bearing pictures, GPS
of `person`/`device_tracker` and server file paths are removed from what it receives.

### Paths without authentication

The following paths never require authentication:

| Path | Reason |
|---|---|
| `/api/health` | Health check (monitoring) |
| `/api/auth/login` | Login endpoint (credentials are the auth) |
| `/api/auth/logout` | Logout (clears the cookie) |
| `/api/auth/session` | Session check |
| `/_app/*` | Static frontend assets |
| `/images/*` | Uploaded area images |
| WebSocket upgrade | Auth handled in the WS handler |

## Security hardening

### Ingress IP validation

The `X-Ingress-Path` header is trusted only when the request originates from the Home Assistant
Supervisor's Ingress proxy (`172.30.32.2`), for both REST and WebSocket. Other apps
(`172.30.32.1`, `172.30.33.x`) and host processes (localhost) are treated as ordinary
external callers; the header is ignored and the attempt is logged as
`Rejected Ingress request from untrusted IP`. The source IP is the TCP connection
address, never an `X-Forwarded-For` header. Loopback is only trusted with the
development environment variable `HUB_DEV=1`, which the Supervisor never sets.

### Rate limiting

| Endpoint | Limit | Window |
|---|---|---|
| `POST /api/auth/login` | 5 attempts per IP | 60 seconds |
| `POST /api/auth/login`, failed | 5 per account from one IP; 30 per account once they come from at least 5 IPs | 15 minutes |
| WebSocket `pair` | 5 attempts per connection, 10 failed attempts per IP across connections (reset by a new PIN or a successful pairing) | 10 minutes |
| Active pairing PIN | invalid after 30 wrong or rate-limited attempts from at least 3 IPs | per PIN |
| Unauthenticated WebSockets | 5 per IP at a time, 30 s to authenticate | – |

### Revocation

Revoking (`DELETE /api/pairing/clients/{id}`) closes the client's live WebSocket at once
(4001, guests 4003). After a permanent delete the socket only accepts `client_reset_ack`
until it is closed; no data is served any more.

### Audit log and log hygiene

Admin requests (Ingress incl. Home Assistant user name, session, admin token) are logged on the
`hub.security` logger as `ADMIN_ACCESS` (every state-changing request and reads of
sensitive paths such as tokens, logs, clients, export), as are admin WebSocket sessions.
Every log record is redacted: query strings in uvicorn lines, `Bearer` values,
`token`/`secret`/`pin`/`password` values and JWTs are masked, and the `websockets` and
`aiosqlite` libraries never log below INFO, even with `log_level: debug`.

### Hashed secret storage

All secrets (API token, integration token, paired-client secrets) are stored as SHA-256
hashes with constant-time comparison. Plaintext secrets are never persisted.

### Encryption key isolation

Home Assistant long-lived tokens are Fernet-encrypted. The encryption key is stored in a separate keyfile
(`/data/.hub_keyfile`) with `0600` permissions, not in the database. The hub holds no
other Home Assistant credential than its Supervisor token.

### Backups contain secrets

The Fernet keyfile (`/data/.hub_keyfile`, including its local copy `.hub_keyfile.bak`)
and the TLS private key (`/data/tls/key.pem`) are excluded from Home Assistant backups (`backup_exclude`).
After a restore the hub creates a new
key and a new certificate: stored Home Assistant tokens can no longer be decrypted (reported as
`token_error: decryption_failed`; the hub asks the integration for new device tokens), and apps must pair again
because the fingerprint changed.

A Home Assistant backup that includes this app still contains:

- the hub's `SUPERVISOR_TOKEN` (the Supervisor stores it in the hub's backup metadata),
- the database with hashed client secrets, encrypted (but without the key undecryptable)
  Home Assistant tokens and the configuration.

A Home Assistant **Core** backup also contains the integration token of this hub (in
plaintext in the integration's config entry). It stays valid until the hub restarts (the
hub rotates it on every start). Under the Supervisor the hub accepts the integration
token only from Home Assistant on its own host, never from the LAN or other apps,
so a leaked copy cannot be used from another machine.

**Only use encrypted backups** (Home Assistant backup encryption key) and treat backup files like
passwords: do not upload or share unencrypted backups, for example as attachments to bug
reports. The hub runs with the Supervisor's default role (no `hassio_role`), which limits what a leaked Supervisor token can do, but it still
reaches the Home Assistant Core API. If an unencrypted backup was exposed, treat it as a compromise:
rotate the client tokens (`POST /api/clients/{id}/rotate-token`),
regenerate the TLS certificate and reinstall the hub to get a new Supervisor token.

### Data safety

- **Snapshots:** Halumar writes a consistent snapshot of its database (`VACUUM INTO`, includes
  the WAL) to `/data/backups/` once a day (newest 7 kept) and before every schema migration
  (newest 3 kept). Each snapshot is integrity-checked before it is kept.
- **Migrations:** the schema version is `PRAGMA user_version`; every migration step runs in
  one transaction together with the version bump. A failing step changes nothing.
- **Damaged database:** Halumar restores the newest valid snapshot automatically (changes
  after it are lost; the damaged file is kept as `hub.db.corrupt-<time>`) and creates a
  Home Assistant notification. Without a usable snapshot it does **not** start with an empty database: it
  answers 503 with instructions and leaves the files untouched. Then restore a Home Assistant backup of
  the hub, or turn on the option **Reset damaged database** (`reset_corrupt_database`),
  restart, and turn it off again.
- **Database of a different build:** a database written by another (pre-release) Halumar
  build can carry the same schema version with different tables. Halumar checks every table
  and column at start; if any is missing it does not start "half working" but answers 503
  (health and API) with the missing columns and the same instructions. With
  **Reset damaged database** the old file is kept as `hub.db.mismatch-<time>` and Halumar
  starts empty (pair the apps again).
- **Encryption key:** the key file is written atomically together with a copy
  (`.hub_keyfile.bak`); a damaged key file is restored from the copy. If both are
  unusable while the database holds encrypted tokens, Halumar stops with instructions instead
  of creating a new key. Option **Reset encryption key** (`reset_encryption_key`) creates a new
  key; stored Home Assistant tokens must then be set again. Exception: right after restoring a Home Assistant backup
  taken while the hub was running (hook marker present), both files are missing by design
  (not in Home Assistant backups) and a new key is created automatically, with a notification. A Home Assistant backup
  taken while the hub was stopped has no marker; after restoring it Halumar stops with the
  same instructions.
- **Home Assistant backups** run hot. The `backup_pre` hook writes `backups/hub-ha-backup.db` and a
  marker; after restoring such a backup Halumar starts from that consistent snapshot (the
  previous files are kept as `hub.db.pre-restore-<time>`). Daily and pre-migration
  snapshots and archived files are excluded from Home Assistant backups.

### CSRF protection

State-changing requests (POST, PUT, PATCH, DELETE) from cookie-authenticated sessions require
a valid `X-CSRF-Token` header matching the `hub_csrf` cookie. Bearer-token clients are
exempt (they carry no ambient cookie credentials).

### WebSocket security

| Protection | Detail |
|---|---|
| Auth timeout | Unauthenticated connections are closed after 30 seconds |
| Max connections | Separate caps for authenticated and unauthenticated sockets |
| Pairing brute-force | Limited pairing attempts per connection |
| Message size limit | 64 KB max incoming message size (frames above 256 KiB are refused by the server before the hub reads them) |
| Ping/pong keepalive | Server-initiated ping detects dead connections |

### Additional protections

- Content Security Policy restricting script, style, and image sources.
- Security headers: `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy`.
- Body size limit, also for a chunked body without `Content-Length` (counted while it is read; 413): 64 KiB without credentials, 1 MiB for authenticated requests, 16 MB only for an admin on the image uploads and 64 MiB on `POST /api/import/areas` (the export of a full hub is about 35 MiB). The whole body must arrive within 15 s (120 s for those large routes), else 408.
- Image upload validation: file-extension whitelist, 10 MB max, chunked reading.
- SQL injection prevention: column-name whitelists for dynamic queries.
- Session limits: capped per user and globally.
- TLS pinning and a signed, token-less compliance channel (see [TLS and fingerprint pinning](#tls-and-fingerprint-pinning)).

## REST API reference

All endpoints are under `/api/`. This page does not list them: the reference is generated from the
code and published in the contract as `openapi/hub.openapi.json`. CI checks that the hub produces
exactly that document for the pinned contract version. How to open it, and what is public contract
and what is internal: [api.md](api.md).

### Health

`GET /api/health`

```json
{
  "status": "ok",
  "db": "ok",
  "version": "2026.12.0",
  "build": "2026-11-26",
  "uptime_seconds": 1234,
  "connected_clients": 2,
  "tls_fingerprint": "<sha256-hex or null>",
  "protocol_version": 1,
  "min_protocol_version": 1,
  "min_app_version": {"ios": "2026.12.0", "ipados": "2026.12.0", "tvos": "2026.12.0", "watchos": "2026.12.0"},
  "last_snapshot": {"file": "hub-daily-20261118.db", "created": "2026-11-18T03:00:00+00:00"},
  "data_recovered": false
}
```

`min_app_version` lists only the platforms that have a lowest accepted app version; a platform
without an entry (at the moment Android phone, tablet and TV) has none.
`protocol_version`, `min_protocol_version` and `min_app_version` are explained under
[Protocol version](#protocol-version). `last_snapshot` is the newest database snapshot (or `null`), `data_recovered` is `true` when
this start restored a snapshot or replaced the encryption key (details in the log and as a Home Assistant
notification). If the start found damaged data it cannot safely repair (see
[Data safety](#data-safety)), every request answers **503**; the health body then has
`"db": "unavailable"`, `error` and `action`.

The endpoint probes the database (read on the shared connection, write lock on a
short-lived second connection). If the database is not usable it answers **503** with
`"status": "error"` and the reason in `db`. It is also the Supervisor watchdog target
(`watchdog:` in `config.yaml`): turn on **Watchdog** on the hub page so the Supervisor
restarts Halumar when the health check fails or the server stops after a start error.

### Integration API

Endpoints under `/api/integration/` accept only the dedicated integration token (see
[Authentication](#authentication)). The status payload contains no personal data: notifications
appear as ids and counts per client, guests as `client_id`/`is_connected`/`expires_at`, and the
pairing PIN is never included.

One Home Assistant user per device. For every paired device the hub asks the companion
integration (WebSocket push `provision_client`, fallback `provisioning[]` in the status
snapshot) to create a dedicated Home Assistant user, non-admin, without password, named
`Halumar · [Guest ][<owner> ]<device>`, `local_only` unless the device's effective profile rule
`remote_access` is on (guests too, when their profile allows it). The integration delivers the user's token to
`POST /api/integration/client-token`; the hub stores it encrypted and hands it to the app.
Renaming a device, changing its owner or remote access, and extending a guest are reported
with `update_client_identity`; revoking a device, a guest expiring and the final removal of
a deleted device remove the Home Assistant user (`deprovision_client`). A deleted device keeps its Home Assistant
user until its app confirmed the reset: right away when the app is connected (at most
~11 s), otherwise when it comes back to the home network, at the latest after 24 hours. Until
then the status snapshot lists it in `ha_user_holds`, and a returning app is reset by
Halumar before it can fail a login at Home Assistant. Use **Revoke** for a lost or stolen
device: it removes the Home Assistant user at once. Manage devices in the hub, not in the Home Assistant user list.
Granting remote access keeps the device's token; taking it away rotates it (open remote
sessions end). Whenever a device loses its key (remote access off, Hub only, a temporary value
ending, access closing), its old token keeps working until the hub has received the replacement and the integration has
removed the old token. While the integration cannot be reached this can take a while; the hub
keeps trying. Timed ends are applied by the hub: while
the hub is stopped, a key whose temporary value has ended keeps working until the hub starts again. The external Home Assistant URL (`client_config.ha_url`) goes only to devices with remote
access: the URL entered in the hub settings, else what Home Assistant reports through the integration
(`external_url`, else the Home Assistant Cloud address; `POST /api/integration/ha-url`). A device whose
token changed after its last hub contact gets `need_token_update` from the compliance view,
so the app stops signing in from outside until it is home again.

Home Assistant behavior that affects `local_only` users:

- They are also blocked when they come through Home Assistant Cloud.
- Tailscale addresses (100.64.0.0/10) do not count as local.
- A reverse proxy that does not send `X-Forwarded-For` makes every request look local.
- With `trusted_networks`, the login dialog lists active users.

## WebSocket protocol

The message reference is the AsyncAPI document `asyncapi/hub-ws.asyncapi.yaml` in the
[contract](https://github.com/Halumar/contracts). This section explains the flow.

The WebSocket is the primary communication channel for paired clients.

### Connection

```
wss://<ha-host>:8100/api/ws
```

The apps use only this TLS URL and pin the certificate. Before the fingerprint is known (a typed
address or a link without `fp`), the app reads the certificate over the same TLS connection
before it sends the PIN. Home Assistant Ingress and the integration use plain `ws://` on port 8099.

### Authentication flow

Ingress: auto-authenticated, the server sends `auth_ok` immediately.

External, pair with PIN:

```json
Server: {"type": "auth_required", "message": "Send auth with secret or pair with PIN"}
Client: {"type": "pair", "pin": "4821-0736", "client_name": "Kitchen Tablet",
         "protocol_version": 1, "platform": "ipados", "app_version": "2026.12.0",
         "app_build": "6", "device_model": "iPad16,3"}
Server: {"type": "pair_ok", "client_id": "uuid", "client_name": "Kitchen Tablet", "secret": "64-char-hex",
         "protocol_version": 1, "min_protocol_version": 1, "min_app_version": {...}}
```

External, auth with secret:

```json
Client: {"type": "auth", "secret": "64-char-hex-string",
         "protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}
Server: {"type": "auth_ok", "client_id": "uuid",
         "protocol_version": 1, "min_protocol_version": 1, "min_app_version": {...}}
```

### Protocol version

Every app sends its client info with `pair` and `auth`: `protocol_version`, `platform`
(`ios` | `ipados` | `tvos` | `watchos` | `android` | `android_tablet` | `android_tv` | `wearos` | `web`) and `app_version` are required, `app_build`
and `device_model` (hardware identifier such as `iPhone18,1`) optional. `wearos` and `web` are
reserved for planned apps that do not exist yet. The hub stores
them per device (admin API, device list) and answers:

- **App too old** – `protocol_version` missing or below `min_protocol_version`, or
  `app_version` below `min_app_version[platform]`: `upgrade_required` with `reason`
  (`protocol` | `app_version`) and a readable `message`, then close code **4426**. The pairing
  stays valid and a PIN is not used up; the app shows "please update" instead of retrying.
- **Malformed client info** (unknown platform, invalid version): `pair_error` / `auth_error`.
- **Platform not offered for the profile** – a guest or child profile outside the platforms of its
  kind, or a TV or watch on a profile on Hub only (unless it is a TV profile): `pair_error`
  `platform_not_allowed` (PIN, QR code, guest invitation; the invitation stays open), or
  `onboard_error` `profile_not_allowed` when another device sets it up.
- **Platform fixed at pairing** – `auth` updates `protocol_version`, `app_version`,
  `app_build` and `device_model`, never `platform`. A valid `platform` other than the stored
  one is refused with `auth_error` `invalid_client_info` and nothing is stored; the audit view
  gets a `client_info_mismatch` entry (once per device per 10 minutes). Only `android` and
  `android_tablet` may stand for each other (the stored value stays). The app-version floor is
  the one of the stored platform.

`protocol_version` starts at 1 and is raised only for changes an older app cannot handle.
The hub serves every app protocol from `min_protocol_version` to its own
`protocol_version` and always supports at least the **two previous protocol versions**. The
same values are in `auth_ok`, `pair_ok`, `GET /api/health` and the Bonjour TXT record
(`pv`, `pv_min`), so an app can check before pairing.

### Client-to-server messages

| Type | Description |
|---|---|
| `pair` | Pair with a PIN (before auth) |
| `auth` | Authenticate with a secret |
| `ping` | Keepalive |
| `get_areas` | Request the area list |
| `get_area` | Request area detail |
| `subscribe` | Subscribe to broadcasts |
| `get_client_config` | Get the Home Assistant token, URLs, and per-client config |
| `client_status` | Report client status |
| `notification_ack` | Acknowledge a notification |
| `notification_dismiss` | Dismiss a notification |
| `get_notifications` | Fetch active notifications (the single WS content channel; the Home Assistant notification sensor carries the same items for remote reads) |
| `client_reset_ack` | Confirm a local wipe after deletion (clears the server-side tombstone) |
| `client_leave` | The device signs itself off before a reset (removed at once with its Home Assistant user; `client_leave_ok`, close 1000) |
| `control` | Switch one entity through the hub (devices on the hub path; `control_ok`, `rule_blocked` or `control_error`) |
| `state_subscribe` | Live states of the device's scope (hub path; at most every 10 s) |
| `history_get`, `forecast_get` | Sensor history and weather forecast (hub path; 10 per 10 s together) |
| `camera_image_get`, `media_image_get` | Camera still image and media artwork, read by the hub (hub path; limits in the AsyncAPI document) |
| `location_report` | Position of a device on the hub path |

### Server-to-client messages

| Type | Description |
|---|---|
| `auth_required` | Authentication needed |
| `auth_ok` | Authentication successful |
| `auth_error` | Authentication failed |
| `pair_ok` | Pairing successful (includes the secret) |
| `pair_error` | Pairing failed |
| `upgrade_required` | App too old for this hub (followed by close 4426) |
| `pong` | Response to a ping |
| `areas` | Area list response |
| `area` | Area detail response |
| `client_config` | Home Assistant config response (token, URLs, notification sensor id, per-client config) |
| `notifications` | Active notifications for this client (push-shaped items) |
| `command` | Remote command from an admin |
| `client_removed` | This client was deleted (paired with the tombstone flow) |
| `control_ok`, `rule_blocked`, `control_error` | Answers of the hub path (`control_error` with `retry_after` for `rate_limited`) |
| `state_subscribed`, `entity_state` | Live states of the hub path |
| `history`, `forecast`, `camera_image`, `media_image` | Reads of the hub path |
| `session_expired` | Admin web-UI session ended (followed by close 4401) |
| `error` | Error message |

### Broadcast events

Sent to authenticated clients when data changes. Events with an `ADMIN` audience reach only
Ingress/admin sockets and never leak to paired clients. Events of one area (`area_updated`,
`entity_*`, `section_*`, `widget_*`, `layout_updated`) reach admin sockets, the integration and
only the paired clients that may read that area; when `is_active` or the tags of an area change,
every app also gets `areas_changed` (no payload) and reloads its list.

| Event | Data |
|---|---|
| `area_created` | `{area_id, name}` |
| `area_updated` | `{area_id}` |
| `area_deleted` | `{area_id}` |
| `areas_deleted` | `{area_ids}` |
| `entity_added` | `{area_id, entity_id}` |
| `entity_updated` | `{area_id, entity_id}` |
| `entity_removed` | `{area_id, entity_id}` |
| `layout_updated` | `{area_id}` |
| `widget_created` | `{area_id, widget_id}` |
| `widget_updated` | `{area_id, widget_id}` |
| `widget_deleted` | `{area_id, widget_id}` |
| `client_paired` | `{client_id, client_name, is_guest}` |
| `client_connected` | `{client_id}` |
| `client_disconnected` | `{client_id}` |
| `client_kicked` | `{client_id}` |
| `client_status_updated` | `{client_id, ...status}` |
| `guest_extended` | `{client_id, expires_at}` |
| `guests_expired` | `{client_ids}` |
| `notification_created` | `{notification_id, client_id}` |
| `notification_delivered` | `{notification_id}` |
| `notification_deleted` | `{notification_id}` |
| `notifications_cleared` | `{client_id, count}` |

## Grid layout system

Each area has `grid_cols` (2 to 4) defining the number of columns. Entities and widgets are
placed on this grid:

| Field | Of | Description | Range | Default |
|---|---|---|---|---|
| `grid_cols` | area | Columns of the area | 2 to 4 | 2 |
| `column_span` | section | Columns the section spans | 1 to 12 | 1 |
| `columns` | section | Columns inside the section | 1 to 2 | 2 |
| `columns` | tile (entity, widget) | Columns the tile spans | 1 to 2 | 1 |
| `rows` | tile (entity, widget) | Rows the tile spans | 1 to 2 | 1 |

Order is `sort_order`. The frontend uses CSS Grid with drag-and-drop reordering.

## mDNS discovery

Halumar announces itself on the local network for auto-discovery:

| Property | Value |
|---|---|
| Service type | `_halumar._tcp.local.` |
| Instance name | `<home title> (<short id>)`, e.g. `Home (3f9a1c)` |
| Host | `hub-<short id>.local.` (`-2`, `-3` … if another device answers for it) |
| Port | 8099 |
| TXT records | `ws_path=/api/ws`, `api_path=/api`, `iid=<instance uuid>`, `version`, `pv` / `pv_min` (protocol version), and (when a TLS cert exists) `tls=1`, `tls_port=8100`, `fp=<sha256-hex>` |

The short id is the start of the instance id `iid`, a UUID created once per installation
(`/data/.instance_id`). Two hubs in one network therefore never share name or host, and host
and fingerprint of an entry always belong to the same hub. Apps should list every entry and
deduplicate by `iid`.

Discovery can be disabled with `mdns_enabled: false` in the hub config.

## TLS and fingerprint pinning

When `tls_enabled` is on (the default), the hub serves the same API over TLS 1.3 on port
8100 in addition to plain HTTP/WS on port 8099. The certificate is a self-signed leaf that
clients pin by SHA-256 fingerprint on a trust-on-first-use basis (it is not CA-validated).

The fingerprint is distributed through three channels so a client can learn and verify it:

- `GET /api/health` (`tls_fingerprint`).
- The mDNS TXT record (`tls`, `tls_port`, `fp`).
- The canonical pairing deep link: `halumar://pair?url=<enc>&pin=XXXX-XXXX[&tls_port=8100&fp=<hex>]`.

Regenerating the certificate (`POST /api/settings/tls/regenerate`) changes the fingerprint,
which forces pinned clients to re-pair and requires a hub restart. Until the restart the
hub keeps advertising the fingerprint of the cert the running server presents;
`GET /api/settings/tls` names the new one as `pending_fingerprint`.

A signed, token-less compliance channel lets a locked-out app learn why it lost access. The
hub derives a small, PII-free status per client (`need_token_update` or `deleted`) and
signs it with HMAC-SHA256 using a key derived from that client's own secret. Only the client
can verify its own status; every other case returns an identical neutral response. The app
acts on the status only after verifying the signature against its own pairing secret.

## Client pairing flow

1. An admin generates a PIN under **Devices > Pair device** (valid 5 minutes, single-use).
2. The client discovers Halumar via mDNS, a scanned QR code, a deep link, or a manual IP entry.
3. The client connects the WebSocket and receives `auth_required`.
4. The client sends `pair` with the PIN and a device name.
5. On success it receives `pair_ok` with a permanent `secret`.
6. The client stores the secret and sends `get_client_config`.
7. The hub delivers the per-client config with the Home Assistant URLs. The companion integration creates the device's own Home Assistant user and token in the background; the hub sends `client_config_updated` as soon as the token arrives (until then `ha_token` is `null`). No manual token entry is required.

## Guest access

Guest clients have time-limited access with automatic expiry.

### Creating guest pairing

```json
POST /api/pairing/generate
{"access": "guest", "guest_hours": 24}
```

Other optional fields: `profile_id`, `own_area_id`, `owner_label`, `guest_expires_at`.

### Guest features

- Automatic expiry, from 1 hour to 90 days per grant (default from the house setting, 24 hours). A background task disconnects expired guests periodically, and guests are hard-deleted at expiry.
- Guests see the areas their profile gives them.
- Own Home Assistant user, local only unless its profile allows remote access: a guest gets its own Home Assistant user like every device; its token is bounded to the guest expiry and the user is removed when the guest expires. Until the token arrives the guest gets no `ha_token` (fail closed); there is no shared guest token.
- Extend expiry via the REST API or the Home Assistant datetime entity.
- Expiry timestamps (`guest_expires_at`, `expires_at` on extend) must carry a timezone offset
  (`Z` or e.g. `+01:00`); values without offset, unparseable values, past values or values more
  than ten years ahead are rejected with 422. They are stored and returned as UTC
  (`2026-11-18T17:00:00+00:00`, seconds truncated) and compared as date-times, so a guest
  expires on time regardless of the offset it was created with.

## Export and import format

`GET /api/export/areas` returns (format version 2, sections-based):

```json
{
  "format_version": 2,
  "version": "2026.12.0",
  "exported_at": "2026-11-18T10:00:00Z",
  "areas": [
    {
      "name": "Living Room",
      "icon": "mdi:sofa",
      "description": "Main living area",
      "color": "#FF5733",
      "grid_cols": 2,
      "sort_order": 0,
      "is_active": true,
      "control_mode": "advanced",
      "ha_area_id": "living_room",
      "tags": ["ground_floor"],
      "sections": [
        {
          "title": "Lights",
          "sort_order": 0,
          "column_span": 1,
          "columns": 2,
          "entities": [
            {
              "entity_id": "light.living_room",
              "display_name": "Main Light",
              "columns": 1,
              "rows": 1,
              "sort_order": 0,
              "is_visible": true,
              "control_mode": null
            }
          ],
          "widgets": [
            {"widget_type": "clock", "config": {}, "columns": 2, "rows": 1, "sort_order": 1}
          ]
        }
      ],
      "excluded_entities": ["sensor.excluded_one"]
    }
  ]
}
```

`POST /api/import/areas` takes `{"areas": [...], "overwrite": false}` (at most 100 areas).
Each area is checked completely before anything is written, with the models of the REST
endpoints (area as `POST /api/areas`, sections, entities and widgets as their `POST`;
`null` for a number is invalid). Invalid areas are skipped and listed in `errors`; a widget
with an unknown type or a config that is no object is skipped alone. Areas whose name exists
are skipped unless `overwrite` is set (then their content is replaced). The import runs as one
transaction with one savepoint per area: other changes wait until it is done, and nobody sees
half an area. Afterwards the admin UI gets `area_created`, sessions that may read a replaced
area get `area_updated`, and every app gets `areas_changed`.

| Result | Meaning |
|---|---|
| 200 `{"imported", "skipped", "failed", "errors"}` | Valid areas imported; `failed` counts the invalid ones |
| 422 | No area imported because every entry was invalid |
| 503 | Database error; the whole import was rolled back (nothing imported), safe to retry |

## Home Assistant sensor sync

Halumar automatically maintains Home Assistant sensors so client apps can read Halumar data remotely
(for example over Home Assistant Cloud) when the hub's LAN port is unreachable.

### Sensors created

| Sensor | Content |
|---|---|
| `sensor.halumar_config` | Area list (active areas only), Home Assistant URLs, version |
| `sensor.halumar_area_{slug}` | Area detail with entities and widgets (active areas only) |
| `sensor.halumar_guests` | Guest count; per guest only `client_id`, `is_connected`, `expires_at` |
| `sensor.halumar_client_{slug}_{uid8}_notifications` | Per-client notifications with full content: state = active count; `notifications` attribute = push-shaped items (same JSON shape as the `get_notifications` WS response and the `display_notification` push), newest first; like the socket at most 50 active ones per device (sticky, then higher priority, then newer ones are kept), one that is not sticky ends 72 h after `created_at` or a later `scheduled_at` (both dismissed by the hub, announced with `dismiss_notification`) |
| `input_boolean.halumar_area_{slug}` | Per-area activation switch (a toggle in Home Assistant updates `areas.is_active`) |

Deactivating an area deletes its `sensor.halumar_area_{slug}` while the `input_boolean`
remains as the way back, and `sensor.halumar_config` filters to active areas only.

### Sensor visibility (by design)

Sensor attributes are readable by any Home Assistant user or long-lived token; Home Assistant's states
API has no per-entity ACL. The per-client notification sensor deliberately carries the full
notification content anyway, because it is the remote delivery path. The hub is LAN-only,
so away from home a client app reaches only Home Assistant (for example via Home Assistant Cloud) and
this sensor is its only notification source. Halumar therefore treats notification content
(and the projected layout data) as not confidential. Do not send notifications whose content
must stay hidden from other Home Assistant users. Only the guests sensor and the integration REST payload are PII-stripped.

### Auto-update triggers

Sensors are updated when:

- Areas, entities, or widgets are created, updated, or deleted.
- Clients connect or disconnect.
- Guests are paired, extended, or expired.
- Notifications are sent or cleared.
- Home Assistant lost them: Home Assistant does not persist states-API entities, so after a Core restart
  or update they are gone. The hub notices it from Home Assistant's events
  (`homeassistant_started`, a removed `sensor.halumar_config`), on every reconnect and from a 30-second reconcile
  loop, and then runs a full sync; this also retries a startup sync that ran before Home Assistant was
  ready.

## Home Assistant custom integration

The Halumar integration creates real Home Assistant devices and entities. It is a single-instance,
poll-plus-push component: the hub's `GET /api/integration/status` snapshot is the single
source of truth for all entity state. It polls every 30 seconds, relaxing to 300 seconds
while a WebSocket push channel is up.

### Hub device (the Halumar Hub)

- Sensors: Version, Areas, Connected clients, Guests
- Per-area sensor (area detail) and per-area switch (activation)
- Pairing Active binary_sensor (whether a pairing window is open)
- Pairing status camera (shows whether a pairing window is open; it shows the QR code with the PIN only when the app option `pairing_qr_camera` allows it: `guests` during a guest pairing, `all` during every pairing)
- Buttons: Start Pairing, Start Guest Pairing, Cancel Pairing

### Client device (one per paired client, nested under the hub)

- Connected binary_sensor (online/offline)
- Sensors: Current Area, Device Name, App Version, Battery, Connection Type, Last Seen, Notifications
- GPS device_tracker (location)
- Guest expiry datetime (editable, guests only)

### Actions

- `halumar.navigate_to_area`: navigate a client to an area
- `halumar.show_entity`: show an entity on a client
- `halumar.display_notification`: send a notification
- `halumar.clear_notifications`: clear a client's notifications
- `halumar.ack_notifications`: acknowledge a client's notifications
- `halumar.refresh_client`: refresh a single client
- `halumar.force_refresh`: force a full data refresh
- `halumar.kick_client`: disconnect a client's session
- `halumar.start_pairing`: start regular pairing
- `halumar.start_guest_pairing`: start guest pairing
- `halumar.cancel_pairing`: cancel the pairing window
- `halumar.manage_client_tags`: add, remove, or set client tags
- `halumar.extend_guest`: extend a guest session
- `halumar.control`: switch entities on behalf of a device after checking its rules
- `halumar.apply_rules`: apply rule snapshots of devices by hand (for checks)
- `halumar.report_location`: update a device's tracker after checking its location rule
- `halumar.panel_announce`: speak and show a text on a wall panel
- `halumar.panel_show_camera`: open a camera view on a wall panel or TV

### Events

The integration fires Home Assistant events for automations, including client connection changes, client
area changes, and pairing lifecycle events (`halumar_client_connected`,
`halumar_client_disconnected`, `halumar_client_area_changed`, `halumar_pairing_started`,
`halumar_pairing_stopped`).

## Building external apps

### Architecture

```
+---------------------------------------------------------+
|                     Your App                            |
+---------------------------------------------------------+
|  Halumar WebSocket (LAN)     |  HA WebSocket            |
|  - Layout (areas, entities)  |  (devices with a key)    |
|  - Real-time broadcasts      |  - Entity states         |
|  - Pairing, per-client config|  - Actions               |
|  - Hub only: states, control |  - State subscriptions   |
|                              |                          |
|  Fallback: HA sensors        |                          |
|  (via HA WS when hub down)   |                          |
+---------------------------------------------------------+
```

### App lifecycle

1. First launch (must be on the LAN):
   - Discover Halumar via mDNS, a QR code, or a deep link.
   - Pair with a PIN and receive a secret.
   - `get_client_config` and receive the per-client Home Assistant token (`null` for a device on
     Hub only without remote access) plus URLs.

2. Normal operation:
   - Connect the Halumar WS with the secret.
   - On Direct or Checked: connect the Home Assistant WebSocket with the token. Layout comes
     from Halumar, states and control from Home Assistant directly (on Checked, switching goes
     through the integration's checked action).
   - On Hub only: read states with `state_subscribe` and switch with `control` on the Halumar
     WebSocket. Without remote access the device has no Home Assistant connection at all.

3. Remote or fallback (Halumar unreachable, for example away from the LAN; only devices with a
   token):
   - Layout from `sensor.halumar_config` via the Home Assistant WebSocket.
   - Notifications from `sensor.halumar_client_{slug}_{uid8}_notifications` via the Home Assistant WebSocket (full push-shaped items; the entity id comes from `client_config.notification_sensor`).
   - States via the Home Assistant WebSocket; switching directly with a full key, otherwise through the
     integration's checked action.

## Configuration

App options in Home Assistant (the **Configuration** tab):

| Option | Type | Default | Description |
|---|---|---|---|
| `log_level` | enum | `info` | Logging level; `trace` logs the same as `debug` |
| `mdns_enabled` | bool | `true` | Enable mDNS discovery |
| `tls_enabled` | bool | `true` | Serve https/wss on port 8100 (fingerprint-pinned); port 8099 stays plain for Ingress |
| `reset_corrupt_database` | bool | `false` | Only after Halumar reported a damaged database without snapshot or a database of a different build: start with an empty database (old file kept). Turn off again |
| `reset_encryption_key` | bool | `false` | Only after Halumar reported a damaged/missing key file: create a new key (stored Home Assistant tokens must be set again). Turn off again |
| `api_docs` | bool | `true` | Serve the API documentation (`/api/docs`, `/api/redoc`, `/api/openapi.json`) to Home Assistant administrators; `false` answers 404 |
| `pairing_qr_camera` | `off`, `guests`, `all` | `off` | When the integration's camera **Pairing status** shows the pairing QR code: never, during guest pairings, or during every pairing ([limits](configuration.md)) |

### Supported architectures

- `amd64` (x86_64)
- `aarch64` (64-bit ARM, for example a Raspberry Pi 4 or 5)

## Version history

See [CHANGELOG.md](../halumar/CHANGELOG.md) for the changes per version.
