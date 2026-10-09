
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
kept (with the original's later precision fixes ported in). This tree
(branches `codenow` and `main`) packages it as a CodeNOW Flask component on
the blueprint scaffold of the `codenow-flask` plugin, so it can run on the
bank's CodeNOW platform again, which it was first built for; the `vercel`
branch holds the same app laid out for Vercel, where it is live (deploy it
only from a `vercel` checkout). This tree behaves as `vercel` did at its
commit 4cdc85c (ported on 2026-10-09, see Current state).

### Tech stack

- **Python 3.12** in the CodeNOW build and runtime images (`build.image` /
  `runtime.image` in `.codenow.yaml`: `python:3.12.6-slim-bullseye`);
  locally any 3.12+ works
- **pip** with one pinned `requirements.txt`: CodeNOW installs only that
  file, so it carries pylint, pytest and coverage too
- **Flask** for the web layer and Jinja2 templates, as a blueprint component:
  the `create_app()` factory in `src/app.py` registers `bp_main`
  (`src/main/`) under `URL_PREFIX`; **waitress** serves the module-level
  `app` (16 threads, `WAITRESS_THREADS`)
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
.venv\Scripts\python -m pylint --disable=C,R,W src   # the build's gate command

# The plugin's readiness check (pylint gate, lint coverage, /health + /,
# the coverage pin the build's unit-test stage needs, pytest) calls `py`;
# with the venv active `py` runs the venv's Python.
.venv\Scripts\Activate.ps1; ./scripts/check.ps1   # must print READY TO COMMIT
```

Run pylint from the project root, or it never loads `.pylintrc`. The local
gate matches the build's exactly only on Python 3.12 (the build image's
version); this machine has 3.13. Names that `src/app.py` imports from
`main.routes` escape pylint's E0611 check (the scaffold's circular import
`main` <-> `main.routes` hides them), so a typo there is caught by the
tests, not the gate. The app
resolves its files from its own location (`src/main/data/`, the log config
under `codenow/config/`), so the working directory does not matter. With
no `DATABASE_URL` the store is a local SQLite file, so a fresh clone runs
with no setup. Locally `URL_PREFIX` is unset and the app answers at `/`;
set it to run as deployed - the suite passes either way (`tests/conftest.py`
sends the tests' paths under it). In Git Bash, set it with
`MSYS_NO_PATHCONV=1 MSYS2_ENV_CONV_EXCL='*'`, or Bash turns `/lei-lookup`
into a Windows path. `tests/test_frontend.py` runs
`src/main/static/app.js` in Node and is skipped when `node` is not on
PATH, as in the CodeNOW build image.

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
    notes.py         # Czech versions of the lookup notes, for the results page
    storage.py       # search store: one `searches` table on SQLite or Postgres
    export.py        # build CSV / Excel from a search (reflects manual decisions)
    upload.py        # parse an uploaded .xlsx/.csv/.tsv/.txt, or pasted rows
  main/              # the bp_main blueprint
    __init__.py      # bp_main; owns templates/, static/ (at /static/main) and data/
    routes.py        # every route except /health; new routes go at the BOTTOM
    templates/       # Jinja2 templates
      base.html      # shared layout (header, Help/Report-bugs dialogs, page shell)
      index.html     # single + bulk lookup forms, bulk = file or pasted rows
      results.html   # /results: summary card (live while running), stepper, tables
      admin.html     # hidden /admin page: the searches, 5 a page (no auth)
    static/
      styles.css     # all styling
      app.js         # vanilla JS: dialogs, forms, job runner loop, validation stepper
      img/           # RB logos (yellow-bar and black-bar variants) + theme icons
    data/            # read-only lookup tables (committed)
      country_mapping.json  # country name (cs/en/native) -> ISO alpha-2 code
      country_alpha3.json   # ISO alpha-3 code -> alpha-2 code ("DEU" -> "DE")
      legal_forms.txt       # legal-form suffixes stripped before name matching
tests/               # pytest (SQLite store, GLEIF and OpenFIGI faked)
  conftest.py        # src/ on sys.path, the tests' paths sent under URL_PREFIX
  test_smoke.py      # the deployment shape: /health, the prefix, waitress
  test_paste.py            # pasted rows (the 2026-10-01 adversarial rounds)
  test_precision_fixes.py  # the original's precision fixes, ISIN checks
  test_review_rules.py     # the reviewers' rules of 2026-10-06
  test_fuzzy_name.py       # GLEIF's fuzzy-name fallback (2026-10-09)
  test_deadline_watch.py   # replies that trickle in past the deadline
  test_store_limits.py     # Postgres limits, the SQLite refusal, /admin paging
  test_frontend.py         # app.js in Node (skipped without `node`)
  test_*.py                # uploads, the /run loop, decisions, exports, pages
scripts/check.ps1    # the plugin's readiness check (copied verbatim)
codenow/config/      # config.yaml, log-config.json, environment-variables
nginx/app.conf       # from the platform scaffold; leave it alone
.codenow.yaml        # the component's build/runtime images, pipelines, port 80
.pylintrc            # puts src/ on pylint's path (verbatim from the plugin)
sonar-project.properties  # coverage report path for the static-analysis stage
.run/                # PyCharm run config + mirrord config (from the component)
requirements.txt     # pinned dependencies, runtime + pylint + pytest
docs/                # project docs (present on disk, gitignored)
```

### Backend

`core/...` paths below are in `src/core/`, and `main.routes` is
`src/main/routes.py`.

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
   in a paste without tabs splits in two (accepted). `country_to_iso`
   costs about 1 ms on a miss, so it is only reached for lines with a
   name, which the 101-entity cap bounds. Three adversarial rounds
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
   server up (the slowest crafted stylesheet still read takes about 6 s
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
   the deadline (`_WATCH_GRACE`). That also ends headers, 1xx replies
   and gzip or chunked bodies that trickle in, where urllib3 and
   http.client read many times inside one call; DNS, the TLS handshake
   and a redirect followed after the timer fired stay bounded only per
   read. On Linux the shut socket reads as the
   reply's end rather than failing, so `read_body` takes a reply that
   ends past the deadline as cut off too (`tests/test_deadline_watch.py`
   runs with Linux's reads emulated as well). Tests fake OpenFIGI at
   `openfigi._post`.
4. When `done` the page reloads and the server renders the detailed tables.

Each stored result row is the entity's `input`, its `match` (a `LookupResult`)
and up to 3 `closest` candidates for manual review
(`core/lookup.CLOSEST_CANDIDATE_LIMIT`). When no name search finds a
record (with the country, the ISIN's country, then none), GLEIF's fuzzy
completions supply the candidates (`GleifClient.search_by_fuzzy_name`,
the GLEIF website's "Did you mean" list), as its filters match whole
words: "GOLDMAN SACHS ASSET MANAGMENT" got "No LEI found" until
2026-10-09. The matcher still decides. The offline replay with GLEIF's
live fuzzy replies for the 68 of its 340 cases that reach it: 2 typos
newly matched right, no wrong LEI; GLEIF suggests nothing for a typo in
the first word or a spelled-out legal form. ISIN resolution is unchanged
from the original: when name+address yields no confident match, a
validated ISIN (`core/isin.py`) can find a LEI directly, corroborate a
near-miss, or - as a last resort - be resolved to an issuer name via
OpenFIGI (`core/openfigi.py`) and re-searched; an ISIN-only input is
resolved by GLEIF's authoritative ISIN mapping, with an OpenFIGI review
fallback. A direct ISIN hit or an OpenFIGI
candidate is not asserted when it lies in another country than the one given
(`core/isin._other_country`), nor when a name candidate with another LEI
matches the name better (an ISIN of a parent, a serial sibling or the named
manager's fund must not override the named entity; the OpenFIGI fallback got
this check on 2026-10-06), and the OpenFIGI fallback asserts only a unique
best candidate whose name clears the usual 75 against the typed name (65
against OpenFIGI's, often cut short). It asked only 65 of the typed name
until 2026-10-06, when a live replay of real searches found a lapsed "GENIUS
SPORTS MEDIA INC." (70) asserted for "Genius Sports Ltd.", which GLEIF lacks.
OpenFIGI's name only finds that candidate: it is asserted only when the
address given agrees with its legal or HQ address (the full-match gate,
`core/isin._address_agrees`; an HQ agreement adds `HQ_ONLY_MATCH`), and
otherwise goes to review - a NO_MATCH whose note names the issuer
OpenFIGI gave, with the candidate offered in the stepper even when the
name search lacked it (`resolve_via_isin` returns `(result, to_review)`).
Reviewers asked for that on 2026-10-06 (Redwire Corporation: GLEIF has
only its Delaware agent as legal address). A match that fallback makes is
typed
`ISIN_OPENFIGI_MATCH` and flagged `ISIN_VIA_OPENFIGI` ("ISIN via OpenFIGI"),
not "Matched by ISIN", and its note says the LEI was found in GLEIF by the
issuer name: GLEIF may have no record of the ISIN (a user checking FIRY INC
there found none, 2026-10-06). An ISIN mapping to several LEIs offers
them in the stepper. The ISIN-based matches flag `COUNTRY_MISMATCH` or
`COUNTRY_UNVERIFIED` unless the given country is recognised and is the
record's; every other match flags `COUNTRY_UNVERIFIED` for a missing or
unrecognised country. **Only an ISSUED LEI is ever asserted**
(`core.models.is_issued`): any other status (LAPSED, RETIRED, MERGED,
PENDING_TRANSFER...) is a stop on every path - the reviewers' rule of
2026-10-06, "anything other than issued ... is only for looking". A full
match on such an LEI becomes a NO_MATCH whose note names it and its
status (`core/lookup._not_usable_no_match`). Only full matches within
`AMBIGUITY_CONFIDENCE_DELTA` of the best compete, so a live LEI beats its
dead twin with `AMBIGUOUS_MATCH`, but a clean match on a stopped LEI is
not handed to a far worse ISSUED one (`_finalize_full_match`; that one
goes to review). The ISIN paths skip stopped records; an ISIN-only
search mapped to one, or an OpenFIGI best candidate that is one, says
so in its note and offers it to be seen. A stopped record still counts
in the OpenFIGI fallback's ambiguity check (the live replay's "X-Energy
Inc": a LAPSED twin of an Italian "X ENERGY S.R.L." keeps the live one
from being asserted). Any other row whose candidates are all stopped
says "No usable LEI found in GLEIF. The closest records cannot be used:
..." with each record's LEI and status (`core.models.stopped_note`,
applied by `lookup_entity` and, for stored searches, by
`core.models.shown_note` on the page and in the downloads), unless its
note names a record of its own (an HQ-only near-miss on a stopped LEI
ends "LEI <lei> status: <status>."). Stopped records never crowd an
ISSUED candidate out of the three review places
(`core/lookup._keep_an_issued`; an ISIN's records list ISSUED first),
and the record a NO_MATCH note names (an HQ-only or name-only
near-miss, a stopped full match, the OpenFIGI candidate) is always
among them (`_keep_pinned`), so a note never stands over another
same-named LEI.
Review candidates must share a distinctive word with the searched name,
or clear the name gate (`core/matcher.shares_name_word`): a name
agreeing only in its legal form ("FISS, spol. s r.o." / "BRŮZA spol. s
r.o.") is no near-miss. "spol. s r.o." is stripped however its dots and
spaces fall (`core/address._RE_SPOL_SRO`, as bank exports drop them),
and a spaced single-letter form ("a. s.") is not stripped right after a
dotted lone letter, where it ends spaced initials ("J. K. S. Group"; but
"M & M s. r. o." strips). A run of initials inside a name ("EURO F.D.
HOLDINGS") is a distinguishing token (`core/matcher._initials_run`),
while a trailing run, one spelling a legal form ("..., L.P.", "P.L.C.")
or, after the first word, a legal designation (`_LEGAL_FORM_INITIALS`:
"N.A.", "S.C.A.", "d.s.s.") is dropped as before; a letter standing
alone ("Firma B a. s.", the letters of "M&M") is a token too, so
siblings named by a letter stay apart. Joined initials and any token of
three letters or fewer are covered only by their exact twin ("A.B.C."
is neither "ABCD" nor "A.B.C.D." "ABCDE"), a name left with no token at
all scores at most AMBIGUOUS_NAME_CAP, names equal but for spacing
("J.P. Morgan" / "JPMorgan") score 100 (`core/matcher.name_similarity`),
and "v likvidaci" (in liquidation) is stripped like a legal form. A
note's percent is the whole number below the score, as the review table
shows it.

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
advisory lock, as `CREATE TABLE IF NOT EXISTS` races between processes
starting at once), never at import, so a slow database cannot keep
`/health` from answering. A Postgres connection gives up after 10 s, uses
TCP keepalives and a TCP user timeout (`_PG_CONNECT_OPTIONS`), sets `SET
LOCAL statement_timeout = '30s'` at the start of each transaction (a
connection pooler may refuse startup options, and a session `SET` would
outlive the transaction on a pooled connection), and turns server-side
prepared statements off. The store keeps the `vercel` branch's refusal of
the SQLite fallback on Vercel (`VERCEL`/`VERCEL_ENV` set; a per-instance
`/tmp` file once lost searches there): with no database URL every store
call would raise `storage.StoreUnavailable`, answered 503 with
`error`/`error_cs` (JSON for the page's API calls, plain text for a page).
CodeNOW sets neither variable, so here it never fires.

**The `/results` page, decisions and downloads** work as before: a summary
card with the four counts (Searched `x / total`, Matched, Need validation,
Unmatched), the orange validation stepper (top 3 candidates per near-miss,
**Accept** one or "None of these", saved via `POST /api/decision` ->
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
(`main.routes._display_flags`), as a searched address rarely matches it.
The stepper follows the reviewers' sketch of 2026-10-06: "You searched"
lists every field the row gave (ISIN, country, city, street, postal
code); a note says why the row was not matched (not the plain "not
found"); and each candidate shows legal name, country, city, its name,
city and address scores (`main.routes._review_candidate`: green at the
matcher's gate, red under it, a dash with nothing to compare; the
address score is street and ZIP agreement,
`core/lookup._street_zip_score`, stored as
`CandidateSummary.address_score`), its LEI status, a GLEIF link and
**Accept** (was "Correct match", which read as a verdict). The street and
the overall percent are gone from the table (the overall still orders
the candidates), and the matched-records table shows no overall percent
either (the confidence stays in the downloads). A candidate whose LEI is
not ISSUED shows a red "!" status and "View only" instead of Accept, and
`record_decision` refuses it; a row is "to validate" only with a
candidate it may accept (`core.models.has_acceptable_candidate`), so a
row whose candidates are all stopped is a no-match, and a decision saved
before 2026-10-06 that no longer stands (`core.models.standing_decision`)
is ignored on the page and in the downloads alike.

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
  for input the user must fix. The script keeps every message it writes
  in both languages on its element (`both(key)`, `serverMessage(data)`,
  `showBoth(el, message)`), so a CZ/EN switch translates it too.
  Every new user-visible string needs both languages.
- **URLs.** Every link, asset and form target is built with `url_for`
  (endpoints are `main.<name>`, assets `main.static`), since the app lives
  under `URL_PREFIX`. The page script builds its requests with `appUrl()`
  from the root the server renders into `<html data-app-root>`; a literal
  path starting with "/" in `app.js` would escape the prefix
  (`tests/test_smoke.py` checks).
- **Caching and history.** `/results` and the downloads are sent with
  `Cache-Control: no-store` (`main.routes.no_store_live_pages`, by
  endpoint, as their paths start with `URL_PREFIX`): from the HTTP
  cache, Back showed saved decisions as undecided. Search stays disabled
  once the results page is on its way (re-enabled on a back-forward
  cache restore), and an explicit theme choice stops the page following
  the OS scheme. `tests/test_frontend.py` drives these in Node.
- **Phones.** Below 700 px the stepper's Accept column is not pinned, as
  it covered much of the table at 375 px. On a desktop the candidate
  table fits without scrolling: legal names and score headers wrap.

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
- waitress runs 16 worker threads (`WAITRESS_THREADS` in `src/app.py`): each
  running search keeps one busy with back-to-back `/run` calls, and
  `/health` queues for a free thread, so waitress's default of 4 let four
  searches starve the probe (`tests/test_smoke.py` holds four and checks).
  If the platform starts waitress itself rather than through
  `src/app.py`'s `__main__`, it needs `--threads=16` too. Postgres
  connects give up after 10 s for the same reason.
- The bare prefix (`/lei-lookup`) serves the page itself
  (`strict_slashes=False` on `main.index`): Werkzeug's slash redirect
  would answer with an absolute `http://` URL behind a TLS ingress. The
  root redirect (`/` -> `/lei-lookup/`) is relative. `URL_PREFIX` is
  stripped of whitespace and a trailing slash; one without its leading
  slash still fails loudly at import.
- Unknown from here, check on the platform: whether its ingress passes
  the prefix through (a stripping ingress would loop on the root
  redirect), its read timeout and body limit (nginx defaults of 60 s
  and 1 MB would cut slow `/run` calls and uploads before the app's
  4 MB cap), and a writable temp dir (waitress spools request bodies
  over 512 KB there, and the default SQLite store lives there).
- Upload cap: 4 MB (`MAX_UPLOAD_BYTES`, the app's `MAX_CONTENT_LENGTH`)
  and 100 entities.
- Restored with the move back to CodeNOW: `.codenow.yaml`, the JSON log
  config, the B3 trace-header echo, waitress, the PyCharm/mirrord run
  configs and the Sonar properties file. Dropped: `vercel.json`,
  `.vercelignore`, `.python-version`, `requirements-dev.txt`, and
  `public/` (now the blueprint's `static/`). The 2026-10-09 port left
  out `vercel`'s `HANDOFF.md` too (its open items that apply here are
  under Current state).

### Current state

On 2026-10-09 this branch was brought up to `vercel` 4cdc85c: everything
`vercel` gained since the tree this one was converted from (its commit
c7d6cd2, 2026-09-24) was ported onto the CodeNOW layout, with `vercel`'s
notes, rules and tests adapted to `src/` and the prefix. That covers pasted
rows on the bulk card; the upload guard's stylesheet budget and
uncalculated-formula check, ISO 8859-2 text files and the newer header
rules; the original tool's precision fixes (2026-09-30) with the ISIN
paths' country and rival checks and the alpha-3 and native country
names; OpenFIGI failures pausing the job, and `DeadlineWatch` ending
replies that trickle in past the deadline; the OpenFIGI fallback's
`ISIN_OPENFIGI_MATCH` type, rival check and name gate; the reviewers'
rules of 2026-10-06 (ISSUED-only stops, the OpenFIGI address check,
"spol. s r.o." forms and initials, review candidates sharing a word,
the stepper's scores and Accept); GLEIF's fuzzy-name fallback
(2026-10-09); the no-store results page, messages that follow a CZ/EN
switch and the phone layout; `/admin` paged 5 searches at a time; and
the store's Postgres limits and schema lock. What deliberately differs
from `vercel`:

- `/health` stays on the bare app, dependency free and always UP. On
  `vercel` it says DOWN when no database is configured, which only
  happens there.
- The store's refusal of a SQLite file on Vercel is kept but inert:
  CodeNOW sets neither `VERCEL` nor `VERCEL_ENV`, so with no
  `DATABASE_URL` the store is SQLite at `LEI_DB_PATH` as before.
- The no-store header and the 503 answer to `storage.StoreUnavailable`
  (JSON for the API calls) pick their requests by endpoint
  (`_NO_STORE_ENDPOINTS`, `_API_ENDPOINTS` in `src/main/routes.py`)
  rather than by a root path such as `/results` or `/api/`, as every
  path of the app starts with `URL_PREFIX`. `tests/test_smoke.py`
  checks the no-store header under a prefix even when `URL_PREFIX` is
  unset, as in the build.

The port's adversarial review found one defect both branches had: on
Linux a reply the `DeadlineWatch` cut off read as complete (see
Backend, step 3). It was fixed on `vercel` (4cdc85c) and here alike.

Not checked on CodeNOW yet, as nothing on this branch has been built
there (deploying means pushing to the component's Bitbucket repo): the
real build (its pylint gate and tests run on Python 3.12.6, the local
ones on 3.13) and the route the platform publishes the component under;
whether its ingress passes `/lei-lookup` through and lets a `/run` call
run for up to about 120 s (`RUN_DEADLINE_SECONDS`); and the volume for
`LEI_DB_PATH` (unset, searches live in the temp dir and are lost on
restart). The conversion of 2026-09-24 and the port of 2026-10-09 were
verified locally (the pylint gate, `scripts/check.ps1`, the suite with
and without `URL_PREFIX` - 935 tests after the port - and a waitress
run under `/lei-lookup`).

Open, as on `vercel` (from its `HANDOFF.md` and Current state):

- No rate limit on creating jobs, and `/admin` has no sign-in: anyone
  who reaches the app can fill the store and read every search (an
  ingress rule or a sign-in is the owner's call).
- Recall on real ISIN rows: of the 62 rows of the 2026-10-06 live
  replay, 9 were matched, 21 went to review and 32 ended without a
  match, many saying "No LEI found in the GLEIF database" (mostly US
  small caps and funds). Find out which have an LEI at all and why the
  rest are missed (renamed issuers, OpenFIGI's names, Japanese legal
  names such as Nomura's). Any matcher change needs the offline replay,
  then the live replay.
- Nothing has run on a real Postgres: the Postgres path is checked only
  against a fake psycopg (`tests/test_store_limits.py`).
- A date or boolean in the name column is looked up as its text.
- "Compartment A" vs "B" still collapses: "compartment" is stripped as
  a legal form before its letter is seen.

The checks below ran on `vercel`; the port carries the same matching
code. The replay scripts are in the gitignored `docs/offline-replay/`
and `docs/live-replay/` of the `vercel` checkout, not here.

On 2026-10-06 (afternoon) the reviewers' feedback (a meeting recording and
an e-mail of annotated screenshots) was implemented: only ISSUED LEIs are
ever matched or accepted, the OpenFIGI path checks the address, Czech
"spol. s r.o." forms are stripped, initials distinguish names, review
candidates must share a distinctive word with the name, and the stepper
shows the whole searched row, name/city/address scores and the LEI
status, with Accept (details under Backend). Checked by the offline
replay (unchanged: 1 wrong LEI, 105/109 pairs; only junk review
candidates dropped, review rows 149 -> 143) and by a live replay of the
62 real ISIN rows (the 59 of the earlier live replay plus the e-mail's):
no row gained a match and no LEI changed; 7 left Matched, each for a new
rule (3 LAPSED LEIs, 4 OpenFIGI names whose address did not agree), and
FISS lost its three legal-form-only candidates.

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
`src/main/data/legal_forms.txt`), single-letter serials as distinguishing
tokens (I/V/X, and a letter after a number or a serial word such as
"Fund" or "Series", `core/matcher._significant_tokens`) and the
contradiction cap of 79 (`ADDRESS_CONTRADICTION_CAP`). DUPLICATE and
CANCELLED now count as not maintained, and the ISIN, country and review
changes above came with it. Checked offline by replaying the old tool's
340-case adversarial baseline from its committed GLEIF snapshot (in the
archive, `Desktop/_old/LEI-2026-09-22/tests/golden/offline_cache.jsonl.gz`,
not copied into this repo): wrong LEIs 5 -> 1 (the old tool's own
"Simp" case), the one lost true match recovered, the four wrong-country
ISIN rows now matched through ISIN plus name with `COUNTRY_MISMATCH`, and
105 of its 109 labelled name pairs (was 104). `normalize_name` also drops
invisible format characters before the legal forms (a trailing zero-width
space kept "Allianz SE" from matching) and turns U+0085 into a space
(`core/address._drop_invisible`); the replay is unchanged by it.

Feature-complete and deployable. Single and bulk search run against live
GLEIF through the job endpoints, every search is persisted under a `job_id`
(SQLite, or Postgres when `DATABASE_URL` is set), and the results page,
the manual validation workflow and the CSV/Excel downloads are real.
`tests/` covers the store and the route flows with GLEIF faked; run it
before every change to the web layer. Matching behaviour (thresholds in
`core/constants.py`) is audit-validated and unchanged from the original
tool's CodeNOW version apart from the legal-form whitespace, the
2026-09-30 precision fixes, the OpenFIGI fallback's rival check and name
gate (2026-10-06: offline replay unchanged; a live replay of the 59 ISIN
rows searched in the 30 days before changed only the Genius Sports row),
the reviewers' rules of the same afternoon and the fuzzy-name fallback
above - re-run the matcher audit (at least the offline replay) before
tuning it. The offline replay never reaches the OpenFIGI fallback; the
live replay replays real searches (GLEIF and OpenFIGI calls: ask the
user first).

An optional LLM-assisted step (e.g. helping disambiguate near-misses) is a
possible next addition; it would plug in after `core/lookup.lookup_entity`
returns and must never override the precision-first assertion rules.
