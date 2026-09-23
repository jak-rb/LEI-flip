
"""Tests of the job runner telling a GLEIF outage from one bad entity.

A GLEIF error that hits every request (an outage, a block of the app's
IPs, a moved API) must pause the job with a 503 and store nothing,
while an error that hits one entity's query fails only that entity.
Also covers rate limits against the call's deadline, broken
Retry-After dates and stored query records that no longer validate.

As in tests/test_runner.py, the real GleifClient and lookup pipeline
run against a fake HTTP session and a simulated clock: no test touches
the network or really waits.
"""

import io
import json
import types

import pytest
import requests

import app as app_module
from core import gleif, openfigi, storage
from core.gleif import DeadlineExceeded, GleifClient, GleifRateLimited

ISIN = "US0378331005"

# Three overlong numbers in an HTTP date: parsedate_to_datetime raises
# OverflowError for each (a huge year, second and zone offset).
OVERFLOWING_DATES = [
    "1 Jan 99999999999999999999 00:00",
    "1 Jan 2026 0:0:99999999999999",
    "1 Jan 2026 00:00:00 +99999999999999999999",
]


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


def _html(status):
    """A reply with an HTML page, as a CDN or firewall sends one."""
    return _response(status, text="<html><h1>Unavailable</h1></html>")


def _mentions(params, word):
    """Whether any query-string value of a GLEIF request holds word."""
    return any(word in str(value) for value in params.values())


def _is_probe(params):
    """Whether a GLEIF request is the runner's one-record probe."""
    return params.get("page[size]") == "1"


def _no_openfigi(*args, **kwargs):
    raise requests.ConnectionError("OpenFIGI is offline in tests")


def _openfigi_two_names(*args, **kwargs):
    """OpenFIGI knowing two issuer names (the longest lookups)."""
    return _response(body=[{"data": [
        {"name": "APPLE INC"}, {"name": "APPLE COMPUTER INC"},
    ]}])


def _answer_after(clock, seconds):
    """A GLEIF handler whose every reply takes ``seconds`` to come."""
    def handler(params, timeout):
        if seconds > timeout:
            clock.now += timeout
            raise requests.Timeout("read timed out")
        clock.now += seconds
        return _response()
    return handler


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
    """The fake HTTP session of each GleifClient (GLEIF: empty)."""
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


def _attempts(job_id):
    """Each entity's counted failed attempts (0 when none)."""
    query = storage.get_search(job_id)["query"]
    return [record.get("failed_attempts", 0) for record in query]


def _assert_outage_then_recovery(client, session, job_id, searched=0):
    """Calls answer 503 and keep the job; once GLEIF is back it ends."""
    for _ in range(app_module.RUN_MAX_ATTEMPTS + 2):
        response = _run(client, job_id)
        assert response.status_code == 503
        body = response.get_json()
        assert body["error"] == app_module.GLEIF_DOWN_MESSAGE
        assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
        assert (body["searched"], body["done"]) == (searched, False)
    assert set(_attempts(job_id)) == {0}

    session.handler = lambda params, timeout: _response()
    response = _run(client, job_id)
    assert response.status_code == 200
    assert response.get_json()["done"] is True
    assert not any(
        note.startswith("Lookup failed") for note in _notes(job_id)
    )


# ---- (a) a GLEIF error on every request is an outage ----

@pytest.mark.parametrize("status", [401, 403, 404, 405, 407, 410, 451])
def test_gleif_refusing_every_request_answers_503_and_stores_nothing(
    client, session, status,
):
    # A firewall blocking the app's IPs, a proxy, or an API that moved:
    # every query, the probe included, meets the same refusal.
    session.handler = lambda params, timeout: _response(
        status, {"errors": [{"status": str(status)}]},
    )
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ", "Gamma a.s.,,CZ"],
    )
    _assert_outage_then_recovery(client, session, job_id)


@pytest.mark.parametrize("reply", [
    lambda: _html(500), lambda: _html(502), lambda: _html(503),
    lambda: _html(504), lambda: _html(200), lambda: _response(408),
    lambda: _response(425),
], ids=["500", "502", "503", "504", "html-200", "408", "425"])
def test_gleif_failing_every_request_answers_503_and_stores_nothing(
    client, session, reply,
):
    session.handler = lambda params, timeout: reply()
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ", "Gamma a.s.,,CZ"],
    )
    _assert_outage_then_recovery(client, session, job_id)


def test_gleif_outage_mid_chunk_keeps_the_rows_before_it(client, session):
    down = {"on": False}

    def handler(params, timeout):
        if _mentions(params, "Beta"):
            down["on"] = True
        return _html(503) if down["on"] else _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Beta a.s.,,CZ", "Gamma a.s.,,CZ"],
    )
    _assert_outage_then_recovery(client, session, job_id, searched=1)


# ---- (b) a GLEIF error on one entity's query only ----

@pytest.mark.parametrize("reply", [
    lambda: _response(400, {"errors": [{"status": "400"}]}),
    lambda: _html(403),
], ids=["400", "waf-403"])
def test_gleif_refusing_one_query_fails_that_entity_at_once(
    client, session, reply,
):
    def handler(params, timeout):
        return reply() if _mentions(params, "Refused") else _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Refused a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"], body["unmatched"]) == (3, True, 3)
    notes = _notes(job_id)
    assert notes[1] == app_module.GLEIF_REFUSED_NOTE
    assert not notes[0].startswith("Lookup failed")
    assert not notes[2].startswith("Lookup failed")
    # GLEIF was asked once whether it answers at all, then the entity
    # was failed: its refused query is not repeated.
    assert len([c for c in session.calls if _is_probe(c["params"])]) == 1
    refused = [c for c in session.calls if _mentions(c["params"], "Refused")]
    assert len(refused) == 1


@pytest.mark.parametrize("reply", [
    lambda: _html(500), lambda: _response(408), lambda: _html(200),
], ids=["500", "408", "html-200"])
def test_gleif_failing_one_query_fails_that_entity_after_the_attempts(
    client, session, reply,
):
    def handler(params, timeout):
        return reply() if _mentions(params, "Broken") else _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Broken a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    for attempt in range(1, app_module.RUN_MAX_ATTEMPTS):
        response = _run(client, job_id)
        assert response.status_code == 503
        body = response.get_json()
        assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
        assert (body["searched"], body["done"]) == (1, False)
        assert _attempts(job_id) == [0, attempt, 0]

    response = _run(client, job_id)
    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"]) == (3, True)
    assert _notes(job_id)[1] == app_module.GLEIF_ERRORS_NOTE


# ---- (c) the probe itself ----

@pytest.mark.parametrize("reply, answering", [
    (lambda: _response(), True),
    (lambda: _response(body={"data": [], "meta": {}}), True),
    (lambda: _html(200), False),
    (lambda: _response(text="[]"), False),
    (lambda: _html(503), False),
    (lambda: _response(403), False),
    (lambda: _response(404), False),
], ids=["empty", "meta", "html-200", "json-list", "503", "403", "404"])
def test_probe_sends_one_small_search_and_reports_the_answer(
    session, clock, reply, answering,
):
    session.handler = lambda params, timeout: reply()
    started = clock.now
    with GleifClient() as gleif_client:
        assert gleif_client.is_answering() is answering
    # One request, never retried: its answer is all that is asked.
    assert len(session.calls) == 1
    assert clock.now == started
    params = session.calls[0]["params"]
    assert params["page[size]"] == "1"
    assert any(key.startswith("filter[") for key in params)


def test_rate_limited_probe_is_a_rate_limit(session, clock):
    # A 429 says nothing about the failed query, so it is not taken
    # for an outage either: the job pauses as for any rate limit.
    session.handler = lambda params, timeout: _response(
        429, headers={"Retry-After": "5"},
    )
    started = clock.now
    with GleifClient() as gleif_client:
        with pytest.raises(GleifRateLimited) as raised:
            gleif_client.is_answering()
    assert raised.value.retry_after == 5
    assert len(session.calls) == 1
    assert clock.now == started


def test_rate_limited_probe_pauses_the_job_and_counts_nothing(
    client, session,
):
    def handler(params, timeout):
        if _is_probe(params):
            return _response(429, headers={"Retry-After": "7"})
        return _html(503) if _mentions(params, "Broken") else _response()
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", "Broken a.s.,,CZ", "Gamma a.s.,,CZ"],
    )

    for _ in range(app_module.RUN_MAX_ATTEMPTS + 1):
        response = _run(client, job_id)
        assert response.status_code == 200
        body = response.get_json()
        assert (body["throttled"], body["retry_after"]) == (True, 7)
        assert (body["searched"], body["done"]) == (1, False)
    assert _attempts(job_id) == [0, 0, 0]


def test_probe_counts_a_connection_error_as_not_answering(session):
    def refuse(params, timeout):
        raise requests.ConnectionError("connection refused")
    session.handler = refuse
    with GleifClient() as gleif_client:
        assert gleif_client.is_answering() is False
    assert len(session.calls) == 1


def test_probe_stops_at_the_deadline(session, clock):
    def hang(params, timeout):
        clock.now += timeout
        raise requests.Timeout("read timed out")
    session.handler = hang
    with GleifClient() as gleif_client:
        gleif_client.deadline = clock.now + 3
        with pytest.raises(DeadlineExceeded):
            gleif_client.is_answering()
        assert session.calls[-1]["timeout"] == 3
        # Once the deadline has passed no request starts at all.
        with pytest.raises(DeadlineExceeded):
            gleif_client.is_answering()
    assert len(session.calls) == 1


# ---- (d) rate limits and the call's deadline ----

def test_short_rate_limit_wait_does_not_hide_a_too_slow_lookup(
    client, session, clock, monkeypatch,
):
    # Each call's first request is rate-limited for 1 s, then every
    # request takes 9 s: the lookup needs longer than a whole call and
    # hardly any of it went on the rate limit, so it is given up.
    monkeypatch.setattr(openfigi.requests, "post", _openfigi_two_names)
    slow = _answer_after(clock, 9)
    state = {"first": True}

    def handler(params, timeout):
        if state["first"]:
            state["first"] = False
            return _response(429, headers={"Retry-After": "1"})
        return slow(params, timeout)
    session.handler = handler
    job_id = _create_job(client, [f"Alpha a.s.,{ISIN},CZ,Praha"])

    for attempt in range(1, app_module.RUN_MAX_ATTEMPTS):
        state["first"] = True
        response = _run(client, job_id)
        assert response.status_code == 200
        body = response.get_json()
        assert "throttled" not in body
        assert (body["searched"], body["done"]) == (0, False)
        assert _attempts(job_id) == [attempt]

    state["first"] = True
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (1, True)
    assert _notes(job_id) == [app_module.GLEIF_TOO_SLOW_NOTE]


def test_earlier_lookups_rate_limit_does_not_make_a_cut_off_throttled(
    client, session, clock, monkeypatch,
):
    # Alpha waits out a 5 s rate limit and ends quickly; Slow then
    # starts, and its 9 s requests run into the call's deadline.
    monkeypatch.setattr(openfigi.requests, "post", _openfigi_two_names)
    state = {"limited": True}
    slow = _answer_after(clock, 9)

    def handler(params, timeout):
        if _mentions(params, "Alpha"):
            if state["limited"]:
                state["limited"] = False
                return _response(429, headers={"Retry-After": "5"})
            clock.now += 0.1
            return _response()
        return slow(params, timeout)
    session.handler = handler
    job_id = _create_job(
        client, ["Alpha a.s.,,CZ", f"Slow a.s.,{ISIN},CZ,Praha"],
    )

    response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    # A plain cut-off: Slow was not rate-limited, and it did not have
    # the call to itself, so nothing is counted against it.
    assert "throttled" not in body
    assert (body["searched"], body["done"]) == (1, False)
    assert _attempts(job_id) == [0, 0]

    # In the next call Slow comes first, and its cut-off counts.
    body = _run(client, job_id).get_json()
    assert "throttled" not in body
    assert (body["searched"], body["done"]) == (1, False)
    assert _attempts(job_id) == [0, 1]


@pytest.mark.parametrize("cut_off_in", ["gleif", "openfigi"])
def test_lookup_whose_deadline_went_on_a_rate_limit_is_not_too_slow(
    client, session, clock, monkeypatch, cut_off_in,
):
    # Each call's first GLEIF request is rate-limited for 110 s of the
    # call's 120: whatever comes after it, GLEIF or OpenFIGI, runs
    # into the deadline, and that is the rate limit's doing.
    state = {"first": True, "limiting": True}

    def handler(params, timeout):
        if state["limiting"] and state["first"]:
            state["first"] = False
            return _response(429, headers={"Retry-After": "110"})
        if state["limiting"] and cut_off_in == "gleif":
            clock.now += timeout
            raise requests.Timeout("read timed out")
        clock.now += 0.1
        return _response()
    session.handler = handler

    def hanging_openfigi(*args, timeout=None, **kwargs):
        if not state["limiting"]:
            raise requests.ConnectionError("OpenFIGI is offline")
        clock.now += timeout
        raise requests.Timeout("OpenFIGI read timed out")
    monkeypatch.setattr(openfigi.requests, "post", hanging_openfigi)
    job_id = _create_job(client, [f",{ISIN}"])

    for _ in range(app_module.RUN_MAX_ATTEMPTS + 2):
        state["first"] = True
        response = _run(client, job_id)
        assert response.status_code == 200
        body = response.get_json()
        assert body["throttled"] is True
        assert 0 < body["retry_after"] <= gleif.MAX_RETRY_AFTER
        assert (body["searched"], body["done"]) == (0, False)
        assert _attempts(job_id) == [0]

    state["limiting"] = False
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (1, True)
    assert not _notes(job_id)[0].startswith("Lookup failed")


@pytest.mark.parametrize("retry_after", ["45", "59"])
def test_lookup_a_rate_limit_pushed_past_the_deadline_is_not_too_slow(
    client, session, clock, monkeypatch, retry_after,
):
    # The lookup makes 20 requests of 4 s each: 80 s, well inside a
    # call's 120. A rate limit of under a minute (GLEIF counts
    # requests per minute) at each call's first request pushes it
    # past the deadline, and that is the rate limit's doing.
    monkeypatch.setattr(openfigi.requests, "post", _openfigi_two_names)
    four_seconds = _answer_after(clock, 4)
    state = {"first": True, "limiting": True}

    def handler(params, timeout):
        if state["limiting"] and state["first"]:
            state["first"] = False
            return _response(429, headers={"Retry-After": retry_after})
        return four_seconds(params, timeout)
    session.handler = handler
    job_id = _create_job(client, [f"Alpha a.s.,{ISIN},CZ,Praha"])

    for _ in range(app_module.RUN_MAX_ATTEMPTS + 2):
        state["first"] = True
        response = _run(client, job_id)
        assert response.status_code == 200
        body = response.get_json()
        assert body["throttled"] is True
        assert body["retry_after"] == int(retry_after)
        assert (body["searched"], body["done"]) == (0, False)
        assert _attempts(job_id) == [0]

    state["limiting"] = False
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (1, True)
    assert not _notes(job_id)[0].startswith("Lookup failed")


# ---- (e) a Retry-After date that overflows ----

@pytest.mark.parametrize("header", OVERFLOWING_DATES)
def test_overflowing_retry_after_date_falls_back_to_the_default(header):
    response = _response(429, headers={"Retry-After": header})
    assert gleif._retry_after(response, 1.5) == 1.5


@pytest.mark.parametrize("header", OVERFLOWING_DATES)
def test_overflowing_retry_after_date_is_still_just_a_rate_limit(
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
    assert (body["searched"], body["done"]) == (1, False)

    limited["on"] = False
    body = _run(client, job_id).get_json()
    assert (body["searched"], body["done"]) == (3, True)
    assert not any(
        note.startswith("Lookup failed") for note in _notes(job_id)
    )


# ---- (f) a stored query record that no longer validates ----

def test_stored_record_that_no_longer_validates_is_failed_not_a_500(
    client, session,
):
    # An unfinished job saved before a name of only invisible
    # characters counted as no name at all.
    stale = {
        "name": "\u200b", "isin": None, "country": "CZ", "city": None,
        "street": None, "postal_code": None,
    }
    fresh = {**stale, "name": "Alpha a.s."}
    job_id = "f" * 32
    storage.create_search(job_id=job_id, mode="bulk", query=[stale, fresh])

    response = _run(client, job_id)

    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"]) == (2, True)
    results = storage.get_search(job_id)["results"]
    assert results[0]["input"] == stale
    assert results[0]["match"]["notes"] == app_module.LOOKUP_ERROR_NOTE
    assert results[0]["match"]["match_type"] == "NO_MATCH"
    assert results[0]["closest"] == []
    assert results[1]["input"] == fresh
    assert not results[1]["match"]["notes"].startswith("Lookup failed")
    # The results page renders the job.
    page = client.get(f"/results?job={job_id}")
    assert page.status_code == 200

