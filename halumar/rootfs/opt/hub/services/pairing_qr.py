"""Pairing QR in the integration's camera (app option ``pairing_qr_camera``).

Every user who can sign in to Home Assistant can read a camera image, the
device users the integration creates for paired devices included. The camera
therefore shows the QR with the pairing PIN only when the admin switched it on
in the app configuration:

* ``off`` (default): never; the camera shows a status image.
* ``guests``: only while a guest pairing window is open. A guest PIN can only
  create a guest device with the profile and end fixed when the window was
  opened (``api/ws/handshake._handle_pair`` takes both from the PIN, never
  from the app).
* ``all``: during every pairing window.

The PIN is never logged; the first QR of a window is noted once in the audit
log (``pairing_qr_shown``, the kind of the window).
"""

from __future__ import annotations

import io
import json
import logging
import os
from typing import Literal

import segno

from services.audit_log import log_pairing_qr_shown

logger = logging.getLogger("hub.services.pairing_qr")

OPTIONS_PATH = "/data/options.json"
OPTION = "pairing_qr_camera"

MODE_OFF = "off"
MODE_GUESTS = "guests"
MODE_ALL = "all"
MODES = (MODE_OFF, MODE_GUESTS, MODE_ALL)

KIND_GUEST = "guest"
KIND_DEVICE = "device"

# Marks a pairing window whose QR was shown once (audit entry written); a
# device paired with its PIN is logged as paired with the camera's code.
SHOWN_KEY = "qr_camera_shown"

# Request header of an integration that can show the QR (it checks the mode
# and never caches the image). Without it the hub sends the status image, so
# an older integration never receives a QR and no audit entry is written.
ACCEPT_HEADER = "X-Pairing-Qr"
ACCEPT_VALUE = "accept"

_warned_unknown = False

Mode = Literal["off", "guests", "all"]


def camera_mode() -> Mode:
    """The app option, read per call (cheap); anything unknown is ``off``."""
    try:
        if os.path.isfile(OPTIONS_PATH):
            with open(OPTIONS_PATH) as f:
                value = json.load(f).get(OPTION, MODE_OFF)
            if value in MODES:
                return value
            global _warned_unknown
            if not _warned_unknown:
                _warned_unknown = True
                logger.warning("Unknown %s value; the camera shows no QR code", OPTION)
    except Exception as e:  # noqa: BLE001 - off is the safe default
        logger.warning("Could not read %s: %s", OPTIONS_PATH, e)
    return MODE_OFF


def window_kind(pin_data: dict) -> str:
    """``guest`` for a guest pairing window, else ``device``."""
    return KIND_GUEST if pin_data.get("is_guest") else KIND_DEVICE


def window_id(pin_data: dict) -> str:
    """Opaque id of a pairing window (when it opened, in ms; not the PIN).

    The integration ends a camera stream when it changes, so a stream never
    carries the QR of a later window.
    """
    return str(int(pin_data.get("created_at", 0) * 1000))


def shows_pin(mode: str, pin_data: dict) -> bool:
    """Whether the camera may show the QR of this pairing window."""
    if mode == MODE_ALL:
        return True
    return mode == MODE_GUESTS and window_kind(pin_data) == KIND_GUEST


def render_qr(deep_link: str) -> bytes:
    """PNG of the canonical pairing deep link (dark modules on white)."""
    buf = io.BytesIO()
    segno.make(deep_link, error="m").save(
        buf, kind="png", scale=6, border=4, dark="#1a1a2e", light="#ffffff"
    )
    return buf.getvalue()


def note_shown(pin_data: dict) -> None:
    """Audit entry for the first QR of a window (not for every poll)."""
    if pin_data.get(SHOWN_KEY):
        return
    pin_data[SHOWN_KEY] = True
    log_pairing_qr_shown(window_kind(pin_data))


# Status image texts (title, lines) per language. The title stays as before
# ("STANDBY" / "PAIRING"); the lines explain why no QR code is shown.
_LINES = {
    "en": {
        "off": ("QR CODE OFF", "ENABLE IT IN", "THE APP'S", "CONFIGURATION"),
        "guests_only": ("QR CODE ONLY", "FOR GUESTS"),
    },
    "de": {
        "off": ("QR-CODE AUS", "EINSCHALTEN IN", "DER APP-", "KONFIGURATION"),
        "guests_only": ("QR-CODE NUR", "FÜR GÄSTE"),
    },
}


def status_text(
    mode: str, pairing_active: bool, lang: str
) -> tuple[str, tuple[str, ...]]:
    """Title and lines of the status image shown instead of a QR code."""
    texts = _LINES["de" if lang == "de" else "en"]
    title = "PAIRING" if pairing_active else "STANDBY"
    if mode == MODE_OFF:
        return title, texts["off"]
    if pairing_active and mode == MODE_GUESTS:
        return title, texts["guests_only"]
    return title, ()
