
"""SQLite store for per-search results.

Uses the standard-library ``sqlite3`` (no extra dependency). A single
``searches`` table holds one row per search - its query and its results
- pruned after a retention window.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


def _resolve_db_path() -> Path:
    """Locate a writable path for the SQLite store.

    The store needs a writable filesystem, and the application directory
    is not one: on CodeNow the image filesystem is read-only, so creating
    the database next to the read-only lookup tables fails with "unable
    to open database file". Resolve the path in this order:

    1. ``LEI_DB_PATH`` - set this to a mounted, writable (ideally
       persistent) volume so the search history survives redeploys.
    2. A ``lei-lookup`` folder under the system temp directory, which is
       writable in a typical container. This keeps the app running, but
       the history is lost when the container restarts.
    """
    configured = os.environ.get("LEI_DB_PATH")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "lei-lookup" / "lei_lookup.db"


#: SQLite store location, on a writable filesystem (see
#: ``_resolve_db_path``). The read-only lookup tables stay in the app's
#: ``data/`` folder; only this file needs write access, so it lives apart.
DB_PATH = _resolve_db_path()

#: Per-search rows older than this are deleted on the next write.
SEARCH_RETENTION_DAYS = 30


def _connect() -> sqlite3.Connection:
    """Open a connection to the store, creating the folder if needed."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the searches table if missing, and migrate older stores."""
    with _connect() as conn:
        conn.execute(
            """
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
        )
        # Drop the old write-only analytics table: searches is now the
        # whole store (one table, holding each query and its results).
        conn.execute("DROP TABLE IF EXISTS usage_events")
        # Migrate older stores to the current schema.
        columns = [
            row["name"] for row in conn.execute("PRAGMA table_info(searches)")
        ]
        # Drop a summary "confidence" column earlier versions carried.
        if "confidence" in columns:
            conn.execute("ALTER TABLE searches DROP COLUMN confidence")
        # Add the query column to stores created before it existed.
        if "query" not in columns:
            conn.execute(
                "ALTER TABLE searches "
                "ADD COLUMN query TEXT NOT NULL DEFAULT '[]'"
            )


def _prune_searches(conn: sqlite3.Connection) -> None:
    """Delete per-search rows older than the retention window."""
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=SEARCH_RETENTION_DAYS)
    ).isoformat()
    conn.execute("DELETE FROM searches WHERE created_at < ?", (cutoff,))


def record_search(
    job_id: str,
    mode: str,
    searched: int,
    found: int,
    query: list,
    results: list,
) -> None:
    """Write one row recording a search: its query and its results.

    Old rows are pruned first. Both ``query`` (what the user searched)
    and ``results`` (the detailed per-entity payload) are stored as JSON.

    Args:
        job_id: Random id identifying this search.
        mode: "single" or "bulk".
        searched: Entities processed (for the summary).
        found: Confident matches (for the summary).
        query: The searched entities' input fields, one dict per entity.
        results: Per-entity detail rows, JSON-serialisable.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        _prune_searches(conn)
        conn.execute(
            "INSERT INTO searches (job_id, created_at, mode, searched, "
            "found, query, results) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, now, mode, searched, found,
             json.dumps(query), json.dumps(results)),
        )


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
        row = conn.execute(
            "SELECT job_id, created_at, mode, searched, found, "
            "query, results FROM searches WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
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
        cursor = conn.execute(
            "SELECT * FROM searches ORDER BY created_at DESC"
        )
        columns = [description[0] for description in cursor.description]
        rows = [dict(row) for row in cursor.fetchall()]
    return columns, rows


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
            "UPDATE searches SET results = ? WHERE job_id = ?",
            (json.dumps(results), job_id),
        )
    return decision

