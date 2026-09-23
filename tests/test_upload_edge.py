
"""Tests of upload edge cases found in the 2026-09-23 test run.

Excel's _xHHHH_ escapes, postal codes saved as zero-padded numbers,
UTF-8 with a few invalid bytes, cells holding only invisible
characters, and files named just ".csv". Most tests call parse_upload
directly; the route tests fake the lookup as tests/test_app.py does,
so nothing reaches GLEIF.
"""

import io
import time
import zipfile

import pytest
from openpyxl import Workbook, load_workbook
from pydantic import ValidationError

import app as app_module
from core import storage
from core.models import InputEntity, InputError, LookupResult
from core.upload import parse_upload

HEADER = ["Name", "ISIN", "Country", "City", "Street", "Postal code"]
GOOD_ROW = ["Alfa a.s.", "CZ0005112300", "CZ", "Praha", "Ulice 1", "110 00"]

#: Czech company names as users upload them. In cp1250, the last three
#: hold byte runs that are valid UTF-8 by chance ("ěšť", "ÍŠ", "ĚŠ").
CZECH_NAMES = [
    "Škoda Auto a.s.",
    "ČEZ, a. s.",
    "Komerční banka, a.s.",
    "Česká spořitelna, a.s.",
    "Plzeňský Prazdroj, a. s.",
    "Třinecké železárny, a. s.",
    "Žďas, a.s.",
    "Budějovický Budvar, národní podnik",
    "Ďáblická stavební s.r.o.",
    "Šťastný & Kůň, v.o.s.",
    "Měšťanský pivovar Havlíčkův Brod, a.s.",
    "PŘÍŠTÍ STOLETÍ s.r.o.",
    "MĚŠŤANSKÝ PIVOVAR POLIČKA, a.s.",
]

#: Cells that look empty: whitespace and Unicode format characters.
INVISIBLE = [
    "\u200b", "\ufeff", "\u2060", "\u200c\u200d", " \u200b ",
    "\u200b\ufeff\u2060",
]


class _FakeGleifClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_lookup(entity, client):
    return LookupResult(notes="No LEI found in the GLEIF database."), []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "lookup_entity", _fake_lookup)
    monkeypatch.setattr(app_module, "GleifClient", _FakeGleifClient)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _lines(names, delimiter=";"):
    """Text rows of the names, each with an empty ISIN and a country."""
    return "".join(f"{name}{delimiter}{delimiter}CZ\n" for name in names)


def _xlsx(rows):
    """An .xlsx of the rows; a cell may be a (value, format) pair."""
    workbook = Workbook()
    sheet = workbook.active
    for row_number, row in enumerate(rows, start=1):
        for column, value in enumerate(row, start=1):
            number_format = None
            if isinstance(value, tuple):
                value, number_format = value
            if value is None:
                continue
            cell = sheet.cell(row=row_number, column=column, value=value)
            if number_format:
                cell.number_format = number_format
    content = io.BytesIO()
    workbook.save(content)
    return content.getvalue()


def _with_sheet(content, sheet_xml):
    """Rewrite an .xlsx with the given worksheet XML."""
    source = zipfile.ZipFile(io.BytesIO(content))
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = sheet_xml
            target.writestr(item.filename, data)
    return result.getvalue()


def _names(content, filename="in.csv"):
    return [entity.name for entity in parse_upload(filename, content)]


def _entities(content, filename="in.csv"):
    return [
        (entity.name, entity.isin)
        for entity in parse_upload(filename, content)
    ]


def _create_bulk(client, content, filename):
    return client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def _stored_query(response):
    return storage.get_search(response.get_json()["job_id"])["query"]


# Excel's _xHHHH_ escapes (report #22).

def test_excel_escapes_in_xlsx_cells_are_decoded():
    # Excel saves a line break typed in a cell (Alt+Enter on Windows)
    # as "_x000D_" plus a newline, and text that really holds "_x000D_"
    # with its underscore escaped as "_x005F_".
    content = _xlsx([
        ["Firma_x000D_\na.s.", "CZ0005112300", "CZ", "Praha",
         "Na Příkopě 33_x000D_\n2. patro"],
        ["Literal _x005F_x000D_ s.r.o."],
        ["Plain_x s.r.o."],
    ])
    entities = parse_upload("in.xlsx", content)
    assert [entity.name for entity in entities] == [
        "Firma\r\na.s.", "Literal _x000D_ s.r.o.", "Plain_x s.r.o.",
    ]
    assert entities[0].street == "Na Příkopě 33\r\n2. patro"


def test_excel_escapes_in_semicolon_lines_are_decoded():
    # A vertical tab: Word's line break, pasted into a cell.
    content = _xlsx([
        ["Name;ISIN;Country"],
        ["Firma_x000B_a.s.;CZ0005112300;CZ"],
    ])
    assert _entities(content, "in.xlsx") == [
        ("Firma\x0ba.s.", "CZ0005112300"),
    ]


def test_a_decoded_control_character_still_downloads(client):
    # The Excel export drops characters XML forbids, such as the
    # vertical tab that "_x000B_" decodes to.
    content = _xlsx([["Bad_x000B_Name a.s.", None, "CZ"]])
    created = _create_bulk(client, content, "in.xlsx")
    assert created.status_code == 200, created.get_json()
    assert _stored_query(created)[0]["name"] == "Bad\x0bName a.s."

    job_id = created.get_json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/run").get_json()["done"]
    excel = client.get(f"/download/excel?job={job_id}")
    assert excel.status_code == 200
    sheet = load_workbook(io.BytesIO(excel.data)).active
    names = [row[0] for row in sheet.iter_rows(values_only=True)]
    assert "BadName a.s." in names


def test_a_semicolon_line_with_an_escaped_line_break_is_read():
    # Decoded before the line is split, the carriage return or line
    # feed would stand outside quotes, which the csv module refuses.
    content = _xlsx([
        ["Name;ISIN;Country"],
        ["Firma_x000D_a.s.;CZ0005112300;CZ"],
        ["Beta_x000A_s.r.o.;CZ0008019106_x000D_;CZ"],
        ['"Gama_x000D_\na.s.";CZ0005112300;CZ'],
    ])
    assert _entities(content, "in.xlsx") == [
        ("Firma\ra.s.", "CZ0005112300"),
        ("Beta\ns.r.o.", "CZ0008019106"),
        ("Gama\r\na.s.", "CZ0005112300"),
    ]


@pytest.mark.parametrize(("saved", "read"), [
    # An emoji saved as its two UTF-16 halves, and lone halves, which
    # are no character at all.
    ("Smile _xD83D__xDE00_ s.r.o.", "Smile \U0001f600 s.r.o."),
    ("Alfa_xD800_ a.s.", "Alfa\ufffd a.s."),
    ("Alfa_xDFFF_", "Alfa\ufffd"),
])
def test_escaped_surrogates_are_read_as_text(saved, read):
    assert _names(_xlsx([[saved, None, "CZ"]]), "in.xlsx") == [read]
    lines = _xlsx([["Name;ISIN;Country"], [f"{saved};;CZ"]])
    assert _names(lines, "in.xlsx") == [read]


def test_an_escaped_lone_surrogate_creates_a_job(client):
    content = _xlsx([["Alfa_xD800_ a.s.", None, "CZ"]])
    created = _create_bulk(client, content, "in.xlsx")
    assert created.status_code == 200, created.data[:200]
    assert _stored_query(created)[0]["name"] == "Alfa\ufffd a.s."


# Postal codes saved as zero-padded numbers (report #21).

def _postal_code(value):
    """The postal code read from an .xlsx whose column F holds value."""
    row = ["Firma a.s.", None, "SK", "Žilina", "Ulica 1", value]
    [entity] = parse_upload("in.xlsx", _xlsx([row]))
    return entity.zip_code


@pytest.mark.parametrize(("value", "number_format", "shown"), [
    (1001, "000 00", "010 01"),
    # How Excel saves the format, and a quoted space.
    (1001, "000\\ 00", "010 01"),
    (1001, '000" "00', "010 01"),
    (2110, "00000", "02110"),
    (800, "0000", "0800"),
    (21101234, "00000\\-0000", "02110-1234"),
    (1001.0, "000 00", "010 01"),
])
def test_a_zero_padded_postal_code_is_read_as_excel_shows_it(
    value, number_format, shown
):
    assert _postal_code((value, number_format)) == shown


@pytest.mark.parametrize(("value", "number_format", "read"), [
    (11000, None, "11000"),
    (1001, "0.00", "1001"),
    (1001, "#,##0", "1001"),
    (1234567, "000 00", "1234567"),
    (-1001, "000 00", "-1001"),
    (1001.5, "000 00", "1001.5"),
])
def test_other_postal_code_numbers_are_read_as_before(
    value, number_format, read
):
    assert _postal_code((value, number_format)) == read


def test_only_the_postal_code_column_keeps_format_zeros():
    row = ["Firma a.s.", None, "SK", (1001, "00000"), "Ulica 1",
           (1001, "00000")]
    [entity] = parse_upload("in.xlsx", _xlsx([row]))
    assert (entity.town, entity.zip_code) == ("1001", "01001")


def test_a_postal_code_with_an_undefined_style_is_read_as_a_number():
    # A writer may name a cell style its workbook does not define.
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>Firma a.s.</t></is></c>'
        '<c r="F1" s="9"><v>1001</v></c></row></sheetData></worksheet>'
    ).encode()
    content = _with_sheet(_xlsx([["x"]]), sheet)
    [entity] = parse_upload("in.xlsx", content)
    assert entity.zip_code == "1001"


# UTF-8 with a few invalid bytes (report #16).

def test_utf8_with_one_invalid_byte_stays_utf8():
    # The report's case: one stray byte used to make the whole file
    # decode as cp1250, garbling every Czech name.
    content = (
        "Škoda Auto a.s.,,CZ\nŽluťoučký kůň s.r.o.,,CZ\n".encode()
        + b"Bad\xffName,,CZ\n"
    )
    assert _names(content) == [
        "Škoda Auto a.s.", "Žluťoučký kůň s.r.o.", "Bad\ufffdName",
    ]
    content = _lines(CZECH_NAMES).encode() + b"Bad\xffName;;CZ\n"
    assert _names(content) == CZECH_NAMES + ["Bad\ufffdName"]


def test_utf8_cut_off_inside_a_character_stays_utf8():
    content = _lines(CZECH_NAMES).encode() + "Kůň".encode()[:-1]
    assert _names(content) == CZECH_NAMES + ["Ků\ufffd"]


def test_mostly_utf8_with_one_cp1250_row_stays_utf8():
    stray = "Žluťoučký kůň s.r.o."
    content = (
        _lines(CZECH_NAMES).encode() + _lines([stray]).encode("cp1250")
    )
    assert _names(content) == CZECH_NAMES + [
        stray.encode("cp1250").decode("utf-8", errors="replace"),
    ]


@pytest.mark.parametrize("names", [
    CZECH_NAMES,
    [name.upper() for name in CZECH_NAMES],
    ["Firma Král s.r.o."],
    # Byte runs valid in UTF-8 by chance, and one that is not.
    ["MĚŠŤAN s.r.o."],
])
def test_cp1250_czech_text_still_decodes_as_cp1250(names):
    assert _names(_lines(names).encode("cp1250")) == names


def test_mostly_cp1250_with_one_utf8_row_stays_cp1250():
    # Not a UTF-8 "ň": it holds a byte cp1250 does not define, which
    # sends the whole file to latin-1, as before.
    stray = "Česká pošta, s.p."
    content = (
        _lines(CZECH_NAMES).encode("cp1250") + _lines([stray]).encode()
    )
    assert _names(content) == CZECH_NAMES + [
        stray.encode().decode("cp1250"),
    ]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_whole_utf_files_decode_as_before(encoding):
    content = _lines(CZECH_NAMES, "\t").encode(encoding)
    assert _names(content, "in.txt") == CZECH_NAMES
    assert _names(_lines(CZECH_NAMES).encode(encoding)) == CZECH_NAMES


def test_utf16_big_endian_with_a_bom_decodes_as_before():
    content = "\ufeff".encode("utf-16-be") + _lines(
        CZECH_NAMES, "\t"
    ).encode("utf-16-be")
    assert _names(content, "in.txt") == CZECH_NAMES


# Cells holding only invisible characters (report #28).

def test_invisible_only_names_are_not_entities():
    rows = [["Alfa a.s."]] + [[cell] for cell in INVISIBLE] + [["Beta"]]
    expected = ["Alfa a.s.", "Beta"]
    assert _names(_lines(row[0] for row in rows).encode()) == expected
    assert _names(_xlsx(rows), "in.xlsx") == expected


def test_an_invisible_only_isin_does_not_make_a_row_an_entity():
    rows = [["", "\u200b", "CZ"], ["Alfa a.s.", "\ufeff", "CZ"]]
    text = "".join(";".join(row) + "\n" for row in rows).encode()
    assert _entities(text) == [("Alfa a.s.", None)]
    assert _entities(_xlsx(rows), "in.xlsx") == [("Alfa a.s.", None)]


def test_a_file_of_invisible_cells_only_has_no_entities():
    content = _lines(INVISIBLE).encode()
    with pytest.raises(InputError) as caught:
        parse_upload("in.csv", content)
    assert str(caught.value).startswith("No entities found.")
    assert caught.value.message_cs.startswith(
        "Nebyly nalezeny žádné subjekty."
    )


def test_the_header_after_an_invisible_only_row_is_skipped():
    rows = [["\u200b"], HEADER, GOOD_ROW]
    text = "".join(";".join(row) + "\n" for row in rows).encode()
    assert _entities(text) == [("Alfa a.s.", "CZ0005112300")]
    assert _entities(_xlsx(rows), "in.xlsx") == [
        ("Alfa a.s.", "CZ0005112300"),
    ]


def test_real_text_with_invisible_characters_is_kept_verbatim():
    names = ["Alfa\u200b a.s.", "\u2060Beta s.r.o.", "Gamma\ufeff"]
    assert _names(_lines(["Zeta a.s."] + names).encode()) == (
        ["Zeta a.s."] + names
    )
    assert _names(_xlsx([[name] for name in names]), "in.xlsx") == names
    entity = InputEntity(name=" Alfa\u200b a.s. ", isin="CZ0005112300\u200b")
    assert (entity.name, entity.isin) == (
        "Alfa\u200b a.s.", "CZ0005112300\u200b",
    )


@pytest.mark.parametrize("blank", INVISIBLE + ["", "   "])
def test_input_entity_takes_a_blank_name_or_isin_as_missing(blank):
    assert InputEntity(name=blank, isin="CZ0005112300").name is None
    assert InputEntity(name="Alfa a.s.", isin=blank).isin is None
    with pytest.raises(ValidationError):
        InputEntity(name=blank, isin=blank)


def test_the_single_form_refuses_an_invisible_only_name(client):
    refused = client.post(
        "/api/jobs", data={"mode": "single", "entity_name": "\u200b"}
    )
    assert refused.status_code == 400
    body = refused.get_json()
    assert body["error"] and body["error_cs"]

    created = client.post("/api/jobs", data={
        "mode": "single", "entity_name": "\ufeff", "isin": "CZ0005112300",
    })
    assert created.status_code == 200, created.get_json()
    assert _stored_query(created)[0]["name"] is None


# One shared string in many cells.

def _shared_string_rows(first, text, count):
    """An .xlsx of a row ``first``, then ``count`` rows of ``text``.

    Both are shared strings, 0 and 1, as Excel saves text, and every
    row after the first names string 1: one text in thousands of cells.
    (openpyxl itself saves inline strings.)
    """
    strings = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/'
        f'2006/main"><si><t>{first}</t></si><si><t>{text}</t></si></sst>'
    ).encode()
    override = (
        b'<Override PartName="/xl/sharedStrings.xml" ContentType="'
        b'application/vnd.openxmlformats-officedocument.spreadsheetml.'
        b'sharedStrings+xml" /></Types>'
    )
    rows = "".join(
        f'<row r="{number}"><c r="A{number}" t="s"><v>1</v></c></row>'
        for number in range(2, count + 2)
    )
    sheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>'
        f"{rows}</sheetData></worksheet>"
    ).encode()
    source = zipfile.ZipFile(io.BytesIO(_with_sheet(_xlsx([["x"]]), sheet)))
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", override)
            target.writestr(item.filename, data)
        target.writestr("xl/sharedStrings.xml", strings)
    return result.getvalue()


@pytest.mark.parametrize(("first", "text"), [
    ("Tiny company", "_x0020_" * 4681),
    ("Tiny company", "\u200b" * 32767),
    ("Tiny company;", ";" + "_x0020_" * 4680),
], ids=["escaped spaces", "zero-width spaces", "semicolon line"])
def test_one_long_shared_string_in_many_cells_is_read_quickly(first, text):
    # Blank once decoded or checked, these rows are no entities and
    # never reach the entity cap: each must cost next to nothing.
    content = _shared_string_rows(first, text, 3000)
    started = time.perf_counter()
    assert _names(content, "in.xlsx") == ["Tiny company"]
    assert time.perf_counter() - started < 5


# Files named just ".csv" (report #29).

@pytest.mark.parametrize("filename", [".csv", ".CSV", ".tsv", ".txt"])
def test_a_text_file_named_just_its_extension_is_read(filename):
    content = "Alfa a.s.\tCZ0005112300\n".encode()
    assert _entities(content, filename) == [("Alfa a.s.", "CZ0005112300")]


def test_an_xlsx_named_just_its_extension_is_read(client):
    content = _xlsx([["Alfa a.s.", "CZ0005112300"]])
    assert _entities(content, ".xlsx") == [("Alfa a.s.", "CZ0005112300")]
    created = _create_bulk(client, content, ".xlsx")
    assert created.status_code == 200, created.get_json()
    assert created.get_json()["total"] == 1


@pytest.mark.parametrize("filename", ["csv", "in.pdf", "in.csv.bak"])
def test_a_name_without_a_supported_extension_is_still_refused(filename):
    with pytest.raises(InputError) as caught:
        parse_upload(filename, b"Alfa a.s.\n")
    assert str(caught.value).startswith("Unsupported file type.")
    assert caught.value.message_cs.startswith("Nepodporovaný typ souboru.")

