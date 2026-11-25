"""Where the integration token is accepted from.

Under the Supervisor the companion integration runs in Home Assistant Core on
the same host as the hub (``host_network``) and reaches it host-internally
(``local-<slug>``, i.e. the hassio bridge address; Docker hands the hub the
host's own address as peer). The integration token lives in plaintext in the
HA config entry, i.e. in every HA Core backup. It is therefore only accepted
from an address of this host itself, never from the LAN or from other add-on
containers.

"Address of this host" is decided by trying to bind a socket to the peer
address: the kernel allows that only for local addresses (no
``ip_nonlocal_bind`` on HA OS). Results are cached briefly.

Without the Supervisor (development, tests) or with the explicit development
flag ``HUB_DEV`` every source is accepted.
"""

import ipaddress
import logging
import os
import socket
import time

from services.ingress import dev_mode

logger = logging.getLogger("hub.integration_source")

_CACHE_SECONDS = 300.0
_cache: dict[str, tuple[bool, float]] = {}


def enforced() -> bool:
    """Only under the Supervisor and without the development flag."""
    return bool(os.environ.get("SUPERVISOR_TOKEN")) and not dev_mode()


def _is_local_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.bind((str(ip), 0))
    except OSError:
        return False
    return True


def source_allowed(client_ip: str | None) -> bool:
    """True if the integration token may be used from ``client_ip``."""
    if not enforced():
        return True
    try:
        ip = ipaddress.ip_address(client_ip or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return True
    if ip.is_unspecified or ip.is_multicast:
        return False
    key = str(ip)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached[1] > now:
        return cached[0]
    allowed = _is_local_address(ip)
    if len(_cache) > 256:
        _cache.clear()
    _cache[key] = (allowed, now + _CACHE_SECONDS)
    return allowed


def reset_cache() -> None:
    _cache.clear()
