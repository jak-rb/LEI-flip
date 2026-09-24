
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
import threading
from pathlib import Path

import psycopg
import pytest
import requests
import waitress
from pythonjsonlogger.jsonlogger import JsonFormatter

import app as app_entry
from config import GlobalConstraints
from core import storage
from core.models import LookupResult
from main import routes

PREFIX = GlobalConstraints.GC_URL_PREFIX

ROOT = Path(__file__).resolve().parents[1]

#: A prefix to publish the app under whatever the environment sets.
TEST_PREFIX = "/lei-lookup"

#: Every local URL a page links to or loads.
_LOCAL_URL = re.compile(r'(?:href|src)="(/[^"]*)"')


class _FakeGleifClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


def test_prefix_tolerates_whitespace_and_a_trailing_slash():
    """A stray space or slash in URL_PREFIX must not move every route."""
    code = (
        "import sys; sys.path.insert(0, 'src'); import config; "
        "print(repr(config.GlobalConstraints.GC_URL_PREFIX))"
    )
    for value, expected in ((" /lei-lookup/ ", "/lei-lookup"), ("/", "")):
        done = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT,
            env={**os.environ, "URL_PREFIX": value},
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert done.stdout.strip() == repr(expected), done.stderr


def test_health_answers_while_four_searches_run(monkeypatch):
    """Running searches must not starve the liveness probe.

    Each running search keeps a waitress thread busy with /run calls;
    with waitress's default of 4 threads, four of them left /health
    queued until one call ended.
    """
    in_flight = threading.Semaphore(0)
    release = threading.Event()

    def slow_lookup(entity, client):
        in_flight.release()
        release.wait(30)
        return LookupResult(notes="No LEI found in the GLEIF database."), []

    monkeypatch.setattr(routes, "lookup_entity", slow_lookup)
    monkeypatch.setattr(routes, "GleifClient", _FakeGleifClient)
    server = waitress.create_server(
        _client().application, host="127.0.0.1", port=0,
        threads=app_entry.WAITRESS_THREADS,
    )
    threading.Thread(target=server.run, daemon=True).start()
    root = f"http://127.0.0.1:{server.effective_port}"
    runs = []
    try:
        for number in range(4):
            job = requests.post(
                f"{root}{PREFIX}/api/jobs",
                data={"mode": "single", "entity_name": f"Slow {number} a.s."},
                timeout=10,
            ).json()["job_id"]
            run = threading.Thread(
                target=requests.post,
                args=(f"{root}{PREFIX}/api/jobs/{job}/run",),
                kwargs={"timeout": 60},
            )
            run.start()
            runs.append(run)
        for _ in runs:
            assert in_flight.acquire(timeout=10)
        response = requests.get(f"{root}/health", timeout=2)
        assert response.json() == {"status": "UP"}
    finally:
        release.set()
        for run in runs:
            run.join(30)
        server.close()
        server.task_dispatcher.shutdown(timeout=5)


def test_postgres_connect_is_bounded(monkeypatch):
    """An unreachable database must not hold a thread for minutes."""
    seen = {}

    def fake_connect(*args, **kwargs):
        seen.update(kwargs)
        raise psycopg.OperationalError("unreachable")

    monkeypatch.setattr(storage, "DATABASE_URL", "postgresql://db/none")
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    with pytest.raises(psycopg.OperationalError):
        storage.get_search("0" * 32)
    assert 0 < seen["connect_timeout"] <= 10


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
    assert root.headers["Location"] == TEST_PREFIX + "/"
    # The bare prefix serves the page itself: Werkzeug's slash redirect
    # would answer with an absolute http:// URL.
    bare = prefixed.get(TEST_PREFIX)
    assert bare.status_code == 200
    assert "Location" not in bare.headers
    assert prefixed.get("/static/main/styles.css").status_code == 404
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

