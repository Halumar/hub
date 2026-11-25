"""Crash-safe files, key file, error state.

- key file: atomic write + backup copy; damaged/empty key -> repaired from the
  copy, otherwise the start stops (never a silent new key)
- images: new file atomically, path committed, only then the old file goes
- damaged data without a way out -> health 503 + instructions, files kept
- duplicate tags are removed in the schema (trigger)
"""

import os
import sqlite3
import stat

import pytest

from .profile_helpers import add_client
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from brand import BRAND_NAME
import services.crypto as crypto
from models.schemas import AreaCreate, AreaUpdate
from services import atomic_io, image_store


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    path = str(tmp_path / ".hub_keyfile")
    monkeypatch.setattr(crypto, "KEYFILE_PATH", path)
    crypto.reset_cache()
    yield path
    crypto.reset_cache()


# ---------------------------------------------------------------- atomic_io


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    p = tmp_path / "f.bin"
    atomic_io.atomic_write_bytes(str(p), b"one", 0o600)
    atomic_io.atomic_write_bytes(str(p), b"two", 0o600)
    assert p.read_bytes() == b"two"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert os.listdir(tmp_path) == ["f.bin"]


def test_atomic_write_failure_keeps_old_content(tmp_path, monkeypatch):
    p = tmp_path / "f.bin"
    p.write_bytes(b"old")

    def boom(*a):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_io.atomic_write_bytes(str(p), b"new")
    assert p.read_bytes() == b"old"
    assert os.listdir(tmp_path) == ["f.bin"]


# ---------------------------------------------------------------- key file


async def _encrypt_once():
    return await crypto.encrypt_token(None, "secret")


async def test_new_key_is_written_with_backup_copy(keyfile):
    await _encrypt_once()
    assert os.path.exists(keyfile) and os.path.exists(keyfile + ".bak")
    assert open(keyfile).read() == open(keyfile + ".bak").read()
    for p in (keyfile, keyfile + ".bak"):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


@pytest.mark.parametrize("damage", ["empty", "truncated", "missing"])
async def test_damaged_key_is_restored_from_copy(keyfile, damage):
    """Tokens stay readable, the original key comes back."""
    cipher = await _encrypt_once()
    original = open(keyfile).read()
    if damage == "missing":
        os.remove(keyfile)
    else:
        with open(keyfile, "w") as fh:
            fh.write("" if damage == "empty" else original[:22])
    crypto.reset_cache()
    notice = crypto.check_keyfile(has_encrypted_tokens=True)
    assert notice and "restored from its backup copy" in notice
    assert open(keyfile).read() == original
    crypto.reset_cache()
    assert await crypto.decrypt_token(None, cipher) == "secret"


async def test_damaged_key_without_copy_stops_and_keeps_file(keyfile):
    await _encrypt_once()
    os.remove(keyfile + ".bak")
    with open(keyfile, "w") as fh:
        fh.write("")
    with pytest.raises(crypto.KeyfileError) as err:
        crypto.check_keyfile(has_encrypted_tokens=True)
    assert "reset_encryption_key" in err.value.action
    assert open(keyfile).read() == "", "damaged key file must not be overwritten"
    # runtime path: never a silent new key either
    crypto.reset_cache()
    with pytest.raises(crypto.KeyfileError):
        await crypto.encrypt_token(None, "x")
    assert open(keyfile).read() == ""


async def test_missing_key_with_tokens_stops(keyfile):
    with pytest.raises(crypto.KeyfileError):
        crypto.check_keyfile(has_encrypted_tokens=True)
    assert not os.path.exists(keyfile)


async def test_keys_left_out_of_restored_ha_backup_get_a_new_key(keyfile):
    """After an HA restore key and copy are missing by design."""
    notice = crypto.check_keyfile(
        has_encrypted_tokens=True, restored_from_ha_backup=True
    )
    assert notice and "Home Assistant backup" in notice
    key = open(keyfile).read()
    Fernet(key.strip().encode())
    assert open(keyfile + ".bak").read() == key


async def test_damaged_key_after_ha_restore_still_stops(keyfile):
    """Only the by-design case (both files missing) creates a new key."""
    with open(keyfile, "w") as fh:
        fh.write("")
    with pytest.raises(crypto.KeyfileError):
        crypto.check_keyfile(has_encrypted_tokens=True, restored_from_ha_backup=True)
    assert open(keyfile).read() == ""


async def test_damaged_key_without_tokens_is_replaced(keyfile):
    with open(keyfile, "w") as fh:
        fh.write("garbage")
    assert crypto.check_keyfile(has_encrypted_tokens=False) is None
    key = open(keyfile).read().strip()
    Fernet(key.encode())
    broken = [p for p in os.listdir(os.path.dirname(keyfile)) if ".broken-" in p]
    assert broken, "damaged file is kept"


async def test_reset_option_creates_new_key_with_notice(keyfile):
    with open(keyfile, "w") as fh:
        fh.write("")
    notice = crypto.check_keyfile(has_encrypted_tokens=True, reset=True)
    assert "reset_encryption_key" in notice
    Fernet(open(keyfile).read().strip().encode())


async def test_valid_key_refreshes_missing_copy(keyfile):
    key = Fernet.generate_key().decode()
    with open(keyfile, "w") as fh:
        fh.write(key + "\n")
    assert crypto.check_keyfile(has_encrypted_tokens=True) is None
    assert open(keyfile + ".bak").read().strip() == key


async def test_fresh_install_without_tokens_creates_nothing_yet(keyfile):
    assert crypto.check_keyfile(has_encrypted_tokens=False) is None
    assert not os.path.exists(keyfile)


async def test_has_encrypted_tokens(db):
    assert not await db.has_encrypted_tokens()
    await add_client(db, client_id="c", name="c", ha_token_encrypted="gAAAA")
    assert await db.has_encrypted_tokens()


# ---------------------------------------------------------------- images


async def test_replace_image_order(tmp_path):
    """New file complete before the path is persisted; old file removed after."""
    images = tmp_path / "images"
    images.mkdir()
    old = images / "0123456789ab_card_image.png"
    old.write_bytes(b"old")
    seen = {}

    async def persist(new_path):
        seen["new_complete"] = open(new_path, "rb").read() == b"new" * 1000
        seen["old_still_there"] = old.exists()

    path = await image_store.replace_image(
        image_dir=str(images),
        filename="ba9876543210_card_image.png",
        content=b"new" * 1000,
        old_path=str(old),
        persist=persist,
    )
    assert seen == {"new_complete": True, "old_still_there": True}
    assert not old.exists()
    assert sorted(os.listdir(images)) == [os.path.basename(path)]


async def test_replace_image_persist_failure_keeps_old(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    old = images / "0123456789ab_card_image.png"
    old.write_bytes(b"old")

    async def persist(new_path):
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        await image_store.replace_image(
            image_dir=str(images),
            filename="ba9876543210_card_image.png",
            content=b"new",
            old_path=str(old),
            persist=persist,
        )
    assert os.listdir(images) == [old.name]
    assert old.read_bytes() == b"old"


async def test_replace_image_rejects_escaping_names(tmp_path):
    with pytest.raises(ValueError):
        await image_store.replace_image(
            image_dir=str(tmp_path),
            filename="../x.png",
            content=b"x",
            old_path=None,
            persist=None,
        )


async def test_cleanup_orphans_only_touches_managed_names(tmp_path, db):
    images = tmp_path / "images"
    images.mkdir()
    for name in (
        "0123456789ab_card_image.png",  # referenced
        "home_aaaaaaaaaaaa_bg_dark.jpg",  # referenced via setting
        "bbbbbbbbbbbb_bg_light.webp",  # orphan
        ".bbbbbbbbbbbb_bg_light.webp.1a2b3c4d.tmp",  # interrupted write
        "user-photo.png",  # not ours
    ):
        (images / name).write_bytes(b"x")
    now = db.now()
    await db.execute(
        "INSERT INTO areas (id, name, card_image_path, bg_dark_path, created_at, "
        "updated_at) VALUES ('a', 'A', ?, ?, ?, ?)",
        (
            "/data/images/0123456789ab_card_image.png",
            "/data/images/cccccccccccc_bg_dark.png",
            now,
            now,
        ),
    )
    await db.set_setting(
        "home_bg_dark_path", "/data/images/home_aaaaaaaaaaaa_bg_dark.jpg"
    )
    result = await image_store.cleanup_orphan_images(db, str(images))
    assert sorted(result["removed"]) == [
        ".bbbbbbbbbbbb_bg_light.webp.1a2b3c4d.tmp",
        "bbbbbbbbbbbb_bg_light.webp",
    ]
    assert result["missing"] == ["cccccccccccc_bg_dark.png"]
    assert sorted(os.listdir(images)) == [
        "0123456789ab_card_image.png",
        "home_aaaaaaaaaaaa_bg_dark.jpg",
        "user-photo.png",
    ]


# ---------------------------------------------------------------- tags


def test_duplicate_tags_are_removed():
    assert AreaCreate(name="x", tags=["a", "b", "a"]).tags == ["a", "b"]
    assert AreaUpdate(tags=["dup", "dup"]).tags == ["dup"]
    assert AreaUpdate().tags is None
    with pytest.raises(ValueError):
        AreaUpdate(tags=["x" * 101])


# ---------------------------------------------------------------- error state


def _corrupt_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE areas (id TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with open(path, "r+b") as fh:
        fh.write(b"\0" * 100)


@pytest.fixture
def app_with_paths(tmp_path, monkeypatch, keyfile):
    import main

    db_path = str(tmp_path / "hub.db")
    monkeypatch.setenv("HUB_DB_PATH", db_path)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(main, "OPTIONS_PATH", str(tmp_path / "options.json"))
    yield main, db_path
    main.app.state.startup_error = None
    main.app.state.db = None


def test_damaged_database_without_snapshot_serves_error_state(app_with_paths, tmp_path):
    """No silent empty database; 503 with instructions; file kept."""
    main, db_path = app_with_paths
    _corrupt_db(db_path)
    before = open(db_path, "rb").read()
    with TestClient(main.app) as client:
        r = client.get("/api/health")
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "error" and body["db"] == "unavailable"
        assert "no usable snapshot" in body["error"]
        assert "reset_corrupt_database" in body["action"]
        r = client.get("/api/areas")
        assert r.status_code == 503 and "action" in r.json()
        r = client.get("/")
        assert r.status_code == 503 and f"{BRAND_NAME} cannot start" in r.text
    assert open(db_path, "rb").read() == before
    assert not [p for p in os.listdir(tmp_path) if ".corrupt-" in p]


def test_lost_key_with_tokens_serves_error_state(app_with_paths, keyfile):
    main, db_path = app_with_paths
    conn = sqlite3.connect(db_path)
    from services.db_migrations import SCHEMA_SQL, latest_version

    conn.executescript(SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {latest_version()}")
    conn.execute(
        "INSERT INTO profiles (id, name, body_json, created_at, updated_at) "
        "VALUES ('p', 'P', '{}', 'now', 'now')"
    )
    conn.execute(
        "INSERT INTO paired_clients (id, name, secret_hash, ha_token_encrypted, "
        "created_at, profile_id) VALUES ('c', 'c', 'h', 'gAAAA', 'now', 'p')"
    )
    conn.commit()
    conn.close()
    with open(keyfile, "w") as fh:
        fh.write("")
    with TestClient(main.app) as client:
        r = client.get("/api/health")
        assert r.status_code == 503
        assert "encryption key" in r.json()["error"]
    assert open(keyfile).read() == ""
