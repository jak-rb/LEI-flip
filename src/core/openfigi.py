
"""OpenFIGI client: resolve an ISIN to its issuer name(s).

OpenFIGI (Bloomberg's free identifier service) maps an ISIN to the
issuer's name. core/isin.py re-searches that name in GLEIF as a
last-resort ISIN fallback. Ported from the original async (httpx)
client and rewritten with requests; the original's response cache is
intentionally dropped.

An OpenFIGI API key is optional: when the OPENFIGI_API_KEY environment
variable is set it is sent as the X-OPENFIGI-APIKEY header to raise the
rate limit, and without it the client runs keyless (fine at this tool's
volume).
"""

import json
import logging
import os
import time
from typing import Optional

import requests

from .constants import OPENFIGI_BASE_URL, OPENFIGI_TIMEOUT
from .gleif import (
    DeadlineExceeded,
    DeadlineWatch,
    _retry_after,
    read_body,
    watched_session,
)

logger = logging.getLogger(__name__)

#: Seconds a rate limit is taken to last when OpenFIGI's reply does not
#: say (keyless, OpenFIGI allows some 25 requests a minute).
DEFAULT_RETRY_AFTER = 10.0


class OpenFigiUnavailable(Exception):
    """OpenFIGI failed in a way worth trying again later.

    A rate limit (HTTP 429), a server error or no answer at all. An
    empty result would pass for "OpenFIGI knows nothing", and the row
    stored on it would never be looked up again.

    Attributes:
        retry_after: For a rate limit, the seconds to wait before the
            next request; None for any other failure.
    """

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


def _post(url: str, **kwargs) -> requests.Response:
    """POST through a session whose requests a DeadlineWatch can cut off."""
    with watched_session() as session:
        return session.post(url, **kwargs)


def resolve_isin_to_names(
    isin: str, deadline: Optional[float] = None
) -> list[str]:
    """Resolve an ISIN to every issuer name OpenFIGI knows for it.

    Sends one mapping request and collects the unique ``name`` values
    across all returned entries (the same issuer can appear under
    slightly different names per exchange or share class). A refused
    request (4xx other than 429) or an unparseable or unexpected body
    returns an empty list; a rate limit, a server error or no answer
    raises OpenFigiUnavailable, so the lookup is tried again later
    instead of being stored as "OpenFIGI knows nothing".

    Args:
        isin: The normalised (upper-cased, space-free) ISIN.
        deadline: Optional ``time.monotonic()`` value. The request does
            not start after it, its timeout is cut to the time left,
            and the reply is read only until it (see read_body and
            DeadlineWatch).

    Returns:
        Unique issuer names in the order returned, or an empty list.

    Raises:
        DeadlineExceeded: If the deadline comes before OpenFIGI answers.
            An empty list would pass for "OpenFIGI knows nothing", so
            the lookup is cut off instead and can be retried later.
        OpenFigiUnavailable: If OpenFIGI rate-limits the request,
            answers with a server error, or cannot be reached.
    """
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OPENFIGI_API_KEY", "")
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    timeout = OPENFIGI_TIMEOUT
    if deadline is not None:
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            raise DeadlineExceeded("OpenFIGI request cut off by the deadline")
        timeout = min(timeout, time_left)

    try:
        with DeadlineWatch(deadline):
            resp = _post(
                OPENFIGI_BASE_URL,
                json=[{"idType": "ID_ISIN", "idValue": isin}],
                headers=headers,
                timeout=timeout,
                stream=True,
            )
            body = read_body(resp, deadline)
    except requests.RequestException as e:
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded(
                "OpenFIGI request cut off by the deadline"
            ) from e
        raise OpenFigiUnavailable(f"OpenFIGI unreachable: {e}") from e

    status = resp.status_code
    if status == 429:
        raise OpenFigiUnavailable(
            "OpenFIGI rate-limited the request",
            retry_after=_retry_after(resp, DEFAULT_RETRY_AFTER),
        )
    if status >= 500:
        raise OpenFigiUnavailable(f"OpenFIGI answered HTTP {status}")
    if status != 200:
        logger.warning(
            "OpenFIGI refused the request for ISIN %s: HTTP %s", isin, status
        )
        return []
    try:
        data = json.loads(body)
    except ValueError as e:
        logger.warning("OpenFIGI returned a non-JSON body for %s: %s", isin, e)
        return []

    first = data[0] if isinstance(data, list) and data else None
    entries = first.get("data") if isinstance(first, dict) else None
    if not isinstance(entries, list):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str):
            continue
        name = name.strip()
        if name and name.lower() not in seen:
            names.append(name)
            seen.add(name.lower())

    if names:
        logger.info(
            "OpenFIGI resolved ISIN %s -> %s (%d names)",
            isin, names[0], len(names),
        )
    return names

