"""Client pairing API for WebSocket authentication.

Pairing flow:
1. Admin generates a pairing PIN via POST /api/pairing/generate
   - PIN format: XXXX-XXXX (8 digits with a hyphen)
   - PIN expires after configurable timeout (default 5 minutes)
2. New client sends {"type": "pair", "pin", "client_name"} over the WebSocket
   - If PIN is valid and not expired, client receives a permanent secret token
   - Secret format: 64-char hex string
   - Secret is stored as SHA-256 hash in the database
   - A client_paired event is broadcast via WebSocket to all admin clients
3. Client uses the secret for WebSocket auth (no REST access for clients)
4. Admin can list/revoke paired clients via GET/DELETE /api/pairing/clients

HA URLs are global settings (auto-detected from HA or manually configured).
Every device gets its own HA user and token from the companion integration
(services/device_provisioning.py); the hub stores the token encrypted.

Security:
- PIN is single-use and invalidated after MAX_PIN_FAILURES failed attempts
  from at least MIN_PIN_FAILURE_IPS source IPs
- Per-IP pairing budget across WS connections (PAIR_IP_LIMIT_*)
- PIN is not logged in plaintext
- Secret stored as hash (plaintext never persisted)

Package layout:
- pin.py             PIN store, rate limit, failures, generate/status/cancel
- clients.py         paired clients: list/read/update/revoke/delete/kick
- guests.py          member invitations, tag catalog, extending a guest
- settings.py        global settings (HA/app URL, home screen)
- tokens.py          per-device HA token rotation (via the integration)
- onboarding.py      onboarding by a paired device: running sessions,
                     cancel

Profiles, device adjustments, own area, remote PIN and bulk changes are in
api/profiles.py.

Routes are registered below in one table; the order is the matching order.
This package is the one namespace of the pairing API: ``IMAGE_DIR`` and
``_generate_pin`` are looked up
through it at call time (``api_pairing.<name>``), so replacing them on
``api.pairing`` still takes effect.
"""

from fastapi import APIRouter

from api.pairing.tokens import RotateTokenResponse, rotate_client_token
from api.pairing.pin import (
    GeneratePinRequest,
    GeneratePinResponse,
    PAIRING_TIMEOUT_SECONDS,
    MAX_PIN_FAILURES,
    MIN_PIN_FAILURE_IPS,
    PAIR_IP_LIMIT_MAX,
    PAIR_IP_LIMIT_WINDOW,
    PAIR_RATE_LIMIT_MAX,
    PAIR_RATE_LIMIT_WINDOW,
    _PAIR_RATE_MAX_KEYS,
    _active_pins,
    _cleanup_expired_pins,
    _generate_pin,
    _generate_secret,
    _get_client_ip,
    _hash_secret,
    _pair_rate_check,
    _pair_rate_limits,
    cancel_pairing,
    clear_active_pins,
    clear_ip_failures,
    generate_pairing_pin,
    get_active_pin,
    match_pin,
    pair_ip_allowed,
    pairing_status,
    register_pin_failure,
    remove_pin,
    retired_pin_reason,
    set_active_pin,
)
from api.pairing.clients import (
    ClientUpdate,
    DeviceActivityResponse,
    PairedClientResponse,
    client_leave,
    delete_all_clients,
    delete_client,
    dismiss_pending_client,
    get_area_visibility,
    get_client,
    get_client_activity,
    kick_client,
    list_paired_clients,
    purge_client_tombstone,
    revoke_client,
    set_access_switch,
    update_client,
)
from api.pairing.guests import (
    extend_guest,
    extend_guest_expiry,
    GuestInvitesResponse,
    cancel_guest_invite,
    get_all_tags,
    list_guest_invites,
)
from api.pairing.onboarding import (
    OnboardingSessionsResponse,
    cancel_onboarding_session,
    list_onboarding_sessions,
)
from api.pairing.settings import (
    AdminDirectAccessUpdate,
    AppUrlResponse,
    AppUrlUpdate,
    ComplianceEndpointUpdate,
    ControlModeSettingsUpdate,
    HAUrlsResponse,
    HAUrlsUpdate,
    HomeSettingsUpdate,
    IMAGE_DIR,
    PairingViaIntegrationUpdate,
    _image_src_setting,
    _safe_remove_image,
    delete_home_image,
    get_admin_direct_access_setting,
    get_app_url,
    get_compliance_endpoint_setting,
    get_control_mode_setting,
    get_ha_urls,
    get_home_settings,
    get_pairing_via_integration_setting,
    update_app_url,
    update_compliance_endpoint_setting,
    update_control_mode_setting,
    update_ha_urls,
    update_home_settings,
    update_pairing_via_integration_setting,
    update_admin_direct_access_setting,
    upload_home_image,
)

router = APIRouter(tags=["pairing"])

# Registration order = matching order. Keep literal paths before
# parameterized paths of the same shape.
router.post("/pairing/generate", response_model=GeneratePinResponse)(
    generate_pairing_pin
)
router.get("/pairing/clients", response_model=list[PairedClientResponse])(
    list_paired_clients
)
router.get("/pairing/clients/{client_id}", response_model=PairedClientResponse)(
    get_client
)
router.get(
    "/pairing/clients/{client_id}/activity", response_model=DeviceActivityResponse
)(get_client_activity)
router.patch("/pairing/clients/{client_id}", response_model=PairedClientResponse)(
    update_client
)
router.put(
    "/pairing/clients/{client_id}/access-switch", response_model=PairedClientResponse
)(set_access_switch)
router.get("/pairing/clients/{client_id}/area-visibility/{area_id}")(
    get_area_visibility
)
router.delete("/pairing/clients/{client_id}", status_code=204)(revoke_client)
router.delete("/pairing/clients/{client_id}/permanent", status_code=204)(delete_client)
router.post("/clients/{client_id}/dismiss-pending")(dismiss_pending_client)
router.delete("/pairing/clients/{client_id}/tombstone", status_code=204)(
    purge_client_tombstone
)
router.get("/pairing/status")(pairing_status)
router.delete("/pairing/cancel", status_code=204)(cancel_pairing)
router.get("/pairing/onboarding", response_model=OnboardingSessionsResponse)(
    list_onboarding_sessions
)
router.delete("/pairing/onboarding/{onboarding_id}", status_code=204)(
    cancel_onboarding_session
)
router.get("/pairing/guest-invites", response_model=GuestInvitesResponse)(
    list_guest_invites
)
router.delete("/pairing/guest-invites/{invite_id}", status_code=204)(
    cancel_guest_invite
)
router.get("/settings/ha-urls", response_model=HAUrlsResponse)(get_ha_urls)
router.put("/settings/ha-urls", response_model=HAUrlsResponse)(update_ha_urls)
router.get("/settings/app-url", response_model=AppUrlResponse)(get_app_url)
router.put("/settings/app-url", response_model=AppUrlResponse)(update_app_url)
router.get("/settings/control-mode")(get_control_mode_setting)
router.put("/settings/control-mode")(update_control_mode_setting)
router.get("/settings/pairing-via-integration")(get_pairing_via_integration_setting)
router.put("/settings/pairing-via-integration")(update_pairing_via_integration_setting)
router.get("/settings/compliance-endpoint")(get_compliance_endpoint_setting)
router.put("/settings/compliance-endpoint")(update_compliance_endpoint_setting)
router.get("/settings/admin-direct-access")(get_admin_direct_access_setting)
router.put("/settings/admin-direct-access")(update_admin_direct_access_setting)
router.get("/settings/home")(get_home_settings)
router.put("/settings/home")(update_home_settings)
router.post("/settings/home/image/{image_type}")(upload_home_image)
router.delete("/settings/home/image/{image_type}", status_code=204)(delete_home_image)
router.get("/tags")(get_all_tags)
router.patch("/clients/{client_id}/extend")(extend_guest)
router.post("/clients/{client_id}/kick", status_code=204)(kick_client)
router.post("/client/leave")(client_leave)
router.post(
    "/clients/{client_id}/rotate-token",
    response_model=RotateTokenResponse,
    response_model_exclude_none=True,
)(rotate_client_token)
router.delete("/pairing/clients", status_code=200)(delete_all_clients)

__all__ = [
    "AdminDirectAccessUpdate",
    "AppUrlResponse",
    "AppUrlUpdate",
    "ClientUpdate",
    "ComplianceEndpointUpdate",
    "ControlModeSettingsUpdate",
    "OnboardingSessionsResponse",
    "GeneratePinRequest",
    "GeneratePinResponse",
    "GuestInvitesResponse",
    "HAUrlsResponse",
    "HAUrlsUpdate",
    "HomeSettingsUpdate",
    "IMAGE_DIR",
    "PAIRING_TIMEOUT_SECONDS",
    "MAX_PIN_FAILURES",
    "MIN_PIN_FAILURE_IPS",
    "PAIR_IP_LIMIT_MAX",
    "PAIR_IP_LIMIT_WINDOW",
    "PAIR_RATE_LIMIT_MAX",
    "PAIR_RATE_LIMIT_WINDOW",
    "DeviceActivityResponse",
    "PairedClientResponse",
    "PairingViaIntegrationUpdate",
    "RotateTokenResponse",
    "_PAIR_RATE_MAX_KEYS",
    "_active_pins",
    "_cleanup_expired_pins",
    "_generate_pin",
    "_generate_secret",
    "_get_client_ip",
    "_hash_secret",
    "_image_src_setting",
    "_pair_rate_check",
    "_pair_rate_limits",
    "_safe_remove_image",
    "cancel_onboarding_session",
    "cancel_pairing",
    "clear_active_pins",
    "clear_ip_failures",
    "client_leave",
    "delete_all_clients",
    "delete_client",
    "delete_home_image",
    "dismiss_pending_client",
    "extend_guest",
    "extend_guest_expiry",
    "generate_pairing_pin",
    "get_active_pin",
    "get_all_tags",
    "get_admin_direct_access_setting",
    "get_app_url",
    "get_area_visibility",
    "get_client",
    "get_client_activity",
    "get_compliance_endpoint_setting",
    "get_control_mode_setting",
    "list_guest_invites",
    "cancel_guest_invite",
    "get_ha_urls",
    "get_home_settings",
    "get_pairing_via_integration_setting",
    "kick_client",
    "list_onboarding_sessions",
    "list_paired_clients",
    "match_pin",
    "pair_ip_allowed",
    "pairing_status",
    "purge_client_tombstone",
    "register_pin_failure",
    "retired_pin_reason",
    "remove_pin",
    "revoke_client",
    "rotate_client_token",
    "router",
    "set_active_pin",
    "update_admin_direct_access_setting",
    "update_app_url",
    "update_client",
    "update_compliance_endpoint_setting",
    "update_control_mode_setting",
    "update_ha_urls",
    "update_home_settings",
    "update_pairing_via_integration_setting",
    "upload_home_image",
]
