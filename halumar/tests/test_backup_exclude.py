"""Keys stay out of HA backups; a restore without them
starts cleanly (new keyfile, new certificate, tokens reported undecryptable).

The Supervisor filters add-on backups with ``PurePath(full_path).match(p)``
for every ``backup_exclude`` pattern (supervisor/apps/app.py,
``_is_excluded_by_filter``); the same check is applied here.
"""

import os
from pathlib import PurePath

import pytest

import services.crypto as crypto
from services import tls
from services.crypto import decrypt_token_with_status, encrypt_token

CONFIG_YAML = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
DATA = "/mnt/data/supervisor/addons/data/local_halumar"


def _backup_exclude() -> list[str]:
    patterns, inside = [], False
    with open(CONFIG_YAML, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("backup_exclude:"):
                inside = True
                continue
            if inside:
                stripped = line.strip()
                if stripped.startswith("- "):
                    patterns.append(stripped[2:].strip().strip('"'))
                elif stripped and not stripped.startswith("#"):
                    break
    return patterns


def _excluded(relative: str) -> bool:
    path = PurePath(DATA, relative)
    return any(path.match(p) for p in _backup_exclude())


@pytest.mark.parametrize(
    "relative",
    [
        ".hub_keyfile",
        ".hub_keyfile.bak",
        ".hub_keyfile.broken-20261118T101500",
        ".hub_keyfile.tmp",
        "..hub_keyfile.1a2b3c4d.tmp",
        "tls/key.pem",
        "tls/key.pem.tmp",
        "tls/.key.pem.1a2b3c4d.tmp",
    ],
)
def test_keys_excluded(relative):
    assert _excluded(relative), relative


@pytest.mark.parametrize(
    "relative",
    [
        "hub.db",
        "tls/cert.pem",
        "images/abc.png",
        ".instance_id",
        "options.json",
        "backups/hub-ha-backup.db",
        "backups/ha-backup.marker",
    ],
)
def test_data_kept(relative):
    assert not _excluded(relative), relative


class TestRestoreWithoutKeys:
    async def test_lost_keyfile_reports_tokens_undecryptable(
        self, tmp_path, monkeypatch
    ):
        keyfile = tmp_path / ".hub_keyfile"
        monkeypatch.setattr(crypto, "KEYFILE_PATH", str(keyfile))
        crypto.reset_cache()
        try:
            ciphertext = await encrypt_token(None, "ha-llat")
            # restore: neither the keyfile nor its local copy is in the backup
            keyfile.unlink()
            (tmp_path / ".hub_keyfile.bak").unlink()
            crypto.reset_cache()
            assert await decrypt_token_with_status(None, ciphertext) == (
                None,
                "decryption_failed",
            )
            assert keyfile.exists(), "a fresh key is created for new tokens"
            fresh = await encrypt_token(None, "new-llat")
            assert await decrypt_token_with_status(None, fresh) == ("new-llat", None)
        finally:
            crypto.reset_cache()

    def test_lost_tls_key_creates_new_certificate(self, tmp_path):
        tls_dir = str(tmp_path / "tls")
        tls.ensure_cert(tls_dir=tls_dir)
        old = tls.get_fingerprint(tls_dir=tls_dir)
        os.remove(os.path.join(tls_dir, "key.pem"))
        tls._fingerprint_cache.clear()
        cert, key = tls.ensure_cert(tls_dir=tls_dir)
        assert os.path.isfile(key)
        assert tls.get_fingerprint(tls_dir=tls_dir) != old


def test_docs_say_every_restore_needs_new_pairing():
    # The Supervisor replaces the hub's data folder on restore, so the keys
    # are gone on the same system too, not only on another one.
    with open(
        os.path.join(os.path.dirname(CONFIG_YAML), "DOCS.md"), encoding="utf-8"
    ) as fh:
        docs = fh.read()
    assert "also on the same system" in docs
    assert "auch auf demselben System" in docs
    assert "on another system" not in docs
    assert "auf einem anderen System" not in docs


def test_key_action_does_not_send_the_admin_to_a_backup():
    """The key file is excluded from backups, so the recovery advice for a
    damaged or missing key must not suggest restoring one."""
    assert _excluded(".hub_keyfile")
    action = crypto.KEY_ACTION
    assert "it contains the key file" not in action
    assert not action.startswith("Restore")
    assert "reset_encryption_key" in action
    assert "never part of Home Assistant backups" in action
    assert crypto.KeyfileError("x").action == action
