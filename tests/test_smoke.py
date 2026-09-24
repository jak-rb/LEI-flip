
"""Smoke tests - the deployment shape, not the business logic.

These catch the failures that are expensive to find after a CodeNOW
deploy: a probe that stopped answering or waits on the store, a landing
page that 500s, a prefix that isn't wired, or a link or script request
that escapes the prefix.
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pythonjsonlogger.jsonlogger import JsonFormatter

import app as app_entry
from config import GlobalConstraints
from core import storage

PREFIX = GlobalConstraints.GC_URL_PREFIX

ROOT = Path(__file__).resolve().parents[1]

#: A prefix to publish the app under whatever the environment sets.
TEST_PREFIX = "/lei-lookup"

#: Every local URL a page links to or loads.
_LOCAL_URL = re.compile(r'(?:href|src)="(/[^"]*)"')


def _client():
    flask_app = app_entry.create_app(config_override={"TESTING": True})
    return flask_app.test_client()


@pytest.fixture
def prefixed(monkeypatch):
    """A client of the app published under TEST_PREFIX."""
    monkeypatch.setattr(app_entry, "url_prefix", TEST_PREFIX)
    return _client()


def test_health_is_up():
    """The platform probe answers 200 {"status": "UP"}, unprefixed."""
    response = _client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "UP"}


def test_health_does_not_touch_the_store(monkeypatch):
    """A probe that waits on the database keeps the old revision live."""
    def unreachable(*args, **kwargs):
        raise AssertionError("/health opened the search store")

    monkeypatch.setattr(storage, "_Connection", unreachable)
    assert _client().get("/health").status_code == 200


def test_import_does_no_store_io():
    """Importing the app must not connect to the store (no import-time I/O).

    With DATABASE_URL naming a server that refuses connections, an
    import that touched the store would fail before /health answered.
    """
    env = {
        **os.environ,
        "DATABASE_URL": "postgresql://nobody@127.0.0.1:1/none",
        "URL_PREFIX": "",
    }
    code = (
        "import sys; sys.path.insert(0, 'src'); import app; "
        "print(app.app.test_client().get('/health').status_code)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "200"


def test_index_renders():
    response = _client().get((PREFIX or "") + "/")
    assert response.status_code == 200


def test_root_redirects_to_prefix():
    """With a prefix configured, the bare root must not 404."""
    if not PREFIX:
        return
    response = _client().get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(PREFIX + "/")


def test_logs_are_json():
    """codenow/config/log-config.json is applied: the platform parses JSON."""
    handlers = logging.getLogger().handlers
    assert any(isinstance(h.formatter, JsonFormatter) for h in handlers)


def test_trace_headers_are_echoed():
    response = _client().get(
        "/health", headers={"X-B3-TraceId": "abc", "X-B3-SpanId": "def"},
    )
    assert response.headers["X-B3-TraceId"] == "abc"
    assert response.headers["X-B3-SpanId"] == "def"


def test_prefixed_app_answers_only_under_its_prefix(prefixed):
    assert prefixed.get("/health").status_code == 200
    root = prefixed.get("/")
    assert root.status_code == 302
    assert root.headers["Location"].endswith(TEST_PREFIX + "/")
    for path in ("/results", "/admin"):
        assert prefixed.get(path).status_code == 404
        assert prefixed.get(TEST_PREFIX + path).status_code != 404
    assert prefixed.post("/api/jobs").status_code == 404


def test_prefixed_pages_link_and_load_under_the_prefix(prefixed):
    """Every local link, stylesheet, image and script is under the prefix."""
    created = prefixed.post(
        TEST_PREFIX + "/api/jobs",
        data={"mode": "single", "entity_name": "Smoke Test a.s."},
    )
    assert created.status_code == 200
    job_id = created.get_json()["job_id"]

    pages = [TEST_PREFIX + "/", f"{TEST_PREFIX}/results?job={job_id}"]
    seen = set()
    for page in pages:
        response = prefixed.get(page)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert f'data-app-root="{TEST_PREFIX}/"' in html
        for url in _LOCAL_URL.findall(html):
            assert url.startswith(TEST_PREFIX + "/"), (page, url)
            seen.add(url.replace("&amp;", "&"))
    assert any(url.endswith("app.js") for url in seen)
    for url in seen:
        assert prefixed.get(url).status_code == 200, url


def test_page_script_builds_every_request_from_the_root():
    """app.js must not start a URL at "/": it would escape the prefix.

    A path appended to a built URL (``+ "/run"``) is fine.
    """
    script = (ROOT / "src" / "main" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    assert not re.findall(r"""(?<!\+ )["'`]/[A-Za-z]""", script)

