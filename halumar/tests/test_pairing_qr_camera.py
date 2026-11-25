"""Pairing QR in the integration's camera (app option pairing_qr_camera).

Under test:
(a) the option: config.yaml schema/default, translations, reading it
    (missing/unknown = off), reported in GET /api/integration/status.
(b) GET /api/integration/pairing-qr for every mode x window state (none,
    member window, guest window, expired, PIN used): QR with
    ``X-Pairing-Pin: included`` + ``X-Pairing-Kind`` only when the mode
    allows the window, else the status image with ``omitted``.
(c) the QR encodes the canonical deep link of the admin UI; the PIN is in no
    header and no log line.
(d) one audit entry per window, not per poll.
(e) a guest window's PIN only ever creates a guest with the profile and end
    fixed when the window opened, whatever the app sends; admin sessions
    are told about the new guest.
"""

import json
import logging
import time
import types
from pathlib import Path

import pytest
import yaml

import api.integration as apiintg
import api.pairing as pairing
import api.websocket as ws
import services.tls as tls
from api import ws as api_ws
from services import audit_log, crypto, pairing_link, pairing_qr
from services import device_provisioning as dp
from services.profiles import rows as profile_rows
from starlette.websockets import WebSocketState

from .profile_helpers import starter_profile

ADDON = Path(__file__).resolve().parents[1]
PIN = "3477-3800"
LAN = "https://192.0.2.10:8100"
CLIENT_INFO = {"protocol_version": 1, "platform": "ios", "app_version": "2026.12.0"}
REAL_LAN_BASE_URL = pairing_link.resolve_lan_base_url


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    dp.reset()
    ws._clients.clear()
    pairing.clear_active_pins()
    pairing._pair_rate_limits.clear()
    audit_log.reset_recent()
    monkeypatch.setattr(tls, "get_fingerprint", lambda *a, **k: None)
    monkeypatch.setattr(pairing, "_generate_pin", lambda: PIN)

    async def _sender(_message):
        return True

    monkeypatch.setattr(dp, "_sender", _sender)

    async def _base_url(_app, _db):
        return LAN

    monkeypatch.setattr(pairing_link, "resolve_lan_base_url", _base_url)
    yield
    ws._clients.clear()
    pairing.clear_active_pins()
    audit_log.reset_recent()
    crypto.reset_cache()
    dp.reset()


@pytest.fixture
def broadcasts(monkeypatch):
    sent: list[dict] = []

    async def _broadcast(message, *a, **k):
        sent.append(message)

    monkeypatch.setattr(api_ws, "broadcast", _broadcast)
    return sent


@pytest.fixture
def set_mode(monkeypatch, tmp_path):
    """Write the app options file with ``pairing_qr_camera`` (None = absent)."""
    path = tmp_path / "options.json"
    monkeypatch.setattr(pairing_qr, "OPTIONS_PATH", str(path))

    def _set(mode):
        options = {"log_level": "info"}
        if mode is not None:
            options["pairing_qr_camera"] = mode
        path.write_text(json.dumps(options))

    _set(None)
    return _set


@pytest.fixture
def qr_links(monkeypatch):
    """Links handed to segno (the real PNG is still rendered)."""
    links: list[str] = []
    real = pairing_qr.segno.make

    def _make(data, **kwargs):
        links.append(data)
        return real(data, **kwargs)

    monkeypatch.setattr(pairing_qr.segno, "make", _make)
    return links


def _integration_request(db, body=None):
    raw = json.dumps(body).encode() if body is not None else b""

    async def _body():
        return raw

    return types.SimpleNamespace(
        state=types.SimpleNamespace(auth_type="integration"),
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url="")
        ),
        headers={"content-length": str(len(raw))},
        body=_body,
        client=types.SimpleNamespace(host="192.0.2.5"),
        url=types.SimpleNamespace(scheme="http"),
    )


def _admin_request(db):
    """Admin through HA Ingress (trusted Supervisor address)."""
    request = _integration_request(db)
    request.state.auth_type = "admin"
    request.headers = {"x-ingress-path": "/api/hassio_ingress/abc"}
    request.client = types.SimpleNamespace(host="172.30.32.2")
    return request


async def _open(db, kind):
    """Open a pairing window through the integration (guest or device)."""
    body = {"is_guest": kind == "guest"}
    if kind == "guest":
        body["guest_expires_hours"] = 5
    result = await apiintg.integration_start_pairing(_integration_request(db, body))
    assert result["pin"] == PIN
    return result


async def _image(db, accept=True):
    """The camera image as an integration that can show the QR asks for it."""
    request = _integration_request(db)
    if accept:
        request.headers = {**request.headers, "X-Pairing-Qr": "accept"}
    return await apiintg.integration_pairing_qr(request)


def _is_qr(resp) -> bool:
    return resp.headers["X-Pairing-Pin"] == "included"


class _WS:
    def __init__(self, db, host="198.51.100.7"):
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(db=db, ha_client=None, app_url="")
        )
        self.headers = {}
        self.cookies = {}
        self.client = types.SimpleNamespace(host=host)
        self.url = types.SimpleNamespace(hostname="hub", port=8100, scheme="wss")
        self.sent = []
        self.application_state = WebSocketState.CONNECTED
        ws._clients[self] = {
            "authenticated": False,
            "client_id": None,
            "pair_attempts": 0,
            "auth_attempts": 0,
            "connected_at": time.time(),
            "ip": host,
        }

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=""):
        pass


async def _pair(db, extra=None):
    conn = _WS(db)
    ok, client_id = await ws._handle_pair(
        conn, db, {"pin": PIN, "client_name": "Gast", **CLIENT_INFO, **(extra or {})}
    )
    return ok, client_id


# ── (a) the option ───────────────────────────────────────────────────────────


class TestOption:
    def test_config_schema_and_default_off(self):
        config = yaml.safe_load((ADDON / "config.yaml").read_text())
        assert config["options"]["pairing_qr_camera"] == "off"
        assert config["schema"]["pairing_qr_camera"] == "list(off|guests|all)"

    @pytest.mark.parametrize("lang", ["en", "de"])
    def test_translated_with_the_warning(self, lang):
        text = yaml.safe_load((ADDON / "translations" / f"{lang}.yaml").read_text())
        entry = text["configuration"]["pairing_qr_camera"]
        assert entry["name"]
        for value in ("off", "guests", "all"):
            assert f"{value}" in entry["description"]
        marker = {"en": "guest and child devices", "de": "Gast- und Kindergeräte"}
        assert marker[lang] in entry["description"]

    @pytest.mark.parametrize(
        ("value", "mode"),
        [
            (None, "off"),
            ("off", "off"),
            ("guests", "guests"),
            ("all", "all"),
            ("ALL", "off"),
            ("yes", "off"),
            (True, "off"),
        ],
    )
    def test_reading(self, set_mode, value, mode):
        set_mode(value)
        assert pairing_qr.camera_mode() == mode

    def test_missing_or_broken_file_is_off(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pairing_qr, "OPTIONS_PATH", str(tmp_path / "none.json"))
        assert pairing_qr.camera_mode() == "off"
        broken = tmp_path / "broken.json"
        broken.write_text("{")
        monkeypatch.setattr(pairing_qr, "OPTIONS_PATH", str(broken))
        assert pairing_qr.camera_mode() == "off"

    @pytest.mark.parametrize("mode", ["off", "guests", "all"])
    async def test_in_the_integration_status(self, db, set_mode, broadcasts, mode):
        set_mode(mode)
        status = await apiintg.integration_status(_integration_request(db))
        assert status["pairing_qr_camera"] == mode


# ── (b) mode x window ────────────────────────────────────────────────────────

# (mode, window) -> QR shown?
MATRIX = [
    ("off", None, False),
    ("off", "device", False),
    ("off", "guest", False),
    ("guests", None, False),
    ("guests", "device", False),
    ("guests", "guest", True),
    ("all", None, False),
    ("all", "device", True),
    ("all", "guest", True),
]


class TestModes:
    @pytest.mark.parametrize(("mode", "window", "shown"), MATRIX)
    async def test_matrix(
        self, db, set_mode, broadcasts, qr_links, mode, window, shown
    ):
        set_mode(mode)
        if window:
            await _open(db, window)
        resp = await _image(db)

        assert resp.media_type == "image/png"
        assert resp.body.startswith(b"\x89PNG")
        assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
        assert resp.headers["X-Pairing-Active"] == ("true" if window else "false")
        if shown:
            assert resp.headers["X-Pairing-Pin"] == "included"
            assert resp.headers["X-Pairing-Kind"] == window
            assert resp.headers["X-Pairing-Window"].isdigit()
            assert len(qr_links) == 1
        else:
            assert resp.headers["X-Pairing-Pin"] == "omitted"
            assert "X-Pairing-Kind" not in resp.headers
            assert "X-Pairing-Window" not in resp.headers
            assert qr_links == []
        for value in resp.headers.values():
            assert PIN not in value and "3477" not in value

    @pytest.mark.parametrize("mode", ["guests", "all"])
    async def test_expired_window(self, db, set_mode, broadcasts, monkeypatch, mode):
        set_mode(mode)
        await _open(db, "guest")
        assert _is_qr(await _image(db))
        real = time.time
        monkeypatch.setattr(
            time, "time", lambda: real() + pairing.PAIRING_TIMEOUT_SECONDS + 1
        )
        resp = await _image(db)
        assert resp.headers["X-Pairing-Pin"] == "omitted"
        assert resp.headers["X-Pairing-Active"] == "false"

    @pytest.mark.parametrize("mode", ["guests", "all"])
    async def test_used_pin(self, db, set_mode, broadcasts, mode):
        set_mode(mode)
        await _open(db, "guest")
        assert _is_qr(await _image(db))
        ok, _cid = await _pair(db)
        assert ok
        resp = await _image(db)
        assert resp.headers["X-Pairing-Pin"] == "omitted"
        assert resp.headers["X-Pairing-Active"] == "false"

    async def test_cancelled_window(self, db, set_mode, broadcasts):
        set_mode("all")
        await _open(db, "device")
        assert _is_qr(await _image(db))
        await apiintg.integration_cancel_pairing(_integration_request(db))
        assert (await _image(db)).headers["X-Pairing-Pin"] == "omitted"

    async def test_window_ending_while_building_the_qr(
        self, db, set_mode, broadcasts, monkeypatch
    ):
        """A PIN used while the link is built is not shown any more."""
        set_mode("all")
        await _open(db, "device")
        from services import home_networks

        async def _cidrs(_db):
            pairing.clear_active_pins()
            return []

        monkeypatch.setattr(home_networks, "cidrs", _cidrs)
        resp = await _image(db)
        assert resp.headers["X-Pairing-Pin"] == "omitted"
        assert resp.headers["X-Pairing-Active"] == "false"

    async def test_mode_switch_takes_effect_on_the_next_request(
        self, db, set_mode, broadcasts
    ):
        set_mode("all")
        await _open(db, "device")
        assert _is_qr(await _image(db))
        set_mode("off")
        assert (await _image(db)).headers["X-Pairing-Pin"] == "omitted"

    async def test_admin_window_counts_too(self, db, set_mode, broadcasts):
        """A guest window opened in the admin UI is a guest window as well."""
        set_mode("guests")
        await pairing.generate_pairing_pin(
            _admin_request(db), pairing.GeneratePinRequest(access="guest")
        )
        resp = await _image(db)
        assert resp.headers["X-Pairing-Kind"] == "guest"
        await pairing.generate_pairing_pin(_admin_request(db), None)
        assert (await _image(db)).headers["X-Pairing-Pin"] == "omitted"


class TestStatusImage:
    @pytest.mark.parametrize(
        ("mode", "active", "lang", "expected"),
        [
            (
                "off",
                False,
                "en",
                (
                    "STANDBY",
                    ("QR CODE OFF", "ENABLE IT IN", "THE APP'S", "CONFIGURATION"),
                ),
            ),
            (
                "off",
                True,
                "de",
                (
                    "PAIRING",
                    ("QR-CODE AUS", "EINSCHALTEN IN", "DER APP-", "KONFIGURATION"),
                ),
            ),
            ("guests", True, "en", ("PAIRING", ("QR CODE ONLY", "FOR GUESTS"))),
            ("guests", True, "de", ("PAIRING", ("QR-CODE NUR", "FÜR GÄSTE"))),
            ("guests", False, "en", ("STANDBY", ())),
            ("all", False, "de", ("STANDBY", ())),
        ],
    )
    def test_texts(self, mode, active, lang, expected):
        assert pairing_qr.status_text(mode, active, lang) == expected

    def test_every_character_has_a_glyph_and_fits(self):
        for texts in pairing_qr._LINES.values():
            for lines in texts.values():
                assert len(lines) <= 4
                for line in lines:
                    assert len(line) <= 15, line
                    for ch in line.replace(" ", ""):
                        assert ch in apiintg._STATUS_FONT, ch

    async def test_off_image_differs_from_plain_standby(self, db, set_mode):
        set_mode("off")
        off = (await _image(db)).body
        set_mode("all")
        standby = (await _image(db)).body
        assert off != standby
        assert off.startswith(b"\x89PNG") and standby.startswith(b"\x89PNG")


# ── (c) QR content, no PIN in headers or logs ───────────────────────────────


class TestQrContent:
    async def test_same_link_as_the_admin_ui(
        self, db, set_mode, broadcasts, qr_links, monkeypatch
    ):
        monkeypatch.setattr(pairing_link, "resolve_lan_base_url", REAL_LAN_BASE_URL)
        await db.set_setting("ha_internal_url", "http://192.0.2.10:8123")
        set_mode("all")
        await _open(db, "device")
        await _image(db)

        status = await pairing.pairing_status(_admin_request(db))
        assert status["pin"] == PIN
        assert qr_links == [status["deep_link"]]
        assert qr_links[0] == (
            "halumar://pair?url=http%3A%2F%2F192.0.2.10%3A8099&pin=3477-3800"
        )

    async def test_lan_address_not_the_integration_host(
        self, db, set_mode, broadcasts, qr_links
    ):
        """The integration may reach the hub under an internal name."""
        set_mode("guests")
        await _open(db, "guest")
        request = _integration_request(db)
        request.headers = {"host": "hub.example.test:8099", "X-Pairing-Qr": "accept"}
        await apiintg.integration_pairing_qr(request)
        assert "example.test" not in qr_links[0]
        assert "192.0.2.10" in qr_links[0]

    async def test_no_pin_in_any_log_line(self, db, set_mode, broadcasts, caplog):
        caplog.set_level(logging.DEBUG)
        set_mode("all")
        await _open(db, "guest")
        for _ in range(3):
            await _image(db)
        await _pair(db)
        await _image(db)
        assert "PAIRING_QR_SHOWN kind=guest" in caplog.text
        assert PIN not in caplog.text
        assert "3477" not in caplog.text and "3800" not in caplog.text
        for entry in audit_log.recent_entries():
            assert PIN not in json.dumps(entry)


# ── (d) audit once per window ───────────────────────────────────────────────


class TestAudit:
    async def test_once_per_window(self, db, set_mode, broadcasts):
        set_mode("all")
        await _open(db, "guest")
        for _ in range(5):
            await _image(db)
        shown = [
            e for e in audit_log.recent_entries() if e["event"] == "pairing_qr_shown"
        ]
        assert len(shown) == 1
        assert shown[0]["details"] == "guest"
        assert shown[0]["category"] == audit_log.CATEGORY_SECURITY

        await _open(db, "device")  # a new window
        await _image(db)
        await _image(db)
        shown = [
            e for e in audit_log.recent_entries() if e["event"] == "pairing_qr_shown"
        ]
        assert [e["details"] for e in shown] == ["device", "guest"]

    async def test_none_without_qr(self, db, set_mode, broadcasts):
        set_mode("guests")
        await _open(db, "device")
        await _image(db)
        assert not [
            e for e in audit_log.recent_entries() if e["event"] == "pairing_qr_shown"
        ]


# ── (e) what a guest window's PIN can create ────────────────────────────────


class TestGuestWindowBoundary:
    async def test_always_a_guest_with_the_fixed_profile_and_end(
        self, db, set_mode, broadcasts
    ):
        set_mode("guests")
        guest_profile = await starter_profile(db, "guest")
        adult = await starter_profile(db, "adult")
        await _open(db, "guest")
        window = pairing.get_active_pin()[1]
        assert window["profile_id"] == guest_profile

        # The admin changes the guest default before the PIN is used.
        await db.set_setting(profile_rows.default_setting_key("guest"), adult)

        ok, client_id = await _pair(
            db,
            {
                "is_guest": False,
                "access": "member",
                "profile_id": adult,
                "guest_expires_at": None,
                "expires_at": None,
                "kind": "adult",
            },
        )
        assert ok
        row = await db.fetchone(
            "SELECT access, expires_at, profile_id FROM paired_clients WHERE id = ?",
            (client_id,),
        )
        assert row == {
            "access": "guest",
            "expires_at": window["guest_expires_at"],
            "profile_id": guest_profile,
        }

    async def test_admin_window_fixes_the_guest_default_too(self, db, broadcasts):
        guest_profile = await starter_profile(db, "guest")
        await pairing.generate_pairing_pin(
            _admin_request(db), pairing.GeneratePinRequest(access="guest")
        )
        assert pairing.get_active_pin()[1]["profile_id"] == guest_profile

    async def test_admin_sessions_hear_of_the_new_guest(self, db, set_mode, broadcasts):
        set_mode("guests")
        await _open(db, "guest")
        ok, client_id = await _pair(db)
        assert ok
        paired = [m for m in broadcasts if m["type"] == "client_paired"]
        assert paired == [
            {
                "type": "client_paired",
                "data": {
                    "client_id": client_id,
                    "client_name": "Gast",
                    "is_guest": True,
                },
            }
        ]


# ── older integrations, unknown option values, camera-QR pairings ───────────


class TestOlderIntegration:
    async def test_no_qr_and_no_audit_without_the_accept_header(
        self, db, set_mode, broadcasts, qr_links
    ):
        set_mode("all")
        await _open(db, "guest")
        resp = await _image(db, accept=False)
        assert resp.headers["X-Pairing-Pin"] == "omitted"
        assert qr_links == []
        assert not [
            e for e in audit_log.recent_entries() if e["event"] == "pairing_qr_shown"
        ]


class TestUnknownValue:
    def test_warned_once(self, set_mode, caplog, monkeypatch):
        monkeypatch.setattr(pairing_qr, "_warned_unknown", False)
        set_mode("everyone")
        caplog.set_level(logging.WARNING)
        for _ in range(5):
            assert pairing_qr.camera_mode() == "off"
        warnings = [r for r in caplog.records if "pairing_qr_camera" in r.getMessage()]
        assert len(warnings) == 1


class TestPairedWithTheCameraCode:
    @pytest.mark.parametrize("shown", [True, False])
    async def test_log_and_notification_say_so(self, db, set_mode, broadcasts, shown):
        from services import event_log, event_notify

        set_mode("guests")
        await _open(db, "guest")
        if shown:
            assert _is_qr(await _image(db))
        ok, client_id = await _pair(db)
        assert ok
        entries = [
            e
            for e in (await event_log.list_entries(db, {}, limit=50))["entries"]
            if e.get("client_id") == client_id and e.get("kind") == "guest_paired"
        ]
        assert len(entries) == 1
        entry = entries[0]
        assert bool((entry.get("detail") or {}).get("camera_qr")) is shown
        text_en = event_notify.message(entry, "en")
        text_de = event_notify.message(entry, "de")
        assert (
            "paired with the code shown on the Home Assistant camera" in text_en
        ) is shown
        assert ("mit dem Code der Home-Assistant-Kamera gekoppelt" in text_de) is shown


class TestWindowId:
    async def test_differs_per_window(self, db, set_mode, broadcasts, monkeypatch):
        set_mode("all")
        clock = [1_800_000_000.0]
        monkeypatch.setattr(time, "time", lambda: clock[0])
        await _open(db, "guest")
        first = (await _image(db)).headers["X-Pairing-Window"]
        assert (await _image(db)).headers["X-Pairing-Window"] == first
        clock[0] += 0.5  # a new window within the same second
        await _open(db, "guest")
        second = (await _image(db)).headers["X-Pairing-Window"]
        assert second != first
