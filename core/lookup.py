
"""Single-entity lookup pipeline: search GLEIF, score, classify.

Ported from the original async pipeline and rewritten synchronous, with
the result notes translated to English. Classification is
precision-first: only a name + legal-address agreement (or an ISIN that
resolves/corroborates, see core/isin.py) asserts a LEI; weaker hits are
returned as NO_MATCH with the near-miss details for manual review.
"""

import logging
import math
from typing import Optional

from .address import country_to_iso
from .constants import (
    ADDRESS_CONTRADICTION_CAP,
    ADDRESS_CORROBORATION_MIN,
    AMBIGUITY_CONFIDENCE_DELTA,
    LAPSED_STATUSES,
)
from .gleif import GleifClient
from .isin import normalize_isin, resolve_isin_only, resolve_via_isin
from .matcher import (
    CITY_MATCH_THRESHOLD,
    NAME_MATCH_THRESHOLD,
    STREET_MATCH_THRESHOLD,
    address_match_score,
    best_name_score,
    name_similarity,
    shares_name_word,
)
from .models import (
    CandidateSummary,
    GleifAddress,
    GleifCandidate,
    InputEntity,
    LookupResult,
    MatchType,
    WarningCode,
    is_issued,
    stopped_note,
)
from .openfigi import resolve_isin_to_names

logger = logging.getLogger(__name__)

#: The note of a search that found nothing better to say.
NOT_FOUND_NOTE = "No LEI found in the GLEIF database."

#: Name score at/above which a candidate is a "strong" name hit.
STRONG_NAME_SCORE = 85

#: Name score for the lone-very-strong-hit name-only heuristic.
VERY_STRONG_NAME_SCORE = 92

#: How many runner-up candidates to surface for manual review (the
#: detail page's validation stepper lets the user confirm one of these).
CLOSEST_CANDIDATE_LIMIT = 3

#: Display-only weights for a candidate's overall match percent. They
#: blend name and address agreement to order the validation table and
#: never gate a match (unlike the audit-validated thresholds in
#: constants.py). The 60/40 split mirrors the 2:1 name-to-address ratio
#: in a matched result's confidence bonus.
OVERALL_NAME_WEIGHT = 0.6
OVERALL_ADDRESS_WEIGHT = 0.4

#: The matcher's weights of street and ZIP in an address score (see
#: core/matcher.address_match_score), reused for the review table's
#: "Address" score.
STREET_WEIGHT = 0.35
ZIP_WEIGHT = 0.25


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


def _street_zip_score(
    entity: InputEntity, address: Optional[GleifAddress], details: dict
) -> Optional[float]:
    """The review table's address score: street and ZIP agreement.

    Weighted as the matcher weighs the two, over the parts both the
    entity and the candidate's address carry, so a missing ZIP neither
    helps nor hurts. None when they share neither: there is nothing to
    compare, which differs from a disagreement (0).
    """
    parts = []
    if entity.street and address and address.address_lines:
        parts.append((details.get("street_score", 0), STREET_WEIGHT))
    if entity.zip_code and address and address.postal_code:
        parts.append((details.get("zip_score", 0), ZIP_WEIGHT))
    if not parts:
        return None
    total = sum(weight for _, weight in parts)
    return sum(score * weight for score, weight in parts) / total


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

    # Also for a country given but not recognised ("DEU" before the
    # alpha-3 table, a misspelling): it filtered nothing and was checked
    # against nothing, so it is as unverified as an empty one.
    if country_to_iso(entity.country) is None:
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
        to CLOSEST_CANDIDATE_LIMIT runner-up candidates for manual
        review.

    Raises:
        GleifApiError: If GLEIF cannot be reached. The caller renders
            this as the "service unavailable" state.
        DeadlineExceeded: If the client's deadline comes first.
    """
    logger.info(
        "Looking up: %s (country: %s, ISIN: %s)",
        entity.name, entity.country, entity.isin,
    )
    result, closest = _lookup(entity, client)
    return _name_stopped_records(result, closest), closest


def _name_stopped_records(
    result: LookupResult, closest: list[CandidateSummary]
) -> LookupResult:
    """Name the records of a no-match whose candidates are all stopped.

    Its plain note ("No LEI found", "manual review is needed", "listed
    for review") would hide them: the row has nothing to accept, so the
    page files it as a no-match and shows only its note (see
    core.models.stopped_note). A note about a record of its own (the
    result names one, as a stopped full match or an HQ-only near-miss
    does) is kept.
    """
    if result.lei or result.lei_status or result.gleif_legal_name:
        return result
    note = stopped_note([candidate.model_dump() for candidate in closest])
    if note is not None:
        result.notes = note
    return result


def _lookup(
    entity: InputEntity, client: GleifClient
) -> tuple[LookupResult, list[CandidateSummary]]:
    """The pipeline of lookup_entity, before stopped records are named."""
    # ISIN-only input: with no name to search or score by, resolve the
    # LEI authoritatively from the ISIN; when GLEIF's mapping has no
    # record of it, fall back to a softer OpenFIGI review (both below).
    if not entity.name:
        resolved = resolve_isin_only(entity, client)
        if resolved is None:
            return _isin_only_openfigi_review(entity, client)
        result, to_review = resolved
        # A multi-LEI mapping, or one record whose LEI is not ISSUED:
        # the records the ISIN maps to go to the review (a stopped one
        # only to be seen). No name was given, so only the address
        # fields (if any) score.
        # ISSUED records first (in GLEIF's order otherwise), so stopped
        # ones never crowd them out.
        to_review = sorted(
            to_review, key=lambda candidate: not is_issued(candidate.status),
        )
        closest = []
        for candidate in to_review[:CLOSEST_CANDIDATE_LIMIT]:
            addr_score, details = address_match_score(
                entity, candidate, "legal"
            )
            closest.append(_build_candidate_summary(
                entity, candidate, 0, addr_score, details,
            ))
        closest.sort(key=lambda summary: summary.overall, reverse=True)
        return result, closest

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
        result, to_review = resolve_via_isin(entity, client)
        result = result or _no_match(NOT_FOUND_NOTE)
        return result, _closest_candidates(
            entity, to_review, result.lei,
            pinned=to_review[0].lei if to_review else None,
        )

    result, to_review = _classify_candidates(entity, candidates, client)
    closest = _closest_candidates(
        entity, to_review + candidates, result.lei,
        pinned=to_review[0].lei if to_review else None,
    )
    return result, closest


def _classify_candidates(
    entity: InputEntity, candidates: list[GleifCandidate], client: GleifClient
) -> tuple[LookupResult, list[GleifCandidate]]:
    """Score the candidates and return the precision-first verdict.

    Returns:
        The verdict, and the candidates to keep in the review: first the
        record a NO_MATCH verdict is about, if any (a stopped full match,
        the OpenFIGI fallback's candidate, an HQ-only or name-only
        near-miss), which the review then always offers, as its note
        names it; the OpenFIGI fallback's may be one the name search
        lacks.
    """
    full_matches: list[LookupResult] = []
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
            # agent case). Flag it and cap confidence below 80 so a
            # clean match wins and a ">= 80" filter never includes it.
            # Missing street/zip data is NOT penalized.
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
                confidence = min(confidence, ADDRESS_CONTRADICTION_CAP)

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
            full_matches.append(result)

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

    full_match = _finalize_full_match(full_matches)
    if full_match:
        return full_match, []

    # An ISIN can resolve a LEI directly or corroborate a near-miss.
    isin_result, to_review = None, []
    if entity.isin:
        isin_result, to_review = resolve_via_isin(
            entity, client,
            hq_candidate=best_hq_candidate,
            name_candidate=best_name_candidate,
        )
        if isin_result and isin_result.lei:
            logger.info("ISIN resolution succeeded: %s", isin_result.lei)
            return isin_result, []

    # Name and legal address agree, but every such LEI is stopped by its
    # status (see core.models.is_issued): say so rather than "not found".
    if full_matches:
        stopped = max(full_matches, key=lambda match: match.confidence)
        record = next(c for c in candidates if c.lei == stopped.lei)
        return _not_usable_no_match(stopped), [record, *to_review]

    # The OpenFIGI fallback found a candidate whose address did not agree.
    if isin_result:
        return isin_result, to_review

    # An HQ-only address match (no legal-address agreement) is never a
    # positive match without ISIN confirmation - precision-first. Report
    # it as NO_MATCH with the near-miss details for manual review.
    if best_hq_candidate and best_hq_score >= ADDRESS_CORROBORATION_MIN:
        return _hq_only_no_match(
            entity, best_hq_candidate, best_hq_score,
            best_hq_name_score, best_hq_details,
        ), [best_hq_candidate, *to_review]

    # A single very strong, unique name hit with no address
    # corroboration is also not asserted; surfaced as NO_MATCH for
    # manual review.
    name_only = _name_only_no_match(entity, candidates)
    if name_only:
        result, record = name_only
        return result, [record, *to_review]

    return _no_match(NOT_FOUND_NOTE), to_review


def _finalize_full_match(
    full_matches: list[LookupResult],
) -> Optional[LookupResult]:
    """Pick the full match to assert, flag ambiguity, log, return it.

    Only an ISSUED LEI within AMBIGUITY_CONFIDENCE_DELTA of the best
    full match is asserted (see core.models.is_issued): None when there
    is none, as every full match near the top is stopped by its status.
    """
    # Only the matches within a small band of the best are in the
    # running: a clean match on a stopped LEI must not hand the match to
    # a far worse ISSUED one (say, one whose street and ZIP contradict
    # the input). Of those, the highest ISSUED confidence wins, the first
    # one GLEIF listed on a tie; a dead twin (a DUPLICATE, or an old
    # RETIRED LEI next to a re-registration) scores the same on name and
    # address, and is never chosen.
    if not full_matches:
        return None
    top = max(match.confidence for match in full_matches)
    usable = [
        match for match in full_matches
        if is_issued(match.lei_status)
        and match.confidence >= top - AMBIGUITY_CONFIDENCE_DELTA
    ]
    if not usable:
        return None
    result = max(usable, key=lambda match: match.confidence)
    # If another DISTINCT LEI cleared the gate within a small band of
    # the winner (or above it, stopped by its status), we may be
    # asserting the wrong sibling.
    rivals = {
        match.lei for match in full_matches
        if match.lei != result.lei
        and match.confidence >= result.confidence - AMBIGUITY_CONFIDENCE_DELTA
    }
    ambiguous_code = WarningCode.AMBIGUOUS_MATCH.value
    if rivals and ambiguous_code not in result.warnings:
        result.warnings = result.warnings + [ambiguous_code]
        logger.info("FULL_MATCH ambiguous among %d LEIs", len(rivals) + 1)
    logger.info("FULL_MATCH found: %s", result.lei)
    return result


def _not_usable_no_match(stopped: LookupResult) -> LookupResult:
    """NO_MATCH for a full match whose LEI is not ISSUED.

    The name and legal address agree, so the record is the entity, but
    its LEI cannot be used (reviewers, 2026-10-06: anything other than
    ISSUED is a stop). The record's details stay for the note and the
    downloads; the LEI itself is left out.
    """
    return LookupResult(
        match_type=MatchType.NO_MATCH,
        lei_status=stopped.lei_status,
        gleif_legal_name=stopped.gleif_legal_name,
        gleif_legal_address=stopped.gleif_legal_address,
        gleif_hq_address=stopped.gleif_hq_address,
        notes=(
            f"Name and legal address match {stopped.gleif_legal_name} "
            f"(LEI {stopped.lei}), but its LEI status is "
            f"{stopped.lei_status} - the LEI cannot be used and was not "
            f"assigned."
        ),
        match_details=stopped.match_details,
        warnings=[
            code for code in stopped.warnings
            if code != WarningCode.AMBIGUOUS_MATCH.value
        ],
    )


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
    if status and not is_issued(status):
        status_note = f" LEI {candidate.lei} status: {status}."

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
) -> Optional[tuple[LookupResult, GleifCandidate]]:
    """NO_MATCH for a lone very strong name hit, with it; else None."""
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
            f"Strong name match ({math.floor(round(ns, 1))}%) with "
            f"{candidate.legal_name}, "
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
    return result, candidate


def _build_candidate_summary(
    entity: InputEntity,
    candidate: GleifCandidate,
    name_score: float,
    addr_score: float,
    details: dict,
) -> CandidateSummary:
    """Assemble a review CandidateSummary from a candidate and its scores.

    The overall percent is the name-weighted (60/40) blend of the given
    name and address scores, which orders the candidates; the
    legal-address sub-scores (``details``), the street and ZIP score
    (see _street_zip_score) and the full legal and HQ addresses back the
    validation row and its expandable detail.
    """
    address = candidate.legal_address or candidate.hq_address
    street_zip = _street_zip_score(entity, candidate.legal_address, details)
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
        address_score=(
            None if street_zip is None else round(street_zip, 1)
        ),
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
    pinned: Optional[str] = None,
) -> list[CandidateSummary]:
    """Top runner-up candidates for the validation table, minus the LEI.

    The set is chosen by name score (the top ``limit``, excluding the
    matched LEI and any candidate that shares no distinctive word with
    the name, as one matching only in its legal form: see
    core/matcher.shares_name_word), then returned sorted by overall
    match percent, highest first. Each summary also carries the
    legal-address sub-scores, so the validation table shows the name,
    city and address scores and its expandable detail the full
    addresses. A candidate listed twice (the ISIN paths may find one the
    name search found too) is taken once. The candidate whose LEI is
    ``pinned`` (the record the verdict's note names) is always offered.
    """
    # The first of each LEI, in the order given: on a tie in name score,
    # GLEIF's own order decides, as it always has.
    unique: dict[str, GleifCandidate] = {}
    for candidate in candidates:
        unique.setdefault(candidate.lei, candidate)
    scored = sorted(
        (
            (best_name_score(entity, c), c) for c in unique.values()
            if c.lei != exclude_lei and shares_name_word(entity.name, c)
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    closest: list[CandidateSummary] = []
    picked = _keep_pinned(
        scored, _keep_an_issued(scored, limit), pinned, limit,
    )
    for name_score, candidate in picked:
        addr_score, details = address_match_score(entity, candidate, "legal")
        closest.append(
            _build_candidate_summary(
                entity, candidate, name_score, addr_score, details
            )
        )
    closest.sort(key=lambda summary: summary.overall, reverse=True)
    return closest


def _keep_an_issued(
    scored: list[tuple[float, GleifCandidate]], limit: int
) -> list[tuple[float, GleifCandidate]]:
    """The first ``limit`` of ``scored``, keeping an ISSUED candidate.

    Stopped records are only to be seen: when they fill every place
    while an ISSUED candidate is further down, that one takes the last
    place, so the review never hides the only candidates that may be
    accepted (and the row is not called one of stopped records).
    """
    picked = scored[:limit]
    if any(is_issued(candidate.status) for _, candidate in picked):
        return picked
    issued = next(
        (pair for pair in scored[limit:] if is_issued(pair[1].status)), None,
    )
    return picked[:limit - 1] + [issued] if issued else picked


def _keep_pinned(
    scored: list[tuple[float, GleifCandidate]],
    picked: list[tuple[float, GleifCandidate]],
    pinned: Optional[str],
    limit: int,
) -> list[tuple[float, GleifCandidate]]:
    """``picked``, holding the candidate whose LEI is ``pinned``.

    A note naming one record above other same-named ones must not stand
    over a review that offers only those (so a user trusting the note
    would accept another LEI). The pinned candidate takes the place of
    the last stopped one, else of the last.
    """
    if pinned is None or any(c.lei == pinned for _, c in picked):
        return picked
    pair = next((p for p in scored if p[1].lei == pinned), None)
    if pair is None:
        return picked
    if len(picked) < limit:
        return picked + [pair]
    for index in range(len(picked) - 1, -1, -1):
        if not is_issued(picked[index][1].status):
            return picked[:index] + picked[index + 1:] + [pair]
    return picked[:-1] + [pair]


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
    no user name to compare, and sharing a distinctive word with it.
    Nothing is auto-asserted; a human confirms on the results page.

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
    figi_names = resolve_isin_to_names(isin, deadline=client.deadline)
    for figi_name in figi_names:
        for candidate in client.search_by_name(figi_name, page_size=5):
            if candidate.lei in seen:
                continue
            seen.add(candidate.lei)
            if not shares_name_word(figi_name, candidate):
                continue
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
    for name_score, candidate in _keep_an_issued(
        scored, CLOSEST_CANDIDATE_LIMIT,
    ):
        addr_score, details = address_match_score(entity, candidate, "legal")
        closest.append(
            _build_candidate_summary(
                entity, candidate, name_score, addr_score, details
            )
        )
    closest.sort(key=lambda summary: summary.overall, reverse=True)
    result = _no_match(
        f"ISIN {isin} is not in GLEIF's authoritative mapping. OpenFIGI "
        f"resolved it to the issuer {figi_names[0]}; the closest GLEIF "
        f"matches are listed for review."
    )
    return result, closest

