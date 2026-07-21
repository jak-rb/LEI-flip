
import json
import logging.config
import secrets
from pathlib import Path

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

app = Flask(__name__)

# Configure structured JSON logging from the CodeNow log config, so the
# platform's log aggregation can parse container output. Anchored to this
# file, so it loads regardless of the working directory.
_LOG_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "codenow" / "config" / "log-config.json"
)
with open(_LOG_CONFIG_PATH, "rt", encoding="utf-8") as _log_config_file:
    logging.config.dictConfig(json.load(_log_config_file))

logger = logging.getLogger(__name__)

# Reject any request body larger than this. Flask raises HTTP 413
# before the route runs, so an oversized upload is refused without being
# read into memory. We only ever accept one file, so this effectively
# caps the file size too.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# Allowed bulk-upload extensions. The browser checks this too, but that
# is only a convenience: a request can reach the server without going
# through our JavaScript, so the server must enforce the rule itself.
ALLOWED_UPLOAD_EXTENSIONS = (".xlsx", ".csv")

# Create the results table on startup if it is missing.
storage.init_db()


@app.after_request
def _propagate_trace_headers(response: Response) -> Response:
    """Echo the B3 distributed-tracing ids back on every response.

    CodeNow propagates trace context via X-B3-* headers; reflecting the
    trace and span ids lets the platform correlate this service's
    responses with the incoming request span.
    """
    for header in ("X-B3-TraceId", "X-B3-SpanId"):
        value = request.headers.get(header)
        if value:
            response.headers[header] = value
    return response


@app.route("/health")
def health():
    """Liveness/readiness probe for the platform."""
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

def _event(payload: dict) -> str:
    """Serialize one NDJSON progress event (a JSON object plus newline)."""
    return json.dumps(payload) + "\n"


def _entity_input(entity: InputEntity) -> dict:
    """The stored 'input' record for one looked-up entity."""
    return {
        "name": entity.name,
        "isin": entity.isin,
        "country": entity.country,
        "city": entity.town,
        "street": entity.street,
        "postal_code": entity.zip_code,
    }


def _result_row(entity, result, closest) -> dict:
    """Build one stored result row: input, matched result, closest."""
    return {
        "input": _entity_input(entity),
        "match": result.model_dump(),
        "closest": [candidate.model_dump() for candidate in closest],
    }


@app.route("/api/search", methods=["post"])
def search():
    """Run a lookup and stream NDJSON progress events.

    Single and bulk modes both run the real GLEIF pipeline and stream
    newline-delimited JSON: per-entity {"searched", "total", "found"}
    lines, a final line carrying "confidence", "job_id", and "done", or a
    single {"error": ...} line on failure.
    """
    if request.form.get("mode", "single") == "bulk":
        return _bulk_search()
    return _single_search()


def _upload_error(filename: str, content: bytes) -> str | None:
    """Validate a bulk upload; return an error message, or None if OK.

    Re-checks the extension the browser also checks (a request can reach
    the server without our JavaScript) and then parses the file, so
    content problems - empty, no usable rows, too many rows - are caught
    up front rather than mid-search.
    """
    if not filename:
        return "Please attach a .xlsx or .csv file."
    if not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        return "Unsupported file type. Please upload a .xlsx or .csv file."
    try:
        parse_upload(filename, content)
    except ValueError as error:
        return str(error)
    return None


@app.route("/api/validate-upload", methods=["post"])
def validate_upload():
    """Pre-flight a bulk file so the index page can flag problems early.

    Lets the bulk form show a file error (wrong type, empty, no usable
    rows, too many rows) next to its Search button before navigating to
    the results page. Returns {"ok": true} or {"ok": false, "error": ...}.
    """
    upload = request.files.get("file_upload")
    filename = (upload.filename or "") if upload else ""
    content = upload.read() if upload else b""
    error = _upload_error(filename, content)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True}


def _single_search() -> Response:
    """Stream a real single-entity lookup and store its result."""
    # Read the form here (not inside the generator): the request context
    # may be gone by the time the streamed body is consumed.
    name = request.form.get("entity_name", "").strip()
    isin = request.form.get("isin") or None
    street = request.form.get("street") or None
    town = request.form.get("city") or None
    country = request.form.get("country") or None
    zip_code = request.form.get("postal_code") or None

    def _lookup_events():
        if not name and not isin:
            yield _event({
                "error": "Please enter an entity name or an ISIN.",
            })
            return
        try:
            entity = InputEntity(
                name=name, isin=isin, street=street, town=town,
                country=country, zip_code=zip_code,
            )
        except ValidationError:
            yield _event({"error": "Please check the entered values."})
            return

        try:
            with GleifClient() as client:
                result, closest = lookup_entity(entity, client)
        except GleifApiError as error:
            logger.exception("GLEIF lookup failed: %s", error)
            yield _event({
                "error": "GLEIF service is unavailable. "
                         "Please try again later.",
            })
            return

        # Every searched entity falls into exactly one bucket: an
        # asserted match, a near-miss to validate, or an outright miss.
        if result.lei:
            matched, need_validation, unmatched = 1, 0, 0
        elif closest:
            matched, need_validation, unmatched = 0, 1, 0
        else:
            matched, need_validation, unmatched = 0, 0, 1
        job_id = secrets.token_hex(16)
        rows = [_result_row(entity, result, closest)]
        storage.record_search(
            job_id=job_id, mode="single", searched=1, found=matched,
            query=[_entity_input(entity)], results=rows,
        )
        yield _event({
            "searched": 1, "total": 1, "matched": matched,
            "need_validation": need_validation, "unmatched": unmatched,
            "job_id": job_id, "done": True,
        })

    def generate():
        # Guard the stream: log any unexpected failure (e.g. a database
        # write error) and end with a clean error event for the browser.
        try:
            yield from _lookup_events()
        except Exception:
            logger.exception("Unexpected error during single search")
            yield _event({
                "error": "An unexpected error occurred. "
                         "Please try again later.",
            })

    return Response(generate(), mimetype="application/x-ndjson")


def _bulk_search() -> Response:
    """Parse the upload, look up each entity, and stream progress."""
    # Read the upload here (not inside the generator): the request
    # context may be gone by the time the streamed body is consumed.
    upload = request.files.get("file_upload")
    filename = (upload.filename or "") if upload else ""
    content = upload.read() if upload else b""

    # Server-side upload validation. The browser restricts the picker
    # to one .xlsx/.csv file, but that check lives in JavaScript and
    # can be bypassed, so re-validate here. (The size cap is enforced
    # separately by MAX_CONTENT_LENGTH, which makes Flask return 413.)
    upload_error = None
    if not filename:
        upload_error = "Please attach a .xlsx or .csv file."
    elif not filename.lower().endswith(ALLOWED_UPLOAD_EXTENSIONS):
        upload_error = (
            "Unsupported file type. Please upload a .xlsx or .csv file."
        )

    def _lookup_events():
        if upload_error:
            yield _event({"error": upload_error})
            return
        try:
            entities = parse_upload(filename, content)
        except ValueError as error:
            yield _event({"error": str(error)})
            return

        total = len(entities)
        rows = []
        matched = need_validation = unmatched = 0
        try:
            with GleifClient() as client:
                for index, entity in enumerate(entities, start=1):
                    result, closest = lookup_entity(entity, client)
                    # One bucket per entity (matched / to validate / miss).
                    if result.lei:
                        matched += 1
                    elif closest:
                        need_validation += 1
                    else:
                        unmatched += 1
                    rows.append(_result_row(entity, result, closest))
                    yield _event({
                        "searched": index, "total": total,
                        "matched": matched,
                        "need_validation": need_validation,
                        "unmatched": unmatched,
                    })
        except GleifApiError as error:
            logger.exception("GLEIF lookup failed: %s", error)
            yield _event({
                "error": "GLEIF service is unavailable. "
                         "Please try again later.",
            })
            return

        job_id = secrets.token_hex(16)
        storage.record_search(
            job_id=job_id, mode="bulk", searched=total, found=matched,
            query=[_entity_input(e) for e in entities], results=rows,
        )
        yield _event({
            "searched": total, "total": total, "matched": matched,
            "need_validation": need_validation, "unmatched": unmatched,
            "job_id": job_id, "done": True,
        })

    def generate():
        # Guard the stream: log any unexpected failure (e.g. a database
        # write error) and end with a clean error event for the browser.
        try:
            yield from _lookup_events()
        except Exception:
            logger.exception("Unexpected error during bulk search")
            yield _event({
                "error": "An unexpected error occurred. "
                         "Please try again later.",
            })

    return Response(generate(), mimetype="application/x-ndjson")

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
    """Render the detailed results page for a stored search.

    Reads the ``?job=`` query parameter and loads that search from the
    store. An unknown or expired id renders an empty-state message;
    otherwise the rows are partitioned into the matched / no-match /
    to-validate groups the page's three zones render.
    """
    job_id = request.args.get("job", "")
    search = storage.get_search(job_id) if job_id else None
    groups = _partition(search["results"]) if search else None
    return render_template("results.html", search=search, groups=groups)

@app.route("/api/decision", methods=["post"])
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

