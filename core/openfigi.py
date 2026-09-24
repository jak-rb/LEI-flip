
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
from .gleif import DeadlineExceeded, read_body

logger = logging.getLogger(__name__)


def resolve_isin_to_names(
    isin: str, deadline: Optional[float] = None
) -> list[str]:
    """Resolve an ISIN to every issuer name OpenFIGI knows for it.

    Sends one mapping request and collects the unique ``name`` values
    across all returned entries (the same issuer can appear under
    slightly different names per exchange or share class). Any network
    error, non-200 response, or unparseable or unexpected body returns
    an empty list, so a flaky OpenFIGI never breaks the lookup pipeline.

    Args:
        isin: The normalised (upper-cased, space-free) ISIN.
        deadline: Optional ``time.monotonic()`` value. The request does
            not start after it, its timeout is cut to the time left,
            and the reply is read only until it (see read_body).

    Returns:
        Unique issuer names in the order returned, or an empty list.

    Raises:
        DeadlineExceeded: If the deadline comes before OpenFIGI answers.
            An empty list would pass for "OpenFIGI knows nothing", so
            the lookup is cut off instead and can be retried later.
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
        resp = requests.post(
            OPENFIGI_BASE_URL,
            json=[{"idType": "ID_ISIN", "idValue": isin}],
            headers=headers,
            timeout=timeout,
            stream=True,
        )
        body = read_body(resp, deadline)
        resp.raise_for_status()
        data = json.loads(body)
    except requests.RequestException as e:
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded(
                "OpenFIGI request cut off by the deadline"
            ) from e
        logger.warning("OpenFIGI request failed for ISIN %s: %s", isin, e)
        return []
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

