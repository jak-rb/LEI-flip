
"""Store for per-search results: Postgres on Vercel, SQLite locally.

One ``searches`` table holds one row per search - its query (the
entities to look up) and its results (one row per entity looked up so
far) - pruned after a retention window. The backend is picked from the
environment: when ``DATABASE_URL`` (or ``POSTGRES_URL``) is set, the
store is that Postgres database (the Neon database attached to the
Vercel project); otherwise it is a local SQLite file, so the app runs
with no setup during development and in tests.

Both backends share one DDL and one set of statements: JSON payloads
are stored as text, timestamps as ISO-8601 UTC strings (which sort
correctly as text), and statements use ``%s`` placeholders (psycopg's
style) that the SQLite path rewrites to ``?``.
"""

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

#: Postgres connection string. The Neon integration sets DATABASE_URL
#: on the Vercel project; with neither variable set the store is SQLite.
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get(
    "POSTGRES_URL"
)

#: Per-search rows older than this are deleted on the next write.
SEARCH_RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    job_id     TEXT    PRIMARY KEY,
    created_at TEXT    NOT NULL,
    mode       TEXT    NOT NULL,
    searched   INTEGER NOT NULL,
    found      INTEGER NOT NULL,
    query      TEXT    NOT NULL DEFAULT '[]',
    results    TEXT    NOT NULL
)
"""

# Whether the schema has been ensured in this process (once per cold
# start on Vercel).
_schema_ready = False


def using_postgres() -> bool:
    """Whether the store is the configured Postgres database."""
    return bool(DATABASE_URL)


def _sqlite_path() -> Path:
    """The local SQLite file: ``LEI_DB_PATH``, else under the temp dir."""
    configured = os.environ.get("LEI_DB_PATH")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "lei-lookup" / "lei_lookup.db"


class _Connection:
    """One open connection with a backend-neutral query interface.

    Statements are written with ``%s`` placeholders; the SQLite path
    rewrites them to ``?``. Rows come back as plain dicts.
    """

    def __init__(self) -> None:
        if using_postgres():
            # Imported lazily: not needed (or installed) for local SQLite.
            import psycopg
            from psycopg.rows import dict_row

            self._raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
            self._sqlite = False
        else:
            path = _sqlite_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            self._raw = sqlite3.connect(path)
            self._raw.row_factory = sqlite3.Row
            self._sqlite = True

    def execute(self, sql: str, params: tuple = ()):
        """Run one statement and return its cursor."""
        if self._sqlite:
            return self._raw.execute(sql.replace("%s", "?"), params)
        return self._raw.execute(sql, params or None)

    def fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        """Run a query and return its first row as a dict, or None."""
        row = self.execute(sql, params).fetchone()
        return None if row is None else dict(row)

    def fetchall(
        self, sql: str, params: tuple = ()
    ) -> tuple[list[str], list[dict]]:
        """Run a query; return its column names and all rows as dicts."""
        cursor = self.execute(sql, params)
        columns = [column[0] for column in cursor.description]
        return columns, [dict(row) for row in cursor.fetchall()]

    def commit(self) -> None:
        """Commit the open transaction."""
        self._raw.commit()

    def close(self) -> None:
        """Close the connection."""
        self._raw.close()


@contextmanager
def _connect() -> Iterator[_Connection]:
    """Open a connection, ensure the schema once, commit on success."""
    global _schema_ready
    conn = _Connection()
    try:
        if not _schema_ready:
            conn.execute(_SCHEMA)
            conn.commit()
            _schema_ready = True
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the searches table if it is missing."""
    with _connect():
        pass


def _now() -> str:
    """The current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _prune_searches(conn: _Connection) -> None:
    """Delete per-search rows older than the retention window."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=SEARCH_RETENTION_DAYS)
    ).isoformat()
    conn.execute("DELETE FROM searches WHERE created_at < %s", (cutoff,))


def create_search(job_id: str, mode: str, query: list) -> None:
    """Create a search: its entities to look up, with no results yet.

    Old rows are pruned first. ``searched`` and ``found`` start at zero
    and advance as ``append_results`` saves looked-up entities.

    Args:
        job_id: Random id identifying this search.
        mode: "single" or "bulk".
        query: The entities' input fields, one dict per entity, in the
            order they will be looked up.
    """
    with _connect() as conn:
        _prune_searches(conn)
        conn.execute(
            "INSERT INTO searches (job_id, created_at, mode, searched, "
            "found, query, results) VALUES (%s, %s, %s, 0, 0, %s, '[]')",
            (job_id, _now(), mode, json.dumps(query)),
        )


def append_results(
    job_id: str, rows: list, offset: int
) -> Optional[dict]:
    """Append looked-up entity rows to a search and advance its progress.

    Args:
        job_id: The search's id.
        rows: Per-entity result rows (JSON-serialisable), in query
            order, continuing the stored results at position ``offset``.
        offset: How many results were stored when the caller picked the
            entities these rows answer (``len(search["results"])``).

    Returns:
        The updated search (as ``get_search`` returns it), or None if
        the job is missing or expired - or if the stored results have
        already moved past ``offset``: another request (a second tab,
        or a refresh mid-search) looked up the same entities first, so
        these rows are dropped instead of being stored twice.
    """
    search = get_search(job_id)
    if search is None or len(search["results"]) != offset:
        return None
    results = search["results"] + rows
    found = sum(1 for row in results if (row.get("match") or {}).get("lei"))
    with _connect() as conn:
        # The WHERE on ``searched`` makes the check-and-append atomic
        # against a rival request that wrote between our read and now.
        cursor = conn.execute(
            "UPDATE searches SET results = %s, searched = %s, found = %s "
            "WHERE job_id = %s AND searched = %s",
            (json.dumps(results), len(results), found, job_id, offset),
        )
        if cursor.rowcount != 1:
            return None
    search.update(results=results, searched=len(results), found=found)
    return search


def get_search(job_id: str) -> Optional[dict]:
    """Return a stored search by id, or None if it is absent/expired.

    Args:
        job_id: The search's id.

    Returns:
        A dict of the summary columns plus ``query`` and ``results``
        (each JSON decoded back into a list), or None if no such row
        exists.
    """
    with _connect() as conn:
        data = conn.fetchone(
            "SELECT job_id, created_at, mode, searched, found, "
            "query, results FROM searches WHERE job_id = %s",
            (job_id,),
        )
    if data is None:
        return None
    data["query"] = json.loads(data["query"]) if data["query"] else []
    data["results"] = json.loads(data["results"])
    return data


def get_all_searches() -> tuple[list[str], list[dict]]:
    """Return (column names, all rows) of the searches table.

    A full dump for the admin page: every column and every stored row,
    newest first. ``query`` and ``results`` come back as their raw stored
    JSON strings (not decoded), so the page shows exactly what the table
    holds.
    """
    with _connect() as conn:
        return conn.fetchall(
            "SELECT * FROM searches ORDER BY created_at DESC"
        )


def record_decision(
    job_id: str, index: int, choice: Optional[str]
) -> Optional[dict]:
    """Flag the user's chosen candidate (or "none") on one entity row.

    The candidates are already stored with the search, so a decision
    only records which one the user confirmed - no candidate data is
    duplicated. The stored search is updated in place so the detail page
    and the downloads reflect the choice.

    Args:
        job_id: The search's id.
        index: Position of the entity in the search's results list.
        choice: A candidate's LEI to confirm, or "none" for no match.

    Returns:
        The saved decision dict, or None if the job is missing, the
        index is out of range, or the LEI is not one of that entity's
        stored candidates.
    """
    search = get_search(job_id)
    if search is None:
        return None
    results = search["results"]
    if not isinstance(index, int) or not (0 <= index < len(results)):
        return None

    row = results[index]
    if choice == "none":
        decision = {"status": "none"}
    else:
        candidate_leis = {c.get("lei") for c in row.get("closest", [])}
        if choice not in candidate_leis:
            return None
        decision = {"status": "confirmed", "lei": choice}

    row["decision"] = decision
    with _connect() as conn:
        conn.execute(
            "UPDATE searches SET results = %s WHERE job_id = %s",
            (json.dumps(results), job_id),
        )
    return decision

