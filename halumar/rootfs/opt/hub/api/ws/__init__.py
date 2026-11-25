"""WebSocket handler for real-time updates and full client communication.

This is the primary communication channel for external clients (wall panels,
mobile apps). All data retrieval and pairing can be done entirely over
WebSocket - no REST API needed. Entity control is NOT proxied here: clients
call Home Assistant directly with their own HA token.

Authentication:
- Ingress connections (X-Ingress-Path header + trusted source IP): no auth required
- External connections: must provide a valid paired client secret via the
  first message {"type": "auth", "secret": "..."} (a ?token= query parameter
  is deliberately NOT supported — query strings leak into logs)
- Unauthenticated clients can send "pair" message with PIN to authenticate
- Companion HA integration: presents "Authorization: Bearer <integration
  token>" on the handshake — verified BEFORE accept (invalid header => close
  4003 without accept). The session authenticates as client_id="integration",
  is READ-ONLY (only ping/pong inbound) and receives every ALL+ADMIN
  broadcast as a refresh trigger, with notification_created redacted to
  ids+counts (the PII contract of /api/integration/*).

Security:
- Max 100 concurrent WebSocket connections
- Unauthenticated connections are accepted but limited
- Max 5 pairing attempts per connection (anti-brute-force)
- Error messages are sanitized (no internal details leaked)
- Secrets are verified via SHA-256 hash comparison
- Ingress source IP validated against trusted Docker networks

Broadcast filtering:
- Events are classified by audience: ALL (every client), or ADMIN (only
  admin/ingress sessions). This prevents admin-only events (area created,
  client paired, etc.) from being sent to external wall panels and iOS apps.
- Admin sessions = ingress connections and dev-mode connections.
- Paired external clients only receive ALL-audience events (area_updated,
  layout_updated, client_config_updated).
- The integration push session receives ALL and ADMIN events (projected
  through _integration_view) but is neither an admin session nor a paired
  client — it never appears in connected-client sets or counts.

Client-to-Server message types:
- pair: Pair with PIN (unauthenticated clients only)
- auth: Authenticate with client secret
- ping: Keepalive
- subscribe: Subscribe to area updates
- get_areas: Load all areas with entity counts
- get_area: Load area detail with entities, widgets, and live HA states
- get_client_config: Get HA connection config (token, URLs) for this client
- client_status: Report client device status (battery, current area, location_shared, etc.)
- location_report: Position of a device with parental controls
- tag_resolve: What a scanned NFC/QR tag does, answer tag_result
- notification_ack: Acknowledge delivery of a notification
- notification_dismiss: Dismiss a notification server-side
- get_notifications: THE single WS content channel for this client's active
  notifications ({"type": "notifications", "notifications": [...]}, items in
  the display_notification push shape). Used for connect-time catch-up while
  the hub WS is reachable; when only HA is reachable, the app reads the
  SAME push-shaped items from the per-client HA notification sensor instead.

Package layout:
- registry.py   connected sockets (``_clients``), counts, disconnect
- broadcast.py  audiences, broadcast/send_to_client, rotation delivery,
                client_removed signal, ping loop
- handshake.py  endpoint, connection slots, auth/pair, message dispatch,
                guest expiry loop
- client.py     authenticated app/admin messages and their data helpers
- admin.py      integration push session (read-only)

This package is the one namespace of the WebSocket handler: the names below are
re-exported, and the modules look up ``broadcast``, ``send_to_client``,
``disconnect_client``, ``_is_ingress``, ``_auth_disabled`` and the
``*_GRACE_SECONDS`` timings through it at call time (``api_ws.<name>``), so
replacing ``api.ws.<name>`` (alias ``api.websocket``) still takes effect.
"""

import asyncio  # noqa: F401 - re-exported as api.websocket.asyncio

from api.ws.registry import (
    INTEGRATION_CLIENT_ID,
    _clients,
    _is_admin_client,
    disconnect_client,
    get_connected_client_ids,
    get_connected_count,
    get_integration_push_connected,
)
from api.ws.broadcast import (
    CONFIG_FETCH_GRACE_SECONDS,
    CONFIG_SWITCH_GRACE_SECONDS,
    RESET_ACK_GRACE_SECONDS,
    WS_PING_INTERVAL,
    WS_PING_TIMEOUT,
    EventAudience,
    _config_fetch_waiters,
    _integration_view,
    _notify_config_fetched,
    broadcast,
    announce_area_active,
    broadcast_area_scoped,
    deliver_rotated_config,
    notify_areas_changed,
    send_to_client,
    send_to_integration,
    signal_client_removed,
    wait_until_disconnected,
    ws_ping_loop,
)
from api.ws.client import (
    _CLIENT_STATUS_COLUMNS,
    _area_visible_for_client,
    _get_area_detail,
    _get_areas,
    _get_client_config,
    _get_image_base_url,
    _handle_client_message,
    _handle_client_status,
    _image_src,
    _purge_tombstone_on_ack,
)
from api.ws.admin import _handle_integration_message
from api.ws.handshake import (
    MAX_AUTH_ATTEMPTS,
    MAX_PAIR_ATTEMPTS,
    MAX_WS_CONNECTIONS_AUTH,
    MAX_WS_CONNECTIONS_UNAUTH,
    MAX_WS_MESSAGE_SIZE,
    MAX_WS_UNAUTH_PER_IP,
    SESSION_EXPIRED_CLOSE_CODE,
    UNAUTH_TIMEOUT_SECONDS,
    _auth_disabled,
    _handle_message,
    _handle_pair,
    _hash_secret,
    _ip_is_trusted,
    _is_ingress,
    _match_pending_deletion,
    _verify_client_secret,
    close_all_session_sockets,
    close_session_sockets,
    guest_expiry_loop,
    router,
    websocket_endpoint,
)

from services import device_provisioning as _device_provisioning

_device_provisioning.set_sender(send_to_integration)

__all__ = [
    "CONFIG_FETCH_GRACE_SECONDS",
    "CONFIG_SWITCH_GRACE_SECONDS",
    "EventAudience",
    "INTEGRATION_CLIENT_ID",
    "MAX_AUTH_ATTEMPTS",
    "MAX_PAIR_ATTEMPTS",
    "MAX_WS_CONNECTIONS_AUTH",
    "MAX_WS_CONNECTIONS_UNAUTH",
    "MAX_WS_MESSAGE_SIZE",
    "MAX_WS_UNAUTH_PER_IP",
    "RESET_ACK_GRACE_SECONDS",
    "UNAUTH_TIMEOUT_SECONDS",
    "WS_PING_INTERVAL",
    "WS_PING_TIMEOUT",
    "_CLIENT_STATUS_COLUMNS",
    "_area_visible_for_client",
    "_auth_disabled",
    "_clients",
    "_config_fetch_waiters",
    "_get_area_detail",
    "_get_areas",
    "_get_client_config",
    "_get_image_base_url",
    "_handle_client_message",
    "_handle_client_status",
    "_handle_integration_message",
    "_handle_message",
    "_handle_pair",
    "_hash_secret",
    "_image_src",
    "_integration_view",
    "_ip_is_trusted",
    "_is_admin_client",
    "_is_ingress",
    "close_all_session_sockets",
    "close_session_sockets",
    "SESSION_EXPIRED_CLOSE_CODE",
    "_match_pending_deletion",
    "_notify_config_fetched",
    "_purge_tombstone_on_ack",
    "_verify_client_secret",
    "broadcast",
    "announce_area_active",
    "broadcast_area_scoped",
    "notify_areas_changed",
    "deliver_rotated_config",
    "disconnect_client",
    "get_connected_client_ids",
    "get_connected_count",
    "get_integration_push_connected",
    "guest_expiry_loop",
    "router",
    "send_to_client",
    "send_to_integration",
    "signal_client_removed",
    "wait_until_disconnected",
    "websocket_endpoint",
    "ws_ping_loop",
]
