
"""Test setup: an isolated store, src/ on sys.path, a prefix-aware client.

The store picks its backend from the environment at import time, so
the variables are set here, before any test module imports ``app`` or
``core.storage``.

The tests name paths as the app sees them ("/api/jobs"). Deployed,
bp_main answers under URL_PREFIX, so when the environment sets one the
test client sends every such path under it (except the unprefixed
/health probe): the suite passes whichever prefix the build sets.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flask.testing import FlaskClient  # noqa: E402

from app import app as flask_app  # noqa: E402
from config import GlobalConstraints  # noqa: E402

PREFIX = GlobalConstraints.GC_URL_PREFIX


class _PrefixedClient(FlaskClient):
    """A test client that sends app paths under URL_PREFIX."""

    def open(self, *args, **kwargs):
        path = args[0] if args else None
        if (
            isinstance(path, str)
            and path.startswith("/")
            and path != "/health"
            and not path.startswith(PREFIX + "/")
        ):
            args = (PREFIX + path, *args[1:])
        return super().open(*args, **kwargs)


if PREFIX:
    flask_app.test_client_class = _PrefixedClient

