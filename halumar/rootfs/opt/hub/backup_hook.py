"""Supervisor backup hooks (config.yaml ``backup_pre``/``backup_post``).

Runs via ``docker exec`` next to the running server:
``python3 /opt/hub/backup_hook.py pre|post``. See
``services/db_snapshots.py`` for what the snapshot and marker are for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brand import BRAND_NAME
from services.db_snapshots import run_backup_hook  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: backup_hook.py pre|post")
        sys.exit(2)
    try:
        sys.exit(
            run_backup_hook(sys.argv[1], os.environ.get("HUB_DB_PATH", "/data/hub.db"))
        )
    except Exception as e:  # noqa: BLE001 - reported to the Supervisor
        print(f"{BRAND_NAME} backup hook failed: {type(e).__name__}: {e}")
        sys.exit(1)
