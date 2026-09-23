
"""End-to-end tests of the Flask routes with the GLEIF lookup faked.

The fake classifies by the searched name: "match" -> asserted LEI,
"review" -> a near-miss with one candidate, "down" -> GLEIF outage,
anything else -> no match.
"""

import io
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference

import app as app_module
from core import storage
from core.gleif import GleifApiError
from core.models import CandidateSummary, LookupResult, MatchType

MATCH_LEI = "M" * 20
REVIEW_LEI = "R" * 20


class _FakeGleifClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_lookup(entity, client):
    name = (entity.name or "").lower()
    if "down" in name:
        raise GleifApiError("GLEIF unreachable")
    if "match" in name:
        result = LookupResult(
            lei=MATCH_LEI, lei_status="ISSUED",
            match_type=MatchType.FULL_MATCH, confidence=90,
            gleif_legal_name="Match AG", gleif_legal_country="DE",
            gleif_legal_city="Berlin", gleif_legal_street="Weg 1",
        )
        return result, []
    if "review" in name:
        candidate = CandidateSummary(
            legal_name="Review Ltd", lei=REVIEW_LEI, status="ISSUED",
            country="GB", city="London", overall=61.0,
        )
        return LookupResult(notes="Strong name match, unverified"), [candidate]
    return LookupResult(notes="No LEI found in the GLEIF database."), []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "lookup_entity", _fake_lookup)
    monkeypatch.setattr(app_module, "GleifClient", _FakeGleifClient)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _create_single(client, **fields):
    return client.post("/api/jobs", data={"mode": "single", **fields})


def _create_bulk(client, content, filename="in.csv"):
    return client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def _run(client, job_id):
    return client.post(f"/api/jobs/{job_id}/run")


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "UP"}


def test_index_and_static(client):
    assert client.get("/").status_code == 200
    assert client.get("/styles.css").status_code == 200
    assert client.get("/app.js").status_code == 200


def test_single_without_name_or_isin_is_rejected(client):
    response = _create_single(client, entity_name="  ")
    assert response.status_code == 400
    assert "entity name or an ISIN" in response.get_json()["error"]
    assert "název subjektu nebo ISIN" in response.get_json()["error_cs"]


def test_single_match_flow(client):
    created = _create_single(client, entity_name="Match AG", country="DE")
    assert created.status_code == 200
    job_id = created.get_json()["job_id"]
    assert created.get_json()["total"] == 1

    progress = _run(client, job_id)
    assert progress.status_code == 200
    body = progress.get_json()
    assert body["done"] is True
    assert (body["searched"], body["total"]) == (1, 1)
    assert (body["matched"], body["need_validation"], body["unmatched"]) == (
        1, 0, 0,
    )

    page = client.get(f"/results?job={job_id}")
    html = page.get_data(as_text=True)
    assert "Search complete" in html
    assert 'data-running="false"' in html
    assert MATCH_LEI in html

    csv = client.get(f"/download/csv?job={job_id}").get_data(as_text=True)
    assert MATCH_LEI in csv and "FULL_MATCH" in csv
    excel = client.get(f"/download/excel?job={job_id}")
    assert excel.status_code == 200
    assert excel.data[:2] == b"PK"


def test_bulk_flow_runs_in_chunks_and_records_decisions(
    client, monkeypatch
):
    monkeypatch.setattr(app_module, "RUN_CHUNK_SIZE", 2)
    csv = (
        "Name,ISIN,Country,City,Street,Postal code\n"
        "Match AG,,DE,Berlin,,\n"
        "Review Ltd,,GB,London,,\n"
        "Nobody s.r.o.,,CZ,Praha,,\n"
    ).encode()
    created = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(csv), "in.csv")},
        content_type="multipart/form-data",
    )
    assert created.status_code == 200, created.get_json()
    job_id = created.get_json()["job_id"]
    assert created.get_json()["total"] == 3

    first = _run(client, job_id).get_json()
    assert first["done"] is False
    assert first["searched"] == 2

    running_page = client.get(f"/results?job={job_id}")
    running_html = running_page.get_data(as_text=True)
    assert 'data-running="true"' in running_html
    assert "Searching" in running_html
    assert "Detailed results" not in running_html

    second = _run(client, job_id).get_json()
    assert second["done"] is True
    assert second["searched"] == 3
    assert (
        second["matched"], second["need_validation"], second["unmatched"]
    ) == (1, 1, 1)

    # Running again on a finished job is a no-op.
    assert _run(client, job_id).get_json()["searched"] == 3

    page = client.get(f"/results?job={job_id}").get_data(as_text=True)
    assert 'class="validate-remaining">1</span>' in page
    assert "Review Ltd" in page and "Nobody s.r.o." in page
    assert ">None<" not in page  # null fields render as empty cells

    bad = client.post(
        "/api/decision",
        json={"job_id": job_id, "index": 1, "choice": "X" * 20},
    )
    assert bad.status_code == 404

    good = client.post(
        "/api/decision",
        json={"job_id": job_id, "index": 1, "choice": REVIEW_LEI},
    )
    assert good.status_code == 200
    assert good.get_json()["decision"] == {
        "status": "confirmed", "lei": REVIEW_LEI,
    }

    decided = client.get(f"/results?job={job_id}").get_data(as_text=True)
    assert 'class="validate-remaining">0</span>' in decided
    csv_out = client.get(f"/download/csv?job={job_id}").get_data(as_text=True)
    assert "MANUAL_MATCH" in csv_out and REVIEW_LEI in csv_out


def test_bulk_rejects_wrong_extension_and_empty_file(client):
    wrong = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(b"x"), "in.xls")},
        content_type="multipart/form-data",
    )
    assert wrong.status_code == 400
    assert "Unsupported file type" in wrong.get_json()["error"]
    assert "Nepodporovaný typ souboru" in wrong.get_json()["error_cs"]

    empty = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(b""), "in.csv")},
        content_type="multipart/form-data",
    )
    assert empty.status_code == 400
    assert "empty" in empty.get_json()["error"].lower()
    assert "prázdný" in empty.get_json()["error_cs"]


def test_bulk_reads_utf16_and_cr_files_and_refuses_unreadable(client):
    rows = "Name,ISIN,Country\r\nMatch AG,,DE\r\nNobody s.r.o.,,CZ\r\n"
    # Windows PowerShell's `>` writes UTF-16; old Mac files end each
    # line with a lone \r. Both used to crash the upload with a 500.
    utf16 = rows.encode("utf-16")
    cr_only = rows.replace("\r\n", "\r").encode()
    for content in (utf16, cr_only):
        created = _create_bulk(client, content)
        assert created.status_code == 200, created.get_json()
        assert created.get_json()["total"] == 2

    # A workbook renamed to .csv, or a cell over the csv module's field
    # limit, is refused with a message instead of a 500.
    workbook = Workbook()
    workbook.active.append(["Match AG", None, "DE"])
    renamed = io.BytesIO()
    workbook.save(renamed)
    for content in (renamed.getvalue(), b"Match AG,," + b"x" * 200_000):
        refused = _create_bulk(client, content)
        assert refused.status_code == 400
        body = refused.get_json()
        assert "Could not read the file" in body["error"]
        assert "Soubor se nepodařilo přečíst" in body["error_cs"]


def test_bulk_reads_tab_separated_files(client):
    rows = [
        ["Název", "ISIN", "Země"],
        ["ČEZ, a. s.", "CZ0005112300", "CZ"],
        ["Komerční banka, a.s.", "", "CZ"],
    ]
    tsv = "\r\n".join("\t".join(row) for row in rows) + "\r\n"
    # Excel's "Unicode Text" export is a UTF-16 .txt; a .tsv, or a .csv
    # holding tabs, is usually UTF-8. The commas in the names must not
    # split them.
    for content, filename in (
        (tsv.encode("utf-16"), "in.txt"),
        (tsv.encode(), "in.tsv"),
        (tsv.encode(), "in.csv"),
    ):
        created = _create_bulk(client, content, filename)
        assert created.status_code == 200, (filename, created.get_json())
        query = storage.get_search(created.get_json()["job_id"])["query"]
        assert [entity["name"] for entity in query] == [
            "ČEZ, a. s.", "Komerční banka, a.s.",
        ]
        assert query[0]["isin"] == "CZ0005112300"

    # A .txt is always tab-separated, so a plain list of names, one per
    # line, keeps the commas inside them.
    names = "ČEZ, a. s.\nŠkoda Auto a.s.\n".encode()
    created = _create_bulk(client, names, "names.txt")
    query = storage.get_search(created.get_json()["job_id"])["query"]
    assert [entity["name"] for entity in query] == [
        "ČEZ, a. s.", "Škoda Auto a.s.",
    ]


def test_bulk_xlsx_saved_on_a_chart_sheet_reads_its_worksheet(client):
    # Excel saves the sheet on screen as the active one; a chart sheet
    # has no cells and used to crash the upload with a 500.
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Name", "ISIN", "Country"])
    sheet.append(["Match AG", None, "DE"])
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=1, min_row=1, max_row=2))
    workbook.create_chartsheet("Chart").add_chart(chart)
    workbook.active = 1
    content = io.BytesIO()
    workbook.save(content)

    created = _create_bulk(client, content.getvalue(), "in.xlsx")
    assert created.status_code == 200, created.get_json()
    assert created.get_json()["total"] == 1


def test_bulk_xlsx_of_semicolon_lines_in_column_a(client):
    # The user's file: a semicolon CSV opened in Excel with the comma as
    # the delimiter, so each line sits in column A and is split again
    # into column B at the comma in the street. Only 2 garbage entities
    # used to be read from it.
    fixture = Path(__file__).parent / "fixtures" / "test_lei.xlsx"
    created = _create_bulk(client, fixture.read_bytes(), "test_lei.xlsx")
    assert created.status_code == 200, created.get_json()
    query = storage.get_search(created.get_json()["job_id"])["query"]
    assert [(entity["name"], entity["isin"]) for entity in query] == [
        ("CBRE Investment Management Listed Real Assets LLC",
         "US12504G1004"),
        ("Real REMAX Group Inc", "US7761051082"),
        ("Abacus Global Management Inc", "US00258Y1047"),
        ("Longeveron Inc", "US54303L3024"),
        ("Nomura Holdings INC", "JP3046680009"),
        ("Nomura Holdings INC", "JP3046710004"),
        ("FIRY INC", "US83067L2088"),
    ]
    assert query[0] == {
        "name": "CBRE Investment Management Listed Real Assets LLC",
        "isin": "US12504G1004",
        "country": "US",
        "city": "Randor",
        "street": "201 King of Prussia Road, Suite 600",
        "postal_code": "PA 19087",
    }
    # FIRY's source line is itself malformed: its ZIP ended up inside
    # the quoted street.
    assert query[-1]["street"] == "1061 Market St,;CA 94103"
    assert query[-1]["postal_code"] is None


def test_bulk_refuses_damaged_or_chart_only_xlsx(client):
    # A sheet cut off halfway only fails once its rows are read, and a
    # workbook of charts alone has no worksheet: both used to be a 500.
    workbook = Workbook()
    for index in range(50):
        workbook.active.append([f"Company {index} a.s.", None, "CZ"])
    whole = io.BytesIO()
    workbook.save(whole)
    damaged = io.BytesIO()
    with zipfile.ZipFile(whole) as source, \
            zipfile.ZipFile(damaged, "w") as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = data[: len(data) // 2]
            target.writestr(item, data)

    charts_only = Workbook()
    charts_only.remove(charts_only.active)
    charts_only.create_chartsheet("Chart").add_chart(BarChart())
    charts = io.BytesIO()
    charts_only.save(charts)

    for content in (damaged.getvalue(), charts.getvalue()):
        refused = _create_bulk(client, content, "in.xlsx")
        assert refused.status_code == 400
        body = refused.get_json()
        assert "Could not read the .xlsx file" in body["error"]
        assert "Soubor .xlsx se nepodařilo přečíst" in body["error_cs"]


def test_gleif_outage_keeps_progress_and_resumes(client, monkeypatch):
    monkeypatch.setattr(app_module, "RUN_CHUNK_SIZE", 5)
    csv = b"Match AG,,DE\nDown Inc,,US\nMatch two,,DE\n"
    created = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(csv), "in.csv")},
        content_type="multipart/form-data",
    )
    job_id = created.get_json()["job_id"]

    failed = _run(client, job_id)
    assert failed.status_code == 503
    body = failed.get_json()
    assert body["error"] == app_module.GLEIF_DOWN_MESSAGE
    assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
    # The first entity completed before the outage and was kept.
    assert body["searched"] == 1 and body["done"] is False

    # GLEIF is back: the next call resumes with the failed entity.
    def _recovered(entity, client_):
        return _fake_lookup(
            entity.model_copy(update={"name": "Match"}), client_,
        )
    monkeypatch.setattr(app_module, "lookup_entity", _recovered)
    resumed = _run(client, job_id).get_json()
    assert resumed["done"] is True
    assert resumed["searched"] == 3 and resumed["matched"] == 3


def test_racing_run_calls_do_not_duplicate_rows(client, monkeypatch):
    # A refresh mid-search leaves the old /run call finishing on the
    # server while the new page starts another one. Simulate the rival
    # call storing the entity while ours is still looking it up.
    job_id = _create_single(client, entity_name="Match AG").get_json()[
        "job_id"
    ]

    def _raced(entity, client_):
        result, closest = _fake_lookup(entity, client_)
        storage.append_results(
            job_id, [app_module._result_row(entity, result, closest)], 0,
        )
        return result, closest
    monkeypatch.setattr(app_module, "lookup_entity", _raced)

    body = _run(client, job_id).get_json()
    assert (body["searched"], body["total"], body["done"]) == (1, 1, True)
    assert len(storage.get_search(job_id)["results"]) == 1


def test_unknown_job_pages(client):
    page = client.get("/results?job=nope").get_data(as_text=True)
    assert "No results to show" in page
    assert _run(client, "nope").status_code == 404
    assert client.get("/download/csv?job=nope").status_code == 404
    assert client.get("/admin").status_code == 200

