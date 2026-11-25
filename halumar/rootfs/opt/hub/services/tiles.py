"""Tile settings per area entity.

Allowed values per domain come from the contract data file
``data/tile_capabilities.json`` (byte-identical copy of contracts
``data/tile-capabilities.json``, checked by ``scripts/ci/contracts-check.sh``).

* :func:`validate_tile` checks a parsed :class:`models.schemas.TileSettings`
  against the capabilities of the entity's domain and against Home Assistant
  (run target and secondary sensor must exist). Errors are FastAPI-style 422
  details with ``type`` ``tile.<reason>``; Home Assistant not reachable = 503.
* :func:`canonical_tile` turns it into the stored form (sorted JSON without
  null fields, ``None`` when everything is default).
* :func:`tile_output` builds the object delivered to the Admin UI and the apps
  (stored settings plus the hub outputs ``icon_svg`` and ``secondary_state``).

The settings reach every device unchanged (Pro is
enforced in the app); a run target does not have to be visible to the device.
"""

from __future__ import annotations

import json
import logging
from functools import cache
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from data.mdi_paths import get_mdi_path
from models.schemas import TileSettings

logger = logging.getLogger("hub.tiles")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_TILE_BYTES = 2048


@cache
def capabilities() -> dict:
    return json.loads((DATA_DIR / "tile_capabilities.json").read_text("utf-8"))


@cache
def icon_set() -> dict:
    return json.loads((DATA_DIR / "tile_icons.json").read_text("utf-8"))


def domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def capability_row(domain: str) -> dict:
    """Row of the capabilities table for ``domain`` (else the ``_other`` row)."""
    rows = capabilities()["domains"]
    for row in rows:
        if domain in row["domains"]:
            return row
    return next(row for row in rows if "_other" in row["domains"])


def _err(loc: list, reason: str, msg: str) -> dict:
    return {"loc": ["body", "tile", *loc], "msg": msg, "type": f"tile.{reason}"}


def capability_errors(entity_id: str, tile: TileSettings) -> list[dict]:
    """Checks that need no Home Assistant: capabilities of the domain."""
    domain = domain_of(entity_id)
    row = capability_row(domain)
    caps = capabilities()
    errors: list[dict] = []

    if tile.icon is not None:
        if not row["icon"]:
            errors.append(
                _err(["icon"], "field_not_allowed", f"no custom icon for {domain}")
            )
        elif (
            tile.icon.startswith("mdi:")
            and get_mdi_path(tile.icon[4:], fallback=False) is None
        ):
            errors.append(
                _err(["icon"], "icon_unknown", f"unknown MDI icon {tile.icon}")
            )
    if tile.variant is not None and tile.variant not in row["variants"]:
        errors.append(
            _err(
                ["variant"],
                "variant_not_allowed",
                f"variant {tile.variant} not allowed for {domain}",
            )
        )
    if tile.accent is not None and not row["accent"]:
        errors.append(
            _err(
                ["accent"],
                "field_not_allowed",
                f"for {domain} the color shows the state",
            )
        )
    if tile.confirm is not None and not row["confirm"]:
        errors.append(
            _err(
                ["confirm"], "field_not_allowed", f"confirm not available for {domain}"
            )
        )
    sec = tile.secondary
    if sec is not None:
        if sec.source == "attribute" and sec.attribute not in row["attributes"]:
            errors.append(
                _err(
                    ["secondary", "attribute"],
                    "attribute_not_allowed",
                    f"attribute {sec.attribute} not allowed for {domain}",
                )
            )
        if sec.source == "entity" and not row["secondary_entity"]:
            errors.append(
                _err(
                    ["secondary", "source"],
                    "field_not_allowed",
                    f"no sensor value for {domain}",
                )
            )
    tap = tile.tap_action
    if tap is not None:
        allowed = set(caps["always"]["tap_actions"]) | set(row["tap_actions"])
        if tap.action not in allowed:
            errors.append(
                _err(
                    ["tap_action", "action"],
                    "action_not_allowed",
                    f"tap action {tap.action} not allowed for {domain}",
                )
            )
    return errors


async def _exists(ha_client, entity_id: str) -> bool:
    try:
        return bool(await ha_client.get_state(entity_id))
    except ValueError:
        return False
    except Exception as exc:  # noqa: BLE001 - any HA/network error
        logger.warning("tile check: Home Assistant not reachable (%s)", exc)
        raise HTTPException(
            status_code=503,
            detail="ha_unavailable: Home Assistant could not be asked, nothing stored",
        ) from None


async def validate_tile(entity_id: str, tile: TileSettings, ha_client) -> None:
    """Raise 422 (``tile.*``) or 503 (HA not reachable); return on success."""
    errors = capability_errors(entity_id, tile)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    tap = tile.tap_action
    if tap is not None and tap.action == "run":
        if not await _exists(ha_client, tap.target_entity_id):
            errors.append(
                _err(
                    ["tap_action", "target_entity_id"],
                    "target_not_found",
                    f"{tap.target_entity_id} does not exist in Home Assistant",
                )
            )
    sec = tile.secondary
    if sec is not None and sec.source == "entity":
        if not await _exists(ha_client, sec.entity_id):
            errors.append(
                _err(
                    ["secondary", "entity_id"],
                    "entity_not_found",
                    f"{sec.entity_id} does not exist in Home Assistant",
                )
            )
    if canonical_tile(tile) and len(canonical_tile(tile).encode()) > MAX_TILE_BYTES:
        errors.append(
            _err([], "too_large", f"tile settings exceed {MAX_TILE_BYTES} bytes")
        )
    if errors:
        raise HTTPException(status_code=422, detail=errors)


def _drop_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    return value


def canonical_tile(tile: TileSettings | dict | None) -> str | None:
    """Stored form: sorted JSON without null fields; ``None`` = all default."""
    if tile is None:
        return None
    data = tile.model_dump() if isinstance(tile, TileSettings) else dict(tile)
    data = _drop_none(data)
    if not data:
        return None
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_stored(raw: str | None) -> dict:
    """Stored column -> dict (``{}`` for NULL or a broken value)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ignoring unreadable tile settings")
        return {}
    return data if isinstance(data, dict) else {}


def changed_fields(old_raw: str | None, new_raw: str | None) -> list[str]:
    old, new = parse_stored(old_raw), parse_stored(new_raw)
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def _state_view(state: dict | None) -> dict | None:
    if not state:
        return None
    return {
        "state": state.get("state"),
        "attributes": state.get("attributes", {}),
        "last_changed": state.get("last_changed"),
    }


async def tile_output(
    raw: str | None, entity_state: dict | None, ha_client, sanitize=None
) -> dict:
    """Object delivered with the area: stored settings + ``icon_svg`` +
    ``secondary_state`` (``sanitize(entity_id, state)`` for paired clients)."""
    tile = parse_stored(raw)
    icon = tile.get("icon")
    if not icon and entity_state:
        ha_icon = (entity_state.get("attributes") or {}).get("icon")
        icon = ha_icon if isinstance(ha_icon, str) else None
    if isinstance(icon, str) and icon.startswith("mdi:"):
        svg = get_mdi_path(icon[4:])
        if svg:
            tile["icon_svg"] = svg
    sec = tile.get("secondary")
    if isinstance(sec, dict) and sec.get("source") == "entity" and sec.get("entity_id"):
        state = None
        try:
            state = _state_view(await ha_client.get_state(sec["entity_id"]))
        except Exception:  # noqa: BLE001 - output is best effort
            state = None
        if state is not None and sanitize is not None:
            state = sanitize(sec["entity_id"], state)
        tile["secondary_state"] = state
    return tile


def tile_from_import(value: Any, entity_id: str) -> tuple[str | None, str | None]:
    """Import/copy: ``(canonical, error)``. No Home Assistant checks (offline)."""
    if value in (None, "", {}):
        return None, None
    if isinstance(value, str):
        value = parse_stored(value)
    try:
        tile = TileSettings.model_validate(value)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError and friends
        return None, f"{entity_id}: tile settings dropped ({str(exc).splitlines()[0]})"
    errors = capability_errors(entity_id, tile)
    if errors:
        return None, f"{entity_id}: tile settings dropped ({errors[0]['type']})"
    return canonical_tile(tile), None


def summary(tile_raw: str | None) -> str:
    """Field names for the audit log (never values)."""
    return ",".join(sorted(parse_stored(tile_raw))) or "-"
