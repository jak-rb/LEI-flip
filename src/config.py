
"""Global configuration, read from environment variables.

Values arrive from the platform (codenow/config/environment-variables and
CodeNOW service bindings). Never hardcode a secret here - this file is
public in the repo; the deployed environment is not.

Env is read at import time, so tests that need different values must set
them before the first `import config`. The search store reads its own
variables (DATABASE_URL, LEI_DB_PATH) in core/storage.py, and the
OpenFIGI client reads OPENFIGI_API_KEY in core/openfigi.py.
"""

import os

try:
    # Local convenience only: load a .env if python-dotenv happens to be
    # installed. The deployed runtime injects real environment variables.
    # try/except is deliberate - a bare import of an unpinned library is
    # pylint E0401 and fails the CodeNOW build gate.
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class GlobalConstraints:
    """Settings shared across the component."""

    # The external route this component is published under, e.g.
    # "/lei-lookup". Empty locally. Every generated URL goes through
    # url_for() (and, in the page script, the root the server renders
    # into <html data-app-root>), so the app works in both cases.
    # Stray whitespace and a trailing slash are dropped ("/" means no
    # prefix); a value without its leading slash still fails loudly at
    # import rather than moving every route somewhere unexpected.
    GC_URL_PREFIX = os.getenv("URL_PREFIX", "").strip().rstrip("/")

    # Sessions: set SECRET_KEY in the deployment environment once
    # anything depends on session state. The random default changes on
    # every restart (and differs per pod), which silently logs users
    # out. The app sets no cookies today.
    GC_SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(32).hex()

