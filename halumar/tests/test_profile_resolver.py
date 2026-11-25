"""Profile resolver: contract vectors and Hypothesis properties.

The vectors are byte copies of the contract's ``fixtures/profiles`` (the
contract gate compares them); the properties are the ones of the contract's
docs/profiles.md.
"""

import copy
import datetime as dt
import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from services.profiles import catalog as cat
from services.profiles import resolver as res
from services.profiles.schema import rule_value_errors

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
RESOLVE = sorted((FIXTURES / "resolve").glob("*.json"))
EXPIRY = sorted((FIXTURES / "expiry").glob("*.json"))
HA_ACCESS = sorted((FIXTURES / "ha-access").glob("*.json"))
NO_ESCALATION = sorted((FIXTURES / "no-escalation").glob("*.json"))
FIELDS = (
    "value",
    "source",
    "reason",
    "locked",
    "fixed",
    "user_choice",
    "expires_at",
    "then",
)


def _body(prof: dict) -> dict:
    if prof.get("from_suggestion"):
        body = cat.materialize(prof["from_suggestion"])
    else:
        body = {
            "kind": "person",
            "child_protection": False,
            "rules": {},
            "fixed": [],
            "user_choice": [],
        }
    for k in (
        "kind",
        "child_protection",
        "fixed",
        "user_choice",
        "has_remote_pin",
        "remote_pin_fixed",
    ):
        if k in prof:
            body[k] = prof[k]
    body["rules"] = {**body["rules"], **prof.get("rules", {})}
    return res.fill_missing(
        cat.catalog(),
        cat.suggestions()["suggestions"],
        body,
        prof.get("from_suggestion"),
    )


def _differences(vector: dict) -> list[str]:
    inp = vector["input"]
    got = res.resolve(
        cat.catalog(),
        platform=inp["platform"],
        access=inp["access"],
        profile=_body(inp["profile"]),
        overrides=inp.get("overrides", []),
        device=inp["device"],
        now=inp["now"],
    )
    lim = inp.get("limit")
    phone = None
    if lim:
        # A watch follows its phone (contract input.limit, resolver.limit_to).
        phone = res.resolve(
            cat.catalog(),
            platform=lim["platform"],
            access=lim["access"],
            profile=_body(lim["profile"]),
            overrides=lim.get("overrides", []),
            device=lim["device"],
            now=inp["now"],
        )
        got = res.limit_to(
            cat.catalog(),
            got,
            phone,
            platform=inp["platform"],
            area_tags=inp.get("area_tags"),
            entity_classes=inp.get("entity_classes"),
            time_zone=inp.get("time_zone"),
            now=inp["now"],
        )
    errs = []
    for key, exp in vector["expected"].items():
        want = {"user_choice": False, "expires_at": None, "then": None, **exp}
        for field in FIELDS + ("enforcement",):
            if field == "enforcement" and field not in want:
                continue
            if not res.json_equal(got[key][field], want[field]):
                errs.append(f"{key}/{field}: {want[field]!r} != {got[key][field]!r}")
    for i, c in enumerate(vector.get("entity_checks", [])):
        r = res.entity_access(
            got,
            entity_id=c["entity_id"],
            device_class=c.get("device_class"),
            route=c["route"],
            quick_access=c.get("quick_access"),
            catalog=cat.catalog(),
        )
        if (
            r["app"] != c["expected_app"]
            or r["quick"] != c.get("expected_quick")
            or (
                "expected_always_visible" in c
                and r["always_visible"] != c["expected_always_visible"]
            )
        ):
            errs.append(f"entity_checks/{i}: {c} != {r}")
    for i, c in enumerate(vector.get("notification_checks", [])):
        shown = res.notification_shown(
            cat.catalog(), got, c["priority"], c.get("entity_id"), c.get("device_class")
        )
        if shown != c["expected_shown"]:
            errs.append(f"notification_checks/{i}: {c['expected_shown']} != {shown}")
    if "expected_remote_pin_source" in vector:
        src = res.remote_pin_source(_body(inp["profile"]), inp["device"])
        if src != vector["expected_remote_pin_source"]:
            errs.append(
                f"remote_pin_source: {vector['expected_remote_pin_source']} != {src}"
            )
    if "expected_ha_access" in vector:
        x = vector["expected_ha_access"]
        acc = res.ha_access(
            got,
            platform=inp["platform"],
            child=x["child"],
            access_open=x["access_open"],
            phone=phone,
        )
        for field, want in x["expected"].items():
            if not res.json_equal(acc[field], want):
                errs.append(f"expected_ha_access/{field}: {want!r} != {acc[field]!r}")
    if "expected_ha_user_group" in vector:
        grp = res.ha_user_group(got)
        if grp != vector["expected_ha_user_group"]:
            errs.append(f"ha_user_group: {vector['expected_ha_user_group']} != {grp}")
    return errs


@pytest.mark.parametrize(
    "path",
    [p for p in RESOLVE if not p.name.startswith("invalid")],
    ids=lambda p: p.stem,
)
def test_resolve_vector(path):
    assert _differences(json.loads(path.read_text())) == []


def test_resolve_vectors_present():
    assert len([p for p in RESOLVE if not p.name.startswith("invalid")]) >= 40


@pytest.mark.parametrize(
    "path",
    [p for p in RESOLVE if p.name.startswith("invalid-semantic")],
    ids=lambda p: p.stem,
)
def test_resolve_invalid_semantic_vector_disagrees(path):
    assert _differences(json.loads(path.read_text())) != []


def _ha_access_differences(vector: dict) -> list[str]:
    """fixtures/profiles/ha-access: a bare member profile with the case's
    control_path and remote_access on its platform, through resolve,
    holds_key, ha_access and snapshot_control_path."""
    errs = []
    for i, c in enumerate(vector["cases"]):
        eff = res.resolve(
            cat.catalog(),
            platform=c["platform"],
            access={"kind": "member", "expires_at": None},
            profile={
                "kind": "person",
                "child_protection": c["child_protection"],
                "rules": {
                    "control_path": c["control_path"],
                    "remote_access": c["remote_access"],
                },
                "fixed": [],
                "user_choice": [],
            },
            overrides=[],
            device={"own_area_id": None, "has_remote_pin": False},
            now=vector["now"],
        )
        got = {
            "control_path": eff["control_path"]["value"],
            **res.ha_access(
                eff,
                platform=c["platform"],
                child=c["child_protection"],
                access_open=c["access_open"],
                phone={"watch": {"value": c.get("phone_watch")}},
            ),
        }
        if got["ha_enforcement"] not in (None, res.snapshot_control_path(eff)):
            errs.append(f"cases/{i}: ha_enforcement is not the snapshot path")
        for field, want in c["expected"].items():
            if not res.json_equal(got.get(field), want):
                errs.append(f"cases/{i} {c}: {field}: {want!r} != {got.get(field)!r}")
    return errs


@pytest.mark.parametrize(
    "path",
    [p for p in HA_ACCESS if not p.name.startswith("invalid")],
    ids=lambda p: p.stem,
)
def test_ha_access_vector(path):
    assert _ha_access_differences(json.loads(path.read_text())) == []


@pytest.mark.parametrize(
    "path",
    [p for p in HA_ACCESS if p.name.startswith("invalid-semantic")],
    ids=lambda p: p.stem,
)
def test_ha_access_invalid_semantic_vector_disagrees(path):
    assert _ha_access_differences(json.loads(path.read_text())) != []


def _no_escalation_differences(vector: dict) -> list[str]:
    """fixtures/profiles/no-escalation: ``looser(rule, new, mine)`` per case
    and ``key_looser`` per key case, the comparisons a setting-up device's own
    values are held against (control_path by key level)."""
    by_key = {r["key"]: r for r in cat.catalog()["rules"]}
    errs = []
    for i, c in enumerate(vector.get("key_cases", [])):
        new, mine = (
            {k: {"value": v} for k, v in side.items()} for side in (c["new"], c["mine"])
        )
        if res.key_looser(new, mine) != c["expected_looser"]:
            errs.append(f"key_cases/{i} {c}")
    for i, c in enumerate(vector.get("cases", [])):
        if res.looser(by_key[c["rule"]], c["new"], c["mine"]) != c["expected_looser"]:
            errs.append(f"cases/{i} {c}")
    return errs


@pytest.mark.parametrize(
    "path",
    [p for p in NO_ESCALATION if not p.name.startswith("invalid")],
    ids=lambda p: p.stem,
)
def test_no_escalation_vector(path):
    assert _no_escalation_differences(json.loads(path.read_text())) == []


@pytest.mark.parametrize(
    "path",
    [p for p in NO_ESCALATION if p.name.startswith("invalid-semantic")],
    ids=lambda p: p.stem,
)
def test_no_escalation_invalid_semantic_vector_disagrees(path):
    assert _no_escalation_differences(json.loads(path.read_text())) != []


def test_ha_access_and_no_escalation_vectors_present():
    assert (
        len(json.loads((FIXTURES / "ha-access" / "matrix.json").read_text())["cases"])
        >= 100
    )
    assert [p for p in NO_ESCALATION if not p.name.startswith("invalid")]


@pytest.mark.parametrize(
    "path",
    [p for p in EXPIRY if not p.name.startswith("invalid")],
    ids=lambda p: p.stem,
)
def test_expiry_vector(path):
    vector = json.loads(path.read_text())
    for case in vector["cases"]:
        got = res.expiry(case["preset"], vector["now"], vector["time_zone"])
        assert got == res.parse_ts(case["expires_at"]), case


def test_expiry_unknown_preset():
    with pytest.raises(ValueError):
        res.expiry("2h", "2026-11-20T18:00:00Z", "Europe/Zurich")


def test_suggestions_are_valid_profiles():
    for s in cat.suggestions()["suggestions"]:
        body = cat.materialize(s["id"])
        for key, value in body["rules"].items():
            assert rule_value_errors(key, value) == [], (s["id"], key)


# ── Hypothesis ──────────────────────────────────────────────────────────────

RULES = cat.catalog()["rules"]
KEYS = [r["key"] for r in RULES]
AREA = "11111111-1111-4111-8111-111111111111"
AREA2 = "22222222-2222-4222-8222-222222222222"
NOW = dt.datetime(2026, 11, 20, 18, 0, tzinfo=dt.timezone.utc)
REASONS = {r["code"] for r in cat.catalog()["reasons"]}


def _candidates() -> dict[str, list]:
    out: dict[str, list] = {}
    for r in RULES:
        values = list(r["order"]) if r.get("order") else [r["default"]]
        for s in cat.suggestions()["suggestions"]:
            if r["key"] in s["rules"]:
                values.append(s["rules"][r["key"]])
        if r["key"] == "areas":
            values += [
                {"mode": "selected", "area_ids": [AREA2], "tags": [], "own_area": True},
                {
                    "mode": "selected",
                    "area_ids": [],
                    "tags": ["kids"],
                    "own_area": False,
                },
            ]
        if r["key"] == "kids_exceptions":
            values.append([{"target": {"own_area": True}, "rules": {"blocked": True}}])
        uniq = []
        for v in values:
            if not any(res.json_equal(v, u) for u in uniq):
                uniq.append(v)
        out[r["key"]] = uniq
    return out


CANDIDATES = _candidates()


def _value(key):
    return st.sampled_from(CANDIDATES[key]).map(copy.deepcopy)


@st.composite
def profiles(draw):
    keys = draw(st.lists(st.sampled_from(KEYS), unique=True))
    rules = {k: draw(_value(k)) for k in keys}
    return {
        "kind": draw(st.sampled_from(cat.PROFILE_KINDS)),
        "child_protection": draw(st.booleans()),
        "rules": rules,
        "fixed": draw(st.lists(st.sampled_from(KEYS), unique=True)),
        "user_choice": draw(st.lists(st.sampled_from(KEYS), unique=True)),
    }


def _ts(minutes: int) -> str:
    return res.fmt_ts(NOW + dt.timedelta(minutes=minutes))


@st.composite
def overrides(draw):
    keys = draw(st.lists(st.sampled_from(KEYS), unique=True, max_size=8))
    out = []
    for k in keys:
        end = draw(st.one_of(st.none(), st.integers(-600, 600).map(_ts)))
        out.append(
            {
                "rule": k,
                "value": draw(_value(k)),
                "user_choice": draw(st.booleans()),
                "expires_at": end,
            }
        )
    return out


accesses = st.one_of(
    st.just({"kind": "member", "expires_at": None}),
    st.integers(-600, 6000).map(lambda m: {"kind": "guest", "expires_at": _ts(m)}),
)
devices = st.fixed_dictionaries(
    {"own_area_id": st.sampled_from([None, AREA]), "has_remote_pin": st.booleans()}
)
platforms = st.sampled_from(list(cat.DEVICE_PLATFORMS))


def run(platform, access, profile, overs, device, now=None):
    return res.resolve(
        cat.catalog(),
        platform=platform,
        access=access,
        profile=profile,
        overrides=overs,
        device=device,
        now=now or res.fmt_ts(NOW),
    )


SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    # No example database: it would be written into the repo tree.
    database=None,
)


def _idx(key, value):
    return cat.order_index(key, value)


@SETTINGS
@given(platforms, accesses, profiles(), overrides(), devices)
def test_every_rule_has_one_valid_value_and_reason(
    platform, access, profile, overs, device
):
    out = run(platform, access, profile, overs, device)
    assert set(out) == set(KEYS)
    for key, e in out.items():
        assert e["reason"] in REASONS
        assert e["source"] in {
            "default",
            "profile",
            "device",
            "boundary",
            "prerequisite",
            "not_applicable",
            "access_expired",
        }
        assert e["locked"] is (not e["user_choice"])
        if e["source"] != "not_applicable":
            assert rule_value_errors(key, e["value"]) == [], (key, e["value"])


@SETTINGS
@given(platforms, accesses, profiles(), overrides(), devices)
def test_fixed_ignores_adjustments(platform, access, profile, overs, device):
    """Adjustments of fixed rules change nothing at all."""
    fixed = set(profile["fixed"]) | cat.always_fixed()
    with_o = run(platform, access, profile, overs, device)
    without = run(
        platform, access, profile, [o for o in overs if o["rule"] not in fixed], device
    )
    assert with_o == without


@SETTINGS
@given(platforms, accesses, profiles(), overrides(), overrides(), devices)
def test_boundary_values_do_not_depend_on_profile_or_adjustment(
    platform, access, p1, o1, o2, device
):
    p2 = copy.deepcopy(p1)
    p2["rules"] = {}
    a = run(platform, access, p1, o1, device)
    b = run(platform, access, p2, o2, device)
    active = {"guest"} if access["kind"] == "guest" else set()
    if p1["child_protection"]:
        active.add("child_protection")
    for r in RULES:
        caps = [x for x in r["boundaries"] if x["boundary"] in active]
        if a[r["key"]]["source"] == "not_applicable":
            continue
        if any(
            r["order"] is None or res.json_equal(x["cap"], r["order"][0]) for x in caps
        ):
            assert res.json_equal(a[r["key"]]["value"], b[r["key"]]["value"])
            assert a[r["key"]]["value"] is not None


@SETTINGS
@given(platforms, profiles(), overrides(), devices)
def test_additional_boundary_never_loosens(platform, profile, overs, device):
    member = {"kind": "member", "expires_at": None}
    guest = {"kind": "guest", "expires_at": _ts(6000)}
    off = dict(profile, child_protection=False)
    on = dict(profile, child_protection=True)
    pairs = [
        (
            run(platform, member, off, overs, device),
            run(platform, guest, off, overs, device),
        ),
        (
            run(platform, member, off, overs, device),
            run(platform, member, on, overs, device),
        ),
        (
            run(platform, guest, off, overs, device),
            run(platform, guest, on, overs, device),
        ),
    ]
    for loose, strict in pairs:
        for r in RULES:
            if not r.get("order"):
                continue
            lv, sv = loose[r["key"]]["value"], strict[r["key"]]["value"]
            if lv is None or sv is None:
                continue
            assert _idx(r["key"], sv) <= _idx(r["key"], lv), r["key"]


@SETTINGS
@given(platforms, st.integers(-6000, 0), profiles(), overrides(), devices)
def test_guest_expiry_sets_every_security_rule_to_strictest(
    platform, minutes, profile, overs, device
):
    profile = dict(profile, child_protection=False)
    out = run(
        platform, {"kind": "guest", "expires_at": _ts(minutes)}, profile, overs, device
    )
    for r in RULES:
        e = out[r["key"]]
        if e["source"] == "not_applicable" or r.get("strictest") is None:
            continue
        assert res.json_equal(e["value"], r["strictest"]), r["key"]
        assert e["reason"] == "access_expired"


@SETTINGS
@given(platforms, accesses, profiles(), overrides(), devices, st.randoms())
def test_order_of_adjustments_does_not_matter(
    platform, access, profile, overs, device, rnd
):
    shuffled = list(overs)
    rnd.shuffle(shuffled)
    assert run(platform, access, profile, overs, device) == run(
        platform, access, profile, shuffled, device
    )


@SETTINGS
@given(platforms, accesses, profiles(), overrides(), st.booleans())
def test_missing_own_area_is_never_more_open(platform, access, profile, overs, pin):
    with_area = run(
        platform, access, profile, overs, {"own_area_id": AREA, "has_remote_pin": pin}
    )
    without = run(
        platform, access, profile, overs, {"own_area_id": None, "has_remote_pin": pin}
    )
    a, b = with_area["areas"]["value"], without["areas"]["value"]
    if a is None:
        return
    assert a["mode"] == b["mode"] and a["tags"] == b["tags"]
    assert set(b["area_ids"]) <= set(a["area_ids"])


@SETTINGS
@given(platforms, accesses, profiles(), overrides(), devices)
def test_expired_adjustment_counts_as_none(platform, access, profile, overs, device):
    active = [
        o
        for o in overs
        if o["expires_at"] is None or res.parse_ts(o["expires_at"]) > NOW
    ]
    assert run(platform, access, profile, overs, device) == run(
        platform, access, profile, active, device
    )


@SETTINGS
@given(
    platforms,
    accesses,
    profiles(),
    devices,
    st.sampled_from([r["key"] for r in RULES if r.get("order")]),
    st.data(),
)
def test_stricter_profile_value_is_never_looser_effective(
    platform, access, profile, device, key, data
):
    order = cat.rule(key)["order"]
    i = data.draw(st.integers(0, len(order) - 1))
    j = data.draw(st.integers(i, len(order) - 1))
    strict = copy.deepcopy(profile)
    strict["rules"][key] = order[i]
    loose = copy.deepcopy(profile)
    loose["rules"][key] = order[j]
    a = run(platform, access, strict, [], device)[key]["value"]
    b = run(platform, access, loose, [], device)[key]["value"]
    if a is None or b is None:
        return
    assert _idx(key, a) <= _idx(key, b)
