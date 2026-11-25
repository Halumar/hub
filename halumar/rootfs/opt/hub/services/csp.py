"""Content-Security-Policy of the HTML pages.

Under HA Ingress the Admin UI runs same-origin with the Home Assistant
frontend, so any script running in it can reach the HA admin's connection.
The policy therefore allows no inline script except the exact inline blocks
of the page itself (SvelteKit bootstrap, the injected ingress path), by
SHA-256 hash computed from the final HTML, and lets the page connect only to
its own origin (HTTP and WebSocket).
"""

import base64
import hashlib
import re

_INLINE_SCRIPT = re.compile(
    rb"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL
)
# Host header values that may appear in the policy (no spaces, quotes, ';').
_HOST = re.compile(
    r"^[A-Za-z0-9.\-]+(:[0-9]{1,5})?$|^\[[0-9A-Fa-f:.]+\](:[0-9]{1,5})?$"
)

DOCS_PATHS = ("/api/docs", "/api/redoc")


def inline_script_hashes(html: bytes) -> list[str]:
    """CSP source expressions for every inline ``<script>`` of ``html``."""
    hashes = []
    for match in _INLINE_SCRIPT.finditer(html):
        digest = hashlib.sha256(match.group(1)).digest()
        source = f"'sha256-{base64.b64encode(digest).decode()}'"
        if source not in hashes:
            hashes.append(source)
    return hashes


def websocket_sources(hosts: list[str | None]) -> list[str]:
    """``ws://host wss://host`` for each valid host (``'self'`` alone does not
    cover WebSockets in every browser)."""
    out: list[str] = []
    for host in hosts:
        if not host or not _HOST.match(host):
            continue
        for scheme in ("ws", "wss"):
            source = f"{scheme}://{host}"
            if source not in out:
                out.append(source)
    return out


def page_policy(script_hashes: list[str], ws_sources: list[str]) -> str:
    """Policy of the Admin UI pages."""
    return "; ".join(
        [
            "default-src 'self'",
            " ".join(["script-src 'self'", *script_hashes]),
            # Svelte sets style attributes; styles cannot run code.
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self'",
            " ".join(["connect-src 'self'", *ws_sources]),
            "worker-src 'self'",
            "frame-ancestors 'self'",
            "form-action 'self'",
            "object-src 'none'",
            "base-uri 'self'",
        ]
    )


def docs_policy() -> str:
    """Policy of the API docs (local Swagger UI / ReDoc assets, no CDN)."""
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            # ReDoc runs its search index in a blob: worker.
            "worker-src 'self' blob:",
            "frame-ancestors 'self'",
            "form-action 'self'",
            "object-src 'none'",
            "base-uri 'self'",
        ]
    )


def is_docs_path(path: str) -> bool:
    return path.rstrip("/") in DOCS_PATHS
