"""The picture marker ``hub_picture_id`` of media players and cameras.

Home Assistant's picture link of these entities carries the entity's access
token. The hub never relays it (get_area, the state_subscribe snapshot,
entity_state changes) and sends ``attributes.hub_picture_id`` instead: an
HMAC with a key of this hub over the link without its token. A renewal of
only the token changes nothing a device sees and is not relayed; a new
artwork is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

import pytest
from api import authz
from services import crypto, state_relay

from .test_hub_path import _WS, FakeHA, _area, _guest, _send, _clean_state  # noqa: F401

MARKER = re.compile(r"^[0-9a-f]{16}$")
PLAYER = "media_player.living"
CAMERA = "camera.door"


def _player(picture: str | None = None, **attributes):
    attrs = {"friendly_name": "Living room", "media_title": "Evening song"}
    if picture is not None:
        attrs["entity_picture"] = picture
    attrs.update(attributes)
    return {"entity_id": PLAYER, "state": "playing", "attributes": attrs}


def _art(token: str, cache: str = "c1") -> str:
    return f"/api/media_player_proxy/{PLAYER}?token={token}&cache={cache}"


def _flat(value) -> str:
    return json.dumps(value, sort_keys=True)


# ── the marker ──────────────────────────────────────────────────────────────


def test_marker_is_an_hmac_with_a_derived_key_over_the_link_without_token():
    attrs = authz.sanitize_attributes(PLAYER, _player(_art("t1"))["attributes"])
    assert "entity_picture" not in attrs
    marker = attrs["hub_picture_id"]
    assert MARKER.match(marker)
    key = crypto.derived_key(authz.PICTURE_ID_PURPOSE)
    link = f"/api/media_player_proxy/{PLAYER}?cache=c1"
    expected = hmac.new(key, link.encode(), hashlib.sha256).hexdigest()[:16]
    assert marker == expected
    # Not keyed with the key file's key itself.
    raw = crypto._key_bytes
    assert raw and key != raw
    assert marker != hmac.new(raw, link.encode(), hashlib.sha256).hexdigest()[:16]


def test_marker_ignores_the_token_and_the_parameter_order():
    one = authz.picture_id(f"/api/media_player_proxy/{PLAYER}?token=a&cache=c1")
    two = authz.picture_id(f"/api/media_player_proxy/{PLAYER}?cache=c1&token=b")
    three = authz.picture_id(f"/api/media_player_proxy/{PLAYER}?cache=c1")
    assert one == two == three


def test_marker_changes_with_the_artwork():
    first = authz.picture_id(_art("t", "c1"))
    assert authz.picture_id(_art("t", "c2")) != first
    other = "/api/media_player_proxy/media_player.kitchen?token=t&cache=c1"
    assert authz.picture_id(other) != first


def test_marker_differs_between_hubs(monkeypatch, tmp_path):
    first = authz.picture_id(_art("t"))
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / "other" / "key"))
    (tmp_path / "other").mkdir()
    crypto.reset_cache()
    assert authz.picture_id(_art("t")) != first


def test_derived_keys_are_independent_per_purpose():
    one = crypto.derived_key(b"purpose one")
    assert one == crypto.derived_key(b"purpose one")
    assert one != crypto.derived_key(b"purpose two") and len(one) == 32


def test_without_a_usable_key_there_is_no_marker_and_no_link(monkeypatch):
    def _broken(_purpose):
        raise crypto.KeyfileError("damaged")

    monkeypatch.setattr(crypto, "derived_key", _broken)
    attrs = authz.sanitize_attributes(PLAYER, _player(_art("t"))["attributes"])
    assert "hub_picture_id" not in attrs and "entity_picture" not in attrs
    assert attrs["media_title"] == "Evening song"


def test_camera_marker_stays_while_the_token_rotates():
    a = authz.sanitize_attributes(
        CAMERA,
        {
            "access_token": "tok-1",
            "entity_picture": f"/api/camera_proxy/{CAMERA}?token=tok-1",
        },
    )
    b = authz.sanitize_attributes(
        CAMERA,
        {
            "access_token": "tok-2",
            "entity_picture": f"/api/camera_proxy/{CAMERA}?token=tok-2",
        },
    )
    assert a == b and set(a) == {"hub_picture_id"}


def test_no_marker_without_a_picture():
    assert "hub_picture_id" not in authz.sanitize_attributes(
        PLAYER, _player()["attributes"]
    )
    assert "hub_picture_id" not in authz.sanitize_attributes(
        PLAYER, _player("")["attributes"]
    )
    assert authz.sanitize_attributes(CAMERA, {"access_token": "x"}) == {}


def test_a_marker_from_home_assistant_is_not_relayed():
    attrs = authz.sanitize_attributes(
        PLAYER, _player(hub_picture_id="/api/media_player_proxy/x")["attributes"]
    )
    assert "hub_picture_id" not in attrs


def test_a_remote_picture_is_marked_too():
    """A remotely reachable artwork: entity_picture is the provider's link,
    entity_picture_local the hub-side proxy link with the token."""
    attrs = authz.sanitize_attributes(
        PLAYER,
        _player(
            "https://images.example.test/cover/123.jpg",
            entity_picture_local=_art("secret"),
            media_image_remotely_accessible=True,
        )["attributes"],
    )
    assert MARKER.match(attrs["hub_picture_id"])
    assert "entity_picture_local" not in attrs
    assert attrs["media_image_remotely_accessible"] is True
    assert "example.test" not in _flat(attrs) and "secret" not in _flat(attrs)


def test_no_link_or_token_survives_in_any_attribute():
    """What was checked: entity_picture, entity_picture_local, media_image_url,
    media_content_id (a stream or thumbnail link), camera stream and still
    links that integrations add, links inside lists and objects."""
    player = authz.sanitize_attributes(
        PLAYER,
        _player(
            _art("secret"),
            entity_picture_local=_art("secret"),
            media_image_url="http://192.0.2.5/art.jpg",
            media_content_id="http://192.0.2.5:8200/MediaItems/22.mp3",
            media_content_type="music",
            source_list=["TV", "Radio"],
            media_playlist={"thumbnail": "/api/media_source/x?authSig=abc"},
            queue=[{"title": "x", "image": "https://cdn.example.test/a.jpg"}],
            app_name="Music",
        )["attributes"],
    )
    assert set(player) == {
        "friendly_name",
        "media_title",
        "media_content_type",
        "source_list",
        "app_name",
        "hub_picture_id",
    }
    camera = authz.sanitize_attributes(
        CAMERA,
        {
            "access_token": "secret",
            "entity_picture": f"/api/camera_proxy/{CAMERA}?token=secret",
            "stream_source": "rtsp://user:pw@192.0.2.9/live",
            "still_image_url": "http://192.0.2.9/snap.jpg",
            "frontend_stream_type": "hls",
            "brand": "Example",
            "motion_detection": False,
        },
    )
    assert set(camera) == {
        "frontend_stream_type",
        "brand",
        "motion_detection",
        "hub_picture_id",
    }
    for attrs in (player, camera):
        text = _flat(attrs)
        assert "secret" not in text and "token" not in text.lower()
        assert "/api/" not in text and "://" not in text
    # A plain media id stays.
    plain = authz.sanitize_attributes(
        PLAYER, _player(media_content_id="spotify:track:abc")["attributes"]
    )
    assert plain["media_content_id"] == "spotify:track:abc"


def test_other_domains_keep_their_picture_rules():
    person = authz.sanitize_attributes(
        "person.p", {"entity_picture": "/api/image/serve/abc/512x512"}
    )
    assert person == {"entity_picture": "/api/image/serve/abc/512x512"}
    image = authz.sanitize_attributes(
        "image.door",
        {
            "entity_picture": "/api/image_proxy/image.door?token=t",
            "entity_picture_local": "/api/image_proxy/image.door?token=t",
        },
    )
    assert image == {}
    assert "hub_picture_id" not in image


def _contract_validate():
    """The contract's tools/validate.py (semantic checks), None without a checkout."""
    import importlib.util

    root = os.environ.get("CONTRACTS_DIR")
    path = Path(root or "") / "tools/validate.py"
    if not root or not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("contract_validate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _schema_validator(rel: str):
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    base = Path(os.environ["CONTRACTS_DIR"]) / "schemas"
    path = base / rel

    def retrieve(uri):
        target = (path.parent / uri).resolve()
        if not target.exists():
            target = base / "common" / Path(uri).name
        return referencing.Resource.from_contents(json.loads(target.read_text()))

    return jsonschema.Draft202012Validator(
        json.loads(path.read_text()), registry=referencing.Registry(retrieve=retrieve)
    )


def test_the_state_message_passes_the_contract_checks():
    """Schema and the contract's semantic check of relayed states (marker
    form, no entity_picture of media players and cameras)."""
    validate = _contract_validate()
    if validate is None:
        pytest.skip("contract checkout not found (set CONTRACTS_DIR)")
    rel = "ws/server/entity_state.json"
    schema = _schema_validator(rel)
    camera = {
        "entity_id": CAMERA,
        "state": "idle",
        "attributes": {
            "access_token": "t",
            "entity_picture": f"/api/camera_proxy/{CAMERA}?token=t",
        },
    }
    for entity_id, state in ((PLAYER, _player(_art("t"))), (CAMERA, camera)):
        message = {
            "type": "entity_state",
            "entity_id": entity_id,
            "state": authz.sanitize_state(entity_id, state),
        }
        assert MARKER.match(message["state"]["attributes"]["hub_picture_id"])
        assert "entity_picture" not in message["state"]["attributes"]
        assert list(schema.iter_errors(message)) == []
        assert validate.semantic_checks(rel, message) == []
    # The contract's cases: the checker rejects them, the hub's output is clean.
    fixtures = Path(os.environ["CONTRACTS_DIR"]) / "fixtures/ws/server/entity_state"
    for name in (
        "invalid-semantic-picture-id-url.json",
        "invalid-semantic-entity-picture-relayed.json",
    ):
        bad = json.loads((fixtures / name).read_text())
        assert validate.semantic_checks(rel, bad) != [], name
        relayed = {
            "type": "entity_state",
            "entity_id": bad["entity_id"],
            "state": authz.sanitize_state(bad["entity_id"], bad["state"]),
        }
        assert validate.semantic_checks(rel, relayed) == [], name
        assert "entity_picture" not in relayed["state"]["attributes"]
    good = json.loads((fixtures / "media-player-picture.json").read_text())
    assert validate.semantic_checks(rel, good) == []
    # Other attributes stay an open object.
    assert relayed["state"]["attributes"]["media_title"] == "Abendlied"


# ── every path a state leaves the hub ───────────────────────────────────────


async def test_get_area_sends_the_marker_and_no_link(db):
    area = await _area(db, "Living", [PLAYER, CAMERA], tags=["gast"])
    guest = await _guest(db)
    ha = FakeHA(
        {
            PLAYER: _player(_art("secret")),
            CAMERA: {
                "entity_id": CAMERA,
                "state": "idle",
                "attributes": {
                    "access_token": "secret",
                    "entity_picture": f"/api/camera_proxy/{CAMERA}?token=secret",
                },
            },
        }
    )
    sock = await _send(db, ha, guest, {"type": "get_area", "area_id": area})
    detail = sock.last()["data"]
    states = {
        e["entity_id"]: e["state"]["attributes"]
        for s in detail["sections"]
        for e in s["entities"]
    }
    assert MARKER.match(states[PLAYER]["hub_picture_id"])
    assert MARKER.match(states[CAMERA]["hub_picture_id"])
    assert "secret" not in _flat(detail) and "_proxy" not in _flat(detail)
    for name in (PLAYER, CAMERA):
        assert "entity_picture" not in states[name]
    validate = _contract_validate()
    if validate is not None:
        for sec in detail["sections"]:
            for e in sec["entities"]:
                assert validate.semantic_checks("common/area-entity.json", e) == []


async def _subscribed(db, ha, entities):
    await _area(db, "Living", entities, tags=["gast"])
    guest = await _guest(db)
    sock = _WS(db, ha, guest)
    sent = []

    async def _sink(message):
        sent.append(message)

    await state_relay.subscribe(sock, db, ha, guest, _sink, start=False)
    return sock, sent


async def test_snapshot_sends_the_marker_and_no_link(db):
    ha = FakeHA({PLAYER: _player(_art("secret"))})
    sock, sent = await _subscribed(db, ha, [PLAYER])
    assert await state_relay.send_snapshot(sock, ha) == 1
    attrs = sent[-1]["state"]["attributes"]
    assert MARKER.match(attrs["hub_picture_id"])
    assert "secret" not in _flat(sent)
    # The token renews right after the snapshot: nothing to relay.
    assert await state_relay.dispatch_event(PLAYER, _player(_art("renewed"))) == 0
    state_relay.unsubscribe(sock)


async def test_a_token_renewal_is_not_relayed_a_new_artwork_is(db):
    ha = FakeHA({})
    sock, sent = await _subscribed(db, ha, [PLAYER])
    assert await state_relay.dispatch_event(PLAYER, _player(_art("t1"))) == 1
    first = sent[-1]["state"]["attributes"]["hub_picture_id"]
    # Only the token (and last_updated) changed.
    renewed = _player(_art("t2"))
    renewed["last_updated"] = "2026-11-25T10:05:00+00:00"
    assert await state_relay.dispatch_event(PLAYER, renewed) == 0
    # A new artwork.
    assert await state_relay.dispatch_event(PLAYER, _player(_art("t3", "c2"))) == 1
    second = sent[-1]["state"]["attributes"]["hub_picture_id"]
    assert second != first
    # Other changes still go out; the picture going away removes the marker.
    assert (
        await state_relay.dispatch_event(
            PLAYER, _player(_art("t4", "c2"), media_title="Next")
        )
        == 1
    )
    assert await state_relay.dispatch_event(PLAYER, _player(None)) == 1
    assert "hub_picture_id" not in sent[-1]["state"]["attributes"]
    assert "t1" not in _flat(sent) and "token" not in _flat(sent)
    state_relay.unsubscribe(sock)


async def test_a_camera_token_renewal_is_not_relayed(db):
    def _cam(token, state="idle"):
        return {
            "entity_id": CAMERA,
            "state": state,
            "attributes": {
                "access_token": token,
                "entity_picture": f"/api/camera_proxy/{CAMERA}?token={token}",
            },
        }

    sock, sent = await _subscribed(db, FakeHA({}), [CAMERA])
    assert await state_relay.dispatch_event(CAMERA, _cam("a")) == 1
    assert await state_relay.dispatch_event(CAMERA, _cam("b")) == 0
    assert await state_relay.dispatch_event(CAMERA, _cam("c", "recording")) == 1
    assert len(sent) == 2
    state_relay.unsubscribe(sock)


async def test_a_removal_is_always_relayed_and_a_new_state_after_it(db):
    sock, sent = await _subscribed(db, FakeHA({}), [PLAYER])
    assert await state_relay.dispatch_event(PLAYER, _player(_art("t1"))) == 1
    assert await state_relay.dispatch_event(PLAYER, None) == 1
    assert await state_relay.dispatch_event(PLAYER, None) == 1
    assert await state_relay.dispatch_event(PLAYER, _player(_art("t2"))) == 1
    assert sent[1]["state"] is None
    state_relay.unsubscribe(sock)
