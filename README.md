# LEI lookup

A small web tool for finding a company's Legal Entity Identifier (LEI)
in the [GLEIF](https://www.gleif.org/) database. A user can run a
single lookup (an entity name or an ISIN, optionally with address
fields to narrow the result) or a bulk lookup by uploading an
`.xlsx`, `.csv`, `.tsv` or `.txt` file of entities. Matching is deterministic and
precision-first (no LLM): it asserts a LEI only when the name and legal
address agree, or an ISIN resolves the identity; weaker hits are
surfaced for manual review.

This tree (branches `codenow` and `main`) packages the app as a **CodeNOW Flask
component** on the blueprint scaffold of the `codenow-flask` plugin: a
`create_app()` factory in `src/app.py`, the routes on `bp_main` in
`src/main/routes.py`, `/health` for the platform probe, and waitress to
serve it. The same app, laid out for Vercel, lives on the `vercel`
branch.

## Endpoints

All routes except `/health` sit under `URL_PREFIX` (see
Configuration); the bare `/` redirects there when a prefix is set.

| Route | Purpose |
|---|---|
| `GET /` | Single + bulk lookup forms |
| `POST /api/jobs` | Create a search job (validates the input, stores the entities) |
| `POST /api/jobs/<id>/run` | Look up the next few pending entities; returns progress |
| `GET /results?job=<id>` | Results page (live progress while running, tables when done) |
| `POST /api/decision` | Record a manual match decision |
| `GET /download/csv?job=<id>` | Export a search as CSV |
| `GET /download/excel?job=<id>` | Export a search as Excel |
| `GET /admin` | Full dump of the `searches` table (internal, unlinked) |
| `GET /static/main/...` | Stylesheet, script and images |
| `GET /health` | Liveness probe, never prefixed (returns `{"status": "UP"}`) |

A search runs as short, resumable steps rather than one long request:
the search page creates a job and navigates to its results page, whose
script calls `/run` until the job reports `done`, then reloads to render
the tables. Each `/run` call looks up at most a few entities (and stops
early after a time budget), so requests stay short, and a page refresh
mid-search simply resumes.

## Project layout

```
src/
  app.py              # create_app() factory + /health (the platform serves `app`)
  config.py           # URL_PREFIX / SECRET_KEY from the environment
  core/               # backend lookup logic
    constants.py      # matcher thresholds + GLEIF settings
    models.py         # pydantic models (InputEntity, LookupResult, ...)
    address.py        # name/address normalization, country -> ISO
    matcher.py        # precision-first fuzzy name + address scoring
    gleif.py          # GLEIF API client (requests, retry/backoff)
    lookup.py         # single-entity pipeline: search -> score -> classify
    isin.py           # ISIN validation + LEI resolution/corroboration
    openfigi.py       # OpenFIGI client: ISIN -> issuer name (fallback)
    notes.py          # Czech versions of the lookup notes
    storage.py        # search store: SQLite, or Postgres (DATABASE_URL)
    export.py         # build CSV / Excel from a stored search
    upload.py         # parse an uploaded .xlsx/.csv/.tsv/.txt into entities
  main/               # the bp_main blueprint
    __init__.py       # bp_main (owns templates/, static/, data/)
    routes.py         # every route except /health
    templates/        # Jinja2 templates (base, index, results, admin)
    static/           # styles.css, app.js, img/
    data/             # committed lookup tables
      country_mapping.json   # country name (cs/en) -> ISO alpha-2
      legal_forms.txt        # legal-form suffixes stripped before matching
tests/                # pytest suite (SQLite store, faked GLEIF)
scripts/check.ps1     # local readiness check: pylint gate, routes, pytest
codenow/config/       # platform config: JSON log config, env defaults
.codenow.yaml         # CodeNOW build/runtime images, pipelines, port
.pylintrc             # keeps the build's pylint gate honest
nginx/app.conf        # from the platform scaffold
requirements.txt      # pinned dependencies (runtime, pylint, pytest)
```

## Running locally

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python src\app.py       # http://127.0.0.1:8080/ (waitress)
.venv\Scripts\python -m pytest -q
./scripts/check.ps1                    # with the venv active: must print READY TO COMMIT
```

Locally `URL_PREFIX` is unset, so the app answers at `/`. Set it (for
example `$env:URL_PREFIX = "/lei-lookup"`) to run it as deployed. The
tests pass either way.

## Configuration

Defaults are documented in `codenow/config/environment-variables`.

- **`URL_PREFIX`**: the route CodeNOW publishes the component under
  (default there: `/lei-lookup`). Every page link, asset and script
  request is built from it.
- **`LEI_DB_PATH`**: path of the SQLite search store. The image
  filesystem is read-only; unset, the store lives under the system temp
  directory and is lost on restart, so point it at a mounted volume to
  keep the history.
- **`DATABASE_URL`** (or `POSTGRES_URL`): a Postgres connection string;
  when set, the store is that database instead of SQLite.
- **`OPENFIGI_API_KEY`** (optional): raises the OpenFIGI rate limit for
  the ISIN fallback. Read from the environment, never hardcoded.
- **`SECRET_KEY`**: only needed once something uses sessions (nothing
  does today).
- **Upload cap**: 4 MB per file and 100 entities.

Logs are JSON (`codenow/config/log-config.json`, python-json-logger),
and the B3 trace headers (`X-B3-TraceId`, `X-B3-SpanId`) are echoed on
every response.

## Data and privacy

Each search is stored for 30 days in the single `searches` table (its
query and results), then pruned on the next write. The app sets no
cookies.

## Deployment (CodeNOW)

CodeNOW builds the component from its Bitbucket repository (GitHub
deploys nothing): commit to the branch the component's VCS settings
name, and the push starts the Tekton pipeline (`python-pip-app-preview`
/ `python-pip-app-release`, see `.codenow.yaml`):

1. `build`: `pip3 install -r requirements.txt`, then
   `pylint --disable=C,R,W ./src` - an `E`/`F` message fails the build.
2. `unit-test`, `static-analysis` (Sonar, `sonar-project.properties`),
   `container-build`, `push-helm`.

Run `./scripts/check.ps1` before every commit: it runs the same gate
and the tests. After a deploy, `/health` must return
`{"status": "UP"}` and the app lives under `URL_PREFIX`.

A `/run` call can take up to about two minutes when GLEIF is slow (its
deadline, `RUN_DEADLINE_SECONDS`). If the component's ingress cuts
requests off sooner, such a call ends with an error on the results
page, whose reload resumes the search: keep the ingress read timeout
above that where the platform allows.
