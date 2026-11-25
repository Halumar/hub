#!/usr/bin/env python3
"""Lint: transaction control only in models/database.py.

Reports every place outside ``models/database.py`` that controls the shared
SQLite transaction itself:

- ``<x>.commit()`` / ``<x>.rollback()``
- ``<x>.connection`` (raw aiosqlite connection, outside the write lock)
- SQL strings starting with BEGIN / COMMIT / ROLLBACK / SAVEPOINT / RELEASE / END

Such call sites must move to ``async with db.transaction():``. Files listed
in the allowlist (``db_commit_allowlist.txt``, ``<path> <max count>``) may
keep up to that many findings until their owner converts them. Without
``--strict`` this is a report (exit 0); with it the script fails (exit 1) on
any finding that is not allowlisted, on more findings than allowed, and on
allowlist entries that are higher than needed (so the list only shrinks).

    python3 halumar/scripts/check_db_commits.py [--strict] [--allowlist FILE] [root]
"""

import argparse
import ast
import os
import re
import sys

DEFAULT_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "rootfs", "opt", "hub"
)
DEFAULT_ALLOWLIST = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "db_commit_allowlist.txt"
)
ALLOWED = {os.path.join("models", "database.py")}
_TX_SQL = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|END)\b", re.I)


def _sql_text(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
        )
    return None


def scan_file(path: str, rel: str) -> list[tuple[str, int, str, str]]:
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    found = []
    for node in ast.walk(tree):
        kind = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("commit", "rollback") and not node.args:
                kind = node.func.attr + "()"
            elif node.func.attr in ("execute", "executemany", "executescript"):
                text = _sql_text(node.args[0]) if node.args else None
                if text and _TX_SQL.match(text):
                    kind = "sql:" + _TX_SQL.match(text).group(1).upper()
        elif isinstance(node, ast.Attribute) and node.attr == "connection":
            kind = ".connection"
        if kind:
            snippet = (
                lines[node.lineno - 1].strip() if node.lineno <= len(lines) else ""
            )
            found.append((rel, node.lineno, kind, snippet))
    return found


def scan(root: str) -> list[tuple[str, int, str, str]]:
    findings = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in ("static", "__pycache__", "data")
        ]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            if rel in ALLOWED:
                continue
            findings += scan_file(path, rel)
    return sorted(set(findings))


def load_allowlist(path: str | None) -> dict[str, int]:
    allow: dict[str, int] = {}
    if not path or not os.path.exists(path):
        return allow
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            rel, count = line.rsplit(None, 1)
            allow[os.path.normpath(rel)] = int(count)
    return allow


def check(
    findings: list[tuple[str, int, str, str]], allow: dict[str, int]
) -> list[str]:
    """Problems that fail ``--strict`` (empty list = gate passes)."""
    per_file: dict[str, int] = {}
    for rel, *_ in findings:
        per_file[rel] = per_file.get(rel, 0) + 1
    problems = []
    for rel, n in sorted(per_file.items()):
        limit = allow.get(rel, 0)
        if n > limit:
            problems.append(
                f"{rel}: {n} call site(s), allowed {limit} - use db.transaction()"
            )
    for rel, limit in sorted(allow.items()):
        n = per_file.get(rel, 0)
        if n < limit:
            problems.append(
                f"{rel}: allowlist says {limit}, found {n} - lower or remove the entry"
            )
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=DEFAULT_ROOT)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--allowlist", default=DEFAULT_ALLOWLIST)
    args = parser.parse_args(argv)
    findings = scan(os.path.abspath(args.root))
    allow = load_allowlist(args.allowlist)
    for rel, line, kind, snippet in findings:
        mark = " (allowlisted)" if rel in allow else ""
        print(f"{rel}:{line}: {kind}{mark}: {snippet}")
    per_file: dict[str, int] = {}
    for rel, *_ in findings:
        per_file[rel] = per_file.get(rel, 0) + 1
    print(
        f"\n{len(findings)} transaction call site(s) outside models/database.py"
        + (
            ": " + ", ".join(f"{f} {n}" for f, n in sorted(per_file.items()))
            if per_file
            else ""
        )
    )
    problems = check(findings, allow)
    for problem in problems:
        print(f"ERROR {problem}" if args.strict else f"note: {problem}")
    if not problems:
        print("OK: only allowlisted call sites remain")
    return 1 if args.strict and problems else 0


if __name__ == "__main__":
    sys.exit(main())
