# API

The hub has a REST API and a WebSocket protocol. This page says which parts are a public contract
and where the reference lives. It deliberately lists no endpoints: the reference is generated from
the code, and a hand-written list would go out of date.

## Public contract

The protocol between hub, integration and apps is defined in the
[Halumar/contracts](https://github.com/Halumar/contracts) repository:

| What | File in the contract |
|---|---|
| REST API of the hub | `openapi/hub.openapi.json` |
| WebSocket protocol (`/api/ws`) | `asyncapi/hub-ws.asyncapi.yaml` |
| Shared types and value schemas | `schemas/` |
| Rule catalog, suggestions, events | `data/` |
| Behavior (profiles, enforcement, setup, family, links) | `docs/` |
| Test vectors both sides must pass | `fixtures/` |

The hub pins one version of the contract in [`.github/contracts-ref`](../.github/contracts-ref).
CI checks that the hub's FastAPI app produces exactly the OpenAPI document of that version
(`scripts/ci/contracts-check.sh`), and that the data files and test vectors the hub ships are
byte-identical copies of the contract.

The contract follows semantic versioning: a breaking change of the protocol needs a new major
version. Clients compare
`protocol_version` and `min_protocol_version` from `GET /api/health`; an app that is too old is
asked to update.

## Internal

Not part of the contract, and free to change at any time:

- the admin page's own use of the API (the Svelte frontend under `frontend/`) beyond what the
  OpenAPI document describes,
- the database schema, file layout in `/data`, and log messages,
- static assets of the admin page (tag `static` in the OpenAPI document).

## Getting the reference

- **From the contract:** open `openapi/hub.openapi.json` of the pinned contract version in any
  OpenAPI viewer. This is the recommended way.
- **From a running hub:** with the option `api_docs` on (the default), a Home Assistant administrator
  opens `/api/docs` (Swagger UI) or `/api/redoc` (ReDoc) in the admin page, or downloads
  `/api/openapi.json`. The pages use local copies of Swagger UI and ReDoc; nothing is loaded from a
  CDN.
- **From the source:** the contract's `tools/snapshot_openapi.py` imports the hub's app (without
  starting it) and writes the annotated document:

  ```bash
  python <contracts>/tools/snapshot_openapi.py --app-dir halumar/rootfs/opt/hub --check
  ```

  It needs the hub's requirements installed (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Authentication in short

| Caller | Credential | Reaches |
|---|---|---|
| Admin page through Home Assistant | Ingress (validated by the Supervisor's proxy address) | admin endpoints |
| Scripts of an administrator | admin API token (Bearer), created in **Settings > Advanced** | admin endpoints |
| Admin page directly at home (when switched on) | Home Assistant login, session cookie with CSRF token | admin endpoints |
| Halumar integration | integration token (Bearer) | only `/api/integration/*` |
| Paired app | its own device secret | the WebSocket protocol, plus `POST /api/client/control`, `/api/client/leave` and `/api/client/location` (Bearer); no other REST endpoint |

## The hub path in short

A device with parental controls or on the profile rule `control_path` `hub` ("Hub only") is on the
hub path; a TV or a watch never is. It reads and switches through the hub: `control` (REST twin
`POST /api/client/control`), `state_subscribe`, `history_get`, `forecast_get`, `camera_image_get`,
`media_image_get` and `location_report`. Whether it also holds a Home Assistant key is set by
`control_path` alone: on Hub only without remote access none, on Hub only with remote access a
read-only one; a device with parental controls on Checked holds a read-only key as well. Every
other device gets `control_error {not_hub_controlled}` for these and uses its own key. The hub answers only for
entities in the device's scope and limits the reads per device (`control_error {rate_limited,
retry_after}`, x-limits `hub_*` in the AsyncAPI document). The admin API names the result per device
in `GET /api/pairing/clients`: `control_path` (after the TV and watch mapping; `null` when the hub
could not resolve the device's rules, the cause is in the hub's log) and `has_ha_token`.
Behavior: the contract's `docs/enforcement.md` §7.

Details: [docs/DEVELOPER.md](DEVELOPER.md#authentication) and
[halumar/ARCHITECTURE.md](../halumar/ARCHITECTURE.md).
