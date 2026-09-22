
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
to narrow the result) or a bulk lookup by uploading an `.xlsx` or `.csv` file
of entities.

It is a lean rebuild based on the core of the original LEI lookup tool created
by Jakub Schrimpel: the precision-first matcher and GLEIF/ISIN resolution are
kept as-is, and the app now lives on Vercel (Python runtime, Neon Postgres)
with no dependency on the bank's CodeNOW platform, which it was first built
for.

### Tech stack

- **Python 3.12** on Vercel's Python runtime (pinned in `.python-version`;
  locally any 3.12+ works)
- **pip** with a pinned `requirements.txt` (runtime) and
  `requirements-dev.txt` (adds pytest)
- **Flask** for the web layer and Jinja2 templates; Vercel loads the `app`
  instance from `app.py` with zero configuration
- **psycopg[binary]** for the Postgres (Neon) search store on Vercel; the
  same module falls back to stdlib **sqlite3** locally (see `core/storage.py`)
- **openpyxl** for reading bulk `.xlsx` uploads and writing the Excel export
- **pydantic** for the core data models (`InputEntity`, `LookupResult`, ...)
- **requests** for the synchronous HTTP calls to GLEIF and OpenFIGI
- **rapidfuzz** for fuzzy name/address scoring, and **unidecode** for
  stripping diacritics during normalization
- Plain HTML/CSS frontend with vanilla JavaScript, no framework. Static files
  live in `public/`, which Vercel's CDN serves at the site root; Flask is
  configured to serve the same folder at the same paths locally.

### Commands

From the project root:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt

.venv\Scripts\python app.py            # dev server on http://localhost:8080
.venv\Scripts\python -m pytest -q      # test suite (SQLite store, faked GLEIF)

vercel deploy                          # preview deployment
vercel deploy --prod                   # production deployment
vercel env pull .env.local             # fetch DATABASE_URL etc. for local use
```

Run the app from the project root (not from a subfolder): the `core`
package, `templates/`, `public/` and `data/` all resolve from there. With no
`DATABASE_URL` in the environment the store is a local SQLite file, so a
fresh clone runs with no setup.

### Project structure

```
app.py             # Flask app + routes (entry point; Vercel loads `app`)
core/              # backend lookup logic (ported + simplified from the original)
  constants.py     # matcher thresholds and GLEIF settings (plain constants)
  models.py        # pydantic data models (InputEntity, GleifCandidate, ...)
  address.py       # name/address normalization and country -> ISO conversion
  matcher.py       # precision-first fuzzy name + address scoring
  gleif.py         # synchronous GLEIF API client (requests, retry/backoff)
  lookup.py        # single-entity pipeline: search -> score -> classify
  isin.py          # ISIN validation + LEI resolution/corroboration
  openfigi.py      # OpenFIGI client: ISIN -> issuer name(s) (ISIN fallback)
  storage.py       # search store: one `searches` table on Postgres or SQLite
  export.py        # build CSV / Excel from a search (reflects manual decisions)
  upload.py        # parse an uploaded .xlsx/.csv into entities (bulk)
data/              # read-only lookup tables (committed)
  country_mapping.json  # country name (cs/en) -> ISO alpha-2 code
  legal_forms.txt       # legal-form suffixes stripped before name matching
templates/         # Jinja2 templates
  base.html        # shared layout (header, Help/Report-bugs dialogs, page shell)
  index.html       # single + bulk lookup forms (submit creates a job, goes to /results)
  results.html     # /results: summary card (live while running), stepper, tables
  admin.html       # hidden /admin page: full dump of the searches table (no auth)
public/            # static files, served by Vercel's CDN at the site root
  styles.css       # all styling
  app.js           # vanilla JS: dialogs, forms, job runner loop, validation stepper
  img/             # RB logos (yellow-bar and black-bar variants) + theme icons
tests/             # pytest: storage round-trips + route flows with GLEIF faked
requirements.txt   # pinned runtime dependencies
requirements-dev.txt
vercel.json        # function config: maxDuration 300 s, files excluded from the bundle
.python-version    # 3.12
.vercelignore      # keeps .venv, tests, docs out of the upload
docs/              # project docs (present on disk, gitignored)
```

### Backend

**A search is a job run in short steps.** Vercel runs the app as a function
with a hard time limit (300 s on Hobby) and no long-lived process, so the old
single streaming request that looked up a whole bulk file is gone. Instead:

1. `POST /api/jobs` validates the input (single form fields, or an uploaded
   `.xlsx`/`.csv` parsed by `core/upload.py` - positional columns, 100-entity
   cap) and stores the entities to look up under a new `job_id` via
   `core/storage.create_search`. Nothing is looked up yet. Unusable input
   returns 400 with an `error` message the search page shows next to its
   Search button (this replaced the separate pre-flight endpoint).
2. The browser navigates to `/results?job=<id>`. For an unfinished job the
   page renders the summary card in its running state and `public/app.js`
   calls `POST /api/jobs/<id>/run` in a loop.
3. Each `/run` call looks up the next pending entities (at most
   `RUN_CHUNK_SIZE`, stopping early once `RUN_TIME_BUDGET_SECONDS` have
   passed) against live GLEIF with `core/lookup.py`, appends their result rows
   with `storage.append_results`, and returns the progress: `searched`,
   `total`, the matched / need-validation / unmatched counts, and `done`.
   A GLEIF outage saves whatever completed and answers 503 with an `error`;
   the card shows it and a reload resumes from the saved progress.
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
far), `found` (asserted matches so far), `query` (the entities, JSON) and
`results` (the rows, JSON). Rows older than 30 days are pruned on every write.
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
`MANUAL_MATCH`, a rejection as `MANUAL_NO_MATCH`). The overall percent shown
per candidate is display-only (`core/lookup._overall_match`) and never gates
a match.

### Frontend

- **Branding.** Raiffeisenbank brand palette only (rules in the header
  comment of `public/styles.css`): one yellow, Off Black, Warm Grey
  neutrals, system font stack, no webfonts or CDN assets.
- **Theme.** Light/dark lives on `<html data-theme>`. An inline script
  in `templates/base.html` resolves it before first paint: the explicit
  choice in `localStorage["leiTheme"]`, else the OS colour scheme.
- **Language (CZ/EN).** Lives on `<html lang>` (`localStorage["leiLang"]`,
  default English). Server-rendered text carries both versions in
  `data-cs` / `data-en` (for attributes: `data-cs-placeholder`,
  `data-cs-title`, `data-cs-aria-label`); `applyLang()` in
  `public/app.js` swaps them, and rich text uses `.only-cs` / `.only-en`
  blocks. Strings the script writes itself live in its `STRINGS` table.
  Every new user-visible string needs both languages.

### Deployment (Vercel)

- The project is deployed from this repository with the Vercel CLI
  (`vercel deploy` / `vercel deploy --prod`) or Git integration. Vercel
  detects Flask from `app.py` and `requirements.txt`; no build step.
- `vercel.json` sets `maxDuration: 300` for `app.py` and excludes
  `tests/`, `docs/`, `.venv/` and `__pycache__` from the bundle;
  `.vercelignore` keeps them out of the upload.
- Environment variables: `DATABASE_URL` (injected by the Neon integration
  added from the project's Storage tab), optional `OPENFIGI_API_KEY`.
- Limits that shaped the design: request bodies max 4.5 MB (so uploads are
  capped at 4 MB via `MAX_CONTENT_LENGTH`), 300 s per function invocation
  (hence the chunked `/run` loop), no writable persistent disk (hence
  Postgres), and Flask's `static_folder` is not served in production (hence
  `public/`).
- Dropped with the move off CodeNOW: `.codenow.yaml`, the `codenow/` JSON log
  config (now `logging.basicConfig`), the B3 trace-header echo, waitress,
  the PyCharm/mirrord run configs and the Sonar properties file.

### Current state

Feature-complete and deployable. Single and bulk search run against live
GLEIF through the job endpoints, every search is persisted under a `job_id`
(Postgres on Vercel, SQLite locally), and the results page, the manual
validation workflow and the CSV/Excel downloads are real. `tests/` covers the
store and the route flows with GLEIF faked; run it before every change to the
web layer. Matching behaviour (thresholds in `core/constants.py`) is
audit-validated and unchanged from the CodeNOW version - re-run the matcher
audit before tuning it.

An optional LLM-assisted step (e.g. helping disambiguate near-misses) is a
possible next addition; it would plug in after `core/lookup.lookup_entity`
returns and must never override the precision-first assertion rules.
