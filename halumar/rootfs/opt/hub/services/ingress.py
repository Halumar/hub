"""Single source of truth for HA Ingress trust (REST and WebSocket).

The Supervisor Ingress proxy is exactly one host: ``172.30.32.2``. Every other
add-on container lives in the same ``172.30.32.0/23`` network, and with
``host_network: true`` every host process reaches the hub over loopback, so
neither of them may be trusted on the strength of a header anyone can set.
Requests carrying ``X-Ingress-Path`` from any other source are
treated as ordinary external requests (the header is ignored).

Loopback and the rest of the hassio network are only trusted with the explicit
development flag ``HUB_DEV`` (default off; the Supervisor offers no way to
set container environment variables, so a user cannot enable it by accident).
"""

import ipaddress
import logging
import os

logger = logging.getLogger("hub.ingress")

# The Supervisor's Ingress proxy (HA developer docs: "only allow connections
# from 172.30.32.2").
SUPERVISOR_INGRESS_IP = ipaddress.ip_address("172.30.32.2")

# Additionally trusted ONLY with the dev flag (local development, lab).
DEV_TRUSTED_NETWORKS = (
    ipaddress.ip_network("172.30.32.0/23"),  # whole hassio network
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
)

DEV_FLAG_ENV = "HUB_DEV"
_FALSE_VALUES = ("", "0", "false", "no", "off")


def dev_mode() -> bool:
    """Return True when the explicit development flag is set."""
    return os.environ.get(DEV_FLAG_ENV, "").strip().lower() not in _FALSE_VALUES


def ip_is_trusted(client_ip: str) -> bool:
    """Return True if client_ip is the Supervisor Ingress proxy.

    Numeric comparison via ipaddress (an IPv4-mapped IPv6 address is unwrapped).
    An unparseable address fails closed. With the dev flag, loopback and the
    whole hassio network are trusted as well.
    """
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip == SUPERVISOR_INGRESS_IP:
        return True
    if dev_mode():
        return any(ip in net for net in DEV_TRUSTED_NETWORKS)
    return False


def is_trusted_ingress(conn, kind: str = "HTTP") -> bool:
    """Return True if a Request/WebSocket is a genuine HA Ingress connection.

    Requires the ``X-Ingress-Path`` header AND the source IP of the TCP
    connection (never ``X-Forwarded-For``) to be the Supervisor Ingress proxy.
    A header from any other source is rejected and logged.
    """
    ingress_path = conn.headers.get("x-ingress-path")
    if not ingress_path:
        return False

    client_ip = conn.client.host if conn.client else ""
    if ip_is_trusted(client_ip):
        return True

    logger.warning(
        "Rejected Ingress request from untrusted IP: %s (%s, header: %s)",
        client_ip,
        kind,
        ingress_path[:80],
    )
    return False
