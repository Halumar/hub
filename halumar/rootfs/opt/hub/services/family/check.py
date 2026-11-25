"""Permission check of the family manager – port of the contract's
``tools/family_ref.py`` (vectors ``tests/fixtures/profiles/family-check``).

``family_check(manager, child, op)`` answers one question: may this device do
this to this child device right now? ``(allowed, reason)`` with reason a
``$defs/family_error_reason`` or None. Value schemas, boundaries on the child
device, limits and request state are checked by :mod:`services.family.service`.
"""

from __future__ import annotations

from services.family import data

# release kind -> rule that must not be fixed in the child's profile
RELEASE_RULE = {"bedtime": "kids_quiet_hours", "pause": "kids_rate_limit"}


def type_blocked(
    entity_id: str, device_class: str | None, blocked_types: list[str]
) -> bool:
    domain = entity_id.split(".", 1)[0]
    return domain in blocked_types or (
        device_class is not None and f"{domain}:{device_class}" in blocked_types
    )


def babysitter_check(
    family: dict, sitter: dict, child: dict, op: dict
) -> tuple[bool, str | None]:
    """A babysitter device only answers requests, with access, at home,
    within its presets; never blocked device types, exceptions or fixed rules."""
    if (
        sitter.get("child_protection")
        or not sitter.get("access_open", True)
        or not sitter.get("at_home", True)
    ):
        return False, "not_permitted"
    if op["kind"] not in ("answer_yes", "answer_no"):
        return False, "not_permitted"
    if not child["child_protection"] or child["client_id"] == sitter["client_id"]:
        return False, "not_your_child"
    if op["kind"] == "answer_no":
        return True, None
    sb = family["babysitter"]
    if op["category"] not in sb["categories"]:
        return False, "not_permitted"
    preset = op.get("preset")
    if preset not in sb["presets"] or (
        preset == "access_end" and not sitter.get("access_until")
    ):
        return False, "preset_not_allowed"
    cat = {c["id"]: c for c in family["request_categories"]}[op["category"]]
    fixed = set(child.get("fixed", []))
    if set(cat["requires_unfixed"]) & fixed:
        return False, "release_not_allowed"
    if cat["release_kind"] != "entity":
        return True, None
    entity_id = op["entity_id"]
    if entity_id.split(".", 1)[0] in family["never_release_domains"]:
        return False, "release_not_allowed"
    if entity_id in child.get("exception_blocked_entities", []):
        return False, "release_not_allowed"
    if type_blocked(entity_id, op.get("device_class"), child.get("blocked_types", [])):
        return False, "release_not_allowed"
    if child.get("quiet_now") and "kids_quiet_hours" in fixed:
        return False, "release_not_allowed"
    return True, None


def family_check(
    manager: dict, child: dict, op: dict, family: dict | None = None
) -> tuple[bool, str | None]:
    family = family or data.family()
    if manager.get("babysitter"):
        return babysitter_check(family, manager, child, op)
    # 1. the acting device: a member without parental controls whose effective rule family_manager is on
    if (
        manager["access"] != "member"
        or manager["child_protection"]
        or not manager["family_manager"]
    ):
        return False, "not_permitted"
    # 2. the target: a child device (parental controls) inside the scope
    scope = manager["scope"]
    if not child["child_protection"] or child["client_id"] == manager["client_id"]:
        return False, "not_your_child"
    if not scope["all"] and child["client_id"] not in scope["client_ids"]:
        return False, "not_your_child"

    fixed = set(child.get("fixed", []))
    kind = op["kind"]
    if kind in ("release_end", "answer_no"):
        return True, None  # ending a release only ever makes it stricter

    if kind in ("rule_set", "rule_reset"):
        rule = op["rule"]
        if rule not in {m["rule"] for m in family["manageable_rules"]}:
            return False, "rule_not_manageable"
        if rule in fixed:
            return False, "rule_fixed"
        if kind == "rule_set" and rule == "areas":
            visible = manager.get("visible_area_ids")
            value = op.get("value") or {}
            if visible is not None:
                # A malformed value gets the answer of the value check.
                if not isinstance(value, dict):
                    return False, "value_invalid"
                area_ids = value.get("area_ids", [])
                if not isinstance(area_ids, list) or not all(
                    isinstance(a, str) for a in area_ids
                ):
                    return False, "value_invalid"
                if value.get("mode") == "all" or value.get("tags"):
                    return False, "areas_not_allowed"
                if set(area_ids) - set(visible):
                    return False, "areas_not_allowed"
        return True, None

    # release or a Yes to a request
    if kind == "answer_yes":
        cat = {c["id"]: c for c in family["request_categories"]}[op["category"]]
        release_kind = cat["release_kind"]
        if set(cat["requires_unfixed"]) & fixed:
            return False, "release_not_allowed"
    else:
        release_kind = op["release_kind"]
    if release_kind in RELEASE_RULE:
        if RELEASE_RULE[release_kind] in fixed:
            return False, "release_not_allowed"
        return True, None

    entity_id = op["entity_id"]
    if entity_id.split(".", 1)[0] in family["never_release_domains"]:
        return False, "release_not_allowed"
    if entity_id in child.get("exception_blocked_entities", []):
        return False, "release_not_allowed"
    if (
        type_blocked(entity_id, op.get("device_class"), child.get("blocked_types", []))
        and "kids_blocked_types" in fixed
    ):
        return False, "release_not_allowed"
    if child.get("quiet_now") and "kids_quiet_hours" in fixed:
        return False, "release_not_allowed"
    return True, None
