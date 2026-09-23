
"""Tunable parameters for the matching pipeline.

These were environment-driven (pydantic-settings) in the original tool.
For this internal tool the values never need per-deployment tuning, so
they live here as plain module constants - the single source of truth.

The thresholds are AUDIT-VALIDATED (matcher-audit, fresh-GLEIF run
2026-06-07). Changing any of them alters match behaviour and bypasses
that validation - re-run the matcher audit after tuning before trusting
results.
"""

#: GLEIF REST API base URL.
GLEIF_BASE_URL = "https://api.gleif.org/api/v1"

#: HTTP request timeout in seconds, applied to GLEIF calls. GLEIF
#: normally answers within a second or two; a short timeout leaves a
#: /run call's time budget room for the retries (see core/gleif.py).
REQUEST_TIMEOUT = 10.0

#: OpenFIGI mapping endpoint. Resolves an ISIN to its issuer name(s) as
#: a last-resort ISIN fallback (see core/openfigi.py). An OpenFIGI API
#: key is optional and read from the OPENFIGI_API_KEY environment
#: variable (never hardcoded); without it the client runs keyless.
OPENFIGI_BASE_URL = "https://api.openfigi.com/v3/mapping"

#: HTTP request timeout in seconds for OpenFIGI calls. Kept separate
#: from REQUEST_TIMEOUT: OpenFIGI can be slow, and this fallback is not
#: on the critical path, so it gets its own budget (still cut to the
#: time a /run call has left).
OPENFIGI_TIMEOUT = 15.0

#: Minimum name-similarity score (0-100) for a candidate to clear the
#: name gate.
NAME_MATCH_THRESHOLD = 75

#: Minimum city-similarity score (0-100) for an address to count as
#: city-confirmed.
CITY_MATCH_THRESHOLD = 70

#: Minimum street-similarity score (0-100) for an address to count as
#: street-confirmed.
STREET_MATCH_THRESHOLD = 55

#: Score cap applied when two names share a stem but each carries a
#: distinguishing token the other lacks (e.g. "Allianz" vs "Allianz
#: Technology SE"). MUST stay below NAME_MATCH_THRESHOLD so such
#: ambiguous pairs can never clear the gate (precision-first). Enforced
#: by the assert below.
AMBIGUOUS_NAME_CAP = 70.0

#: Minimum score for a significant token to count as "covered" by a
#: counterpart token on the other side.
TOKEN_COVER_THRESHOLD = 85

#: Minimum name score to accept an ISIN-based match (used in step 5.7).
ISIN_NAME_THRESHOLD = 50

#: Minimum fuzzy-match score (0-100) for the OpenFIGI ISIN fallback:
#: BOTH the input name AND the OpenFIGI-resolved name must score at
#: least this against a GLEIF candidate before its LEI is accepted. This
#: double-match guard (core/isin.py) stops OpenFIGI inventing a match.
OPENFIGI_NAME_THRESHOLD = 65

#: If two or more distinct LEIs clear the FULL_MATCH gate within this
#: many confidence points of the winner, the result is flagged
#: AMBIGUOUS_MATCH.
AMBIGUITY_CONFIDENCE_DELTA = 2.0

#: GLEIF registration statuses meaning the LEI is no longer maintained.
LAPSED_STATUSES = frozenset(
    {"LAPSED", "RETIRED", "ANNULLED", "MERGED", "TRANSFERRED"}
)


# The precision guarantee depends on ambiguous name pairs staying below
# the gate. Fail loudly at import time if a future edit breaks it.
assert AMBIGUOUS_NAME_CAP < NAME_MATCH_THRESHOLD, (
    f"AMBIGUOUS_NAME_CAP ({AMBIGUOUS_NAME_CAP}) must stay below "
    f"NAME_MATCH_THRESHOLD ({NAME_MATCH_THRESHOLD}); otherwise ambiguous "
    "name pairs could clear the match gate."
)

