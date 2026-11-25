"""Protocol version and client-info check (contract docs/protocol.md).

One integer ``PROTOCOL_VERSION`` covers the whole hub protocol. Additive
changes (new optional fields, message types, enum values) do not bump it,
clients decode tolerantly. Compatibility rule: the hub serves every client
protocol version from ``MIN_PROTOCOL_VERSION`` up to ``PROTOCOL_VERSION`` and
``MIN_PROTOCOL_VERSION <= max(1, PROTOCOL_VERSION - SUPPORTED_PREVIOUS_VERSIONS)``,
so a hub always supports at least the two previous protocol versions
(``tests/test_protocol_version.py`` enforces it).

Apps send their client info with ``auth`` and ``pair``: ``protocol_version``,
``platform``, ``app_version`` (required), ``app_build``, ``device_model``.
A missing or too low protocol version, or an app version below
``MIN_APP_VERSION[platform]``, is answered with ``upgrade_required`` and
close code 4426; malformed client info with ``auth_error``/``pair_error``.
The platform is stored at pairing and never changes with ``auth``
(``same_device_platform``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from brand import BRAND_NAME

PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1
SUPPORTED_PREVIOUS_VERSIONS = 2

# The Android values are reserved before the v1.0 freeze (no Android app yet).
PLATFORMS = (
    "ios",
    "ipados",
    "tvos",
    "watchos",
    "android",
    "android_tablet",
    "android_tv",
    "wearos",
    "web",
)

# Lowest accepted app version per platform (a platform without an entry has
# no floor). Raised only when a hub release needs a fixed app build.
MIN_APP_VERSION: dict[str, str] = {
    "ios": "2026.12.0",
    "ipados": "2026.12.0",
    "tvos": "2026.12.0",
    "watchos": "2026.12.0",
}

# The platform of a paired device is fixed at pairing: profile defaults,
# rules per platform, watch, panel and TV handling depend on it. ``auth``
# never changes it. Only these platforms may be reported in place of each
# other (the Android app tells phone from tablet by the current screen width,
# a foldable reports both); the stored one stays. Any other difference is
# refused (``same_device_platform``).
PLATFORM_SIBLINGS = frozenset({"android", "android_tablet"})

UPGRADE_REQUIRED_CLOSE_CODE = 4426
UPGRADE_REASON_PROTOCOL = "protocol"
UPGRADE_REASON_APP_VERSION = "app_version"

_APP_VERSION_RE = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6}){0,3}([-+][0-9A-Za-z.-]{1,20})?")
_APP_BUILD_RE = re.compile(r"[0-9A-Za-z.-]{1,32}")
_DEVICE_MODEL_RE = re.compile(r"[ -~]{1,64}")

_STORE_HINT = {
    "ios": "Please update the app in the App Store.",
    "ipados": "Please update the app in the App Store.",
    "tvos": "Please update the app in the App Store.",
    "watchos": "Please update the app on your iPhone in the App Store.",
    "android": "Please update the app in Google Play.",
    "android_tablet": "Please update the app in Google Play.",
    "android_tv": "Please update the app in Google Play.",
    "wearos": "Please update the app on your watch in Google Play.",
    "web": "Please reload the page to load the current version.",
}


def protocol_fields() -> dict:
    """``protocol_version``, ``min_protocol_version``, ``min_app_version``
    (auth_ok, pair_ok, upgrade_required, /api/health)."""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "min_protocol_version": MIN_PROTOCOL_VERSION,
        "min_app_version": dict(MIN_APP_VERSION),
    }


def txt_fields() -> dict[str, str]:
    """Bonjour TXT keys ``pv`` and ``pv_min``."""
    return {"pv": str(PROTOCOL_VERSION), "pv_min": str(MIN_PROTOCOL_VERSION)}


def version_key(version: str) -> tuple[int, ...]:
    """Numeric parts of ``2026.12.0[-suffix]`` for comparison (suffix ignored)."""
    core = re.split(r"[-+]", version, maxsplit=1)[0]
    parts = [int(p) for p in core.split(".") if p.isdigit()]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_valid_app_version(value) -> bool:
    return isinstance(value, str) and _APP_VERSION_RE.fullmatch(value) is not None


def _valid_protocol_version(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


@dataclass(frozen=True)
class ClientInfo:
    """Client info of an ``auth``/``pair`` message (best effort when rejected)."""

    protocol_version: int | None
    platform: str | None
    app_version: str | None
    app_build: str | None = None
    device_model: str | None = None

    def columns(self) -> dict:
        """Values for the ``paired_clients`` columns."""
        return {
            "protocol_version": self.protocol_version,
            "platform": self.platform,
            "app_version": self.app_version,
            "app_build": self.app_build,
            "device_model": self.device_model,
        }


@dataclass(frozen=True)
class Evaluation:
    info: ClientInfo
    error: str | None = None
    upgrade_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.upgrade_reason is None


def evaluate(message: dict, paired_platform: str | None = None) -> Evaluation:
    """Check the client info of an ``auth``/``pair`` message.

    Order: protocol version first (an app that predates the versioned
    protocol sends none of the fields), then the field formats, then the
    app-version floor of the platform. ``paired_platform`` = the platform a
    paired device was stored with at pairing: its floor applies, not the one
    of the reported platform.
    """
    pv = message.get("protocol_version")
    platform = message.get("platform")
    app_version = message.get("app_version")
    app_build = message.get("app_build")
    device_model = message.get("device_model")
    info = ClientInfo(
        protocol_version=pv if _valid_protocol_version(pv) else None,
        platform=platform if platform in PLATFORMS else None,
        app_version=app_version if is_valid_app_version(app_version) else None,
        app_build=(
            app_build
            if isinstance(app_build, str) and _APP_BUILD_RE.fullmatch(app_build)
            else None
        ),
        device_model=(
            device_model
            if isinstance(device_model, str)
            and _DEVICE_MODEL_RE.fullmatch(device_model)
            else None
        ),
    )

    if info.protocol_version is None or info.protocol_version < MIN_PROTOCOL_VERSION:
        return Evaluation(info, upgrade_reason=UPGRADE_REASON_PROTOCOL)

    invalid = []
    if info.platform is None:
        invalid.append("platform")
    if info.app_version is None:
        invalid.append("app_version")
    if app_build is not None and info.app_build is None:
        invalid.append("app_build")
    if device_model is not None and info.device_model is None:
        invalid.append("device_model")
    if invalid:
        return Evaluation(info, error=f"Invalid client info: {', '.join(invalid)}")

    floor = MIN_APP_VERSION.get(paired_platform or info.platform)
    if floor and version_key(info.app_version) < version_key(floor):
        return Evaluation(info, upgrade_reason=UPGRADE_REASON_APP_VERSION)
    return Evaluation(info)


def same_device_platform(paired: str | None, reported: str | None) -> bool:
    """True when ``reported`` may come from a device paired as ``paired``.

    A device stored without a platform keeps none, whatever it reports.
    """
    if paired is None or paired == reported:
        return True
    return paired in PLATFORM_SIBLINGS and reported in PLATFORM_SIBLINGS


def upgrade_required_message(evaluation: Evaluation) -> dict:
    """The ``upgrade_required`` answer (the caller closes with 4426)."""
    platform = evaluation.info.platform
    hint = _STORE_HINT.get(platform, "Please update the app.")
    return {
        "type": "upgrade_required",
        "reason": evaluation.upgrade_reason,
        "message": f"This version of the {BRAND_NAME} app is too old for this hub. {hint}",
        **protocol_fields(),
        "platform": platform,
    }


def update_required(row: dict) -> bool:
    """Admin hint: would this device get ``upgrade_required`` on its next connect?

    True when the device has not reported a valid protocol version yet, or
    its last reported protocol or app version is below the current minimums.
    """
    pv = row.get("protocol_version")
    if not isinstance(pv, int) or pv < MIN_PROTOCOL_VERSION:
        return True
    floor = MIN_APP_VERSION.get(row.get("platform") or "")
    app_version = row.get("app_version")
    if not floor:
        return False
    return not is_valid_app_version(app_version) or version_key(
        app_version
    ) < version_key(floor)
