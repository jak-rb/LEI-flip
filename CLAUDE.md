
# CLAUDE.md

<!--
  TEMPLATE CONTRACT (do not change):
  Everything from "## How to basic" down to and including the "### Overview"
  heading is universal and identical across every project. Project repos MUST
  NOT edit it. A project may only:
    1. add content INSIDE "### Overview", and
    2. append new sections AFTER "### Overview".
-->

## How to basic

How Claude Code should work in this repository. These rules are universal and
apply before any project-specific guidance below.

### Philosophy

**Bias toward caution over speed. For trivial tasks, use judgment.**

1. **Think before coding.** Don't assume, don't hide confusion, surface
   tradeoffs. State your assumptions explicitly and ask when uncertain. If
   multiple interpretations exist, present them instead of silently picking one.
   If something is unclear, stop and name what's confusing.

2. **Simplicity first.** Write the minimum code that solves the problem, nothing
   speculative. No features beyond what was asked, no abstractions for single-use
   code, no configurability that wasn't requested, no error handling for
   impossible scenarios. If 200 lines could be 50, rewrite it. Ask: "Would a
   senior engineer call this overcomplicated?"

3. **Surgical changes.** Touch only what you must; clean up only your own mess.
   Don't improve adjacent code, don't refactor what isn't broken, and match the
   existing style even if you'd do it differently. Remove only the
   imports/variables/functions your own changes made unused; mention unrelated
   dead code rather than deleting it. Every changed line should trace directly to
   the request.

4. **Goal-driven execution.** Define success criteria, then loop until verified.
   Turn vague tasks into testable goals ("fix the bug" → "write a test that
   reproduces it, then make it pass"). For multi-step work, state a brief plan
   with a verification check per step. Strong success criteria let you iterate
   independently; weak ones ("make it work") force constant clarification.

### Technical principles

How Claude should write code, design, and structure the project.

#### Universal – every codebase

- **Names say what they mean.** Clear, descriptive names over clever or
  abbreviated ones. Match the conventions already in use.
- **Comments explain why, not what.** Match the surrounding comment density.
  Don't narrate code that speaks for itself; don't leave commented-out blocks.
- **Dependencies are a cost.** Prefer the standard library and existing
  dependencies. Ask before adding a new one.
- **Errors that can actually happen.** Handle realistic failure modes; don't
  guard against the impossible.
- **Never hardcode secrets.** No credentials, tokens, or keys in source or logs.
- **Frame every code file with blank lines.** Start and end each programming
  file with one empty line (not data files like `.csv`).
- **Verify your work.** Run the tests, linters, or the app itself before
  claiming something is done. Report failures honestly.
- **Reference code precisely.** Point to `file:line` so claims can be checked.
- **No em-dashes.** When writing prose, use a hyphen (-) or en-dash (–), never
  an em-dash (—).

#### New codebase (Python)

Apply when starting fresh, with no existing conventions to follow.

- **Naming.** `snake_case` for functions, variables, and modules; `PascalCase`
  for classes (e.g. Pydantic models and `Enum` classes); `UPPER_SNAKE_CASE` for
  module-level constants.
- **Docstrings.** Google style for modules and public functions. Private
  functions take a one-line docstring and a `_` name prefix.
- **Explicit imports.** No `from X import *`.
- **Two blank lines** between top-level function and class definitions (one
  blank line between methods), per PEP 8.
- **Early returns over deep nesting** – especially in node functions.
- **Fix typos at the source.** When you touch a symbol with a typo, rename it
  rather than propagate the mistake.
- **Line length.** Limit code lines to 79 characters. For flowing prose
  (docstrings, comments), limit to 72 characters.

#### Existing codebase

Apply when conventions are already established.

- **Match the codebase.** Follow the existing structure, naming, idioms, and
  formatting, even where they differ from the "New codebase" rules above.
  Consistency with the surrounding code beats personal preference.
- **When the conventions are unclear or inconsistent, ask** whether to apply the
  "New codebase" rules instead.

## Project

Project-specific guidance. The "### Overview" below is mandatory and written for
humans. Add detail inside it, and append any further sections (commands, stack,
structure, conventions, current state, …) after it.

### Overview

LEI lookup is a small web tool for finding a company's Legal Entity Identifier
(LEI) in the [GLEIF](https://www.gleif.org/) database. A user can either run a
single lookup (enter an entity name or an ISIN, optionally with address fields
to narrow the result) or a bulk lookup by uploading an `.xlsx`, `.csv`, `.tsv`
or `.txt` file of entities.

It is a lean rebuild based on the core of the original LEI lookup tool created
by Jakub Schrimpel: the precision-first matcher and GLEIF/ISIN resolution are
kept as-is. This branch (`codenow`) packages it as a CodeNOW Flask component
on the blueprint scaffold of the `codenow-flask` plugin, so it can run on the
bank's CodeNOW platform again, which it was first built for; the `vercel`
branch (and `main`) hold the same app laid out for Vercel, where it is live.

### Tech stack

- **Python 3.12** in the CodeNOW build and runtime images (`build.image` /
  `runtime.image` in `.codenow.yaml`: `python:3.12.6-slim-bullseye`);
  locally any 3.12+ works
- **pip** with one pinned `requirements.txt`: CodeNOW installs only that
  file, so it carries pylint, pytest and coverage too
- **Flask** for the web layer and Jinja2 templates, as a blueprint component:
  the `create_app()` factory in `src/app.py` registers `bp_main`
  (`src/main/`) under `URL_PREFIX`; **waitress** serves the module-level
  `app`
- **python-json-logger** for the JSON logs `codenow/config/log-config.json`
  configures
- stdlib **sqlite3** for the search store, or **psycopg[binary]** for
  Postgres when `DATABASE_URL` is set (see `core/storage.py`)
- **openpyxl** for reading bulk `.xlsx` uploads and writing the Excel export
- **pydantic** for the core data models (`InputEntity`, `LookupResult`, ...)
- **requests** for the synchronous HTTP calls to GLEIF and OpenFIGI
- **rapidfuzz** for fuzzy name/address scoring, and **unidecode** for
  stripping diacritics during normalization
- Plain HTML/CSS frontend with vanilla JavaScript, no framework. Static files
  live in the blueprint's `src/main/static/`, served at
  `<URL_PREFIX>/static/main/`.

### Commands

From the project root:

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python src\app.py       # waitress on http://127.0.0.1:8080/
.venv\Scripts\python -m pytest -q      # test suite (SQLite store, faked GLEIF)
.venv\Scripts\python -m pylint --disable=C,R,W src   # EXACTLY the build gate

# The plugin's readiness check (pylint gate, lint coverage, /health + /,
# pytest) calls `py`; with the venv active `py` runs the venv's Python.
.venv\Scripts\Activate.ps1; ./scripts/check.ps1   # must print READY TO COMMIT
```

Run pylint from the project root, or it never loads `.pylintrc`. The app
resolves its files from its own location (`src/main/data/`, the log config
under `codenow/config/`), so the working directory does not matter. With
no `DATABASE_URL` the store is a local SQLite file, so a fresh clone runs
with no setup. Locally `URL_PREFIX` is unset and the app answers at `/`;
set it to run as deployed - the suite passes either way (`tests/conftest.py`
sends the tests' paths under it). In Git Bash, set it with
`MSYS_NO_PATHCONV=1 MSYS2_ENV_CONV_EXCL='*'`, or Bash turns `/lei-lookup`
into a Windows path.

### Project structure

```
src/                 # everything the build lints and the platform runs
  app.py             # create_app() factory, /health, JSON logging, B3 echo
  config.py          # GlobalConstraints: URL_PREFIX, SECRET_KEY from env
  core/              # backend lookup logic (ported + simplified from the original)
    constants.py     # matcher thresholds and GLEIF settings (plain constants)
    models.py        # pydantic data models (InputEntity, GleifCandidate, ...)
    address.py       # name/address normalization and country -> ISO conversion
    matcher.py       # precision-first fuzzy name + address scoring
    gleif.py         # synchronous GLEIF API client (requests, retry/backoff)
    lookup.py        # single-entity pipeline: search -> score -> classify
    isin.py          # ISIN validation + LEI resolution/corroboration
    openfigi.py      # OpenFIGI client: ISIN -> issuer name(s) (ISIN fallback)
    notes.py         # Czech versions of the lookup notes
    storage.py       # search store: one `searches` table on SQLite or Postgres
    export.py        # build CSV / Excel from a search (reflects manual decisions)
    upload.py        # parse an uploaded .xlsx/.csv/.tsv/.txt into entities
  main/              # the bp_main blueprint
    __init__.py      # bp_main; owns templates/, static/ (at /static/main) and data/
    routes.py        # every route except /health; new routes go at the BOTTOM
    templates/       # Jinja2 templates
      base.html      # shared layout (header, Help/Report-bugs dialogs, page shell)
      index.html     # single + bulk lookup forms (submit creates a job, goes to /results)
      results.html   # /results: summary card (live while running), stepper, tables
      admin.html     # hidden /admin page: full dump of the searches table (no auth)
    static/
      styles.css     # all styling
      app.js         # vanilla JS: dialogs, forms, job runner loop, validation stepper
      img/           # RB logos (yellow-bar and black-bar variants) + theme icons
    data/            # read-only lookup tables (committed)
      country_mapping.json  # country name (cs/en) -> ISO alpha-2 code
      legal_forms.txt       # legal-form suffixes stripped before name matching
tests/               # pytest: storage, route flows with GLEIF faked, smoke tests
scripts/check.ps1    # the plugin's readiness check (copied verbatim)
codenow/config/      # config.yaml, log-config.json, environment-variables
nginx/app.conf       # from the platform scaffold; leave it alone
.codenow.yaml        # the component's build/runtime images, pipelines, port 80
.pylintrc            # puts src/ on pylint's path (insurance for the build gate)
sonar-project.properties  # coverage report path for the static-analysis stage
.run/                # PyCharm run config + mirrord config (from the component)
requirements.txt     # pinned dependencies, runtime + pylint + pytest
docs/                # project docs (present on disk, gitignored)
```

### Backend

**A search is a job run in short steps.** The app was rebuilt for Vercel,
which runs it as a function with a hard time limit, so the old single
streaming request that looked up a whole bulk file is gone; on CodeNOW the
short steps keep requests short behind the ingress and let a reload resume
a search. Instead:

1. `POST /api/jobs` validates the input (single form fields, or an uploaded
   `.xlsx`/`.csv`/`.tsv`/`.txt` parsed by `core/upload.py` - positional
   columns, 100-entity cap; a `.csv` detects comma, semicolon or tab, a
   `.tsv`/`.txt` is always tab-separated; an `.xlsx` whose rows are whole
   semicolon lines in column A - a semicolon CSV Excel opened with the
   wrong delimiter - is rebuilt and split at the semicolons) and stores
   the entities to look up under a new `job_id` via
   `core/storage.create_search`. Nothing is looked up yet. Unusable input
   returns 400 with an `error` message the search page shows next to its
   Search button (this replaced the separate pre-flight endpoint).
   Upload rules: blank rows (also cells of only whitespace or invisible
   format characters, `core.models.is_blank`) are skipped, and the first
   non-blank row is dropped when it holds column labels (Name/ISIN/Země/PSČ,
   PARTY_FULL_NAME/ISIN_IDENT, "ISIN (optional)", ...;
   `core/upload._is_header`): never when its ISIN is valid or it has at
   least as many data-looking cells (a digit, a country name or code) as
   labels, so "Party City,,US,..." stays data; a numbered label
   ("Address 1", "ADDR_LINE_2", `core/upload._is_numbered_label`) counts
   as neither label nor data, so "Firma 1" still starts a list; a
   one-cell title row above a header is dropped with it. A row with a value over its
   `InputEntity` length limit refuses the whole file, naming the row (as
   numbered in the file), the field and the limit - rows are never dropped
   or truncated silently. Parsing stops at the 101st entity row ("more
   than 100"). An `.xlsx` is read with `reset_dimensions()` and at most 6
   columns (50 in the semicolon-lines mode), through a guarded zip archive
   that charges every read to a budget (50 MB unpacked, 1,000,000 XML
   nodes: room for some 500,000 shared strings from other sheets, about
   5 s) and refuses DTDs, so a small crafted file cannot tie the server
   up (the slowest crafted file is refused in about 9 s locally);
   `core/upload._load_workbook` relies on openpyxl 3.1.5 internals
   (`ExcelReader.archive`), so re-check it when upgrading openpyxl.
   Cells are read as Excel shows them: `_xHHHH_` escapes are decoded, and
   a numeric postal code with a zeros-only number format keeps its leading
   zeros (1001 as "000 00" -> "010 01"). The semicolon-lines mode is
   chosen on the rebuilt lines (cells joined with commas), so names with
   commas ("ČEZ, a. s.") work, while a plain sheet whose names hold ';'
   keeps its columns. Text files: UTF-16 by BOM, then UTF-8; a file that
   is mostly valid UTF-8 keeps UTF-8 with U+FFFD for the bad bytes, else
   cp1250 (undefined bytes become U+FFFD). The extension is the part from
   the last dot, so a file named just ".csv" is read.
2. The browser navigates to `/results?job=<id>`. For an unfinished job the
   page renders the summary card in its running state and
   `src/main/static/app.js`
   calls `POST /api/jobs/<id>/run` in a loop.
3. Each `/run` call looks up the next pending entities (at most
   `RUN_CHUNK_SIZE`, stopping early once `RUN_TIME_BUDGET_SECONDS` have
   passed) against live GLEIF with `core/lookup.py`, appends their result rows
   with `storage.append_results`, and returns the progress: `searched`,
   `total`, the matched / need-validation / unmatched counts, and `done`.
   The budget only stops a call from starting another lookup; the GLEIF
   client's deadline (`GleifClient.deadline` = start +
   `RUN_DEADLINE_SECONDS`, 120 s, also passed to OpenFIGI) bounds the one
   under way: no request or retry starts after it and each timeout
   (`REQUEST_TIMEOUT`, 10 s) is cut to the time left. A lookup cut off by
   the deadline is not stored; the next call starts it again.
   A GLEIF outage saves whatever completed and answers 503 with
   `error`/`error_cs`; the card shows it and a reload resumes from the
   saved progress. An outage is a connection error or timeout, or a GLEIF
   error that a one-record probe search (`GleifClient.is_answering`) gets
   too: then nothing is stored and no attempt is counted. While GLEIF
   answers the probe, the error is the entity's: a query GLEIF refuses (a
   4xx other than 408/425/429) is stored at once as a NO_MATCH row whose
   note starts "Lookup failed", and repeated server errors (5xx, 408, 425,
   a non-JSON 200) or a lookup too slow for a whole call of its own after
   `RUN_MAX_ATTEMPTS` (3) calls; an unexpected error, or a stored record
   that no longer passes the input rules, is failed at once - so a job
   always finishes. When GLEIF rate-limits (429) for longer than the call
   has left, the reply is 200 with the progress plus `throttled` and
   `retry_after`, and the page counts the wait down (2-30 s) and continues
   by itself; a cut-off counts as throttled when the lookup's own
   rate-limit waits (`GleifClient.rate_limit_waits`, reset per lookup)
   left it less than `RUN_DEADLINE_SECONDS - RUN_TIME_BUDGET_SECONDS` of
   its own time. requests' timeout bounds each socket read, not a whole
   reply, so GLEIF and OpenFIGI replies are streamed and read one socket
   read at a time (`core/gleif.read_body`): a reply that trickles in stops
   at most one request timeout past the deadline.
4. When `done` the page reloads and the server renders the detailed tables.

Each stored result row is the entity's `input`, its `match` (a `LookupResult`)
and up to 3 `closest` candidates for manual review
(`core/lookup.CLOSEST_CANDIDATE_LIMIT`). ISIN resolution is unchanged from the
original: when name+address yields no confident match, a validated ISIN
(`core/isin.py`) can find a LEI directly, corroborate a near-miss, or - as a
last resort - be resolved to an issuer name via OpenFIGI (`core/openfigi.py`)
and re-searched; an ISIN-only input is resolved by GLEIF's authoritative ISIN
mapping, with an OpenFIGI review fallback.

**The store** (`core/storage.py`) is one `searches` table: `job_id`,
`created_at` (ISO-8601 UTC text), `mode`, `searched` (entities looked up so
far), `found` (asserted matches so far), `query` (the entities, JSON; a
record may carry `failed_attempts`, counted by
`storage.record_failed_attempt`) and `results` (the rows, JSON). Rows older
than 30 days are pruned when a search is created. Job ids are 32 lowercase
hex characters (`secrets.token_hex(16)`): `get_search`, `append_results` and
`record_decision` return None for any other id without querying (a NUL made
Postgres raise), and `append_results` never stores rows past the job's
entities.
The backend is chosen at import from the environment: `DATABASE_URL` (or
`POSTGRES_URL`) selects Postgres through psycopg, otherwise SQLite at
`LEI_DB_PATH` or under the system temp dir. Both share one DDL and one set of
`%s`-placeholder statements (rewritten to `?` for SQLite); the schema is
created lazily on first use, once per process.

**The `/results` page, decisions and downloads** work as before: a summary
card with the four counts (Searched `x / total`, Matched, Need validation,
Unmatched), the orange validation stepper (top 3 candidates per near-miss,
confirm one or "None of these", saved via `POST /api/decision` ->
`storage.record_decision`), the matched and no-match tables, and the CSV /
Excel downloads built by `core/export.py` (a confirmed pick exports as
`MANUAL_MATCH`, a rejection as `MANUAL_NO_MATCH`; characters XML forbids are
dropped - those that read as whitespace (VT, FF, FS-US) become a space - and
formula-like cells neutralised; notes stay English). A decision is accepted only
once the job is finished, and only on a row the stepper offers (candidates,
no algorithmic match); otherwise the reply is 404, and a body that is not
`{job_id: str, index: int, choice: str}` gets a 400 with `error`/`error_cs`.
`record_decision` writes with a compare-and-swap on the stored results
(`UPDATE ... WHERE results = <the text it read>`, retried on a miss), so
simultaneous decisions all persist; on SQLite each attempt first takes the
write lock (`BEGIN IMMEDIATE`, `storage._Connection.begin_write`, also used
by `record_failed_attempt`), and on Postgres the losing UPDATE waits on the
row lock and re-checks its WHERE. While a save is pending the stepper
disables its decision buttons; after it, the card's counts update from the
reply's `counts`, and a failed save shows a bilingual message. The results
page shows lookup notes in both languages (`core/notes.czech_note`). The overall percent shown
per candidate is display-only (`core/lookup._overall_match`) and never gates
a match.

### Frontend

- **Branding.** Raiffeisenbank brand palette only (rules in the header
  comment of `src/main/static/styles.css`): one yellow, Off Black, Warm Grey
  neutrals, system font stack, no webfonts or CDN assets.
- **Theme.** Light/dark lives on `<html data-theme>`. An inline script
  in `src/main/templates/base.html` resolves it before first paint: the explicit
  choice in `localStorage["leiTheme"]`, else the OS colour scheme.
- **Language (CZ/EN).** Lives on `<html lang>` (`localStorage["leiLang"]`,
  default English). Server-rendered text carries both versions in
  `data-cs` / `data-en` (for attributes: `data-cs-placeholder`,
  `data-cs-title`, `data-cs-aria-label`); `applyLang()` in
  `src/main/static/app.js` swaps them, and rich text uses `.only-cs` / `.only-en`
  blocks. Strings the script writes itself live in its `STRINGS` table.
  Error messages in the server's JSON replies come as `error` (English)
  and `error_cs` (Czech): raise `core.models.InputError(english, czech)`
  for input the user must fix, and the script's `serverError()` shows
  the one for the current language.
  Every new user-visible string needs both languages.
- **URLs.** Every link, asset and form target is built with `url_for`
  (endpoints are `main.<name>`, assets `main.static`), since the app lives
  under `URL_PREFIX`. The page script builds its requests with `appUrl()`
  from the root the server renders into `<html data-app-root>`; a literal
  path starting with "/" in `app.js` would escape the prefix
  (`tests/test_smoke.py` checks).

### Deployment (CodeNOW)

- CodeNOW builds the component from its **Bitbucket** repository; GitHub
  deploys nothing. Commit to the branch the component's VCS settings name.
  The push starts the Tekton pipeline from `.codenow.yaml`
  (`python-pip-app-preview` / `python-pip-app-release`): `build` runs
  `pip3 install -r requirements.txt` then `pylint --disable=C,R,W ./src`
  (any `E`/`F` fails it and every later stage shows Skipped), then
  `unit-test` -> `static-analysis` (Sonar) -> `container-build` ->
  `push-helm`.
- `.codenow.yaml` is the component's own copy (restored from the imported
  Bitbucket history, commit 6d4d53c), not a reconstruction: build and
  runtime image `python:3.12.6-slim-bullseye`, `runtime.port` 80, external
  endpoint enabled. Advanced mode is off, so the Dockerfile and helm chart
  are the platform's. Check `build.image` before using syntax newer than
  3.12.
- `/health` is the liveness probe: on the bare app, never under the
  prefix, and dependency free (`tests/test_smoke.py` fails if it opens the
  store or if importing the app does store I/O). A failing probe keeps the
  previous revision live, which looks like "my deploy did nothing".
- `URL_PREFIX` (default `/lei-lookup` in
  `codenow/config/environment-variables`) must match the route the
  platform publishes the component under; the bare `/` redirects there.
  Other variables: `LEI_DB_PATH` (a mounted volume, as the image
  filesystem is read-only; unset, the SQLite store lives in the temp dir
  and is lost on restart), `DATABASE_URL` for Postgres instead, optional
  `OPENFIGI_API_KEY`, `SECRET_KEY` (unused today).
- A `/run` call can take up to about `RUN_DEADLINE_SECONDS` (120 s) when
  GLEIF is slow. An ingress that cuts requests off sooner makes that call
  end with the results page's "reload to resume" error; what the call had
  finished is still stored, so a reload continues.
- Upload cap: 4 MB (`MAX_UPLOAD_BYTES`, the app's `MAX_CONTENT_LENGTH`)
  and 100 entities.
- Restored with the move back to CodeNOW: `.codenow.yaml`, the JSON log
  config, the B3 trace-header echo, waitress, the PyCharm/mirrord run
  configs and the Sonar properties file. Dropped: `vercel.json`,
  `.vercelignore`, `.python-version`, `requirements-dev.txt`, and
  `public/` (now the blueprint's `static/`).

### Current state

The 2026-09-23 test run's backlog (the former `HANDOFF.md`) is done: every
confirmed defect was fixed test-first, re-verified by an adversarial pass,
and shipped. Its four open questions were settled on 2026-09-24: the
`.xlsx` node budget doubled, numbered header labels, streamed reply
reads, and any whitespace between a multi-word legal form's words
(`core/address._load_legal_forms`, so "s.  r. o." or a no-break space
strips like "s. r. o."). That last one touches matching; instead of a
live matcher audit it was checked offline: old and new `normalize_name`
agree on all 26,220 names of the old tool's GLEIF cache, golden and
precision corpora, and a tab or no-break-space copy of each now
normalizes like the original, as does a doubled-space copy of all but 4
of 4,859. Those 4 are left as is: a trailing parenthetical is only
stripped up to 20 characters, and doubled spaces inside one push it over.

Feature-complete and deployable. Single and bulk search run against live
GLEIF through the job endpoints, every search is persisted under a `job_id`
(SQLite, or Postgres when `DATABASE_URL` is set), and the results page, the
manual
validation workflow and the CSV/Excel downloads are real. `tests/` covers the
store and the route flows with GLEIF faked; run it before every change to the
web layer. Matching behaviour (thresholds in `core/constants.py`) is
audit-validated and unchanged from the CodeNOW version (apart from the
legal-form whitespace above) - re-run the matcher audit before tuning it.

The `codenow` branch was converted on 2026-09-24 and verified locally (the
pylint gate, `scripts/check.ps1`, the suite with and without `URL_PREFIX`,
waitress runs); it has not been built on CodeNOW yet, because deploying
means pushing it to the component's Bitbucket repo.

An optional LLM-assisted step (e.g. helping disambiguate near-misses) is a
possible next addition; it would plug in after `core/lookup.lookup_entity`
returns and must never override the precision-first assertion rules.
