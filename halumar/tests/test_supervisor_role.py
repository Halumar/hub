"""The hub runs with the Supervisor DEFAULT role.

config.yaml requests no `hassio_role`. Every Supervisor endpoint the hub
calls must therefore be reachable with the default role. The patterns
below are copied from the Supervisor's own access check
(supervisor/api/middleware/security.py, `_V1_PATTERNS`, checked 18.11.2026):

- unrestricted: allowed for every app regardless of role
- role_access[default]: allowed with `hassio_api: true` and the default role
- no_security_check: HA Core proxy (needs `homeassistant_api: true`)

The test scans the hub code for Supervisor URLs so a new call to a
manager-only endpoint fails here instead of with 403 on a user's instance.
"""

import os
import re

APP_ROOT = os.path.join(os.path.dirname(__file__), "..", "rootfs", "opt", "hub")
CONFIG_YAML = os.path.join(os.path.dirname(__file__), "..", "config.yaml")

RE_SLUG = r"[-_.A-Za-z0-9]+"

API_UNRESTRICTED = re.compile(
    r"^(?:"
    r"|/addons/self/(?!security|update)[^/]+"
    r"|/addons/self/options/config"
    r"|/info"
    r"|/services.*"
    r"|/discovery.*"
    r"|/auth"
    r")$"
)
ROLE_DEFAULT = re.compile(r"^(?:|/.+/info)$")
NO_SECURITY_CHECK = re.compile(
    r"^(?:"
    r"|/homeassistant/api/.*"
    r"|/homeassistant/websocket"
    r"|/core/api/.*"
    r"|/core/websocket"
    r"|/supervisor/ping"
    r"|/ingress/[-_A-Za-z0-9]+/.*"
    r")$"
)

# Matches "http://supervisor/<path>", "ws://supervisor/<path>" and
# f"{SUPERVISOR_API}/<path>" / f"{HA_API}/<path>" in the source.
URL_RE = re.compile(
    r"""(?:https?|wss?)://supervisor(/[^"'\s?]*)|\{SUPERVISOR_API\}(/[^"'\s?]*)|\{HA_API\}(/[^"'\s?]*)"""
)


def _supervisor_paths() -> set[str]:
    paths: set[str] = set()
    for root, _dirs, files in os.walk(APP_ROOT):
        if "static" in root.split(os.sep):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                src = fh.read()
            for m in URL_RE.finditer(src):
                if m.group(1) is not None:
                    path = m.group(1)
                elif m.group(2) is not None:
                    path = m.group(2)
                else:
                    path = "/core/api" + m.group(3)
                # f-string placeholders -> a concrete sample segment
                path = re.sub(r"\{[^}]*\}", "sample", path).rstrip("`")
                # "/core/api" alone is the HA_API base constant, not a call.
                if path in ("", "/", "/core/api"):
                    continue
                paths.add(path)
    return paths


def _allowed_with_default_role(path: str) -> bool:
    return bool(
        API_UNRESTRICTED.match(path)
        or ROLE_DEFAULT.match(path)
        or NO_SECURITY_CHECK.match(path)
    )


def test_config_yaml_has_no_hassio_role():
    with open(CONFIG_YAML, encoding="utf-8") as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("hassio_role:") for ln in lines)
    # The APIs the hub needs stay enabled.
    text = "".join(lines)
    for key in ("hassio_api: true", "homeassistant_api: true", "auth_api: true"):
        assert key in text, key


def test_config_yaml_names_the_oldest_home_assistant():
    """The Supervisor refuses the hub on an older HA instead of
    letting it fail at runtime (same minimum as the integration's hacs.json)."""
    with open(CONFIG_YAML, encoding="utf-8") as fh:
        lines = [ln.strip() for ln in fh if ln.startswith("homeassistant:")]
    assert lines == ['homeassistant: "2025.11.0"']


def test_scan_finds_the_known_endpoints():
    paths = _supervisor_paths()
    for expected in (
        "/addons/self/info",
        "/addons/self/logs",
        "/addons/self/restart",
        "/discovery",
        "/auth",
        "/info",
        "/core/info",
        "/core/websocket",
        "/core/api/states/sample",
    ):
        assert expected in paths, (expected, sorted(paths))


def test_every_supervisor_endpoint_works_with_default_role():
    denied = sorted(p for p in _supervisor_paths() if not _allowed_with_default_role(p))
    assert denied == [], f"endpoints that need a Supervisor role: {denied}"


def test_manager_only_endpoints_are_rejected_by_the_check():
    """Sanity check of the copied patterns: manager endpoints are NOT allowed."""
    for path in (
        "/backups",
        "/addons/core_ssh/restart",
        "/host/reboot",
        "/store/reload",
    ):
        assert not _allowed_with_default_role(path), path
