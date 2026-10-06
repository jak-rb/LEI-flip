# Handoff - 2026-10-06

Where the 2026-10-06 session left off, for the next one. Like the former
HANDOFF.md, retire this file once its open items are settled.

## Done (branch `vercel`, all deployed to production)

| Commit | Change |
|---|---|
| `d662df0` | A match made through OpenFIGI is typed `ISIN_OPENFIGI_MATCH` and flagged "ISIN via OpenFIGI" (was "Matched by ISIN"), and its note says the LEI was found in GLEIF by the issuer name; every match other than a full match shows its note on the results page. A colleague could not find FIRY INC's ISIN (US83067L2088) in GLEIF: it is not there. |
| `167ad34` | The OpenFIGI fallback skips a candidate when the name search found another LEI that matches the typed name better, as the direct ISIN path does. |
| `042e3e2` | The results page flags a GLEIF street "c/o" someone as "Agent's address (c/o)": for many US companies GLEIF has only their registered agent (FIRY: CSC in Wilmington, as legal and HQ address). Page only; the downloads are unchanged. |
| `f23b9b4` | The OpenFIGI fallback holds the typed name to the usual gate of 75 (OpenFIGI's own name keeps its 65). |

792 tests pass, and the offline replay is unchanged by all four.

## How the last two were checked

The offline replay never reaches the OpenFIGI fallback (none of its 340
cases ends there), so a live replay was added in the gitignored
`docs/live-replay/` (see its README): it re-runs the ISIN rows of the last
30 days' production searches against live GLEIF and OpenFIGI. On
2026-10-06 (59 rows) no LEI differed from what colleagues had got, the
rival check changed no row, and the name gate changed one: "Genius Sports
Ltd." (GG00BMF1JR16) had been given GENIUS SPORTS MEDIA INC.
(254900MUCRCHF4X7HD76, lapsed), as GLEIF has no record of Genius Sports
Limited; it now goes to review. The replay calls GLEIF and OpenFIGI live,
so ask the user before running it.

## Open

For the user:
- Tell the colleague who searched "Genius Sports Ltd." on 2026-10-06 that
  the LEI they got is wrong (above). Their stored search keeps it until
  they search again.

Candidates for a next session:
1. Recall on real ISIN rows: 45 of the 59 end without a match, 24 of them
   with candidates to review, and 34 say "No LEI found in the GLEIF
   database" (mostly US small caps and funds). Find out which have an LEI
   at all and why the rest are missed (renamed issuers, OpenFIGI's names,
   Japanese legal names such as Nomura's). Data:
   `docs/live-replay/replay_results_2026-10-06.json`. Any matcher change
   needs the offline replay, then the live replay.
2. "Investec Fund Series" (GB00B12GL767) is matched through GLEIF's ISIN
   data to "GLOBAL GOLD FUND", a sub-fund, at a name score of 62.7 (the
   direct ISIN path accepts 50). Arguable rather than wrong, as the user
   named the umbrella; decide whether that path should assert so loose a
   name.
3. `main` and `codenow` (the CodeNOW layout) have none of the fixes since
   2026-09-24. The desktop app's "Create PR" (`vercel` into `main`) must
   not be merged, as the layouts differ; port the fixes only if the
   CodeNOW build still matters.
4. Still open from before (CLAUDE.md, "Current state"): no rate limit on
   job creation and an unauthenticated `/admin`, no run on Python 3.12 or
   on a real Postgres, a date or boolean name searched as its text, and
   "Compartment A"/"B" collapsing.
