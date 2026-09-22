
"""Test setup: an isolated SQLite store and the project root on sys.path.

The store picks its backend from the environment at import time, so
the variables are set here, before any test module imports ``app`` or
``core.storage``.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.pop("DATABASE_URL", None)
os.environ.pop("POSTGRES_URL", None)
os.environ["LEI_DB_PATH"] = str(
    Path(tempfile.mkdtemp(prefix="lei-test-")) / "lei_test.db"
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

