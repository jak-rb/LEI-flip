
"""Tests of rows pasted into the bulk form (core/upload.py, app.py).

Most tests call parse_pasted_rows directly; the route tests create a
job from pasted rows, with the lookup faked.
"""

import time

import pytest

import app as app_module
from core import storage
from core.models import InputError, LookupResult
from core.upload import MAX_ENTITIES, parse_pasted_rows

#: The colleague's bank export: its header and a row with no ISIN.
HEADER = (
    "PARTY_FULL_NAME", "ISIN_IDENT", "COUNTRY_CODE", "ADDR_CITY_NAME",
    "ADDR_STREET_NAME", "ADDR_ZIP_CODE",
)
ADVISO = (
    "Adviso Finance s.r.o.", None, "CZ", "Praha", "Školská 1736/12", "11000",
)


def _fields(text):
    """Each parsed entity as (name, isin, country, city, street, zip)."""
    return [
        (e.name, e.isin, e.country, e.town, e.street, e.zip_code)
        for e in parse_pasted_rows(text)
    ]


def _refusal(text):
    """The (English, Czech) message parse_pasted_rows refuses it with."""
    with pytest.raises(InputError) as caught:
        parse_pasted_rows(text)
    return str(caught.value), caught.value.message_cs


class _FakeGleifClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client(monkeypatch):
    def _no_match(entity, client_):
        return LookupResult(notes="No LEI found in the GLEIF database."), []
    monkeypatch.setattr(app_module, "lookup_entity", _no_match)
    monkeypatch.setattr(app_module, "GleifClient", _FakeGleifClient)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# Rows copied from Excel: tab-separated, read by position.

def test_rows_copied_from_excel_keep_an_empty_isin_cell():
    text = (
        "\t".join(HEADER) + "\r\n"
        "Adviso Finance s.r.o.\t\tCZ\tPraha\tŠkolská 1736/12\t11000\r\n"
        "FIRY INC\tUS83067L2088\tUS\tSan Francisco\t1061 Market St,"
        "\tCA 94103\r\n"
    )
    assert _fields(text) == [ADVISO, (
        "FIRY INC", "US83067L2088", "US", "San Francisco",
        "1061 Market St,", "CA 94103",
    )]


def test_a_cell_excel_quoted_keeps_its_line_break():
    text = 'Alfa a.s.\t\tCZ\tPraha\t"Ulice 1\nBudova B"\t11000\n'
    assert _fields(text)[0][4] == "Ulice 1\nBudova B"


def test_rows_without_a_header_are_all_searched():
    text = "Alfa a.s.\t\tCZ\nBeta s.r.o.\tCZ0005112300\tCZ\n"
    assert [row[:3] for row in _fields(text)] == [
        ("Alfa a.s.", None, "CZ"),
        ("Beta s.r.o.", "CZ0005112300", "CZ"),
    ]


# Text without tabs.

def test_rows_whose_tabs_became_spaces_find_the_country():
    # As the request arrived: the e-mail turned every tab into two
    # spaces, so the empty ISIN cell left four.
    text = (
        "  ".join(HEADER) + "\n"
        "Adviso Finance s.r.o.    CZ  Praha  Školská 1736/12  11000"
    )
    assert _fields(text) == [ADVISO]


@pytest.mark.parametrize(("line", "expected"), [
    # An ISIN in its place: every column by position.
    ("FIRY INC  US83067L2088  US  San Francisco  1061 Market St,  "
     "CA 94103",
     ("FIRY INC", "US83067L2088", "US", "San Francisco",
      "1061 Market St,", "CA 94103")),
    # A country by name after an empty ISIN cell, and a name with its
    # country code alone.
    ("Alfa a.s.    Czech Republic  Praha",
     ("Alfa a.s.", None, "Czech Republic", "Praha", None, None)),
    ("Adviso Finance s.r.o.  CZ",
     ("Adviso Finance s.r.o.", None, "CZ", None, None, None)),
    # No ISIN column at all, and a country code right after the name.
    ("Alfa S.A.  LU  Luxembourg  2 Bd Adenauer",
     ("Alfa S.A.", None, "LU", "Luxembourg", "2 Bd Adenauer", None)),
    # An ISIN with no name before it.
    ("CZ0005112300  CZ  Praha",
     (None, "CZ0005112300", "CZ", "Praha", None, None)),
])
def test_spaced_columns_are_placed_by_what_they_hold(line, expected):
    assert _fields(line) == [expected]


@pytest.mark.parametrize(("line", "expected"), [
    # A doubled space inside the name: the name ends where the empty
    # ISIN cell left its wider gap, joined by single spaces, even where
    # a piece of it is a country code or a country too.
    ("Allianz  SE    DE  München  Königinstr. 28  80802",
     ("Allianz SE", None, "DE", "München", "Königinstr. 28", "80802")),
    ("Erste Group Bank  AG    AT  Wien  Am Belvedere 1  1100",
     ("Erste Group Bank AG", None, "AT", "Wien", "Am Belvedere 1", "1100")),
    ("Komerční banka,  a.s.    CZ  Praha  Na Příkopě 33  11407",
     ("Komerční banka, a.s.", None, "CZ", "Praha", "Na Příkopě 33",
      "11407")),
    ("Deutsche Bank  Luxembourg    LU  Luxembourg  2 Bd Adenauer  1115",
     ("Deutsche Bank Luxembourg", None, "LU", "Luxembourg",
      "2 Bd Adenauer", "1115")),
    ("Bank of America  NA    US  Charlotte  100 N Tryon St  28255",
     ("Bank of America NA", None, "US", "Charlotte", "100 N Tryon St",
      "28255")),
    ("Alfa  Pension Fund 1    CZ  Praha",
     ("Alfa Pension Fund 1", None, "CZ", "Praha", None, None)),
    ("Volksbank Mittelhessen  eG    DE  Gießen",
     ("Volksbank Mittelhessen eG", None, "DE", "Gießen", None, None)),
    ("Ford Motor  Company    US  Dearborn",
     ("Ford Motor Company", None, "US", "Dearborn", None, None)),
    ("Volvo AB    SE  Göteborg",
     ("Volvo AB", None, "SE", "Göteborg", None, None)),
    ("ArcelorMittal S.A.    Luxembourg  Luxembourg  24 Bd d'Avranches",
     ("ArcelorMittal S.A.", None, "Luxembourg", "Luxembourg",
      "24 Bd d'Avranches", None)),
    # ... or at a valid ISIN, or at a placeholder before the country.
    ("Allianz  SE  DE0008404005  DE  München",
     ("Allianz SE", "DE0008404005", "DE", "München", None, None)),
    ("Allianz  SE  #N/A  DE  München",
     ("Allianz SE", None, "DE", "München", None, None)),
    # A Slovak VAT number in a column past the six is no ISIN.
    ("Tatra banka, a.s.  SK  Bratislava  Hodžovo námestie 3  81106  "
     "SK2020408522",
     ("Tatra banka, a.s.", None, "SK", "Bratislava",
      "Hodžovo námestie 3", "81106")),
])
def test_a_spaced_name_ends_at_its_isin_or_country(line, expected):
    assert _fields(line) == [expected]


@pytest.mark.parametrize("line", [
    # Could be cut short: "Bank of America" is the holding's name, and
    # "Pension Fund 1" once read as an ISIN with its spaces dropped;
    # "#2" ends a sub-fund's name, not an Excel error value.
    "Bank of America  NA  US  Charlotte",
    "Alfa  Pension Fund 1  CZ  Praha",
    "Allianz  SE",
    "Alfa Fund  #2  CZ  Praha  Rohanské nábřeží 693/10  18600",
    "Alfa Bond 2023",
])
def test_a_spaced_line_without_a_sure_end_of_name_is_one_name(line):
    assert _fields(line) == [(line, None, None, None, None, None)]


@pytest.mark.parametrize("placeholder", [
    "#N/A", "#NENÍ_K_DISPOZICI", "N/A", "n/a", "NULL", "0", "-", "nan",
    "ISIN not available",
])
def test_a_placeholder_isin_before_the_country_is_no_isin(placeholder):
    line = f"Adviso Finance s.r.o.  {placeholder}  CZ  Praha"
    assert _fields(line) == [
        ("Adviso Finance s.r.o.", None, "CZ", "Praha", None, None),
    ]


@pytest.mark.parametrize(("line", "expected"), [
    # An empty cell's wider gap after a "-" or "#2" in the name wins.
    ("Beta Funds SICAV  -  Luxembourg    LU  Luxembourg  5 allée Scheffer",
     ("Beta Funds SICAV - Luxembourg", None, "LU", "Luxembourg",
      "5 allée Scheffer", None)),
    ("Alfa Fund  #2    CZ  Praha",
     ("Alfa Fund #2", None, "CZ", "Praha", None, None)),
    # A name that joins into an ISIN shape stays a name.
    ("Alfa Bond 2023    CZ  Praha  Rybná 682/14  11000",
     ("Alfa Bond 2023", None, "CZ", "Praha", "Rybná 682/14", "11000")),
    # A ";" inside an e-mailed row's cell is text, not a delimiter.
    ("Alfa a.s.    CZ  Praha  Ulice 1; budova B  11000",
     ("Alfa a.s.", None, "CZ", "Praha", "Ulice 1; budova B", "11000")),
    # A doubled space inside the city moves the street into the postal
    # code: the row keeps its name and country, not the whole paste
    # refused.
    ("ŠKODA AUTO a.s.    CZ  Mladá  Boleslav  tř. Václava Klementa 869  "
     "29301",
     ("ŠKODA AUTO a.s.", None, "CZ", None, None, None)),
])
def test_a_spaced_row_is_not_misled_by_its_cells(line, expected):
    assert _fields(line) == [expected]


def test_na_with_no_country_after_it_is_namibia():
    assert _fields("Foo Ltd    NA  Windhoek") == [
        ("Foo Ltd", None, "NA", "Windhoek", None, None),
    ]


@pytest.mark.parametrize("header", [
    "  ".join(HEADER), "Short name  ISIN  Country  City",
    "CPTY_NAME  ISIN  COUNTRY  CITY  STREET  ZIP",
])
def test_a_spaced_header_is_skipped(header):
    assert _fields(header + "\nAlfa a.s.    CZ  Praha") == [
        ("Alfa a.s.", None, "CZ", "Praha", None, None),
    ]


def test_semicolon_lines_are_split_at_their_semicolons():
    text = (
        '"FIRY INC";"US83067L2088";"US";"San Francisco";"1061 Market St,";'
        '"CA 94103"\n'
        "Adviso Finance s.r.o.;;CZ;Praha;Školská 1736/12;11000\n"
    )
    assert _fields(text) == [(
        "FIRY INC", "US83067L2088", "US", "San Francisco",
        "1061 Market St,", "CA 94103",
    ), ADVISO]


def test_a_column_of_names_keeps_each_name_whole():
    # Copied from one Excel column: no tabs, and commas, a semicolon or
    # doubled spaces inside the names. "SE" is a country code too, but
    # here the legal form that ends the name.
    names = [
        "ČEZ, a. s.", "Komerční banka, a.s.", "Apple; Inc",
        "Allianz  SE", "Adviso  Finance s.r.o.",
    ]
    assert [row[0] for row in _fields("\n".join(names))] == names


def test_a_column_of_isins_is_searched_by_isin():
    assert _fields("US83067L2088\nCZ0005112300\n") == [
        (None, "US83067L2088", None, None, None, None),
        (None, "CZ0005112300", None, None, None, None),
    ]


# Refusals.

@pytest.mark.parametrize("text", ["", "  \r\n\t \n", "\u200b"])
def test_blank_text_is_refused_in_both_languages(text):
    assert _refusal(text) == (
        "Please paste the rows to look up.",
        "Vložte prosím řádky k vyhledání.",
    )


def test_a_header_alone_finds_no_entities():
    english, czech = _refusal("\t".join(HEADER))
    assert english.startswith("No entities found.")
    assert czech.startswith("Nebyly nalezeny žádné subjekty.")


def test_more_than_the_cap_is_refused_per_search():
    text = "".join(f"Firma {index}\t\tCZ\n" for index in range(150))
    assert _refusal(text) == (
        f"Too many entities (more than {MAX_ENTITIES}). The maximum is "
        f"{MAX_ENTITIES} per search.",
        f"Příliš mnoho subjektů (více než {MAX_ENTITIES}). Maximum je "
        f"{MAX_ENTITIES} na jedno vyhledávání.",
    )


def test_a_too_long_value_names_its_pasted_line():
    text = "Alfa a.s.\n\nBeta " + "x" * 600 + "\n"
    english, czech = _refusal(text)
    assert english == "Row 3: the name is longer than 500 characters."
    assert czech == "Řádek 3: název je delší než 500 znaků."


def test_an_unbalanced_quote_gets_a_message_about_the_rows():
    text = '"Alfa\t\tCZ\n' + "x" * (200 * 1024)
    english, czech = _refusal(text)
    assert english.startswith("Could not read the pasted rows:")
    assert czech.startswith("Vložené řádky se nepodařilo přečíst:")


def test_megabytes_of_spaced_lines_are_read_quickly():
    # Placing spaced columns looks the second value up as a country,
    # which costs about a millisecond when it is none. Lines with a
    # name stop at the cap; lines without one never get that far.
    junk = "".join(
        f"\u200b  \u200b  Mesto {index}\n" for index in range(150_000)
    )
    started = time.perf_counter()
    assert _fields(junk + "Alfa a.s.  CZ  Praha\n") == [
        ("Alfa a.s.", None, "CZ", "Praha", None, None),
    ]
    english, _ = _refusal("".join(
        f"Firma {index}  Mesto {index}  Ulice\n" for index in range(150_000)
    ))
    assert english.startswith("Too many entities")
    assert time.perf_counter() - started < 10


def test_a_header_like_line_of_countless_values_is_read_quickly():
    # "ISIN" second makes the line a header candidate; only its six
    # columns are checked, each check costing a country lookup.
    line = "Alfa a.s.  ISIN  " + "  ".join(
        f"M{index}" for index in range(400_000)
    )
    started = time.perf_counter()
    english, _ = _refusal(line)
    assert english.startswith("Row 1: the name is longer than")
    assert time.perf_counter() - started < 10


def test_countless_open_parentheses_are_read_quickly():
    # The label check scanned such a value in quadratic time: 120,000
    # "(" took a minute, 4 MB in a header cell would take hours.
    started = time.perf_counter()
    english, _ = _refusal("Alfa  " + "(" * 120_000)
    assert english.startswith("Row 1: the name is longer than")
    assert time.perf_counter() - started < 10


# The route.

def test_pasted_rows_create_a_job_that_runs(client):
    text = "\t".join(HEADER) + "\nAlfa a.s.\t\tCZ\tPraha\nBeta s.r.o.\n"
    response = client.post("/api/jobs", data={"mode": "paste", "rows": text})
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 2

    search = storage.get_search(body["job_id"])
    assert search["mode"] == "paste"
    assert [record["name"] for record in search["query"]] == [
        "Alfa a.s.", "Beta s.r.o.",
    ]
    assert search["query"][0]["country"] == "CZ"

    progress = client.post(f"/api/jobs/{body['job_id']}/run").get_json()
    assert progress["done"] is True
    assert progress["unmatched"] == 2


@pytest.mark.parametrize(("rows", "english"), [
    ("", "Please paste the rows to look up."),
    ("\t".join(HEADER), "No entities found."),
])
def test_the_route_refuses_unusable_rows_in_both_languages(
    client, rows, english
):
    response = client.post("/api/jobs", data={"mode": "paste", "rows": rows})
    assert response.status_code == 400
    body = response.get_json()
    assert body["error"].startswith(english)
    assert body["error_cs"] and body["error_cs"] != body["error"]


def test_the_search_page_offers_the_paste_box(client):
    html = client.get("/").get_data(as_text=True)
    assert 'id="pasted_rows"' in html
    assert 'data-source="paste"' in html
    assert "Vložit řádky" in html and "Paste rows" in html
    # Help says what the spaced reading needs.
    assert "at least two spaces apart" in " ".join(html.split())
    assert "alespoň dvě mezery" in " ".join(html.split())

