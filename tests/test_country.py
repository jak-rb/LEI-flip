
"""Country input to ISO code: "UK" and "ČR" (report defect #20).

Both are what people type, and both used to reach GLEIF as a country
filter nobody can match, turning a perfect match into NO_MATCH. The
differential test pins every other input to the old answer: this is
matcher territory, so only the intended inputs may change.
"""

import pytest
from unidecode import unidecode

from core import address
from core.address import country_to_iso
from core.matcher import address_match_score
from core.models import GleifAddress, GleifCandidate, InputEntity


def _old_country_to_iso(country_name):
    """country_to_iso as it was before the fix (commit 3f314ec)."""
    if not country_name:
        return None
    cleaned = country_name.strip().upper()
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned
    mapping = address._load_country_map()
    key = country_name.strip().lower()
    if key in mapping:
        return mapping[key]
    key_ascii = unidecode(key)
    for k, v in mapping.items():
        if unidecode(k) == key_ascii:
            return v
    return None


@pytest.mark.parametrize("country, iso", [
    ("UK", "GB"), ("uk", "GB"), (" Uk ", "GB"),
    ("ČR", "CZ"), ("čr", "CZ"), (" ČR ", "CZ"),
])
def test_common_abbreviations_give_their_iso_code(country, iso):
    assert country_to_iso(country) == iso


@pytest.mark.parametrize("country, iso", [
    # An ISO code stays itself, "CR" included: it is Costa Rica, not
    # a "ČR" typed without its háček.
    ("CR", "CR"), ("cr", "CR"), ("GB", "GB"), ("CZ", "CZ"),
    ("SK", "SK"), ("SR", "SR"),
    # An unknown code still reaches the matcher as a code: it gates
    # every candidate out, as before, rather than dropping the
    # country check altogether.
    ("XX", "XX"), ("ZZ", "ZZ"),
    ("Česko", "CZ"), ("United Kingdom", "GB"),
])
def test_codes_and_names_keep_their_meaning(country, iso):
    assert country_to_iso(country) == iso


def _inputs():
    """Every mapping key and value, and variants of each."""
    mapping = address._load_country_map()
    base = set(mapping) | set(mapping.values())
    base |= {
        a + b for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for b in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    }
    base |= {
        "U.K.", "U.S.", "USA", "U.S.A.", "UAE", "EN", "ČR", "SR", "ÚK",
        "Čr", "čR", "ÇA", "Ñ", "", " ", "Czech Republic",
    }
    variants = set()
    for text in base:
        variants |= {
            text, text.upper(), text.lower(), text.title(),
            f" {text} ", unidecode(text),
        }
    return sorted(variants)


def test_only_the_intended_inputs_change():
    changed = {
        text: (_old_country_to_iso(text), country_to_iso(text))
        for text in _inputs()
        if _old_country_to_iso(text) != country_to_iso(text)
    }
    # "ÚK" is "UK" with a stray accent; like "ČR" it now reads as the
    # country it names instead of an unknown code.
    assert changed == {
        text: (text.strip().upper(), iso)
        for text, iso in {
            "UK": "GB", "uk": "GB", "Uk": "GB", " UK ": "GB",
            " uk ": "GB", " Uk ": "GB",
            "ČR": "CZ", "čr": "CZ", "Čr": "CZ", "čR": "CZ",
            " ČR ": "CZ", " čr ": "CZ", " Čr ": "CZ", " čR ": "CZ",
            "ÚK": "GB", "úk": "GB", "Úk": "GB",
            " ÚK ": "GB", " úk ": "GB", " Úk ": "GB",
        }.items()
        if text in changed
    }
    assert {"UK", "ČR"} <= set(changed)


@pytest.mark.parametrize("typed, candidate_country", [
    ("UK", "GB"), ("ČR", "CZ"),
])
def test_the_abbreviation_passes_the_matchers_country_gate(
    typed, candidate_country
):
    entity = InputEntity(
        name="Alfa Holdings", country=typed, town="Praha",
        street="Ulice 1",
    )
    candidate = GleifCandidate(
        lei="A" * 20, legal_name="Alfa Holdings", status="ISSUED",
        legal_address=GleifAddress(
            country=candidate_country, city="Praha",
            address_lines=["Ulice 1"],
        ),
    )
    score, details = address_match_score(entity, candidate, "legal")
    assert details["country_match"] is True
    assert score > 90

