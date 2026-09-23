# Handoff - 2026-09-23

Work in progress, left for the next Claude session. Read this first,
then CLAUDE.md. Delete this file (and its pointers in CLAUDE.md and
README.md) once the backlog below is done.

## Where things stand

Shipped today on branch `vercel`, all deployed to production
(https://lei-lookup.vercel.app and https://lei-flip.vercel.app):

| Commit | What |
|---|---|
| `fc9fd90` | CSV uploads no longer crash (HTML 500, "The search could not be started") on UTF-16, lone-CR, binary or oversized-cell files |
| `f22c82f` | Every JSON error carries English `error` and Czech `error_cs`; `core.models.InputError`; `serverError()` in `public/app.js` |
| `99b3dc4` | An `.xlsx` saved while a chart sheet was active reads its first worksheet |
| `04dfd20` | `.tsv`/`.txt` uploads (always tab-separated), `.csv` also detects tab; damaged or chart-only `.xlsx` gives a 400 |

- Tests: 21 passing (`.venv/Scripts/python -m pytest -q`).
- One real live search ran fine (job `52a97793...`: Raiffeisenbank a.s.,
  ČEZ, a. s., ISIN US0378331005 - all matched).
- The user's own test file is now `tests/fixtures/test_lei.xlsx`; the
  screenshot of the original bug is
  `tests/fixtures/bug_could_not_start_2026-09-23.png`.

## The crazy test run (Opus ultracode workflow)

- Run `wf_fa931678-843`, launched from session
  `551abfa2-90b0-4c24-8e4e-85e1877b2e58`: 8 areas, 118 checklist cases
  plus about 10 invented cases per tester, 3 verifiers per area, and a
  completeness-critic round. All testers and all 27 verifiers finished;
  only the final synthesis may still have been running at handoff.
- Confirmed = at least 2 of 3 verifiers reproduced it and called it a
  real bug. Keys below are `area#Fn` as in the digest.
- Full digest with every repro and verdict (local only, gitignored):
  `docs/crazy-test-2026-09-23.txt`.
- Raw per-agent results:
  `C:/Users/jakub/.claude/projects/C--Users-jakub-Desktop-LEI/551abfa2-90b0-4c24-8e4e-85e1877b2e58/subagents/workflows/wf_fa931678-843/journal.jsonl`
- Final synthesis, if it finished:
  `C:/Users/jakub/AppData/Local/Temp/claude/C--Users-jakub-Desktop-LEI/551abfa2-90b0-4c24-8e4e-85e1877b2e58/tasks/w6ryckg6d.output`
- Workflow resume is same-session only. Do not re-run the whole test;
  work from the list below, then run a fresh, smaller ultracode
  verification of the fixes.
- Code locations below were reported by the testers; re-check the line
  numbers before editing.

## What to do next, in order

For every fix: write a failing test first, fix, run pytest, then commit,
push `vercel` and `vercel deploy --prod --yes` (standing rule, do not
ask), and re-check production with job-free probes.

### 1. The user's Excel file (they asked for this explicitly)

`tests/fixtures/test_lei.xlsx` is a semicolon CSV that was opened in
Excel with the wrong delimiter: each whole line sits in column A,
split again into column B wherever the street has a comma. The header
is `PARTY_FULL_NAME;"ISIN_IDENT";"COUNTRY_CODE";"ADDR_CITY_NAME";
"ADDR_STREET_NAME";"ADDR_ZIP_CODE"`. Today the app reads only 2 garbage
entities (the header, and FIRY with `;CA 94103"` as its ISIN) and
silently drops the other 6 rows (critic-round#F1, `c01`).

Plan agreed with the user:
- When every non-empty row has its data in column A with semicolons,
  rebuild each line by joining the row's non-empty cells with `,` (the
  text in column B keeps its leading space) and parse it as a
  semicolon CSV.
- Recognise DB-style headers: a second cell containing `isin` (such as
  `ISIN_IDENT`), not only exactly `isin`.
- Expected: 7 entities - CBRE Investment Management Listed Real Assets
  LLC, Real REMAX Group Inc, Abacus Global Management Inc, Longeveron
  Inc, Nomura Holdings INC (twice, JP3046680009 and JP3046710004), and
  FIRY INC. FIRY's source line is itself malformed, so its street is
  `1061 Market St,;CA 94103` and it has no ZIP.
- Add the fixture as a regression test, then run the file ONCE for real
  on the live site through the page (the user asked for this) and show
  them the results table.

### 2. Rows lost silently and header detection (high)

- Any over-long field (name > 500, ISIN or ZIP > 20, ...) makes
  `_rows_to_entities` drop the whole row without a word; a 101-row file
  can even pass the cap (text-formats#F2, xlsx-formats#F3,
  hostile-uploads#F5, critic-round#F1). Location:
  `core/upload.py` `_rows_to_entities` (`except ValidationError:
  continue`). Keep the row (truncate or drop only the bad field) or
  refuse the file with a bilingual message naming the row numbers.
- When every row fails, the message says "No entities found" instead
  of why (text-formats#F8).
- `_drop_header` only looks at the literal first row: a blank or title
  first row makes the header an entity, and a legal 100-entity file
  with a blank top row is refused as 101 (text-formats#F5,
  xlsx-formats#F8, xlsx-formats#F9, hostile-uploads#F7). Header names
  outside the exact word list are searched although Help says header
  names "don't matter" (xlsx-formats#F10).

### 3. Excel export crashes or corrupt files (high)

ASCII control characters (NUL, 0x01-0x1F except tab/LF/CR) anywhere in
stored text make `/download/excel` answer 500; U+FFFE/U+FFFF produce a
corrupt .xlsx. Reachable from uploads and the single form
(xlsx-formats#F4, text-formats#F1, single-form#F1-F3,
hostile-uploads#F1-F2, results-exports#F2). Fix in `core/export.py`
`_sanitize`: strip `openpyxl.cell.cell.ILLEGAL_CHARACTERS_RE` matches
plus U+FFFE/U+FFFF.

### 4. Jobs that can never finish (high)

- Any exception other than `GleifApiError` in a lookup (malformed GLEIF
  reply, odd OpenFIGI data) gives an HTML 500, throws away the rows the
  chunk already finished, and fails on the same entity forever
  (job-runner#F1 `r07`, critic-round#F3 `c03`). `app.py` `run_job`.
- GLEIF answering 200 with a non-JSON body is a 500 instead of the
  bilingual 503 (job-runner#F5): `core/gleif.py`, `resp.json()` outside
  the try that maps errors to `GleifApiError`.
- A GLEIF error that repeats for one entity stalls the whole job as a
  "temporary outage" (critic-round#F4). Idea: after N failed attempts
  store that entity as a failed row with a note and move on.
- GLEIF's 60 requests/minute limit makes a normal bulk search stop on
  "GLEIF unavailable" many times; `Retry-After` is ignored
  (critic-round#F2, `core/gleif.py` backoff).
- One lookup against a GLEIF that times out intermittently can outlast
  Vercel's 300 s (job-runner#F7): the time budget is only checked
  between lookups.

### 5. Decisions API and races (high)

- `/api/decision` answers 500 for a JSON body that is not an object,
  or a list/dict `job_id` or `choice` (results-exports#F1,
  frontend-i18n#F1, single-form#F4). Validate types in `app.py`
  `decision()`.
- `storage.record_decision` rewrites the whole results JSON without a
  guard: a decision racing a `/run` erases the rows `/run` just saved
  and the job never finishes (job-runner#F3, results-exports#F4), and
  two decisions at once lose one (job-runner#F4, results-exports#F3).
  Make it atomic (guard on `searched`, or a row lock).
- Decisions are accepted on rows that already have an algorithmic
  match and override the export; "none" on a confident match
  double-counts the entity and drops its LEI (results-exports#F6,
  frontend-i18n#F3).
- On Postgres a job id containing NUL makes `/results`, downloads,
  `/run` and `/decision` answer 500 (results-exports#F5): validate the
  job id format (32 hex chars) before touching the store.

### 6. Slow or memory-hungry uploads (high/medium)

- A 5 KB .xlsx with one stray cell at XFD1048576 or a forged
  `<dimension>` exhausts memory and time; a shared-strings bomb runs
  for minutes (xlsx-formats#F1, hostile-uploads#F3, hostile-uploads#F4).
  In `_read_xlsx`: `iter_rows(max_col=6)`, stop once more than
  MAX_ENTITIES usable rows are seen, and bound the row count.
- Rows beyond a stale `<dimension>` are silently dropped
  (xlsx-formats#F2): read-only mode trusts the dimension
  (`ws.reset_dimensions()`), which must be combined with the caps above.
- Legal-size (< 4 MiB) uploads take 20-110 s before "Too many entities"
  (text-formats#F4, xlsx-formats#F5, xlsx-formats#F6,
  hostile-uploads#F8): stop parsing after MAX_ENTITIES + 1 entities.
- A long whitespace-heavy single-form name makes one `/run` take
  minutes (single-form#F5): quadratic backtracking in the legal-form
  regexes in `core/address.py` (around lines 88-91). The matcher is
  audit-validated: collapse whitespace and cap the length before
  matching rather than changing the patterns, and re-run the matcher
  audit if matching behaviour changes.

### 7. Medium and low polish (ask the user which to do)

- Excel's `_x000D_` escape is stored and exported literally
  (xlsx-formats#F7): `openpyxl.utils.escape.unescape`.
- Leading-zero postal codes stored as formatted numbers lose the zero
  (xlsx-formats#F11).
- One invalid UTF-8 byte decodes the whole file as cp1250, garbling
  every Czech name (hostile-uploads#F6).
- Country "UK" or "ČR" turns a perfect match into NO_MATCH
  (single-form#F9, `core/address.py` country conversion). Matcher
  territory: audit before changing.
- ISIN is not stripped, so spaces or a whitespace-only ISIN reach GLEIF
  as a country filter; a padded valid ISIN gets a vague error
  (single-form#F7, #F8, #F12, `app.py` `_single_entities`).
- A form field over 500 KB or more than 1000 form parts gets Werkzeug's
  HTML 413 and the page says "That file is too large"
  (single-form#F6, #F11, live-site#F1, #F3, hostile-uploads#F7): raise
  `MAX_FORM_MEMORY_SIZE` / `MAX_FORM_PARTS` or answer 413 as JSON.
- The no-match table's Notes column is English-only in the Czech UI
  (frontend-i18n#F2; the notes come from `core/lookup.py`).
- A file named just ".csv" passes the browser check but the server
  rejects it (frontend-i18n#F5).
- "(, Praha)" is rendered when only a city is given
  (results-exports#F13, frontend-i18n#F7).
- The summary card counts go stale after a decision until reload
  (frontend-i18n#F8); a failed decision save is ignored silently
  (results-exports#F9).
- Cells holding only invisible characters (ZWSP, BOM) become entities
  (xlsx-formats#F14).

### Refuted - not bugs, do not redo

Fewer than 2 of 3 verifiers confirmed these: cp1252 decoded as cp1250
(Czech-first decoding is intended); cp852 files; a stray cp1250 byte in
a UTF-8 file (text-formats#F10 - but the similar hostile-uploads#F6 was
confirmed, see section 7); a mid-file BOM; an unbalanced quote merging
lines; a quoted leading word in a .txt; the Czech header-label list
(text-formats#F9 - the similar xlsx-formats#F10 was confirmed); a
single-column .csv split at commas (documented); null GLEIF legalName
or null record fields; infinite candidate scores on /results; the
browser runner without backoff; a storage error on /run; a
non-serialisable result row; CSV export without a BOM; /admin response
size; booleans as decision index; a "none" decision on a row without
candidates; formula payloads with a leading tab; CR/CRLF in the Excel
export; an out-of-range activeTab; a file named ".xlsx"; an empty chart
sheet (an openpyxl bug); hidden xlsx columns; the `sep=` directive line;
an ASCII-only PDF renamed .txt; OpenFIGI slowness; the anti-formula
apostrophe on re-upload; Safari below 14; the Czech wording review;
GET /api/jobs giving 404 instead of 405.

## Rules for this work

- Tests never call the real GLEIF or OpenFIGI (fake `lookup_entity` and
  `GleifClient` as in `tests/test_app.py`).
- Live-site probes only with inputs the server must reject, or made-up
  job ids; never open `/admin`. Ask before any real live search, except
  the one run of the user's Excel file approved above.
- The user allows Opus 5.5 for ultracode workflows.
