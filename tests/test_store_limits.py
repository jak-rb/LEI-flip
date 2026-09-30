
"""Tests of the store's limits on Vercel and Postgres, and /admin paging.

On Vercel the store refuses a per-instance SQLite file (a missing
DATABASE_URL once made production lose searches between instances
without a trace). A Postgres connection is bounded (connect timeout,
keepalives, a statement timeout per transaction) and the lazy schema
creation takes an advisory lock; with no Postgres here, those are
checked against a fake psycopg that records what it is asked to do.
"""

import secrets
import sys
import types

import pytest

import app as app_module
from core import storage

CANT_STORE = {
    "error": "The search store is not available. Please try again later.",
    "error_cs": "Úložiště vyhledávání není dostupné. Zkuste to prosím "
                "později.",
}


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# ---- no database on Vercel ----

@pytest.fixture
def on_vercel_without_database(monkeypatch):
    monkeypatch.setattr(storage, "_ON_VERCEL", True)
    monkeypatch.setattr(storage, "DATABASE_URL", None)


@pytest.mark.usefixtures("on_vercel_without_database")
def test_on_vercel_the_store_refuses_a_local_sqlite_file(client):
    response = client.post(
        "/api/jobs", data={"mode": "single", "entity_name": "Alfa a.s."},
    )
    assert response.status_code == 503
    assert response.get_json() == CANT_STORE

    job_id = secrets.token_hex(16)
    assert client.post(f"/api/jobs/{job_id}/run").get_json() == CANT_STORE
    page = client.get(f"/results?job={job_id}")
    assert page.status_code == 503
    assert page.get_data(as_text=True) == (
        CANT_STORE["error"] + "\n" + CANT_STORE["error_cs"] + "\n"
    )


@pytest.mark.usefixtures("on_vercel_without_database")
def test_on_vercel_health_is_down_without_a_database(client):
    response = client.get("/health")
    assert (response.status_code, response.get_json()) == (
        503, {"status": "DOWN"},
    )


def test_health_is_up_with_a_store(client):
    assert client.get("/health").get_json() == {"status": "UP"}


# ---- the Postgres connection, against a fake psycopg ----

class _FakeCursor:
    description = [("job_id",)]

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakePostgres:
    """A psycopg connection that records statements and commits."""

    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append(sql)
        return _FakeCursor()

    def commit(self):
        self.log.append("COMMIT")

    def close(self):
        self.log.append("CLOSE")


@pytest.fixture
def fake_psycopg(monkeypatch):
    connects, log = [], []

    def connect(conninfo, **kwargs):
        connects.append((conninfo, kwargs))
        return _FakePostgres(log)

    psycopg = types.ModuleType("psycopg")
    psycopg.connect = connect
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    psycopg.rows = rows
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", rows)
    monkeypatch.setattr(storage, "DATABASE_URL", "postgresql://db.invalid/x")
    monkeypatch.setattr(storage, "_schema_ready", False)
    return connects, log


def test_a_postgres_connection_is_bounded(fake_psycopg):
    connects, _ = fake_psycopg
    storage.init_db()
    (conninfo, options), = connects
    assert conninfo == "postgresql://db.invalid/x"
    assert options["connect_timeout"] == 10
    assert options["prepare_threshold"] is None
    assert options["keepalives"] == 1
    assert options["tcp_user_timeout"] == 15_000


def test_every_postgres_transaction_gets_a_statement_timeout(fake_psycopg):
    _, log = fake_psycopg
    storage.get_search(secrets.token_hex(16))
    timeout = "SET LOCAL statement_timeout = '30s'"
    lock = "SELECT pg_advisory_xact_lock(%s)"
    assert log[:2] == [timeout, lock]
    assert "CREATE TABLE IF NOT EXISTS searches" in log[2]
    # The schema's transaction is committed; the query opens another.
    assert log[3:5] == ["COMMIT", timeout]
    assert log[5].lstrip().startswith("SELECT")
    assert log[-2:] == ["COMMIT", "CLOSE"]

    log.clear()
    storage.get_search(secrets.token_hex(16))
    assert log[0] == timeout
    assert lock not in log


# ---- /admin pages ----

def test_admin_pages_through_the_searches(client, monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_PAGE_SIZE", 2)
    job_ids = []
    for _ in range(3):
        job_ids.append(secrets.token_hex(16))
        storage.create_search(job_ids[-1], "single", [{"name": "A"}])

    first = client.get("/admin").get_data(as_text=True)
    assert job_ids[2] in first and job_ids[1] in first
    assert job_ids[0] not in first
    assert 'href="?page=2"' in first and "Newer" not in first

    second = client.get("/admin?page=2").get_data(as_text=True)
    assert job_ids[0] in second and job_ids[1] not in second
    assert 'href="?page=1"' in second

    assert client.get("/admin?page=0").status_code == 200
    assert client.get("/admin?page=x").status_code == 200

