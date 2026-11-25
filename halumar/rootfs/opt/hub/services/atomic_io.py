"""Crash-safe file writes.

A plain ``open(path, "w")`` truncates first and writes later: a crash or power
loss in between would leave an empty or half-written file under the final
name (an empty keyfile would make every stored token unreadable). These helpers
write to a temp file in the same directory, fsync it, ``os.replace`` it into
place (atomic on POSIX) and fsync the directory so the rename itself survives
a power loss. A reader sees either the old or the new content, never a mix.
"""

import os
import shutil
import uuid


def fsync_dir(path: str) -> None:
    """fsync a directory so a rename/create inside it is durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tmp_name(path: str) -> str:
    directory, name = os.path.split(path)
    return os.path.join(directory, f".{name}.{uuid.uuid4().hex[:8]}.tmp")


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` atomically with permissions ``mode``."""
    tmp = _tmp_name(path)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    fsync_dir(os.path.dirname(os.path.abspath(path)))


def atomic_write_text(path: str, text: str, mode: int = 0o644) -> None:
    """Text variant of :func:`atomic_write_bytes` (UTF-8)."""
    atomic_write_bytes(path, text.encode("utf-8"), mode)


def atomic_copy(src: str, dst: str) -> None:
    """Copy ``src`` to ``dst`` atomically (temp copy + fsync + replace)."""
    tmp = _tmp_name(dst)
    try:
        shutil.copyfile(src, tmp)
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    fsync_dir(os.path.dirname(os.path.abspath(dst)))
