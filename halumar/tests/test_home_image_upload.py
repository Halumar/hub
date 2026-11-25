"""Home background upload goes through services.image_store.replace_image:
the new file is written atomically, the setting is committed, and
only then the replaced file is removed. Delete commits before removing the file.
"""

import asyncio
import io
import os
import sqlite3
import types

import pytest
from starlette.datastructures import Headers, UploadFile

import api.pairing as pairing


@pytest.fixture(autouse=True)
def image_dir(tmp_path, monkeypatch):
    path = tmp_path / "images"
    monkeypatch.setattr(pairing, "IMAGE_DIR", str(path))

    async def _broadcast(*_a, **_k):
        return None

    import api.websocket as ws

    monkeypatch.setattr(ws, "broadcast", _broadcast)
    return str(path)


class _Request:
    def __init__(self, db, content: bytes):
        self.state = types.SimpleNamespace(auth_type="ingress")
        self.app = types.SimpleNamespace(state=types.SimpleNamespace(db=db))
        self._content = content

    async def form(self):
        return {
            "file": UploadFile(
                file=io.BytesIO(self._content),
                filename="bg.png",
                headers=Headers({"content-type": "image/png"}),
            )
        }


async def _upload(db, content: bytes):
    return await pairing.upload_home_image(_Request(db, content), "bg_dark")


async def test_upload_replaces_the_old_file_after_the_commit(
    db, image_dir, monkeypatch
):
    await _upload(db, b"old")
    old = await db.get_setting("home_bg_dark_path")
    seen = {}
    real_commit = db.connection.commit

    async def observing_commit():
        seen["old_exists"] = os.path.exists(old)
        seen["files"] = len(os.listdir(image_dir))
        await real_commit()

    with monkeypatch.context() as m:
        m.setattr(db.connection, "commit", observing_commit)
        await _upload(db, b"new")

    new = await db.get_setting("home_bg_dark_path")
    assert seen == {"old_exists": True, "files": 2}
    assert open(new, "rb").read() == b"new"
    assert os.listdir(image_dir) == [os.path.basename(new)]


async def test_failing_commit_keeps_the_old_image(db, image_dir, monkeypatch):
    await _upload(db, b"old")
    old = await db.get_setting("home_bg_dark_path")

    async def broken_commit():
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as m, pytest.raises(sqlite3.OperationalError):
        m.setattr(db.connection, "commit", broken_commit)
        await _upload(db, b"new")
    assert await db.get_setting("home_bg_dark_path") == old
    assert os.listdir(image_dir) == [os.path.basename(old)]


async def test_parallel_uploads_leave_exactly_the_referenced_file(db, image_dir):
    await asyncio.gather(*(_upload(db, bytes([i]) * 10) for i in range(5)))
    current = await db.get_setting("home_bg_dark_path")
    assert os.listdir(image_dir) == [os.path.basename(current)]


async def test_delete_removes_setting_and_file(db, image_dir):
    await _upload(db, b"img")
    request = _Request(db, b"")
    await pairing.delete_home_image(request, "bg_dark")
    assert await db.get_setting("home_bg_dark_path") is None
    assert os.listdir(image_dir) == []
