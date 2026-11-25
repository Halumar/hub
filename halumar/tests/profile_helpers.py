"""Test helpers of the profile model: paired devices with profiles."""

import hashlib
import uuid

from models.database import Database


async def starter_profile(db, suggestion_id: str) -> str:
    """Id of the starter profile created from ``suggestion_id`` (adult, child,
    guest, wall_panel, apple_tv)."""
    row = await db.fetchone(
        "SELECT id FROM profiles WHERE origin_suggestion = ? ORDER BY created_at LIMIT 1",
        (suggestion_id,),
    )
    assert row, f"no profile from suggestion {suggestion_id}"
    return row["id"]


async def add_client(
    db,
    *,
    client_id: str | None = None,
    name: str = "Test Client",
    secret: str | None = None,
    secret_hash: str | None = None,
    guest: bool = False,
    expires_at: str | None = None,
    platform: str | None = None,
    profile: str | None = None,
    profile_id: str | None = None,
    own_area_id: str | None = None,
    overrides: dict | None = None,
    tags: list[str] | None = None,
    remote: bool | None = None,
    **columns,
) -> str:
    """Insert a paired device of the profile model; returns its id.

    ``profile`` = suggestion id of a starter profile (``adult``, ``child``,
    ``guest``, ``wall_panel``, ``apple_tv``); default: the default profile of
    the device type (guests: the guest default). ``overrides`` = rule ->
    value (permanent device adjustments); ``tags`` = areas as the
    integration's client tags; ``remote`` = adjustment of remote_access.
    Further keyword arguments are written as columns.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    from services.profiles import store as profiles

    # Shorthands of older tests: guest flag, client tags as JSON text, rights.
    if "is_guest" in columns:
        guest = bool(columns.pop("is_guest"))
    if isinstance(tags, str):
        tags = _json.loads(tags)
    if "remote_allowed" in columns:
        remote = bool(columns.pop("remote_allowed"))
    # Former rights (one rule setup_devices).
    invite = bool(columns.pop("can_invite_guests", False))
    add = bool(columns.pop("can_add_devices", False))
    if invite or add:
        kinds = (["guest", "house_sitter"] if invite else []) + (
            ["child", "panel", "tv", "person"] if add else []
        )
        overrides = {
            **(overrides or {}),
            "setup_devices": {"enabled": True, "kinds": kinds},
        }
    columns.pop("url_mode", None)

    client_id = client_id or str(uuid.uuid4())
    if secret_hash is None:
        secret_hash = hashlib.sha256(
            (secret or "secret-" + client_id).encode()
        ).hexdigest()
    if profile_id is None:
        if profile:
            profile_id = await starter_profile(db, profile)
        else:
            profile_id = await profiles.default_profile_for(db, platform, guest)
    if guest and not expires_at:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    now = Database.now()
    cols = {
        "id": client_id,
        "name": name,
        "secret_hash": secret_hash,
        "is_active": 1,
        "created_at": now,
        "access": "guest" if guest else "member",
        "expires_at": expires_at if guest else None,
        "platform": platform,
        "profile_id": profile_id,
        "own_area_id": own_area_id,
        **columns,
    }
    async with db.transaction():
        await db.execute(
            f"INSERT INTO paired_clients ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            tuple(cols.values()),
        )
        adjust = dict(overrides or {})
        if tags is not None:
            adjust["areas"] = profiles.areas_from_tags(tags, guest=guest)
        if remote is not None:
            adjust["remote_access"] = remote
        for key, value in adjust.items():
            await db.execute(
                "INSERT INTO device_overrides (client_id, rule_key, value_json, "
                "user_choice, expires_at, note, created_by, created_at) "
                "VALUES (?, ?, ?, 0, NULL, NULL, 'test', ?)",
                (client_id, key, _json.dumps(value), now),
            )
    return client_id
