
"""Tests of the job runner: lookup failures, GLEIF errors, time limits.

The real GleifClient and lookup pipeline run against a fake HTTP
session and a simulated clock, so no test touches the network or
really waits: a request "takes" time only by moving the fake clock.
"""

import io
import json
import logging
import types
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests

import app as app_module
from core import gleif, openfigi, storage
from core.constants import OPENFIGI_TIMEOUT, REQUEST_TIMEOUT
from core.gleif import DeadlineExceeded, GleifApiError, GleifClient
from core.models import LookupResult

ISIN = "US0378331005"


class _Clock:
    """A simulated time.monotonic() / time.sleep() pair."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _FakeSession:
    """Stands in for requests.Session: each GET goes to ``handler``."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append({"params": params, "timeout": timeout})
        return self.handler(params, timeout)

    def close(self):
        pass


def _response(status=200, body=None, text=None, headers=None):
    """A requests.Response with a JSON (or raw ``text``) body."""
    response = requests.Response()
    response.status_code = status
    if text is None:
        text = json.dumps({"data": []} if body is None else body)
    response._content = text.encode()
    response.headers.update(headers or {})
    response.url = "https://gleif.invalid/api/v1/lei-records"
    return response


def _mentions(params, word):
    """Whether any query-string value of a GLEIF request holds word."""
    return any(word in str(value) for value in params.values())


def _no_openfigi(*args, **kwargs):
    raise requests.ConnectionError("OpenFIGI is offline in tests")


def _openfigi_two_names(*args, **kwargs):
    """OpenFIGI knowing two issuer names (the longest lookups)."""
    return _response(body=[{"data": [
        {"name": "APPLE INC"}, {"name": "APPLE COMPUTER INC"},
    ]}])


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    fake_time = types.SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep,
    )
    monkeypatch.setattr(app_module, "time", fake_time)
    monkeypatch.setattr(gleif, "time", fake_time)
    monkeypatch.setattr(openfigi, "time", fake_time, raising=False)
    return clock


@pytest.fixture
def session(monkeypatch, clock):
    """The fake HTTP session every real GleifClient gets (GLEIF: empty)."""
    fake = _FakeSession(lambda params, timeout: _response())
    monkeypatch.setattr(gleif.requests, "Session", lambda: fake)
    monkeypatch.setattr(openfigi.requests, "post", _no_openfigi)
    return fake


@pytest.fixture
def client(session):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _create_job(client, lines):
    content = "\n".join(lines).encode()
    created = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), "in.csv")},
        content_type="multipart/form-data",
    )
    assert created.status_code == 200, created.get_json()
    return created.get_json()["job_id"]


def _run(client, job_id):
    return client.post(f"/api/jobs/{job_id}/run")


def _notes(job_id):
    results = storage.get_search(job_id)["results"]
    return [row["match"]["notes"] for row in results]


# ---- (a) an unexpected error in one lookup ----

def test_unexpected_error_in_one_lookup_is_stored_and_job_finishes(
    client, monkeypatch, caplog,
):
    monkeypatch.setattr(app_module, "RUN_CHUNK_SIZE", 5)

    def lookup(entity, client_):
        if entity.name == "Gamma a.s.":
            raise KeyError("legalName")
        return LookupResult(notes="No LEI found in the GLEIF database."), []
    monkeypatch.setattr(app_module, "lookup_entity", lookup)
    names = ["Alpha a.s.", "Beta a.s.", "Gamma a.s.", "Delta a.s.",
             "Epsilon a.s."]
    job_id = _create_job(client, [f"{name},,CZ" for name in names])

    with caplog.at_level(logging.ERROR):
        response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"], body["unmatched"]) == (5, True, 5)
    results = storage.get_search(job_id)["results"]
    assert [row["input"]["name"] for row in results] == names
    failed = results[2]["match"]
    assert failed["match_type"] == "NO_MATCH" and failed["lei"] is None
    assert failed["notes"].startswith("Lookup failed")
    assert results[2]["closest"] == []
    # The traceback is logged for whoever reads the function logs.
    assert any(
        record.exc_info and record.exc_info[0] is KeyError
        for record in caplog.records
    )


# ---- (b) a GLEIF reply that is not a JSON object ----

@pytest.mark.parametrize("text", [
    "<html>Service temporarily unavailable</html>",
    "",
    '{"data": [',
    "[]",
    "null",
])
def test_gleif_reply_that_is_not_a_json_object_is_a_gleif_error(
    session, text,
):
    session.handler = lambda params, timeout: _response(text=text)
    with GleifClient() as gleif_client:
        with pytest.raises(GleifApiError):
            gleif_client.search_by_name("Alpha a.s.")


def test_non_json_gleif_reply_mid_chunk_keeps_rows_and_answers_503(
    client, session,
):
    def handler(params, timeout):
        if _mentions(params, "Beta"):
            return _response(text="<html>Temporarily unavailable</html>")
        return _response()
    session.handler = handler
    job_id = _create_job(client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ"])

    response = _run(client, job_id)

    assert response.status_code == 503
    body = response.get_json()
    assert body["error"] == app_module.GLEIF_DOWN_MESSAGE
    assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
    assert (body["searched"], body["done"]) == (1, False)


# ---- (c) odd OpenFIGI replies ----

@pytest.mark.parametrize("body, names", [
    ([{"data": [{"name": None}]}], []),
    ([{"data": None}], []),
    ([{"data": ["x", 1, None]}], []),
    ([{"data": [{"name": 7}, {"ticker": "AAPL"}]}], []),
    ([{"data": "Apple Inc"}], []),
    ([{"warning": "No identifier found."}], []),
    ([None], []),
    ([], []),
    ({"data": [{"name": "Apple Inc"}]}, []),
    (None, []),
    ("Apple Inc", []),
    ([{"data": [
        {"name": " Apple Inc "}, None, {"name": "APPLE INC"},
        {"name": None}, {"name": "Apple Computer"},
    ]}], ["Apple Inc", "Apple Computer"]),
])
def test_openfigi_tolerates_unexpected_reply_shapes(
    monkeypatch, body, names,
):
    monkeypatch.setattr(
        openfigi.requests, "post",
        lambda *args, **kwargs: _response(text=json.dumps(body)),
    )
    assert openfigi.resolve_isin_to_names(ISIN) == names


def test_isin_only_job_finishes_when_openfigi_names_are_null(
    client, monkeypatch,
):
    monkeypatch.setattr(
        openfigi.requests, "post",
        lambda *args, **kwargs: _response(body=[{"data": [{"name": None}]}]),
    )
    job_id = _create_job(client, [f",{ISIN}"])

    response = _run(client, job_id)

    assert response.status_code == 200
    assert response.get_json()["done"] is True
    assert "OpenFIGI did not lead" in _notes(job_id)[0]


# ---- (d) a GLEIF error that repeats for one entity ----

@pytest.mark.parametrize("status", [400, 404, 414])
def test_gleif_refusing_one_query_fails_only_that_entity(
    client, session, status,
):
    def handler(params, timeout):
        if _mentions(params, "Refused"):
            return _response(status, {"errors": [{"status": str(status)}]})
        return _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Refused a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"], body["unmatched"]) == (3, True, 3)
    note = _notes(job_id)[1]
    assert note.startswith("Lookup failed") and "refused" in note
    # A refused query is not retried: repeating it cannot help.
    refused = [c for c in session.calls if _mentions(c["params"], "Refused")]
    assert len(refused) == 1


def test_gleif_server_error_for_one_entity_is_retried_then_given_up(
    client, session,
):
    def handler(params, timeout):
        if _mentions(params, "Broken"):
            return _response(500, {"errors": [{"status": "500"}]})
        return _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Broken a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    # Each call retries the failing request inside it, then reports
    # an outage and keeps the saved progress ...
    for _ in range(app_module.RUN_MAX_ATTEMPTS - 1):
        response = _run(client, job_id)
        assert response.status_code == 503
        body = response.get_json()
        assert body["error"] == app_module.GLEIF_DOWN_MESSAGE
        assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
        assert (body["searched"], body["done"]) == (1, False)
    broken = [c for c in session.calls if _mentions(c["params"], "Broken")]
    assert len(broken) == gleif.MAX_RETRIES * (app_module.RUN_MAX_ATTEMPTS - 1)

    # ... until the entity has failed often enough to be given up.
    response = _run(client, job_id)
    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"]) == (3, True)
    note = _notes(job_id)[1]
    assert note.startswith("Lookup failed")
    query = storage.get_search(job_id)["query"]
    assert query[1]["failed_attempts"] == app_module.RUN_MAX_ATTEMPTS


def test_gleif_server_error_that_clears_on_retry_is_not_a_failure(
    session, clock,
):
    replies = [_response(503), _response(502, text="<html>Bad</html>")]
    record = {"id": "L" * 20, "attributes": {"entity": {
        "legalName": {"name": "Apple Inc."}}}}
    session.handler = lambda params, timeout: (
        replies.pop(0) if replies else _response(body={"data": [record]})
    )
    started = clock.now
    with GleifClient() as gleif_client:
        candidates = gleif_client.lookup_by_isin(ISIN)
    assert [candidate.lei for candidate in candidates] == ["L" * 20]
    assert len(session.calls) == 3
    assert clock.now - started == gleif.INITIAL_BACKOFF * 3  # 1 s + 2 s


def test_real_outage_answers_503_keeps_progress_and_fails_no_entity(
    client, session,
):
    outage = {"on": False}

    def handler(params, timeout):
        if outage["on"]:
            raise requests.ConnectionError("connection refused")
        if _mentions(params, "Beta"):
            outage["on"] = True
            raise requests.ConnectionError("connection refused")
        return _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    for _ in range(app_module.RUN_MAX_ATTEMPTS + 2):
        response = _run(client, job_id)
        assert response.status_code == 503
        body = response.get_json()
        assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
        assert (body["searched"], body["done"]) == (1, False)
    assert "failed_attempts" not in storage.get_search(job_id)["query"][1]

    outage["on"] = False
    session.handler = lambda params, timeout: _response()
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (3, True)
    assert not any(note.startswith("Lookup failed") for note in _notes(
        job_id))


def test_racing_failure_counts_are_never_lost(client, monkeypatch):
    job_id = _create_job(client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ"])
    assert storage.record_failed_attempt(job_id, 0) == 1

    real_fetchone = storage._Connection.fetchone
    raced = []
    rivals = []

    def fetchone_then_rival(self, sql, params=()):
        row = real_fetchone(self, sql, params)
        if not raced:
            raced.append(True)
            # A racing /run call counts the same entity twice between
            # this call's read and its write.
            rivals.append(storage.record_failed_attempt(job_id, 0))
            rivals.append(storage.record_failed_attempt(job_id, 0))
        return row
    monkeypatch.setattr(storage._Connection, "fetchone", fetchone_then_rival)
    # No write lock from the read on, as on Postgres: on SQLite the
    # rivals would queue behind it instead of landing in between.
    monkeypatch.setattr(storage._Connection, "begin_write", lambda self: None)
    # This call lost the race, so its own count is not applied on top
    # of a stale read (that would set the count back to 2).
    assert storage.record_failed_attempt(job_id, 0) is None
    monkeypatch.setattr(storage._Connection, "fetchone", real_fetchone)

    assert rivals == [2, 3]
    query = storage.get_search(job_id)["query"]
    assert query[0]["failed_attempts"] == 3
    assert "failed_attempts" not in query[1]
    assert storage.record_failed_attempt(job_id, 2) is None
    assert storage.record_failed_attempt("0" * 32, 0) is None


# ---- (e) GLEIF's rate limit ----

def test_rate_limit_waits_as_long_as_retry_after_asks(session, clock):
    when = datetime.now(timezone.utc) + timedelta(seconds=20)
    for header, low, high in (("7", 7, 7),
                              (format_datetime(when, usegmt=True), 18, 20)):
        replies = [_response(429, headers={"Retry-After": header})]
        session.handler = lambda params, timeout: (
            replies.pop(0) if replies else _response()
        )
        started = clock.now
        with GleifClient() as gleif_client:
            assert gleif_client.lookup_by_isin(ISIN) == []
        assert low <= clock.now - started <= high


def test_rate_limit_past_the_deadline_answers_throttled_with_progress(
    client, session, clock,
):
    limited = {"on": True}

    def handler(params, timeout):
        if limited["on"] and _mentions(params, "Beta"):
            return _response(429, headers={"Retry-After": "200"})
        return _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    started = clock.now
    response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    assert body["throttled"] is True and body["retry_after"] == 200
    assert (body["searched"], body["done"]) == (1, False)
    assert "error" not in body
    # The call hands the wait to the browser instead of sleeping.
    assert clock.now - started < 1

    limited["on"] = False
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (3, True)
    assert "throttled" not in body


# An absurdly long number of seconds, and a Latin-1 superscript two
# (which str.isdigit() accepts but float() does not).
@pytest.mark.parametrize(
    "header", ["9" * 400, "\xb2"], ids=["overlong", "superscript"],
)
def test_broken_retry_after_is_still_just_a_rate_limit(
    client, session, header,
):
    limited = {"on": True}

    def handler(params, timeout):
        if limited["on"] and _mentions(params, "Beta"):
            return _response(429, headers={"Retry-After": header})
        return _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    assert body["throttled"] is True
    assert isinstance(body["retry_after"], int)
    assert 0 < body["retry_after"] <= gleif.MAX_RETRY_AFTER
    assert (body["searched"], body["done"]) == (1, False)

    limited["on"] = False
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (3, True)
    assert not any(note.startswith("Lookup failed") for note in _notes(
        job_id))


# ---- (f) the deadline of one /run call ----

def _call_bound():
    """Longest one /run call may take: its deadline, plus slack.

    No request outlasts the deadline, as its timeout is cut to fit.
    """
    return app_module.RUN_DEADLINE_SECONDS + 1


def test_one_run_call_ends_far_under_the_function_time_limit():
    # vercel.json lets app.py run for at most 300 s per invocation.
    assert app_module.RUN_TIME_BUDGET_SECONDS < _call_bound() <= 300 / 2


def test_request_timeout_is_short():
    assert REQUEST_TIMEOUT <= 10


def test_one_run_call_ends_in_time_when_every_request_times_out(
    client, session, clock, monkeypatch,
):
    monkeypatch.setattr(app_module, "RUN_CHUNK_SIZE", 5)

    def handler(params, timeout):
        clock.now += timeout
        raise requests.Timeout("read timed out")
    session.handler = handler
    job_id = _create_job(
        client, [f"Firm {index} a.s.,{ISIN},CZ,Praha" for index in range(5)],
    )

    started = clock.now
    response = _run(client, job_id)

    assert clock.now - started <= _call_bound()
    # A real outage: 503 with the saved progress (none yet).
    assert response.status_code == 503
    body = response.get_json()
    assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
    assert (body["searched"], body["done"]) == (0, False)


def test_one_run_call_ends_in_time_when_first_attempts_time_out(
    client, session, clock,
):
    # Every request's first attempt times out and its retry answers:
    # one such lookup used to run past Vercel's 300 s.
    attempts = {"count": 0}
    starts = []

    def handler(params, timeout):
        starts.append((clock.now, timeout))
        attempts["count"] += 1
        if attempts["count"] % 2:
            clock.now += timeout
            raise requests.Timeout("read timed out")
        clock.now += 0.5
        return _response()
    session.handler = handler
    job_id = _create_job(client, [
        f"Nonexistent Holding (Europe) a.s.,{ISIN},Czech Republic,Praha",
        f"Another Missing Firm s.r.o.,{ISIN},Czech Republic,Brno",
    ])

    started = clock.now
    response = _run(client, job_id)

    assert clock.now - started <= _call_bound()
    deadline = started + app_module.RUN_DEADLINE_SECONDS
    # No request starts after the deadline or may run past it.
    assert all(
        start < deadline and start + timeout <= deadline + 1e-9
        for start, timeout in starts
    )
    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"]) == (0, False)


def _answer_after(clock, seconds):
    """A GLEIF handler whose every reply takes ``seconds`` to come."""
    def handler(params, timeout):
        if seconds > timeout:
            clock.now += timeout
            raise requests.Timeout("read timed out")
        clock.now += seconds
        return _response()
    return handler


def test_lookup_cut_off_by_the_deadline_is_retried_not_failed(
    client, session, clock,
):
    # Alpha's requests take 5 s each, Slow's 9 s: Alpha ends inside the
    # budget, so Slow starts, but it cannot finish before the deadline
    # and the call returns Alpha alone.
    slow = {"on": True}
    alpha, late = _answer_after(clock, 5), _answer_after(clock, 9)

    def handler(params, timeout):
        if not slow["on"]:
            clock.now += 0.1
            return _response()
        if _mentions(params, "Alpha"):
            return alpha(params, timeout)
        return late(params, timeout)
    session.handler = handler
    job_id = _create_job(client, ["Alpha a.s.,,CZ", f"Slow a.s.,{ISIN},CZ"])

    started = clock.now
    response = _run(client, job_id)

    assert clock.now - started <= _call_bound()
    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"]) == (1, False)

    slow["on"] = False
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (2, True)
    assert "failed_attempts" not in storage.get_search(job_id)["query"][1]
    assert not _notes(job_id)[1].startswith("Lookup failed")


def test_lookup_that_never_fits_in_a_call_is_given_up(
    client, session, clock, monkeypatch,
):
    # Every request takes 9 s and the lookup makes 20 of them: it needs
    # longer than a whole call, so repeating it can never help.
    monkeypatch.setattr(openfigi.requests, "post", _openfigi_two_names)
    session.handler = _answer_after(clock, 9)
    job_id = _create_job(client, [f"Alpha a.s.,{ISIN},CZ,Praha"])

    for _ in range(app_module.RUN_MAX_ATTEMPTS - 1):
        started = clock.now
        body = _run(client, job_id).get_json()
        assert clock.now - started <= _call_bound()
        assert (body["searched"], body["done"]) == (0, False)
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (1, True)
    assert _notes(job_id)[0] == app_module.GLEIF_TOO_SLOW_NOTE


def _flaky(clock):
    """A GLEIF whose every 4th request hangs until its timeout."""
    requests_seen = {"count": 0}

    def handler(params, timeout):
        requests_seen["count"] += 1
        if requests_seen["count"] % 4 == 0:
            clock.now += timeout
            raise requests.Timeout("read timed out")
        clock.now += 0.5
        return _response()
    return handler


@pytest.mark.parametrize("gleif_kind", ["slow", "flaky"])
def test_gleif_that_is_slow_or_flaky_but_answers_fails_no_entity(
    client, session, clock, monkeypatch, gleif_kind,
):
    # Each lookup makes 20 requests. Answered in 2.5 s each, or with a
    # quarter of them hanging for the whole timeout first, it outlasts
    # the budget but fits well within one call's deadline.
    monkeypatch.setattr(openfigi.requests, "post", _openfigi_two_names)
    session.handler = (
        _answer_after(clock, 2.5) if gleif_kind == "slow" else _flaky(clock)
    )
    lines = [f"Firm {index} a.s.,{ISIN},CZ,Praha" for index in range(3)]
    job_id = _create_job(client, lines)

    calls = 0
    body = {"done": False}
    while not body["done"] and calls < 10:
        started = clock.now
        response = _run(client, job_id)
        calls += 1
        assert clock.now - started <= _call_bound()
        assert response.status_code == 200
        body = response.get_json()

    assert body["done"] is True
    assert not any(note.startswith("Lookup failed") for note in _notes(
        job_id))
    # A call starts no lookup once its budget is spent, so progress
    # comes back one lookup at a time here, none of it cut off.
    assert calls == len(lines)


def test_openfigi_request_stops_at_the_deadline(monkeypatch, clock):
    timeouts = []

    def post(url, json=None, headers=None, timeout=None):
        timeouts.append(timeout)
        clock.now += timeout
        raise requests.Timeout("read timed out")
    monkeypatch.setattr(openfigi.requests, "post", post)

    deadline = clock.now + 3
    with pytest.raises(DeadlineExceeded):
        openfigi.resolve_isin_to_names(ISIN, deadline=deadline)
    assert timeouts == [3]
    # Once the deadline has passed no request starts at all.
    with pytest.raises(DeadlineExceeded):
        openfigi.resolve_isin_to_names(ISIN, deadline=deadline)
    assert timeouts == [3]
    # With no deadline a timeout stays the usual soft failure.
    assert openfigi.resolve_isin_to_names(ISIN) == []
    assert timeouts[-1] == OPENFIGI_TIMEOUT

