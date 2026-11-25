"""Camera still images and media artwork on the hub path
(services/hub_images.py), against a fake Home Assistant.

The hub reads the image with its own access, only for an entity of the
message's domain in the device's scope, checks the request first, the scope
next (one answer for every entity the device may not see), then the limits
and the kids rules, and never forwards a URL or a token.
"""

import asyncio
import json
import time

import pytest
from services import hub_control, hub_images

from .test_hub_path import (  # noqa: F401 - fixtures
    JPEG,
    FakeHA,
    _area,
    _clean_state,
    _device,
    _guest,
    _home,
    _send,
)
from models.database import Database


# ── still images ────────────────────────────────────────────────────────────


async def _image(db, ha, client_id, **message):
    message.setdefault("type", "camera_image_get")
    message.setdefault("request_id", "i1")
    return (await _send(db, ha, client_id, message)).last()


async def test_camera_image_is_served_with_the_hubs_access(db):
    await _home(db)
    guest = await _guest(db)
    ha = FakeHA()
    answer = await _image(db, ha, guest, entity_id="camera.door", width=640)
    assert answer["type"] == "camera_image"
    assert answer["content_type"] == "image/jpeg"
    import base64

    assert base64.b64decode(answer["data_b64"]) == JPEG
    assert ha.image_calls == [("camera_proxy", "camera.door", 640)]
    # Best effort: width and a 16:9 height for the camera proxy.
    assert ha.sizes == [(640, 360)]
    # Nothing of Home Assistant in the answer: no URL, no token.
    text = json.dumps(answer)
    assert "token" not in text and "/api/" not in text
    art = await _image(
        db,
        ha,
        guest,
        type="media_image_get",
        request_id="m1",
        entity_id="media_player.tv",
    )
    assert art["type"] == "media_image" and art["entity_id"] == "media_player.tv"
    assert ha.image_calls[-1] == ("media_player_proxy", "media_player.tv", None)


async def test_image_scope_is_checked_before_home_assistant(db):
    await _home(db)
    guest = await _guest(db)
    ha = FakeHA()
    for entity_id in ("camera.garage", "camera.unknown"):
        answer = await _image(db, ha, guest, entity_id=entity_id)
        assert answer["reason"] == "not_visible"
    # A visible entity of another domain.
    wrong = await _image(db, ha, guest, entity_id="light.living")
    assert wrong["reason"] == "not_visible"
    media_as_camera = await _image(
        db, ha, guest, type="media_image_get", entity_id="camera.door"
    )
    assert media_as_camera["reason"] == "not_visible"
    bad = await _image(db, ha, guest, entity_id="camera.door", width=10)
    assert bad["reason"] == "invalid_request"
    # The request's shape comes first, also for a device off the hub path.
    keyed = await _device(db, profile="adult")
    assert (await _image(db, ha, keyed, entity_id="camera.door", width=10))[
        "reason"
    ] == "invalid_request"
    assert (await _image(db, ha, keyed, entity_id="camera.door"))[
        "reason"
    ] == "not_hub_controlled"
    no_id = await _image(db, ha, guest, request_id=None, entity_id="camera.door")
    assert no_id["reason"] == "invalid_request"
    assert ha.image_calls == []


async def test_image_limits_per_entity_and_device(db, monkeypatch):
    await _home(db)
    guest = await _guest(db)
    ha = FakeHA()
    assert (await _image(db, ha, guest, entity_id="camera.door"))[
        "type"
    ] == "camera_image"
    again = await _image(db, ha, guest, entity_id="camera.door")
    assert again["reason"] == "rate_limited" and again["retry_after"] == 1
    # Another entity is not held up by the first one.
    art = await _image(
        db, ha, guest, type="media_image_get", entity_id="media_player.tv"
    )
    assert art["type"] == "media_image"
    # The budget of the device: 20 images per 10 s ...
    hub_control.reset()
    hub_images._device_window[guest] = [[time.monotonic(), 10]] * 20
    over = await _image(db, ha, guest, entity_id="camera.door")
    assert over["reason"] == "rate_limited" and over["retry_after"] >= 1
    # ... and 16 MiB per 10 s.
    hub_control.reset()
    hub_images._device_window[guest] = [
        [time.monotonic(), hub_images.DEVICE_BYTES_PER_WINDOW]
    ]
    over = await _image(db, ha, guest, entity_id="camera.door")
    assert over["reason"] == "rate_limited" and over["retry_after"] >= 1
    hub_control.reset()
    assert (await _image(db, ha, guest, entity_id="camera.door"))["type"] == (
        "camera_image"
    )


async def test_one_open_image_request_per_entity(db):
    await _home(db)
    guest = await _guest(db)
    gate = asyncio.Event()

    class SlowHA(FakeHA):
        async def get_image(self, *a, **k):
            await gate.wait()
            return ("image/png", b"\x89PNG....")

    ha = SlowHA()
    first = asyncio.ensure_future(_image(db, ha, guest, entity_id="camera.door"))
    await asyncio.sleep(0.01)
    hub_control._images.clear()  # past the 1 s spacing, the first one still open
    second = await _image(db, ha, guest, request_id="i2", entity_id="camera.door")
    assert second["reason"] == "rate_limited"
    # The same request id while it is open: invalid_request, the first goes on.
    duplicate = await _image(
        db, ha, guest, type="media_image_get", entity_id="media_player.tv"
    )
    assert duplicate["reason"] == "invalid_request"
    gate.set()
    assert (await first)["content_type"] == "image/png"


async def test_a_device_holds_at_most_its_share_of_open_image_reads(db):
    """Two open reads per device (its share of the hub-wide eight; the
    contract's maximum of four is an upper bound)."""
    cameras = [f"camera.c{i}" for i in range(5)]
    await _area(db, "Kameras", cameras, tags=["gast"])
    guest = await _guest(db)
    gate = asyncio.Event()

    class SlowHA(FakeHA):
        async def get_image(self, *a, **k):
            await gate.wait()
            return ("image/jpeg", JPEG)

    ha = SlowHA()
    running = [
        asyncio.ensure_future(_image(db, ha, guest, request_id=f"o{i}", entity_id=c))
        for i, c in enumerate(cameras[:2])
    ]
    await asyncio.sleep(0.02)
    third = await _image(db, ha, guest, request_id="o3", entity_id=cameras[2])
    assert third["reason"] == "rate_limited" and third["retry_after"] == 1
    gate.set()
    done = await asyncio.gather(*running)
    assert all(a["type"] == "camera_image" for a in done)


async def test_image_failures_and_types(db):
    from services.ha_client import ImageTooLarge

    import httpx

    await _home(db)
    guest = await _guest(db)

    async def one(answer):
        hub_control.reset()
        ha = FakeHA(images={"camera.door": answer})
        return await _image(db, ha, guest, entity_id="camera.door")

    assert (await one(ImageTooLarge()))["reason"] == "ha_error"
    assert (await one(("image/svg+xml", b"<svg/>")))["reason"] == "ha_error"
    assert (await one(("text/html", b"<html>")))["reason"] == "ha_error"
    assert (await one(("image/jpeg", b"")))["reason"] == "ha_error"
    assert (await one(httpx.ConnectError("down")))["reason"] == "ha_unavailable"
    request = httpx.Request(
        "GET", "http://supervisor/core/api/camera_proxy/camera.door"
    )
    status = httpx.HTTPStatusError(
        "500", request=request, response=httpx.Response(500, request=request)
    )
    assert (await one(status))["reason"] == "ha_error"
    # The proxy's answer while Home Assistant is down: not reachable.
    down = httpx.HTTPStatusError(
        "502", request=request, response=httpx.Response(502, request=request)
    )
    assert (await one(down))["reason"] == "ha_unavailable"
    alias = await one(("image/JPG; charset=binary", JPEG))
    assert alias["content_type"] == "image/jpeg"
    assert hub_images.content_type_of("image/webp") == "image/webp"
    assert hub_images.content_type_of(None) is None


async def test_blocked_device_types_apply_to_images_of_children(db):
    """A child's blocked camera answers rule_blocked {blocked}; scope first,
    so an out-of-scope camera is not_visible, never rule_blocked."""
    from services.profiles import catalog
    from services.profiles import rows as profile_rows

    await _home(db)
    body = catalog.materialize("child")
    body["rules"]["kids_blocked_types"] = ["camera"]
    async with db.transaction():
        pid = await profile_rows.insert_profile(
            db, name="Kind Kamera", body=body, now=Database.now(), actor=None
        )
    child = await _device(db, profile_id=pid, tags=["gast"])
    ha = FakeHA()
    blocked = await _image(db, ha, child, entity_id="camera.door")
    assert blocked["type"] == "rule_blocked" and blocked["reason"] == "blocked"
    hidden = await _image(db, ha, child, entity_id="camera.garage")
    assert hidden["reason"] == "not_visible"
    art = await _image(
        db, ha, child, type="media_image_get", entity_id="media_player.tv"
    )
    assert art["type"] == "media_image"
    assert ha.image_calls == [("media_player_proxy", "media_player.tv", None)]


async def test_get_image_streams_with_a_size_limit(monkeypatch):
    """HAClient.get_image stops reading beyond the limit."""
    import httpx

    from services import ha_client as hac

    def handler(request):
        assert request.url.path == "/core/api/camera_proxy/camera.door"
        assert request.url.params.get("width") == "320"
        return httpx.Response(
            200, headers={"content-type": "image/jpeg"}, content=b"x" * 100
        )

    client = hac.HAClient("supervisor-token")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ctype, data = await client.get_image(
        "camera_proxy", "camera.door", max_bytes=200, timeout=5, width=320
    )
    assert ctype == "image/jpeg" and data == b"x" * 100

    def redirect(request):
        return httpx.Response(302, headers={"location": "https://example.test/x.jpg"})

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(redirect))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_image("camera_proxy", "camera.door", max_bytes=200, timeout=5)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(hac.ImageTooLarge):
        await client.get_image(
            "camera_proxy", "camera.door", max_bytes=50, timeout=5, width=320
        )
    with pytest.raises(ValueError):
        await client.get_image("states", "camera.door", max_bytes=50, timeout=5)
    await client.close()


async def test_closed_access_and_devices_off_the_hub_path_get_no_image(db):
    await _home(db)
    closed = await _guest(db, access_switch=0)
    keyed = await _device(db, profile="adult", tags=["gast"])
    watch = await _device(db, platform="watchos", profile="adult")
    ha = FakeHA()
    assert (await _image(db, ha, closed, entity_id="camera.door"))[
        "reason"
    ] == "not_visible"
    for cid in (keyed, watch):
        for message in (
            {"entity_id": "camera.door"},
            {"type": "media_image_get", "entity_id": "media_player.tv"},
        ):
            answer = await _image(db, ha, cid, **message)
            assert answer["reason"] == "not_hub_controlled"
    assert ha.image_calls == []


async def test_image_reads_are_bounded_for_the_whole_hub(db, monkeypatch):
    """All devices together have at most HUB_OPEN_MAX open reads, and one
    read ends after FETCH_TIMEOUT_SECONDS in total (ha_unavailable)."""
    await _home(db)
    guest = await _guest(db)
    gate = asyncio.Event()

    class SlowHA(FakeHA):
        async def get_image(self, *a, **k):
            await gate.wait()
            return ("image/jpeg", JPEG)

    monkeypatch.setattr(hub_images, "HUB_OPEN_MAX", 1)
    other = await _guest(db)
    first = asyncio.ensure_future(_image(db, SlowHA(), other, entity_id="camera.door"))
    await asyncio.sleep(0.02)
    busy = await _image(db, FakeHA(), guest, entity_id="camera.door")
    assert busy["reason"] == "rate_limited"
    gate.set()
    assert (await first)["type"] == "camera_image"
    hub_control.reset()
    monkeypatch.setattr(hub_images, "FETCH_TIMEOUT_SECONDS", 0.05)

    class StuckHA(FakeHA):
        async def get_image(self, *a, **k):
            await asyncio.sleep(5)

    stuck = await _image(db, StuckHA(), guest, entity_id="camera.door")
    assert stuck["reason"] == "ha_unavailable"
    assert hub_images._open == set()
