
"""Parse an uploaded .xlsx, .csv, .tsv or .txt file into entities.

Columns are read by position, in the order the bulk form documents:
Name, ISIN, Country, City, Street, Postal code. The first non-blank
row is skipped when it holds column labels (a header), and so is a
title row above a header. Uses openpyxl for .xlsx and the stdlib csv
module for the text formats, so no extra dependency is needed.

A row with a name or an ISIN is never dropped or cut short: one with a
value over its length limit refuses the whole file, with a message
naming the row. Reading stops as soon as a file is known to hold too
many entities, and an .xlsx is read within a budget of bytes and XML
nodes, so a small crafted file cannot tie the server up.
"""

import codecs
import csv
import io
import itertools
import logging
import re
import xml.parsers.expat
import zipfile
from collections.abc import Iterable, Iterator

from openpyxl.chartsheet import Chartsheet
from openpyxl.reader.excel import ExcelReader
from openpyxl.utils.escape import unescape
from pydantic import ValidationError
from unidecode import unidecode

from .address import country_to_iso
from .isin import is_valid_isin, normalize_isin
from .models import InputEntity, InputError, is_blank

logger = logging.getLogger(__name__)

#: Maximum number of entities accepted in one bulk upload.
MAX_ENTITIES = 100

#: Columns in the documented order: position -> InputEntity field.
_COLUMNS = ("name", "isin", "country", "town", "street", "zip_code")

#: A row as read from a file: its number as the user sees it there
#: (1-based, counting the header and blank rows) and its trimmed cells.
_Row = tuple[int, list[str]]

#: How a message names each column: field -> (English, Czech).
_FIELD_NAMES = {
    "name": ("the name", "název"),
    "isin": ("the ISIN", "ISIN"),
    "country": ("the country", "země"),
    "town": ("the city", "město"),
    "street": ("the street", "ulice"),
    "zip_code": ("the postal code", "PSČ"),
}

#: At most this many rows are named in one message.
_LISTED_ROWS = 5

#: Words the column labels of a header are made of, lower-case and
#: without diacritics: "Název subjektu", "Legal name",
#: "PARTY_FULL_NAME", "ISIN kód", "ISIN_IDENT", "Země" and "PSČ" are
#: all labels. A cell with any other word, such as a company name, is
#: data.
_HEADER_WORDS = frozenset({
    # Name
    "name", "entity", "company", "legal", "full", "party", "issuer",
    "firm", "firma", "firmy", "nazev", "subjekt", "subjektu",
    "obchodni", "jmeno", "emitent", "emitenta", "spolecnost",
    "spolecnosti", "counterparty", "protistrana", "client", "klient",
    "customer",
    # ISIN
    "isin", "code", "kod", "ident", "identifier",
    # Address
    "country", "zeme", "stat", "city", "town", "mesto", "obec",
    "street", "ulice", "address", "adresa", "addr", "zip", "postal",
    "postcode", "psc",
})

#: A note in parentheses, such as "(optional)" in "ISIN (optional)":
#: it says how to fill a column, not what the column is.
_LABEL_NOTE = re.compile(r"\([^)]*\)")

#: An ISIN anywhere in a cell, once its spaces are removed.
_ISIN_SHAPE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

#: Columns read from an .xlsx row whose column A holds a semicolon
#: line: Excel split the line again at every comma, so rebuilding it
#: takes the cells after the six documented columns too.
_LINE_COLUMNS = 50

#: Most an .xlsx may make openpyxl read: bytes unpacked, and XML nodes
#: (elements and attributes) parsed, counting a part again each time it
#: is read. A real 100-entity workbook takes under 10,000 nodes, and
#: the budget leaves room for some 250,000 shared strings from other
#: sheets (about 5 s to read), while
#: a crafted file of a few kilobytes can unpack to gigabytes, hold
#: millions of tiny elements (openpyxl spends up to some 10
#: microseconds on each node), or name one part from many places so
#: that it is parsed again and again.
_MAX_READ_BYTES = 50 * 1024 * 1024
_MAX_READ_NODES = 500_000

#: The last row of an Excel worksheet. A row numbered past it is not
#: from Excel, and would make openpyxl yield every empty row before it.
_LAST_ROW = 1_048_576

#: A number format that only pads a number with zeros, its digits
#: maybe grouped by spaces or hyphens, which Excel saves escaped or
#: quoted: "00000", "000\ 00" (the Czech and Slovak postal code) or
#: "00000\-0000" (a US ZIP+4). They show 1001 as "01001", "010 01"
#: and "00000-1001".
_ZERO_PADDED_FORMAT = re.compile(r'0(?:0|\\?[ -]|"[ -]+")*')

#: Text formats read with the csv module: extension -> delimiter, where
#: None means detect it. Excel's "Text (Tab delimited)" and "Unicode
#: Text" exports, and cells copied from Excel into a text editor, are
#: tab-separated .txt; a .tsv is tab-separated by definition.
_TEXT_DELIMITERS = {".csv": None, ".tsv": "\t", ".txt": "\t"}

#: Shown (English, Czech) for a text file that cannot be read as rows.
_UNREADABLE_TEXT = (
    "Could not read the file. Save it from Excel as CSV UTF-8, or "
    "upload the .xlsx instead.",
    "Soubor se nepodařilo přečíst. Uložte ho z Excelu jako CSV UTF-8 "
    "nebo nahrajte soubor .xlsx.",
)


def parse_upload(filename: str, content: bytes) -> list[InputEntity]:
    """Parse uploaded bytes into entities.

    Args:
        filename: The original file name (its extension picks the parser).
        content: The raw file bytes.

    Returns:
        The parsed entities (rows with a name or an ISIN).

    Raises:
        InputError: For an empty/unreadable file, an unsupported
            extension, no usable rows, more than MAX_ENTITIES rows, or
            rows with a value over its length limit.
    """
    if not content:
        raise InputError("The file is empty.", "Soubor je prázdný.")

    # The part from the last dot, as app.py checks it: to pathlib, a
    # file named just ".csv" has no extension.
    _, dot, ext = filename.lower().rpartition(".")
    ext = dot + ext
    if ext == ".xlsx":
        rows = _read_xlsx(content)
    elif ext in _TEXT_DELIMITERS:
        rows = _read_csv(content, _TEXT_DELIMITERS[ext])
    else:
        raise InputError(
            "Unsupported file type. Please upload a .xlsx, .csv, .tsv or "
            ".txt file.",
            "Nepodporovaný typ souboru. Nahrajte prosím soubor .xlsx, "
            ".csv, .tsv nebo .txt.",
        )

    entities = _rows_to_entities(rows)
    if not entities:
        raise InputError(
            "No entities found. Each row needs a name (first column) or "
            "an ISIN (second column).",
            "Nebyly nalezeny žádné subjekty. Každý řádek musí obsahovat "
            "název (první sloupec) nebo ISIN (druhý sloupec).",
        )
    return entities


def _read_xlsx(content: bytes) -> list[_Row]:
    """Read the active worksheet's entity rows (see _entity_rows)."""
    # The whole read sits in the try: in read-only mode a damaged sheet
    # only fails once its rows are read.
    try:
        workbook = _load_workbook(content)
        sheet = workbook.active
        # Excel saves the sheet on screen as the active one; a chart
        # sheet has no cells, so fall back to the first worksheet.
        if isinstance(sheet, Chartsheet):
            sheet = workbook.worksheets[0]
        # Read-only mode stops at the size the sheet states, which some
        # writers leave stale: the rows past it would be lost.
        sheet.reset_dimensions()
        if _holds_semicolon_lines(sheet):
            rows = _entity_rows(_split_semicolon_lines(sheet))
        else:
            decoded: dict[str, str] = {}
            rows = _entity_rows(
                (number, [
                    _decode_escapes(cell, decoded).strip() for cell in cells
                ])
                for number, cells in _filled_rows(
                    sheet, len(_COLUMNS), _COLUMNS.index("zip_code")
                )
            )
        workbook.close()
    except _TooMuchData:
        logger.warning("Uploaded .xlsx needs more than the read budget")
        raise InputError(
            "The .xlsx file holds too much data to read. Keep only the "
            f"sheet with the entities (at most {MAX_ENTITIES}), or save "
            "it as CSV UTF-8.",
            "Soubor .xlsx obsahuje příliš mnoho dat. Ponechte v něm jen "
            f"list se subjekty (nejvýše {MAX_ENTITIES}) nebo ho uložte "
            "jako CSV UTF-8.",
        ) from None
    except InputError:
        raise
    except Exception as error:
        logger.warning("Failed to parse uploaded .xlsx file: %s", error)
        raise InputError(
            "Could not read the .xlsx file; is it a valid Excel file?",
            "Soubor .xlsx se nepodařilo přečíst. Je to platný soubor "
            "Excelu?",
        ) from error
    return rows


class _TooMuchData(Exception):
    """An .xlsx made openpyxl read more than the read budget allows.

    Not a ValueError, which openpyxl catches and wraps as its own.
    """


class _GuardedArchive(zipfile.ZipFile):
    """A zip archive that stops its reader once it has read too much.

    openpyxl may read one part many times over (every sheet entry and
    chart anchor can name the same one), so every read is charged: the
    bytes handed out, and the XML nodes in them, counted by an expat
    parser of its own (openpyxl parses with expat too). A part with a
    DTD is refused: Office Open XML parts never have one, and its
    entities could blow a small part up to gigabytes of text.
    """

    def __init__(self, file) -> None:
        super().__init__(file)
        self._bytes_left = _MAX_READ_BYTES
        self._nodes_left = _MAX_READ_NODES

    def open(self, name, mode="r", pwd=None, **kwargs):
        """Open a part whose reads are charged to the budget."""
        info = self.getinfo(name) if isinstance(name, str) else name
        # Checked up front, as reading a whole part holds it in memory.
        if info.file_size > self._bytes_left:
            raise _TooMuchData
        part = super().open(info, mode, pwd, **kwargs)
        counter = xml.parsers.expat.ParserCreate()
        counter.StartElementHandler = self._charge_nodes
        counter.StartDoctypeDeclHandler = _refuse_doctype
        read = part.read

        def charged_read(size=-1):
            nonlocal counter
            data = read(size)
            self._bytes_left -= len(data)
            if self._bytes_left < 0:
                raise _TooMuchData
            if counter is not None:
                try:
                    counter.Parse(data, not data)
                except xml.parsers.expat.ExpatError:
                    # Not XML: openpyxl fails on it too or reads it raw.
                    counter = None
            return data

        part.read = charged_read
        return part

    def _charge_nodes(self, name, attributes) -> None:
        """Charge one element and its attributes to the budget."""
        self._nodes_left -= 1 + len(attributes)
        if self._nodes_left < 0:
            raise _TooMuchData


def _refuse_doctype(*declaration) -> None:
    """Refuse a part that declares a DTD (see _GuardedArchive)."""
    raise ValueError("an .xlsx part declares a DTD")


def _load_workbook(content: bytes):
    """Open an .xlsx read-only, its reads guarded by _GuardedArchive.

    Does what openpyxl's load_workbook does, with the archive swapped
    before anything is read from it.
    """
    reader = ExcelReader(
        io.BytesIO(content), read_only=True, data_only=True,
        keep_links=False,
    )
    reader.archive.close()
    reader.archive = _GuardedArchive(io.BytesIO(content))
    reader.read()
    return reader.wb


def _filled_rows(
    sheet, width: int, zip_index: int | None = None
) -> Iterator[tuple[int, list[str]]]:
    """Yield (worksheet row number, cells as text) of non-blank rows.

    Reads the first ``width`` columns of every row, text as it is
    saved: its escapes are left to the caller (see _decode_escapes).
    Given a ``zip_index``, the cell there is a postal code, and a number
    in it keeps the zeros its format shows (see _postal_code_text).
    That takes openpyxl's cell objects, which cost more than bare
    values, so only then are they read.
    """
    with_cells = zip_index is not None
    rows = sheet.iter_rows(max_col=width, values_only=not with_cells)
    for number, row in enumerate(rows, start=1):
        if number > _LAST_ROW:
            raise ValueError(f"row {number} is past Excel's last row")
        values = [cell.value for cell in row] if with_cells else row
        # Checked on the raw values first, as it is cheap: one stray
        # far-away cell makes openpyxl yield a million empty rows.
        if values.count(None) == len(values):
            continue
        cells = ["" if cell is None else str(cell) for cell in values]
        if with_cells:
            cells[zip_index] = _postal_code_text(
                row[zip_index], cells[zip_index]
            )
        if any(cell.strip() for cell in cells):
            yield number, cells


def _decode_escapes(text: str, decoded: dict[str, str]) -> str:
    """Text read from an .xlsx, with Excel's _xHHHH_ escapes decoded.

    Excel saves a character XML cannot hold, such as the carriage
    return of a line break typed in a cell, as "_x000D_" (and a real
    "_x" as "_x005F_x"); openpyxl leaves them as they are. An escaped
    UTF-16 pair of surrogates becomes its one character, and a lone
    surrogate, which is no character, becomes U+FFFD. One shared
    string can fill any number of cells, so each text is decoded once
    and kept in ``decoded``.
    """
    if "_x" not in text:
        return text
    if text not in decoded:
        decoded[text] = unescape(text).encode(
            "utf-16", "surrogatepass"
        ).decode("utf-16", "replace")
    return decoded[text]


def _postal_code_text(cell, text: str) -> str:
    """A postal code cell's text, with the zeros its format shows.

    A postal code typed in Excel as a number loses its leading zeros,
    which a format of zeros (see _ZERO_PADDED_FORMAT) shows again:
    1001 formatted "000 00" is shown as "010 01". Any other cell, and
    a number with more digits than the format has zeros, keeps
    ``text``, its value as text.
    """
    value = cell.value
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or value < 0:  # bool is an int too
        return text
    try:
        number_format = cell.number_format
    except IndexError:  # a style or format the workbook lacks
        return text
    if not _ZERO_PADDED_FORMAT.fullmatch(number_format):
        return text
    pattern = number_format.replace("\\", "").replace('"', "")
    zeros = pattern.count("0")
    if len(str(value)) > zeros:
        return text
    digits = iter(str(value).zfill(zeros))
    return "".join(next(digits) if char == "0" else char for char in pattern)


def _holds_semicolon_lines(sheet) -> bool:
    """Whether the non-blank rows are semicolon lines split by Excel.

    That is what Excel shows for a semicolon CSV opened with the comma
    as the delimiter: each whole line lands in column A, split again
    into the next columns wherever a value holds a comma, even one in
    the name ("ČEZ, a. s."). So each row's line is rebuilt first (see
    _line_fields). Every line must hold a semicolon, and on more than
    half of them the second field must be empty, an ISIN or a label,
    as the documented layout has it: a plain sheet whose names hold a
    semicolon ("Apple; Inc" beside its ISIN) is read as columns. At
    most a header and MAX_ENTITIES + 1 rows are checked: read as
    columns, that many rows would be too many entities anyway, so the
    lines are the one reading left to try.
    """
    checked = laid_out = 0
    decoded: dict[str, str] = {}
    for _, cells in _filled_rows(sheet, _LINE_COLUMNS):
        # Joining the cells with commas adds no semicolon.
        if not any(";" in cell for cell in cells):
            return False
        fields = _line_fields(cells)
        # Decoded, and empty when invisible, as the entity rows see it.
        second = (
            _decode_escapes(fields[1], decoded) if len(fields) > 1 else ""
        )
        if (
            is_blank(second) or _is_label(second) or _is_isin_label(second)
            or _ISIN_SHAPE.fullmatch(normalize_isin(second))
        ):
            laid_out += 1
        checked += 1
        if checked > MAX_ENTITIES + 1:
            break
    return laid_out * 2 > checked


def _split_semicolon_lines(sheet) -> Iterator[_Row]:
    """Rebuild each row's original line and split it at semicolons."""
    decoded: dict[str, str] = {}
    for number, cells in _filled_rows(sheet, _LINE_COLUMNS):
        # Escapes are decoded only in the fields: a line break decoded
        # before would stand outside any quotes, which the csv module
        # refuses.
        yield number, [
            _decode_escapes(field, decoded).strip()
            for field in _line_fields(cells)
        ]


def _line_fields(cells: list[str]) -> list[str]:
    """A row's original line, rebuilt and split at its semicolons.

    Excel split the line at its commas, so joining the cells with
    commas restores it (a cell keeps its leading space). Each line is
    parsed on its own: an unbalanced quote must not swallow the rows
    after it. Escapes are left in the fields (see _decode_escapes).
    """
    while not cells[-1].strip():
        cells.pop()
    line = ",".join(cells)
    return next(csv.reader([line], delimiter=";"), [])


def _decode(content: bytes) -> str:
    """Decode CSV bytes, trying common encodings (incl. Czech cp1250).

    UTF-8 with a few invalid bytes, such as a row pasted in from a
    cp1250 file or a character cut off at the end, stays UTF-8 with
    U+FFFD for those bytes (see _is_mostly_utf8): read as cp1250, every
    Czech letter in it would be garbled.
    """
    # UTF-16 (what Windows PowerShell's `>` writes, for one) starts
    # with a byte-order mark and would decode as garbage below.
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    text = content.decode("utf-8-sig", errors="replace")
    if _is_mostly_utf8(text):
        return text
    # The five bytes cp1250 leaves undefined become U+FFFD: read as
    # latin-1 instead, every Czech letter in the file would be garbled.
    return content.decode("cp1250", errors="replace")


def _is_mostly_utf8(text: str) -> bool:
    """Whether text decoded as UTF-8 with replacements is UTF-8 still.

    It is when its valid characters outside ASCII outnumber the U+FFFD
    replacements more than three to one. Czech text in cp1250 gives
    mostly replacements: its accented letters are bytes that UTF-8
    takes for lead or continuation bytes, and they seldom line up into
    a valid sequence. A UTF-8 file with a stray byte or two has few.
    """
    replaced = text.count("\ufffd")
    non_ascii = len(text) - len(text.encode("ascii", errors="ignore"))
    return replaced * 3 < non_ascii - replaced


def _read_csv(content: bytes, delimiter: str | None) -> list[_Row]:
    """Read delimited text's entity rows; a None delimiter is detected.

    Detection picks whichever of comma, semicolon and tab is most common
    in the first non-blank lines; a tie keeps the comma, then the
    semicolon.
    """
    text = _decode(content)
    # Text never holds a NUL: this is binary content, such as an Excel
    # workbook renamed to .csv, which would only parse into garbage.
    if "\x00" in text:
        logger.warning("Uploaded text file is binary (contains NUL)")
        raise InputError(*_UNREADABLE_TEXT)
    if delimiter is None:
        lines = io.StringIO(text, newline="")
        sample = "".join(
            itertools.islice((line for line in lines if line.strip()), 5)
        )
        delimiter = max((",", ";", "\t"), key=sample.count)
    # newline="" leaves line endings to the csv module, which accepts
    # \n, \r\n and the lone \r of old Mac files alike.
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows = (
        (number, [cell.strip() for cell in row[:len(_COLUMNS)]])
        for number, row in enumerate(reader, start=1)
        if row
    )
    try:
        return _entity_rows(rows)
    except csv.Error as error:  # e.g. a cell over the 128 KB limit
        logger.warning("Failed to parse uploaded text file: %s", error)
        raise InputError(*_UNREADABLE_TEXT) from error


def _entity_rows(rows: Iterable[_Row]) -> list[_Row]:
    """Keep the rows that hold a name or an ISIN, cut to the columns.

    Blank rows are skipped, and so are a header and a title row above
    it (see _after_header). Reading stops at the first row over
    MAX_ENTITIES, so a huge file is refused without being read to its
    end.

    Args:
        rows: The file's rows, in order.

    Returns:
        The rows to build entities from.

    Raises:
        InputError: When more than MAX_ENTITIES rows hold an entity.
    """
    kept = []
    for number, cells in _after_header(_non_blank_rows(rows)):
        # Counted before validation: an invalid row counts toward the
        # cap too, and "too many" is then the error to show.
        if not any(cells[:2]):
            continue
        kept.append((number, cells))
        if len(kept) > MAX_ENTITIES:
            raise InputError(
                f"Too many entities (more than {MAX_ENTITIES}). The "
                f"maximum is {MAX_ENTITIES} per file.",
                f"Příliš mnoho subjektů (více než {MAX_ENTITIES}). "
                f"Maximum je {MAX_ENTITIES} na soubor.",
            )
    return kept


def _non_blank_rows(rows: Iterable[_Row]) -> Iterator[_Row]:
    """The rows with a filled cell, cut to the columns."""
    # Whether each name or ISIN text is blank: one .xlsx shared string
    # can fill any number of cells, and such a row is not counted.
    blank: dict[str, bool] = {}
    for number, cells in rows:
        cells = cells[:len(_COLUMNS)]
        # A name or ISIN with no visible character, such as a lone
        # zero-width space, is as empty as it looks.
        for index, cell in enumerate(cells[:2]):
            if cell not in blank:
                blank[cell] = is_blank(cell)
            if blank[cell]:
                cells[index] = ""
        if any(cells):
            yield number, cells


def _after_header(rows: Iterator[_Row]) -> Iterator[_Row]:
    """The non-blank rows, less a first row that is a header.

    A first row of one filled cell may be a title ("Seznam subjektů")
    above the header: it is skipped with the next row when that is a
    header of two labels or more. Otherwise the first row is data, so
    a list of names keeps its first name whatever the second is.
    """
    first = next(rows, None)
    if first is None or _is_header(first[1]):
        yield from rows
        return
    second = None
    if sum(1 for cell in first[1] if cell) == 1:
        second = next(rows, None)
        if (
            second is not None and _is_header(second[1])
            and sum(_labels(second[1])) >= 2
        ):
            yield from rows
            return
    yield first
    if second is not None:
        yield second
    yield from rows


def _is_header(cells: list[str]) -> bool:
    """Whether a row holds column labels rather than an entity.

    A company may be named like labels ("Party City", "Client
    Company"), so a row whose ISIN is valid, or with a cell that looks
    like data (see _looks_like_data), is never a header. Otherwise the
    name and the ISIN cells must each be empty or a label, one of them
    a label, or most filled cells must be labels: a city or a street
    may be named like a label (Clarks is in Street, Somerset), so the
    address cells only count together.
    """
    isin = cells[1] if len(cells) > 1 else ""
    if is_valid_isin(normalize_isin(isin)):
        return False
    labels = _labels(cells)
    if any(
        cell and not label and _looks_like_data(cell)
        for cell, label in zip(cells, labels)
    ):
        return False
    if any(labels[:2]) and all(
        label or not cell for cell, label in zip(cells[:2], labels[:2])
    ):
        return True
    filled = [label for cell, label in zip(cells, labels) if cell]
    return sum(filled) * 2 > len(filled)


def _labels(cells: list[str]) -> list[bool]:
    """Which cells are labels; the ISIN cell's may be worded freely."""
    labels = [_is_label(cell) for cell in cells]
    if len(cells) > 1 and _is_isin_label(cells[1]):
        labels[1] = True
    return labels


def _looks_like_data(cell: str) -> bool:
    """Whether a cell holds a digit or a country (name or code).

    A postal code, a street number and an ISIN hold digits, and
    country_to_iso knows a country's name and passes a two-letter code
    through. Label cells ("Země", "Country") are not checked.
    """
    return (
        any(char.isdigit() for char in cell)
        or country_to_iso(cell) is not None
    )


def _is_label(cell: str) -> bool:
    """Whether a cell is a column label, such as "ISIN kód".

    A note in parentheses is left out: "Name (required)" is a label.
    """
    words = _words(_LABEL_NOTE.sub(" ", cell))
    return bool(words) and all(word in _HEADER_WORDS for word in words)


def _is_isin_label(cell: str) -> bool:
    """Whether an ISIN cell is a label, such as "ISIN (optional)"."""
    words = _words(cell)
    return (
        bool(words) and words[0].startswith("isin")
        and not _ISIN_SHAPE.search(normalize_isin(cell))
    )


def _words(cell: str) -> list[str]:
    """A cell's words, lower-case and without diacritics."""
    return re.findall(r"[a-z0-9]+", unidecode(cell).lower())


def _rows_to_entities(rows: list[_Row]) -> list[InputEntity]:
    """Map positional columns to one entity per row.

    Raises:
        InputError: Naming the rows with a value over its length limit
            (precision first: a value is never cut short to fit).
    """
    entities: list[InputEntity] = []
    too_long: list[tuple[int, list[tuple[str, int]]]] = []
    for number, row in rows:
        values = {}
        for index, field in enumerate(_COLUMNS):
            cell = row[index].strip() if index < len(row) else ""
            values[field] = cell or None
        try:
            entities.append(InputEntity(**values))
        except ValidationError as error:
            # Each row has a name or an ISIN and only text values, so a
            # length limit is the one check it can fail.
            limits = {
                issue["loc"][0]: issue["ctx"]["max_length"]
                for issue in error.errors()
            }
            too_long.append((number, [
                (field, limits[field]) for field in _COLUMNS
                if field in limits
            ]))
    if too_long:
        raise _too_long_error(too_long)
    return entities


def _too_long_error(
    rows: list[tuple[int, list[tuple[str, int]]]]
) -> InputError:
    """The error naming the rows that have a value over its limit."""
    english, czech = [], []
    for number, limits in rows[:_LISTED_ROWS]:
        english.append(f"Row {number}: " + "; ".join(
            f"{_FIELD_NAMES[field][0]} is longer than {limit} characters"
            for field, limit in limits
        ) + ".")
        czech.append(f"Řádek {number}: " + "; ".join(
            f"{_FIELD_NAMES[field][1]} je delší než {limit} znaků"
            for field, limit in limits
        ) + ".")
    more = len(rows) - _LISTED_ROWS
    if more > 0:
        english.append(f"And {more} more row{'s' if more > 1 else ''}.")
        if more == 1:
            more_cs = "další řádek"
        elif more < 5:
            more_cs = "další řádky"
        else:
            more_cs = "dalších řádků"
        czech.append(f"A ještě {more} {more_cs}.")
    return InputError(" ".join(english), " ".join(czech))

