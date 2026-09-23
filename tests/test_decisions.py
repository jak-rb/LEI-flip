
"""Tests for manual decisions and job ids: validation, rules and races.

The store runs on SQLite here. The races are forced deterministically
by running a rival write between a decision's read and its write
(through a hook on the store's connection), plus one threaded burst of
simultaneous decisions.
"""

import io
import secrets
import threading

import pytest

import app as app_module
from core import storage
from core.models import CandidateSummary, LookupResult, MatchType

MATCH_LEI = "M" * 20
RUNNER_UP_LEI = "U" * 20
REVIEW_LEI = "R" * 20


class _FakeGleifClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_lookup(entity, client):
    """Classify by name: "match" asserts a LEI, "review" is a near-miss.

    A match keeps a runner-up candidate, as real lookups often do.
    """
    name = (entity.name or "").lower()
    if "match" in name:
        result = LookupResult(
            lei=MATCH_LEI, lei_status="ISSUED",
            match_type=MatchType.FULL_MATCH, confidence=90,
            gleif_legal_name="Match AG",
        )
        runner_up = CandidateSummary(
            legal_name="Match Holding", lei=RUNNER_UP_LEI, status="ISSUED",
        )
        return result, [runner_up]
    if "review" in name:
        candidate = CandidateSummary(
            legal_name="Review Ltd", lei=REVIEW_LEI, status="ISSUED",
        )
        return LookupResult(notes="Strong name match, unverified"), [candidate]
    return LookupResult(notes="No LEI found in the GLEIF database."), []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "lookup_entity", _fake_lookup)
    monkeypatch.setattr(app_module, "GleifClient", _FakeGleifClient)
    app_module.app.config["TESTING"] = True
    # A crash must show up as the 500 a browser would get, not as an
    # exception raised into the test.
    monkeypatch.setitem(app_module.app.config, "PROPAGATE_EXCEPTIONS", False)
    return app_module.app.test_client()


def _finished_bulk_job(client, names):
    """Create a bulk job of these names and run it to completion."""
    content = "".join(f"{name},,CZ\n" for name in names).encode()
    created = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), "in.csv")},
        content_type="multipart/form-data",
    )
    job_id = created.get_json()["job_id"]
    while not client.post(f"/api/jobs/{job_id}/run").get_json()["done"]:
        pass
    return job_id


def _review_row(lei):
    """A stored near-miss row: no match, one candidate to validate."""
    return {
        "input": {"name": lei},
        "match": {"lei": None},
        "closest": [{"lei": lei}],
    }


def _store_job(rows, total=None):
    """Store a job of ``total`` entities, ``rows`` looked up."""
    job_id = secrets.token_hex(16)
    total = len(rows) if total is None else total
    storage.create_search(
        job_id=job_id, mode="bulk",
        query=[{"name": f"entity {i}"} for i in range(total)],
    )
    if rows:
        assert storage.append_results(job_id, rows, 0) is not None
    return job_id


def _decisions(job_id):
    """The stored decision of every row (None where undecided)."""
    results = storage.get_search(job_id)["results"]
    return [row.get("decision") for row in results]


def test_decision_rejects_malformed_bodies_with_a_json_4xx(client):
    job_id = _finished_bulk_job(client, ["Review Ltd"])
    deep = b"[" * 100_000 + b"]" * 100_000
    raw_bodies = [
        b'"x"', b"[1]", b"123", b"true", b"null", b"NaN", b"{", b"\xff",
        deep, b'{"job_id": ' + deep + b"}",
    ]
    objects = [
        {},
        {"job_id": [job_id], "index": 0, "choice": "none"},
        {"job_id": {"a": 1}, "index": 0, "choice": "none"},
        {"job_id": 123, "index": 0, "choice": "none"},
        {"job_id": job_id, "index": 0, "choice": [REVIEW_LEI]},
        {"job_id": job_id, "index": 0, "choice": {"x": 1}},
        {"job_id": job_id, "index": 0, "choice": None},
        {"job_id": job_id, "index": "0", "choice": "none"},
        {"job_id": job_id, "index": 0.0, "choice": "none"},
        {"job_id": job_id, "index": None, "choice": "none"},
        # A JSON true is not row 1, nor false row 0.
        {"job_id": job_id, "index": True, "choice": "none"},
        {"job_id": job_id, "index": False, "choice": "none"},
    ]
    responses = [
        (body, client.post(
            "/api/decision", data=body, content_type="application/json",
        ))
        for body in raw_bodies
    ] + [
        (body, client.post("/api/decision", json=body)) for body in objects
    ] + [
        ("form", client.post("/api/decision", data={"job_id": job_id})),
    ]

    wrong = [
        (repr(body)[:40], response.status_code)
        for body, response in responses
        if response.status_code not in (400, 404)
        or not response.is_json
        or "error" not in response.get_json()
    ]
    assert wrong == []
    assert _decisions(job_id) == [None]

    refused = client.post("/api/decision", data=b"[1]",
                          content_type="application/json").get_json()
    assert refused["error"] and refused["error_cs"]


MALFORMED_JOB_IDS = [
    "", "nope", "\x00", "0" * 31, "0" * 33, "A" * 32, "g" * 32,
    "0" * 32 + "\n", " " + "0" * 31, 123, None, ["0" * 32], {"a": 1},
    b"0" * 32,
]


class _UntouchableConnection:
    def __init__(self):
        raise AssertionError("the store was queried")


def test_store_ignores_malformed_job_ids_without_querying(monkeypatch):
    monkeypatch.setattr(storage, "_Connection", _UntouchableConnection)
    for job_id in MALFORMED_JOB_IDS:
        assert storage.get_search(job_id) is None, job_id
        assert storage.record_decision(job_id, 0, "none") is None, job_id
        assert storage.append_results(
            job_id, [_review_row(REVIEW_LEI)], 0,
        ) is None, job_id


def test_well_formed_unknown_job_id_is_simply_missing():
    assert storage.get_search(secrets.token_hex(16)) is None
    assert storage.record_decision(secrets.token_hex(16), 0, "none") is None


def test_malformed_job_ids_give_the_empty_state_or_404(client, monkeypatch):
    # On Postgres a NUL in the id made every one of these a 500 (psycopg
    # refuses NUL in text). Such an id must never reach the store.
    monkeypatch.setattr(storage, "_Connection", _UntouchableConnection)
    for job_id in ("%00", "A" * 32, "0" * 33):
        page = client.get(f"/results?job={job_id}")
        assert page.status_code == 200
        assert "No results to show" in page.get_data(as_text=True)
        assert client.get(f"/download/csv?job={job_id}").status_code == 404
        assert client.get(f"/download/excel?job={job_id}").status_code == 404
        run = client.post(f"/api/jobs/{job_id}/run")
        assert run.status_code == 404 and "error" in run.get_json()

    decided = client.post(
        "/api/decision",
        json={"job_id": "\x00", "index": 0, "choice": "none"},
    )
    assert decided.status_code == 404 and "error" in decided.get_json()


def test_decisions_only_on_finished_jobs_and_rows_to_validate():
    matched = {
        "input": {"name": "m"},
        "match": {"lei": MATCH_LEI},
        "closest": [{"lei": RUNNER_UP_LEI}],
    }
    missed = {"input": {"name": "x"}, "match": {"lei": None}, "closest": []}
    job_id = _store_job([matched, _review_row(REVIEW_LEI)], total=3)

    # Still running: the stepper is only shown for a finished job.
    assert storage.record_decision(job_id, 1, REVIEW_LEI) is None

    assert storage.append_results(job_id, [missed], 2) is not None
    # An algorithmic match, or a row with no candidates, has nothing to
    # validate.
    assert storage.record_decision(job_id, 0, "none") is None
    assert storage.record_decision(job_id, 0, RUNNER_UP_LEI) is None
    assert storage.record_decision(job_id, 2, "none") is None
    assert _decisions(job_id) == [None, None, None]

    assert storage.record_decision(job_id, 1, REVIEW_LEI) == {
        "status": "confirmed", "lei": REVIEW_LEI,
    }
    assert storage.record_decision(job_id, 1, "none") == {"status": "none"}
    assert _decisions(job_id) == [None, {"status": "none"}, None]


def test_decision_on_a_matched_row_is_refused_and_export_keeps_lei(client):
    job_id = _finished_bulk_job(client, ["Match AG", "Review Ltd"])
    for choice in ("none", RUNNER_UP_LEI):
        refused = client.post(
            "/api/decision",
            json={"job_id": job_id, "index": 0, "choice": choice},
        )
        assert refused.status_code == 404
    accepted = client.post(
        "/api/decision",
        json={"job_id": job_id, "index": 1, "choice": REVIEW_LEI},
    )
    assert accepted.status_code == 200

    csv = client.get(f"/download/csv?job={job_id}").get_data(as_text=True)
    assert MATCH_LEI in csv and "FULL_MATCH" in csv
    assert "MANUAL_NO_MATCH" not in csv
    assert csv.count("MANUAL_MATCH") == 1


def test_append_past_the_query_is_rejected():
    job_id = _store_job([], total=1)
    row = _review_row(REVIEW_LEI)
    assert storage.append_results(job_id, [row, row], 0) is None
    assert storage.append_results(job_id, [row], 0) is not None
    assert storage.append_results(job_id, [row], 1) is None
    assert storage.get_search(job_id)["searched"] == 1


def test_simultaneous_decisions_on_different_rows_all_persist():
    count = 20
    leis = [f"{i:020d}" for i in range(count)]
    job_id = _store_job([_review_row(lei) for lei in leis])
    barrier = threading.Barrier(count)
    saved = [None] * count
    errors = []

    def decide(index):
        try:
            barrier.wait()
            saved[index] = storage.record_decision(
                job_id, index, leis[index],
            )
        except Exception as error:  # reported by the assert below
            errors.append(error)

    threads = [
        threading.Thread(target=decide, args=(i,)) for i in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    expected = [{"status": "confirmed", "lei": lei} for lei in leis]
    assert saved == expected
    assert _decisions(job_id) == expected


def _hook_once(monkeypatch, method, wanted, rival):
    """Run ``rival`` once around the first call ``wanted`` picks."""
    original = getattr(storage._Connection, method)
    armed = [True]

    def hooked(self, sql, params=()):
        if not (armed[0] and wanted(sql)):
            return original(self, sql, params)
        armed[0] = False
        # Before a write lands, or after a read returned: either way the
        # hooked request then carries on with what it read earlier.
        if method == "execute":
            rival()
            return original(self, sql, params)
        result = original(self, sql, params)
        rival()
        return result

    monkeypatch.setattr(storage._Connection, method, hooked)
    return armed


def test_rival_decision_between_read_and_write_is_not_lost(monkeypatch):
    job_id = _store_job([_review_row("A" * 20), _review_row("B" * 20)])
    armed = _hook_once(
        monkeypatch, "execute",
        lambda sql: sql.startswith("UPDATE searches SET results = %s WHERE"),
        lambda: storage.record_decision(job_id, 1, "B" * 20),
    )

    assert storage.record_decision(job_id, 0, "A" * 20) is not None
    assert armed == [False]  # the rival really ran mid-decision
    assert _decisions(job_id) == [
        {"status": "confirmed", "lei": "A" * 20},
        {"status": "confirmed", "lei": "B" * 20},
    ]


def test_decision_racing_run_cannot_shrink_the_results(monkeypatch):
    # The decision reads a half-finished job, then /run saves the rest
    # before the decision writes. The results must keep all 10 rows.
    rows = [_review_row(f"{i:020d}") for i in range(10)]
    job_id = _store_job(rows[:5], total=10)
    armed = _hook_once(
        monkeypatch, "fetchone",
        lambda sql: "FROM searches WHERE job_id" in sql,
        lambda: storage.append_results(job_id, rows[5:], 5),
    )

    storage.record_decision(job_id, 0, rows[0]["closest"][0]["lei"])
    assert armed == [False]
    search = storage.get_search(job_id)
    assert len(search["results"]) == 10 and search["searched"] == 10

    # The job is finished now, so the same decision is accepted.
    assert storage.record_decision(
        job_id, 0, rows[0]["closest"][0]["lei"],
    ) is not None
    assert len(storage.get_search(job_id)["results"]) == 10

