#!/usr/bin/env python3
"""Validate the request bodies the admin UI sent in the e2e run against the contract schemas.

  E2E_REQUEST_LOG=/tmp/req.jsonl npx playwright test profiles
  <contracts-venv>/bin/python e2e/check-request-schemas.py /tmp/req.jsonl <contracts worktree>

Each line of the log is {"method", "path", "body"} (written by e2e/profiles-hub.js).
"""

import json
import re
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

log, repo = Path(sys.argv[1]), Path(sys.argv[2])
SCHEMAS = repo / "schemas"


def retrieve(uri: str) -> Resource:
    path = Path(uri.removeprefix("file://"))
    return Resource.from_contents(
        {**json.loads(path.read_text()), "$id": path.as_uri()},
        default_specification=DRAFT202012,
    )


REGISTRY = Registry(retrieve=retrieve)

ROUTES = [
    ("POST", r"^/profiles$", "rest/profile-write.request.json"),
    ("PATCH", r"^/profiles/[^/]+(\?dry_run=true)?$", "rest/profile-write.request.json"),
    ("POST", r"^/profiles/[^/]+/duplicate$", "rest/profile-name.request.json"),
    ("POST", r"^/profile-suggestions/[^/]+$", "rest/profile-name.request.json"),
    ("POST", r"^/profiles/from-device$", "rest/profile-from-device.request.json"),
    ("PUT", r"^/settings/profiles$", "rest/profile-settings.json"),
    (
        "PUT",
        r"^/pairing/clients/[^/]+/profile(\?dry_run=true)?$",
        "rest/client-profile.request.json",
    ),
    (
        "PUT",
        r"^/pairing/clients/[^/]+/overrides/[^/?]+(\?dry_run=true)?$",
        "rest/device-override.request.json",
    ),
    ("PUT", r"^/pairing/clients/[^/]+/own-area$", "rest/client-own-area.request.json"),
    (
        "PUT",
        r"^/pairing/clients/[^/]+/remote-pin$",
        "rest/client-remote-pin.request.json",
    ),
    ("POST", r"^/pairing/clients/bulk$", "rest/clients-bulk.request.json"),
    ("POST", r"^/pairing/generate$", "rest/pairing-generate.request.json"),
    ("PATCH", r"^/pairing/clients/[^/]+$", "rest/client-update.request.json"),
]

MOCK_ID = re.compile(r"^(c|p|a)-[a-z0-9-]+$")


def real_ids(v):
    """The mock hub uses readable ids (c-kid, p-child, a-gast); the hub uses UUIDs."""
    if isinstance(v, str) and MOCK_ID.match(v):
        return "00000000-0000-4000-8000-" + format(abs(hash(v)) % 16**12, "012x")
    if isinstance(v, list):
        return [real_ids(x) for x in v]
    if isinstance(v, dict):
        return {k: real_ids(x) for k, x in v.items()}
    return v


counts, errors, unknown = {}, [], set()
for line in log.read_text().splitlines():
    req = json.loads(line)
    if req["body"] is None:
        continue
    for method, pattern, schema in ROUTES:
        if req["method"] == method and re.match(pattern, req["path"]):
            path = SCHEMAS / schema
            v = Draft202012Validator(
                {**json.loads(path.read_text()), "$id": path.as_uri()},
                registry=REGISTRY,
            )
            errs = [
                f"{'/'.join(map(str, e.absolute_path))}: {e.message}"
                for e in v.iter_errors(real_ids(req["body"]))
            ]
            counts[schema] = counts.get(schema, 0) + 1
            if errs:
                errors.append((req["method"], req["path"], req["body"], errs))
            break
    else:
        unknown.add(f"{req['method']} {req['path']}")

for schema, n in sorted(counts.items()):
    print(f"{n:4d}  {schema}")
for u in sorted(unknown):
    print(f"   ?  no schema: {u}")
for m, p, b, errs in errors:
    print(f"FAIL {m} {p} {json.dumps(b)}\n     " + "\n     ".join(errs))
print(f"{sum(counts.values())} bodies checked, {len(errors)} invalid")
sys.exit(1 if errors else 0)
