#!/usr/bin/env python3
"""Generate THIRD_PARTY_NOTICES.md for the Halumar Hub (what the add-on image ships).

Inventory:
  * Python packages: halumar/rootfs/opt/hub/requirements.txt plus all transitive
    dependencies, as installed in the running interpreter (markers evaluated for the
    image: Linux, CPython of this interpreter's version). requirements.txt pins every
    one of them with `==`: the direct ones first, the transitive ones after the
    "# Transitive" line (each must be required by a shipped package).
  * npm packages in the Admin UI bundle: node_modules/.cache/third-party/bundle-packages.json,
    written by `npx vite build --config scripts/license-scan.config.js` in frontend/.
  * Vendored API docs assets (halumar/rootfs/opt/hub/docs_assets).

Every license must be on ALLOWED (compatible with AGPL-3.0-only); an unknown or new
license fails, so a new dependency gets a review. License texts are copied from the
installed packages.

Writes THIRD_PARTY_NOTICES.md (repository root) and copies LICENSE, NOTICE and the
notices into halumar/rootfs/usr/share/doc/halumar/ (shipped in the image).

  python scripts/third_party_notices.py           write
  python scripts/third_party_notices.py --check   CI: THIRD_PARTY_NOTICES.md (components,
                                                  versions, licenses, texts) and the image
                                                  copies are up to date, every shipped
                                                  Python package is pinned and installed
                                                  in the pinned version, no pin is unused,
                                                  LICENSE is the unchanged AGPL text
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as md
import json
import re
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
except ImportError:  # pip's vendored copy (CI images without packaging)
    try:
        from pip._vendor.packaging.requirements import Requirement  # type: ignore[no-redef]
    except ImportError:
        raise SystemExit(
            "needs the Python package 'packaging' (pip install packaging)"
        ) from None

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "halumar/rootfs/opt/hub/requirements.txt"
FRONTEND = ROOT / "frontend"
BUNDLE_PACKAGES = FRONTEND / "node_modules/.cache/third-party/bundle-packages.json"
DOCS_ASSETS = ROOT / "halumar/rootfs/opt/hub/docs_assets"
OUT = ROOT / "THIRD_PARTY_NOTICES.md"
IMAGE_DOC = ROOT / "halumar/rootfs/usr/share/doc/halumar"
IMAGE_COPIES = ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md")
# sha256 of https://www.gnu.org/licenses/agpl-3.0.txt (LICENSE must stay unchanged).
AGPL_3_0 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"

# SPDX ids accepted in the hub (AGPL-3.0-only compatible, see docs/proposals/license.md §5.1).
ALLOWED = {
    "0BSD",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
    "LGPL-2.1-or-later",
    "MIT",
    "MIT-0",
    "MPL-2.0",
    "PSF-2.0",
}
# Packages whose metadata has no SPDX expression (checked by hand against the
# license files of the named version line).
OVERRIDES = {
    "aiosqlite": "MIT",
    "uvloop": "MIT OR Apache-2.0",
}
# Python packages under LGPL/MPL, each named with its terms in render().
WEAK_COPYLEFT = {"certifi", "zeroconf"}
CLASSIFIERS = {
    "License :: OSI Approved :: MIT License": "MIT",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
}
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice)([-._].*)?$", re.I)

# Marker environment of the add-on image (Alpine, CPython).
PY = f"{sys.version_info.major}.{sys.version_info.minor}"
TARGET_ENV = {
    "os_name": "posix",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "platform_machine": "x86_64",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": PY,
    "python_full_version": f"{PY}.0",
}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def spdx_ids(expr: str) -> set[str]:
    return {
        t for t in re.split(r"[\s()]+", expr) if t and t not in {"AND", "OR", "WITH"}
    }


def license_of_dist(dist: md.Distribution) -> str:
    meta = dist.metadata
    name = norm(meta["Name"])
    if name in OVERRIDES:
        return OVERRIDES[name]
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    lic = (meta.get("License") or "").strip()
    if lic and "\n" not in lic and spdx_ids(lic) <= ALLOWED:
        return lic
    found = sorted(
        {CLASSIFIERS[c] for c in meta.get_all("Classifier") or [] if c in CLASSIFIERS}
    )
    return " OR ".join(found) if found else (lic.splitlines()[0] if lic else "UNKNOWN")


def requirements() -> tuple[list[Requirement], list[Requirement]]:
    """Direct requirements and the transitive pins (after the "# Transitive" line)."""
    direct: list[Requirement] = []
    transitive: list[Requirement] = []
    target = direct
    for line in REQUIREMENTS.read_text("utf-8").splitlines():
        if re.match(r"#\s*Transitive\b", line):
            target = transitive
        line = line.split("#", 1)[0].strip()
        if line:
            target.append(Requirement(line))
    return direct, transitive


def pins(reqs: list[Requirement]) -> tuple[dict[str, str], list[str]]:
    """Exact pins (normalised name -> version) and problems with the others."""
    pinned: dict[str, str] = {}
    problems = []
    rel = REQUIREMENTS.relative_to(ROOT)
    for req in reqs:
        specs = list(req.specifier)
        key = norm(req.name)
        if (
            len(specs) != 1
            or specs[0].operator != "=="
            or "*" in specs[0].version
            or req.marker is not None
            or req.url
        ):
            problems.append(f"{rel}: {req} is not pinned exactly (use {req.name}==X)")
        elif key in pinned:
            problems.append(f"{rel}: {req.name} is listed twice")
        else:
            pinned[key] = specs[0].version
    return pinned, problems


def check_pins(pinned: dict[str, str], py: list[dict]) -> list[str]:
    """Every shipped package pinned in the installed version, no unused pin."""
    rel = REQUIREMENTS.relative_to(ROOT)
    problems = []
    shipped = {norm(p["name"]): p for p in py}
    for key, p in shipped.items():
        if key not in pinned:
            problems.append(
                f"{rel}: {p['name']} {p['version']} ships in the image but is not "
                f"pinned (add {key}=={p['version']})"
            )
        elif pinned[key] != p["version"]:
            problems.append(
                f"{p['name']} {p['version']} is installed but {rel} pins "
                f"{pinned[key]}: install {rel} and run the script again"
            )
    for key in sorted(pinned.keys() - shipped.keys()):
        problems.append(
            f"{rel}: {key}=={pinned[key]} is pinned but nothing requires it, so it "
            "would ship without a notice (remove it)"
        )
    return problems


def python_packages(direct: list[Requirement]) -> list[dict]:
    seen: dict[str, dict] = {}
    todo: list[tuple[str, frozenset[str]]] = [
        (req.name, frozenset(req.extras)) for req in direct
    ]
    while todo:
        name, extras = todo.pop()
        key = norm(name)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            raise SystemExit(
                f"{name} is not installed: run with the hub requirements installed"
            ) from None
        if not dist.metadata["Name"]:
            raise SystemExit(
                f"{name} has broken package metadata in this environment: "
                "reinstall it, or use a fresh venv with the hub requirements"
            )
        entry = seen.get(key)
        if entry and extras <= entry["extras"]:
            continue
        if not entry:
            entry = seen[key] = {
                "name": dist.metadata["Name"],
                "version": dist.version,
                "license": license_of_dist(dist),
                "extras": frozenset(),
                "texts": [
                    (str(f).split(".dist-info/", 1)[1], f.read_text("utf-8"))
                    for f in dist.files or []
                    if ".dist-info/" in str(f) and LICENSE_FILE.match(Path(str(f)).name)
                ],
                "url": f"https://pypi.org/project/{dist.metadata['Name']}/{dist.version}/",
            }
        entry["extras"] = entry["extras"] | extras
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker is not None:
                envs = [{**TARGET_ENV, "extra": e} for e in sorted(entry["extras"])]
                if not any(req.marker.evaluate(env) for env in envs or [TARGET_ENV]):
                    continue
            todo.append((req.name, frozenset(req.extras)))
    return sorted(seen.values(), key=lambda p: norm(p["name"]))


def npm_packages() -> list[dict]:
    if not BUNDLE_PACKAGES.exists():
        raise SystemExit(
            f"{BUNDLE_PACKAGES.relative_to(ROOT)} missing: run "
            "`npx vite build --config scripts/license-scan.config.js` in frontend/"
        )
    out = []
    for name in json.loads(BUNDLE_PACKAGES.read_text("utf-8")):
        pkg_dir = FRONTEND / "node_modules" / name
        pkg = json.loads((pkg_dir / "package.json").read_text("utf-8"))
        lic = pkg.get("license") or "UNKNOWN"
        if isinstance(lic, dict):
            lic = lic.get("type", "UNKNOWN")
        out.append(
            {
                "name": name,
                "version": pkg["version"],
                "license": lic.strip("()") if " " not in lic.strip("()") else lic,
                "texts": [
                    (p.name, p.read_text("utf-8"))
                    for p in sorted(pkg_dir.iterdir())
                    if p.is_file() and LICENSE_FILE.match(p.name)
                ],
                "url": f"https://www.npmjs.com/package/{name}/v/{pkg['version']}",
            }
        )
    return out


def docs_assets() -> list[dict]:
    versions = dict(
        line.split(" ", 1)
        for line in (DOCS_ASSETS / "VERSIONS").read_text("utf-8").splitlines()
        if line.strip()
    )
    spec = {
        "swagger-ui-dist": (
            "Apache-2.0",
            ["swagger-ui-dist.LICENSE", "swagger-ui-dist.NOTICE"],
        ),
        "redoc": ("MIT", ["redoc.LICENSE"]),
    }
    return [
        {
            "name": name,
            "version": versions[name],
            "license": lic,
            "texts": [(f, (DOCS_ASSETS / f).read_text("utf-8")) for f in files],
            "url": f"https://www.npmjs.com/package/{name}/v/{versions[name]}",
        }
        for name, (lic, files) in spec.items()
    ]


def check_allowed(groups: dict[str, list[dict]]) -> list[str]:
    problems = []
    for group, pkgs in groups.items():
        for p in pkgs:
            ids = spdx_ids(p["license"])
            if not ids or not ids <= ALLOWED:
                problems.append(
                    f"{group}: {p['name']} {p['version']} has license "
                    f"{p['license']!r} (not on the allow list)"
                )
            if not p["texts"]:
                problems.append(f"{group}: {p['name']} ships no license file")
    # Weak copyleft Python packages need their own sentence in render().
    copyleft = {
        norm(p["name"])
        for p in groups["Python"]
        if any(i.startswith(("LGPL-", "MPL-")) for i in spdx_ids(p["license"]))
    }
    for name in sorted(copyleft ^ WEAK_COPYLEFT):
        problems.append(
            f"Python: {name} is weak copyleft but not named in the notices text, or "
            "named there but no longer weak copyleft (WEAK_COPYLEFT, render())"
        )
    return problems


def table(pkgs: list[dict]) -> str:
    rows = ["| Component | Version | License (SPDX) |", "|---|---|---|"]
    rows += [
        f"| [{p['name']}]({p['url']}) | {p['version']} | {p['license']} |" for p in pkgs
    ]
    return "\n".join(rows)


def texts(pkgs: list[dict]) -> str:
    parts = []
    for p in pkgs:
        for fname, body in p["texts"]:
            fence = "````" if "```" in body else "```"
            parts.append(
                f"### {p['name']} {p['version']} – {fname}\n\n{fence}text\n{body.rstrip()}\n{fence}\n"
            )
    return "\n".join(parts)


def render(groups: dict[str, list[dict]]) -> str:
    py, npm, docs = groups["Python"], groups["npm"], groups["API docs"]
    mdi = next(p for p in npm if p["name"] == "@mdi/js")
    return f"""# Third-party notices – Halumar Hub

<!-- Generated by scripts/third_party_notices.py. Do not edit by hand. -->

The Halumar Hub is licensed under the GNU Affero General Public License v3.0 only
(see `LICENSE`, `NOTICE`). It ships the third-party components listed below, each under
its own license. The full license texts follow at the end of this file.

The app image contains this file, `LICENSE` and `NOTICE` in `/usr/share/doc/halumar/`.

## Python packages (app image)

Installed unmodified from PyPI as separate packages (`requirements.txt` and their
dependencies). The source code of every listed version is available at the linked
PyPI page. For zeroconf (LGPL-2.1-or-later) you may replace the installed package
with a modified version of your own; certifi (MPL-2.0) is used unmodified.

{table(py)}

## Admin UI (JavaScript/CSS bundle in `/opt/hub/static`)

npm packages compiled into the browser bundle by Vite (`frontend/`).

{table(npm)}

### Material Design Icons

The icon names and SVG paths in `/opt/hub/data/mdi_paths.py` and in the Admin UI
icon search come from [@mdi/js]({mdi["url"]}) {mdi["version"]} by
[Pictogrammers](https://pictogrammers.com/), licensed under Apache-2.0 (Pictogrammers
Free License). Icons of the category "Brand / Logo" and deprecated icons are not covered
by that license and are **not** included (`frontend/scripts/gen-mdi-icons.mjs`).
Halumar does not ship other brand logos either.

## API documentation pages (`/opt/hub/docs_assets`)

Vendored unmodified (except removed source-map references) for `/api/docs` and
`/api/redoc` (`scripts/update-api-docs.sh`). The license files of the bundled
sub-dependencies are shipped next to them (`*.js.LICENSE.txt`).

{table(docs)}

## Fonts

None. The Admin UI uses the fonts installed on the viewing device.

## Halumar contract

The files listed in `NOTICE` are generated from or copied from
[Halumar/contracts](https://github.com/Halumar/contracts), Copyright 2026 xonatec,
licensed under Apache-2.0: `brand.py` (hub) and `src/lib/brand.ts` (Admin UI), the data
files and value schemas under `data/` that the contract defines, and the shared test
vectors and test data.

## Base image

The app image is built on `ghcr.io/hassio-addons/base` (Alpine Linux with s6-overlay
and Bashio) and adds the Alpine packages `python3`, `py3-pip` and `libffi`. These
components keep their own licenses (among them GPL-2.0, MIT, BSD); their notices and
source code are available from the Alpine Linux package repository
(https://pkgs.alpinelinux.org) and https://github.com/hassio-addons/addon-base. Each
released image carries an SPDX SBOM attestation listing every package
(`gh attestation verify oci://ghcr.io/halumar/<arch>-hub:<version> --owner halumar`).

## License texts

{texts(py + npm + docs)}"""


def inventory(text: str) -> list[tuple[str, str, str]]:
    """(component, version, license) rows of all tables."""
    rows = re.findall(
        r"^\| \[([^\]]+)\]\([^)]*\) \| ([^|]+) \| ([^|]+) \|$", text, re.M
    )
    return [(n, v.strip(), lic.strip()) for n, v, lic in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    direct, transitive = requirements()
    pinned, problems = pins(direct + transitive)
    groups = {
        "Python": python_packages(direct),
        "npm": npm_packages(),
        "API docs": docs_assets(),
    }
    problems += check_pins(pinned, groups["Python"])
    problems += check_allowed(groups)
    text = render(groups)

    if hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() != AGPL_3_0:
        problems.append("LICENSE is not the unchanged GNU AGPL v3 text")
    if args.check:
        current = OUT.read_text("utf-8") if OUT.exists() else ""
        if inventory(current) != inventory(text):
            problems.append(
                f"{OUT.name} is out of date (components, versions or licenses "
                "changed): run scripts/third_party_notices.py"
            )
        elif current != text:
            problems.append(
                f"{OUT.name} is out of date (license texts or wording changed): "
                "run scripts/third_party_notices.py"
            )
        for name in IMAGE_COPIES:
            copy = IMAGE_DOC / name
            if not copy.exists() or copy.read_bytes() != (ROOT / name).read_bytes():
                problems.append(f"{copy.relative_to(ROOT)} differs from {name}")
    else:
        OUT.write_text(text, "utf-8")
        IMAGE_DOC.mkdir(parents=True, exist_ok=True)
        for name in IMAGE_COPIES:
            (IMAGE_DOC / name).write_bytes((ROOT / name).read_bytes())
        print(f"wrote {OUT.relative_to(ROOT)} and {IMAGE_DOC.relative_to(ROOT)}/")

    for p in problems:
        print(f"::error::{p}")
    print(
        f"{len(groups['Python'])} Python, {len(groups['npm'])} npm, "
        f"{len(groups['API docs'])} vendored components"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
