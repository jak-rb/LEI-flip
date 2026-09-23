
"""Czech versions of the lookup notes, for the results page.

The lookup code writes its notes in English (core/lookup.py,
core/isin.py and the failed-lookup notes in app.py), and they are
stored and exported that way. The results page shows every note in
both languages: ``czech_note`` maps a known English note to Czech,
keeping its parameters (ISINs, names, addresses, scores), and returns
any note it does not know unchanged, so it then reads in English in
both languages.
"""

import re
from typing import Optional

# Every note the lookup code writes, as an English template and its
# Czech version. A {field} stands for text the note carries (a name,
# an address, an ISIN, a score); the English text must stay identical
# to the code that writes it.
_NOTE_TEMPLATES = [
    (
        "Full match on name and legal address.",
        "Úplná shoda názvu i sídla.",
    ),
    (
        "No LEI found in the GLEIF database.",
        "V databázi GLEIF nebylo nalezeno žádné LEI.",
    ),
    (
        "Name matches ({name}), but the address matches only the "
        "headquarters ({hq_address}). Legal address: {legal_address}. "
        "LEI not assigned - HQ-only address match without ISIN "
        "confirmation.",
        "Název se shoduje ({name}), ale adresa odpovídá pouze centrále "
        "({hq_address}). Sídlo: {legal_address}. LEI nebylo přiřazeno - "
        "shoda jen s adresou centrály bez potvrzení přes ISIN.",
    ),
    (
        "Strong name match ({score}%) with {name}, but the address was "
        "not verified - LEI not assigned. Manual review recommended.",
        "Silná shoda názvu ({score} %) se subjektem {name}, ale adresa "
        "nebyla ověřena - LEI nebylo přiřazeno. Doporučujeme ruční "
        "kontrolu.",
    ),
    (
        "ISIN {isin} is not in GLEIF's authoritative mapping, and "
        "OpenFIGI did not lead to a GLEIF match either.",
        "ISIN {isin} není v autoritativním mapování GLEIF a ani OpenFIGI "
        "nevedlo ke shodě v GLEIF.",
    ),
    (
        "ISIN {isin} is not in GLEIF's authoritative mapping. OpenFIGI "
        "resolved it to an issuer name; the closest GLEIF matches are "
        "listed for review.",
        "ISIN {isin} není v autoritativním mapování GLEIF. OpenFIGI "
        "k němu našlo název emitenta; nejbližší shody z GLEIF jsou "
        "uvedeny ke kontrole.",
    ),
    (
        "ISIN {isin} found in GLEIF; LEI assigned despite the address "
        "not matching.",
        "ISIN {isin} byl nalezen v GLEIF; LEI bylo přiřazeno, přestože "
        "adresa nesouhlasí.",
    ),
    (
        "By ISIN {isin} the issuer is {name} - matches the HQ address in "
        "GLEIF.",
        "Podle ISIN {isin} je emitentem {name} - shoduje se s adresou "
        "centrály v GLEIF.",
    ),
    (
        "Strong name match ({score}%) with {name}. ISIN {isin} confirms "
        "the LEI.",
        "Silná shoda názvu ({score} %) se subjektem {name}. ISIN {isin} "
        "potvrzuje LEI.",
    ),
    (
        "ISIN {isin} resolved via OpenFIGI ({figi_name}); LEI found in "
        "GLEIF.",
        "ISIN {isin} byl dohledán přes OpenFIGI ({figi_name}); LEI bylo "
        "nalezeno v GLEIF.",
    ),
    (
        "No name was given and the ISIN is not valid.",
        "Nebyl zadán název a ISIN není platný.",
    ),
    (
        "ISIN {isin} maps to multiple LEIs in GLEIF; manual review is "
        "needed.",
        "ISIN {isin} odpovídá v GLEIF více LEI; je nutná ruční kontrola.",
    ),
    (
        "LEI resolved from ISIN {isin} via GLEIF's authoritative ISIN "
        "mapping; no name was provided to cross-check.",
        "LEI bylo určeno z ISIN {isin} podle autoritativního mapování "
        "ISIN v GLEIF; nebyl zadán název pro křížovou kontrolu.",
    ),
    (
        "Lookup failed because of an internal error - LEI not assigned. "
        "Please search this entity again.",
        "Vyhledání selhalo kvůli interní chybě - LEI nebylo přiřazeno. "
        "Vyhledejte prosím tento subjekt znovu.",
    ),
    (
        "Lookup failed: GLEIF refused the query - LEI not assigned. "
        "Please check the entered values.",
        "Vyhledání selhalo: GLEIF dotaz odmítl - LEI nebylo přiřazeno. "
        "Zkontrolujte prosím zadané hodnoty.",
    ),
    (
        "Lookup failed: GLEIF kept answering with an error - LEI not "
        "assigned. Please search this entity again later.",
        "Vyhledání selhalo: GLEIF opakovaně odpovídal chybou - LEI "
        "nebylo přiřazeno. Vyhledejte prosím tento subjekt později "
        "znovu.",
    ),
    (
        "Lookup failed: the GLEIF search took too long - LEI not "
        "assigned. Please search this entity again later.",
        "Vyhledání selhalo: vyhledávání v GLEIF trvalo příliš dlouho - "
        "LEI nebylo přiřazeno. Vyhledejte prosím tento subjekt později "
        "znovu.",
    ),
]

# The sentence some notes end with when the LEI is not maintained: the
# HQ-only near-miss adds the first, the ISIN matches the second.
_STATUS_TEMPLATES = [
    (" LEI status: {status}.", " Stav LEI: {status}."),
    (
        " WARNING: the LEI has status {status} (not maintained).",
        " UPOZORNĚNÍ: LEI má stav {status} (není udržováno).",
    ),
]

_FIELD_RE = re.compile(r"\{(\w+)\}")


def _compile(english: str) -> re.Pattern:
    """A regex matching an English template, one group per field."""
    parts = _FIELD_RE.split(english)
    return re.compile(
        "".join(
            f"(?P<{part}>.*?)" if position % 2 else re.escape(part)
            for position, part in enumerate(parts)
        ),
        re.DOTALL,
    )


_NOTES = [
    (_compile(english), czech) for english, czech in _NOTE_TEMPLATES
]
# {body} is the note the sentence follows.
_STATUS_SENTENCES = [
    (_compile("{body}" + english), czech)
    for english, czech in _STATUS_TEMPLATES
]


def czech_note(note: Optional[str]) -> str:
    """The Czech version of a lookup note.

    Args:
        note: A note as the lookup code wrote it (English), or None.

    Returns:
        The note in Czech with its parameters kept; the note itself
        when it is not one the lookup code writes; "" for no note.
    """
    if not note:
        return ""
    body, status_sentence = note, ""
    for pattern, czech in _STATUS_SENTENCES:
        match = pattern.fullmatch(note)
        if match:
            body = match["body"]
            status_sentence = czech.format(status=match["status"])
            break
    for pattern, czech in _NOTES:
        match = pattern.fullmatch(body)
        if match:
            return czech.format(**match.groupdict()) + status_sentence
    return note

