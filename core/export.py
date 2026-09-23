
"""Build CSV and Excel exports from a stored search.

One row per searched entity, carrying the lookup's "answer" columns
(used by the /download routes). Characters XML forbids are dropped, and
cells that a spreadsheet could read as a formula are neutralised
(CWE-1236).
"""

import csv
import io
import re
from typing import Optional

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

#: Export column headers, in order.
COLUMNS = [
    "Name",
    "ISIN",
    "Country",
    "Town",
    "Street",
    "ZIP code",
    "LEI",
    "LEI_status",
    "Match_type",
    "Confidence",
    "Name_score",
    "City_score",
    "Street_score",
    "ZIP_score",
    "Warnings",
    "GLEIF_legal_name",
    "GLEIF_legal_address",
    "GLEIF_hq_address",
    "Notes",
]

#: Leading characters that make a spreadsheet treat a cell as a formula.
_FORMULA_TRIGGERS = ("=", "+", "-", "@")

#: Characters XML 1.0 forbids: openpyxl refuses the control characters
#: (a 500 on the Excel download) and writes U+FFFE/U+FFFF into a
#: workbook that no longer opens. User input and GLEIF text can hold
#: them.
_XML_ILLEGAL_RE = re.compile(
    ILLEGAL_CHARACTERS_RE.pattern + r"|[\ufffe\uffff]"
)


def _sanitize(value: object) -> str:
    """Stringify a value, XML-safe and with formulas neutralised."""
    text = "" if value is None else str(value)
    # Dropped first, so no formula can hide behind a control character.
    text = _XML_ILLEGAL_RE.sub("", text)
    if text and text[0] in _FORMULA_TRIGGERS:
        return "'" + text
    return text


def _find_candidate(row: dict, lei: Optional[str]) -> Optional[dict]:
    """The stored candidate with this LEI, or None if absent."""
    for candidate in row.get("closest", []):
        if candidate.get("lei") == lei:
            return candidate
    return None


def _answer_fields(row: dict) -> dict:
    """The result columns for a row, honouring any manual decision.

    A confirmed decision draws the answer (with the chosen candidate's
    scores and addresses); a "none" decision records an explicit reviewed
    no-match; with no decision the algorithmic match is used unchanged.
    Every value is already stored - nothing is recomputed here.
    """
    decision = row.get("decision") or {}

    if decision.get("status") == "confirmed":
        candidate = _find_candidate(row, decision.get("lei"))
        if candidate is not None:
            return {
                "lei": candidate.get("lei"),
                "lei_status": candidate.get("status"),
                "match_type": "MANUAL_MATCH",
                "confidence": "",
                "name_score": candidate.get("name_score", ""),
                "city_score": candidate.get("city_score", ""),
                "street_score": candidate.get("street_score", ""),
                "zip_score": candidate.get("zip_score", ""),
                "warnings": "",
                "gleif_legal_name": candidate.get("legal_name"),
                "gleif_legal_address": candidate.get("legal_address"),
                "gleif_hq_address": candidate.get("hq_address"),
                "notes": "Manually confirmed",
            }

    if decision.get("status") == "none":
        # The user reviewed the candidates and rejected them all. Leave
        # the identity/score columns blank, but label the outcome so the
        # row does not read as blank (unprocessed) in the export.
        fields = {
            key: ""
            for key in (
                "lei", "lei_status", "confidence", "name_score",
                "city_score", "street_score", "zip_score", "warnings",
                "gleif_legal_name", "gleif_legal_address",
                "gleif_hq_address",
            )
        }
        fields["match_type"] = "MANUAL_NO_MATCH"
        fields["notes"] = "Reviewed - none of the candidates matched"
        return fields

    match = row.get("match", {})
    details = match.get("match_details") or {}
    confidence = match.get("confidence")
    return {
        "lei": match.get("lei"),
        "lei_status": match.get("lei_status"),
        "match_type": match.get("match_type"),
        "confidence": (
            round(confidence, 1)
            if isinstance(confidence, (int, float)) else ""
        ),
        "name_score": details.get("name_score", ""),
        "city_score": details.get("city_score", ""),
        "street_score": details.get("street_score", ""),
        "zip_score": details.get("zip_score", ""),
        "warnings": "; ".join(match.get("warnings") or []),
        "gleif_legal_name": match.get("gleif_legal_name"),
        "gleif_legal_address": match.get("gleif_legal_address"),
        "gleif_hq_address": match.get("gleif_hq_address"),
        "notes": match.get("notes"),
    }


def _row_cells(row: dict) -> list[str]:
    """Turn one stored result row into ordered, sanitised cell values."""
    source = row.get("input", {})
    answer = _answer_fields(row)
    values = [
        source.get("name"),
        source.get("isin"),
        source.get("country"),
        source.get("city"),
        source.get("street"),
        source.get("postal_code"),
        answer["lei"],
        answer["lei_status"],
        answer["match_type"],
        answer["confidence"],
        answer["name_score"],
        answer["city_score"],
        answer["street_score"],
        answer["zip_score"],
        answer["warnings"],
        answer["gleif_legal_name"],
        answer["gleif_legal_address"],
        answer["gleif_hq_address"],
        answer["notes"],
    ]
    return [_sanitize(v) for v in values]


def build_csv(search: dict) -> str:
    """Render a stored search as CSV text (header + one row per entity)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(COLUMNS)
    for row in search.get("results", []):
        writer.writerow(_row_cells(row))
    return buffer.getvalue()


def build_xlsx(search: dict) -> io.BytesIO:
    """Render a stored search as an .xlsx workbook in a BytesIO buffer."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "LEI results"
    sheet.append(COLUMNS)
    for row in search.get("results", []):
        sheet.append(_row_cells(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer

