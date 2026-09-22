
"""Tests for the search store (run against the local SQLite backend)."""

import secrets

from core import storage


def _job(query=None):
    """Create a fresh search and return its id."""
    job_id = secrets.token_hex(8)
    storage.create_search(
        job_id=job_id, mode="bulk",
        query=query or [{"name": "A"}, {"name": "B"}],
    )
    return job_id


def _row(lei=None, closest=()):
    return {
        "input": {"name": "x"},
        "match": {"lei": lei},
        "closest": [{"lei": c} for c in closest],
    }


def test_create_then_get_is_empty():
    job_id = _job()
    search = storage.get_search(job_id)
    assert search["mode"] == "bulk"
    assert search["query"] == [{"name": "A"}, {"name": "B"}]
    assert search["results"] == []
    assert search["searched"] == 0 and search["found"] == 0


def test_append_results_advances_progress_and_found():
    job_id = _job()
    updated = storage.append_results(job_id, [_row(lei="L1")], 0)
    assert updated["searched"] == 1 and updated["found"] == 1

    updated = storage.append_results(job_id, [_row()], 1)
    assert updated["searched"] == 2 and updated["found"] == 1
    assert storage.get_search(job_id)["results"] == updated["results"]


def test_append_to_unknown_job_returns_none():
    assert storage.append_results("nope", [_row()], 0) is None


def test_append_at_stale_offset_is_rejected():
    # Two requests racing on one job both start from zero stored
    # results; the one that loses must not store the entity twice.
    job_id = _job()
    assert storage.append_results(job_id, [_row(lei="L1")], 0) is not None
    assert storage.append_results(job_id, [_row(lei="L1")], 0) is None
    assert storage.get_search(job_id)["searched"] == 1

    updated = storage.append_results(job_id, [_row()], 1)
    assert updated["searched"] == 2


def test_get_unknown_job_returns_none():
    assert storage.get_search("nope") is None


def test_record_decision_confirm_none_and_invalid():
    job_id = _job()
    storage.append_results(job_id, [_row(closest=("C1", "C2"))], 0)

    assert storage.record_decision(job_id, 0, "C2") == {
        "status": "confirmed", "lei": "C2",
    }
    assert storage.get_search(job_id)["results"][0]["decision"]["lei"] == "C2"

    assert storage.record_decision(job_id, 0, "none") == {"status": "none"}
    # Unknown LEI, out-of-range index, non-int index, unknown job.
    assert storage.record_decision(job_id, 0, "ZZZ") is None
    assert storage.record_decision(job_id, 5, "C1") is None
    assert storage.record_decision(job_id, "0", "C1") is None
    assert storage.record_decision("nope", 0, "C1") is None


def test_prune_drops_rows_past_retention():
    old_id = _job()
    with storage._connect() as conn:
        conn.execute(
            "UPDATE searches SET created_at = %s WHERE job_id = %s",
            ("2000-01-01T00:00:00+00:00", old_id),
        )
    assert storage.get_search(old_id) is not None

    _job()  # a write prunes first
    assert storage.get_search(old_id) is None


def test_get_all_searches_returns_columns_and_newest_first():
    first = _job()
    second = _job()
    columns, rows = storage.get_all_searches()
    assert columns[:2] == ["job_id", "created_at"]
    ids = [row["job_id"] for row in rows]
    assert ids.index(second) < ids.index(first)
    assert isinstance(rows[0]["results"], str)

