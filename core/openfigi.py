
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

import logging
import os

import requests

from .constants import OPENFIGI_BASE_URL, OPENFIGI_TIMEOUT

logger = logging.getLogger(__name__)


def resolve_isin_to_names(isin: str) -> list[str]:
    """Resolve an ISIN to every issuer name OpenFIGI knows for it.

    Sends one mapping request and collects the unique ``name`` values
    across all returned entries (the same issuer can appear under
    slightly different names per exchange or share class). Any network
    error, non-200 response, or unparseable body returns an empty list,
    so a flaky OpenFIGI never breaks the lookup pipeline.

    Args:
        isin: The normalised (upper-cased, space-free) ISIN.

    Returns:
        Unique issuer names in the order returned, or an empty list.
    """
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("OPENFIGI_API_KEY", "")
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key

    try:
        resp = requests.post(
            OPENFIGI_BASE_URL,
            json=[{"idType": "ID_ISIN", "idValue": isin}],
            headers=headers,
            timeout=OPENFIGI_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("OpenFIGI request failed for ISIN %s: %s", isin, e)
        return []
    except ValueError as e:
        logger.warning("OpenFIGI returned a non-JSON body for %s: %s", isin, e)
        return []

    if not data or not isinstance(data, list):
        return []

    entries = data[0].get("data", []) if isinstance(data[0], dict) else []
    names: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        name = entry.get("name", "").strip()
        if name and name.lower() not in seen:
            names.append(name)
            seen.add(name.lower())

    if names:
        logger.info(
            "OpenFIGI resolved ISIN %s -> %s (%d names)",
            isin, names[0], len(names),
        )
    return names
