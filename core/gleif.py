
"""Synchronous client for the GLEIF REST API.

Talks to the public GLEIF API (no key required), running several search
strategies per name and merging the de-duplicated candidates. Ported
from the original async (httpx) client and rewritten with requests; the
original's SQLite response cache is intentionally dropped.
"""

import logging
import re
import time
from typing import Optional

import requests
from unidecode import unidecode

from .constants import GLEIF_BASE_URL, REQUEST_TIMEOUT
from .models import GleifAddress, GleifCandidate

logger = logging.getLogger(__name__)

#: How many times a rate-limited or failed request is retried.
MAX_RETRIES = 3

#: Seconds to wait before the first retry; doubled after each attempt.
INITIAL_BACKOFF = 1.0

# Strategy-3 abbreviation expansions, applied before a legalName search.
_RE_LMT = re.compile(r"\bLmt\.?\b", re.IGNORECASE)
_RE_CORP = re.compile(r"\bCorp\.?\b", re.IGNORECASE)
_RE_INC = re.compile(r"\bInc\.?\b", re.IGNORECASE)

# Strategy-4 stripping: parenthetical text and a few common legal forms.
_RE_PARENS = re.compile(r"\(.*?\)")
_RE_LEGAL_FORMS = re.compile(
    r"\b(S\.A\.S\.?|SAS|LLP|LLC|Ltd\.?|Inc\.?|Corp\.?|GmbH|AG|Lmt\.?"
    r"|a\.s\.?|s\.r\.o\.?)\b",
    re.IGNORECASE,
)
_RE_TRAILING_PUNCT = re.compile(r"[,;]+\s*$")
_RE_WHITESPACE = re.compile(r"\s+")


class GleifApiError(Exception):
    """Raised when the GLEIF API is unreachable after all retries."""


def _parse_address(data: dict) -> GleifAddress:
    """Parse a GLEIF address object into a GleifAddress."""
    return GleifAddress(
        country=data.get("country"),
        region=data.get("region"),
        city=data.get("city"),
        postal_code=data.get("postalCode"),
        address_lines=data.get("addressLines", []),
    )


def _collect_names(items: list) -> list[str]:
    """Extract name strings from a GLEIF otherNames-style list."""
    names = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name", "")
            if name:
                names.append(name)
        elif isinstance(item, str):
            names.append(item)
    return names


def _parse_candidate(record: dict) -> GleifCandidate:
    """Parse one GLEIF API record into a GleifCandidate."""
    attrs = record.get("attributes", {})
    entity = attrs.get("entity", {})

    legal_name = entity.get("legalName", {}).get("name", "")
    status = attrs.get("registration", {}).get("status", "UNKNOWN")
    legal_addr = entity.get("legalAddress", {})
    hq_addr = entity.get("headquartersAddress", {})

    # Include transliterated names too (e.g. ASCII forms of
    # non-Latin-script entities).
    other_names = _collect_names(entity.get("otherNames", []))
    other_names += _collect_names(entity.get("transliteratedOtherNames", []))

    return GleifCandidate(
        lei=record.get("id", attrs.get("lei", "")),
        legal_name=legal_name,
        status=status,
        legal_address=_parse_address(legal_addr) if legal_addr else None,
        hq_address=_parse_address(hq_addr) if hq_addr else None,
        other_names=other_names,
    )


def _clean_name(name: str) -> str:
    """Expand common abbreviations and drop diacritics (strategy 3)."""
    cleaned = name.strip()
    cleaned = _RE_LMT.sub("Limited", cleaned)
    cleaned = _RE_CORP.sub("Corporation", cleaned)
    cleaned = _RE_INC.sub("Incorporated", cleaned)
    return unidecode(cleaned)


def _strip_legal_forms(name: str) -> str:
    """Remove parentheses and common legal forms (strategy 4)."""
    stripped = _RE_PARENS.sub("", name).strip()
    stripped = _RE_LEGAL_FORMS.sub("", stripped)
    stripped = _RE_TRAILING_PUNCT.sub("", stripped)
    stripped = _RE_WHITESPACE.sub(" ", stripped).strip()
    return unidecode(stripped)


class GleifClient:
    """Synchronous GLEIF API client with retry and backoff."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT):
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/vnd.api+json"})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def _request(self, path: str, params: dict) -> dict:
        """GET ``path`` with retry and exponential backoff.

        Retries on rate limits (HTTP 429) and transport errors, waiting
        a doubling backoff between attempts. Any failure that outlives
        the retries is raised as GleifApiError, so callers have a single
        exception type to catch.

        Args:
            path: API path appended to the GLEIF base URL.
            params: Query-string parameters.

        Returns:
            The parsed JSON response body.

        Raises:
            GleifApiError: If no successful response is obtained.
        """
        url = GLEIF_BASE_URL + path
        backoff = INITIAL_BACKOFF

        for attempt in range(MAX_RETRIES):
            last = attempt == MAX_RETRIES - 1
            try:
                resp = self._session.get(
                    url, params=params, timeout=self._timeout
                )
            except requests.RequestException as e:
                if last:
                    raise GleifApiError(
                        f"GLEIF API unreachable: {e}"
                    ) from e
                logger.warning(
                    "GLEIF transport error: %s; retrying in %.1fs", e, backoff
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 429:
                if last:
                    raise GleifApiError(
                        "GLEIF API rate-limited after retries"
                    )
                logger.warning(
                    "GLEIF rate limited; retrying in %.1fs (attempt %d/%d)",
                    backoff, attempt + 1, MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                raise GleifApiError(f"GLEIF API error: {e}") from e

            return resp.json()

        # The loop always returns or raises; this guards a logic slip.
        raise GleifApiError("GLEIF API request failed")

    def search_by_name(
        self,
        name: str,
        country: Optional[str] = None,
        page_size: int = 10,
    ) -> list[GleifCandidate]:
        """Search GLEIF by entity name, merging several strategies.

        Runs up to five queries and merges the de-duplicated results:
        full-text on the raw name; legalName on the raw name; legalName
        on a cleaned name (expanded abbreviations, no diacritics);
        legalName on a name with parentheses and legal forms stripped;
        and that stripped name as a country-constrained full-text query.
        A country filter is added to each query when ``country`` is set.

        Args:
            name: The entity name to search for.
            country: Optional ISO alpha-2 country filter.
            page_size: Maximum records to request per query.

        Returns:
            De-duplicated candidates, in the order first seen.
        """
        seen_leis: set[str] = set()
        candidates: list[GleifCandidate] = []

        def add(records: list[dict]) -> None:
            for record in records:
                candidate = _parse_candidate(record)
                if candidate.lei not in seen_leis:
                    candidates.append(candidate)
                    seen_leis.add(candidate.lei)

        size = str(page_size)

        # Strategy 1: full-text search on the raw name.
        params = {"filter[fulltext]": name, "page[size]": size}
        if country:
            params["filter[entity.legalAddress.country]"] = country
        add(self._request("/lei-records", params).get("data", []))

        # Strategy 2: exact legalName filter on the raw name.
        params = {"filter[entity.legalName]": name, "page[size]": size}
        if country:
            params["filter[entity.legalAddress.country]"] = country
        add(self._request("/lei-records", params).get("data", []))

        # Strategy 3: legalName on a cleaned name.
        cleaned = _clean_name(name)
        if cleaned != name:
            params = {"filter[entity.legalName]": cleaned, "page[size]": size}
            if country:
                params["filter[entity.legalAddress.country]"] = country
            add(self._request("/lei-records", params).get("data", []))

        # Strategy 4: legalName on a name with legal forms stripped,
        # plus the same stripped name as country-constrained full-text.
        stripped = _strip_legal_forms(name)
        if stripped and stripped != unidecode(name) and len(stripped) > 3:
            params = {
                "filter[entity.legalName]": stripped,
                "page[size]": size,
            }
            add(self._request("/lei-records", params).get("data", []))

            if country:
                params = {
                    "filter[fulltext]": stripped,
                    "filter[entity.legalAddress.country]": country,
                    "page[size]": size,
                }
                add(self._request("/lei-records", params).get("data", []))

        return candidates

    def search_by_name_no_country(
        self, name: str, page_size: int = 10
    ) -> list[GleifCandidate]:
        """Search by name with no country filter, as a fallback."""
        return self.search_by_name(name, country=None, page_size=page_size)

    def search_by_isin(self, isin: str) -> list[GleifCandidate]:
        """Full-text search for records mentioning an ISIN.

        A broad full-text search for the ISIN string, used by the ISIN
        corroboration paths (a name/HQ near-miss is promoted only when
        this search returns the same LEI). For an authoritative ISIN ->
        LEI resolution use ``lookup_by_isin`` instead.

        Args:
            isin: The (normalised) ISIN to search for.

        Returns:
            The parsed candidate records (may be empty).
        """
        params = {"filter[fulltext]": isin, "page[size]": "5"}
        data = self._request("/lei-records", params)
        return [_parse_candidate(record) for record in data.get("data", [])]

    def lookup_by_isin(self, isin: str) -> list[GleifCandidate]:
        """Resolve an ISIN to its LEI record(s) via GLEIF's ISIN filter.

        Uses GLEIF's authoritative, maintained ISIN -> LEI mapping
        (``filter[isin]``), not the broad full-text search. An ISIN is
        globally unique, so this normally returns zero or one record; the
        ISIN-only lookup treats a single hit as a confident match.

        Args:
            isin: The (normalised) ISIN to resolve.

        Returns:
            The parsed candidate records (0 or 1 in practice; more only
            if GLEIF's own mapping is ambiguous).
        """
        params = {"filter[isin]": isin, "page[size]": "5"}
        data = self._request("/lei-records", params)
        return [_parse_candidate(record) for record in data.get("data", [])]

