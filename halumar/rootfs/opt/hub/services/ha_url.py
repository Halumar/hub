"""The Home Assistant URLs handed to apps.

Internal URL when HA has no ``internal_url`` set: HA does not have to listen
on 8123: the Supervisor knows the real port and whether HA serves TLS
(GET /core/info -> ``port``, ``ssl``), so the URL also works on instances
that do not listen on 8123.

External URL: HA's ``external_url`` or, without one, the Nabu Casa remote UI
address, reported by the integration (see below).
"""

import logging
from urllib.parse import urlsplit

logger = logging.getLogger("hub.ha_url")

DEFAULT_HA_PORT = 8123


async def detect_internal_url(ha_client, local_ip: str) -> str:
    """Build ``http(s)://<local_ip>[:port]`` from the Supervisor's core info.

    Falls back to http and 8123 if the Supervisor cannot be asked (manual
    setups without a Supervisor). The default port of the scheme is omitted.
    """
    port, ssl = DEFAULT_HA_PORT, False
    try:
        info = await ha_client.get_core_info()
        port = int(info.get("port") or DEFAULT_HA_PORT)
        ssl = bool(info.get("ssl"))
    except Exception as e:  # noqa: BLE001 - fallback is logged
        logger.warning(
            "Could not read HA port/ssl from the Supervisor (%s); assuming http:%d",
            e,
            DEFAULT_HA_PORT,
        )
    scheme = "https" if ssl else "http"
    if (scheme, port) in (("http", 80), ("https", 443)):
        return f"{scheme}://{local_ip}"
    return f"{scheme}://{local_ip}:{port}"


# ── External URL ────────────────────────────────────────────────
#
# Settings: ``ha_url_detected`` is what Home Assistant reports (its
# ``external_url``, else the Nabu Casa remote UI address: the integration
# asks HA's ``get_url`` and reports it, POST /api/integration/ha-url).
# ``ha_url`` is the URL handed to devices with remote access: the detected
# one, unless the admin set a different one in the hub (manual override wins).

MAX_URL_LENGTH = 2048
NABU_CASA_SUFFIX = ".ui.nabu.casa"


def normalize_external_url(value: object) -> str | None:
    """``scheme://host[:port][/path]`` without trailing slash, or None.

    Only http(s), a host, no credentials, query or fragment.
    """
    if not isinstance(value, str):
        return None
    url = value.strip().rstrip("/")
    if not url or len(url) > MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on an invalid port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    return url


def is_nabu_casa_url(url: str) -> bool:
    """``https://<instance>.ui.nabu.casa`` exactly (no port, no path)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    label = host[: -len(NABU_CASA_SUFFIX)] if host.endswith(NABU_CASA_SUFFIX) else ""
    return (
        parts.scheme == "https"
        and bool(label)
        and "." not in label
        and parts.port is None
        and parts.path in ("", "/")
    )


class UrlCheckUnavailable(Exception):
    """Home Assistant gave no definite answer (restart, socket not up yet):
    the report is neither accepted nor refused, the integration retries."""


async def reported_url_acceptable(ha_client, url: str) -> bool:
    """Whether a URL reported by the integration may be handed to devices.

    The integration token only allows what Home Assistant itself would
    answer: its configured ``external_url`` (read by the hub from the
    Supervisor) or the Nabu Casa remote UI address of this instance (the hub
    asks HA's ``cloud/status`` itself). The token holder cannot point the
    devices' HA token to an arbitrary host, not even to another instance's
    Nabu Casa address; the admin can still set any URL in the hub.

    False only on a definite answer (another URL, not logged in to Home
    Assistant Cloud, another remote domain, no cloud integration). Raises
    :class:`UrlCheckUnavailable` when Home Assistant cannot be asked: the
    integration keeps a refusal and would never send the URL again.
    """
    try:
        config = await ha_client.get_config()
    except Exception as e:  # noqa: BLE001 - no definite answer yet
        logger.warning("Could not read HA's external_url to check a report: %s", e)
        config = None
    configured = normalize_external_url((config or {}).get("external_url"))
    if configured and configured == url:
        return True
    if not is_nabu_casa_url(url):
        if config is None:
            raise UrlCheckUnavailable("external_url not readable")
        return False
    try:
        domain = await ha_client.get_cloud_remote_domain()
    except Exception as e:  # noqa: BLE001 - no definite answer yet
        logger.warning("Could not read HA's cloud remote domain: %s", e)
        raise UrlCheckUnavailable("cloud/status not answered") from e
    return bool(domain) and domain == (urlsplit(url).hostname or "").lower()


async def apply_detected_external_url(db, url: str | None) -> bool:
    """Store the detected external URL; True if the URL devices get changed.

    The active ``ha_url`` follows the detection unless the admin overrode it
    (active value differs from the previous detection).
    """
    previous_detected = await db.get_setting("ha_url_detected")
    active = await db.get_setting("ha_url")
    if previous_detected == url:
        return False
    follows_detection = not active or active == previous_detected
    async with db.transaction():
        if url:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                ("ha_url_detected", url),
            )
        else:
            await db.execute("DELETE FROM settings WHERE key = 'ha_url_detected'")
        if follows_detection:
            if url:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    ("ha_url", url),
                )
            else:
                await db.execute("DELETE FROM settings WHERE key = 'ha_url'")
    if follows_detection and active != url:
        logger.info("External HA URL now %s", "detected" if url else "not available")
        return True
    return False
