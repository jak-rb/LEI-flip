
"""Fuzzy matching logic for entity names and addresses."""

import logging
from typing import Optional

from rapidfuzz import fuzz

from .address import (
    country_to_iso,
    extract_zip,
    normalize_address_part,
    normalize_name,
)
from .constants import (
    AMBIGUOUS_NAME_CAP,
    CITY_MATCH_THRESHOLD,
    NAME_MATCH_THRESHOLD,
    STREET_MATCH_THRESHOLD,
    TOKEN_COVER_THRESHOLD,
)
from .models import GleifCandidate, InputEntity

logger = logging.getLogger(__name__)

# A significant token must fuzzy-match a counterpart at or above this
# score to count as "covered". Tolerant of spelling variants and
# inflections (e.g. plural forms) but not of unrelated words.
_TOKEN_COVER_THRESHOLD = TOKEN_COVER_THRESHOLD

# Connectives / articles that carry no entity-distinguishing signal.
# Applied after normalize_name() has lower-cased and stripped
# diacritics and legal forms. "&" maps to "and" before tokenizing, then
# dropped here. "und" is German "and"; "et" is French. The Nordic "og"
# needs no entry - it is stripped upstream as a legal form.
_NAME_STOPWORDS = frozenset({
    "and", "und", "the", "of", "for", "von", "van", "de", "der", "den",
    "die", "das", "la", "le", "el", "du", "des", "et", "y", "a",
})


def _significant_tokens(normalized: str) -> list[str]:
    """Entity-distinguishing tokens of an already-normalized name."""
    # Single-character fragments are dropped EXCEPT digit-bearing ones:
    # serially-numbered entity families (e.g. "Tesco Property Finance 1
    # PLC" vs "... 3 PLC") are distinguished ONLY by that serial, so
    # dropping it would collapse them into a confident wrong match.
    # Keeping any token containing a digit preserves the serial as a
    # distinguishing token. Legal forms are already removed upstream.
    cleaned = normalized.replace("&", " and ").replace("-", " ")
    tokens = []
    for token in cleaned.split():
        if token in _NAME_STOPWORDS:
            continue
        if len(token) >= 2 or any(c.isdigit() for c in token):
            tokens.append(token)
    return tokens


def _token_covered(token: str, others: list[str]) -> bool:
    """Whether ``token`` has a fuzzy counterpart among ``others``."""
    return any(
        fuzz.ratio(token, o) >= _TOKEN_COVER_THRESHOLD for o in others
    )


def name_similarity(input_name: str, gleif_name: str) -> float:
    """Score how similarly two names denote the same entity (0-100).

    Precision-first: a high score is returned ONLY when the two
    normalized names are token-set-equivalent (every distinguishing
    token on each side has a fuzzy counterpart on the other, order
    independent). partial_ratio is deliberately excluded - it scored a
    generic input 100 against any longer name that merely contained it
    ("Allianz" vs "Allianz Technology SE"). Either side carrying a
    distinguishing token the other lacks is capped below the gate, so
    subset / substring traps and shared-generic-word pairs ("Apex
    Capital" vs "Summit Capital") cannot pass, while legal-form,
    abbreviation, diacritic, word-order, and connective variants of the
    SAME entity still score high.

    Args:
        input_name: The user-provided entity name.
        gleif_name: A candidate name from GLEIF (legal or other name).

    Returns:
        A similarity score from 0 (no match) to 100 (token-equivalent).
    """
    n1 = normalize_name(input_name)
    n2 = normalize_name(gleif_name)

    if not n1 or not n2:
        return 0.0

    # Normalize connectives/hyphens so "Johnson & Johnson"/"Coca-Cola"
    # compare like their spelled-out / spaced forms.
    s1 = n1.replace("&", " and ").replace("-", " ")
    s2 = n2.replace("&", " and ").replace("-", " ")

    # token_set_ratio handles reordering; ratio handles spelling
    # closeness. partial_ratio is excluded - it is the substring bug.
    base = float(max(fuzz.token_set_ratio(s1, s2), fuzz.ratio(s1, s2)))

    t1 = _significant_tokens(n1)
    t2 = _significant_tokens(n2)
    if not t1 or not t2:
        # One side reduced to only connectives/legal forms - fall back
        # to the plain string ratio, conservative for such inputs.
        return float(fuzz.ratio(s1, s2))

    uncovered = [t for t in t1 if not _token_covered(t, t2)]
    uncovered += [t for t in t2 if not _token_covered(t, t1)]
    if uncovered:
        # A distinguishing token has no counterpart, so the names may
        # denote different entities. Cap below the gate; reflect the
        # true string closeness.
        return min(float(fuzz.ratio(s1, s2)), AMBIGUOUS_NAME_CAP)

    return base


def best_name_score(entity: InputEntity, candidate: GleifCandidate) -> float:
    """Best name score across a candidate's legal and other names.

    Args:
        entity: The entity being looked up.
        candidate: A GLEIF candidate record.

    Returns:
        The highest name_similarity score over all of the candidate's
        names (0-100).
    """
    scores = [name_similarity(entity.name, candidate.legal_name)]

    for other_name in candidate.other_names:
        scores.append(name_similarity(entity.name, other_name))

    return max(scores)


def city_similarity(
    input_city: Optional[str], gleif_city: Optional[str]
) -> float:
    """Fuzzy-compare two city names.

    Args:
        input_city: City from the input entity (may be None).
        gleif_city: City from a GLEIF address (may be None).

    Returns:
        A similarity score from 0 to 100; 0 if either side is empty.
    """
    c1 = normalize_address_part(input_city)
    c2 = normalize_address_part(gleif_city)

    if not c1 or not c2:
        return 0.0

    # Handle compound cities like "Hradec Kralove, Plotiste nad Labem"
    # by also comparing the first part (main city).
    c1_main = c1.split(",")[0].strip()
    c2_main = c2.split(",")[0].strip()

    return max(
        fuzz.token_set_ratio(c1, c2),
        fuzz.token_set_ratio(c1_main, c2_main),
        fuzz.partial_ratio(c1_main, c2_main),
    )


def street_similarity(
    input_street: Optional[str], gleif_street: Optional[str]
) -> float:
    """Fuzzy-compare two street addresses.

    Args:
        input_street: Street from the input entity (may be None).
        gleif_street: Street from a GLEIF address (may be None).

    Returns:
        A similarity score from 0 to 100; 0 if either side is empty.
    """
    s1 = normalize_address_part(input_street)
    s2 = normalize_address_part(gleif_street)

    if not s1 or not s2:
        return 0.0

    return max(
        fuzz.token_set_ratio(s1, s2),
        fuzz.partial_ratio(s1, s2),
    )


def _gleif_street(candidate: GleifCandidate, addr_type: str) -> str:
    """Street lines of the candidate's legal or HQ address."""
    addr = (
        candidate.legal_address
        if addr_type == "legal"
        else candidate.hq_address
    )
    if not addr or not addr.address_lines:
        return ""
    return ", ".join(addr.address_lines)


def _gleif_city(
    candidate: GleifCandidate, addr_type: str
) -> Optional[str]:
    """City of the candidate's legal or HQ address."""
    addr = (
        candidate.legal_address
        if addr_type == "legal"
        else candidate.hq_address
    )
    return addr.city if addr else None


def _gleif_country(
    candidate: GleifCandidate, addr_type: str
) -> Optional[str]:
    """ISO country of the candidate's legal or HQ address."""
    addr = (
        candidate.legal_address
        if addr_type == "legal"
        else candidate.hq_address
    )
    return addr.country if addr else None


def _gleif_zip(
    candidate: GleifCandidate, addr_type: str
) -> Optional[str]:
    """Postal code of the candidate's legal or HQ address."""
    addr = (
        candidate.legal_address
        if addr_type == "legal"
        else candidate.hq_address
    )
    return addr.postal_code if addr else None


def zip_similarity(
    input_zip: Optional[str], gleif_zip: Optional[str]
) -> float:
    """Compare two ZIP codes after normalization.

    Args:
        input_zip: ZIP from the input entity (may be None).
        gleif_zip: ZIP from a GLEIF address (may be None).

    Returns:
        100 for an exact match, 80 when one is a prefix of the other,
        else 0 (also 0 if either side is empty).
    """
    z1 = extract_zip(input_zip)
    z2 = extract_zip(gleif_zip)

    if not z1 or not z2:
        return 0.0

    # Exact match after normalization.
    if z1 == z2:
        return 100.0

    # One is a prefix of the other (e.g. "1010" vs "1010 Wien").
    if z1.startswith(z2) or z2.startswith(z1):
        return 80.0

    return 0.0


def address_match_score(
    entity: InputEntity, candidate: GleifCandidate, addr_type: str
) -> tuple[float, dict]:
    """Compute an address match score (0-100) for a candidate.

    Args:
        entity: The entity being looked up.
        candidate: A GLEIF candidate record.
        addr_type: Which candidate address to score, "legal" or "hq".

    Returns:
        A ``(score, details)`` tuple. ``details`` carries the per-field
        sub-scores and a ``country_match`` flag.
    """
    input_country = country_to_iso(entity.country)
    gleif_country = _gleif_country(candidate, addr_type)

    # Country MUST match.
    if input_country and gleif_country and input_country != gleif_country:
        return 0.0, {"country_match": False}

    city_score = city_similarity(
        entity.town, _gleif_city(candidate, addr_type)
    )
    street_score = street_similarity(
        entity.street, _gleif_street(candidate, addr_type)
    )
    z_score = zip_similarity(
        entity.zip_code, _gleif_zip(candidate, addr_type)
    )

    # Country is pass/fail above; city, street, and zip are weighted.
    if z_score > 0:
        overall = city_score * 0.40 + street_score * 0.35 + z_score * 0.25
    else:
        # No ZIP data available - fall back to city + street only.
        overall = city_score * 0.5 + street_score * 0.5

    if input_country and gleif_country:
        country_match = input_country == gleif_country
    else:
        country_match = None

    details = {
        "country_match": country_match,
        "city_score": city_score,
        "street_score": street_score,
        "zip_score": z_score,
        "overall": overall,
    }

    return overall, details

