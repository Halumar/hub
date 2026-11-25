"""Deleting a home image only removes files inside IMAGE_DIR (_safe_remove_image).

upload_home_image and delete_home_image delete through _safe_remove_image, which
resolves the real path and never unlinks anything outside IMAGE_DIR.
"""

import os
from types import SimpleNamespace

import api.pairing as pairing


async def _async_noop(*_a, **_k):
    return None


def _admin_request(db):
    return SimpleNamespace(
        state=SimpleNamespace(auth_type="admin"),
        app=SimpleNamespace(state=SimpleNamespace(db=db, ha_client=None)),
    )


class TestSafeRemoveImage:
    def test_inside_path_is_removed(self, tmp_path, monkeypatch):
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        monkeypatch.setattr(pairing, "IMAGE_DIR", str(image_dir))
        inside = image_dir / "bg.png"
        inside.write_text("x")

        pairing._safe_remove_image(str(inside))
        assert not inside.exists()

    def test_outside_path_is_not_removed(self, tmp_path, monkeypatch):
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        monkeypatch.setattr(pairing, "IMAGE_DIR", str(image_dir))
        outside = tmp_path / "secret.txt"
        outside.write_text("keep me")

        pairing._safe_remove_image(str(outside))
        assert outside.exists()  # containment refused the delete

    def test_traversal_escape_is_not_removed(self, tmp_path, monkeypatch):
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        monkeypatch.setattr(pairing, "IMAGE_DIR", str(image_dir))
        outside = tmp_path / "escape.txt"
        outside.write_text("keep me")
        poisoned = os.path.join(str(image_dir), "..", "escape.txt")

        pairing._safe_remove_image(poisoned)
        assert outside.exists()

    def test_none_and_empty_are_noops(self):
        # Must not raise.
        pairing._safe_remove_image(None)
        pairing._safe_remove_image("")


class TestDeleteHomeImageEndpoint:
    async def test_delete_does_not_escape_image_dir(self, tmp_path, monkeypatch, db):
        """A poisoned home_*_path setting can't delete a file outside IMAGE_DIR,
        yet the setting row is still cleared."""
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        monkeypatch.setattr(pairing, "IMAGE_DIR", str(image_dir))
        victim = tmp_path / "victim.txt"
        victim.write_text("must survive")
        await db.set_setting("home_bg_light_path", str(victim))

        await pairing.delete_home_image(_admin_request(db), "bg_light")

        assert victim.exists()  # containment protected the outside file
        assert await db.get_setting("home_bg_light_path") is None  # setting cleared

    async def test_delete_removes_contained_image(self, tmp_path, monkeypatch, db):
        image_dir = tmp_path / "images"
        image_dir.mkdir()
        monkeypatch.setattr(pairing, "IMAGE_DIR", str(image_dir))
        img = image_dir / "home_dark.png"
        img.write_text("x")
        await db.set_setting("home_bg_dark_path", str(img))

        await pairing.delete_home_image(_admin_request(db), "bg_dark")

        assert not img.exists()  # a properly-contained image IS removed
        assert await db.get_setting("home_bg_dark_path") is None
