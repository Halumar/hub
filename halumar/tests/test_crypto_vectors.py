"""The hub's crypto against the shared contract vectors.

HPKE seal/open (onboarding, watch access) and the settings PIN hash must
give byte for byte what the apps compute; ``fixtures/crypto`` of the
contract holds the vectors all three implementations check. Without a
contract checkout these skip locally; CI fails instead (conftest.py).
"""

import json
import os
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from services import onboard_crypto as oc
from services import pin_hash, watch_access


def _fixture(name: str) -> dict | None:
    root = os.environ.get("CONTRACTS_DIR")
    path = Path(root or "/nonexistent") / "fixtures" / "crypto" / name / "v1.json"
    return json.loads(path.read_text()) if path.is_file() else None


SEAL = _fixture("onboard-seal-vectors")
PIN = _fixture("settings-pin-vectors")
NO_CONTRACT = pytest.mark.skip(reason="contract checkout not found (set CONTRACTS_DIR)")
h = bytes.fromhex


def _cases(vectors: dict | None) -> list:
    if vectors is None:
        return [pytest.param(None, marks=NO_CONTRACT, id="no-contract")]
    return [
        pytest.param(c, id=c.get("name") or f"case{i}")
        for i, c in enumerate(vectors["cases"])
    ]


@pytest.mark.parametrize("case", _cases(SEAL))
def test_seal_gives_the_vector(case):
    enc, ct = oc.seal(
        h(case["pk_r"]),
        h(case["pt"]),
        info=h(case["info"]),
        aad=h(case["aad"]),
        sk_e=h(case["sk_e"]),
    )
    assert enc.hex() == case["enc"]
    assert ct.hex() == case["ct"]


@pytest.mark.parametrize("case", _cases(SEAL))
def test_open_reads_the_vector_and_checks_aad_and_info(case):
    sk_r, enc, ct = h(case["sk_r"]), h(case["enc"]), h(case["ct"])
    info, aad = h(case["info"]), h(case["aad"])
    assert oc.open_sealed(sk_r, enc, ct, info=info, aad=aad) == h(case["pt"])
    with pytest.raises(InvalidTag):
        oc.open_sealed(sk_r, enc, ct, info=info, aad=aad + b"x")
    with pytest.raises(InvalidTag):
        oc.open_sealed(sk_r, enc, ct, info=info + b"x", aad=aad)


@pytest.mark.skipif(SEAL is None, reason="contract checkout not found")
def test_suite_and_labels_match_the_contract():
    by_name = {c["name"]: c for c in SEAL["cases"]}
    assert SEAL["suite"] == oc.SUITE
    assert h(by_name["onboard-v1"]["info"]) == oc.INFO
    assert h(by_name["watch-access-v1"]["info"]) == watch_access.INFO


@pytest.mark.parametrize("case", _cases(PIN))
def test_settings_pin_hash_matches_the_vector(case):
    assert PIN["iterations"] == pin_hash.ITERATIONS
    assert pin_hash.verify_pin(case["pin"], case["hash"], case["salt"])
    assert not pin_hash.verify_pin(case["pin"] + "0", case["hash"], case["salt"])
