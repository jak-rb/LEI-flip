
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
import secrets
import time

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_file,
)
from pydantic import ValidationError

from core import export, storage
from core.gleif import GleifApiError, GleifClient
from core.lookup import lookup_entity
from core.models import InputEntity
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

# Reject any request body larger than this. Flask raises HTTP 413
# before the route runs, so an oversized upload is refused without
# being read into memory. Vercel itself caps request bodies at 4.5 MB,
# so the limit sits just under that.
MAX_UPLOAD_BYTES = 4 * 1024 * 1024  # 4 MiB
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Allowed bulk-upload extensions. The browser checks this too, but a
# request can reach the server without going through our JavaScript,
# so the server must enforce the rule itself.
ALLOWED_UPLOAD_EXTENSIONS = (".xlsx", ".csv")

#: How many entities one /run call looks up at most, and the wall-clock
#: budget after which a call stops early and returns partial progress.
#: One lookup is a handful of GLEIF requests (more with retries or the
#: OpenFIGI fallback), so together these keep every call well under
#: the function's time limit.
RUN_CHUNK_SIZE = 5
RUN_TIME_BUDGET_SECONDS = 40

GLEIF_DOWN_MESSAGE = "GLEIF service is unavailable. Please try again later."


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


def _single_entities() -> list[InputEntity]:
    """The one entity of the single-lookup form.

    Raises:
        ValueError: With the message to show when the input is unusable.
    """
    name = request.form.get("entity_name", "").strip()
    isin = request.form.get("isin") or None
    if not name and not isin:
        raise ValueError("Please enter an entity name or an ISIN.")
    try:
        entity = InputEntity(
            name=name,
            isin=isin,
            street=request.form.get("street") or None,
            town=request.form.get("city") or None,
            country=request.form.get("country") or None,
            zip_code=request.form.get("postal_code") or None,
        )
    except ValidationError as error:
        raise ValueError("Please check the entered values.") from error
    return [entity]


def _bulk_entities() -> list[InputEntity]:
    """The entities of the uploaded bulk file.

    Re-checks the extension the browser also checks, then parses the
    file, so content problems - empty, no usable rows, too many rows -
    are reported up front rather than mid-search. (The size cap is
    enforced separately by MAX_CONTENT_LENGTH, which makes Flask
    return 413.)

    Raises:
        ValueError: With the message to show when the file is unusable.
    """
    upload = request.files.get("file_upload")
    filename = (upload.filename or "") if upload else ""
    if not filename:
        raise ValueError("Please attach a .xlsx or .csv file.")
    if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        raise ValueError(
            "Unsupported file type. Please upload a .xlsx or .csv file."
        )
    return parse_upload(filename, upload.read())


@app.route("/api/jobs", methods=["POST"])
def create_job():
    """Create a search job from the single form or a bulk upload.

    Validates the input and stores the entities to look up under a new
    ``job_id`` (nothing is looked up yet). Returns ``{"job_id", "total"}``,
    or 400 with ``{"error": ...}`` when the input is unusable, so the
    search page can show the message next to its Search button.
    """
    mode = "bulk" if request.form.get("mode") == "bulk" else "single"
    try:
        entities = _bulk_entities() if mode == "bulk" else _single_entities()
    except ValueError as error:
        return {"error": str(error)}, 400

    job_id = secrets.token_hex(16)
    storage.create_search(
        job_id=job_id,
        mode=mode,
        query=[_entity_input(entity) for entity in entities],
    )
    return {"job_id": job_id, "total": len(entities)}


@app.route("/api/jobs/<job_id>/run", methods=["POST"])
def run_job(job_id: str):
    """Look up the next few pending entities of a job and save them.

    Returns the job's progress (see ``_progress``); the browser keeps
    calling until ``done`` is true. A GLEIF outage saves whatever
    completed and answers 503 with an ``error`` message, so a later
    call resumes from there. Two calls racing on one job (a second tab,
    or a refresh while the previous call is still running) cannot store
    an entity twice: the store only accepts rows that continue from the
    result count this call started at.
    """
    search = storage.get_search(job_id)
    if search is None:
        return {"error": "not found"}, 404

    offset = len(search["results"])
    pending = search["query"][offset:]
    rows = []
    error = None
    started = time.monotonic()
    try:
        with GleifClient() as client:
            for record in pending[:RUN_CHUNK_SIZE]:
                entity = _entity_from_input(record)
                result, closest = lookup_entity(entity, client)
                rows.append(_result_row(entity, result, closest))
                if time.monotonic() - started > RUN_TIME_BUDGET_SECONDS:
                    break
    except GleifApiError as exc:
        logger.exception("GLEIF lookup failed: %s", exc)
        error = GLEIF_DOWN_MESSAGE

    if rows:
        # None: a rival call stored these entities first (or the job
        # expired), so the store's own progress is what we report.
        search = (
            storage.append_results(job_id, rows, offset)
            or storage.get_search(job_id)
            or search
        )
    progress = _progress(search)
    if error:
        return {"error": error, **progress}, 503
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
    it. Returns the saved decision, or 404 if the search, the index, or
    the candidate LEI is unknown.
    """
    data = request.get_json(silent=True) or {}
    saved = storage.record_decision(
        data.get("job_id", ""), data.get("index"), data.get("choice"),
    )
    if saved is None:
        return {"error": "not found"}, 404
    return {"ok": True, "decision": saved}


if __name__ == "__main__":
    app.run(port=8080)

