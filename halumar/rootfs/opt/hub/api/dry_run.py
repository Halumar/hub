"""A dry run is never carried out for real.

Writes that support a dry run declare it: a query parameter ``dry_run``
(profile writes, adjustments, assignment, own area) or a field ``dry_run``
of the request body (bulk change, guest extension). Every other write that
receives ``dry_run`` true, as a query parameter or a top-level body field,
is refused with 422 before it runs, so a new endpoint cannot carry out a
preview by accident. The check is a dependency of every REST router
(``main.py``).
"""

from __future__ import annotations

import functools
import inspect
import json
import typing

from fastapi import HTTPException, Request
from pydantic import BaseModel

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FALSE = (None, False, 0, "", "0", "false", "no", "off")


def supports_dry_run(endpoint) -> bool:
    """Whether a route's endpoint takes ``dry_run`` (a parameter of that name,
    or a request model with that field)."""
    return _supports(endpoint)


@functools.lru_cache(maxsize=None)
def _supports(endpoint) -> bool:
    try:
        params = inspect.signature(endpoint).parameters.values()
    except (TypeError, ValueError):
        return False
    try:
        # Annotations may be strings (postponed evaluation).
        hints = typing.get_type_hints(endpoint)
    except Exception:  # noqa: BLE001 - fall back to the raw annotations
        hints = {}
    for p in params:
        if p.name == "dry_run":
            return True
        ann = hints.get(p.name, p.annotation)
        if (
            inspect.isclass(ann)
            and issubclass(ann, BaseModel)
            and "dry_run" in ann.model_fields
        ):
            return True
    return False


def _asked(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE
    return value not in _FALSE


async def reject_unsupported_dry_run(request: Request) -> None:
    """Refuse ``dry_run`` true on a write that has no dry run (422)."""
    if request.method not in WRITE_METHODS:
        return
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    if endpoint is not None and supports_dry_run(endpoint):
        return
    asked = _asked(request.query_params.get("dry_run"))
    if not asked and "json" in (request.headers.get("content-type") or ""):
        try:
            body = json.loads(await request.body() or b"null")
        except (ValueError, UnicodeDecodeError):
            body = None
        asked = isinstance(body, dict) and "dry_run" in body and _asked(body["dry_run"])
    if asked:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "dry_run_not_supported",
                "message": "This request has no dry run; nothing was changed.",
            },
        )
