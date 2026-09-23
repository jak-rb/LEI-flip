
"""Data models for the LEI Lookup Tool."""

import math
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class MatchType(str, Enum):
    """Classification of a single lookup outcome."""

    FULL_MATCH = "FULL_MATCH"
    HQ_MATCH = "HQ_MATCH"
    ISIN_MATCH = "ISIN_MATCH"
    ISIN_GLEIF_MATCH = "ISIN_GLEIF_MATCH"
    NAME_ONLY_MATCH = "NAME_ONLY_MATCH"
    NO_MATCH = "NO_MATCH"


class WarningCode(str, Enum):
    """Machine-readable warning codes attached to a LookupResult.

    Used to flag risky matches the user should manually audit.
    """

    UNVERIFIED_ADDRESS = "UNVERIFIED_ADDRESS"
    HQ_ONLY_MATCH = "HQ_ONLY_MATCH"
    LAPSED_STATUS = "LAPSED_STATUS"
    ISIN_ONLY = "ISIN_ONLY"
    #: Matched with no entity name to cross-check (e.g. an ISIN-only
    #: lookup); the identity rests entirely on the identifier.
    NAME_UNVERIFIED = "NAME_UNVERIFIED"
    SINGLE_CANDIDATE_HEURISTIC = "SINGLE_CANDIDATE_HEURISTIC"
    COUNTRY_UNVERIFIED = "COUNTRY_UNVERIFIED"
    CHECK_FAILED = "CHECK_FAILED"
    #: Two or more DISTINCT LEIs cleared the name+address gate with
    #: near-equal confidence - the asserted LEI may be the wrong sibling
    #: (e.g. co-located serially-numbered SPVs). Disambiguate manually.
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    #: The name+city matched but the input street AND zip both actively
    #: contradict the candidate's (both sides have data and disagree).
    #: Common at shared registered-agent addresses - verify first.
    ADDRESS_CONTRADICTION = "ADDRESS_CONTRADICTION"


class InputEntity(BaseModel):
    """Entity to look up, from a single form or a bulk upload row."""

    # Optional: a lookup needs either a name or an ISIN (see the
    # name-or-ISIN check below), so an ISIN on its own is a valid input.
    name: Optional[str] = Field(None, max_length=500)
    isin: Optional[str] = Field(None, max_length=20)
    street: Optional[str] = Field(None, max_length=500)
    town: Optional[str] = Field(None, max_length=200)
    # Country name (any language); converted to ISO during matching.
    country: Optional[str] = Field(None, max_length=200)
    zip_code: Optional[str] = Field(None, max_length=20)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: Optional[str]) -> Optional[str]:
        """Normalize a blank or whitespace-only name to None."""
        # A blank cell must never silently become a query that
        # mismatches everything; treat it as "no name given" instead.
        return (v or "").strip() or None

    @model_validator(mode="after")
    def _require_name_or_isin(self) -> "InputEntity":
        """Require at least one of a name or an ISIN to search by."""
        if not self.name and not (self.isin or "").strip():
            raise ValueError("either a name or an ISIN is required")
        return self


class InputError(ValueError):
    """Input the user must fix, with its message in English and Czech.

    The English text is the exception's message (``str(error)``); the
    web layer returns both, so the page shows the one for its language.
    """

    def __init__(self, message: str, message_cs: str) -> None:
        super().__init__(message)
        self.message_cs = message_cs


class GleifAddress(BaseModel):
    """Address as returned by the GLEIF API."""

    country: Optional[str] = None  # ISO 3166-1 alpha-2
    region: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    address_lines: list[str] = Field(default_factory=list)

    def format(self) -> str:
        """Render the address as one comma-separated string.

        Returns:
            The non-empty address parts joined by ", ".
        """
        parts = []
        if self.address_lines:
            parts.append(", ".join(self.address_lines))
        if self.city:
            parts.append(self.city)
        if self.postal_code:
            parts.append(self.postal_code)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)

    def street(self) -> Optional[str]:
        """Join the street address lines into one string, or None."""
        return ", ".join(self.address_lines) if self.address_lines else None


class GleifCandidate(BaseModel):
    """A candidate LEI record from a GLEIF search."""

    lei: str
    legal_name: str
    status: str  # ISSUED, LAPSED, RETIRED, etc.
    legal_address: Optional[GleifAddress] = None
    hq_address: Optional[GleifAddress] = None
    other_names: list[str] = Field(default_factory=list)


class CandidateSummary(BaseModel):
    """A runner-up GLEIF candidate for the detail page's closest list.

    Carries the fields each validation row shows (name, country, city,
    street, and an overall match percent) plus the supporting detail its
    expandable section reveals (the per-field scores and the full legal
    and HQ addresses).
    """

    legal_name: str
    lei: str
    status: str
    country: Optional[str] = None
    city: Optional[str] = None
    street: Optional[str] = None
    #: Name-weighted (60/40) blend of name and address agreement, shown
    #: as the row's overall match percent. Display-only: it never gates a
    #: match (see core/lookup.py).
    overall: float = 0.0
    name_score: float = 0.0
    city_score: float = 0.0
    street_score: float = 0.0
    zip_score: float = 0.0
    legal_address: Optional[str] = None
    hq_address: Optional[str] = None


class LookupResult(BaseModel):
    """Result of a single LEI lookup."""

    lei: Optional[str] = None
    lei_status: Optional[str] = None
    match_type: MatchType = MatchType.NO_MATCH
    confidence: float = 0.0
    gleif_legal_name: Optional[str] = None
    gleif_legal_address: Optional[str] = None
    gleif_hq_address: Optional[str] = None
    # Structured legal-address parts, so the matched-results table can
    # show Country / City / Street columns (not just the formatted line).
    gleif_legal_country: Optional[str] = None
    gleif_legal_city: Optional[str] = None
    gleif_legal_street: Optional[str] = None
    notes: str = ""
    match_details: Optional[dict] = None
    warnings: list[str] = Field(default_factory=list)

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        """Keep confidence a finite percentage in [0, 100]."""
        # Guards against out-of-range / NaN / inf values that would
        # otherwise serialize to invalid JSON (Infinity/NaN) or show a
        # nonsensical score to the user.
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        if math.isnan(v) or math.isinf(v):
            return 0.0
        return min(100.0, max(0.0, v))

    @property
    def check_status(self) -> dict:
        """Per-field pass/fail status from the matcher thresholds.

        Returns:
            A dict with keys name, city, street, and zip, each "pass",
            "fail", or "na".
        """
        # Imported here to avoid a circular import at module load time.
        from .matcher import (
            NAME_MATCH_THRESHOLD,
            CITY_MATCH_THRESHOLD,
            STREET_MATCH_THRESHOLD,
        )

        def status(score, threshold):
            # A non-numeric (or missing) score is "not applicable",
            # not a crash - a malformed dict must never raise here.
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                return "na"
            return "pass" if score >= threshold else "fail"

        def zip_status(score):
            # ZIP passes on any score > 0 (0 = no data/no match, 80 =
            # prefix, 100 = exact), so it does not use a threshold.
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                return "na"
            return "pass" if score > 0 else "fail"

        md = self.match_details or {}
        return {
            "name": status(md.get("name_score"), NAME_MATCH_THRESHOLD),
            "city": status(md.get("city_score"), CITY_MATCH_THRESHOLD),
            "street": status(md.get("street_score"), STREET_MATCH_THRESHOLD),
            "zip": zip_status(md.get("zip_score")),
        }

