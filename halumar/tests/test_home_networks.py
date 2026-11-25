"""Further networks that count as at home.

Normalizing (1:1 with the contract reference, every vector of
fixtures/network/home-networks), GET/PUT /api/settings/home-networks
(422 codes, audit and log entry, push), client_config.home_networks, the
rule snapshot (rules_hash follows the list) and the pairing link ``hn``.
"""

import json
import re
import types
import urllib.parse
from pathlib import Path

import pytest
from fastapi import HTTPException

from api import home_networks as api
from api import ws
from api.ws.client import _get_client_config
from services import enforcement, event_log, tls
from services import home_networks as hn
from services.pairing_link import build_pairing_deep_link

from .profile_helpers import add_client
from .test_enforcement import (  # noqa: F401 - the fixture sent
    ADULT_USER,
    FakeHA,
    _contracts_root,
    _errors,
    _validator,
    sent,
)

TAILSCALE = [
    {"cidr": "100.64.0.0/10", "name": "Tailscale"},
    {"cidr": "fd7a:115c:a1e0::/48", "name": "Tailscale"},
]


def _req(db):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="ingress", actor="Admin"),
        app=types.SimpleNamespace(state=types.SimpleNamespace(db=db, ha_client=None)),
        client=None,
    )


def _body(entries):
    return api.HomeNetworksBody(networks=[api.HomeNetworkEntry(**e) for e in entries])


@pytest.fixture
def pushed(monkeypatch):
    log = []

    async def _broadcast(message, *args, **kwargs):
        log.append(message)

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    monkeypatch.setattr(enforcement, "notify_change", lambda *a: log.append("rules"))
    return log


def _vectors():
    root = _contracts_root()
    folder = root / "fixtures" / "network" / "home-networks" if root else None
    if folder is None or not folder.is_dir():
        pytest.skip(
            "contract checkout with home networks not found (set CONTRACTS_DIR)"
        )
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


def test_contract_vectors():
    vectors = _vectors()
    assert len(vectors) >= 10
    for v in vectors:
        try:
            got = {"networks": hn.normalize(v["input"])}
        except hn.HomeNetworkError as err:
            got = {"error": err.code}
        assert got == v["expect"], v["name"]
        nets = [n["cidr"] for n in got.get("networks", [])]
        for case in v.get("hosts", []):
            assert hn.contains(nets, case["address"]) is case["home"], (v["name"], case)


def test_more_than_ten_entries_are_refused_even_if_they_would_merge():
    entries = [{"cidr": "10.0.0.0/8"}] * 11
    with pytest.raises(hn.HomeNetworkError) as err:
        hn.normalize(entries)
    assert err.value.code == "home_networks_too_many"
    assert err.value.detail() == {
        "code": "home_networks_too_many",
        "message": "at most 10 networks",
    }


async def test_get_is_empty_by_default(db):
    assert await api.get_home_networks(_req(db)) == {"networks": []}


async def test_put_normalizes_logs_and_pushes(db, pushed):
    event_log.register(db, None)
    answer = await api.put_home_networks(
        _req(db),
        _body([{"cidr": " 100.101.102.103/10 ", "name": " Tailscale "}, TAILSCALE[1]]),
    )
    assert answer == {"networks": TAILSCALE}
    assert await api.get_home_networks(_req(db)) == {"networks": TAILSCALE}
    assert {"type": "client_config_updated", "data": {}} in pushed
    assert "rules" in pushed
    rows = await db.fetchall("SELECT * FROM event_log WHERE kind = 'home_networks_set'")
    assert len(rows) == 1 and rows[0]["category"] == "change"
    assert json.loads(rows[0]["detail_json"]) == {
        "before": [],
        "after": ["100.64.0.0/10", "fd7a:115c:a1e0::/48"],
    }
    # Same list again: nothing logged or pushed.
    pushed.clear()
    await api.put_home_networks(_req(db), _body(TAILSCALE))
    assert pushed == []
    assert (
        len(
            await db.fetchall(
                "SELECT id FROM event_log WHERE kind = 'home_networks_set'"
            )
        )
        == 1
    )
    # Emptying the list is a change, too.
    assert await api.put_home_networks(_req(db), _body([])) == {"networks": []}
    assert (
        len(
            await db.fetchall(
                "SELECT id FROM event_log WHERE kind = 'home_networks_set'"
            )
        )
        == 2
    )


@pytest.mark.parametrize(
    ("cidr", "code"),
    [
        ("8.8.8.0/24", "home_network_public"),
        ("100.0.0.0/8", "home_network_public"),
        ("2001:db8::/32", "home_network_public"),
        ("100.64.0.1", "home_network_invalid"),
        ("tailscale", "home_network_invalid"),
    ],
)
async def test_put_refuses_and_stores_nothing(db, pushed, cidr, code):
    await api.put_home_networks(_req(db), _body(TAILSCALE[:1]))
    pushed.clear()
    with pytest.raises(HTTPException) as err:
        await api.put_home_networks(_req(db), _body([TAILSCALE[0], {"cidr": cidr}]))
    assert err.value.status_code == 422
    assert err.value.detail["code"] == code
    assert err.value.detail["cidr"] == cidr
    assert cidr in err.value.detail["message"]
    assert await api.get_home_networks(_req(db)) == {"networks": TAILSCALE[:1]}
    assert pushed == []


async def test_put_refuses_eleven(db, pushed):
    with pytest.raises(HTTPException) as err:
        await api.put_home_networks(
            _req(db), _body([{"cidr": f"10.{i}.0.0/16"} for i in range(11)])
        )
    assert err.value.detail["code"] == "home_networks_too_many"


async def test_answer_matches_the_contract(db, pushed):
    validator = _validator("rest/home-networks.json")
    answer = await api.put_home_networks(_req(db), _body(TAILSCALE))
    assert _errors(validator, answer) == []


async def test_broken_setting_counts_as_empty(db):
    await db.set_setting(hn.SETTING_KEY, "{not json")
    assert await hn.get(db) == []
    await db.set_setting(hn.SETTING_KEY, json.dumps([{"cidr": "8.8.8.0/24"}]))
    assert await hn.get(db) == []


async def test_client_config_and_snapshot_carry_the_list(db, sent):  # noqa: F811
    cid = await add_client(
        db, name="iPhone", platform="ios", profile="adult", ha_user_id=ADULT_USER
    )
    ha = FakeHA()
    config = await _get_client_config(db, cid, ha)
    assert config["home_networks"] == []
    before = (await enforcement.build_one(db, ha, cid))["rules_hash"]

    await hn.save(db, TAILSCALE)
    config = await _get_client_config(db, cid, ha)
    assert config["home_networks"] == ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
    snap = await enforcement.build_one(db, ha, cid)
    assert snap["home_networks"] == ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
    assert snap["rules_hash"] != before
    assert snap["rules_hash"] == enforcement.rules_hash(snap)
    assert _errors(_validator("common/enforcement-snapshot.json"), snap) == []


def test_pairing_link_hn(monkeypatch):
    monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: "ab" * 32)
    nets = ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
    link = build_pairing_deep_link("https://100.101.102.103:8100", "1234-5678", nets)
    assert link.endswith("&hn=100.64.0.0%2F10%2Cfd7a%3A115c%3Aa1e0%3A%3A%2F48")
    assert "&hn=" not in build_pairing_deep_link(
        "https://h.local:8100", "1234-5678", []
    )
    root = _contracts_root()
    if root is not None:
        schema = json.loads((root / "schemas/deep-link/pairing-link.json").read_text())
        assert re.match(schema["properties"]["uri"]["pattern"], link)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
        assert params["hn"] == ",".join(nets)
        assert Path(root / "tools/home_networks_ref.py").exists()
