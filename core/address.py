
"""Address normalization and country code conversion."""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from unidecode import unidecode

_COUNTRY_MAP: Optional[dict[str, str]] = None
_LEGAL_FORMS: Optional[list[str]] = None
_LEGAL_FORM_PATTERNS: Optional[list[re.Pattern]] = None

# Project data folder (../data relative to this package).
DATA_DIR = Path(__file__).resolve().parents[1] / "data"

# Pre-compiled patterns for normalize_address_part.
_RE_STREET_ABBR = re.compile(r'\bstr\.?\b', re.IGNORECASE)
_RE_AVE_ABBR = re.compile(r'\bave\.?\b', re.IGNORECASE)
_RE_RD_ABBR = re.compile(r'\brd\.?\b', re.IGNORECASE)
_RE_BLVD_ABBR = re.compile(r'\bblvd\.?\b', re.IGNORECASE)
_RE_DR_ABBR = re.compile(r'\bdr\.?\b', re.IGNORECASE)
_RE_ST_ABBR = re.compile(r'\bst\.?\b', re.IGNORECASE)
_RE_PUNCT = re.compile(r'[,.:;/]+')
_RE_WHITESPACE = re.compile(r'\s+')
# For normalize_name.
_RE_COMMA_TRAIL = re.compile(r'[,.:;]+')
_RE_LONG_WHITESPACE = re.compile(r'\s{21,}')

# US state abbreviations (50 + DC). Used to detect "STATE 12345" style
# ZIPs (e.g. "NY 10019") so the state token can be stripped, WITHOUT
# mangling non-US postcodes such as UK "EC1A 1BB", where two leading
# letters are followed by a digit rather than a separator.
_US_STATE_ABBRS = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
)
# Only strip a leading 2-letter token when it is a real US state code
# AND is followed by a separator (space/hyphen). The mandatory
# separator keeps UK outward codes like "EC1A"/"AB10"/"CA1" intact.
_RE_ZIP_STATE = re.compile(
    r'^(?:' + '|'.join(_US_STATE_ABBRS) + r')[\s-]+'
)
_RE_ZIP_PREFIX = re.compile(r'^[A-Z]{2,3}-')

# Share class suffix patterns (e.g. "- A", "- BI EUR", "Class A",
# "(Acc)"), stripped before name comparison.
_RE_SHARE_CLASS_SUFFIX = re.compile(
    r'\s*-\s*[A-Z]{1,4}'
    r'(?:\s+(?:SUB|VOT|ACC|DIS|INC|CAP|USD|EUR|GBP|CHF|CZK|JPY|SEK|NOK'
    r'|DKK|PLN|HUF|AUD|CAD|SGD|HKD))*\s*$',
    re.IGNORECASE,
)
_RE_SHARE_CLASS_WORD = re.compile(
    r'\s+(?:class|share|trida|klasse|classe)\s+[A-Z0-9]{1,5}\b.*$',
    re.IGNORECASE,
)
_RE_TRAILING_PARENS = re.compile(r'\s*\([^)]{1,20}\)\s*$')


def _load_country_map() -> dict[str, str]:
    """Lazily load and cache the country-name -> ISO map."""
    global _COUNTRY_MAP
    if _COUNTRY_MAP is None:
        path = DATA_DIR / "country_mapping.json"
        with open(path, encoding="utf-8") as f:
            _COUNTRY_MAP = json.load(f)
    return _COUNTRY_MAP


def _load_legal_forms() -> list[str]:
    """Lazily load legal forms and pre-compile their strip patterns."""
    global _LEGAL_FORMS, _LEGAL_FORM_PATTERNS
    if _LEGAL_FORMS is not None:
        return _LEGAL_FORMS

    with open(DATA_DIR / "legal_forms.txt", encoding="utf-8") as f:
        forms = [line.strip().lower() for line in f if line.strip()]

    # Sort longest first so we strip "pty ltd" before "ltd".
    forms.sort(key=len, reverse=True)

    patterns = []
    for form in forms:
        pattern = (
            r'(?:^|[\s,])\s*' + re.escape(form)
            + r'\s*(?:[,.]?\s*$|(?=[\s,]))'
        )
        patterns.append(re.compile(pattern, re.IGNORECASE))

    # Published only once complete, patterns first: a thread that sees
    # _LEGAL_FORMS set uses the patterns at once, and normalize_name
    # caches whatever result they give.
    _LEGAL_FORM_PATTERNS = patterns
    _LEGAL_FORMS = forms
    return _LEGAL_FORMS


def country_to_iso(country_name: Optional[str]) -> Optional[str]:
    """Convert a country name to an ISO 3166-1 alpha-2 code.

    Accepts English or Czech names (diacritics optional) and common
    abbreviations such as "UK" and "ČR", and passes through values that
    are already two-letter ISO codes.

    Args:
        country_name: The country name or code to convert (may be None).

    Returns:
        The uppercase ISO alpha-2 code, or None if unrecognized.
    """
    if not country_name:
        return None

    mapping = _load_country_map()
    key = country_name.strip().lower()

    # The mapping comes first: "UK" is not an ISO code, and "ČR" is how
    # Czech users write Czechia.
    if key in mapping:
        return mapping[key]

    # Already an ISO code? An unknown two-letter value is passed on too:
    # as a country no candidate has, it keeps the country check strict
    # instead of dropping it. Checked before the diacritics retry, so
    # "CR" stays Costa Rica rather than "ČR" without its háček.
    cleaned = country_name.strip().upper()
    two_letters = len(cleaned) == 2 and cleaned.isalpha()
    if two_letters and cleaned.isascii():
        return cleaned

    # Retry without diacritics.
    key_ascii = unidecode(key)
    for k, v in mapping.items():
        if unidecode(k) == key_ascii:
            return v

    return cleaned if two_letters else None


def _shorten_whitespace_run(match: re.Match) -> str:
    """Shorten a long whitespace run without changing normalize_name."""
    # The legal-form patterns backtrack quadratically over a run of
    # whitespace: "A", 498 spaces, "B" took up to a second per call,
    # and one /run took minutes. For a run of two or more characters,
    # the output of normalize_name depends only on the run's last
    # character (a legal-form match may leave it behind), on whether it
    # holds a newline (which stops the share-class ".*"), U+0085 (which
    # unidecode drops) or any other whitespace (all of which unidecode
    # turns into whitespace), and on whether it is longer than the 20
    # characters the trailing-parentheses pattern allows. The shorter
    # run keeps all of that in at most 24 characters: its first 20,
    # one of each of those three kinds in the rest, and its last one.
    run = match.group()
    rest = run[20:-1]
    kinds = "".join(char for char in "\n\x85" if char in rest)
    if any(char not in "\n\x85" for char in rest):
        kinds += " "
    return run[:20] + kinds + run[-1]


# The matcher normalizes the searched name again for every name of
# every candidate, so each lookup repeats the same few inputs.
@lru_cache(maxsize=1024)
def normalize_name(name: str) -> str:
    """Normalize an entity name for matching.

    Lowercases, removes legal forms and share-class suffixes, strips
    diacritics and punctuation, and collapses whitespace.

    Args:
        name: The raw entity name.

    Returns:
        The normalized name, or "" if the input was empty.
    """
    if not name:
        return ""

    result = name.strip().lower()
    result = _RE_LONG_WHITESPACE.sub(_shorten_whitespace_run, result)

    _load_legal_forms()
    for pattern in _LEGAL_FORM_PATTERNS:
        result = pattern.sub(' ', result)

    # Strip share-class suffixes (e.g. "- BI EUR", "Class A", "(Acc)").
    result = _RE_SHARE_CLASS_SUFFIX.sub('', result)
    result = _RE_SHARE_CLASS_WORD.sub('', result)
    result = _RE_TRAILING_PARENS.sub('', result)

    result = unidecode(result)

    result = _RE_COMMA_TRAIL.sub(' ', result)
    result = _RE_WHITESPACE.sub(' ', result)
    return result.strip()


def normalize_address_part(text: Optional[str]) -> str:
    """Normalize a single address component for comparison.

    Lowercases, strips diacritics, expands common abbreviations
    ("St." -> street, "Ave." -> avenue, ...), and collapses punctuation
    and whitespace.

    Args:
        text: The raw address component (may be None).

    Returns:
        The normalized component, or "" if the input was empty.
    """
    if not text:
        return ""
    result = unidecode(text.strip().lower())

    # Expand common abbreviations to their full words.
    result = _RE_STREET_ABBR.sub('street', result)
    result = _RE_AVE_ABBR.sub('avenue', result)
    result = _RE_RD_ABBR.sub('road', result)
    result = _RE_BLVD_ABBR.sub('boulevard', result)
    result = _RE_DR_ABBR.sub('drive', result)
    result = _RE_ST_ABBR.sub('street', result)

    result = _RE_PUNCT.sub(' ', result)
    result = _RE_WHITESPACE.sub(' ', result)
    return result.strip()


def extract_zip(zip_code: Optional[str]) -> str:
    """Extract the comparable core of a ZIP code.

    Strips US-style state prefixes ("NY 10019") and "FL-" style
    prefixes, and removes spaces.

    Args:
        zip_code: The raw ZIP/postal code (may be None).

    Returns:
        The cleaned ZIP string, or "" if the input was empty.
    """
    if not zip_code:
        return ""
    # Remove US-style state prefixes ("NY 10019", "CA 92130").
    cleaned = _RE_ZIP_STATE.sub('', zip_code.strip())
    # Remove "FL-" style prefixes.
    cleaned = _RE_ZIP_PREFIX.sub('', cleaned)
    return cleaned.replace(' ', '')

