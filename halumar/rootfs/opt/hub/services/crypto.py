"""AES encryption service for sensitive token storage.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) from the
`cryptography` package to encrypt HA Long-Lived Tokens before storing
them in the database.

The encryption key is stored in a separate keyfile (/data/.hub_keyfile)
with 0600 permissions, NOT in the database. This ensures that a leaked or
backed-up database alone does not expose the key needed to decrypt tokens.

On first use the key is auto-generated and written atomically together with
a backup copy (``.hub_keyfile.bak``). A damaged keyfile is restored from
that copy; without one Halumar refuses to start instead of silently creating
a new key that would make every stored token unreadable.

:func:`derived_key` gives other parts of the hub a key of their own for one
fixed purpose (HKDF over the keyfile's key, e.g. the picture marker of
``api.authz``); the keyfile's key itself is used only by Fernet.

Note: the key lives ONLY in the keyfile. Nothing is read from the
`settings` table, so encryption does not need the database for key
resolution.
"""

import base64
import logging
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from brand import BRAND_NAME
from services.atomic_io import atomic_write_text, fsync_dir

logger = logging.getLogger("hub.services.crypto")

# Cache the Fernet instance to avoid repeated file reads
_fernet: Fernet | None = None
# The key's raw bytes (for derived_key) and the keys derived from it.
_key_bytes: bytes | None = None
_derived: dict[bytes, bytes] = {}

# Keyfile path — inside the HA add-on persistent /data volume
KEYFILE_PATH = "/data/.hub_keyfile"

KEY_ACTION = (
    "Turn on the add-on option 'reset_encryption_key' and restart: "
    f"{BRAND_NAME} then creates a new key, the stored device HA tokens become "
    "unreadable and are requested from the integration again. Turn the option "
    "off afterwards. The key file is never part of Home Assistant backups, so "
    "restoring a backup does not bring it back."
)


class KeyfileError(RuntimeError):
    """The key file exists but is unusable (or is gone while tokens exist).

    Never answered with a silently generated key: that would make every
    stored token unreadable for good.
    """

    def __init__(self, reason: str, action: str = KEY_ACTION):
        super().__init__(reason)
        self.reason = reason
        self.action = action


def _backup_keyfile_path() -> str:
    """Second copy of the key next to the keyfile (same permissions)."""
    return KEYFILE_PATH + ".bak"


def _read_key(path: str) -> tuple[str | None, str]:
    """Return ``(key, state)``; state is ok, missing, empty, invalid or unreadable."""
    if not os.path.lexists(path):
        return None, "missing"
    try:
        with open(path, "r") as f:
            key = f.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.error("Failed to read keyfile %s: %s", path, type(e).__name__)
        return None, "unreadable"
    if not key:
        return None, "empty"
    try:
        Fernet(key.encode())
    except (ValueError, TypeError):
        return None, "invalid"
    return key, "ok"


def _write_keyfile(key: str, path: str | None = None) -> None:
    """Write the key atomically (tmp + fsync + replace) with 0600 permissions."""
    path = path or KEYFILE_PATH
    try:
        atomic_write_text(path, key + "\n", 0o600)
    except Exception as e:
        logger.error("Failed to write keyfile %s: %s", path, type(e).__name__)
        raise


def _set_aside(path: str) -> str:
    """Move a damaged key file out of the way (kept for forensics)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dest = f"{path}.broken-{ts}"
    os.replace(path, dest)
    fsync_dir(os.path.dirname(os.path.abspath(path)))
    return dest


def _restore_from_backup(primary_state: str, key: str) -> None:
    if primary_state != "missing":
        moved = _set_aside(KEYFILE_PATH)
        logger.warning("Damaged keyfile (%s) moved to %s", primary_state, moved)
    _write_keyfile(key)
    logger.warning(
        "Encryption key restored from its backup copy %s", _backup_keyfile_path()
    )


def _read_keyfile() -> str | None:
    """Return the key, or None if no keyfile exists yet (fresh install).

    A damaged keyfile is repaired from the backup copy; without a usable
    copy it raises :class:`KeyfileError` and the file is left untouched.
    """
    key, state = _read_key(KEYFILE_PATH)
    if state == "ok":
        return key
    backup, backup_state = _read_key(_backup_keyfile_path())
    if backup_state == "ok":
        _restore_from_backup(state, backup)
        return backup
    if state == "missing":
        return None
    raise KeyfileError(
        f"The {BRAND_NAME} encryption key file {KEYFILE_PATH} is {state} and there "
        "is no usable backup copy."
    )


def check_keyfile(
    has_encrypted_tokens: bool,
    reset: bool = False,
    restored_from_ha_backup: bool = False,
) -> str | None:
    """Start-up check of the key file. Returns a notice or None.

    - valid key: refresh the backup copy if needed
    - damaged/missing key with a valid backup copy: restore it
    - no usable key and no encrypted tokens in the database: nothing to lose,
      a new key is created (damaged file kept as ``.broken-<time>``)
    - key and copy both missing right after restoring an HA backup: expected,
      the keys are never part of HA backups (``backup_exclude``); a
      new key is created and the notice says the stored tokens are lost
    - no usable key but encrypted tokens exist: :class:`KeyfileError`, unless
      ``reset`` (app option ``reset_encryption_key``) is set
    """
    reset_cache()
    key, state = _read_key(KEYFILE_PATH)
    backup_path = _backup_keyfile_path()
    backup, backup_state = _read_key(backup_path)
    if state == "ok":
        if backup != key:
            _write_keyfile(key, backup_path)
        return None
    if backup_state == "ok":
        _restore_from_backup(state, backup)
        return (
            f"The {BRAND_NAME} encryption key file was {state} and has been restored "
            "from its backup copy."
        )
    if state == "missing" and backup_state == "missing" and not has_encrypted_tokens:
        return None  # fresh install: the key is created on first use
    keys_left_out_of_backup = (
        restored_from_ha_backup and state == "missing" and backup_state == "missing"
    )
    if has_encrypted_tokens and not reset and not keys_left_out_of_backup:
        what = "missing" if state == "missing" else state
        reason = (
            f"The {BRAND_NAME} encryption key file {KEYFILE_PATH} is {what} and there "
            "is no usable backup copy, but the database holds encrypted HA "
            f"tokens. A new key would make them unreadable, so {BRAND_NAME} does not "
            "create one."
        )
        logger.error("%s %s", reason, KEY_ACTION)
        raise KeyfileError(reason)
    for path, path_state in ((KEYFILE_PATH, state), (backup_path, backup_state)):
        if path_state != "missing":
            logger.warning("Damaged key file moved to %s", _set_aside(path))
    new_key = Fernet.generate_key().decode()
    _write_keyfile(new_key)
    _write_keyfile(new_key, backup_path)
    if has_encrypted_tokens and keys_left_out_of_backup and not reset:
        notice = (
            f"{BRAND_NAME} was restored from a Home Assistant backup. The encryption "
            "key is never part of HA backups, so a new key was created. Stored "
            "HA tokens are unreadable; the devices get new ones from the "
            "integration."
        )
        logger.warning(notice)
        return notice
    if has_encrypted_tokens:
        notice = (
            f"A new {BRAND_NAME} encryption key was created (option "
            "reset_encryption_key). Stored HA tokens are unreadable; the "
            "devices get new ones from the integration. Turn the option off."
        )
        logger.error(notice)
        return notice
    logger.warning("Damaged key file replaced (no encrypted tokens were stored)")
    return None


def _load_fernet() -> Fernet:
    """The cached Fernet instance; reads (or on a fresh install creates) the key.

    Key resolution order:
    1. In-memory cache (fast path)
    2. Keyfile on disk (/data/.hub_keyfile), repaired from its backup
       copy if damaged
    3. Generate new key ONLY if no keyfile exists at all (fresh install);
       a damaged one raises KeyfileError instead of being overwritten
    """
    global _fernet, _key_bytes
    if _fernet is not None:
        return _fernet

    key = _read_keyfile()

    # Generate new key if none exists (fresh install)
    if not key:
        key = Fernet.generate_key().decode()
        _write_keyfile(key)
        _write_keyfile(key, _backup_keyfile_path())
        logger.info("Generated new encryption key at %s", KEYFILE_PATH)

    raw = key.encode() if isinstance(key, str) else key
    _fernet = Fernet(raw)
    _key_bytes = base64.urlsafe_b64decode(raw)
    return _fernet


async def _get_fernet() -> Fernet:
    """Get or create the Fernet encryption instance (see :func:`_load_fernet`).

    Takes no ``db``: the key lives only in the keyfile — there is no DB-stored
    key to migrate or read (see module docstring).
    """
    return _load_fernet()


def derived_key(purpose: bytes) -> bytes:
    """A 32-byte key of this hub for one fixed ``purpose`` label.

    HKDF-SHA-256 over the key file's key with ``purpose`` as the info: the
    key file's key itself is never used outside Fernet, and keys of
    different purposes are independent. Changes when the key file is
    renewed. Raises :class:`KeyfileError` like the token encryption.
    """
    cached = _derived.get(purpose)
    if cached is not None:
        return cached
    _load_fernet()
    assert _key_bytes is not None
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(
        _key_bytes
    )
    _derived[purpose] = key
    return key


async def encrypt_token(db, plaintext: str) -> str:
    """Encrypt a token string. Returns base64-encoded ciphertext.

    ``db`` is accepted for a stable call contract (all callers pass their
    Database) but is unused: the key is resolved from the keyfile only.
    """
    if not plaintext:
        return ""
    f = await _get_fernet()
    encrypted = f.encrypt(plaintext.encode())
    return encrypted.decode()  # Fernet returns bytes, we store as string


async def decrypt_token(db, ciphertext: str) -> str | None:
    """Decrypt a token string. Returns plaintext or None if decryption fails."""
    result, _ = await decrypt_token_with_status(db, ciphertext)
    return result


async def decrypt_token_with_status(
    db, ciphertext: str
) -> tuple[str | None, str | None]:
    """Decrypt a token string with error status.

    Returns (plaintext, error) tuple:
    - (token, None) if successful
    - (None, None) if ciphertext is empty (no token configured)
    - (None, "decryption_failed") if decryption failed (key changed?)
    """
    if not ciphertext:
        return None, None  # No token configured
    try:
        f = await _get_fernet()
        decrypted = f.decrypt(ciphertext.encode())
        return decrypted.decode(), None
    except InvalidToken:
        logger.error("Failed to decrypt token - encryption key may have changed")
        return None, "decryption_failed"
    except Exception as e:
        logger.error("Token decryption error: %s", type(e).__name__)
        return None, "decryption_failed"


def reset_cache():
    """Reset the cached Fernet instance (e.g., for testing)."""
    global _fernet, _key_bytes
    _fernet = None
    _key_bytes = None
    _derived.clear()
