
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
import re
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

#: A job id as ``app.create_job`` mints it (``secrets.token_hex(16)``).
#: Any other id is refused before it reaches the database: psycopg
#: raises on a NUL in a text parameter, which made such ids a 500.
_JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

#: How many times a decision reads the search and tries to write it
#: back. A try fails only when a rival write to that search landed
#: between the read and the write, so this is far beyond what one
#: user's tabs can produce.
_DECISION_ATTEMPTS = 50

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

    def begin_write(self) -> None:
        """Start a read-modify-write that holds the write lock.

        On SQLite, ``BEGIN IMMEDIATE`` takes the write lock before the
        read, so rival writers queue behind it and the compare-and-swap
        cannot miss. Without it every miss was a write transaction of
        its own, and a burst of simultaneous writers made some wait
        past the busy timeout ("database is locked"). On Postgres this
        sends nothing: psycopg opens the transaction with the first
        statement, and an UPDATE that meets a rival's uncommitted write
        waits on the row lock, then checks the compare-and-swap's WHERE
        against the row the rival committed, so there a miss never
        writes over the rival's change.
        """
        if self._sqlite:
            self._raw.execute("BEGIN IMMEDIATE")

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


def _is_job_id(job_id) -> bool:
    """Whether ``job_id`` is a str in the minted job id format."""
    return isinstance(job_id, str) and bool(
        _JOB_ID_PATTERN.fullmatch(job_id)
    )


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
        these rows are dropped instead of being stored twice. Also
        None if the rows would run past the job's entities.
    """
    search = get_search(job_id)
    if search is None or len(search["results"]) != offset:
        return None
    # Never past the query: decisions are only taken once every entity
    # has its row, so no append can land after one and wipe it.
    if offset + len(rows) > len(search["query"]):
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
        (each JSON decoded back into a list), or None if the id is not
        in the minted format (the database is not queried then) or no
        such row exists.
    """
    if not _is_job_id(job_id):
        return None
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
    and the downloads reflect the choice. Only a finished search takes
    decisions, and only on a row the detail page offers for validation.

    The write is a compare-and-swap: it lands only while the stored
    results are still exactly the text this call read; otherwise the
    call reads again and retries. A decision therefore never overwrites
    a rival decision on another row, or rows a /run appended, with its
    older copy of the results. On SQLite the read and the write run
    under one write lock (``begin_write``), so there the swap never
    misses.

    Args:
        job_id: The search's id.
        index: Position of the entity in the search's results list.
        choice: A candidate's LEI to confirm, or "none" for no match.

    Returns:
        The saved decision dict, or None if the job is missing or still
        running, the index is out of range, the row has nothing to
        validate, the LEI is not one of that entity's stored
        candidates, or rival writes won every attempt.
    """
    if not _is_job_id(job_id) or not isinstance(index, int):
        return None
    with _connect() as conn:
        for _ in range(_DECISION_ATTEMPTS):
            conn.begin_write()
            stored = conn.fetchone(
                "SELECT query, results FROM searches WHERE job_id = %s",
                (job_id,),
            )
            if stored is None:
                return None
            query = json.loads(stored["query"]) if stored["query"] else []
            results = json.loads(stored["results"])
            decision = _apply_decision(query, results, index, choice)
            if decision is None:
                return None
            cursor = conn.execute(
                "UPDATE searches SET results = %s "
                "WHERE job_id = %s AND results = %s",
                (json.dumps(results), job_id, stored["results"]),
            )
            if cursor.rowcount == 1:
                return decision
            # A rival write landed since the read: end this transaction
            # so the next read sees it.
            conn.commit()
    return None


def _apply_decision(
    query: list, results: list, index: int, choice: Optional[str]
) -> Optional[dict]:
    """Set the choice on ``results[index]``; None if not allowed."""
    if len(results) < len(query) or not (0 <= index < len(results)):
        return None
    row = results[index]
    # The detail page's to-validate test (app._partition): candidates,
    # but no algorithmic match.
    if (row.get("match") or {}).get("lei") or not row.get("closest"):
        return None
    if choice == "none":
        decision = {"status": "none"}
    else:
        candidate_leis = {c.get("lei") for c in row["closest"]}
        if choice not in candidate_leis:
            return None
        decision = {"status": "confirmed", "lei": choice}
    row["decision"] = decision
    return decision


def record_failed_attempt(job_id: str, index: int) -> Optional[int]:
    """Count one more failed lookup attempt for one entity of a search.

    The count is kept on the entity's own record in the stored query,
    as ``failed_attempts``, so it needs no schema change. The write is
    a compare-and-swap on the stored query text, so it is atomic on
    both backends: when a racing request changed the query since it
    was read, this attempt is not counted rather than written over
    the rival's newer count. On SQLite the read and the write run
    under one write lock (``begin_write``), so no rival can come
    between them.

    Args:
        job_id: The search's id.
        index: Position of the entity in the search's query.

    Returns:
        The entity's failed attempts so far, this one included, or None
        if the job is missing, the index is out of range, or a racing
        request's count won.
    """
    with _connect() as conn:
        conn.begin_write()
        data = conn.fetchone(
            "SELECT query FROM searches WHERE job_id = %s", (job_id,)
        )
        if data is None:
            return None
        query = json.loads(data["query"]) if data["query"] else []
        if not 0 <= index < len(query):
            return None
        count = query[index].get("failed_attempts", 0) + 1
        query[index]["failed_attempts"] = count
        cursor = conn.execute(
            "UPDATE searches SET query = %s WHERE job_id = %s AND query = %s",
            (json.dumps(query), job_id, data["query"]),
        )
        if cursor.rowcount != 1:
            return None
    return count

