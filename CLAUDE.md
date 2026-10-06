
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
or `.txt` file of entities, or by pasting a few rows copied from Excel.

It is a lean rebuild based on the core of the original LEI lookup tool created
by Jakub Schrimpel: the precision-first matcher and GLEIF/ISIN resolution are
kept (with the original's later precision fixes ported in), and the app now
lives on Vercel (Python runtime, Neon Postgres)
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
  upload.py        # parse an uploaded .xlsx/.csv/.tsv/.txt, or pasted rows, into entities
data/              # read-only lookup tables (committed)
  country_mapping.json  # country name (cs/en/native) -> ISO alpha-2 code
  country_alpha3.json   # ISO alpha-3 code -> alpha-2 code ("DEU" -> "DE")
  legal_forms.txt       # legal-form suffixes stripped before name matching
templates/         # Jinja2 templates
  base.html        # shared layout (header, Help/Report-bugs dialogs, page shell)
  index.html       # single + bulk lookup forms, bulk = file or pasted rows (submit creates a job)
  results.html     # /results: summary card (live while running), stepper, tables
  admin.html       # hidden /admin page: the searches table, 5 rows a page (no auth)
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
   `.xlsx`/`.csv`/`.tsv`/`.txt` parsed by `core/upload.py` - positional
   columns, 100-entity cap; a `.csv` detects comma, semicolon or tab, a
   `.tsv`/`.txt` is always tab-separated; an `.xlsx` whose rows are whole
   semicolon lines in column A - a semicolon CSV Excel opened with the
   wrong delimiter - is rebuilt and split at the semicolons) and stores
   the entities to look up under a new `job_id` via
   `core/storage.create_search`. Nothing is looked up yet. Unusable input
   returns 400 with an `error` message the search page shows next to its
   Search button (this replaced the separate pre-flight endpoint).
   Pasted rows (the bulk card's File / Paste rows switch; `mode=paste`,
   form field `rows`, stored with mode "paste") go through
   `core/upload.parse_pasted_rows` and the same header, cap and length
   rules: text with a tab is read like a `.tsv`, by position (an empty
   ISIN cell keeps its place); with no tab, semicolon lines when every
   line holds one, else line by line (`_spaced_cells`), for rows whose
   tabs an e-mail turned into spaces (columns 2+ spaces apart). A name
   may hold a doubled space too, and a name cut short can match its
   parent ("Bank of America  NA" -> "Bank of America"), so the name
   ends only at a sure boundary and is joined with single spaces: a
   country after a gap of 4+ spaces (an empty ISIN cell's trace), a
   valid ISIN written as one word (not after a country code: a Slovak
   VAT number passes the ISIN check), or a placeholder (an Excel error
   value, "#" and a letter, so not a fund's "#2"; N/A, NULL, 0, -)
   before a country, unless a wide gap follows. Failing those,
   and only with no wide gap, an uppercase two-letter code right after
   the first value is the country (not a legal form or "NA"). Any other
   line is one value, the name: a lost match, never a wrong one. A valid
   ISIN alone is an ISIN-only row; a row whose street lands in the
   postal code (a doubled space in its city) keeps only name and
   country rather than refusing the paste; semicolon lines are not
   taken when a line has a wide gap; an Excel-quoted multi-line cell
   in a paste without tabs splits in two (accepted). `country_to_iso` costs
   about 1 ms on a miss, so it is only reached for lines with a name,
   which the 101-entity cap bounds. Three adversarial rounds
   (2026-10-01) shaped these rules; `tests/test_paste.py` holds their
   cases. The same rounds found `_LABEL_NOTE` quadratic on many "("
   (4 MB in a header cell took hours): now `\([^()]*\)`.
   Upload rules: blank rows (also cells of only whitespace or invisible
   format characters, `core.models.is_blank`) are skipped, and the first
   non-blank row is dropped when it holds column labels (Name/ISIN/Země/PSČ,
   PARTY_FULL_NAME/ISIN_IDENT, "ISIN (optional)", ...;
   `core/upload._is_header`): never when its ISIN is valid or it has at
   least as many data-looking cells (a digit, a country name or code) as
   labels, so "Party City,,US,..." stays data; a numbered label (an
   address-line word and a short number last: "Address 1", "ADDR_LINE_2",
   `core/upload._is_numbered_label`) counts as neither label nor data,
   while a street with its house number ("Obchodní 12") is data and "Firma
   1" still starts a list; plural labels ("Company names", "Klienti")
   head a one-column list; a one-cell title row above a header (numbered
   labels counted) is dropped with it, also above semicolon lines. A row
   with a value over its
   `InputEntity` length limit refuses the whole file, naming the row (as
   numbered in the file), the field and the limit - rows are never dropped
   or truncated silently. Parsing stops at the 101st entity row ("more
   than 100"). An `.xlsx` is read with `reset_dimensions()` and at most 6
   columns (50 in the semicolon-lines mode), through a guarded zip archive
   that charges every read to a budget (50 MB unpacked, 1,000,000 XML
   nodes: room for some 500,000 shared strings from other sheets, about
   5 s; the stylesheet also has its own 200,000 nodes, as openpyxl spends
   about 25 microseconds on each style node, and a number format longer
   than Excel's 255 characters is refused, as openpyxl rescans it per
   style) and refuses DTDs, so a small crafted file cannot tie the
   function up (the slowest crafted stylesheet still read takes about 6 s
   locally, a shared-strings one about 5 s);
   `core/upload._load_workbook` relies on openpyxl 3.1.5 internals
   (`ExcelReader.archive`), so re-check it when upgrading openpyxl.
   The same guard notes formulas saved with no value (a script-written
   or never-calculated workbook; `core/upload._UncalculatedFormulas`,
   using `ReadOnlyWorksheet._worksheet_path`): one in the columns read
   refuses the file, naming the cells, as it would read as empty.
   Cells are read as Excel shows them: `_xHHHH_` escapes are decoded, and
   a numeric postal code with a zeros-only number format keeps its leading
   zeros (1001 as "000 00" -> "010 01"). The semicolon-lines mode is
   chosen on the rebuilt lines (cells joined with commas), so names with
   commas ("ČEZ, a. s.") work, while a plain sheet whose names hold ';'
   keeps its columns. Text files: UTF-16 by BOM, then UTF-8; a file that
   is mostly valid UTF-8 keeps UTF-8 with U+FFFD for the bad bytes, else
   ISO 8859-2 when no byte is 0x80-0x9F and some byte reads differently
   from cp1250 (its Š, Ž, Ť, š, ž, ť), else cp1250 (undefined bytes become
   U+FFFD). The extension is the part from
   the last dot, so a file named just ".csv" is read.
2. The browser navigates to `/results?job=<id>`. For an unfinished job the
   page renders the summary card in its running state and `public/app.js`
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
   has left, the reply is 200 with the progress plus `throttled`,
   `retry_after` and `service` ("GLEIF"), and the page counts the wait
   down (2-30 s) and continues by itself. OpenFIGI failing pauses the same
   way (`service` "OpenFIGI", `core.openfigi.OpenFigiUnavailable`): a rate
   limit after its Retry-After and costing no attempt, a server error or
   no answer after `OPENFIGI_PAUSE_SECONDS` (10) and counting one, until
   the entity is stored as failed after `RUN_MAX_ATTEMPTS` - an empty
   result would have been stored as a final "no match". A cut-off counts
   as throttled when the lookup's own
   rate-limit waits (`GleifClient.rate_limit_waits`, reset per lookup)
   left it less than `RUN_DEADLINE_SECONDS - RUN_TIME_BUDGET_SECONDS` of
   its own time. requests' timeout bounds each socket read, not a whole
   reply, so GLEIF and OpenFIGI replies are streamed and read one socket
   read at a time (`core/gleif.read_body`), and each request runs under a
   `core/gleif.DeadlineWatch`: the connections of a `watched_session`
   report themselves to it, and a timer shuts the socket down 1 s after
   the deadline (`_WATCH_GRACE`). That also ends headers, redirects, 1xx
   replies and gzip or chunked bodies that trickle in, where urllib3 and
   http.client read many times inside one call; DNS and the TLS handshake
   stay bounded only per read. Tests fake OpenFIGI at `openfigi._post`.
4. When `done` the page reloads and the server renders the detailed tables.

Each stored result row is the entity's `input`, its `match` (a `LookupResult`)
and up to 3 `closest` candidates for manual review
(`core/lookup.CLOSEST_CANDIDATE_LIMIT`). ISIN resolution is unchanged from the
original: when name+address yields no confident match, a validated ISIN
(`core/isin.py`) can find a LEI directly, corroborate a near-miss, or - as a
last resort - be resolved to an issuer name via OpenFIGI (`core/openfigi.py`)
and re-searched; an ISIN-only input is resolved by GLEIF's authoritative ISIN
mapping, with an OpenFIGI review fallback. A direct ISIN hit or an OpenFIGI
candidate is not asserted when it lies in another country than the one given
(`core/isin._other_country`), nor when a name candidate with another LEI
matches the name better (an ISIN of a parent, a serial sibling or the named
manager's fund must not override the named entity; the OpenFIGI fallback got
this check on 2026-10-06, as its 65 let "Trust 2023-B", at 70, override
"Trust 2023-A"), and the OpenFIGI fallback
asserts only a unique best candidate. A match that fallback makes is typed
`ISIN_OPENFIGI_MATCH` and flagged `ISIN_VIA_OPENFIGI` ("ISIN via OpenFIGI"),
not "Matched by ISIN", and its note says the LEI was found in GLEIF by the
issuer name: GLEIF may have no record of the ISIN (a user checking FIRY INC
there found none, 2026-10-06). An ISIN mapping to several LEIs offers
them in the stepper. The ISIN-based matches flag `COUNTRY_MISMATCH` or
`COUNTRY_UNVERIFIED` unless the given country is recognised and is the
record's; every other match flags `COUNTRY_UNVERIFIED` for a missing or
unrecognised country. Among full matches within the ambiguity band a
maintained LEI beats a dead twin (`core/lookup._finalize_full_match`).

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
created lazily on first use, once per process (on Postgres under an
advisory lock, as `CREATE TABLE IF NOT EXISTS` races between cold starts).
On Vercel (`VERCEL`/`VERCEL_ENV` set) the SQLite fallback is refused:
with no database URL every store call raises `storage.StoreUnavailable`,
answered 503 with `error`/`error_cs` (plain text for a page), and
`/health` says DOWN - a per-instance `/tmp` file once lost searches
silently. A Postgres connection gives up after 10 s, uses TCP keepalives
and a TCP user timeout (`_PG_CONNECT_OPTIONS`), sets `SET LOCAL
statement_timeout = '30s'` at the start of each transaction (Neon's
pooler refuses startup options), and turns server-side prepared
statements off.

**The `/results` page, decisions and downloads** work as before: a summary
card with the four counts (Searched `x / total`, Matched, Need validation,
Unmatched), the orange validation stepper (top 3 candidates per near-miss,
confirm one or "None of these", saved via `POST /api/decision` ->
`storage.record_decision`), the matched and no-match tables, and the CSV /
Excel downloads built by `core/export.py` (the CSV starts with a UTF-8
byte-order mark, so Excel does not read it as cp1250; a confirmed pick
exports as
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
page shows lookup notes in both languages (`core/notes.czech_note`), and each
matched row flags a non-ISSUED LEI status and its warnings (all but
`CHECK_FAILED`, which only means a missing street or ZIP), both languages,
from the template's `flag_labels`; a match through the ISIN also shows its
note there, saying how it was made, and a GLEIF street "c/o" an agent (for
many US companies GLEIF has only their registered agent's office, CSC's in
Wilmington for FIRY INC) gets the page's own "Agent's address (c/o)" flag
(`app._display_flags`), as a searched address rarely matches it. The overall
percent shown
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
  Error messages in the server's JSON replies come as `error` (English)
  and `error_cs` (Czech): raise `core.models.InputError(english, czech)`
  for input the user must fix. The script keeps every message it writes
  in both languages on its element (`both(key)`, `serverMessage(data)`,
  `showBoth(el, message)`), so a CZ/EN switch translates it too.
  Every new user-visible string needs both languages.
- **Caching and history.** `/results` and the downloads are sent with
  `Cache-Control: no-store` (`app.no_store_live_pages`): from the HTTP
  cache, Back showed saved decisions as undecided. Search stays disabled
  once the results page is on its way (re-enabled on a back-forward
  cache restore), and an explicit theme choice stops the page following
  the OS scheme. `tests/test_frontend.py` drives these in Node.
- **Phones.** Below 700 px the stepper's Overall/Confirm columns are not
  pinned, as the two covered the whole table at 375 px.

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

A focused test run on 2026-09-30 (six lenses plus skeptics) found that the
rebuild's matcher predates the original tool's last precision fixes (its
commit 24a4d7f, 2026-09-18). Those were ported: the narrow share-class
patterns (`core/address._SHARE_CLASS_TOKEN`; RAIF/SIF moved to
`data/legal_forms.txt`), single-letter serials as distinguishing tokens
(I/V/X, and a letter after a number or a serial word such as "Fund" or
"Series", `core/matcher._significant_tokens`) and the contradiction cap of
79 (`ADDRESS_CONTRADICTION_CAP`). DUPLICATE and CANCELLED now count as not
maintained, and the ISIN, country and review changes above came with it.
Checked offline by replaying the old tool's 340-case adversarial baseline
from its committed GLEIF snapshot (in the archive,
`Desktop/_old/LEI-2026-09-22/tests/golden/offline_cache.jsonl.gz` - not
copied into this public repo; the replay scripts and today's expected
output are in the gitignored `docs/offline-replay/`): wrong LEIs 5 -> 1
(the old tool's own "Simp" case), the one lost true match recovered, the
four wrong-country ISIN rows now matched through ISIN plus name with
`COUNTRY_MISMATCH`, and 105 of its 109 labelled name pairs (was 104). "Compartment A" vs "B" still collapses:
"compartment" is stripped as a legal form before its letter is seen.
`normalize_name` also drops invisible format characters before the legal
forms (a trailing zero-width space kept "Allianz SE" from matching) and
turns U+0085 into a space (`core/address._drop_invisible`); the replay
is unchanged by it. Still open after that run: no rate limit on creating
jobs (anyone can fill the store; a Vercel Firewall rule or sign-in is
the owner's call), nothing has run on Python 3.12 or a real Postgres
here (a throwaway Neon branch would do the latter), and a date or
boolean in the name column is looked up as its text.

Feature-complete and deployable. Single and bulk search run against live
GLEIF through the job endpoints, every search is persisted under a `job_id`
(Postgres on Vercel, SQLite locally), and the results page, the manual
validation workflow and the CSV/Excel downloads are real. `tests/` covers the
store and the route flows with GLEIF faked; run it before every change to the
web layer. Matching behaviour (thresholds in `core/constants.py`) is
audit-validated and unchanged from the CodeNOW version apart from the
legal-form whitespace, the 2026-09-30 precision fixes and the OpenFIGI
fallback's rival check (2026-10-06, offline replay unchanged) above - re-run
the matcher audit (at least the offline replay) before tuning it.

An optional LLM-assisted step (e.g. helping disambiguate near-misses) is a
possible next addition; it would plug in after `core/lookup.lookup_entity`
returns and must never override the precision-first assertion rules.
