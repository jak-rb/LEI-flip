
"""ISIN-based LEI resolution.

Ported from the original, with notes translated to English. The lookup
pipeline calls this when a name + address search did not yield a
confident match: a validated ISIN can find a LEI directly, corroborate
an HQ-only / strong-name near-miss, or - as a last resort - be mapped to
an issuer name via OpenFIGI and re-searched in GLEIF (see core/openfigi.py).
Precision-first: every path still requires some name agreement.
"""

import logging
import math
import re
from typing import Optional

from .address import country_to_iso
from .constants import (
    ADDRESS_CORROBORATION_MIN,
    CITY_MATCH_THRESHOLD,
    ISIN_NAME_THRESHOLD,
    NAME_MATCH_THRESHOLD,
    OPENFIGI_NAME_THRESHOLD,
    STREET_MATCH_THRESHOLD,
)
from .gleif import GleifClient
from .matcher import address_match_score, best_name_score, name_similarity
from .models import (
    GleifAddress,
    GleifCandidate,
    InputEntity,
    LookupResult,
    MatchType,
    WarningCode,
    is_issued,
)
from .openfigi import resolve_isin_to_names

logger = logging.getLogger(__name__)

#: ISO 6166: 2-letter country + 9 alphanumeric NSIN + 1 numeric check.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

#: Minimum name score for the ISIN to corroborate a name near-miss.
_NAME_CORROBORATION_MIN = 85


def normalize_isin(isin: Optional[str]) -> str:
    """Upper-case and strip all whitespace from a raw ISIN string."""
    if not isin:
        return ""
    return re.sub(r"\s+", "", isin).upper()


def is_valid_isin(isin: str) -> bool:
    """Validate an ISIN: ISO 6166 structure plus Luhn check digit.

    Letters expand to numbers (A=10 .. Z=35) and the Luhn checksum over
    the resulting digit string must be ``0 mod 10``. Expects an already
    normalised (upper-cased, space-free) input.

    Args:
        isin: The normalised ISIN.

    Returns:
        True if both the format and the check digit are valid.
    """
    if not _ISIN_RE.match(isin):
        return False
    digits = "".join(str(int(char, 36)) for char in isin)
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _address_agrees(
    entity: InputEntity, candidate: GleifCandidate
) -> Optional[str]:
    """Which of a candidate's addresses agrees with the entity's, if any.

    The gate of a full match (see core/lookup.py), on the legal address
    first, then the headquarters: no other country, a city of at least
    CITY_MATCH_THRESHOLD, an address score of at least
    ADDRESS_CORROBORATION_MIN, and no street and ZIP that both
    contradict the candidate's.

    Returns:
        "legal", "hq", or None when neither agrees (or the entity gives
        no address to check).
    """
    for addr_type, address in (
        ("legal", candidate.legal_address), ("hq", candidate.hq_address),
    ):
        score, details = address_match_score(entity, candidate, addr_type)
        contradiction = (
            bool(entity.street) and bool(entity.zip_code)
            and bool(address and address.address_lines)
            and bool(address and address.postal_code)
            and details.get("street_score", 0) < STREET_MATCH_THRESHOLD
            and details.get("zip_score", 0) == 0
        )
        if (
            details.get("country_match") is not False
            and details.get("city_score", 0) >= CITY_MATCH_THRESHOLD
            and score >= ADDRESS_CORROBORATION_MIN
            and not contradiction
        ):
            return addr_type
    return None


def _countries(candidate: GleifCandidate) -> set[str]:
    """The countries of a candidate's legal and HQ addresses."""
    return {
        address.country
        for address in (candidate.legal_address, candidate.hq_address)
        if address and address.country
    }


def _other_country(entity: InputEntity, candidate: GleifCandidate) -> bool:
    """Whether the entity's recognised country is not the candidate's."""
    country = country_to_iso(entity.country)
    countries = _countries(candidate)
    return bool(country and countries) and country not in countries


def _country_warnings(
    entity: InputEntity, candidate: GleifCandidate
) -> list[str]:
    """COUNTRY_UNVERIFIED or COUNTRY_MISMATCH for an ISIN-based match.

    No warning only when the entity's country is recognised and is the
    country of the candidate's legal or HQ address: a filled-in country
    that nothing was checked against must not read as verified.
    """
    if _other_country(entity, candidate):
        return [WarningCode.COUNTRY_MISMATCH.value]
    if not (country_to_iso(entity.country) and _countries(candidate)):
        return [WarningCode.COUNTRY_UNVERIFIED.value]
    return []


def _fmt(address: Optional[GleifAddress]) -> Optional[str]:
    """Format a GLEIF address, or None when absent."""
    return address.format() if address else None


def _legal_parts(candidate: GleifCandidate) -> dict:
    """Structured legal-address fields for a matched LookupResult."""
    la = candidate.legal_address
    return {
        "gleif_legal_country": la.country if la else None,
        "gleif_legal_city": la.city if la else None,
        "gleif_legal_street": la.street() if la else None,
    }


def _isin_returns_lei(
    isin_candidates: list[GleifCandidate], lei: str
) -> bool:
    """Whether the ISIN search returned the given LEI."""
    return any(candidate.lei == lei for candidate in isin_candidates)


def resolve_via_isin(
    entity: InputEntity,
    client: GleifClient,
    hq_candidate: Optional[GleifCandidate] = None,
    name_candidate: Optional[GleifCandidate] = None,
) -> tuple[Optional[LookupResult], list[GleifCandidate]]:
    """Resolve or corroborate a LEI via the entity's ISIN.

    Tries, in order: a direct GLEIF ISIN hit whose name matches; then an
    HQ near-miss the ISIN points to; then a strong name near-miss the
    ISIN points to; then the OpenFIGI fallback. Only an ISSUED LEI is
    ever asserted (see core.models.is_issued).

    Args:
        entity: The entity being looked up (must carry an ISIN).
        client: An open GLEIF client.
        hq_candidate: The best HQ-only near-miss candidate, if any.
        name_candidate: The best strong-name near-miss candidate, if any.

    Returns:
        A ``(result, to_review)`` tuple. ``result`` is a positive
        LookupResult; or a NO_MATCH saying why the OpenFIGI fallback's
        candidate was not asserted, with that candidate in
        ``to_review``; or None when the ISIN did not resolve.
    """
    if not entity.isin:
        return None, []
    isin = normalize_isin(entity.isin)
    if not is_valid_isin(isin):
        logger.info(
            "Skipping ISIN resolution for %s: %r is not a valid ISIN",
            entity.name, entity.isin,
        )
        return None, []

    isin_candidates = client.search_by_isin(isin)
    rivals = [c for c in (hq_candidate, name_candidate) if c is not None]

    direct = _direct_isin_match(
        entity, isin, isin_candidates, rivals=rivals,
    )
    if direct:
        return direct, []

    if hq_candidate is not None:
        confirmed = _isin_confirms_hq(
            entity, isin, isin_candidates, hq_candidate
        )
        if confirmed:
            return confirmed, []

    if name_candidate is not None and name_candidate is not hq_candidate:
        confirmed = _isin_confirms_name(
            entity, isin, isin_candidates, name_candidate
        )
        if confirmed:
            return confirmed, []

    return _openfigi_fallback(entity, isin, client, rivals)


def _direct_isin_match(
    entity: InputEntity,
    isin: str,
    isin_candidates: list[GleifCandidate],
    rivals: Optional[list[GleifCandidate]] = None,
) -> Optional[LookupResult]:
    """Accept the best GLEIF ISIN hit whose name matches the entity.

    A hit is not asserted when its LEI is not ISSUED, when it lies in
    another country than the one the entity gives, or when the name
    search found another LEI whose name matches better (``rivals``): an
    ISIN of a related entity (a parent, a fund of the named manager)
    must not override the named one. Such a row stays for review with
    its name candidates.
    """
    rival_scores = [
        (rival.lei, best_name_score(entity, rival))
        for rival in rivals or []
    ]
    best: Optional[tuple[float, GleifCandidate]] = None
    for candidate in isin_candidates:
        if not is_issued(candidate.status):
            continue
        ns = best_name_score(entity, candidate)
        if ns < ISIN_NAME_THRESHOLD:
            continue
        if _other_country(entity, candidate):
            continue
        if any(
            lei != candidate.lei and score > ns
            for lei, score in rival_scores
        ):
            continue
        if best is None or ns > best[0]:
            best = (ns, candidate)
    if best is None:
        return None

    ns, candidate = best
    warnings = [
        WarningCode.ISIN_ONLY.value,
        WarningCode.UNVERIFIED_ADDRESS.value,
    ]
    warnings += _country_warnings(entity, candidate)
    logger.info("ISIN_GLEIF_MATCH: %s -> %s", isin, candidate.lei)
    return LookupResult(
        lei=candidate.lei,
        lei_status=candidate.status,
        match_type=MatchType.ISIN_GLEIF_MATCH,
        confidence=min(75 + ns * 0.15, 95),
        gleif_legal_name=candidate.legal_name,
        gleif_legal_address=_fmt(candidate.legal_address),
        gleif_hq_address=_fmt(candidate.hq_address),
        notes=(
            f"ISIN {isin} found in GLEIF; LEI assigned despite the "
            f"address not matching."
        ),
        match_details={"name_score": round(ns, 1)},
        warnings=warnings,
        **_legal_parts(candidate),
    )


def _isin_confirms_hq(
    entity: InputEntity,
    isin: str,
    isin_candidates: list[GleifCandidate],
    hq_candidate: GleifCandidate,
) -> Optional[LookupResult]:
    """Promote an ISSUED HQ near-miss when the ISIN points to its LEI."""
    if not is_issued(hq_candidate.status) or not _isin_returns_lei(
        isin_candidates, hq_candidate.lei
    ):
        return None
    warnings = [WarningCode.HQ_ONLY_MATCH.value]
    warnings += _country_warnings(entity, hq_candidate)
    return LookupResult(
        lei=hq_candidate.lei,
        lei_status=hq_candidate.status,
        match_type=MatchType.ISIN_MATCH,
        confidence=80,
        gleif_legal_name=hq_candidate.legal_name,
        gleif_legal_address=_fmt(hq_candidate.legal_address),
        gleif_hq_address=_fmt(hq_candidate.hq_address),
        notes=(
            f"By ISIN {isin} the issuer is {hq_candidate.legal_name} - "
            f"matches the HQ address in GLEIF."
        ),
        warnings=warnings,
        **_legal_parts(hq_candidate),
    )


def _isin_confirms_name(
    entity: InputEntity,
    isin: str,
    isin_candidates: list[GleifCandidate],
    name_candidate: GleifCandidate,
) -> Optional[LookupResult]:
    """Promote an ISSUED strong name near-miss the ISIN points to."""
    ns = best_name_score(entity, name_candidate)
    if ns < _NAME_CORROBORATION_MIN or not is_issued(name_candidate.status):
        return None
    if not _isin_returns_lei(isin_candidates, name_candidate.lei):
        return None
    confidence = min(70 + ns * 0.1, 85)
    warnings = [
        WarningCode.ISIN_ONLY.value,
        WarningCode.UNVERIFIED_ADDRESS.value,
    ]
    warnings += _country_warnings(entity, name_candidate)
    return LookupResult(
        lei=name_candidate.lei,
        lei_status=name_candidate.status,
        match_type=MatchType.ISIN_MATCH,
        confidence=confidence,
        gleif_legal_name=name_candidate.legal_name,
        gleif_legal_address=_fmt(name_candidate.legal_address),
        gleif_hq_address=_fmt(name_candidate.hq_address),
        notes=(
            f"Strong name match ({math.floor(round(ns, 1))}%) with "
            f"{name_candidate.legal_name}. ISIN {isin} confirms the LEI."
        ),
        match_details={"name_score": round(ns, 1)},
        warnings=warnings,
        **_legal_parts(name_candidate),
    )


def _openfigi_fallback(
    entity: InputEntity,
    isin: str,
    client: GleifClient,
    rivals: list[GleifCandidate],
) -> Optional[LookupResult]:
    """Map the ISIN to a name via OpenFIGI, then re-search GLEIF.

    The last-resort ISIN path, tried only after the direct and near-miss
    paths miss. Precision guard: a candidate is accepted only when BOTH
    the input name and the OpenFIGI-resolved name fuzzy-match it, so
    OpenFIGI can never invent a match. Nothing but names links the
    candidate here, so the input name must clear the usual
    NAME_MATCH_THRESHOLD, which a name with a token the other lacks
    ("Genius Sports" against "Genius Sports Media", "2023-A" against
    "2023-B") never does; OpenFIGI's names, often cut short, need only
    OPENFIGI_NAME_THRESHOLD.
    The re-search is by name only, so a candidate in another country
    than the entity's is skipped, and only a unique best candidate (by
    both scores) is asserted: same-named banks and groups exist in
    several countries, and GLEIF's result order must not pick one.
    As on the direct path, a candidate is skipped too when the name
    search found another LEI whose name matches better (``rivals``):
    OpenFIGI names the ISIN's issuer, which may be a relative of the
    entity named. A best candidate whose LEI is not ISSUED is never
    asserted, only offered for review with a note saying so.
    OpenFIGI's name only finds the candidate; a name is no proof, so the
    best one is asserted only when the address the entity gives agrees
    with its legal or headquarters address (see _address_agrees), and
    is otherwise offered for review, as reviewers asked on 2026-10-06.
    The result says the LEI came from that name (ISIN_OPENFIGI_MATCH,
    ISIN_VIA_OPENFIGI): GLEIF may have no record of the ISIN, so a
    user checking a match "by ISIN" there finds nothing.

    Args:
        entity: The entity being looked up.
        isin: The normalised, validated ISIN.
        client: An open GLEIF client.
        rivals: The near-misses of the name search (see
            _direct_isin_match).

    Returns:
        A ``(result, to_review)`` tuple: a positive LookupResult; a
        NO_MATCH naming the candidate whose address did not agree,
        which ``to_review`` holds; or ``(None, [])`` if nothing
        corroborated.
    """
    rival_scores = [
        (rival.lei, best_name_score(entity, rival)) for rival in rivals
    ]
    # (input score + OpenFIGI score, input score, candidate, name)
    passing: list[tuple[float, float, GleifCandidate, str]] = []
    for figi_name in resolve_isin_to_names(isin, deadline=client.deadline):
        logger.info(
            "OpenFIGI resolved ISIN %s -> %r; re-searching GLEIF",
            isin, figi_name,
        )
        candidates = client.search_by_name(figi_name, page_size=5)
        for candidate in candidates:
            # Stopped records (see is_issued) take part too: an equal
            # one makes the best ambiguous, and one that is best is
            # shown for review but never asserted.
            input_ns = best_name_score(entity, candidate)
            figi_ns = name_similarity(figi_name, candidate.legal_name)
            if (
                input_ns < NAME_MATCH_THRESHOLD
                or figi_ns < OPENFIGI_NAME_THRESHOLD
                or _other_country(entity, candidate)
                or any(
                    lei != candidate.lei and score > input_ns
                    for lei, score in rival_scores
                )
            ):
                continue
            passing.append(
                (input_ns + figi_ns, input_ns, candidate, figi_name)
            )
    if not passing:
        return None, []

    best = max(passing, key=lambda entry: entry[0])
    if any(
        entry[0] == best[0] and entry[2].lei != best[2].lei
        for entry in passing
    ):
        logger.info("OpenFIGI fallback for %s is ambiguous", isin)
        return None, []

    total, input_ns, candidate, figi_name = best
    found = f"ISIN {isin} resolved via OpenFIGI to the issuer {figi_name}"
    if not is_issued(candidate.status):
        return LookupResult(
            match_type=MatchType.NO_MATCH,
            lei_status=candidate.status,
            gleif_legal_name=candidate.legal_name,
            gleif_legal_address=_fmt(candidate.legal_address),
            gleif_hq_address=_fmt(candidate.hq_address),
            notes=(
                f"{found}, and GLEIF has {candidate.legal_name} (LEI "
                f"{candidate.lei}) under that name, but its LEI status is "
                f"{candidate.status} - the LEI cannot be used and was not "
                f"assigned."
            ),
            match_details={"name_score": round(input_ns, 1)},
            warnings=[WarningCode.ISIN_VIA_OPENFIGI.value],
        ), [candidate]
    agrees = _address_agrees(entity, candidate)
    if agrees is None:
        # The check needs a town (as a full match does): with only a
        # street or ZIP given, the note must not call them a mismatch.
        if not (entity.town or entity.street or entity.zip_code):
            reason = "no address was given to check it against"
        elif not entity.town:
            reason = "no town was given to check its address against"
        else:
            reason = "its address does not match the one given"
        logger.info(
            "OpenFIGI candidate %s for %s left for review: address",
            candidate.lei, isin,
        )
        return LookupResult(
            match_type=MatchType.NO_MATCH,
            lei_status=candidate.status,
            gleif_legal_name=candidate.legal_name,
            gleif_legal_address=_fmt(candidate.legal_address),
            gleif_hq_address=_fmt(candidate.hq_address),
            notes=(
                f"{found}, and GLEIF has {candidate.legal_name} under "
                f"that name, but {reason} - LEI not assigned. Please "
                f"review."
            ),
            match_details={"name_score": round(input_ns, 1)},
            warnings=[WarningCode.ISIN_VIA_OPENFIGI.value],
        ), [candidate]

    confidence = min(65 + total * 0.05, 85)
    warnings = [WarningCode.ISIN_VIA_OPENFIGI.value]
    if agrees == "hq":
        warnings.append(WarningCode.HQ_ONLY_MATCH.value)
    warnings += _country_warnings(entity, candidate)
    which = "legal" if agrees == "legal" else "headquarters"
    logger.info("ISIN_OPENFIGI_MATCH: %s -> %s", isin, candidate.lei)
    return LookupResult(
        lei=candidate.lei,
        lei_status=candidate.status,
        match_type=MatchType.ISIN_OPENFIGI_MATCH,
        confidence=confidence,
        gleif_legal_name=candidate.legal_name,
        gleif_legal_address=_fmt(candidate.legal_address),
        gleif_hq_address=_fmt(candidate.hq_address),
        notes=(
            f"{found}; the LEI was found in GLEIF by that name, not by "
            f"the ISIN, and its {which} address matches the one given."
        ),
        match_details={"name_score": round(input_ns, 1)},
        warnings=warnings,
        **_legal_parts(candidate),
    ), []


def resolve_isin_only(
    entity: InputEntity, client: GleifClient
) -> Optional[tuple[LookupResult, list[GleifCandidate]]]:
    """Resolve a name-less input by its ISIN, authoritatively.

    Used when the user supplied an ISIN but no name. Resolution is
    authoritative: GLEIF's own ISIN -> LEI mapping (``lookup_by_isin``).
    Exactly one hit with an ISSUED LEI is auto-asserted as a confident
    match; one with any other status, an invalid ISIN or an ambiguous
    multi-LEI mapping returns a NO_MATCH, the first and the last with
    the records the ISIN maps to, for review.

    Args:
        entity: The name-less entity being looked up (carries an ISIN).
        client: An open GLEIF client.

    Returns:
        A ``(result, to_review)`` tuple: the confident match or a
        NO_MATCH, and the GLEIF records to offer for review (empty for
        a match or an invalid ISIN). None when the ISIN is valid but
        absent from GLEIF's mapping - the signal for the caller to try
        the softer OpenFIGI review (see lookup.py).
    """
    isin = normalize_isin(entity.isin)
    if not is_valid_isin(isin):
        return LookupResult(
            match_type=MatchType.NO_MATCH,
            notes="No name was given and the ISIN is not valid.",
        ), []

    candidates = client.lookup_by_isin(isin)
    if len(candidates) == 1 and not is_issued(candidates[0].status):
        candidate = candidates[0]
        return LookupResult(
            match_type=MatchType.NO_MATCH,
            lei_status=candidate.status,
            gleif_legal_name=candidate.legal_name,
            gleif_legal_address=_fmt(candidate.legal_address),
            gleif_hq_address=_fmt(candidate.hq_address),
            notes=(
                f"ISIN {isin} maps in GLEIF to {candidate.legal_name} "
                f"(LEI {candidate.lei}), but its LEI status is "
                f"{candidate.status} - the LEI cannot be used and was not "
                f"assigned."
            ),
        ), candidates
    if len(candidates) == 1:
        return _isin_only_match(entity, isin, candidates[0]), []

    if not candidates:
        # Valid ISIN, but GLEIF's authoritative mapping has no record of
        # it. Signal the caller to try the softer OpenFIGI review path.
        return None

    return LookupResult(
        match_type=MatchType.NO_MATCH,
        notes=(
            f"ISIN {isin} maps to multiple LEIs in GLEIF; manual review "
            f"is needed."
        ),
    ), candidates


def _isin_only_match(
    entity: InputEntity, isin: str, candidate: GleifCandidate
) -> LookupResult:
    """Auto-assert a confident match from an authoritative ISIN mapping."""
    warnings = [
        WarningCode.NAME_UNVERIFIED.value,
        WarningCode.ISIN_ONLY.value,
        WarningCode.UNVERIFIED_ADDRESS.value,
    ]
    warnings += _country_warnings(entity, candidate)
    logger.info("ISIN_ONLY_MATCH: %s -> %s", isin, candidate.lei)
    return LookupResult(
        lei=candidate.lei,
        lei_status=candidate.status,
        match_type=MatchType.ISIN_GLEIF_MATCH,
        confidence=95,
        gleif_legal_name=candidate.legal_name,
        gleif_legal_address=_fmt(candidate.legal_address),
        gleif_hq_address=_fmt(candidate.hq_address),
        notes=(
            f"LEI resolved from ISIN {isin} via GLEIF's authoritative "
            f"ISIN mapping; no name was provided to cross-check."
        ),
        warnings=warnings,
        **_legal_parts(candidate),
    )

