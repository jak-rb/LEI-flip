
"""LEI lookup web app: the Flask routes (and the Vercel entrypoint).

Vercel loads the ``app`` instance from this file and runs it as one
function. A search runs as short, resumable steps: ``POST /api/jobs``
stores the entities to look up, and the browser then calls
``POST /api/jobs/<id>/run`` repeatedly, each call looking up the next
few entities against GLEIF and saving their results, until the job is
done. Every request therefore stays far below the function time limit,
and a page refresh mid-search resumes where it left off.
"""

import logging
import math
import secrets
import time
import unicodedata

from flask import (
    Flask,
    Request,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)
from pydantic import ValidationError

from core import export, storage
from core.gleif import (
    DeadlineExceeded,
    GleifApiError,
    GleifClient,
    GleifQueryError,
    GleifRateLimited,
    GleifServerError,
)
from core.lookup import lookup_entity
from core.models import InputEntity, InputError, LookupResult
from core.notes import czech_note
from core.upload import parse_upload

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

# Static files live in public/. Vercel's CDN serves that folder at the
# site root; pointing Flask's static route at it with no URL prefix
# makes local runs serve the same paths (url_for('static', ...) yields
# "/styles.css" in both).
app = Flask(__name__, static_folder="public", static_url_path="")

# Render a missing value as an empty cell rather than the text "None":
# many stored fields (a candidate's street, an ISIN-only input's
# country) are legitimately null.
app.jinja_env.finalize = lambda value: "" if value is None else value

# The results page shows each lookup note in English and in Czech.
app.jinja_env.filters["czech_note"] = czech_note

# Reject any request body larger than this. Flask raises HTTP 413
# before the route runs, so an oversized upload is refused without
# being read into memory. Vercel itself caps request bodies at 4.5 MB,
# so the limit sits just under that.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MiB
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


class _UploadCapRequest(Request):
    """Flask's request, with form fields allowed up to the upload cap.

    Werkzeug refuses a form field (or a urlencoded body) over 500 kB
    with a 413 far below MAX_UPLOAD_BYTES, and Flask 3.0 has no config
    key for that limit. Werkzeug's cap of 1000 form parts stays: the
    page sends at most seven, so only a crafted request gets past it.
    """

    max_form_memory_size = MAX_UPLOAD_BYTES


app.request_class = _UploadCapRequest


@app.errorhandler(413)
def request_too_large(error):
    """Refuse an oversized request with a JSON error the page can show.

    Covers a body over MAX_UPLOAD_BYTES and more form parts than
    Werkzeug parses. Vercel's own limit (bodies over 4.5 MB) answers
    before the app runs, with plain text.
    """
    return {
        "error": "The request is too large (max 4 MB).",
        "error_cs": "Požadavek je příliš velký (max. 4 MB).",
    }, 413


# Allowed bulk-upload extensions. The browser checks this too, but a
# request can reach the server without going through our JavaScript,
# so the server must enforce the rule itself.
ALLOWED_UPLOAD_EXTENSIONS = (".xlsx", ".csv", ".tsv", ".txt")

#: How many entities one /run call looks up at most, and the wall-clock
#: budget after which a call starts no further lookup and returns
#: partial progress.
RUN_CHUNK_SIZE = 5
RUN_TIME_BUDGET_SECONDS = 40

#: The GLEIF client's deadline, in seconds from the start of a /run
#: call: no request or retry starts after it and each request's
#: timeout is cut to fit it, so a call ends by then even when GLEIF is
#: slow, far under the function's time limit. It lies well past the
#: budget because one lookup makes up to about 20 requests, which a
#: slow or flaky GLEIF can stretch past the budget while still
#: answering every one.
RUN_DEADLINE_SECONDS = 120

#: How many /run calls may fail on the same entity - GLEIF answering
#: its lookup with server errors, or the lookup not fitting even into a
#: call's whole deadline - before it is stored as a failed lookup, so
#: that one bad entity cannot stall the job for ever.
RUN_MAX_ATTEMPTS = 3

GLEIF_DOWN_MESSAGE = "GLEIF service is unavailable. Please try again later."
GLEIF_DOWN_MESSAGE_CS = "Služba GLEIF je nedostupná. Zkuste to prosím později."

# Notes of an entity stored as a failed lookup (the no-match table and
# the downloads show them, like the notes core/lookup.py writes).
LOOKUP_ERROR_NOTE = (
    "Lookup failed because of an internal error - LEI not assigned. "
    "Please search this entity again."
)
GLEIF_REFUSED_NOTE = (
    "Lookup failed: GLEIF refused the query - LEI not assigned. Please "
    "check the entered values."
)
GLEIF_ERRORS_NOTE = (
    "Lookup failed: GLEIF kept answering with an error - LEI not "
    "assigned. Please search this entity again later."
)
GLEIF_TOO_SLOW_NOTE = (
    "Lookup failed: the GLEIF search took too long - LEI not assigned. "
    "Please search this entity again later."
)


@app.route("/health")
def health():
    """Liveness probe."""
    return jsonify(status="UP"), 200


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/admin")
def admin():
    """Unlinked page dumping the whole searches table (no auth).

    Not reachable from any link - the URL has to be typed. It renders
    every column and row of the searches table and nothing else.
    """
    columns, rows = storage.get_all_searches()
    return render_template("admin.html", columns=columns, rows=rows)


def _entity_input(entity: InputEntity) -> dict:
    """The stored 'input' record for one entity (also the job's query)."""
    return {
        "name": entity.name,
        "isin": entity.isin,
        "country": entity.country,
        "city": entity.town,
        "street": entity.street,
        "postal_code": entity.zip_code,
    }


def _entity_from_input(record: dict) -> InputEntity:
    """Rebuild the InputEntity a job's stored query record describes."""
    return InputEntity(
        name=record.get("name"),
        isin=record.get("isin"),
        street=record.get("street"),
        town=record.get("city"),
        country=record.get("country"),
        zip_code=record.get("postal_code"),
    )


def _result_row(entity, result, closest) -> dict:
    """Build one stored result row: input, matched result, closest."""
    return {
        "input": _entity_input(entity),
        "match": result.model_dump(),
        "closest": [candidate.model_dump() for candidate in closest],
    }


def _bucket_counts(results: list) -> dict:
    """Count looked-up rows per bucket: matched / to validate / miss.

    Every row lands in exactly one bucket: an asserted match, a
    near-miss with candidates to validate, or an outright miss.
    """
    matched = need_validation = unmatched = 0
    for row in results:
        if (row.get("match") or {}).get("lei"):
            matched += 1
        elif row.get("closest"):
            need_validation += 1
        else:
            unmatched += 1
    return {
        "matched": matched,
        "need_validation": need_validation,
        "unmatched": unmatched,
    }


def _progress(search: dict) -> dict:
    """A job's progress: entities done, per-bucket counts, finished?"""
    results = search["results"]
    total = len(search["query"])
    return {
        "job_id": search["job_id"],
        "searched": len(results),
        "total": total,
        **_bucket_counts(results),
        "done": len(results) >= total,
    }


def _form_value(key: str) -> str | None:
    """A single-form field without surrounding whitespace, or None."""
    return (request.form.get(key) or "").strip() or None


def _none_if_invisible(value: str | None) -> str | None:
    """None for a value of only whitespace and format characters."""
    # Format characters (category Cf: U+200B, U+FEFF, ...) are
    # invisible, so a field holding only them looks empty to the user.
    if value and all(
        char.isspace() or unicodedata.category(char) == "Cf"
        for char in value
    ):
        return None
    return value


def _single_entities() -> list[InputEntity]:
    """The one entity of the single-lookup form.

    Raises:
        InputError: With the message to show when the input is unusable.
    """
    name = _none_if_invisible(_form_value("entity_name"))
    isin = _none_if_invisible(_form_value("isin"))
    if not name and not isin:
        raise InputError(
            "Please enter an entity name or an ISIN.",
            "Zadejte název subjektu nebo ISIN.",
        )
    try:
        entity = InputEntity(
            name=name,
            isin=isin,
            street=_form_value("street"),
            town=_form_value("city"),
            country=_form_value("country"),
            zip_code=_form_value("postal_code"),
        )
    except ValidationError as error:
        raise InputError(
            "Please check the entered values.",
            "Zkontrolujte prosím zadané hodnoty.",
        ) from error
    return [entity]


def _bulk_entities() -> list[InputEntity]:
    """The entities of the uploaded bulk file.

    Re-checks the extension the browser also checks, then parses the
    file, so content problems - empty, no usable rows, too many rows -
    are reported up front rather than mid-search. (The size cap is
    enforced separately by MAX_CONTENT_LENGTH, which makes Flask
    return 413.)

    Raises:
        InputError: With the message to show when the file is unusable.
    """
    upload = request.files.get("file_upload")
    filename = (upload.filename or "") if upload else ""
    if not filename:
        raise InputError(
            "Please attach a .xlsx, .csv, .tsv or .txt file.",
            "Přiložte prosím soubor .xlsx, .csv, .tsv nebo .txt.",
        )
    if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        raise InputError(
            "Unsupported file type. Please upload a .xlsx, .csv, .tsv or "
            ".txt file.",
            "Nepodporovaný typ souboru. Nahrajte prosím soubor .xlsx, "
            ".csv, .tsv nebo .txt.",
        )
    return parse_upload(filename, upload.read())


@app.route("/api/jobs", methods=["POST"])
def create_job():
    """Create a search job from the single form or a bulk upload.

    Validates the input and stores the entities to look up under a new
    ``job_id`` (nothing is looked up yet). Returns ``{"job_id", "total"}``,
    or 400 with ``{"error", "error_cs"}`` (the message in English and
    Czech) when the input is unusable, so the search page can show it
    next to its Search button.
    """
    mode = "bulk" if request.form.get("mode") == "bulk" else "single"
    try:
        entities = _bulk_entities() if mode == "bulk" else _single_entities()
    except ValueError as error:
        # An InputError carries its Czech version; any other error only
        # has its own text.
        return {
            "error": str(error),
            "error_cs": getattr(error, "message_cs", str(error)),
        }, 400

    job_id = secrets.token_hex(16)
    storage.create_search(
        job_id=job_id,
        mode=mode,
        query=[_entity_input(entity) for entity in entities],
    )
    return {"job_id": job_id, "total": len(entities)}


def _failed_row(entity: InputEntity, note: str) -> dict:
    """The stored row of an entity whose lookup failed: a NO_MATCH."""
    return _result_row(entity, LookupResult(notes=note), [])


def _gave_up(job_id: str, index: int) -> bool:
    """Count a failed attempt at an entity; True once none are left."""
    failures = storage.record_failed_attempt(job_id, index)
    return failures is not None and failures >= RUN_MAX_ATTEMPTS


def _lookup_row(
    job_id: str,
    index: int,
    entity: InputEntity,
    client: GleifClient,
    alone: bool,
) -> dict:
    """Look up one entity of a job and build its stored result row.

    A lookup that failed for good becomes a NO_MATCH row whose note
    says so: an unexpected error, a query GLEIF refuses, or - once
    RUN_MAX_ATTEMPTS calls have failed on the entity - GLEIF server
    errors or a lookup too slow for a call's whole deadline. Whatever
    is worth retrying later is raised instead.

    Args:
        job_id: The job's id.
        index: The entity's position in the job's query.
        entity: The entity to look up.
        client: The call's open GLEIF client.
        alone: Whether the lookup has the call's whole deadline (it is
            the first of the call).

    Returns:
        The entity's result row.

    Raises:
        DeadlineExceeded: If the call's deadline cut the lookup off.
        GleifApiError: If GLEIF is unavailable or rate-limiting.
    """
    try:
        result, closest = lookup_entity(entity, client)
    except GleifQueryError as exc:
        logger.warning(
            "GLEIF refused entity %d of job %s: %s", index, job_id, exc
        )
        return _failed_row(entity, GLEIF_REFUSED_NOTE)
    except GleifServerError:
        if not _gave_up(job_id, index):
            raise
        logger.exception("Giving up on entity %d of job %s", index, job_id)
        return _failed_row(entity, GLEIF_ERRORS_NOTE)
    except DeadlineExceeded:
        # Cut off, not failed: the next call looks it up afresh. Only
        # a lookup that had the call's whole deadline to itself counts
        # as failing, as trying that again cannot go better.
        if not alone or not _gave_up(job_id, index):
            raise
        logger.warning(
            "Giving up on entity %d of job %s: too slow", index, job_id
        )
        return _failed_row(entity, GLEIF_TOO_SLOW_NOTE)
    except GleifApiError:
        raise
    except Exception:
        # A bug or a reply of a shape nobody expected: retrying the
        # entity would only fail the same way again.
        logger.exception(
            "Lookup failed for entity %d of job %s", index, job_id
        )
        return _failed_row(entity, LOOKUP_ERROR_NOTE)
    return _result_row(entity, result, closest)


@app.route("/api/jobs/<job_id>/run", methods=["POST"])
def run_job(job_id: str):
    """Look up the next few pending entities of a job and save them.

    Returns the job's progress (see ``_progress``); the browser keeps
    calling until ``done`` is true. A GLEIF outage saves whatever
    completed and answers 503 with an ``error`` message (and its Czech
    ``error_cs``), so a later call resumes from there. When GLEIF
    rate-limits for longer than the call has left, the reply is the
    progress plus ``throttled`` and ``retry_after`` (the seconds to
    wait before the next call). Once RUN_TIME_BUDGET_SECONDS have
    passed no further lookup starts, and the one under way must end by
    RUN_DEADLINE_SECONDS. A lookup cut off by that deadline is not
    stored and the next call starts it again, while one that failed
    for good is stored as a failed lookup (see ``_lookup_row``), so
    the job can always finish. Two calls racing on one job (a second
    tab, or a refresh while the previous call is still running) cannot
    store an entity twice: the store only accepts rows that continue
    from the result count this call started at.
    """
    search = storage.get_search(job_id)
    if search is None:
        return {
            "error": "Search not found.",
            "error_cs": "Vyhledávání nebylo nalezeno.",
        }, 404

    offset = len(search["results"])
    pending = search["query"][offset:]
    rows = []
    gleif_down = False
    retry_after = None
    started = time.monotonic()
    with GleifClient() as client:
        client.deadline = started + RUN_DEADLINE_SECONDS
        for index, record in enumerate(pending[:RUN_CHUNK_SIZE], offset):
            entity = _entity_from_input(record)
            try:
                rows.append(
                    _lookup_row(job_id, index, entity, client, alone=not rows)
                )
            except DeadlineExceeded:
                break
            except GleifRateLimited as exc:
                logger.warning("GLEIF lookup paused: %s", exc)
                retry_after = exc.retry_after
                break
            except GleifApiError as exc:
                logger.exception("GLEIF lookup failed: %s", exc)
                gleif_down = True
                break
            if time.monotonic() - started > RUN_TIME_BUDGET_SECONDS:
                break

    if rows:
        # None: a rival call stored these entities first (or the job
        # expired), so the store's own progress is what we report.
        search = (
            storage.append_results(job_id, rows, offset)
            or storage.get_search(job_id)
            or search
        )
    progress = _progress(search)
    if gleif_down:
        return {
            "error": GLEIF_DOWN_MESSAGE,
            "error_cs": GLEIF_DOWN_MESSAGE_CS,
            **progress,
        }, 503
    if retry_after is not None:
        return {
            **progress,
            "throttled": True,
            "retry_after": math.ceil(retry_after),
        }
    return progress


@app.route("/download/csv")
def download_csv():
    """Serve a stored search as a CSV attachment (keyed by ?job=)."""
    search = storage.get_search(request.args.get("job", ""))
    if search is None:
        abort(404)
    return Response(
        export.build_csv(search),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


@app.route("/download/excel")
def download_excel():
    """Serve a stored search as an Excel attachment (keyed by ?job=)."""
    search = storage.get_search(request.args.get("job", ""))
    if search is None:
        abort(404)
    return send_file(
        export.build_xlsx(search),
        mimetype=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        ),
        as_attachment=True,
        download_name="results.xlsx",
    )


def _candidate_by_lei(closest: list, lei) -> dict | None:
    """The stored candidate with this LEI, or None if absent."""
    for candidate in closest:
        if candidate.get("lei") == lei:
            return candidate
    return None


def _partition(results: list) -> dict:
    """Sort stored rows into matched / no-match / to-validate groups.

    A row is "to validate" when it has candidates but no algorithmic
    match. Such a row also appears under matched (if a candidate was
    confirmed) or no-match (if marked "none"), so the bottom tables show
    the current state while the stepper stays navigable for changes.

    Args:
        results: The stored per-entity result rows.

    Returns:
        A dict with the ``matched``, ``no_match``, and ``to_validate``
        display lists plus the ``validate_total`` / ``validate_done``
        counters.
    """
    matched = []
    no_match = []
    to_validate = []

    for index, row in enumerate(results):
        source = row.get("input", {})
        match = row.get("match", {})
        closest = row.get("closest", [])
        decision = row.get("decision") or {}
        algo_lei = match.get("lei")

        if closest and not algo_lei:
            to_validate.append({
                "index": index,
                "input": source,
                "closest": closest,
                "decision": decision or None,
            })

        if algo_lei:
            matched.append({
                "searched": source.get("name") or source.get("isin"),
                "legal_name": match.get("gleif_legal_name"),
                "country": match.get("gleif_legal_country"),
                "city": match.get("gleif_legal_city"),
                "street": match.get("gleif_legal_street"),
                "overall": match.get("confidence"),
                "lei": algo_lei,
            })
        elif decision.get("status") == "confirmed":
            candidate = _candidate_by_lei(closest, decision.get("lei"))
            if candidate:
                matched.append({
                    "searched": source.get("name") or source.get("isin"),
                    "legal_name": candidate.get("legal_name"),
                    "country": candidate.get("country"),
                    "city": candidate.get("city"),
                    "street": candidate.get("street"),
                    "overall": candidate.get("overall"),
                    "lei": candidate.get("lei"),
                })

        if decision.get("status") == "none" or (not algo_lei and not closest):
            no_match.append({
                "searched": source.get("name") or source.get("isin"),
                "country": source.get("country"),
                "city": source.get("city"),
                "notes": match.get("notes"),
            })

    done = sum(1 for record in to_validate if record["decision"])
    return {
        "matched": matched,
        "no_match": no_match,
        "to_validate": to_validate,
        "validate_total": len(to_validate),
        "validate_done": done,
        # Summary-card counts (each searched entity lands in one bucket).
        "matched_count": len(matched),
        "need_validation_count": len(to_validate) - done,
        "unmatched_count": len(no_match),
    }


@app.route("/results")
def results():
    """Render the results page for a stored search.

    Reads the ``?job=`` query parameter and loads that search from the
    store. An unknown or expired id renders an empty-state message. A
    job that is still being looked up renders the summary card in its
    running state (the page's script then drives ``/api/jobs/<id>/run``
    to completion); a finished job renders the card's final counts and
    the matched / no-match / to-validate tables under it.
    """
    job_id = request.args.get("job", "")
    search = storage.get_search(job_id) if job_id else None
    if search is None:
        return render_template("results.html", search=None)

    progress = _progress(search)
    running = not progress["done"]
    groups = None if running else _partition(search["results"])
    return render_template(
        "results.html",
        search=search,
        progress=progress,
        running=running,
        groups=groups,
    )


@app.route("/api/decision", methods=["POST"])
def decision():
    """Record the user's manual match choice for one searched entity.

    Expects a JSON body ``{"job_id", "index", "choice"}`` where choice
    is a candidate's LEI to confirm or "none" for no match. Flags the
    choice on the stored search so the detail page and downloads reflect
    it. Returns the saved decision plus ``counts``, the summary card's
    matched / need-validation / unmatched numbers after it (so the page
    can update the card without a reload); 400 with
    ``{"error", "error_cs"}`` if the body is not such an object (a
    string job id and choice, an integer index); or 404, also with
    ``error`` and ``error_cs``, if the search is unknown or still
    running, or the index or the candidate LEI is not one the page
    offers for validation.
    """
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        # JSON nested thousands deep overflows the parser instead of
        # failing as invalid JSON (which silent=True turns into None).
        data = None
    # type() rather than isinstance(): a JSON true or false arrives as
    # a bool, which Python counts as an int.
    if not (
        isinstance(data, dict)
        and isinstance(data.get("job_id"), str)
        and type(data.get("index")) is int
        and isinstance(data.get("choice"), str)
    ):
        return {
            "error": "Invalid decision request.",
            "error_cs": "Neplatný požadavek na rozhodnutí.",
        }, 400
    saved = storage.record_decision(
        data["job_id"], data["index"], data["choice"],
    )
    if saved is None:
        return {
            "error": "Search or record not found.",
            "error_cs": "Vyhledávání nebo záznam nebyl nalezen.",
        }, 404
    reply = {"ok": True, "decision": saved}
    search = storage.get_search(data["job_id"])
    if search is not None:  # None only if it expired since the save
        groups = _partition(search["results"])
        reply["counts"] = {
            "matched": groups["matched_count"],
            "need_validation": groups["need_validation_count"],
            "unmatched": groups["unmatched_count"],
        }
    return reply


if __name__ == "__main__":
    app.run(port=8080)

