# LEI lookup

A small web tool for finding a company's Legal Entity Identifier (LEI)
in the [GLEIF](https://www.gleif.org/) database. A user can run a
single lookup (an entity name or an ISIN, optionally with address
fields to narrow the result) or a bulk lookup by uploading an
`.xlsx`/`.csv` file of entities. Matching is deterministic and
precision-first (no LLM): it asserts a LEI only when the name and legal
address agree, or an ISIN resolves the identity; weaker hits are
surfaced for manual review.

The app is a Flask application deployed on [Vercel](https://vercel.com)
(Python runtime) with a Neon Postgres store for the 30-day search
history. It runs locally with no setup, falling back to SQLite.

## Endpoints

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
| `GET /health` | Liveness probe (returns `{"status": "UP"}`) |

A search runs as short, resumable steps rather than one long request:
the search page creates a job and navigates to its results page, whose
script calls `/run` until the job reports `done`, then reloads to render
the tables. Each `/run` call looks up at most a few entities (and stops
early after a time budget), so no request comes near Vercel's function
limit, and a page refresh mid-search simply resumes.

## Project layout

```
app.py              # Flask app + routes (Vercel entrypoint: exposes `app`)
core/               # backend lookup logic
  constants.py      # matcher thresholds + GLEIF settings
  models.py         # pydantic models (InputEntity, LookupResult, ...)
  address.py        # name/address normalization, country -> ISO
  matcher.py        # precision-first fuzzy name + address scoring
  gleif.py          # GLEIF API client (requests, retry/backoff)
  lookup.py         # single-entity pipeline: search -> score -> classify
  isin.py           # ISIN validation + LEI resolution/corroboration
  openfigi.py       # OpenFIGI client: ISIN -> issuer name (fallback)
  storage.py        # search store: Postgres (DATABASE_URL) or SQLite
  export.py         # build CSV / Excel from a stored search
  upload.py         # parse an uploaded .xlsx/.csv into entities
data/               # committed lookup tables
  country_mapping.json   # country name (cs/en) -> ISO alpha-2
  legal_forms.txt        # legal-form suffixes stripped before matching
templates/          # Jinja2 templates (base, index, results, admin)
public/             # styles.css, app.js (served by Vercel's CDN at /)
tests/              # pytest suite (SQLite store, faked GLEIF)
requirements.txt    # runtime dependencies
requirements-dev.txt
vercel.json         # function config (maxDuration, excluded files)
.python-version     # Python 3.12 (Vercel runtime)
```

## Running locally

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt   # Windows
# source .venv/bin/activate && pip install -r requirements-dev.txt  # Linux/macOS

.venv/Scripts/python app.py      # http://localhost:8080, SQLite store
.venv/Scripts/python -m pytest -q
```

Without `DATABASE_URL` the store is a SQLite file (`LEI_DB_PATH`, or a
`lei-lookup/` folder under the system temp directory). To run against
the real Postgres store, export `DATABASE_URL` in the shell first (for
example the value from `vercel env pull .env.local`).

## Configuration

- **`DATABASE_URL`** (or `POSTGRES_URL`): Postgres connection string
  for the search store. Set automatically on Vercel by the Neon
  integration. Unset locally means SQLite.
- **`LEI_DB_PATH`**: path of the local SQLite file (SQLite mode only).
- **`OPENFIGI_API_KEY`** (optional): raises the OpenFIGI rate limit for
  the ISIN fallback. Read from the environment, never hardcoded.
- **Upload cap**: 4 MB per file (Vercel's request body limit is
  4.5 MB) and 100 entities.

## Data and privacy

Each search is stored for 30 days in the single `searches` table (its
query and results), then pruned on the next write. The app sets no
cookies.

## Deployment (Vercel)

Vercel detects the Flask app from `app.py` (zero configuration) and
runs it as one Python function; `vercel.json` sets the function's
`maxDuration` to 300 s and keeps tests and the virtual environment out
of the bundle. Static files are served from `public/` by the CDN.

```bash
vercel link              # once, picks/creates the Vercel project
vercel deploy            # preview deployment
vercel deploy --prod     # production
```

The search store is a Neon Postgres database added to the project from
the Vercel Marketplace (Storage tab); its integration injects
`DATABASE_URL` into the deployment. The table is created on first use.
