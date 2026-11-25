"""``data/family.json`` – byte copy of the contract (``scripts/ci/contracts-check.sh``)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "family.json"


@cache
def family() -> dict[str, Any]:
    return json.loads(DATA_FILE.read_text("utf-8"))


def manageable_rules() -> list[str]:
    return [m["rule"] for m in family()["manageable_rules"]]


def categories() -> dict[str, dict]:
    return {c["id"]: c for c in family()["request_categories"]}


def presets() -> dict[str, dict]:
    return {p["id"]: p for p in family()["release_presets"]}


def never_release_domains() -> set[str]:
    return set(family()["never_release_domains"])


def limits() -> dict[str, Any]:
    return family()["limits"]


def settings_defaults() -> dict[str, Any]:
    return dict(family()["settings_defaults"])


def notify_defaults() -> dict[str, dict]:
    return {n["kind"]: n for n in family()["notify_defaults"]}


def notify_actions() -> list[dict]:
    return family()["notify_actions"]


def decline_messages() -> dict[str, dict]:
    return {m["id"]: m for m in family()["decline_messages"]}


def label(item: dict, key: str, language: str) -> str:
    texts = item.get(key) or {}
    return texts.get(language) or texts.get("de") or ""


def babysitter() -> dict[str, Any]:
    """What a babysitter device may answer."""
    return family()["babysitter"]
