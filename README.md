# LEI lookup

A small internal web tool for finding a company's Legal Entity Identifier
(LEI) in the [GLEIF](https://www.gleif.org/) database. A user can run a
single lookup (an entity name or an ISIN, optionally with address fields
to narrow the result) or a bulk lookup by uploading an `.xlsx`/`.csv`
file of entities. Matching is deterministic and precision-first (no LLM):
it asserts a LEI only when the name and legal address agree, or an ISIN
resolves the identity; weaker hits are surfaced for manual review.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /` | Single + bulk lookup forms |
| `GET /results?job=<id>` | Detailed results for a stored search |
| `POST /api/search` | Run a lookup; streams NDJSON progress events |
| `POST /api/validate-upload` | Pre-flight check of a bulk file |
| `POST /api/decision` | Record a manual match decision |
| `GET /download/csv?job=<id>` | Export a search as CSV |
| `GET /download/excel?job=<id>` | Export a search as Excel |
| `GET /admin` | Full dump of the `searches` table (internal, unlinked) |
| `GET /health` | Liveness/readiness probe (returns `{"status": "UP"}`) |

## Project layout

```
src/
  app.py            # Flask app + routes (entry point)
  core/             # backend lookup logic
    constants.py    # matcher thresholds + GLEIF settings
    models.py       # pydantic models (InputEntity, LookupResult, ...)
    address.py      # name/address normalization, country -> ISO
    matcher.py      # precision-first fuzzy name + address scoring
    gleif.py        # GLEIF API client (requests, retry/backoff)
    lookup.py       # single-entity pipeline: search -> score -> classify
    isin.py         # ISIN validation + LEI resolution/corroboration
    openfigi.py     # OpenFIGI client: ISIN -> issuer name (fallback)
    storage.py      # SQLite store: one `searches` table
    export.py       # build CSV / Excel from a stored search
    upload.py       # parse an uploaded .xlsx/.csv into entities
  data/             # committed lookup tables + runtime SQLite store
    country_mapping.json   # country name (cs/en) -> ISO alpha-2
    legal_forms.txt        # legal-form suffixes stripped before matching
    lei_lookup.db          # runtime store (created on first run; gitignored)
  templates/        # Jinja2 templates (base, index, results, admin)
  static/           # styles.css, app.js
codenow/config/     # platform config, incl. JSON log config
requirements.txt    # pip dependencies
```

## Running locally

From the repository root, create a virtual environment and install the
dependencies, then start the app from `src/`:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

cd src
python app.py                 # dev server on http://localhost:8080
```

For a production-style run, use the bundled waitress server instead of
Flask's dev server (run from `src/`, where `app:app` is importable):

```bash
cd src
waitress-serve --port=8080 app:app
```

## Configuration

- **Logging.** Structured JSON logging is configured at startup from
  `codenow/config/log-config.json` (python-json-logger), so container
  output is parseable by the platform's log aggregation.
- **Database location (`LEI_DB_PATH`).** The SQLite store must live on a
  writable filesystem. Set `LEI_DB_PATH` to the file path for the store -
  on a container platform, a mounted, ideally persistent volume. If unset,
  it defaults to a `lei-lookup/` folder under the system temp directory,
  which keeps the app running but loses history on restart. It is never
  written into the read-only application directory.
- **OpenFIGI (optional).** The ISIN fallback works without a key. Set the
  `OPENFIGI_API_KEY` environment variable to use a higher rate limit; it
  is read from the environment and never hardcoded.
- **Upload cap.** Bulk uploads are limited to 5 MB and 100 entities.

## Data and privacy

Each search is stored for 30 days in the single `searches` table of the
SQLite store (its query and results), then pruned. The store lives at
`LEI_DB_PATH` (see Configuration), defaulting to the system temp
directory. On a container platform, point `LEI_DB_PATH` at a mounted
persistent volume if the history needs to survive redeploys. The app sets
no cookies.

## Deployment (CodeNow)

The repository follows the CodeNow Python pip-app layout (see
`.codenow.yaml`): the release/preview pipelines build and run the service,
exposing it on the configured port. `GET /health` is the probe endpoint
(excluded from mirrord traffic stealing). The lookup pipeline is
synchronous and network-bound (blocking GLEIF calls), and progress is a
streamed NDJSON response, so disable response buffering on any proxy in
front so events reach the browser as they happen.

The image filesystem is read-only, so the SQLite store must not be written
into the application directory (doing so crashes startup with
`unable to open database file`). Set `LEI_DB_PATH` to a writable path -
a mounted persistent volume to keep the 30-day history across redeploys, or
leave it unset to use the ephemeral system temp directory.
