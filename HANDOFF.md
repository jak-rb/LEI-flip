# Handoff - 2026-10-06

Where the 2026-10-06 sessions left off, for the next one. Like the former
HANDOFF.md, retire this file once its open items are settled.

## Done (branch `vercel`, all deployed to production)

Morning:

| Commit | Change |
|---|---|
| `d662df0` | A match made through OpenFIGI is typed `ISIN_OPENFIGI_MATCH` and flagged "ISIN via OpenFIGI" (was "Matched by ISIN"), and its note says the LEI was found in GLEIF by the issuer name; every match other than a full match shows its note on the results page. A colleague could not find FIRY INC's ISIN (US83067L2088) in GLEIF: it is not there. |
| `167ad34` | The OpenFIGI fallback skips a candidate when the name search found another LEI that matches the typed name better, as the direct ISIN path does. |
| `042e3e2` | The results page flags a GLEIF street "c/o" someone as "Agent's address (c/o)": for many US companies GLEIF has only their registered agent (FIRY: CSC in Wilmington, as legal and HQ address). Page only; the downloads are unchanged. |
| `f23b9b4` | The OpenFIGI fallback holds the typed name to the usual gate of 75 (OpenFIGI's own name keeps its 65). |

Afternoon: the reviewers' feedback (the last ten minutes of a status
meeting's recording, and an e-mail of annotated screenshots) in one
commit (see `git log`):

- Only an ISSUED LEI is ever matched or accepted. Any other status is a
  stop on every path: shown in red with "!", "View only" instead of
  Accept, refused by `/api/decision`; a row whose candidates are all
  stopped is a no-match whose note names the LEI and its status.
- The OpenFIGI path asserts only when the address given agrees with the
  candidate's legal or HQ address; otherwise the row goes to review, the
  note naming the issuer OpenFIGI gave.
- Names: Czech "spol. s r.o." / "a. s." / "společnost s ručením
  omezeným" forms are stripped; review candidates must share a
  distinctive word with the name (not just "spol. s r.o."); initials
  inside a name ("EURO F.D. HOLDINGS") tell names apart.
- Stepper: "You searched" shows every field given (ISIN, street, ZIP
  too), a note says why the row was not matched, and the table shows
  name / city / address scores (green, red, or a dash) and the LEI status
  instead of the street and the overall percent; "Correct match" is now
  "Accept". The matched-records table lost its overall percent too
  (follow-up commit, at the user's request).

Checks: offline replay identical to its expectation (only junk review
candidates dropped); a live replay of the 62 real ISIN rows (the 59
below plus the e-mail's) changed no LEI and added no match, while 7 rows
left Matched, each for a new rule (Billington, LIPOCINE, Nova Minerals:
LAPSED; Redwire, FIRY x3: OpenFIGI address). The live run also found
that skipping stopped records weakened the OpenFIGI fallback's ambiguity
check ("X-Energy Inc"); they count there again.

Then an adversarial review (5 area reviewers, a skeptic per finding)
confirmed 17 findings, all fixed in a third commit with a regression
test each (877 tests; the new tests fail on the code before it). The
one high: a clean match on a stopped LEI handed the match to a far
worse ISSUED record (now only full matches near the best compete).
Others: undotted "spol s r o" spellings, review candidates at the name
gate dropped when split differently ("Raiffeisen Bank"), rows of only
stopped records saying "No LEI found" (now "No usable LEI found in
GLEIF. The closest records cannot be used: ..."), spaced initials,
mid-name "N.A.", "A.B.C." vs "ABCD", score display and old stored rows.
A second round (fix confirmers plus fresh reviewers of the fix diff)
found two of them only partly fixed and six slips in the fixes (two of
them new wrong-LEI paths: "L.P. Holdings" scoring 100 against "Holdings"
and "Firma B a. s." against "FIRMA BAS a.s."); all fixed in a fourth
commit, again test-first (891 tests).

## How the morning's last two were checked

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
- Searches stored before the afternoon's change keep their old verdicts
  (a LAPSED LEI or an OpenFIGI name match in Matched records) until they
  are searched again or expire (by 2026-11-05); only decisions on stopped
  candidates are ignored on the page and in the downloads. Reviewers
  should search their test files again.

Candidates for a next session:
1. Recall on real ISIN rows: now 9 of the 62 are matched, 21 go to review
   and 32 end without a match, many saying "No LEI found in the GLEIF
   database" (mostly US small caps and funds). Find out which have an LEI
   at all and why the rest are missed (renamed issuers, OpenFIGI's names,
   Japanese legal names such as Nomura's). Any matcher change needs the
   offline replay, then the live replay.
2. ~~"Investec Fund Series" matched to its sub-fund through GLEIF's ISIN
   data~~ settled by the reviewers: a GLEIF ISIN hit stands even when the
   address differs; only the OpenFIGI path must check the address.
3. `main` and `codenow` (the CodeNOW layout) have none of the fixes since
   2026-09-24. The desktop app's "Create PR" (`vercel` into `main`) must
   not be merged, as the layouts differ; port the fixes only if the
   CodeNOW build still matters.
4. Still open from before (CLAUDE.md, "Current state"): no rate limit on
   job creation and an unauthenticated `/admin`, no run on Python 3.12 or
   on a real Postgres, a date or boolean name searched as its text, and
   "Compartment A"/"B" collapsing.
