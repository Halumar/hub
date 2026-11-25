"""HA URL without internal_url uses the real port/scheme.

Port and ssl come from the Supervisor's GET /core/info (HA on port 80 is
handed to the apps without a port); :8123 is the fallback when the
Supervisor gives no answer.
"""

import httpx
import pytest

from services.ha_client import HAClient
from services.ha_url import detect_internal_url


class _FakeHA:
    def __init__(self, info=None, error=None):
        self._info = info
        self._error = error

    async def get_core_info(self):
        if self._error:
            raise self._error
        return self._info


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"port": 80, "ssl": False}, "http://198.51.100.178"),
        ({"port": 8123, "ssl": False}, "http://198.51.100.178:8123"),
        ({"port": 443, "ssl": True}, "https://198.51.100.178"),
        ({"port": 8443, "ssl": True}, "https://198.51.100.178:8443"),
        ({}, "http://198.51.100.178:8123"),
    ],
)
async def test_url_from_core_info(info, expected):
    assert await detect_internal_url(_FakeHA(info), "198.51.100.178") == expected


async def test_supervisor_unreachable_falls_back_to_8123():
    url = await detect_internal_url(
        _FakeHA(error=RuntimeError("no supervisor")), "1.2.3.4"
    )
    assert url == "http://1.2.3.4:8123"


async def test_get_core_info_unwraps_supervisor_envelope():
    def handler(request):
        assert request.url.path == "/core/info"
        return httpx.Response(
            200, json={"result": "ok", "data": {"port": 80, "ssl": False}}
        )

    client = HAClient("token")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.get_core_info() == {"port": 80, "ssl": False}
    finally:
        await client.close()
