
"""Address normalization and country code conversion."""

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Optional

from unidecode import unidecode

_COUNTRY_MAP: Optional[dict[str, str]] = None
_ALPHA3: Optional[dict[str, str]] = None
_LEGAL_FORMS: Optional[list[str]] = None
_LEGAL_FORM_PATTERNS: Optional[list[re.Pattern]] = None

# The lookup tables live with the component's other data files, in
# src/main/data/ (../main/data relative to this package), resolved from
# this file because the deployed working directory is not guaranteed.
DATA_DIR = Path(__file__).resolve().parents[1] / "main" / "data"

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

# Share class suffix patterns (e.g. "- A", "- BI EUR", "- I Acc",
# "Class A", "(Acc)"), stripped before name comparison.
#
# Both are deliberately narrow: what follows the hyphen or the word
# "class" must look like a share class - a 1-2 letter code with an
# optional digit (A, BI, R2), a currency, or a distribution/hedging
# keyword - and the hyphen must have a space on both sides. The looser
# patterns ported first stripped any trailing hyphenated word of up to
# four letters and any word after "class"/"share", which collapsed real
# names to a bare stem ("V-SPED s.r.o." -> "v", "SIM-ROLL" -> "sim",
# "Trust 2023-A" -> "trust 2023", "World Class Air" -> "world") and let
# a sibling or a fragment be asserted at full confidence. The original
# tool narrowed the hyphen form the same way on 2026-09-18 (its RAIF /
# SIF designators moved to main/data/legal_forms.txt). An unusual class
# code now stays in the name and only counts as a distinguishing token:
# NO_MATCH with details, never a wrong LEI.
_SHARE_CLASS_TOKEN = (
    r'(?:[A-Z]{1,2}\d?|SUB|VOT|ACC|DIS|DIST|INC|CAP|HEDGED|HDG|UNHEDGED'
    r'|INST|RETAIL|USD|EUR|GBP|CHF|CZK|JPY|SEK|NOK|DKK|PLN|HUF|AUD|CAD'
    r'|SGD|HKD)'
)
_RE_SHARE_CLASS_SUFFIX = re.compile(
    r'\s+-\s+' + _SHARE_CLASS_TOKEN
    + r'(?:\s+' + _SHARE_CLASS_TOKEN + r')*\s*$',
    re.IGNORECASE,
)
_RE_SHARE_CLASS_WORD = re.compile(
    r'\s+(?:(?:share\s+)?class|share|trida|klasse|classe)\s+'
    r'(?:' + _SHARE_CLASS_TOKEN + r'|\d{1,3})\b.*$',
    re.IGNORECASE,
)
_RE_TRAILING_PARENS = re.compile(r'\s*\([^)]{1,20}\)\s*$')

# The Czech "spol. s r.o." however its dots and spaces fall ("spol s r
# o", "spol.s.r.o.", "SPOL. S R.O"): bank exports often drop them, and
# GLEIF's own spelling is stripped as a legal form, so a name keeping
# them would no longer match it.
_RE_SPOL_SRO = re.compile(
    r'(?:^|(?<=[\s,(]))spol(?:\.\s*|\s+)s\.?\s*r\.?\s*o\.?(?=[\s,)]|$)',
    re.IGNORECASE,
)


def _load_country_map() -> dict[str, str]:
    """Lazily load and cache the country-name -> ISO map."""
    global _COUNTRY_MAP
    if _COUNTRY_MAP is None:
        path = DATA_DIR / "country_mapping.json"
        with open(path, encoding="utf-8") as f:
            _COUNTRY_MAP = json.load(f)
    return _COUNTRY_MAP


def _load_alpha3() -> dict[str, str]:
    """Lazily load and cache the ISO alpha-3 -> alpha-2 table."""
    global _ALPHA3
    if _ALPHA3 is None:
        with open(DATA_DIR / "country_alpha3.json", encoding="utf-8") as f:
            _ALPHA3 = json.load(f)
    return _ALPHA3


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
        # Any whitespace between a form's words, as in "s.  r. o." or
        # a no-break space copied from a register: the name is not
        # collapsed until after the forms are stripped.
        words = r'\s+'.join(re.escape(word) for word in form.split())
        # A spaced form of single letters ("a. s.", "v. o. s.") is not
        # taken right after a dotted lone letter: there it is the end of
        # spaced initials, as in "J. K. S. Group" or "H. A. S. spol. s
        # r.o.". After an undotted one ("M & M s. r. o.", "Firma B a.
        # s.") it is the legal form.
        after_initial = (
            r'(?<!\s)(?<!\b[^\W\d_]\.)'
            if ' ' in form and re.fullmatch(r'[^\W\d_]\.', form.split()[0])
            else ''
        )
        pattern = (
            r'(?:^|' + after_initial + r'[\s,])\s*' + words
            + r'\s*(?:[,.]?\s*$|(?=[\s,]))'
        )
        patterns.append(re.compile(pattern, re.IGNORECASE))

    # Published only once complete, patterns first: a thread that sees
    # _LEGAL_FORMS set uses the patterns at once, and normalize_name
    # caches whatever result they give.
    _LEGAL_FORM_PATTERNS = patterns
    _LEGAL_FORMS = forms
    return _LEGAL_FORMS


def is_legal_form(text: str) -> bool:
    """Whether a text is one of the legal forms, such as "SE" or "a.s."."""
    return " ".join(text.lower().split()) in _load_legal_forms()


def country_to_iso(country_name: Optional[str]) -> Optional[str]:
    """Convert a country name to an ISO 3166-1 alpha-2 code.

    Accepts English or Czech names (diacritics optional), common native
    and official names ("Deutschland", "Slovak Republic"), abbreviations
    such as "UK" and "ČR", and ISO alpha-3 codes ("DEU"), and passes
    through values that are already two-letter ISO codes.

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

    # An ISO alpha-3 code, as bank data exports often carry ("CZE").
    cleaned = country_name.strip().upper()
    if cleaned in _load_alpha3():
        return _ALPHA3[cleaned]

    # Already an ISO code? An unknown two-letter value is passed on too:
    # as a country no candidate has, it keeps the country check strict
    # instead of dropping it. Checked before the diacritics retry, so
    # "CR" stays Costa Rica rather than "ČR" without its háček.
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


def _drop_invisible(text: str) -> str:
    """Drop format characters; turn U+0085 into a space."""
    # A zero-width space, a direction mark or a BOM pasted with a name
    # would stop a legal form next to it from being stripped, and
    # unidecode would drop it only after that. U+0085 is whitespace to
    # the patterns but dropped by unidecode, merging its two words.
    if text.isascii():
        return text
    return "".join(
        " " if char == "\x85" else char
        for char in text
        if unicodedata.category(char) != "Cf"
    ).strip()


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

    result = _drop_invisible(name.strip().lower())
    result = _RE_LONG_WHITESPACE.sub(_shorten_whitespace_run, result)

    _load_legal_forms()
    result = _RE_SPOL_SRO.sub(' ', result)
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

