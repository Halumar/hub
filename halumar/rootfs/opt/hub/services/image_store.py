"""Crash-safe image replacement and start-up clean-up.

:func:`replace_image` orders an image replacement safely: write the new file atomically (tmp +
fsync + replace), persist the new path (the caller's DB write, ideally inside
``db.transaction()``), and only then delete the old file. A crash at any
point leaves the database pointing at a complete file; at worst a file is
left over, which :func:`cleanup_orphan_images` removes on the next start.

Used by ``api/areas.py`` (area images) and ``api/pairing/settings.py`` (home
image).
"""

import asyncio
import logging
import os
import re
from typing import Awaitable, Callable

from services.atomic_io import atomic_write_bytes

logger = logging.getLogger("hub.images")

# Only files the hub itself names this way are ever removed automatically.
_MANAGED_NAME = re.compile(
    r"^(home_)?[0-9a-f]{12}_(card_image|bg_light|bg_dark)\.(jpg|jpeg|png|webp|gif)$"
)
_TMP_NAME = re.compile(r"^\..+\.[0-9a-f]{8}\.tmp$")

AREA_IMAGE_COLUMNS = ("card_image_path", "bg_light_path", "bg_dark_path")
HOME_IMAGE_SETTINGS = ("home_bg_light_path", "home_bg_dark_path")


def _inside(path: str, image_dir: str) -> bool:
    real_dir = os.path.realpath(image_dir)
    real = os.path.realpath(path)
    return os.path.commonpath([real, real_dir]) == real_dir and real != real_dir


def remove_image(path: str | None, image_dir: str) -> bool:
    """Delete ``path`` if it is a file inside ``image_dir``; never raises."""
    if not path:
        return False
    if not _inside(path, image_dir):
        logger.warning("Refusing to delete image outside %s: %s", image_dir, path)
        return False
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("Could not delete image %s: %s", path, e)
        return False


async def replace_image(
    *,
    image_dir: str,
    filename: str,
    content: bytes,
    old_path: str | None,
    persist: Callable[[str], Awaitable[str | None]],
) -> str:
    """Store ``content`` as ``image_dir/filename``, persist, then drop the old file.

    ``persist(new_path)`` must durably store the new path (commit). If it
    raises, the new file is removed again and the old one stays untouched.
    If it returns a path, that is the file it replaced (read in the same
    transaction) and is removed instead of ``old_path``.
    """
    os.makedirs(image_dir, exist_ok=True)
    new_path = os.path.join(image_dir, filename)
    if not _inside(new_path, image_dir):
        raise ValueError(f"image name escapes {image_dir}: {filename!r}")
    await asyncio.to_thread(atomic_write_bytes, new_path, content, 0o644)
    try:
        replaced = await persist(new_path)
    except BaseException:
        remove_image(new_path, image_dir)
        raise
    if replaced is not None:
        old_path = replaced
    if old_path and os.path.basename(old_path) != filename:
        remove_image(old_path, image_dir)
    return new_path


async def referenced_image_names(db) -> set[str]:
    """Basenames of every image the database points to."""
    names: set[str] = set()
    rows = await db.fetchall(
        f"SELECT {', '.join(AREA_IMAGE_COLUMNS)} FROM areas"  # noqa: S608 - constants
    )
    for row in rows:
        names.update(os.path.basename(v) for v in row.values() if v)
    for key in HOME_IMAGE_SETTINGS:
        value = await db.get_setting(key)
        if value:
            names.add(os.path.basename(value))
    return names


async def cleanup_orphan_images(db, image_dir: str) -> dict:
    """Start-up clean-up: unreferenced add-on images and temp files.

    Only names the hub generates are considered; anything else in the
    directory is left alone. Database references to missing files are
    reported (log), not changed.
    """
    result = {"removed": [], "missing": []}
    if not os.path.isdir(image_dir):
        return result
    referenced = await referenced_image_names(db)
    for name in sorted(os.listdir(image_dir)):
        path = os.path.join(image_dir, name)
        if not os.path.isfile(path):
            continue
        if _TMP_NAME.match(name) or (
            _MANAGED_NAME.match(name) and name not in referenced
        ):
            if remove_image(path, image_dir):
                result["removed"].append(name)
    present = set(os.listdir(image_dir))
    result["missing"] = sorted(n for n in referenced if n not in present)
    if result["removed"]:
        logger.info(
            "Removed %d leftover image file(s): %s",
            len(result["removed"]),
            ", ".join(result["removed"]),
        )
    if result["missing"]:
        logger.warning(
            "Image file(s) referenced by the database are missing: %s",
            ", ".join(result["missing"]),
        )
    return result
