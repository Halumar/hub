# Backend test suite (`halumar/tests`)

The pytest suite of the hub backend, the FastAPI app under `../rootfs/opt/hub`. The tests run without
Home Assistant, a network or shared state: every test gets its own SQLite database file and fakes for
the HA client and the WebSocket. `conftest.py` puts `../rootfs/opt/hub` on `sys.path`, so the tests
import `api.*`, `models.*` and `services.*` directly.

## Running

```sh
pip install -r ../rootfs/opt/hub/requirements.txt -r ../requirements-dev.txt
cd halumar/tests
CONTRACTS_DIR=/path/to/contracts python -m pytest
```

- `pytest.ini` sets `asyncio_mode = auto`: async tests need no decorator. They need pytest-asyncio
  (`requirements-dev.txt`); if it is installed without its plugin entry point, add
  `-p pytest_asyncio.plugin`.
- `CONTRACTS_DIR` points to a checkout of the contract repository. Without it, `conftest.py` looks
  for `../contracts` next to this repository and for `.contracts` in it (where the CI action
  `.github/actions/contracts` checks it out). Tests that need the contract skip when none is found;
  with `CI` set, a missing checkout fails the run.
- CI runs the suite with a coverage gate (`.github/workflows/ci.yml`).

## Shared fixtures

`conftest.py`:

- `db`: a `models.database.Database` on `tmp_path/test.db`, initialized with the full schema and
  closed afterwards. A real file (not `:memory:`) so backup and recovery can be tested.
- `db_with_area`: `db` plus one area with a default section; returns `(db, area_id, section_id)`.
- `db_with_client`: `db` plus one paired client; returns `(db, client_id, client_secret, secret_hash)`.
- `direct_admin_on`: direct admin access (HA login on :8100) switched on; it is off by default.
- Temporary directories for images, the hub instance id and the Hypothesis cache, so nothing is
  written into the repository or `/data`.

`profile_helpers.py`: `add_client` (a paired device with platform, profile, guest flag, tags and adjustments) and
`starter_profile` (the profile created from a starter suggestion).

## Layout by area

| Area | Files (pattern) | What they check |
| --- | --- | --- |
| Authentication and sessions | `test_auth_*`, `test_login_admin_only.py`, `test_admin_direct_access.py`, `test_ingress_trust.py`, `test_proxy_headers.py`, `test_ws_admin_session.py`, `test_ws_dev_auth.py`, `test_integration_token.py`, `test_reboot_token.py` | Rate limits, sessions and CSRF, clean 401 answers, Ingress trust by address, admin login only for HA admins over TLS, the dedicated integration token |
| Pairing and onboarding | `test_pairing_*`, `test_onboarding.py`, `test_guest_invites.py`, `test_client_platform_fixed.py`, `test_ws_hardening.py`, `test_input_hardening.py` | PIN window and limits, the pairing deep link and QR camera, setting up a device from a paired device, guest invitations, the platform fixed at pairing, input bounds |
| Devices and their HA users | `test_device_users.py`, `test_guest_fail_closed.py`, `test_guest_area_scope.py`, `test_guest_expiry_tz.py`, `test_kid_token.py`, `test_client_*`, `test_compliance.py`, `test_remote_access.py`, `test_insecure_http.py`, `test_home_networks.py`, `test_watch_access.py`, `test_child_watch.py` | One HA user per device, token delivery, delete and reset handshake, signed compliance status, what a device may read, guest areas and expiry, remote access, watches |
| WebSocket | `test_websocket.py`, `test_broadcast.py`, `test_layout_broadcast_scope.py`, `test_live_tag_broadcast.py`, `test_sync_broadcast.py`, `test_ws_integration_push.py`, `test_ws_no_service_proxy.py`, `test_protocol_version.py` | Broadcast audiences, area-scoped layout events, the integration session, unknown message types, protocol and app versions |
| Areas, layout and tiles | `test_area_*`, `test_section_capacity.py`, `test_update_null.py`, `test_tiles.py`, `test_quick_access.py`, `test_room_climate.py`, `test_scene_targets.py`, `test_route_resolution.py`, `test_schemas.py`, `test_slug.py` | Validation and bounds, reorder, visibility by tags, tile settings, quick access, room climate, request schemas, entity id slugs |
| Import, export and images | `test_export_round_trip.py`, `test_import_*`, `test_home_image_*`, `test_handler_transactions.py` | Lossless round trip (Hypothesis), per-area savepoints, crash-safe image replacement inside the image directory |
| Profiles and kids rules | `test_profile_*`, `test_profiles_api.py`, `test_home_header.py`, `test_kids_mode.py`, `test_kids_rules.py`, `test_enforcement.py`, `test_access.py`, `test_access_requests.py`, `test_location.py` | The resolver against the contract vectors (resolve, ha-access, no-escalation, expiry) and Hypothesis properties, the admin API, parental controls, enforcement in HA, access hours and early access, location |
| The hub path | `test_hub_path.py`, `test_hub_images.py`, `test_kid_token.py`, `test_kids_mode.py` | "Hub only" devices against a fake Home Assistant: scope of every read and of `control`, no grant for non-children, REST twin, closed access, a watch secret, no escalation, read and image limits, still images, key withdrawal until the hub has received the replacement and the integration has removed the old token, the end of a temporary adjustment |
| Family | `test_family.py`, `test_family_hardening.py` | Family manager rights and scope, the away route of the integration, what the child and the asking device learn |
| Notifications | `test_notification*.py` | Types, presentation fields, popup modes, dismiss, bounds, handover after an outage, notifications for watches |
| Home Assistant side | `test_sensor_*`, `test_config_sensor_active.py`, `test_integration_rest.py`, `test_integration_ack.py`, `test_discovery.py`, `test_ha_url_detect.py`, `test_panel.py`, `test_panel_commands_and_scan_tags.py`, `test_resilience.py`, `test_supervisor_role.py`, `test_mdns.py` | Sensor shapes and ids, the integration REST surface, Supervisor discovery and role, wall panel status and commands, scan tags, reconnect and resync after an HA restart, Bonjour |
| Log and audit | `test_event_log.py`, `test_audit_view.py`, `test_log_redaction.py`, `test_setup_assistant.py` | The log tab, alarms and retention, the audit view, no credentials in the hub log |
| Database and migrations | `test_database.py`, `test_db_*`, `test_migrations.py`, `test_data_safety.py`, `test_backup_exclude.py` | Schema, transactions on the shared connection, the transaction lint, snapshots and recovery, start errors and health, versioned migrations, keys outside HA backups |
| Crypto and TLS | `test_crypto.py`, `test_crypto_vectors.py`, `test_tls.py` | Token encryption at rest, the contract crypto vectors, the self-signed certificate and the TLS 1.3 floor |
| Contract and hub identity | `test_hub_identity_and_access_fields.py`, plus every file that reads `CONTRACTS_DIR` | Data files, value schemas and vectors match the pinned contract; answers validate against its schemas |
| Image, static files and release | `test_image_base.py`, `test_static_serving.py`, `test_mdi_brand_icons.py`, `test_release_pipeline.py` | The pinned base image, static serving and gzip, no brand icons in the MDI tables, the release workflows |

## Fixture data

- `fixtures/profiles/` and `fixtures/climate/`: byte copies of the contract's `fixtures/profiles` and
  `fixtures/climate`. `scripts/ci/contracts-check.sh` fails when they differ.
- `fixtures/schema_v8.sql`: a frozen v8 schema, the starting point of the migration tests.

## Related: Playwright end-to-end tests

The admin UI has its own Playwright suite in `../../frontend/e2e`. It mocks `/api` and the WebSocket in
the browser and runs without this backend.
