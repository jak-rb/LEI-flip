
"""Synchronous client for the GLEIF REST API.

Talks to the public GLEIF API (no key required), running several search
strategies per name and merging the de-duplicated candidates. Ported
from the original async (httpx) client and rewritten with requests; the
original's SQLite response cache is intentionally dropped.
"""

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
import urllib3
from unidecode import unidecode

from .constants import GLEIF_BASE_URL, REQUEST_TIMEOUT
from .models import GleifAddress, GleifCandidate

logger = logging.getLogger(__name__)

#: How many times a rate-limited or failed request is retried.
MAX_RETRIES = 3

#: Seconds to wait before the first retry; doubled after each attempt.
INITIAL_BACKOFF = 1.0

#: Longest Retry-After, in seconds, taken at its word. GLEIF limits
#: requests per minute, so a real wait is about a minute at most; a
#: longer (or absurd) value is cut to this, so it stays a sane number.
MAX_RETRY_AFTER = 300.0

# 4xx statuses that report a passing state (Request Timeout, Too
# Early) rather than a refused query, so they are retried like a
# server error.
_TRANSIENT_CLIENT_ERRORS = (408, 425)

# Most bytes taken from a reply body at a time (see read_body).
_READ_SIZE = 64 * 1024

# The query of GleifClient.is_answering: a full-text search like the
# first one of every name search, for a single record.
_PROBE_PARAMS = {"filter[fulltext]": "GLEIF", "page[size]": "1"}

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


class GleifServerError(GleifApiError):
    """GLEIF kept answering with a server error or an unreadable body.

    A server error is a 5xx, 408 or 425. GLEIF itself is reachable
    here, so the fault may lie with this one query rather than with
    the whole service; GleifClient.is_answering tells which.
    """


class GleifQueryError(GleifApiError):
    """GLEIF refused the query (a 4xx other than 408, 425 or 429).

    Asking again at once cannot help. The refusal may be of this one
    query, or of every request (a block of the app, a moved API);
    GleifClient.is_answering tells which.
    """


class GleifRateLimited(GleifApiError):
    """GLEIF is rate-limiting, and waiting does not fit the deadline.

    Attributes:
        retry_after: Seconds GLEIF asked to wait before asking again.
    """

    def __init__(self, retry_after: float):
        super().__init__(
            f"GLEIF API rate-limited; retry in {retry_after:.0f}s"
        )
        self.retry_after = retry_after


class DeadlineExceeded(Exception):
    """The caller's deadline came before the lookup could finish.

    Not a failure of the lookup: it was cut off, and can simply be
    started again later.
    """


def _retry_after(resp: requests.Response, default: float) -> float:
    """Seconds a 429 reply asks to wait (its Retry-After), else default.

    Retry-After holds either a number of seconds or an HTTP date. The
    wait is capped at MAX_RETRY_AFTER.
    """
    value = resp.headers.get("Retry-After", "").strip()
    # isdigit() alone also accepts digits float() rejects, such as a
    # Latin-1 superscript two.
    if value.isascii() and value.isdigit():
        return min(float(value), MAX_RETRY_AFTER)
    # A date with an out-of-range year, second or zone offset makes
    # the parser raise OverflowError rather than ValueError.
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def read_body(resp: requests.Response, deadline: Optional[float]) -> bytes:
    """A streamed reply's body, read until it ends or the deadline.

    requests' timeout bounds each socket read, not the whole reply, so
    a body that trickles in byte by byte could outlast the deadline by
    far. Each read here takes what one socket read brings, and the
    deadline is checked in between: reading stops at most one request
    timeout after it. The reply is closed either way.

    Args:
        resp: A reply requested with ``stream=True``.
        deadline: Optional ``time.monotonic()`` value; None means none.

    Raises:
        DeadlineExceeded: If the deadline passes before the body ends.
        requests.ConnectionError: If the connection fails mid-body.
    """
    chunks = []
    try:
        while True:
            chunk = resp.raw.read1(_READ_SIZE, decode_content=True)
            if not chunk:
                break
            chunks.append(chunk)
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("Reply cut off by the deadline")
    except (urllib3.exceptions.HTTPError, OSError) as e:
        # What requests would raise for the same failure while it
        # reads the body itself.
        raise requests.ConnectionError(e) from e
    finally:
        resp.close()
    return b"".join(chunks)


def _json_object(body: bytes) -> Optional[dict]:
    """The body's JSON object, or None if it holds anything else."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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
    """Synchronous GLEIF API client with retry and backoff.

    Attributes:
        deadline: Optional ``time.monotonic()`` value. No request or
            retry starts after it, and each request's timeout is cut
            to the time left, so the caller's time limit holds even
            when GLEIF is slow. None (the default) means no deadline.
        rate_limit_waits: The seconds of each rate-limit wait (a 429's
            Retry-After) the client slept, oldest first. The caller
            may empty it, as the job runner does before each lookup,
            to see the waits of one lookup alone.
    """

    def __init__(self, timeout: float = REQUEST_TIMEOUT):
        self._timeout = timeout
        self.deadline: Optional[float] = None
        self.rate_limit_waits: list[float] = []
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/vnd.api+json"})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self._session.close()

    def _time_left(self) -> float:
        """Seconds until the deadline (infinite when there is none)."""
        if self.deadline is None:
            return math.inf
        return self.deadline - time.monotonic()

    def _cut_off(self) -> Exception:
        """The error for running out of time before a request is done.

        Whether the time went on waiting out rate limits is the
        caller's to judge, from ``rate_limit_waits``.
        """
        return DeadlineExceeded("GLEIF request cut off by the deadline")

    def _pause(self, seconds: float) -> None:
        """Sleep before a retry unless the deadline comes first."""
        if seconds >= self._time_left():
            raise self._cut_off()
        time.sleep(seconds)

    def _request(
        self, path: str, params: dict, attempts: int = MAX_RETRIES
    ) -> dict:
        """GET ``path`` with retry and exponential backoff.

        Retries transport errors and server errors (HTTP 5xx, 408 and
        425, or a body that is not a JSON object) after a doubling
        backoff, and a rate limit (HTTP 429) after the wait its
        Retry-After asks for. Any other 4xx is not retried. Every GLEIF
        failure is raised as a GleifApiError or one of its subclasses,
        so callers have a single exception type to catch. No attempt
        or retry starts after the deadline, each attempt's timeout is
        cut to the time left, and the reply is read only until the
        deadline (see read_body).

        Args:
            path: API path appended to the GLEIF base URL.
            params: Query-string parameters.
            attempts: How many times the request is sent at most.

        Returns:
            The parsed JSON response body.

        Raises:
            GleifQueryError: If GLEIF refuses the query (a 4xx other
                than 408, 425 or 429).
            GleifServerError: If GLEIF keeps answering with a server
                error or a body that is not a JSON object.
            GleifRateLimited: If GLEIF keeps rate-limiting, or the wait
                it asks for does not fit before the deadline.
            GleifApiError: If GLEIF stays unreachable.
            DeadlineExceeded: If the deadline comes first.
        """
        url = GLEIF_BASE_URL + path
        backoff = INITIAL_BACKOFF

        for attempt in range(attempts):
            last = attempt == attempts - 1
            time_left = self._time_left()
            if time_left <= 0:
                raise self._cut_off()
            try:
                resp = self._session.get(
                    url, params=params,
                    timeout=min(self._timeout, time_left), stream=True,
                )
                body = read_body(resp, self.deadline)
            except requests.RequestException as e:
                # Past the deadline, the likely cause is the timeout
                # that was cut to fit it: a cut-off, not an outage.
                if self._time_left() <= 0:
                    raise self._cut_off() from e
                if last:
                    raise GleifApiError(
                        f"GLEIF API unreachable: {e}"
                    ) from e
                logger.warning(
                    "GLEIF transport error: %s; retrying in %.1fs", e, backoff
                )
                self._pause(backoff)
                backoff *= 2
                continue

            status = resp.status_code
            if status == 429:
                wait = _retry_after(resp, backoff)
                if last or wait >= self._time_left():
                    raise GleifRateLimited(wait)
                logger.warning(
                    "GLEIF rate limited; retrying in %.1fs (attempt %d/%d)",
                    wait, attempt + 1, attempts,
                )
                self.rate_limit_waits.append(wait)
                time.sleep(wait)
                backoff *= 2
                continue

            if (
                400 <= status < 500
                and status not in _TRANSIENT_CLIENT_ERRORS
            ):
                raise GleifQueryError(
                    f"GLEIF API refused the query: HTTP {status}"
                )
            data = _json_object(body) if status < 400 else None
            if data is not None:
                return data
            problem = (
                f"HTTP {status}" if status >= 400
                else "a body that is not a JSON object"
            )
            if last:
                raise GleifServerError(f"GLEIF API answered with {problem}")
            logger.warning(
                "GLEIF API answered with %s; retrying in %.1fs",
                problem, backoff,
            )
            self._pause(backoff)
            backoff *= 2

        # The loop always returns or raises; this guards a logic slip.
        raise GleifApiError("GLEIF API request failed")

    def is_answering(self) -> bool:
        """Whether GLEIF answers a plain search right now.

        Sends one small full-text search, the kind every name search
        starts with, and does not retry it. After a query failed, this
        tells a failure of that query (GLEIF answers here) from GLEIF
        failing every request: an outage, a block of the app, a moved
        API.

        Returns:
            True if GLEIF answered it with a JSON object.

        Raises:
            GleifRateLimited: If GLEIF answers with a rate limit, which
                tells nothing about the failed query.
            DeadlineExceeded: If the deadline comes first.
        """
        try:
            self._request("/lei-records", _PROBE_PARAMS, attempts=1)
        except GleifRateLimited:
            raise
        except GleifApiError:
            return False
        return True

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

