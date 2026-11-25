"""Static serving: JSON 404 for unknown /api paths, gzip for immutable assets.

- An unknown GET below /api answers a JSON 404, not the SPA fallback's
  index.html with 200, so the admin UI never parses HTML as JSON.
- Immutable JS/CSS go out gzip-compressed on the direct ports, but not under
  Ingress (HA's proxy compresses itself) and not to clients without gzip.
"""

import asyncio
import gzip
import json
import os

import pytest
from starlette.requests import Request

import main
from services import static_compress


def _request(path: str, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": raw,
        "client": ("192.0.2.1", 1),
        "server": ("test", 80),
    }
    return Request(scope)


def _largest_immutable_js() -> str:
    root = os.path.join(main.static_dir, "_app", "immutable")
    best, size = None, -1
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".js"):
                full = os.path.join(dirpath, name)
                if os.path.getsize(full) > size:
                    best, size = full, os.path.getsize(full)
    if best is None:
        pytest.skip("frontend build not present")
    return os.path.relpath(best, root)


@pytest.mark.parametrize(
    "path",
    ["api/does-not-exist", "api/pairing/clients/x/notification-stacked-style", "api"],
)
def test_unknown_api_get_is_json_404(path):
    resp = asyncio.run(main.spa_fallback(_request("/" + path), path))
    assert resp.status_code == 404
    assert resp.media_type == "application/json"
    assert json.loads(resp.body) == {"detail": "Not Found"}


def test_spa_routes_still_get_html():
    resp = asyncio.run(main.spa_fallback(_request("/areas/abc"), "areas/abc"))
    assert resp.status_code == 200
    assert resp.media_type == "text/html"


def test_immutable_js_is_gzipped_outside_ingress():
    rel = _largest_immutable_js()
    resp = asyncio.run(
        main.serve_immutable(
            _request("/x", {"Accept-Encoding": "gzip, deflate, br"}), rel
        )
    )
    assert resp.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in resp.headers["vary"]
    assert "immutable" in resp.headers["cache-control"]
    with open(os.path.join(main.static_dir, "_app", "immutable", rel), "rb") as fh:
        assert gzip.decompress(resp.body) == fh.read()


def test_immutable_not_gzipped_under_ingress():
    rel = _largest_immutable_js()
    req = _request(
        "/x", {"Accept-Encoding": "gzip", "X-Ingress-Path": "/api/hassio_ingress/abc"}
    )
    resp = asyncio.run(main.serve_immutable(req, rel))
    assert "content-encoding" not in resp.headers


def test_immutable_not_gzipped_without_accept_encoding():
    rel = _largest_immutable_js()
    resp = asyncio.run(main.serve_immutable(_request("/x"), rel))
    assert "content-encoding" not in resp.headers


@pytest.mark.parametrize(
    "header,expected",
    [
        ("gzip", True),
        ("br, gzip;q=0.5", True),
        ("gzip;q=0", False),
        ("identity", False),
        ("*", True),
        ("", False),
    ],
)
def test_accepts_gzip_parsing(header, expected):
    assert (
        static_compress.accepts_gzip(_request("/x", {"Accept-Encoding": header}))
        is expected
    )
