"""On-the-fly gzip for the frontend's immutable assets (external access).

FileResponse serves files uncompressed. Under HA Ingress the Supervisor proxy
compresses responses itself; on the direct ports (8099/8100) this module
compresses the large assets, for example the MDI icon chunk.

Assets under ``_app/immutable/`` never change for a given file name, so each
file is compressed once (off the event loop) and kept in memory. Only used for
requests that did NOT come through Ingress and that accept gzip.
"""

import asyncio
import gzip
import mimetypes
import os

from starlette.requests import Request
from starlette.responses import Response

_COMPRESSIBLE = {".js", ".mjs", ".css", ".json", ".svg", ".html", ".map", ".txt"}
_MIN_SIZE = 1024
_LEVEL = 6

# path -> (mtime_ns, size, gzipped bytes)
_cache: dict[str, tuple[int, int, bytes]] = {}


def accepts_gzip(request: Request) -> bool:
    """True when the client accepts gzip and the request is not via Ingress."""
    if request.headers.get("x-ingress-path"):
        return False  # HA's Ingress proxy compresses on its own
    for part in request.headers.get("accept-encoding", "").split(","):
        token, _, params = part.strip().partition(";")
        if token.strip().lower() not in ("gzip", "*"):
            continue
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.strip().partition("=")
            if key.strip() == "q":
                try:
                    q = float(value)
                except ValueError:
                    q = 0.0
        if q > 0:
            return True
    return False


def is_compressible(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _COMPRESSIBLE


async def gzip_response(path: str, headers: dict[str, str]) -> Response | None:
    """Gzipped response for ``path`` or None when not worth compressing."""
    if not is_compressible(path):
        return None
    st = os.stat(path)
    if st.st_size < _MIN_SIZE:
        return None
    cached = _cache.get(path)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        body = cached[2]
    else:

        def _compress() -> bytes:
            with open(path, "rb") as fh:
                return gzip.compress(fh.read(), compresslevel=_LEVEL, mtime=0)

        body = await asyncio.to_thread(_compress)
        _cache[path] = (st.st_mtime_ns, st.st_size, body)
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(
        content=body,
        media_type=media_type,
        headers={**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
    )
