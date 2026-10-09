
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

from app import app as flask_app
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


@pytest.mark.parametrize("name, normalized", [
    ("Allianz SE​", "allianz"),
    ("Raiffeisenbank ﻿a.s.", "raiffeisenbank"),
    ("Alfa s.r.o.‎", "alfa"),
    ("Raiff­eisen AG", "raiffeisen"),
    ("Českáspořitelna, a.s.", "ceska sporitelna"),
])
def test_invisible_characters_do_not_block_a_legal_form(name, normalized):
    assert normalize_name(name) == normalized


def test_a_name_with_a_trailing_zero_width_space_still_matches():
    assert name_similarity("Allianz SE​", "Allianz SE") == 100


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
    # Never asserted (2026-10-06: anything but ISSUED is a stop), but
    # still flagged as not maintained in the downloads.
    entity = InputEntity(
        name="ACME HOLDING a.s.", town="Praha", country="CZ",
    )
    client = _CannedGleif(
        [_candidate("ACME HOLDING a.s.", "D" * 20, status=status)],
    )
    result, closest = lookup_module.lookup_entity(entity, client)
    assert result.lei is None and result.lei_status == status
    assert "LAPSED_STATUS" in result.warnings
    assert [(c.lei, c.status) for c in closest] == [("D" * 20, status)]

    isin_only, to_review = lookup_module.lookup_entity(
        InputEntity(isin=APPLE_ISIN),
        _CannedGleif(by_isin=[_candidate("ACME", "D" * 20, status=status)]),
    )
    assert isin_only.lei is None and status in isin_only.notes
    assert [c.lei for c in to_review] == ["D" * 20]


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
    result, _ = isin_module.resolve_via_isin(entity, client)
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
    # The address agrees, as the fallback now requires (see
    # tests/test_review_rules.py).
    entity = InputEntity(
        name="Alpha Bank", country="Greece", town="Athens", isin=RBI_ISIN,
    )
    result, _ = isin_module.resolve_via_isin(
        entity, _CannedGleif(by_name=_alpha_banks()),
    )
    assert result.lei == "G" * 20
    assert "COUNTRY_MISMATCH" not in result.warnings


def test_an_ambiguous_openfigi_fallback_asserts_nothing(monkeypatch):
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: ["ALPHA BANK"],
    )
    entity = InputEntity(name="Alpha Bank", town="Athens", isin=RBI_ISIN)
    assert isin_module.resolve_via_isin(
        entity, _CannedGleif(by_name=_alpha_banks()),
    ) == (None, [])


class _ByNameGleif(_CannedGleif):
    """A canned client whose name search answers by the searched name.

    A search for a name holding one of the ``answers`` words returns
    that word's records, as many as a page holds; any other name gets
    ``by_name``.
    """

    def __init__(self, answers, by_name=(), by_isin=()):
        super().__init__(by_name, by_isin)
        self.answers = answers

    def search_by_name(self, name, country=None, page_size=10):
        for word, records in self.answers.items():
            if word in name.lower():
                return records[:page_size]
        return self.by_name


def test_the_openfigi_fallback_does_not_override_the_named_entity(
    monkeypatch,
):
    # OpenFIGI names the ISIN's issuer, the Austrian parent, and GLEIF's
    # search by that name finds only the parent. Its name scores 65.1
    # against the Czech bank's, which the fallback's 65 let through:
    # with no country to tell them apart, the parent's LEI was
    # asserted. The rival check and the name gate now each stop it.
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names",
        lambda *a, **k: ["RAIFFEISEN BANK INTERNATIONAL AG"],
    )
    entity = InputEntity(
        name="Raiffeisenbank a.s.", town="Brno", isin=RBI_ISIN,
    )
    client = _ByNameGleif(
        {"international": [_rbi()]},
        by_name=[_raiffeisenbank()], by_isin=[_rbi()],
    )
    result, closest = lookup_module.lookup_entity(entity, client)
    assert result.lei is None
    assert "C" * 20 in [c.lei for c in closest]


def test_the_openfigi_fallback_does_not_assert_a_serial_sibling(
    monkeypatch,
):
    # The same with a sibling's ISIN, in the named trust's own country,
    # so the country check cannot help: GLEIF's search by the sibling's
    # name (5 records a page) did not list the named trust, and
    # "2023-A" against "2023-B" scores 70.
    family = "Ford Credit Auto Owner Trust"
    named = _candidate(
        f"{family} 2023-A", "N" * 20, country="US", city="Wilmington",
    )
    siblings = [
        _candidate(
            f"{family} {series}", f"{index:020d}", country="US",
            city="Wilmington",
        )
        for index, series in enumerate(
            ("2023-B", "2023-C", "2023-D", "2024-A", "2024-B", "2024-C"),
        )
    ]
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names",
        lambda *a, **k: [f"{family.upper()} 2023-B"],
    )
    # Any valid ISIN: OpenFIGI is faked.
    entity = InputEntity(
        name=f"{family} 2023-A", country="US", town="Detroit",
        isin=APPLE_ISIN,
    )
    client = _ByNameGleif(
        {"2023-b": siblings}, by_name=[named, *siblings],
    )
    result, closest = lookup_module.lookup_entity(entity, client)
    assert result.lei is None
    assert "N" * 20 in [c.lei for c in closest]


def test_the_openfigi_fallback_holds_the_typed_name_to_the_name_gate(
    monkeypatch,
):
    # Found by a live replay of real searches (2026-10-06): GLEIF has no
    # record of Genius Sports Limited, the ISIN's issuer, so nothing
    # rivals the record it has, a lapsed US "GENIUS SPORTS MEDIA INC.".
    # "Media" caps the typed name at 70, under the name gate, but the
    # fallback asked only 65 of it.
    media = _candidate(
        "GENIUS SPORTS MEDIA INC.", "M" * 20, country="US",
        city="Wilmington", status="LAPSED",
    )
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names",
        lambda *a, **k: ["GENIUS SPORTS LTD"],
    )
    entity = InputEntity(name="Genius Sports Ltd.", isin="GG00BMF1JR16")
    result, closest = lookup_module.lookup_entity(
        entity, _CannedGleif([media]),
    )
    assert result.lei is None
    assert [c.lei for c in closest] == ["M" * 20]


def test_the_rival_check_blocks_a_name_that_clears_the_gate(monkeypatch):
    # OpenFIGI names a near twin of the entity named, and GLEIF's search
    # by that name lists only the twin, whose name clears the gate
    # (96.3) but matches the typed name worse than the entity found.
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names",
        lambda *a, **k: ["ALPHA HOLDINGS"],
    )
    named = _candidate("Alpha Holding a.s.", "A" * 20)
    twin = _candidate("Alpha Holdings a.s.", "B" * 20)
    entity = InputEntity(
        name="Alpha Holding a.s.", town="Brno", isin=APPLE_ISIN,
    )
    result, closest = lookup_module.lookup_entity(
        entity, _ByNameGleif({"holdings": [twin]}, by_name=[named]),
    )
    assert result.lei is None
    assert "A" * 20 in [c.lei for c in closest]


def test_an_equally_named_rival_leaves_the_openfigi_fallback(monkeypatch):
    # Only a better name blocks: both banks are named "Alpha Bank", and
    # the one in the given country (and address) is still asserted.
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: ["ALPHA BANK"],
    )
    entity = InputEntity(
        name="Alpha Bank", country="Greece", town="Athens", isin=RBI_ISIN,
    )
    result, _ = lookup_module.lookup_entity(
        entity, _CannedGleif(by_name=_alpha_banks()),
    )
    assert result.lei == "G" * 20


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
    page = flask_app.test_client().get(f"/results?job={job_id}")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert body.count('class="match-flags"') == 1
    assert 'data-cs="LEI RETIRED"' in body
    assert 'data-en="Several similar LEIs"' in body
    assert 'data-cs="Více podobných LEI"' in body
    assert 'data-en="Country differs"' in body
    assert "CHECK_FAILED" not in body and "LAPSED_STATUS" not in body

