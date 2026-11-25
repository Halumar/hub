"""The platform a device reports at pairing gives it nothing its profile does
not allow (services/client_pairing.check_platform).

Every pairing path (PIN, QR, guest invitation, set-up from a paired device,
a watch) goes through ``client_pairing.insert_client``. A profile of a guest
kind or with parental controls is offered only on the platforms of its kind;
a TV or a watch (which always hold a key) is refused on a profile on Hub
only unless the profile's kind is meant for it. A guest invitation is
checked again on the platform the invited device reports.
"""

import pytest
from models.database import Database
from services import client_pairing, crypto, guest_invites, onboarding, protocol
from services.profiles import catalog
from services.profiles import rows as profile_rows
from services.profiles import store
from services.profiles.catalog import ProfileError

from .profile_helpers import add_client, starter_profile
from .test_hub_path import FakeHA, _get_client_config

PLATFORMS = (
    "ios",
    "ipados",
    "android",
    "android_tablet",
    "tvos",
    "android_tv",
    "watchos",
    "wearos",
)
KEY_PLATFORMS = ("tvos", "android_tv", "watchos", "wearos")


@pytest.fixture(autouse=True)
def _keyfile(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "KEYFILE_PATH", str(tmp_path / ".hub_keyfile"))
    crypto.reset_cache()
    yield
    crypto.reset_cache()


async def _pair(db, platform, *, guest=False, profile_id=None):
    info = protocol.ClientInfo(
        protocol_version=1, platform=platform, app_version="2026.12.0"
    )
    cid, _secret = await client_pairing.insert_client(
        db,
        client_name="Gerät",
        info=info,
        is_guest=guest,
        guest_expires_at="2099-01-01T00:00:00+00:00" if guest else None,
        profile_id=profile_id,
        secret="s" * 43,
    )
    async with db.transaction():
        await db.execute(
            "UPDATE paired_clients SET ha_token_status = 'active', "
            "ha_token_encrypted = ? WHERE id = ?",
            (await crypto.encrypt_token(db, "device-token"), cid),
        )
    return cid


async def _profile(db, suggestion, **rules):
    body = catalog.materialize(suggestion)
    body["rules"].update(rules)
    kind = catalog.suggestion(suggestion)["kind"]
    async with db.transaction():
        return await profile_rows.insert_profile(
            db,
            name=f"{suggestion} {sorted(rules.items())}",
            body=body,
            now=Database.now(),
            actor=None,
            kind=kind,
        )


async def test_a_guest_paired_as_a_tv_is_refused(db):
    cid = await _pair(db, "ios", guest=True)
    cfg = await _get_client_config(db, cid, FakeHA())
    assert cfg["rules"]["control_path"]["value"] == "hub" and cfg["ha_token"] is None
    for platform in ("tvos", "android_tv", "watchos", "wearos"):
        with pytest.raises(ProfileError) as e:
            await _pair(db, platform, guest=True)
        assert e.value.code == client_pairing.PLATFORM_NOT_ALLOWED


async def test_a_hub_only_person_profile_is_refused_on_a_tv_or_watch(db):
    hub = await _profile(db, "adult", control_path="hub", remote_access=False)
    for platform in KEY_PLATFORMS:
        with pytest.raises(ProfileError):
            await _pair(db, platform, profile_id=hub)
    # With remote access the profile holds a read-only key anyway.
    remote = await _profile(db, "adult", control_path="hub", remote_access=True)
    tv = await _pair(db, "tvos", profile_id=remote)
    assert (await _get_client_config(db, tv, FakeHA()))["ha_token"] == "device-token"
    # A TV profile on a TV, a Checked profile on a watch.
    assert await _pair(db, "tvos", profile_id=await starter_profile(db, "apple_tv"))
    assert await _pair(db, "watchos", profile_id=await starter_profile(db, "adult"))


async def test_every_kind_on_every_platform(db):
    """The matrix of profile kinds (their suggestions) and platforms."""
    cases = []
    for sug in catalog.suggestions()["suggestions"]:
        prof = await store.profile_row(db, await _profile(db, sug["id"]))
        kind = sug["kind"]
        guest = kind in catalog.GUEST_ACCESS_KINDS
        for platform in PLATFORMS:
            try:
                client_pairing.check_platform(prof, platform, guest=guest)
                ok = True
            except ProfileError:
                ok = False
            limited = guest or sug["child_protection"]
            on_hub = sug["rules"].get("control_path") == "hub"
            key_needed = platform in KEY_PLATFORMS and platform not in (
                catalog.kind_platforms(kind)
            )
            want = not (
                (limited and platform not in catalog.kind_platforms(kind))
                or (key_needed and on_hub and not sug["rules"].get("remote_access"))
            )
            cases.append((sug["id"], platform, ok, want))
    wrong = [c for c in cases if c[2] != c[3]]
    assert wrong == []
    # Spot checks of the matrix.
    by = {(s, p): ok for s, p, ok, _w in cases}
    assert by[("guest", "ios")] and not by[("guest", "tvos")]
    assert not by[("child", "watchos")] and by[("child", "ipados")]
    assert by[("apple_tv", "tvos")] and by[("adult", "watchos")]
    assert not by[("house_sitter", "android_tv")]


async def test_a_watch_set_up_on_a_hub_only_profile_is_refused(db):
    hub = await _profile(db, "adult", control_path="hub", remote_access=False)
    info = protocol.ClientInfo(
        protocol_version=1, platform="watchos", app_version="2026.12.0"
    )
    with pytest.raises(ProfileError):
        await client_pairing.insert_client(
            db,
            client_name="Uhr",
            info=info,
            profile_id=hub,
            set_up_method="onboard",
            set_up_by_client_id="phone",
            set_up_by_name="Phone",
            seal_key="k" * 43,
        )


async def test_a_guest_invitation_is_checked_on_the_reported_platform(db):
    """A member on Hub only without a key may invite a guest to a phone, but
    the invitation never gives a key: on a TV it is refused."""
    hub = await _profile(
        db,
        "adult",
        control_path="hub",
        remote_access=False,
        setup_devices={"enabled": True, "kinds": ["guest"]},
    )
    member = await add_client(db, name="Phone", platform="ios", profile_id=hub)
    await add_client(db, name="Gast", guest=True, tags=["*"])
    choice = await guest_invites.choose(db, member, {}, "ios")
    assert choice.profile["kind"] == "guest"
    with pytest.raises(onboarding.SetupError):
        await guest_invites.choose(db, member, {}, "tvos")


async def test_the_platform_stays_fixed_after_pairing(db):
    """The reported platform is stored at pairing; auth with another one is
    refused (invalid_client_info) as before."""
    cid = await _pair(db, "ios", guest=True)
    row = await db.fetchone("SELECT platform FROM paired_clients WHERE id = ?", (cid,))
    assert row["platform"] == "ios"
