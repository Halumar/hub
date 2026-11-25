"""Further networks that count as at home (contracts docs/protocol.md).

Built in, every component treats loopback, link-local, RFC 1918 and fc00::/7
as the home network. The admin can add up to ten more networks (e.g.
Tailscale 100.64.0.0/10). The stored list goes to every app
(``client_config.home_networks``), into every rule snapshot of the
integration (``home_networks``, part of ``rules_hash``) and into the pairing
link (``hn``). Normalizing is a 1:1 port of the contract reference
``tools/home_networks_ref.py`` (vectors ``fixtures/network/home-networks``).
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any

from models.database import Database

SETTING_KEY = "home_networks"
MAX_NETWORKS = 10
MAX_NAME = 40
ALLOWED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)

MESSAGES = {
    "home_network_invalid": "{cidr} is not a network in CIDR notation (e.g. 100.64.0.0/10)",
    "home_network_public": (
        "{cidr} is not a private network; allowed are 10.0.0.0/8, 172.16.0.0/12, "
        "192.168.0.0/16, 100.64.0.0/10 and fc00::/7"
    ),
    "home_networks_too_many": "at most 10 networks",
}


class HomeNetworkError(ValueError):
    """Refused input; ``code`` is the 422 detail code of the contract."""

    def __init__(self, code: str, cidr: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.cidr = cidr

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "message": MESSAGES[self.code].format(cidr=self.cidr),
        }
        if self.cidr is not None:
            out["cidr"] = self.cidr
        return out


def parse(cidr: Any) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """One network with the host bits cleared; raises HomeNetworkError."""
    text = cidr.strip() if isinstance(cidr, str) else ""
    if "/" not in text:
        raise HomeNetworkError("home_network_invalid", str(cidr))
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        raise HomeNetworkError("home_network_invalid", str(cidr)) from None
    if not any(
        net.version == a.version and net.subnet_of(a)  # type: ignore[arg-type]
        for a in ALLOWED
    ):
        raise HomeNetworkError("home_network_public", str(cidr))
    return net


def _name(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()[:MAX_NAME]
    return text or None


def normalize(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored form of the admin input; raises HomeNetworkError.

    Host bits cleared, duplicates and networks inside another merged into the
    larger one (its name wins, else the first name found), sorted (IPv4
    before IPv6, then by address), at most ten.
    """
    if len(entries) > MAX_NETWORKS:
        raise HomeNetworkError("home_networks_too_many")
    parsed = [(parse(e.get("cidr")), _name(e.get("name"))) for e in entries]
    order = sorted(
        range(len(parsed)), key=lambda i: (parsed[i][0].version, parsed[i][0].prefixlen)
    )
    kept: list[tuple[Any, str | None]] = []
    for i in order:
        net, name = parsed[i]
        outer = next(
            (k for k in kept if k[0].version == net.version and net.subnet_of(k[0])),
            None,
        )
        if outer is None:
            kept.append((net, name))
        elif outer[1] is None and name is not None:
            kept[kept.index(outer)] = (outer[0], name)
    kept.sort(key=lambda k: (k[0].version, int(k[0].network_address), k[0].prefixlen))
    return [{"cidr": str(net), "name": name} for net, name in kept]


def contains(networks: list[str], address: str) -> bool:
    """Whether an IP address lies inside one of the networks (mapped IPv6 as IPv4)."""
    try:
        ip = ipaddress.ip_address(address.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in ipaddress.ip_network(n) for n in networks)


async def get(db: Database) -> list[dict[str, Any]]:
    """The stored list (never raises: a broken setting counts as empty)."""
    raw = await db.get_setting(SETTING_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return normalize(data if isinstance(data, list) else [])
    except (ValueError, TypeError, AttributeError):
        return []


async def cidrs(db: Database) -> list[str]:
    """client_config / snapshot / pairing link form: the CIDRs only."""
    return [n["cidr"] for n in await get(db)]


async def save(db: Database, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize and store; raises HomeNetworkError (nothing stored then)."""
    networks = normalize(entries)
    await db.set_setting(SETTING_KEY, json.dumps(networks, ensure_ascii=False))
    return networks
