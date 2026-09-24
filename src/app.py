
"""Application entry point - LEI lookup as a CodeNOW Flask component.

The platform imports this module and serves ``app`` with waitress.
Anything that fails at import time crash-loops the pod before /health
can answer, so nothing here touches the network or the search store
(its schema is created on first use); the only file read is the
bundled log config, with a plain-text fallback.

Local:   py src/app.py                  # http://127.0.0.1:8080/
CodeNOW: waitress serves this app on the port from .codenow.yaml.
"""

import json
import logging
import logging.config
import os

from flask import Flask, Request, Response, jsonify, redirect, request
from config import GlobalConstraints
from core.notes import czech_note
from main.routes import MAX_UPLOAD_BYTES, bp_main

# URL prefix - the external route the component is published under.
# Set via URL_PREFIX (see codenow/config/environment-variables).
url_prefix = GlobalConstraints.GC_URL_PREFIX

# Structured JSON logging, so the platform's log aggregation can parse
# the container output. Anchored to this file, not the working
# directory.
_LOG_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "codenow", "config", "log-config.json",
)


class _UploadCapRequest(Request):
    """Flask's request, with form fields allowed up to the upload cap.

    Werkzeug refuses a form field (or a urlencoded body) over 500 kB
    with a 413 far below MAX_UPLOAD_BYTES, and Flask 3.0 has no config
    key for that limit. Werkzeug's cap of 1000 form parts stays: the
    page sends at most seven, so only a crafted request gets past it.
    """

    max_form_memory_size = MAX_UPLOAD_BYTES


def _configure_logging():
    """Log as codenow/config/log-config.json says, else as plain text."""
    try:
        with open(_LOG_CONFIG_PATH, encoding="utf-8") as config_file:
            logging.config.dictConfig(json.load(config_file))
    except (OSError, ValueError):
        # A missing or unusable config must not stop the app starting.
        logging.basicConfig(
            level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
        )


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(config_override=None):
    """Create and return a configured Flask application.

    Parameters
    ----------
    config_override:
        Optional dict merged into ``app.config`` (useful in tests).
    """
    flask_app = Flask(__name__)

    flask_app.config["SECRET_KEY"] = GlobalConstraints.GC_SECRET_KEY
    # Reject any request body larger than this. Flask raises HTTP 413
    # before the route runs, so an oversized upload is refused without
    # being read into memory.
    flask_app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    flask_app.request_class = _UploadCapRequest
    if config_override:
        flask_app.config.update(config_override)

    # Render a missing value as an empty cell rather than the text
    # "None": many stored fields (a candidate's street, an ISIN-only
    # input's country) are legitimately null.
    flask_app.jinja_env.finalize = lambda value: "" if value is None else value
    # The results page shows each lookup note in English and in Czech.
    flask_app.jinja_env.filters["czech_note"] = czech_note

    flask_app.register_blueprint(bp_main, url_prefix=url_prefix)

    if url_prefix:
        @flask_app.route("/")
        def root_redirect():
            """Send the bare component root to the prefixed app."""
            return redirect(url_prefix.rstrip("/") + "/", code=302)

    @flask_app.after_request
    def propagate_trace_headers(response: Response) -> Response:
        """Echo the B3 distributed-tracing ids back on every response.

        CodeNOW propagates trace context via X-B3-* headers; reflecting
        the trace and span ids lets the platform correlate this
        service's responses with the incoming request span.
        """
        for header in ("X-B3-TraceId", "X-B3-SpanId"):
            value = request.headers.get(header)
            if value:
                response.headers[header] = value
        return response

    # Add application routes to main/routes.py on bp_main - NOT here.

    # This route is required by CodeNOW: it is the liveness probe.
    # Keep it on the bare app (never under the prefix) and keep it
    # dependency free - a probe that waits on a database makes the
    # platform restart a healthy app, or keep serving the previous
    # revision.
    @flask_app.route("/health")
    def health_check():
        return jsonify(status="UP"), 200

    return flask_app


# ---------------------------------------------------------------------------
# Module-level app - this is what the platform serves
# ---------------------------------------------------------------------------

_configure_logging()
app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        app.run(host="0.0.0.0", port=port)

