"""Shared test fixtures for Halumar test suite.

Provides:
- In-memory SQLite database (isolated per test)
- FastAPI TestClient with mocked dependencies
- Helper functions for creating test data
"""

import asyncio
import hashlib
import os
import sys
import tempfile
import uuid

import pytest

# Add the app root to sys.path so we can import modules directly
APP_ROOT = os.path.join(os.path.dirname(__file__), "..", "rootfs", "opt", "hub")
sys.path.insert(0, APP_ROOT)

# main.py mounts the image directory at import time; outside the hub
# container there is no /data volume, so point it at a temp dir.
os.environ.setdefault("HUB_IMAGE_DIR", tempfile.mkdtemp(prefix="hub-test-images-"))
# The hub's stable instance id lives in /data as well (services/mdns.py).
os.environ.setdefault(
    "HUB_INSTANCE_ID_PATH",
    os.path.join(tempfile.mkdtemp(prefix="hub-test-instance-"), "instance_id"),
)
# hypothesis keeps caches (unicode tables) in .hypothesis/ of the working
# directory, i.e. in the repo tree; keep them in a temp dir instead (the
# property tests also set database=None).
os.environ.setdefault(
    "HYPOTHESIS_STORAGE_DIRECTORY", tempfile.mkdtemp(prefix="hub-test-hypothesis-")
)


def _contracts_dir() -> str | None:
    """Contract checkout for the conformance tests: CONTRACTS_DIR, else a
    ``contracts`` checkout next to this repository or ``.contracts`` in it
    (what .github/actions/contracts creates)."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = os.environ.get("CONTRACTS_DIR")
    candidates = (
        [Path(env)] if env else [repo.parent / "contracts", repo / ".contracts"]
    )
    for path in candidates:
        if (path / "schemas").is_dir():
            return str(path.resolve())
    return None


# Every contract test reads CONTRACTS_DIR; without a checkout they skip
# locally. In CI a missing checkout fails the run (pytest_configure).
CONTRACTS_DIR = _contracts_dir()
if CONTRACTS_DIR:
    os.environ["CONTRACTS_DIR"] = CONTRACTS_DIR


def pytest_configure(config):
    if os.environ.get("CI") and not CONTRACTS_DIR:
        raise pytest.UsageError(
            "contract checkout not found: CI must provide CONTRACTS_DIR "
            "(.github/actions/contracts), the contract tests would only skip"
        )


from models.database import Database

from .profile_helpers import add_client, starter_profile  # noqa: E402, F401


@pytest.fixture
def direct_admin_on(monkeypatch):
    """Direct admin access (HA login on :8100) switched on; off by default."""
    import api.auth as auth

    monkeypatch.setattr(auth, "_direct_admin_enabled", True)


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db(tmp_path):
    """Create an isolated in-memory database for each test.

    Uses a temp file path so that backup/restore logic can be tested.
    The database is fully initialized with the current schema.
    """
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
async def db_with_area(db):
    """Database with a single test area pre-created.

    Returns (db, area_id, area_name).
    """
    area_id = str(uuid.uuid4())
    area_name = "Test Area"
    now = Database.now()

    await db.execute(
        """INSERT INTO areas (id, name, icon, description, sort_order, is_active, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (area_id, area_name, "mdi:home", "A test area", 0, 1, now, now),
    )

    # Create a default section
    section_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO area_sections (id, area_id, title, sort_order, column_span, columns)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (section_id, area_id, "Default", 0, 1, 2),
    )

    await db.commit()
    return db, area_id, section_id


@pytest.fixture
async def db_with_client(db):
    """Database with a paired client pre-created.

    Returns (db, client_id, client_secret, secret_hash).
    """
    client_id = str(uuid.uuid4())
    client_secret = "test-secret-" + uuid.uuid4().hex[:16]
    secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
    now = Database.now()

    await add_client(
        db, client_id=client_id, name="Test Client", secret=client_secret, last_seen=now
    )
    return db, client_id, client_secret, secret_hash
