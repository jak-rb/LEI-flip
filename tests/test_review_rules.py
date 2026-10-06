
"""Tests of the reviewers' feedback of 2026-10-06 (e-mail and meeting).

One section per point: names that agree only in a legal form, or that
differ in initials, are no match; an LEI that is not ISSUED is a stop,
never matched or accepted; a candidate found by OpenFIGI's issuer name
must agree with the address given; and the validation stepper shows the
whole searched row, the name, city and address scores, the LEI status
and an Accept button instead of the overall match. The cases are the
reviewers' own (Euro-Holdings, FISS, Redwire, Billington, JPMorgan).
GLEIF and OpenFIGI are faked; nothing touches the network.
"""

import csv
import io
import json
import re
import secrets

import pytest

import app as app_module
from core import isin as isin_module
from core import lookup as lookup_module
from core import storage
from core.address import normalize_name
from core.constants import NAME_MATCH_THRESHOLD
from core.matcher import name_similarity
from core.models import (
    GleifAddress,
    GleifCandidate,
    InputEntity,
    MatchType,
)

REDWIRE_ISIN = "US75776W1036"
REDWIRE_LEI = "5493004R8KDZC5RS4U44"


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


def _address(country, city, street, zip_code):
    return GleifAddress(
        country=country, city=city, postal_code=zip_code,
        address_lines=[street],
    )


def _candidate(name, lei, country="CZ", city="Praha", street="Hlavni 1",
               zip_code="11000", status="ISSUED", hq=None):
    """A candidate; its HQ address is its legal one unless ``hq``."""
    legal = _address(country, city, street, zip_code)
    return GleifCandidate(
        lei=lei, legal_name=name, status=status,
        legal_address=legal, hq_address=hq or legal,
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


def _stored_job(rows):
    """Store a finished search of (entity, result, closest) rows."""
    job_id = secrets.token_hex(16)
    storage.create_search(
        job_id, "bulk",
        [app_module._entity_input(entity) for entity, _, _ in rows],
    )
    storage.append_results(
        job_id, [app_module._result_row(*row) for row in rows], 0,
    )
    return job_id


def _lookup_job(*lookups):
    """Store a finished search of (entity, client) lookups."""
    return _stored_job([
        (entity, *lookup_module.lookup_entity(entity, client))
        for entity, client in lookups
    ])


def _page(job_id):
    return app_module.app.test_client().get(
        f"/results?job={job_id}",
    ).get_data(as_text=True)


def _card_counts(page):
    return {
        key: int(re.search(rf'id="metric-{key}">(\d+)<', page).group(1))
        for key in ("matched", "validate", "unmatched")
    }


def _exported(job_id):
    text = app_module.app.test_client().get(
        f"/download/csv?job={job_id}",
    ).get_data(as_text=True)
    header, *rows = csv.reader(io.StringIO(text[1:]))
    return [dict(zip(header, row)) for row in rows]


# ---- names: legal forms and initials ----

@pytest.mark.parametrize("name", [
    "FISS, spol. s r.o.", "FISS spol. s r.o.", "FISS, spol. s r. o.",
    "FISS spol.s r.o.", "FISS, spol. s.r.o.", "FISS, s.r.o.",
    "FISS, společnost s ručením omezeným", "FISS, a. s.", "FISS k. s.",
])
def test_czech_legal_forms_are_stripped(name):
    assert normalize_name(name) == "fiss"


def test_a_branch_keeps_its_words_after_a_spaced_legal_form():
    assert normalize_name("ČEZ, a. s., odštěpný závod") == (
        "cez odstepny zavod"
    )


@pytest.mark.parametrize("other", [
    "BRŮZA spol. s r.o.", "IZOSAN spol. s r.o.", "VISUS spol. s r.o.",
])
def test_a_shared_legal_form_adds_nothing_to_a_name_score(other):
    # "It matches only in a.s., s.r.o. and such: that is no match at
    # all" - the unstripped "spol. s r.o." made each of these 70.
    bare = other.removesuffix(" spol. s r.o.")
    assert name_similarity("FISS, spol. s r.o.", other) == (
        name_similarity("FISS", bare)
    )
    assert name_similarity("FISS, spol. s r.o.", "BRŮZA spol. s r.o.") == 0


@pytest.mark.parametrize("searched, other", [
    # "The names are certainly not a 100% match" (it was 100).
    ("Euro-Holdings Ltd", "EURO F.D. HOLDINGS S.A."),
    ("Morgan", "J.P. Morgan"),
    ("Holdings Ltd", "C.D. Holdings Ltd"),
])
def test_initials_tell_names_apart(searched, other):
    assert name_similarity(searched, other) < NAME_MATCH_THRESHOLD


@pytest.mark.parametrize("searched, other", [
    ("J.P. Morgan Chase & Co.", "JPMorgan Chase & Co."),
    ("ABC Trading", "A.B.C. Trading"),
    ("Euro-Holdings Ltd", "EuroHoldings Limited"),
    # Trailing letters are a legal form the list does not know.
    ("XYZ Partners, L.P.", "XYZ Partners"),
    ("Citibank, N.A.", "Citibank"),
    # Letters that spell a legal form, and "&" between letters.
    ("J&T Dividend Fund", "J&T SICAV P.L.C. - J&T Dividend Fund"),
    ("AT&T Inc.", "AT&T INC."),
    ("ČEZ, a. s.", "ČEZ"),
])
def test_initials_and_spacing_of_the_same_name_still_match(searched, other):
    assert name_similarity(searched, other) == 100


def test_candidates_agreeing_only_in_a_legal_form_are_not_offered():
    # FISS in Sobotovice: GLEIF's search lists firms sharing nothing
    # but "spol. s r.o."; they must not show at all.
    entity = InputEntity(
        name="FISS, spol. s r.o.", country="Česká republika",
        town="Sobotovice", street="Sobotovice 168", zip_code="66467",
    )
    others = [
        _candidate("BRŮZA spol. s r.o.", "B" * 20, city="Brňany"),
        _candidate("IZOSAN spol. s r.o.", "I" * 20, city="Vracovice"),
        _candidate("VISUS spol. s r.o.", "V" * 20),
    ]
    result, closest = lookup_module.lookup_entity(
        entity, _CannedGleif(others),
    )
    assert result.lei is None and closest == []

    kin = _candidate("FISS TRADE spol. s r.o.", "K" * 20, city="Brno")
    _, closest = lookup_module.lookup_entity(
        entity, _CannedGleif([*others, kin]),
    )
    assert [c.lei for c in closest] == ["K" * 20]


def test_the_euro_holdings_candidate_no_longer_scores_100():
    entity = InputEntity(
        name="Euro-Holdings Ltd", isin="MHY234DY1099",
        country="United Kingdom", town="Birmingham",
        street="Unit 1-3 Stratford Street North", zip_code="B11 1BU",
    )
    euro_fd = _candidate(
        "EURO F.D. HOLDINGS S.A.", "2" * 20, country="LU",
        city="LUXEMBOURG", street="10, RUE HENRI M. SCHNADT",
        zip_code="L-2530",
    )
    result, closest = lookup_module.lookup_entity(
        entity, _CannedGleif([euro_fd]),
    )
    assert result.lei is None
    assert [c.name_score for c in closest] == [70.0]


# ---- an LEI that is not ISSUED is a stop ----

def _billington(status="LAPSED"):
    return _candidate(
        "BILLINGTON HOLDINGS PLC", "B" * 20, country="GB", city="BARNSLEY",
        street="BARNSLEY ROAD, WOMBWELL", zip_code="S73 8DS",
        status=status,
    )


def _billington_entity():
    return InputEntity(
        name="Billington Holdings PLC", isin="GB0000332667",
        country="United Kingdom", town="Barnsley",
        street="Steel House, Barnsley Rd, Wombwell", zip_code="S73 8DS",
    )


@pytest.mark.parametrize("status", [
    "LAPSED", "RETIRED", "MERGED", "ANNULLED", "PENDING_TRANSFER",
])
def test_a_full_match_whose_lei_is_not_issued_is_never_asserted(status):
    result, closest = lookup_module.lookup_entity(
        _billington_entity(), _CannedGleif([_billington(status)]),
    )
    assert result.lei is None
    assert result.match_type == MatchType.NO_MATCH
    assert result.notes == (
        f"Name and legal address match BILLINGTON HOLDINGS PLC (LEI "
        f"{'B' * 20}), but its LEI status is {status} - the LEI cannot "
        f"be used and was not assigned."
    )
    assert [(c.lei, c.status) for c in closest] == [("B" * 20, status)]


def test_a_stopped_lei_never_appears_in_the_matched_records():
    # Billington Holdings PLC came out matched at 100% with "LEI
    # LAPSED": "it is a stop ... throw it out", nothing to decide.
    job_id = _lookup_job(
        (_billington_entity(), _CannedGleif([_billington()])),
    )
    page = _page(job_id)
    assert "No matched records." in page
    assert 'class="validation"' not in page
    assert _card_counts(page) == {"matched": 0, "validate": 0, "unmatched": 1}
    nomatch = page.split('class="results-table table-nomatch"', 1)[1]
    assert "LAPSED - the LEI cannot be used" in nomatch
    assert "LAPSED - nelze ho použít" in nomatch
    exported = _exported(job_id)[0]
    assert (exported["LEI"], exported["Match_type"]) == ("", "NO_MATCH")
    assert exported["LEI_status"] == "LAPSED"


def test_a_live_lei_is_still_asserted_beside_its_stopped_twin():
    live = _billington("ISSUED").model_copy(update={"lei": "L" * 20})
    result, closest = lookup_module.lookup_entity(
        _billington_entity(), _CannedGleif([_billington(), live]),
    )
    assert (result.lei, result.lei_status) == ("L" * 20, "ISSUED")
    assert [c.lei for c in closest] == ["B" * 20]


def test_the_isin_paths_never_assert_a_stopped_lei(monkeypatch):
    entity = InputEntity(name="Billington Holdings PLC", isin="GB0000332667")
    retired = _billington("RETIRED")
    # The direct ISIN hit, and the strong name near-miss it confirms.
    result, _ = isin_module.resolve_via_isin(
        entity, _CannedGleif(by_isin=[retired]), name_candidate=retired,
    )
    assert result is None
    # The HQ near-miss it confirms.
    assert isin_module._isin_confirms_hq(
        entity, "GB0000332667", [retired], retired,
    ) is None
    # The OpenFIGI fallback: shown for review, saying why.
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names",
        lambda *a, **k: ["BILLINGTON HOLDINGS PLC"],
    )
    result, to_review = isin_module.resolve_via_isin(
        entity, _CannedGleif(by_name=[retired]),
    )
    assert result.lei is None and to_review == [retired]
    assert result.notes.endswith(
        f"GLEIF has BILLINGTON HOLDINGS PLC (LEI {'B' * 20}) under that "
        "name, but its LEI status is RETIRED - the LEI cannot be used and "
        "was not assigned."
    )


def test_a_stopped_twin_still_makes_the_openfigi_fallback_ambiguous(
    monkeypatch,
):
    # Found by the live replay of 2026-10-06: OpenFIGI's "X-ENERGY INC"
    # finds two Italian "X ENERGY S.R.L.", one ISSUED and one LAPSED.
    # The stopped one still makes the pick ambiguous, as before, so the
    # live one is not asserted for a US company even with no address
    # to check.
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: ["X-ENERGY INC"],
    )
    live = _candidate("X ENERGY S.R.L.", "L" * 20, country="IT", city="Roma")
    lapsed = _candidate(
        "X ENERGY S.R.L.", "D" * 20, country="IT", city="Roma",
        status="LAPSED",
    )
    entity = InputEntity(
        name="X-Energy Inc", isin="US98386P1021", town="Roma",
    )
    assert isin_module.resolve_via_isin(
        entity, _CannedGleif(by_name=[live, lapsed]),
    ) == (None, [])


def test_an_isin_only_search_never_asserts_a_stopped_lei():
    result, closest = lookup_module.lookup_entity(
        InputEntity(isin="GB0000332667"),
        _CannedGleif(by_isin=[_billington("RETIRED")]),
    )
    assert result.lei is None
    assert "RETIRED - the LEI cannot be used" in result.notes
    assert [c.status for c in closest] == ["RETIRED"]


def _jpmorgan_job():
    """JPMorgan Funds SICAV: a RETIRED sub-fund next to an ISSUED one."""
    entity = InputEntity(
        name="JPMorgan Funds SICAV", isin="LU2539336078",
        country="Luxembourg", town="Senningerberg",
        street="6 route de Treves", zip_code="L-2633",
    )
    retired = _candidate(
        "JPMORGAN FUNDS - SYSTEMATIC ALPHA FUND", "5493001Z0FWMVJEGJS92",
        country="LU", city="Senningerberg", street="6, Route de Trèves",
        zip_code="L-2633", status="RETIRED",
    )
    issued = _candidate(
        "JPMORGAN FUNDS - GLOBAL BOND FUND", "5493000000000000JPMB",
        country="LU", city="Senningerberg", street="6, Route de Trèves",
        zip_code="L-2633",
    )
    return _lookup_job((entity, _CannedGleif([retired, issued])))


def test_a_stopped_candidate_is_shown_only_to_be_seen():
    page = _page(_jpmorgan_job())
    stepper = page.split('class="validation"', 1)[1]
    rows = re.findall(
        r'<tr class="candidate-row( is-stopped)?" data-lei="(\w+)">(.*?)</tr>',
        stepper, re.DOTALL,
    )
    by_lei = {lei: (stopped, row) for stopped, lei, row in rows}
    stopped, row = by_lei["5493001Z0FWMVJEGJS92"]
    assert stopped and "lei-status-stop" in row and "RETIRED" in row
    assert "candidate-confirm" not in row
    assert 'data-en="View only"' in row and 'data-cs="Jen k nahlédnutí"' in row
    live, row = by_lei["5493000000000000JPMB"]
    assert not live and "lei-status-ok" in row
    assert 'data-en="Accept"' in row and 'data-cs="Přijmout"' in row


def test_a_stopped_candidate_cannot_be_accepted():
    job_id = _jpmorgan_job()
    client = app_module.app.test_client()
    refused = client.post("/api/decision", json={
        "job_id": job_id, "index": 0, "choice": "5493001Z0FWMVJEGJS92",
    })
    assert refused.status_code == 404
    accepted = client.post("/api/decision", json={
        "job_id": job_id, "index": 0, "choice": "5493000000000000JPMB",
    })
    assert accepted.status_code == 200
    assert accepted.get_json()["counts"]["matched"] == 1


def test_an_older_acceptance_of_a_stopped_lei_no_longer_counts():
    # Decisions saved before 2026-10-06 could confirm any candidate.
    job_id = _jpmorgan_job()
    search = storage.get_search(job_id)
    search["results"][0]["decision"] = {
        "status": "confirmed", "lei": "5493001Z0FWMVJEGJS92",
    }
    with storage._connect() as conn:
        conn.execute(
            "UPDATE searches SET results = %s WHERE job_id = %s",
            (json.dumps(search["results"]), job_id),
        )
    page = _page(job_id)
    assert "No matched records." in page
    assert _card_counts(page)["validate"] == 1
    assert _exported(job_id)[0]["Match_type"] == "NO_MATCH"


# ---- OpenFIGI's issuer name must agree with the address given ----

def _redwire(hq_city="Jacksonville"):
    """REDWIRE CORPORATION as GLEIF has it: a Delaware agent's seat."""
    return _candidate(
        "REDWIRE CORPORATION", REDWIRE_LEI, country="US",
        city="WILMINGTON",
        street="c/o THE CORPORATION TRUST COMPANY, CORPORATION TRUST "
               "CENTER, 1209 ORANGE STREET",
        zip_code="19801",
        hq=_address(
            "US", hq_city, "8226 PHILIPS HIGHWAY, SUITE 101", "32256",
        ),
    )


def _redwire_entity(**address):
    fields = {
        "country": "USA", "town": "Jacksonville",
        "street": "8226 Philips Highway, Suite 102", "zip_code": "FL 32256",
    }
    fields.update(address)
    return InputEntity(name="Redwire Corporation", isin=REDWIRE_ISIN, **fields)


@pytest.fixture
def _redwire_figi(monkeypatch):
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: ["REDWIRE CORP"],
    )


@pytest.mark.usefixtures("_redwire_figi")
def test_an_openfigi_candidate_whose_address_disagrees_goes_to_review():
    # "Here it skipped the address check and approved the record - it
    # must go to review too."
    entity = _redwire_entity()
    redwire = _redwire(hq_city="Wilmington")
    result, closest = lookup_module.lookup_entity(
        entity, _CannedGleif([redwire]),
    )
    assert result.lei is None and result.match_type == MatchType.NO_MATCH
    assert result.notes == (
        f"ISIN {REDWIRE_ISIN} resolved via OpenFIGI to the issuer REDWIRE "
        "CORP, and GLEIF has REDWIRE CORPORATION under that name, but its "
        "address does not match the one given - LEI not assigned. Please "
        "review."
    )
    assert [c.lei for c in closest] == [REDWIRE_LEI]

    page = _page(_stored_job([(entity, result, closest)]))
    assert _card_counts(page) == {"matched": 0, "validate": 1, "unmatched": 0}
    stepper = page.split('class="validation"', 1)[1]
    assert "resolved via OpenFIGI to the issuer REDWIRE CORP" in stepper
    assert "Podle OpenFIGI patří ISIN" in stepper


@pytest.mark.usefixtures("_redwire_figi")
def test_an_openfigi_candidate_with_no_address_to_check_goes_to_review():
    entity = InputEntity(name="Redwire Corporation", isin=REDWIRE_ISIN)
    result, closest = lookup_module.lookup_entity(
        entity, _CannedGleif([_redwire()]),
    )
    assert result.lei is None
    assert "no address was given to check it against" in result.notes
    assert [c.lei for c in closest] == [REDWIRE_LEI]


@pytest.mark.usefixtures("_redwire_figi")
def test_an_openfigi_candidate_whose_hq_agrees_is_asserted_and_says_so():
    result, _ = lookup_module.lookup_entity(
        _redwire_entity(), _CannedGleif([_redwire()]),
    )
    assert (result.lei, result.match_type) == (
        REDWIRE_LEI, MatchType.ISIN_OPENFIGI_MATCH,
    )
    assert result.warnings == ["ISIN_VIA_OPENFIGI", "HQ_ONLY_MATCH"]
    assert result.notes.endswith(
        "and its headquarters address matches the one given."
    )


@pytest.mark.usefixtures("_redwire_figi")
def test_an_openfigi_candidate_found_only_by_that_name_is_offered(
    monkeypatch,
):
    # GLEIF's search by the typed name finds nothing; only OpenFIGI's
    # name finds the record, which must then reach the review.
    class _OnlyByFigiName(_CannedGleif):
        def search_by_name(self, name, country=None, page_size=10):
            return self.by_name if name == "REDWIRE CORP" else []

        def search_by_name_no_country(self, name, page_size=10):
            return self.search_by_name(name)

    redwire = _redwire(hq_city="Wilmington").model_copy(
        update={"other_names": ["Redwire Corp."]},
    )
    entity = _redwire_entity().model_copy(update={"name": "Redwire Corp"})
    result, closest = lookup_module.lookup_entity(
        entity, _OnlyByFigiName([redwire]),
    )
    assert result.lei is None
    assert [c.lei for c in closest] == [REDWIRE_LEI]


# ---- the validation stepper ----

def _review_job():
    """A near-miss: the address agrees, but "Group" caps the name."""
    entity = InputEntity(
        name="Alpha Holding a.s.", isin="CZ0009010468", country="CZ",
        town="Brno", street="Hlavni 1", zip_code="602 00",
    )
    near = _candidate(
        "Alpha Holding Group a.s.", "A" * 20, city="Brno", street="Jina 5",
        zip_code="11000",
    )
    return _lookup_job((entity, _CannedGleif([near])))


def test_the_stepper_shows_the_whole_searched_row():
    page = _page(_review_job())
    searched = re.search(
        r'<p class="detail-searched">(.*?)</p>', page, re.DOTALL,
    ).group(1)
    text = " ".join(re.sub(r"<[^>]+>", " ", searched).split())
    assert text == (
        "You searched: Alpha Holding a.s. ISIN CZ0009010468 Country CZ "
        "City Brno Street Hlavni 1 Postal code 602 00"
    )


def test_the_stepper_shows_an_isin_only_row_by_its_isin_once():
    entity = InputEntity(isin="CZ0009010468", country="CZ")
    records = [
        _candidate("Alpha Holding a.s.", "A" * 20),
        _candidate("Beta Holding a.s.", "B" * 20),
    ]
    page = _page(_lookup_job((entity, _CannedGleif(by_isin=records))))
    searched = re.search(
        r'<p class="detail-searched">(.*?)</p>', page, re.DOTALL,
    ).group(1)
    assert " ".join(re.sub(r"<[^>]+>", " ", searched).split()) == (
        "You searched: CZ0009010468 Country CZ"
    )


def test_the_candidate_table_shows_scores_and_status_not_overall():
    page = _page(_review_job())
    table = page.split('class="results-table candidates-table"', 1)[1]
    table = table.split("</table>", 1)[0]
    for english, czech in (
        ("Name score", "Skóre názvu"), ("City score", "Skóre města"),
        ("Address score", "Skóre adresy"), ("LEI status", "Stav LEI"),
    ):
        assert f'data-cs="{czech}" data-en="{english}"' in table
    assert "Overall match" not in table and "%" not in table
    assert "Correct match" not in page
    # The name (70, capped by "Group") fails; the city passes; the street
    # "Jina 5" scores 44.4 against "Hlavni 1" and the ZIP disagrees, so
    # the address is (35 * 44.4 + 25 * 0) / 60, about 26: a fail.
    cells = re.findall(r'class="col-score score-(\w+)">([^<]*)<', table)
    assert cells[0] == ("fail", "70") and cells[1] == ("pass", "100")
    assert cells[2] == ("fail", "26")
    assert 'class="lei-status lei-status-ok">ISSUED<' in table


def test_a_score_with_nothing_to_compare_shows_a_dash():
    entity = InputEntity(name="Alpha Holding a.s.")
    near = _candidate("Alpha Holding a.s.", "A" * 20, city="Brno")
    page = _page(_lookup_job((entity, _CannedGleif([near]))))
    cells = re.findall(r'class="col-score score-(\w+)">([^<]*)<', page)
    assert cells == [("pass", "100"), ("na", "–"), ("na", "–")]


def test_the_stepper_explains_why_a_row_was_not_matched():
    # An HQ-only near-miss says so above its candidates; the plain "not
    # found" would only contradict them and is left out.
    hq_only = InputEntity(name="Alpha Holding a.s.", town="Praha")
    alpha = _candidate(
        "Alpha Holding a.s.", "A" * 20, city="Brno",
        hq=_address("CZ", "Praha", "Hq 2", "11000"),
    )
    plain = InputEntity(name="Alpha Holding Group", town="Ostrava")
    group = _candidate("Alpha Holding a.s.", "G" * 20, city="Brno")
    page = _page(_lookup_job(
        (hq_only, _CannedGleif([alpha])), (plain, _CannedGleif([group])),
    ))
    notes = re.findall(r'<p class="detail-note"\s+data-en="([^"]*)"', page)
    assert len(notes) == 1 and "matches only the headquarters" in notes[0]
    assert page.count('class="validate-record"') == 2


def test_help_explains_accept_and_the_issued_rule():
    page = app_module.app.test_client().get("/").get_data(as_text=True)
    assert "<strong>Accept</strong>" in page
    assert "<strong>Přijmout</strong>" in page
    assert "Only an <strong>ISSUED</strong> LEI can be used." in page
    assert "Použít lze jen LEI ve stavu" in page


# ---- the review table's address score ----

@pytest.mark.parametrize("street, zip_code, expected", [
    ("Hlavni 1", None, 100.0),        # the street alone
    (None, "11000", 100.0),           # the ZIP alone
    ("Hlavni 1", "99999", 58.3),      # street agrees, ZIP does not
    (None, None, None),               # nothing to compare
])
def test_the_address_score_weighs_street_and_zip(street, zip_code, expected):
    entity = InputEntity(
        name="Alpha Holding a.s.", street=street, zip_code=zip_code,
    )
    _, closest = lookup_module.lookup_entity(
        entity, _CannedGleif([_candidate("Alpha Holding a.s.", "A" * 20)]),
    )
    assert closest[0].address_score == expected

