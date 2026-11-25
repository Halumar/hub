"""Small JSON Schema (2020-12 subset) validator for the profile rule values.

The hub checks profile bodies and device adjustments against the contract's
``schemas/common/profile-rule-values.json`` (copied byte for byte to
``data/schemas/`` together with ``defs.json`` and ``kids-rules.json``, the
contract gate compares them). Only the keywords these schemas use are
supported; an unknown keyword is a programming error and raises.
"""

from __future__ import annotations

import json
import math
import re
from functools import cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "schemas"

_IGNORED = frozenset(
    {
        "$schema",
        "$id",
        "title",
        "description",
        "default",
        "examples",
        "format",
        "deprecated",
        "$comment",
        "$defs",
        "readOnly",
        "writeOnly",
    }
)
_KNOWN = frozenset(
    {
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "maxProperties",
        "minProperties",
        "$ref",
    }
)


@cache
def _document(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text("utf-8"))


@cache
def _pattern(text: str) -> re.Pattern:
    """ECMA-262 semantics of ``pattern``: ``$`` is the end of the string.
    Python's ``$`` also matches before a final newline ("22:00\\n")."""
    out, escaped, in_class = [], False, False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            out.append(ch)
            escaped = True
        elif in_class:
            out.append(ch)
            in_class = ch != "]"
        elif ch == "[":
            out.append(ch)
            in_class = True
        elif ch == "$":
            out.append(r"\Z")
        else:
            out.append(ch)
    return re.compile("".join(out))


def _resolve_ref(ref: str, base: str) -> tuple[dict, str]:
    file, _, pointer = ref.partition("#")
    doc_name = file or base
    node: Any = _document(doc_name)
    for part in [p for p in pointer.split("/") if p]:
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node, doc_name


def _type_ok(value: Any, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        # Strict: 40.0 is an integer in JSON Schema, but every reader of the
        # value (snapshot, apps) checks for an int and drops it.
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if kind == "string":
        return isinstance(value, str)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    raise ValueError(f"unknown type {kind}")


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def errors(value: Any, schema: dict | bool, base: str, path: str = "") -> list[str]:
    """Messages for every violation of ``schema`` (empty = valid)."""
    if schema is True:
        return []
    if schema is False:
        return [f"{path or '/'}: not allowed"]
    out: list[str] = []
    unknown = set(schema) - _KNOWN - _IGNORED
    if unknown:
        raise ValueError(f"unsupported schema keywords {sorted(unknown)}")
    if "$ref" in schema:
        target, doc = _resolve_ref(schema["$ref"], base)
        out += errors(value, target, doc, path)
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, k) for k in kinds):
            return out + [f"{path or '/'}: must be {' or '.join(kinds)}"]
    if "enum" in schema and not any(_equal(value, v) for v in schema["enum"]):
        out.append(f"{path or '/'}: must be one of {schema['enum']}")
    if "const" in schema and not _equal(value, schema["const"]):
        out.append(f"{path or '/'}: must be {schema['const']!r}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{path}/{key}: required")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                out += errors(item, props[key], base, f"{path}/{key}")
            elif "additionalProperties" in schema:
                out += errors(
                    item, schema["additionalProperties"], base, f"{path}/{key}"
                )
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            out.append(f"{path or '/'}: too many properties")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            out.append(f"{path or '/'}: too few properties")
    if isinstance(value, list):
        if "items" in schema:
            for i, item in enumerate(value):
                out += errors(item, schema["items"], base, f"{path}/{i}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path or '/'}: too many items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append(f"{path or '/'}: too few items")
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in value]
            if len(set(seen)) != len(seen):
                out.append(f"{path or '/'}: items must be unique")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path or '/'}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"{path or '/'}: too long")
        if "pattern" in schema and not _pattern(schema["pattern"]).search(value):
            out.append(f"{path or '/'}: does not match the pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path or '/'}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path or '/'}: above {schema['maximum']}")
    for sub in schema.get("allOf", []):
        out += errors(value, sub, base, path)
    if "anyOf" in schema and not any(
        not errors(value, sub, base, path) for sub in schema["anyOf"]
    ):
        out.append(f"{path or '/'}: matches none of the allowed forms")
    if "oneOf" in schema and (
        sum(1 for sub in schema["oneOf"] if not errors(value, sub, base, path)) != 1
    ):
        out.append(f"{path or '/'}: must match exactly one form")
    if "not" in schema and not errors(value, schema["not"], base, path):
        out.append(f"{path or '/'}: not allowed")
    if "if" in schema:
        if not errors(value, schema["if"], base, path):
            if "then" in schema:
                out += errors(value, schema["then"], base, path)
        elif "else" in schema:
            out += errors(value, schema["else"], base, path)
    return out


RULE_VALUES = "profile-rule-values.json"


def rule_value_errors(key: str, value: Any) -> list[str]:
    """Violations of the value schema of one rule (unknown key = one error)."""
    props = _document(RULE_VALUES)["properties"]
    if key not in props:
        return [f"/{key}: unknown rule"]
    return errors(value, props[key], RULE_VALUES, f"/{key}")
