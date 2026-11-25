"""Canonical pairing deep link (single source for every QR/link surface).

Both QR producers (the web UI pairing card and the integration's camera PNG)
and the copy-link button emit EXACTLY the same string, built here:

    halumar://pair?url=<percent-encoded http(s) base URL, no trailing slash>
                   &pin=<XXXX-XXXX>[&tls_port=<int>&fp=<64 lowercase hex>]
                   [&hn=<percent-encoded comma-separated home networks>]

``fp``/``tls_port`` are appended whenever a TLS certificate exists so the app
can TOFU-pin the hub's self-signed cert at pairing. Host policy is
local-only: this module only resolves the reachable local base URL.
"""

import urllib.parse

from fastapi import Request

from models.database import Database
from namespace import DEEP_LINK_PAIR


def build_pairing_deep_link(
    base_url: str, pin: str, home_networks: list[str] | None = None
) -> str:
    """Build the canonical halumar:// pairing deep link.

    The base URL is stripped of any trailing slash and fully percent-encoded.
    ``tls_port``/``fp`` are included only when a certificate actually exists
    (never a fake value); fp = lowercase hex SHA-256 of the DER leaf cert.
    """
    from services import tls

    encoded_url = urllib.parse.quote(base_url.rstrip("/"), safe="")
    link = f"{DEEP_LINK_PAIR}?url={encoded_url}&pin={pin}"

    fp = tls.get_fingerprint()
    if fp:
        link += f"&tls_port={tls.TLS_PORT}&fp={fp}"
    if home_networks:
        # Lets the app accept a hub host inside them before pairing.
        link += "&hn=" + urllib.parse.quote(",".join(home_networks), safe="")
    return link


async def resolve_base_url(request: Request, db: Database) -> str:
    """Resolve the base URL under which paired clients can reach the hub.

    Resolution order:
    1. The configured ``app_url`` (Settings UI).
    2. Only when NOT an Ingress request: ``X-Forwarded-Host``/``-Proto``.
    3. Only when NOT an Ingress request: a routable ``Host`` header
       (scheme taken from the request).
    4. Host from the ``ha_internal_url`` setting,
    5. else local IP detection (same source as mDNS);
       for 4/5 ``https://<host>:8100`` when a TLS certificate exists,
       ``http://<host>:8099`` otherwise.

    Web-UI requests run through HA Ingress, where ``X-Forwarded-*``/``Host``
    point at HA ``:8123`` — encoding those would produce an unreachable URL,
    hence the Ingress skip for steps 2 and 3 (Ingress therefore always gets
    the TLS URL from 4/5 when a certificate exists).
    """
    app_url = getattr(request.app.state, "app_url", "") or ""
    if app_url:
        return app_url.rstrip("/")

    from api.auth import _is_trusted_ingress

    if not _is_trusted_ingress(request):
        # Reverse-proxy headers point at the externally reachable host.
        forwarded_host = request.headers.get("x-forwarded-host")
        if forwarded_host:
            scheme = request.headers.get("x-forwarded-proto") or "http"
            return f"{scheme}://{forwarded_host}"

        # Direct access: use the Host header if it points to a routable
        # address. Reject localhost, loopback, and Docker-internal networks
        # (172.17.x for docker0, 172.30.x for HA Supervisor). Other 172.x
        # addresses (e.g. 172.16.x LAN) are allowed.
        host_header = request.headers.get("host", "")
        host_name = host_header.split(":")[0] if host_header else ""
        _non_routable = (
            not host_name
            or host_name in ("localhost", "127.0.0.1", "0.0.0.0")
            or host_name.startswith("172.17.")
            or host_name.startswith("172.30.")
            or "." not in host_name
        )
        if not _non_routable:
            return f"{request.url.scheme}://{host_header}"

    ha_internal_url = await db.get_setting("ha_internal_url")
    host = urllib.parse.urlsplit(ha_internal_url).hostname if ha_internal_url else None
    if not host:
        from services.mdns import _get_local_ip

        host = _get_local_ip()
    return _lan_base_url(host)


async def resolve_lan_base_url(app, db: Database) -> str:
    """Base URL for a pairing link built outside an HTTP request.

    Guest invitations are created over an app's WebSocket: the configured
    ``app_url`` wins, else the host of ``ha_internal_url``, else the local IP
    (steps 1, 4 and 5 of :func:`resolve_base_url`).
    """
    app_url = getattr(app.state, "app_url", "") or ""
    if app_url:
        return app_url.rstrip("/")
    ha_internal_url = await db.get_setting("ha_internal_url")
    host = urllib.parse.urlsplit(ha_internal_url).hostname if ha_internal_url else None
    if not host:
        from services.mdns import _get_local_ip

        host = _get_local_ip()
    return _lan_base_url(host)


def _lan_base_url(host: str) -> str:
    """Base URL on the LAN: TLS port whenever a certificate exists.

    Apps pair over ``wss://host:8100`` and pin ``fp``; the plain ``:8099``
    is only the fallback without a certificate.
    """
    from services import tls

    if tls.get_fingerprint():
        return f"https://{host}:{tls.TLS_PORT}"
    return f"http://{host}:8099"
