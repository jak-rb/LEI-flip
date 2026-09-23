
"""Parse an uploaded .xlsx, .csv, .tsv or .txt file into entities.

Columns are read by position, in the order the bulk form documents:
Name, ISIN, Country, City, Street, Postal code. A first row that looks
like a header is skipped. Uses openpyxl for .xlsx and the stdlib csv
module for the text formats, so no extra dependency is needed.
"""

import codecs
import csv
import io
import logging
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.chartsheet import Chartsheet
from pydantic import ValidationError

from .models import InputEntity, InputError

logger = logging.getLogger(__name__)

#: Maximum number of entities accepted in one bulk upload.
MAX_ENTITIES = 100

#: Columns in the documented order: position -> InputEntity field.
_COLUMNS = ("name", "isin", "country", "town", "street", "zip_code")

#: First-cell values (lower-cased) that mark a header row to skip.
_HEADER_CELLS = frozenset({
    "name", "entity", "entity name", "company", "company name",
    "issuer", "issuer name", "firma", "nazev", "název",
})

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
            extension, no usable rows, or more than MAX_ENTITIES rows.
    """
    if not content:
        raise InputError("The file is empty.", "Soubor je prázdný.")

    ext = Path(filename).suffix.lower()
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

    rows = _drop_header(rows)
    entities = _rows_to_entities(rows)
    if not entities:
        raise InputError(
            "No entities found. Each row needs a name (first column) or "
            "an ISIN (second column).",
            "Nebyly nalezeny žádné subjekty. Každý řádek musí obsahovat "
            "název (první sloupec) nebo ISIN (druhý sloupec).",
        )
    if len(entities) > MAX_ENTITIES:
        raise InputError(
            f"Too many entities ({len(entities)}). The maximum is "
            f"{MAX_ENTITIES} per file.",
            f"Příliš mnoho subjektů ({len(entities)}). Maximum je "
            f"{MAX_ENTITIES} na soubor.",
        )
    return entities


def _read_xlsx(content: bytes) -> list[list[str]]:
    """Read the active worksheet into rows of trimmed string cells."""
    # The whole read sits in the try: in read-only mode a damaged sheet
    # only fails once its rows are read.
    try:
        workbook = load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
        sheet = workbook.active
        # Excel saves the sheet on screen as the active one; a chart
        # sheet has no cells, so fall back to the first worksheet.
        if isinstance(sheet, Chartsheet):
            sheet = workbook.worksheets[0]
        rows = []
        for raw in sheet.iter_rows(values_only=True):
            rows.append(["" if cell is None else str(cell) for cell in raw])
        workbook.close()
        if _holds_semicolon_lines(rows):
            return _split_semicolon_lines(rows)
    except Exception as error:
        logger.warning("Failed to parse uploaded .xlsx file: %s", error)
        raise InputError(
            "Could not read the .xlsx file; is it a valid Excel file?",
            "Soubor .xlsx se nepodařilo přečíst. Je to platný soubor "
            "Excelu?",
        ) from error
    return [[cell.strip() for cell in row] for row in rows]


def _holds_semicolon_lines(rows: list[list[str]]) -> bool:
    """Whether every non-empty row keeps a semicolon line in column A.

    That is what Excel shows for a semicolon CSV opened with the comma
    as the delimiter: each whole line lands in column A, split again
    into the next columns wherever a value holds a comma.
    """
    filled = [row for row in rows if any(cell.strip() for cell in row)]
    return bool(filled) and all(";" in row[0] for row in filled)


def _split_semicolon_lines(rows: list[list[str]]) -> list[list[str]]:
    """Rebuild each row's original line and split it at semicolons."""
    result = []
    for row in rows:
        cells = list(row)
        while cells and not cells[-1].strip():
            cells.pop()
        if not cells:
            continue
        # Excel split the line at its commas, so joining the cells with
        # commas restores it (a cell keeps its leading space). Each line
        # is parsed on its own: an unbalanced quote must not swallow
        # the rows after it.
        line = ",".join(cells)
        fields = next(csv.reader([line], delimiter=";"), [])
        result.append([field.strip() for field in fields])
    return result


def _decode(content: bytes) -> str:
    """Decode CSV bytes, trying common encodings (incl. Czech cp1250)."""
    encodings = ("utf-8-sig", "utf-8", "cp1250", "latin-1")
    # UTF-16 (what Windows PowerShell's `>` writes, for one) starts
    # with a byte-order mark and would decode as garbage below.
    if content.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16",) + encodings
    for encoding in encodings:
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return content.decode("latin-1")  # latin-1 never fails


def _read_csv(content: bytes, delimiter: str | None) -> list[list[str]]:
    """Read delimited text into rows; a None delimiter is detected.

    Detection picks whichever of comma, semicolon and tab is most common
    in the first lines; a tie keeps the comma, then the semicolon.
    """
    text = _decode(content)
    # Text never holds a NUL: this is binary content, such as an Excel
    # workbook renamed to .csv, which would only parse into garbage.
    if "\x00" in text:
        logger.warning("Uploaded text file is binary (contains NUL)")
        raise InputError(*_UNREADABLE_TEXT)
    if delimiter is None:
        sample = "\n".join(text.splitlines()[:5])
        delimiter = max((",", ";", "\t"), key=sample.count)
    # newline="" leaves line endings to the csv module, which accepts
    # \n, \r\n and the lone \r of old Mac files alike.
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        return [[cell.strip() for cell in row] for row in reader]
    except csv.Error as error:  # e.g. a cell over the 128 KB limit
        logger.warning("Failed to parse uploaded text file: %s", error)
        raise InputError(*_UNREADABLE_TEXT) from error


def _drop_header(rows: list[list[str]]) -> list[list[str]]:
    """Drop the first row when it looks like a header.

    Besides the usual labels, a second cell naming the ISIN column in
    database style (such as ISIN_IDENT) marks a header.
    """
    if not rows or not rows[0]:
        return rows
    first = rows[0][0].strip().lower()
    second = rows[0][1].strip().lower() if len(rows[0]) > 1 else ""
    if first in _HEADER_CELLS or "isin" in second:
        return rows[1:]
    return rows


def _rows_to_entities(rows: list[list[str]]) -> list[InputEntity]:
    """Map positional columns to entities, skipping empty/invalid rows."""
    entities: list[InputEntity] = []
    for row in rows:
        values = {}
        for index, field in enumerate(_COLUMNS):
            cell = row[index].strip() if index < len(row) else ""
            values[field] = cell or None
        # Keep a row that has a name or an ISIN; skip only when both are
        # missing. InputEntity enforces the same name-or-ISIN rule.
        if not values["name"] and not values["isin"]:
            continue
        try:
            entities.append(InputEntity(**values))
        except ValidationError:
            continue  # skip a row that fails validation (e.g. oversized)
    return entities

