"""Create a paired device: shared by PIN pairing, onboarding and guest
invitations.

``insert_client`` writes the row (secret stored as SHA-256 only, HA token
pending until the integration delivers it); ``announce_paired``
records the activity, asks the integration for the device's own HA user and
tells admin sessions. The caller sends its answer (``pair_ok`` /
``onboard_ok``) in between, so the device holds its secret before anything
else happens.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid

from models.database import HA_TOKEN_PENDING, Database
from services import client_activity, device_provisioning
from services.protocol import ClientInfo

logger = logging.getLogger("hub.services.client_pairing")


def generate_secret() -> str:
    """64-char hex secret of a paired client."""
    return secrets.token_hex(32)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# Reason of a refused platform (ProfileError code; pair_error platform_not_allowed,
# onboard_error profile_not_allowed).
PLATFORM_NOT_ALLOWED = "profile.platform"


def check_platform(prof: dict, platform: str | None, *, guest: bool) -> None:
    """The platform a device reports at pairing gives it nothing its profile
    does not allow; raises ``ProfileError`` otherwise.

    * A profile of a guest kind or with parental controls only on the
      platforms the catalog lists for its kind (``profile_kinds``).
    * A platform that always holds a key (TV, watch: Hub only resolves to
      Checked there) not with a profile on Hub only, unless the profile's
      kind is meant for that platform (a TV profile on a TV).
    """
    from services import onboarding
    from services.profiles import catalog as cat
    from services.profiles import resolver as res
    from services.profiles.catalog import ProfileError

    kind = prof.get("kind") or "person"
    actual = cat.device_platform(platform)
    if (kind in cat.GUEST_ACCESS_KINDS or prof["child_protection"]) and (
        actual not in cat.kind_platforms(kind)
    ):
        raise ProfileError(
            422,
            PLATFORM_NOT_ALLOWED,
            "this profile is not offered on this device type",
        )
    if actual in res.KEY_PLATFORMS and actual not in cat.kind_platforms(kind):
        # What the profile gives where no key is needed (a phone).
        rules = onboarding.new_device_rules(prof, cat.FALLBACK_PLATFORM, guest=guest)
        if not res.holds_key(rules):
            raise ProfileError(
                422,
                PLATFORM_NOT_ALLOWED,
                "a profile on Hub only cannot be used on this device type",
            )


async def insert_client(
    db: Database,
    *,
    client_name: str,
    info: ClientInfo,
    is_guest: bool = False,
    guest_expires_at: str | None = None,
    profile_id: str | None = None,
    own_area_id: str | None = None,
    areas: dict | None = None,
    owner_label: str | None = None,
    secret: str | None = None,
    set_up_by_client_id: str | None = None,
    set_up_by_name: str | None = None,
    set_up_method: str | None = None,
    overrides: dict | None = None,
    seal_key: str | None = None,
    client_id: str | None = None,
) -> tuple[str, str]:
    """Insert the device; returns ``(client_id, secret)``.

    ``client_id`` / ``secret`` given: the caller sealed them already
    (onboarding seals before the device exists).

    ``profile_id`` None = the default profile of the device type (guests: the
    guest default profile). ``areas`` = a device adjustment of the rule
    ``areas`` (area choice when set up from a paired device, guest tags of the
    integration), dropped when the profile fixes the rule; ``overrides`` =
    further device adjustments (rule key -> value) chosen when a member set
    the device up (panel_start, remote_access), also dropped when the
    profile fixes the rule. ``set_up_by_*`` = the member device that set it
    up (onboarding or guest invitation). ``seal_key`` = the X25519 public key
    (base64url) a watch paired with: its HA access is sealed to it
    (``services/watch_access.py``).
    """
    from services.profiles import rows as profile_rows
    from services.profiles import store as profiles
    from services.profiles.catalog import ProfileError

    client_id = client_id or str(uuid.uuid4())
    client_secret = secret or generate_secret()
    now = Database.now()
    if profile_id is None or not await profiles.profile_row(db, profile_id):
        profile_id = await profiles.default_profile_for(db, info.platform, is_guest)
    prof = await profiles.profile_row(db, profile_id)
    if is_guest and prof["child_protection"]:
        raise ProfileError(
            422,
            "profile.guest_child_protection",
            "a guest cannot have parental controls",
        )
    check_platform(prof, info.platform, guest=is_guest)
    if own_area_id and not await db.fetchone(
        "SELECT id FROM areas WHERE id = ?", (own_area_id,)
    ):
        own_area_id = None
    async with db.transaction():
        await db.execute(
            """INSERT INTO paired_clients
               (id, name, secret_hash, is_active, created_at, last_seen,
                access, expires_at, owner_label, profile_id, own_area_id,
                ha_token_status, ha_token_pending_since,
                protocol_version, platform, app_version, app_build,
                device_model, set_up_by_client_id, set_up_by_name,
                set_up_method, seal_key)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                client_id,
                client_name,
                hash_secret(client_secret),
                now,
                now,
                "guest" if is_guest else "member",
                guest_expires_at if is_guest else None,
                owner_label,
                profile_id,
                own_area_id,
                HA_TOKEN_PENDING,
                now,
                *info.columns().values(),
                set_up_by_client_id,
                set_up_by_name if set_up_by_client_id else None,
                (set_up_method or "onboard") if set_up_by_client_id else None,
                seal_key,
            ),
        )
        wanted = dict(overrides or {})
        if areas is not None:
            wanted["areas"] = areas
        fixed = profiles.body_of(prof)["fixed"]
        created_by = set_up_by_name or "pairing"
        for key, value in wanted.items():
            if key in fixed:
                continue
            await db.execute(
                "INSERT INTO device_overrides (client_id, rule_key, value_json, "
                "user_choice, expires_at, note, created_by, created_at, origin, "
                "created_by_client_id) VALUES (?, ?, ?, 0, NULL, NULL, ?, ?, ?, ?)",
                (
                    client_id,
                    key,
                    profile_rows.dumps(value),
                    created_by,
                    now,
                    "admin" if not set_up_by_client_id else "system",
                    set_up_by_client_id,
                ),
            )
        if wanted:
            await profiles.prune_overrides(db, client_id=client_id)
    return client_id, client_secret


async def announce_paired(
    db: Database,
    ha_client,
    *,
    client_id: str,
    client_name: str,
    info: ClientInfo,
    is_guest: bool = False,
    method: str = "pin",
    confirmed_by: str | None = None,
    invited_by: str | None = None,
    set_up: dict | None = None,
    camera_qr: bool = False,
) -> None:
    """Activity, HA user request and admin broadcast of a new device.

    ``camera_qr``: paired with the code shown on the Home Assistant camera
    (app option pairing_qr_camera); the log entry says so.
    """
    from api import ws as api_ws
    from api.ws import EventAudience

    detail = {
        "platform": info.platform,
        "app_version": info.app_version,
        "method": method,
        "confirmed_by": confirmed_by,
    }
    if invited_by:
        detail["invited_by"] = invited_by
    await client_activity.record(db, client_id, client_activity.PAIRED, detail)
    from services import event_log

    await event_log.device_paired(
        db,
        client_id,
        guest=is_guest,
        method=method,
        actor=confirmed_by,
        invited_by=invited_by,
        camera_qr=camera_qr,
    )
    if set_up:
        await event_log.device_set_up(db, client_id, **set_up)

    # Ask the integration for the device's own HA user and token. Without a
    # connected integration the request stays in provisioning[] of the status
    # snapshot; the app gets the token via client_config_updated later.
    await device_provisioning.request_provision(
        db, client_id, reason=device_provisioning.REASON_PAIR
    )

    await api_ws.broadcast(
        {
            "type": "client_paired",
            "data": {
                "client_id": client_id,
                "client_name": client_name,
                "is_guest": bool(is_guest),
            },
        },
        audience=EventAudience.ADMIN,
    )

    if is_guest:
        try:
            from services.ha_sensors import sync_guests_sensor

            await sync_guests_sensor(db, ha_client)
        except Exception as e:  # noqa: BLE001 - sensor sync is best effort
            logger.warning("Failed to sync guests sensor: %s", e)
