"""mDNS/Bonjour service announcement for Halumar.

Announces the hub on the local network so apps find it without typing an IP.
Two hubs in one LAN (test + production, neighbours sharing a network) must be
distinguishable and must never mix host and fingerprint of different hubs:

* **Instance name** ``<home title> (<short id>)``, e.g. ``Home (3f9a1c)``.
  The short id comes from a stable per-installation UUID (``iid``). A name
  conflict is resolved by zeroconf's probing (``allow_name_change``).
* **Host name** ``hub-<short id>.local.``; if another responder already
  answers for it with a different address, ``-2``, ``-3`` … is appended.
* **TXT** ``ws_path``, ``api_path``, ``iid`` (full instance id), ``version``,
  ``pv``/``pv_min`` (protocol version) and, when a certificate
  exists, ``tls``/``tls_port``/``fp``. Host (SRV/A)
  and ``fp`` come from this one registration, so they always belong together.

Uses ``AsyncZeroconf`` on the hub's event loop. Registration (including the
host probe) runs as a background task so the hub start is not delayed.
Without a network address yet (``127.0.0.1``) it waits for one; afterwards the
address is checked every :data:`ADDRESS_CHECK_SECONDS` and the announcement
follows a change (DHCP); zeroconf rescans its interfaces first
(``async_update_interfaces``), since it binds to those present at creation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import socket
import sqlite3
import uuid

from zeroconf import AddressResolverIPv4, IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from brand import BRAND_NAME
from namespace import BONJOUR_SERVICE_TYPE
from services.protocol import txt_fields

logger = logging.getLogger("hub.services.mdns")

SERVICE_TYPE = BONJOUR_SERVICE_TYPE
# Host name "hub-<short id>.local." (functional, not part of the namespace)
HOST_PREFIX = "hub"
FALLBACK_TITLE = BRAND_NAME

# Stable instance id, created once per installation (kept in backups: a
# restored hub is the same hub).
INSTANCE_ID_PATH = os.environ.get("HUB_INSTANCE_ID_PATH", "/data/.instance_id")
SHORT_ID_LEN = 6

# DNS label limit for the instance part of the service name (bytes).
MAX_INSTANCE_LABEL = 63
# Probe for a foreign responder on our host name (seconds per candidate).
HOST_PROBE_TIMEOUT = 1.0
MAX_HOST_CANDIDATES = 10
# How often the announced address is compared with the current one (seconds).
ADDRESS_CHECK_SECONDS = 30.0
LOOPBACK = "127.0.0.1"

_aiozc: AsyncZeroconf | None = None
_service_info: AsyncServiceInfo | None = None
_register_task: asyncio.Task | None = None
_address_task: asyncio.Task | None = None


def _get_local_ip() -> str:
    """Get the local IP address of this machine."""
    try:
        # Create a socket and connect to an external address to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            # Doesn't actually send data, just determines which interface to use
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# path -> (id, persisted) for the life of the process: the id never changes
# while the hub runs, also when the file cannot be written.
_instance_ids: dict[str, tuple[str, bool]] = {}


def _write_instance_id(path: str, iid: str) -> bool:
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w") as f:
            f.write(iid + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        if path not in _instance_ids:
            # Logged once; every later call tries the write again.
            logger.warning(
                "Could not persist instance id at %s: %s; keeping %s for this run "
                "and retrying",
                path,
                e,
                iid,
            )
        return False


def load_instance_id(path: str = INSTANCE_ID_PATH) -> str:
    """Return the stable instance id, creating it on first use.

    The id stays the same for the life of the process. When the file cannot
    be written the id is still kept in memory (the status, Bonjour and
    onboarding name the same hub on every call) and the write is retried on
    the next call.
    """
    cached = _instance_ids.get(path)
    if cached is not None:
        iid, persisted = cached
        if not persisted and _write_instance_id(path, iid):
            logger.info("Instance id persisted at %s", path)
            _instance_ids[path] = (iid, True)
        return iid
    try:
        with open(path) as f:
            iid = str(uuid.UUID(f.read().strip()))
        _instance_ids[path] = (iid, True)
        return iid
    except (OSError, ValueError):
        pass
    iid = str(uuid.uuid4())
    persisted = _write_instance_id(path, iid)
    _instance_ids[path] = (iid, persisted)
    return iid


def short_id(instance_id: str) -> str:
    return instance_id.replace("-", "")[:SHORT_ID_LEN].lower()


def instance_label(title: str | None, sid: str) -> str:
    """``<title> (<sid>)`` within the 63-byte DNS label limit."""
    clean = re.sub(r"[\x00-\x1f\x7f.]+", " ", title or "").strip() or FALLBACK_TITLE
    suffix = f" ({sid})"
    budget = MAX_INSTANCE_LABEL - len(suffix.encode())
    encoded = clean.encode()[:budget]
    clean = encoded.decode("utf-8", errors="ignore").rstrip()
    return f"{clean}{suffix}"


def host_name(sid: str, n: int = 1) -> str:
    return (
        f"{HOST_PREFIX}-{sid}.local." if n <= 1 else f"{HOST_PREFIX}-{sid}-{n}.local."
    )


async def _home_title(db) -> str | None:
    """Home title (Settings, default HA location name)."""
    if db is not None:
        return await db.get_setting("home_title") or await db.get_setting(
            "ha_location_name"
        )
    # No Database handed in: read the settings table directly, read-only.
    path = os.environ.get("HUB_DB_PATH", "/data/hub.db")
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = dict(
                con.execute(
                    "SELECT key, value FROM settings "
                    "WHERE key IN ('home_title', 'ha_location_name')"
                ).fetchall()
            )
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.debug("Could not read home title for mDNS: %s", e)
        return None
    return rows.get("home_title") or rows.get("ha_location_name")


async def _free_host(aiozc: AsyncZeroconf, sid: str, own_ip: str) -> str:
    """First host name candidate no other responder answers for."""
    for n in range(1, MAX_HOST_CANDIDATES + 1):
        candidate = host_name(sid, n)
        resolver = AddressResolverIPv4(candidate)
        found = await resolver.async_request(aiozc.zeroconf, HOST_PROBE_TIMEOUT * 1000)
        foreign = set(resolver.parsed_addresses()) - {own_ip} if found else set()
        if not foreign:
            return candidate
        logger.warning(
            "mDNS host %s is already used by %s, trying the next name",
            candidate,
            ", ".join(sorted(foreign)),
        )
    return host_name(f"{sid}-{uuid.uuid4().hex[:4]}")


def build_properties(
    instance_id: str, version: str, app_url: str = ""
) -> dict[str, str]:
    """TXT record of the announcement."""
    properties = {
        "ws_path": "/api/ws",
        "api_path": "/api",
        "iid": instance_id,
        "version": version,
        **txt_fields(),
    }
    # Include app_url if configured (tells clients the external base URL)
    if app_url:
        properties["app_url"] = app_url

    # TLS fingerprint for pinning, only when a certificate exists.
    from services.tls import TLS_PORT, get_fingerprint

    fp = get_fingerprint()
    if fp:
        properties["tls"] = "1"
        properties["tls_port"] = str(TLS_PORT)
        properties["fp"] = fp
    return properties


async def _register(
    version: str, port: int, app_url: str, db, instance_id_path: str
) -> None:
    global _service_info
    local_ip = _get_local_ip()
    if local_ip == LOOPBACK:
        logger.warning("No network address yet - mDNS waits for one")
    waited = local_ip == LOOPBACK
    while local_ip == LOOPBACK:
        await asyncio.sleep(ADDRESS_CHECK_SECONDS)
        local_ip = _get_local_ip()
    if waited:
        # The sockets were bound to the interfaces present at creation.
        await _update_interfaces()

    instance_id = load_instance_id(instance_id_path)
    sid = short_id(instance_id)
    label = instance_label(await _home_title(db), sid)
    host = await _free_host(_aiozc, sid, local_ip)

    info = AsyncServiceInfo(
        type_=SERVICE_TYPE,
        name=f"{label}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties=build_properties(instance_id, version, app_url),
        server=host,
    )
    # Probing renames the instance ("… (2)") if the name is taken.
    await _aiozc.async_register_service(info, allow_name_change=True)
    _service_info = info
    logger.info(
        "mDNS service registered: %s on %s (%s:%d, iid %s)",
        info.name,
        host,
        local_ip,
        port,
        instance_id,
    )


async def _register_logged(*args) -> None:
    global _address_task
    try:
        await _register(*args)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - discovery is optional
        logger.error("Failed to start mDNS service: %s", e)
        return
    _address_task = asyncio.create_task(_follow_address())


async def _update_interfaces() -> None:
    """Bind zeroconf to the interfaces present now (it keeps the ones it saw
    when it was created): a late or changed address is announced on it."""
    try:
        await _aiozc.async_update_interfaces()
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - the old sockets keep working
        logger.warning("mDNS interface rescan failed: %s", e)


def _announced_ip() -> str | None:
    addresses = getattr(_service_info, "addresses", None) or []
    return socket.inet_ntoa(addresses[0]) if addresses else None


async def _follow_address() -> None:
    """Announce the new address after a change (DHCP, network came up late)."""
    while _aiozc is not None and _service_info is not None:
        await asyncio.sleep(ADDRESS_CHECK_SECONDS)
        ip = _get_local_ip()
        old = _announced_ip()
        if ip == LOOPBACK or ip == old:
            continue
        before = list(_service_info.addresses)
        await _update_interfaces()
        try:
            _service_info.addresses = [socket.inet_aton(ip)]
            await _aiozc.async_update_service(_service_info)
            logger.info("mDNS address changed: %s -> %s", old, ip)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - retried at the next check
            _service_info.addresses = before
            logger.warning("mDNS address update to %s failed: %s", ip, e)


async def start_mdns(
    version: str = "",
    port: int = 8099,
    app_url: str = "",
    *,
    db=None,
    instance_id_path: str | None = None,
) -> None:
    """Start the mDNS announcement (returns at once, registers in background).

    Args:
        version: Halumar version string (TXT ``version``)
        port: Server port number
        app_url: Configured app URL (TXT ``app_url``)
        db: Database for the home title; without it the settings table is
            read directly (read-only).
        instance_id_path: override of the instance id file (tests)
    """
    global _aiozc, _register_task
    if _aiozc is not None:
        return
    try:
        _aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    except Exception as e:  # noqa: BLE001 - no multicast socket: no discovery
        logger.error("Failed to start mDNS service: %s", e)
        _aiozc = None
        return
    _register_task = asyncio.create_task(
        _register_logged(
            version, port, app_url, db, instance_id_path or INSTANCE_ID_PATH
        )
    )


async def stop_mdns() -> None:
    """Stop mDNS service announcement."""
    global _aiozc, _service_info, _register_task, _address_task

    for task in (_register_task, _address_task):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    _register_task = _address_task = None
    if _aiozc is None:
        return
    try:
        if _service_info is not None:
            await _aiozc.async_unregister_service(_service_info)
        await _aiozc.async_close()
        logger.info("mDNS service unregistered")
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to stop mDNS service: %s", e)
    finally:
        _aiozc = None
        _service_info = None
