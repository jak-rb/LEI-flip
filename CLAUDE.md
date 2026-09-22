
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
to narrow
the result) or a bulk lookup by uploading an `.xlsx` or `.csv` file of entities.

It is a fresh, deliberately lean rebuild based on the core of the original LEI
lookup tool created by Jakub Schrimpel. The goal is to keep that core while
dropping the heavier infrastructure for simplicity.

### Tech stack

- **Python 3.12** (matches the CodeNow runtime image
  `python:3.12.6-slim-bullseye`)
- **pip** with a pinned `requirements.txt` for dependency management
- **Flask** for the web server and Jinja2 templates
- **waitress** as the production WSGI server on CodeNow
- **python-json-logger** for structured JSON logging (CodeNow log aggregation)
- **openpyxl** for generating the Excel (`.xlsx`) export
- **pydantic** for the core data models (`InputEntity`, `LookupResult`, …)
- **requests** for the (synchronous) HTTP calls to the GLEIF API
- **rapidfuzz** for fuzzy name/address scoring, and **unidecode** for stripping
  diacritics during normalization
- Plain HTML/CSS frontend with vanilla JavaScript, no framework. Submitting a
  search on the index page stashes the input in `sessionStorage` and redirects
  to `/results`, which runs the lookup as an async streaming flow: it `fetch`es
  `/api/search` and reads newline-delimited JSON (NDJSON) progress events to
  update the summary card live, then reloads as `/results?job=...` to render the
  detailed tables.

### Commands

Create the virtual environment and install dependencies from the project
root, then run the app from `src/`:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt   # install dependencies

cd src
python app.py                       # dev server on http://localhost:8080
waitress-serve --port=8080 app:app  # production-style run (WSGI)
```

The app imports its packages as `core.*` and resolves `templates/`,
`static/`, and `data/` relative to `src/`, so start it from inside `src/`.

### Project structure

```
src/               # all application code (run the app from here)
  app.py           # Flask app and routes (entry point)
  core/            # backend lookup logic (ported + simplified from the original)
    constants.py   # matcher thresholds and GLEIF settings (plain constants)
    models.py      # pydantic data models (InputEntity, GleifCandidate, …)
    address.py     # name/address normalization and country -> ISO conversion
    matcher.py     # precision-first fuzzy name + address scoring
    gleif.py       # synchronous GLEIF API client (requests, retry/backoff)
    lookup.py      # single-entity pipeline: search -> score -> classify
    isin.py        # ISIN validation + LEI resolution/corroboration
    openfigi.py    # OpenFIGI client: ISIN -> issuer name(s) (ISIN fallback)
    storage.py     # SQLite store: one `searches` table (query + results) + decisions
    export.py      # build CSV / Excel from a search (reflects manual decisions)
    upload.py      # parse an uploaded .xlsx/.csv into entities (bulk)
  data/            # read-only lookup tables (committed)
    country_mapping.json  # country name (cs/en) -> ISO alpha-2 code
    legal_forms.txt       # legal-form suffixes stripped before name matching
  templates/       # Jinja2 templates
    base.html      # shared layout (header, Help/Report-bugs dialogs, page shell)
    index.html     # single + bulk lookup forms (search hands off to /results)
    results.html   # detail page (/results): summary card, validation stepper, result tables
    admin.html     # hidden /admin page: full dump of the searches table (no auth, unlinked)
  static/
    styles.css     # all styling
    app.js         # vanilla JS: dialogs, forms, search hand-off + streaming card, validation stepper
requirements.txt   # pinned pip dependencies
.codenow.yaml      # CodeNow build/runtime config (python pip-app pipelines)
codenow/config/    # platform config, incl. the JSON log config (log-config.json)
.run/              # PyCharm run + mirrord configs (working dir src/)
sonar-project.properties  # SonarQube coverage report path
docs/              # project docs (present on disk, gitignored)
  PLAN.md          # open items still to review/adjust (build is done)
  HOW.md           # how the matcher works + how every percentage is computed
```

### Backend

The web layer is fully real (nothing stubbed). `src/app.py` exposes a streaming
search endpoint (`POST /api/search`, NDJSON progress events), a bulk-file
pre-flight check (`POST /api/validate-upload`), CSV/Excel download endpoints,
the `/results` page, `POST /api/decision`, a hidden, unlinked `/admin` page
that dumps the whole `searches` table (no auth), and a `GET /health` liveness
probe for the platform. At import it configures structured JSON logging (from
`codenow/config/log-config.json`) and echoes B3 (`X-B3-*`) trace headers back
on every response, both for CodeNow.

**Search runs on the detail page.** Submitting either form validates the input,
stashes it in `sessionStorage`, and navigates to `/results` (a bulk file is
carried as a `data:` URL, since a File cannot survive the navigation). The
detail page reads the stashed input, POSTs it to `/api/search`, streams the
lookup into the summary card at the top, then reloads as `/results?job=...` so
the server renders the detailed tables. "Back to search" returns to a clean
index with no job.

**Single and bulk lookups are real.** `/api/search` builds `InputEntity` objects
(single form, or `core/upload.py` parsing an `.xlsx`/`.csv` - positional columns,
100-entity cap), runs `core/lookup.py` against the live GLEIF API, and streams
per-entity progress bucketed into matched / need-validation / unmatched counts;
a GLEIF outage streams the red error state. Each search is **persisted**: it
mints a `job_id` and writes one row to the single `searches` table via
`core/storage.py` - the `query` (what the user searched, as JSON) plus, per
entity, its matched result and 3 closest candidates - then returns the
`job_id` in the done event. ISIN resolution is wired in: when
name+address yields no confident match, a validated ISIN (`core/isin.py`) can
find a LEI directly, corroborate a near-miss, or - as a last resort - be
resolved to an issuer name via OpenFIGI (`core/openfigi.py`) and re-searched in
GLEIF, accepted only when the typed name and the OpenFIGI name both match. An
input needs a name or an ISIN (or both). When only an ISIN is given, resolution
is by ISIN alone: GLEIF's authoritative ISIN->LEI mapping (`lookup_by_isin`)
auto-asserts a confident match, and if the ISIN is not in that mapping an
OpenFIGI review surfaces candidates for manual confirmation.

The CSV and Excel **downloads are real**: `/download/csv?job=` and
`/download/excel?job=` stream a file built by `core/export.py`: one row per
entity with structured columns (input fields, LEI / status / match type /
confidence, the per-field scores, GLEIF name and addresses, warnings, notes)
and a spreadsheet formula-injection guard, reflecting any manual decision
(`MANUAL_MATCH` for a confirmed pick, `MANUAL_NO_MATCH` for a rejection). A
missing job returns 404.

**The `/results` page.** A summary card at the top shows four counts that
partition every searched entity - Searched (`x / total`), Matched (green), Need
validation (orange), Unmatched (red) - with "Back to search" on its left and the
downloads on its right; its header reads "Search complete", "Needs review", or
"No matches found". Below it, three zones render: an orange "To validate (N)"
**stepper** (near-misses one at a time, top 3 candidates, arrows to navigate, N
counts down as decisions are made), a green "Matched records" table, and a red
"No matches" table. Candidate rows and the matched table show Legal name /
Country / City / Street / a GLEIF link / an **overall match percent**; candidate
rows are sorted by that percent (highest first), and the percent and the confirm
button stay pinned to the right as a row scrolls. Each candidate row has a left
chevron expanding to its full legal + HQ addresses and per-field scores. The overall percent is a display-only, name-weighted (60/40)
blend of name and address agreement (`core/lookup._overall_match`) and never
gates a match. Confirming a candidate or "none" is saved via
`POST /api/decision` (`core/storage.record_decision`), which only flags the
choice on that entity's stored row (no candidate data is duplicated), so the
tables and downloads reflect it. `core/lookup.CLOSEST_CANDIDATE_LIMIT` is 3.

The backend was ported and simplified from the original tool at
`../kuba_repository/lei-lookup-tool/` (Jakub Schrimpel's FastAPI version). It
keeps that tool's core - the precision-first matcher, the GLEIF search
strategies, ISIN resolution (including the OpenFIGI fallback), and the lookup
tables - but switches to synchronous Flask, stays LLM-free, and leaves out the
heavier infrastructure the original carried (the persistent SQLite response
cache, containerised deployment). The original remains the reference for
matching behaviour.

### Current state

The app is feature-complete and nothing is stubbed. Single and bulk search run
against the live GLEIF pipeline (by name, by ISIN, or both - including ISIN
resolution and an ISIN-only path), each search is persisted to SQLite under a
`job_id`, and the `/results` page, its manual-validation workflow, and the
CSV/Excel downloads are all real.

Frontend behaviour: the Help ("?") and "Report bugs" header buttons open
`<dialog>` popups (`base.html`); the Help dialog walks through the single and
bulk lookups and choosing the right candidate in the validation step; its intro
notes the OpenFIGI fallback used when an ISIN is not found directly in GLEIF. Both lookup
forms validate input (single: a name or an ISIN required; bulk: one
`.xlsx`/`.csv`, added by click or drag-and-drop, with a remove button; the bulk
file is pre-flighted server-side via `/api/validate-upload`, so file problems -
wrong type, empty, no usable rows, over the 100-entity cap - surface as red text
by the Search button) and, on submit, hand the search off to
`/results` (see Backend). The detail page streams
live progress into the summary card - the four counts, an animating spinner, and
the state header - and reports GLEIF-down, server, and connection failures with
distinct messages, then reloads as `/results?job=...` to render the tables.

What remains is tracked in `docs/PLAN.md`: the matching-quality review and
production hardening (disabling proxy response buffering so the NDJSON search
stream reaches the browser live). This repo is CodeNow-deployable: the app
lives under `src/` (entry `src/app.py`) with the platform's JSON logging,
`GET /health`, and B3 trace headers, and runs via pip + `requirements.txt` +
waitress on the `python:3.12.6-slim-bullseye` image (see `.codenow.yaml`).

The SQLite store lives on a **writable** path resolved by
`core/storage._resolve_db_path`: the `LEI_DB_PATH` env var if set, else a
folder under the system temp dir. It is deliberately NOT written beside the
committed lookup tables in the app's `data/` folder: the CodeNow image
filesystem is read-only, so creating the DB there crashed startup with
`sqlite3.OperationalError: unable to open database file`. Point `LEI_DB_PATH`
at a mounted persistent volume for the 30-day search history to survive
redeploys (the temp-dir default keeps the app running but is ephemeral).

The earlier separate `../lei-lookup1/` scaffolding copy has been folded into
this repo and is now redundant.
