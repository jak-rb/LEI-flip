
"""Single-entity lookup pipeline: search GLEIF, score, classify.

Ported from the original async pipeline and rewritten synchronous, with
the result notes translated to English. Classification is
precision-first: only a name + legal-address agreement (or an ISIN that
resolves/corroborates, see core/isin.py) asserts a LEI; weaker hits are
returned as NO_MATCH with the near-miss details for manual review.
"""

import logging
from typing import Optional

from .address import country_to_iso
from .constants import AMBIGUITY_CONFIDENCE_DELTA, LAPSED_STATUSES
from .gleif import GleifClient
from .isin import normalize_isin, resolve_isin_only, resolve_via_isin
from .matcher import (
    CITY_MATCH_THRESHOLD,
    NAME_MATCH_THRESHOLD,
    STREET_MATCH_THRESHOLD,
    address_match_score,
    best_name_score,
    name_similarity,
)
from .models import (
    CandidateSummary,
    GleifCandidate,
    InputEntity,
    LookupResult,
    MatchType,
    WarningCode,
)
from .openfigi import resolve_isin_to_names

logger = logging.getLogger(__name__)

#: Minimum address score (0-100) for an address to corroborate a name.
ADDRESS_CORROBORATION_MIN = 40

#: Name score at/above which a candidate is a "strong" name hit.
STRONG_NAME_SCORE = 85

#: Name score for the lone-very-strong-hit name-only heuristic.
VERY_STRONG_NAME_SCORE = 92

#: How many runner-up candidates to surface for manual review (the
#: detail page's validation stepper lets the user confirm one of these).
CLOSEST_CANDIDATE_LIMIT = 3

#: Display-only weights for a candidate's overall match percent. They
#: blend name and address agreement for the validation table and never
#: gate a match (unlike the audit-validated thresholds in constants.py).
#: The 60/40 split mirrors the 2:1 name-to-address ratio in a matched
#: result's confidence bonus.
OVERALL_NAME_WEIGHT = 0.6
OVERALL_ADDRESS_WEIGHT = 0.4


def _overall_match(name_score: float, address_score: float) -> float:
    """Blend name and address scores into a review overall percent.

    Always weighted, so an unverified address earns none of its 40%: a
    perfect name with no address confirmed reads 60, not 100. This keeps
    the percent honest about how much of the match is actually verified.
    """
    return (
        name_score * OVERALL_NAME_WEIGHT
        + address_score * OVERALL_ADDRESS_WEIGHT
    )


def _apply_common_warnings(
    result: LookupResult, entity: InputEntity
) -> LookupResult:
    """Attach warnings that depend only on result and input state."""
    warnings = list(result.warnings)

    status = result.lei_status
    if status and status.upper() in LAPSED_STATUSES:
        if WarningCode.LAPSED_STATUS.value not in warnings:
            warnings.append(WarningCode.LAPSED_STATUS.value)

    if any(v == "fail" for v in result.check_status.values()):
        if WarningCode.CHECK_FAILED.value not in warnings:
            warnings.append(WarningCode.CHECK_FAILED.value)

    if not entity.country:
        if WarningCode.COUNTRY_UNVERIFIED.value not in warnings:
            warnings.append(WarningCode.COUNTRY_UNVERIFIED.value)

    result.warnings = warnings
    return result


def _no_match(notes: str) -> LookupResult:
    """Build a bare NO_MATCH result carrying only an explanatory note."""
    return LookupResult(match_type=MatchType.NO_MATCH, notes=notes)


def _isin_country(isin: Optional[str]) -> Optional[str]:
    """The ISO country code an ISIN starts with, if one is present."""
    # Anything but two letters (a stray space, digits) would only reach
    # GLEIF as a country filter nobody can match.
    prefix = normalize_isin(isin)[:2]
    if len(prefix) == 2 and prefix.isascii() and prefix.isalpha():
        return prefix
    return None


def lookup_entity(
    entity: InputEntity, client: GleifClient
) -> tuple[LookupResult, list[CandidateSummary]]:
    """Run the name + address matching pipeline for one entity.

    Searches GLEIF by name (narrowing by country when known), scores
    every candidate with the matcher, and classifies the outcome
    precision-first: a confident FULL_MATCH asserts the LEI, while
    weaker name-only or HQ-only hits stay NO_MATCH unless the entity's
    ISIN resolves or corroborates them (see core/isin.py). An ISIN-only
    input (no name) is resolved authoritatively by its ISIN instead
    (see core/isin.resolve_isin_only).

    Args:
        entity: The entity to look up (a name or an ISIN is required;
            the other fields are optional).
        client: An open GLEIF client.

    Returns:
        A ``(result, closest)`` tuple: the chosen LookupResult plus up
        to two runner-up candidates (by name score) for manual review.

    Raises:
        GleifApiError: If GLEIF cannot be reached. The caller renders
            this as the "service unavailable" state.
        DeadlineExceeded: If the client's deadline comes first.
    """
    logger.info(
        "Looking up: %s (country: %s, ISIN: %s)",
        entity.name, entity.country, entity.isin,
    )

    # ISIN-only input: with no name to search or score by, resolve the
    # LEI authoritatively from the ISIN; when GLEIF's mapping has no
    # record of it, fall back to a softer OpenFIGI review (both below).
    if not entity.name:
        result = resolve_isin_only(entity, client)
        if result is not None:
            return result, []
        return _isin_only_openfigi_review(entity, client)

    iso_country = country_to_iso(entity.country)
    isin_country = _isin_country(entity.isin)

    # Search by name; if a country-constrained query is empty, retry
    # with the ISIN-derived country, then with no country filter.
    candidates = client.search_by_name(entity.name, country=iso_country)
    if not candidates and isin_country and isin_country != iso_country:
        logger.info("Retrying with ISIN country %s.", isin_country)
        candidates = client.search_by_name(
            entity.name, country=isin_country
        )
    if not candidates and (iso_country or isin_country):
        logger.info("Retrying with no country filter.")
        candidates = client.search_by_name_no_country(entity.name)

    if not candidates:
        logger.info("No GLEIF candidates for %s", entity.name)
        result = (
            resolve_via_isin(entity, client)
            or _no_match("No LEI found in the GLEIF database.")
        )
        return result, []

    result = _classify_candidates(entity, candidates, client)
    closest = _closest_candidates(entity, candidates, result.lei)
    return result, closest


def _classify_candidates(
    entity: InputEntity, candidates: list[GleifCandidate], client: GleifClient
) -> LookupResult:
    """Score the candidates and return the precision-first verdict."""
    best_full_match: Optional[LookupResult] = None
    full_match_leis: list[tuple[str, float]] = []
    best_hq_candidate: Optional[GleifCandidate] = None
    best_hq_score = 0.0
    best_hq_name_score = 0.0
    best_hq_details: dict = {}
    best_name_candidate: Optional[GleifCandidate] = None
    best_name_score_val = 0.0

    for candidate in candidates:
        ns = best_name_score(entity, candidate)
        if ns < NAME_MATCH_THRESHOLD:
            continue

        legal_score, legal_details = address_match_score(
            entity, candidate, "legal"
        )
        if (
            legal_details.get("country_match") is not False
            and legal_details.get("city_score", 0) >= CITY_MATCH_THRESHOLD
            and legal_score >= ADDRESS_CORROBORATION_MIN
        ):
            confidence = min(85 + ns * 0.1 + legal_score * 0.05, 100)

            # Active street+zip contradiction: name+city matched, but
            # the input street AND zip are both present, the candidate
            # carries both, and both disagree (the shared registered-
            # agent case). Flag it and shave confidence so a clean
            # match wins. Missing street/zip data is NOT penalized.
            la = candidate.legal_address
            street_s = legal_details.get("street_score", 0)
            contradiction = (
                bool(entity.street) and bool(entity.zip_code)
                and bool(la and la.address_lines)
                and bool(la and la.postal_code)
                and street_s < STREET_MATCH_THRESHOLD
                and legal_details.get("zip_score", 0) == 0
            )
            if contradiction:
                confidence = min(confidence, 80.0)

            result = LookupResult(
                lei=candidate.lei,
                lei_status=candidate.status,
                match_type=MatchType.FULL_MATCH,
                confidence=confidence,
                gleif_legal_name=candidate.legal_name,
                gleif_legal_address=(
                    candidate.legal_address.format()
                    if candidate.legal_address else None
                ),
                gleif_hq_address=(
                    candidate.hq_address.format()
                    if candidate.hq_address else None
                ),
                gleif_legal_country=(la.country if la else None),
                gleif_legal_city=(la.city if la else None),
                gleif_legal_street=(la.street() if la else None),
                notes="Full match on name and legal address.",
                match_details={
                    "name_score": round(ns, 1),
                    "city_score": round(legal_details.get("city_score", 0), 1),
                    "street_score": round(street_s, 1),
                    "zip_score": round(legal_details.get("zip_score", 0), 1),
                },
            )
            _apply_common_warnings(result, entity)
            contradiction_code = WarningCode.ADDRESS_CONTRADICTION.value
            if contradiction and contradiction_code not in result.warnings:
                result.warnings = result.warnings + [contradiction_code]
            full_match_leis.append((candidate.lei, confidence))
            if (
                best_full_match is None
                or confidence > best_full_match.confidence
            ):
                best_full_match = result

        # Track the best HQ-only address match even when legal matched.
        hq_score, hq_details = address_match_score(entity, candidate, "hq")
        if (
            hq_details.get("country_match") is not False
            and hq_details.get("city_score", 0) >= CITY_MATCH_THRESHOLD
            and hq_score > best_hq_score
        ):
            best_hq_candidate = candidate
            best_hq_score = hq_score
            best_hq_name_score = ns
            best_hq_details = hq_details

        # Track the strongest name-matched candidate (for ISIN step).
        if ns >= STRONG_NAME_SCORE and ns > best_name_score_val:
            best_name_candidate = candidate
            best_name_score_val = ns

    if best_full_match:
        return _finalize_full_match(best_full_match, full_match_leis)

    # An ISIN can resolve a LEI directly or corroborate a near-miss.
    if entity.isin:
        isin_result = resolve_via_isin(
            entity, client,
            hq_candidate=best_hq_candidate,
            name_candidate=best_name_candidate,
        )
        if isin_result:
            logger.info("ISIN resolution succeeded: %s", isin_result.lei)
            return isin_result

    # An HQ-only address match (no legal-address agreement) is never a
    # positive match without ISIN confirmation - precision-first. Report
    # it as NO_MATCH with the near-miss details for manual review.
    if best_hq_candidate and best_hq_score >= ADDRESS_CORROBORATION_MIN:
        return _hq_only_no_match(
            entity, best_hq_candidate, best_hq_score,
            best_hq_name_score, best_hq_details,
        )

    # A single very strong, unique name hit with no address
    # corroboration is also not asserted; surfaced as NO_MATCH for
    # manual review.
    name_only = _name_only_no_match(entity, candidates)
    if name_only:
        return name_only

    return _no_match("No LEI found in the GLEIF database.")


def _finalize_full_match(
    result: LookupResult, full_match_leis: list[tuple[str, float]]
) -> LookupResult:
    """Flag an ambiguous full match, log, and return it."""
    # If two or more DISTINCT LEIs cleared the gate within a small
    # band of the winner, we may be asserting the wrong sibling.
    near_leis = {
        lei
        for lei, conf in full_match_leis
        if conf >= result.confidence - AMBIGUITY_CONFIDENCE_DELTA
    }
    ambiguous_code = WarningCode.AMBIGUOUS_MATCH.value
    if len(near_leis) >= 2 and ambiguous_code not in result.warnings:
        result.warnings = result.warnings + [ambiguous_code]
        logger.info("FULL_MATCH ambiguous among %d LEIs", len(near_leis))
    logger.info("FULL_MATCH found: %s", result.lei)
    return result


def _hq_only_no_match(
    entity: InputEntity,
    candidate: GleifCandidate,
    hq_score: float,
    name_score: float,
    hq_details: dict,
) -> LookupResult:
    """Build the NO_MATCH result for an HQ-only address near-miss."""
    legal_addr = (
        candidate.legal_address.format()
        if candidate.legal_address else ""
    )
    hq_addr = (
        candidate.hq_address.format() if candidate.hq_address else ""
    )
    status = candidate.status
    status_note = ""
    if status and status.upper() in LAPSED_STATUSES:
        status_note = f" LEI status: {status}."

    result = LookupResult(
        match_type=MatchType.NO_MATCH,
        confidence=0,
        lei_status=status,
        gleif_legal_name=candidate.legal_name,
        gleif_legal_address=legal_addr or None,
        gleif_hq_address=hq_addr or None,
        notes=(
            f"Name matches ({candidate.legal_name}), but the address "
            f"matches only the headquarters ({hq_addr}). Legal address: "
            f"{legal_addr}. LEI not assigned - HQ-only address match "
            f"without ISIN confirmation.{status_note}"
        ),
        match_details={
            "name_score": round(name_score, 1),
            "city_score": round(hq_details.get("city_score", 0), 1),
            "street_score": round(hq_details.get("street_score", 0), 1),
            "zip_score": round(hq_details.get("zip_score", 0), 1),
            "hq_address_score": round(hq_score, 1),
            "address_type": "hq",
        },
        warnings=[WarningCode.HQ_ONLY_MATCH.value],
    )
    _apply_common_warnings(result, entity)
    return result


def _name_only_no_match(
    entity: InputEntity, candidates: list[GleifCandidate]
) -> Optional[LookupResult]:
    """NO_MATCH for a lone very strong name hit, else None."""
    contenders = [(c, best_name_score(entity, c)) for c in candidates]
    ambiguous_pool = [(c, s) for c, s in contenders if s >= STRONG_NAME_SCORE]
    strong_hits = [
        (c, s)
        for c, s in contenders
        if s >= VERY_STRONG_NAME_SCORE and c.status == "ISSUED"
    ]
    if len(strong_hits) != 1 or len(ambiguous_pool) != 1:
        return None

    candidate, ns = strong_hits[0]

    # Country gate: a provided country must match the candidate's.
    input_country = country_to_iso(entity.country)
    cand_country = (
        (candidate.legal_address.country if candidate.legal_address else None)
        or (candidate.hq_address.country if candidate.hq_address else None)
    )
    if input_country and cand_country and input_country != cand_country:
        logger.info(
            "Name-only candidate skipped: country mismatch (%s vs %s)",
            input_country, cand_country,
        )
        return None

    legal_addr = (
        candidate.legal_address.format() if candidate.legal_address else None
    )
    hq_addr = (
        candidate.hq_address.format() if candidate.hq_address else None
    )
    result = LookupResult(
        lei_status=candidate.status,
        match_type=MatchType.NO_MATCH,
        confidence=0,
        gleif_legal_name=candidate.legal_name,
        gleif_legal_address=legal_addr,
        gleif_hq_address=hq_addr,
        notes=(
            f"Strong name match ({ns:.0f}%) with {candidate.legal_name}, "
            f"but the address was not verified - LEI not assigned. Manual "
            f"review recommended."
        ),
        match_details={"name_score": round(ns, 1)},
        warnings=[
            WarningCode.UNVERIFIED_ADDRESS.value,
            WarningCode.SINGLE_CANDIDATE_HEURISTIC.value,
        ],
    )
    _apply_common_warnings(result, entity)
    return result


def _build_candidate_summary(
    candidate: GleifCandidate,
    name_score: float,
    addr_score: float,
    details: dict,
) -> CandidateSummary:
    """Assemble a review CandidateSummary from a candidate and its scores.

    The overall percent is the name-weighted (60/40) blend of the given
    name and address scores; the address sub-scores and the full legal
    and HQ addresses back the validation row's expandable detail.
    """
    address = candidate.legal_address or candidate.hq_address
    return CandidateSummary(
        legal_name=candidate.legal_name,
        lei=candidate.lei,
        status=candidate.status,
        country=address.country if address else None,
        city=address.city if address else None,
        street=address.street() if address else None,
        overall=round(_overall_match(name_score, addr_score), 1),
        name_score=round(name_score, 1),
        city_score=round(details.get("city_score", 0), 1),
        street_score=round(details.get("street_score", 0), 1),
        zip_score=round(details.get("zip_score", 0), 1),
        legal_address=(
            candidate.legal_address.format()
            if candidate.legal_address else None
        ),
        hq_address=(
            candidate.hq_address.format()
            if candidate.hq_address else None
        ),
    )


def _closest_candidates(
    entity: InputEntity,
    candidates: list[GleifCandidate],
    exclude_lei: Optional[str],
    limit: int = CLOSEST_CANDIDATE_LIMIT,
) -> list[CandidateSummary]:
    """Top runner-up candidates for the validation table, minus the LEI.

    The set is chosen by name score (the top ``limit``, excluding the
    matched LEI), then returned sorted by overall match percent, highest
    first, to match the table's display order. Each summary also carries
    the legal-address sub-scores and an overall match percent (see
    _overall_match), so the validation table shows a richer row than the
    name score alone and its expandable detail can show the full
    addresses.
    """
    scored = sorted(
        ((best_name_score(entity, c), c) for c in candidates),
        key=lambda pair: pair[0],
        reverse=True,
    )
    closest: list[CandidateSummary] = []
    for name_score, candidate in scored:
        if candidate.lei == exclude_lei:
            continue
        addr_score, details = address_match_score(entity, candidate, "legal")
        closest.append(
            _build_candidate_summary(
                candidate, name_score, addr_score, details
            )
        )
        if len(closest) >= limit:
            break
    closest.sort(key=lambda summary: summary.overall, reverse=True)
    return closest


def _name_vs_openfigi(figi_name: str, candidate: GleifCandidate) -> float:
    """Best name-similarity of a candidate's names to an OpenFIGI name."""
    score = name_similarity(figi_name, candidate.legal_name)
    for other in candidate.other_names:
        score = max(score, name_similarity(figi_name, other))
    return score


def _isin_only_openfigi_review(
    entity: InputEntity, client: GleifClient
) -> tuple[LookupResult, list[CandidateSummary]]:
    """OpenFIGI review fallback for an ISIN-only lookup.

    Reached only when the ISIN is valid but absent from GLEIF's
    authoritative mapping. Resolves the ISIN to issuer name(s) via
    OpenFIGI, searches GLEIF by each, and returns the closest matches as
    review candidates - scored against the OpenFIGI name, since there is
    no user name to compare. Nothing is auto-asserted; a human confirms
    on the results page.

    Args:
        entity: The name-less entity being looked up (carries an ISIN).
        client: An open GLEIF client.

    Returns:
        A ``(NO_MATCH result, closest)`` tuple; ``closest`` is empty when
        OpenFIGI leads nowhere.
    """
    isin = normalize_isin(entity.isin)
    scored: list[tuple[float, GleifCandidate]] = []
    seen: set[str] = set()
    for figi_name in resolve_isin_to_names(isin, deadline=client.deadline):
        for candidate in client.search_by_name(figi_name, page_size=5):
            if candidate.lei in seen:
                continue
            seen.add(candidate.lei)
            scored.append(
                (_name_vs_openfigi(figi_name, candidate), candidate)
            )

    if not scored:
        return _no_match(
            f"ISIN {isin} is not in GLEIF's authoritative mapping, and "
            f"OpenFIGI did not lead to a GLEIF match either."
        ), []

    scored.sort(key=lambda pair: pair[0], reverse=True)
    closest: list[CandidateSummary] = []
    for name_score, candidate in scored[:CLOSEST_CANDIDATE_LIMIT]:
        addr_score, details = address_match_score(entity, candidate, "legal")
        closest.append(
            _build_candidate_summary(
                candidate, name_score, addr_score, details
            )
        )
    closest.sort(key=lambda summary: summary.overall, reverse=True)
    result = _no_match(
        f"ISIN {isin} is not in GLEIF's authoritative mapping. OpenFIGI "
        f"resolved it to an issuer name; the closest GLEIF matches are "
        f"listed for review."
    )
    return result, closest

