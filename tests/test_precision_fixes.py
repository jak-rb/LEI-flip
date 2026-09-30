
"""Tests of the precision fixes: no path may assert a wrong LEI.

Covers the narrow share-class patterns and serial tokens (ported from
the original tool's 2026-09-18 fixes), the contradiction cap, a live
LEI winning over its dead twin, the DUPLICATE status, the ISIN paths'
country and rival checks, the country warnings, the alpha-3 and native
country names, and the review candidates of a multi-LEI ISIN. GLEIF and
OpenFIGI are faked; nothing touches the network.
"""

import secrets

import pytest

import app as app_module
from core import isin as isin_module
from core import lookup as lookup_module
from core import storage
from core.address import country_to_iso, normalize_name
from core.constants import NAME_MATCH_THRESHOLD
from core.isin import is_valid_isin
from core.matcher import name_similarity
from core.models import GleifAddress, GleifCandidate, InputEntity

RBI_ISIN = "AT0000606306"
APPLE_ISIN = "US0378331005"


class _CannedGleif:
    """A GLEIF client answering every search from canned lists."""

    deadline = None

    def __init__(self, by_name=(), by_isin=()):
        self.by_name = list(by_name)
        self.by_isin = list(by_isin)

    def search_by_name(self, name, country=None, page_size=10):
        return self.by_name

    def search_by_name_no_country(self, name, page_size=10):
        return self.by_name

    def search_by_isin(self, isin):
        return self.by_isin

    def lookup_by_isin(self, isin):
        return self.by_isin


def _candidate(
    name, lei, country="CZ", city="Praha", street="Vinohradska 1",
    zip_code="12000", status="ISSUED",
):
    """A candidate with the same legal and HQ address."""
    address = GleifAddress(
        country=country, city=city, postal_code=zip_code,
        address_lines=[street],
    )
    return GleifCandidate(
        lei=lei, legal_name=name, status=status,
        legal_address=address, hq_address=address,
    )


@pytest.fixture(autouse=True)
def _no_openfigi(monkeypatch):
    """OpenFIGI finds nothing unless a test says otherwise."""
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        lookup_module, "resolve_isin_to_names", lambda *a, **k: [],
    )


# ---- share-class patterns and serial tokens ----

@pytest.mark.parametrize("name, normalized", [
    # A hyphenated name is no share class.
    ("V-SPED s.r.o.", "v-sped"),
    ("Z - FIN, a.s.", "z - fin"),
    ("SIM-ROLL, s.r.o.", "sim-roll"),
    ("Coca-Cola Company", "coca-cola"),
    ("Ford Credit Auto Owner Trust 2023-A",
     "ford credit auto owner trust 2023-a"),
    ("World Class Air Ltd", "world class air"),
    # Real share classes are still stripped.
    ("Amundi Funds - Pioneer US Bond - A EUR",
     "amundi funds - pioneer us bond"),
    ("Fidelity Funds - Asia Fund - A DIS USD", "fidelity funds - asia fund"),
    ("VIS, akciová společnost - A2 GBP", "vis"),
    ("J&T Bond CZK Class A", "j&t bond czk"),
    ("XYZ Fund Share Class B", "xyz fund"),
    ("BlackRock Global Funds - World Mining Fund Class A2",
     "blackrock global funds - world mining fund"),
    ("Alpha SICAV - RAIF", "alpha"),
])
def test_share_class_patterns_strip_only_share_classes(name, normalized):
    assert normalize_name(name) == normalized


@pytest.mark.parametrize("searched, sibling", [
    ("Ford Credit Auto Owner Trust 2023-A",
     "Ford Credit Auto Owner Trust 2023-B"),
    ("Toyota Auto Receivables 2023-A Owner Trust",
     "Toyota Auto Receivables 2023-B Owner Trust"),
    ("Apollo Investment Fund V, L.P.", "Apollo Investment Fund X, L.P."),
    ("Apollo Investment Fund V, L.P.", "Apollo Investment Fund, L.P."),
    ("Investec Fund Series", "Ninety One Funds Series I"),
    ("Alpha Series C", "Alpha Series D"),
    ("Alpha Fund A", "Alpha Fund"),
    ("T-SOFT a.s.", "T-LED s.r.o."),
    ("V", "V-SPED s.r.o."),
    ("Sim", "SIM-ROLL, s.r.o."),
    ("World Class Air Ltd", "World Class Bank Ltd"),
    ("Market Share Media Ltd", "Market Share Group Ltd"),
])
def test_serial_siblings_stay_below_the_name_gate(searched, sibling):
    assert name_similarity(searched, sibling) < NAME_MATCH_THRESHOLD
    assert name_similarity(sibling, searched) < NAME_MATCH_THRESHOLD


@pytest.mark.parametrize("searched, gleif_name", [
    ("Coca-Cola Company", "Coca Cola Company"),
    ("Ford Credit Auto Owner Trust 2023-A",
     "FORD CREDIT AUTO OWNER TRUST 2023-A"),
    ("Apollo Investment Fund V, L.P.", "APOLLO INVESTMENT FUND V, L.P."),
    ("Ninety One Funds Series I", "NINETY ONE FUNDS SERIES I"),
    ("ČEZ, a. s.", "CEZ a.s."),
    ("J&T Banka", "J & T BANKA, a.s."),
])
def test_the_same_entity_still_clears_the_name_gate(searched, gleif_name):
    assert name_similarity(searched, gleif_name) >= NAME_MATCH_THRESHOLD


def test_a_one_letter_fragment_is_not_matched_to_a_hyphenated_name():
    entity = InputEntity(name="V", town="Praha", country="CZ")
    client = _CannedGleif([_candidate("V-SPED s.r.o.", "S" * 20)])
    result, _ = lookup_module.lookup_entity(entity, client)
    assert result.lei is None


# ---- the full-match verdict ----

def test_a_street_and_zip_contradiction_caps_below_80():
    entity = InputEntity(
        name="ACME HOLDING a.s.", town="Praha", country="CZ",
        street="Na Prikope 99", zip_code="11000",
    )
    client = _CannedGleif([_candidate("ACME HOLDING a.s.", "A" * 20)])
    result, _ = lookup_module.lookup_entity(entity, client)
    assert result.lei == "A" * 20
    assert result.confidence == 79
    assert "ADDRESS_CONTRADICTION" in result.warnings


@pytest.mark.parametrize("dead_status", [
    "RETIRED", "MERGED", "DUPLICATE", "ANNULLED", "LAPSED",
])
@pytest.mark.parametrize("dead_first", [True, False])
def test_a_live_lei_beats_its_dead_twin(dead_status, dead_first):
    entity = InputEntity(
        name="ACME HOLDING a.s.", town="Praha", country="CZ",
        street="Vinohradska 1", zip_code="12000",
    )
    dead = _candidate("ACME HOLDING a.s.", "D" * 20, status=dead_status)
    live = _candidate("ACME HOLDING a.s.", "L" * 20)
    candidates = [dead, live] if dead_first else [live, dead]
    result, closest = lookup_module.lookup_entity(
        entity, _CannedGleif(candidates),
    )
    assert (result.lei, result.lei_status) == ("L" * 20, "ISSUED")
    assert "AMBIGUOUS_MATCH" in result.warnings
    assert [c.lei for c in closest] == ["D" * 20]


@pytest.mark.parametrize("status", ["DUPLICATE", "CANCELLED"])
def test_a_duplicate_lei_is_flagged_as_not_maintained(status):
    entity = InputEntity(
        name="ACME HOLDING a.s.", town="Praha", country="CZ",
    )
    client = _CannedGleif(
        [_candidate("ACME HOLDING a.s.", "D" * 20, status=status)],
    )
    result, _ = lookup_module.lookup_entity(entity, client)
    assert result.lei == "D" * 20
    assert "LAPSED_STATUS" in result.warnings

    isin_only = lookup_module.lookup_entity(
        InputEntity(isin=APPLE_ISIN),
        _CannedGleif(by_isin=[_candidate("ACME", "D" * 20, status=status)]),
    )[0]
    assert "LAPSED_STATUS" in isin_only.warnings
    assert "(not maintained)" in isin_only.notes


# ---- countries ----

@pytest.mark.parametrize("given, code", [
    ("DEU", "DE"), ("cze", "CZ"), ("SVK", "SK"), ("AUT", "AT"),
    ("GBR", "GB"), ("Deutschland", "DE"), ("Österreich", "AT"),
    ("Slovak Republic", "SK"), ("Slovenská republika", "SK"),
    ("Russian Federation", "RU"), ("United States of America", "US"),
    ("The Netherlands", "NL"), ("Luxemburg", "LU"),
    ("North Macedonia", "MK"), ("Kosovo", "XK"), ("Türkiye", "TR"),
    ("Czech Rep.", "CZ"), ("England", "GB"), ("Montenegro", "ME"),
    # Unchanged: names, the ČR/CR distinction, a passed-on code.
    ("Germany", "DE"), ("ČR", "CZ"), ("CR", "CR"), ("USA", "US"),
    ("XY", "XY"), ("Neverland", None),
])
def test_country_names_and_codes(given, code):
    assert country_to_iso(given) == code


@pytest.mark.parametrize("country", [None, "Neverland"])
def test_an_unrecognised_country_is_flagged_unverified(country):
    entity = InputEntity(
        name="Nordic Trading GmbH", town="Hamburg", country=country,
    )
    client = _CannedGleif(
        [_candidate("Nordic Trading Inc", "U" * 20, country="US",
                    city="Hamburg")],
    )
    result, _ = lookup_module.lookup_entity(entity, client)
    assert "COUNTRY_UNVERIFIED" in result.warnings


@pytest.mark.parametrize("country", ["Deutschland", "DEU", "Germany"])
def test_a_recognised_other_country_blocks_the_full_match(country):
    entity = InputEntity(
        name="Nordic Trading GmbH", town="Hamburg", country=country,
    )
    client = _CannedGleif(
        [_candidate("Nordic Trading Inc", "U" * 20, country="US",
                    city="Hamburg")],
    )
    result, _ = lookup_module.lookup_entity(entity, client)
    assert result.lei is None


def test_an_isin_only_match_in_another_country_is_flagged():
    rbi = _candidate(
        "Raiffeisen Bank International AG", "R" * 20, country="AT",
        city="Wien",
    )
    result, _ = lookup_module.lookup_entity(
        InputEntity(isin=RBI_ISIN, country="CZ"), _CannedGleif(by_isin=[rbi]),
    )
    assert result.lei == "R" * 20
    assert "COUNTRY_MISMATCH" in result.warnings
    assert "COUNTRY_UNVERIFIED" not in result.warnings

    same_country = lookup_module.lookup_entity(
        InputEntity(isin=RBI_ISIN, country="Rakousko"),
        _CannedGleif(by_isin=[rbi]),
    )[0]
    assert not {"COUNTRY_MISMATCH", "COUNTRY_UNVERIFIED"} & set(
        same_country.warnings
    )


# ---- the ISIN paths ----

def _raiffeisenbank():
    """The Czech bank the user names; its legal seat is in Praha."""
    return _candidate("Raiffeisenbank a.s.", "C" * 20)


def _rbi():
    """The Austrian parent, the issuer of RBI_ISIN."""
    return _candidate(
        "Raiffeisen Bank International AG", "R" * 20, country="AT",
        city="Wien", street="Am Stadtpark 9", zip_code="1030",
    )


def test_the_isins_used_here_are_valid():
    assert is_valid_isin(RBI_ISIN) and is_valid_isin(APPLE_ISIN)


@pytest.mark.parametrize("country", ["CZ", None])
def test_an_isin_of_another_issuer_does_not_override_the_named_entity(
    country,
):
    # Brno is a branch town, so nothing matches the legal address.
    entity = InputEntity(
        name="Raiffeisenbank a.s.", town="Brno", country=country,
        isin=RBI_ISIN,
    )
    client = _CannedGleif([_raiffeisenbank()], by_isin=[_rbi()])
    result, closest = lookup_module.lookup_entity(entity, client)
    assert result.lei is None
    assert "C" * 20 in [c.lei for c in closest]


def test_the_best_isin_hit_is_asserted_not_the_first():
    entity = InputEntity(name="Alpha Holding a.s.", isin=APPLE_ISIN)
    weaker = _candidate("Alpha Holding Group SE", "W" * 20)
    exact = _candidate("Alpha Holding a.s.", "E" * 20)
    client = _CannedGleif(by_isin=[weaker, exact])
    assert name_similarity(entity.name, weaker.legal_name) >= 50
    result = isin_module.resolve_via_isin(entity, client)
    assert result.lei == "E" * 20


def test_the_isin_still_resolves_the_named_entity():
    entity = InputEntity(
        name="Raiffeisenbank a.s.", town="Brno", country="CZ",
        isin=RBI_ISIN,
    )
    client = _CannedGleif([_raiffeisenbank()], by_isin=[_raiffeisenbank()])
    result, _ = lookup_module.lookup_entity(entity, client)
    assert result.lei == "C" * 20


def _alpha_banks():
    return [
        _candidate("Alpha Bank Ltd", "Y" * 20, country="CY", city="Nicosia"),
        _candidate("ALPHA BANK S.A.", "G" * 20, country="GR", city="Athens"),
    ]


def test_the_openfigi_fallback_keeps_to_the_given_country(monkeypatch):
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: ["ALPHA BANK"],
    )
    entity = InputEntity(name="Alpha Bank", country="Greece", isin=RBI_ISIN)
    result = isin_module.resolve_via_isin(
        entity, _CannedGleif(by_name=_alpha_banks()),
    )
    assert result.lei == "G" * 20
    assert "COUNTRY_MISMATCH" not in result.warnings


def test_an_ambiguous_openfigi_fallback_asserts_nothing(monkeypatch):
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: ["ALPHA BANK"],
    )
    entity = InputEntity(name="Alpha Bank", isin=RBI_ISIN)
    assert isin_module.resolve_via_isin(
        entity, _CannedGleif(by_name=_alpha_banks()),
    ) is None


def test_a_multi_lei_isin_offers_its_records_for_review():
    records = [
        _candidate("Alpha Holding a.s.", "A" * 20),
        _candidate("Alpha Holding SE", "B" * 20, country="DE", city="Berlin"),
    ]
    result, closest = lookup_module.lookup_entity(
        InputEntity(isin=APPLE_ISIN), _CannedGleif(by_isin=records),
    )
    assert result.lei is None
    assert "multiple LEIs" in result.notes
    assert sorted(c.lei for c in closest) == ["A" * 20, "B" * 20]

# ---- the results page flags risky matches ----

def test_matched_rows_show_a_dead_status_and_the_warnings():
    job_id = secrets.token_hex(16)
    storage.create_search(job_id, "bulk", [{"name": "A"}, {"name": "B"}])
    match = {
        "lei": "D" * 20, "lei_status": "RETIRED", "confidence": 100,
        "match_type": "FULL_MATCH", "gleif_legal_name": "ACME a.s.",
        "warnings": ["LAPSED_STATUS", "CHECK_FAILED", "AMBIGUOUS_MATCH",
                     "COUNTRY_MISMATCH"],
    }
    clean = {
        "lei": "C" * 20, "lei_status": "ISSUED", "confidence": 99,
        "match_type": "FULL_MATCH", "gleif_legal_name": "Clean a.s.",
        "warnings": ["CHECK_FAILED"],
    }
    storage.append_results(job_id, [
        {"input": {"name": "A"}, "match": match, "closest": []},
        {"input": {"name": "B"}, "match": clean, "closest": []},
    ], 0)
    page = app_module.app.test_client().get(f"/results?job={job_id}")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert body.count('class="match-flags"') == 1
    assert 'data-cs="LEI RETIRED"' in body
    assert 'data-en="Several similar LEIs"' in body
    assert 'data-cs="Více podobných LEI"' in body
    assert 'data-en="Country differs"' in body
    assert "CHECK_FAILED" not in body and "LAPSED_STATUS" not in body

