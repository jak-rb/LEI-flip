
"""Tests of the fuzzy name search, the fallback for a misspelled name.

A colleague searched "GOLDMAN SACHS ASSET MANAGMENT" (2026-10-09) and
was told "No LEI found", while GLEIF's website offered Goldman Sachs
Asset Management B.V., L.P. and others: GLEIF's API filters match whole
words, and only its fuzzy completions tolerate the typo. GLEIF is
faked; nothing touches the network.
"""

import pytest

from main import routes as app_module
from core import gleif
from core import lookup as lookup_module
from core.gleif import GleifQueryError
from core.models import (
    GleifAddress,
    GleifCandidate,
    InputEntity,
    MatchType,
)

TYPO = "GOLDMAN SACHS ASSET MANAGMENT"
BV_LEI = "549300YXQ0Y6SPQTPE40"
LP_LEI = "CF5M58QA35CFPUX70H17"
BELGIUM_LEI = "3OP0E66QECMEQ9I7GB39"


def _completion(value, lei=None):
    """One item of GLEIF's /fuzzycompletions reply."""
    item = {"type": "fuzzycompletions", "attributes": {"value": value}}
    if lei:
        item["relationships"] = {
            "lei-records": {"data": {"type": "lei-records", "id": lei}},
        }
    return item


def _record(lei, name):
    """One GLEIF /lei-records record, as much of it as is parsed."""
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": name},
                "legalAddress": {"country": "NL", "city": "Den Haag"},
            },
            "registration": {"status": "ISSUED"},
        },
    }


class _ScriptedClient(gleif.GleifClient):
    """A GleifClient whose requests are answered from canned replies."""

    def __init__(self, completions, records=(), refuse=False):
        super().__init__()
        self.completions = completions
        self.records = list(records)
        self.refuse = refuse
        self.requests = []

    def _request(self, path, params, attempts=gleif.MAX_RETRIES):
        self.requests.append((path, dict(params)))
        if path == "/fuzzycompletions":
            if self.refuse:
                raise GleifQueryError("GLEIF API refused the query: HTTP 400")
            return {"data": self.completions}
        wanted = params["filter[lei]"].split(",")
        # GLEIF lists the records by LEI, not in the order asked.
        return {
            "data": sorted(
                (r for r in self.records if r["id"] in wanted),
                key=lambda r: r["id"],
            ),
        }


def test_fuzzy_search_returns_the_suggested_records_in_gleif_order():
    client = _ScriptedClient(
        completions=[
            _completion("GOLDMAN SACHS ASSET MANAGEMENT B.V.", BV_LEI),
            _completion("A"),  # a suggestion with no record to it
            _completion("GOLDMAN SACHS ASSET MANAGEMENT, L.P.", LP_LEI),
            _completion("Goldman Sachs Asset Management B.V", BV_LEI),
            _completion("Goldman Sachs Asset Management Belgium", BELGIUM_LEI),
        ],
        records=[
            _record(BV_LEI, "GOLDMAN SACHS ASSET MANAGEMENT B.V."),
            _record(LP_LEI, "GOLDMAN SACHS ASSET MANAGEMENT, L.P."),
            _record(BELGIUM_LEI, "Goldman Sachs Asset Management Belgium"),
        ],
    )

    found = client.search_by_fuzzy_name(TYPO)

    assert [c.lei for c in found] == [BV_LEI, LP_LEI, BELGIUM_LEI]
    assert found[0].legal_name == "GOLDMAN SACHS ASSET MANAGEMENT B.V."
    assert client.requests[0] == (
        "/fuzzycompletions", {"field": "entity.legalName", "q": TYPO},
    )
    path, params = client.requests[1]
    assert path == "/lei-records"
    assert params["filter[lei]"] == f"{BV_LEI},{LP_LEI},{BELGIUM_LEI}"
    assert params["page[size]"] == "3"


def test_no_suggested_record_makes_no_record_request():
    client = _ScriptedClient(completions=[_completion("A")])

    assert client.search_by_fuzzy_name(TYPO) == []
    assert [path for path, _ in client.requests] == ["/fuzzycompletions"]


def test_a_refused_fuzzy_query_finds_nothing():
    # GLEIF refuses a q over 255 characters (a name may hold 500): the
    # exact searches took it, so the lookup must not fail on it.
    client = _ScriptedClient(completions=[], refuse=True)

    assert client.search_by_fuzzy_name("A" * 300) == []


def _gsam(lei, name, country, city, street, zip_code):
    address = GleifAddress(
        country=country, city=city, postal_code=zip_code,
        address_lines=[street],
    )
    return GleifCandidate(
        lei=lei, legal_name=name, status="ISSUED",
        legal_address=address, hq_address=address,
    )


GSAM_RECORDS = [
    _gsam(BV_LEI, "GOLDMAN SACHS ASSET MANAGEMENT B.V.", "NL", "Den Haag",
          "Prinses Beatrixlaan 35", "2595 AK"),
    _gsam(LP_LEI, "GOLDMAN SACHS ASSET MANAGEMENT, L.P.", "US", "New York",
          "200 West Street", "10282"),
    _gsam(BELGIUM_LEI, "Goldman Sachs Asset Management Belgium", "BE",
          "Brussel", "Marnixlaan 23", "1000"),
]


class _FuzzyOnlyGleif:
    """GLEIF finding a name only by its fuzzy search."""

    deadline = None

    def __init__(self, fuzzy, by_name=()):
        self.fuzzy = list(fuzzy)
        self.by_name = list(by_name)
        self.fuzzy_queries = []

    def search_by_name(self, name, country=None, page_size=10):
        return self.by_name

    def search_by_name_no_country(self, name, page_size=10):
        return self.by_name

    def search_by_fuzzy_name(self, name):
        self.fuzzy_queries.append(name)
        return self.fuzzy

    def search_by_isin(self, isin):
        return []

    def lookup_by_isin(self, isin):
        return []


def test_a_misspelled_name_offers_what_gleif_suggests_for_review():
    client = _FuzzyOnlyGleif(GSAM_RECORDS)

    result, closest = lookup_module.lookup_entity(
        InputEntity(name=TYPO), client,
    )

    assert client.fuzzy_queries == [TYPO]
    assert result.match_type == MatchType.NO_MATCH
    assert result.lei is None
    # The B.V. and the L.P. carry the name; all three share its words.
    assert {c.lei for c in closest} == {BV_LEI, LP_LEI, BELGIUM_LEI}
    by_lei = {c.lei: c for c in closest}
    assert by_lei[BV_LEI].name_score >= 75
    assert by_lei[LP_LEI].name_score >= 75
    assert by_lei[BELGIUM_LEI].name_score < 75
    # The results page offers them in the validation stepper.
    row = app_module._result_row(InputEntity(name=TYPO), result, closest)
    groups = app_module._partition([row])
    assert groups["validate_total"] == 1
    assert groups["no_match"] == []


def test_a_misspelled_name_with_its_address_is_matched():
    entity = InputEntity(
        name=TYPO, country="Netherlands", town="Den Haag",
        street="Prinses Beatrixlaan 35", zip_code="2595 AK",
    )

    result, _ = lookup_module.lookup_entity(
        entity, _FuzzyOnlyGleif(GSAM_RECORDS),
    )

    assert result.match_type == MatchType.FULL_MATCH
    assert result.lei == BV_LEI


@pytest.mark.parametrize("name, fuzzy", [
    # A typo that turns the name into another word is no match.
    ("Merlin Capital s.r.o.", "Marlin Capital s.r.o."),
    ("Alfa Bank", "Alpha Bank"),
])
def test_a_suggested_name_with_another_word_is_not_matched(name, fuzzy):
    record = _gsam("X" * 20, fuzzy, "CZ", "Praha", "Na Prikope 1", "11000")
    entity = InputEntity(
        name=name, country="CZ", town="Praha",
        street="Na Prikope 1", zip_code="11000",
    )

    result, _ = lookup_module.lookup_entity(entity, _FuzzyOnlyGleif([record]))

    assert result.lei is None


def test_the_fuzzy_search_waits_for_the_name_search_to_find_nothing():
    client = _FuzzyOnlyGleif(GSAM_RECORDS, by_name=GSAM_RECORDS[:1])

    lookup_module.lookup_entity(InputEntity(name=TYPO), client)

    assert client.fuzzy_queries == []

