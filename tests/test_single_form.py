
"""Tests of the single form, the request limits and GLEIF auth errors.

The single form's fields are trimmed and an invisible-only name or
ISIN counts as empty; the ISIN's country prefix reaches GLEIF only as
two ASCII letters; oversized requests get a JSON 413 while form
fields may use the whole upload cap; and GLEIF refusing access (401,
403, 407) to every request counts as GLEIF being unavailable, not as
one bad query.
Nothing here touches the network: lookups, GLEIF's HTTP session and
OpenFIGI are faked.
"""

import importlib.metadata
import io
import json
import random
import string
from html.parser import HTMLParser
from pathlib import Path

import pytest
import requests
import urllib3

from app import app as flask_app
from main import routes as app_module
from core import gleif, openfigi, storage
from core import isin as core_isin, lookup as core_lookup
from core.gleif import GleifClient, GleifQueryError
from core.isin import is_valid_isin
from core.models import InputEntity, LookupResult

ROOT = Path(__file__).resolve().parents[1]
APPLE_ISIN = "US0378331005"
NEED_NAME_OR_ISIN = "Please enter an entity name or an ISIN."
NEED_NAME_OR_ISIN_CS = "Zadejte název subjektu nebo ISIN."


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
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _create_single(client, **fields):
    return client.post("/api/jobs", data={"mode": "single", **fields})


def _stored_input(response):
    """The stored query record of a single-form job just created."""
    assert response.status_code == 200, response.get_json()
    job_id = response.get_json()["job_id"]
    return storage.get_search(job_id)["query"][0]


# ---- (a) trimmed single-form fields ----

@pytest.mark.parametrize("isin", [
    " " + APPLE_ISIN,
    APPLE_ISIN + " " * 9,
    "\t" + APPLE_ISIN + " " * 12,
])
def test_padded_isin_is_trimmed_and_accepted(client, isin):
    stored = _stored_input(_create_single(
        client, entity_name="Apple Inc", isin=isin,
    ))
    assert stored["isin"] == APPLE_ISIN


def test_padded_isin_alone_is_an_isin_only_search(client):
    stored = _stored_input(_create_single(
        client, isin=" " * 5 + APPLE_ISIN + " " * 9,
    ))
    assert (stored["name"], stored["isin"]) == (None, APPLE_ISIN)


def test_whitespace_only_isin_is_no_isin(client):
    stored = _stored_input(_create_single(
        client, entity_name="Apple Inc", isin="   ",
    ))
    assert stored["isin"] is None


def test_address_fields_are_trimmed(client):
    stored = _stored_input(_create_single(
        client, entity_name="  ČEZ, a. s.  ", country=" CZ ",
        city="\tPraha  ", street="  Duhová 2/1444 ", postal_code=" 14053 ",
    ))
    assert stored == {
        "name": "ČEZ, a. s.",
        "isin": None,
        "country": "CZ",
        "city": "Praha",
        "street": "Duhová 2/1444",
        "postal_code": "14053",
    }


def test_whitespace_only_address_fields_are_empty(client):
    stored = _stored_input(_create_single(
        client, entity_name="Apple Inc", country="  ", city="\t",
        street=" \n ", postal_code="   ",
    ))
    assert [stored[key] for key in (
        "country", "city", "street", "postal_code",
    )] == [None, None, None, None]


@pytest.mark.parametrize("name, isin", [
    ("   ", "   "),
    ("\u200b", ""),
    ("\ufeff", None),
    (" \u200b \ufeff\u2060 ", "\t"),
    ("", "\u200b"),
    ("\u200b", " \ufeff "),
])
def test_blank_or_invisible_name_and_isin_need_a_name_or_isin(
    client, name, isin,
):
    fields = {"entity_name": name}
    if isin is not None:
        fields["isin"] = isin
    response = _create_single(client, **fields)
    assert response.status_code == 400
    assert response.get_json() == {
        "error": NEED_NAME_OR_ISIN,
        "error_cs": NEED_NAME_OR_ISIN_CS,
    }


def test_invisible_only_name_with_an_isin_is_an_isin_only_search(client):
    stored = _stored_input(_create_single(
        client, entity_name="\u200b\ufeff", isin=APPLE_ISIN,
    ))
    assert (stored["name"], stored["isin"]) == (None, APPLE_ISIN)


def test_name_keeps_its_inner_text(client):
    stored = _stored_input(_create_single(
        client, entity_name=" \u200bApple  Inc ",
    ))
    assert stored["name"] == "\u200bApple  Inc"


# ---- (b) the ISIN's country prefix sent to GLEIF ----

def _valid_isin(rng):
    """A random ISIN with a correct check digit."""
    body = "".join(rng.choice(string.ascii_uppercase) for _ in range(2))
    body += "".join(
        rng.choice(string.ascii_uppercase + string.digits) for _ in range(9)
    )
    for digit in string.digits:
        if is_valid_isin(body + digit):
            return body + digit
    raise AssertionError(f"no check digit for {body}")


def _valid_isins():
    rng = random.Random(20260923)
    known = [
        APPLE_ISIN, "CZ0005112300", "CZ0008019106", "CZ0008040318",
        "JP3046680009", "JP3046710004", "US12504G1004",
    ]
    return known + [_valid_isin(rng) for _ in range(500)]


def test_valid_isins_give_the_same_country_prefix_as_before():
    for isin in _valid_isins():
        assert is_valid_isin(isin)
        for raw in (isin, isin.lower()):
            # The rule before this fix: the raw first two characters.
            assert core_lookup._isin_country(raw) == raw[:2].upper()


@pytest.mark.parametrize("isin, country", [
    (" " + APPLE_ISIN, "US"),
    ("U S0378331005", "US"),
    ("us 0378 3310 05", "US"),
    ("   ", None),
    ("\t\t", None),
    ("12345", None),
    ("1US0378331005", None),
    ("ÄB0378331005", None),
    ("\u200bUS0378331005", None),
    ("U", None),
    ("", None),
    (None, None),
])
def test_malformed_isin_gives_a_country_only_when_two_letters(isin, country):
    assert core_lookup._isin_country(isin) == country


class _RecordingClient:
    """A GLEIF client that finds nothing and records country filters."""

    deadline = None

    def __init__(self):
        self.countries = []

    def search_by_name(self, name, country=None, page_size=10):
        self.countries.append(country)
        return []

    def search_by_name_no_country(self, name, page_size=10):
        self.countries.append(None)
        return []

    def search_by_isin(self, isin):
        return []

    def lookup_by_isin(self, isin):
        return []


@pytest.mark.parametrize("isin, countries", [
    (" " + APPLE_ISIN, [None, "US", None]),
    (APPLE_ISIN, [None, "US", None]),
    ("  ", [None]),
    ("12", [None]),
])
def test_lookup_sends_only_a_letter_prefix_as_country_filter(
    monkeypatch, isin, countries,
):
    monkeypatch.setattr(
        core_isin, "resolve_isin_to_names", lambda *args, **kwargs: [],
    )
    recording = _RecordingClient()
    core_lookup.lookup_entity(
        InputEntity(name="Apple Inc", isin=isin), recording,
    )
    assert recording.countries == countries


# ---- (c) request limits ----

TOO_LARGE = {
    "error": "The request is too large (max 4 MB).",
    "error_cs": "Požadavek je příliš velký (max. 4 MB).",
}


@pytest.mark.parametrize("content_type", [
    "multipart/form-data", "application/x-www-form-urlencoded",
])
@pytest.mark.parametrize("size", [500_001, 3 * 1024 * 1024])
def test_long_text_field_under_the_cap_gets_the_json_field_error(
    client, content_type, size,
):
    response = client.post(
        "/api/jobs",
        data={"mode": "single", "entity_name": "A" * size},
        content_type=content_type,
    )
    assert response.status_code == 400
    assert response.get_json() == {
        "error": "Please check the entered values.",
        "error_cs": "Zkontrolujte prosím zadané hodnoty.",
    }


def test_padded_name_under_the_cap_is_trimmed(client):
    stored = _stored_input(client.post(
        "/api/jobs",
        data={
            "mode": "single",
            "entity_name": " " * 600_000 + "Apple Inc" + " " * 600_000,
        },
        content_type="multipart/form-data",
    ))
    assert stored["name"] == "Apple Inc"


def test_request_over_the_cap_gets_a_json_413(client):
    content = b"x" * (app_module.MAX_UPLOAD_BYTES + 1)
    response = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), "a.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert response.is_json
    assert response.get_json() == TOO_LARGE


def test_text_field_over_the_cap_gets_a_json_413(client):
    response = client.post(
        "/api/jobs",
        data={
            "mode": "single",
            "entity_name": "A" * (app_module.MAX_UPLOAD_BYTES + 1),
        },
    )
    assert response.status_code == 413
    assert response.get_json() == TOO_LARGE


def test_too_many_form_parts_get_a_json_413(client):
    data = {"mode": "single", "entity_name": "Acme"}
    data.update({f"field{index}": "x" for index in range(1001)})
    response = client.post(
        "/api/jobs", data=data, content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert response.get_json() == TOO_LARGE


class _Inputs(HTMLParser):
    """Collects the maxlength of every named <input> of a page."""

    def __init__(self):
        super().__init__()
        self.maxlength = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name"):
            self.maxlength[attrs["name"]] = attrs.get("maxlength")


def _model_max_length(field):
    metadata = InputEntity.model_fields[field].metadata
    return next(item.max_length for item in metadata
                if hasattr(item, "max_length"))


def _page_maxlength(client):
    """The maxlength of every named input of the search page."""
    inputs = _Inputs()
    inputs.feed(client.get("/").get_data(as_text=True))
    return inputs.maxlength


def test_free_text_inputs_have_the_model_length_limits(client):
    maxlength = _page_maxlength(client)
    form_to_model = {
        "entity_name": "name",
        "country": "country",
        "city": "town",
        "street": "street",
    }
    for form_name, model_field in form_to_model.items():
        assert maxlength[form_name] == str(
            _model_max_length(model_field)
        ), form_name


# The browser applies maxlength to a paste before anything trims it,
# while the server trims first. A cap at the model limit on a short
# identifier would send a padded value cut short, and the server would
# search that wrong value instead of the one pasted.
@pytest.mark.parametrize("form_name, padded, trimmed", [
    ("isin", " " * 9 + APPLE_ISIN, APPLE_ISIN),
    ("isin", "\t" * 30 + APPLE_ISIN + " " * 30, APPLE_ISIN),
    ("postal_code", " " * 15 + "110 00", "110 00"),
])
def test_page_does_not_cut_a_padded_value_the_server_accepts(
    client, form_name, padded, trimmed,
):
    assert _page_maxlength(client)[form_name] is None
    stored = _stored_input(_create_single(
        client, entity_name="Apple Inc", **{form_name: padded},
    ))
    assert stored[form_name] == trimmed


def test_requirements_pin_the_installed_werkzeug():
    pins = (ROOT / "requirements.txt").read_text().splitlines()
    installed = importlib.metadata.version("werkzeug")
    assert f"Werkzeug=={installed}" in pins


# ---- (d) GLEIF refusing access ----

class _FakeSession:
    """Stands in for requests.Session: each GET goes to ``handler``."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, stream=False):
        self.calls.append(dict(params or {}))
        return self.handler(dict(params or {}))

    def close(self):
        pass


def _raw(body):
    """A reply body as requests streams it (see core.gleif.read_body)."""
    return urllib3.HTTPResponse(body=io.BytesIO(body), preload_content=False)


def _response(status=200):
    response = requests.Response()
    response.status_code = status
    response.raw = _raw(json.dumps(
        {"data": []} if status == 200 else {"errors": [{"status": status}]}
    ).encode())
    response.url = "https://gleif.invalid/api/v1/lei-records"
    return response


def _no_openfigi(*args, **kwargs):
    raise requests.ConnectionError("OpenFIGI is offline in tests")


@pytest.fixture
def session(monkeypatch):
    fake = _FakeSession(lambda params: _response())
    monkeypatch.setattr(gleif.requests, "Session", lambda: fake)
    monkeypatch.setattr(openfigi.requests, "post", _no_openfigi)
    return fake


@pytest.mark.parametrize("status", [401, 403, 407])
def test_gleif_denying_access_is_not_asked_again(session, status):
    session.handler = lambda params: _response(status)
    with GleifClient() as gleif_client:
        with pytest.raises(GleifQueryError) as raised:
            gleif_client.search_by_name("Alpha a.s.")
    assert f"HTTP {status}" in str(raised.value)
    # Asking again at once would only be denied again. Whether GLEIF
    # denies this query or every request, the job runner asks GLEIF
    # itself (see tests/test_runner_outage.py).
    assert len(session.calls) == 1


@pytest.mark.parametrize("status", [400, 404, 405, 414, 422])
def test_other_gleif_4xx_still_refuse_only_the_query(session, status):
    session.handler = lambda params: _response(status)
    with GleifClient() as gleif_client:
        with pytest.raises(GleifQueryError):
            gleif_client.search_by_name("Alpha a.s.")


@pytest.mark.parametrize("status", [401, 403, 407])
def test_gleif_denying_access_answers_503_and_keeps_the_entity(
    monkeypatch, session, status,
):
    monkeypatch.setattr(app_module, "RUN_CHUNK_SIZE", 5)
    # From Denied on, GLEIF denies the app every request, the runner's
    # check whether GLEIF answers at all included.
    denied = {"on": False}

    def handler(params):
        if any("Denied" in str(value) for value in params.values()):
            denied["on"] = True
        return _response(status) if denied["on"] else _response()
    session.handler = handler
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    content = b"Alpha a.s.,,CZ\nDenied a.s.,,CZ\nGamma a.s.,,CZ"
    created = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), "in.csv")},
        content_type="multipart/form-data",
    )
    job_id = created.get_json()["job_id"]

    # However often it is retried, the entity is never given up: the
    # job waits for GLEIF to let it in again.
    for _ in range(app_module.RUN_MAX_ATTEMPTS + 1):
        response = client.post(f"/api/jobs/{job_id}/run")
        assert response.status_code == 503
        body = response.get_json()
        assert body["error"] == app_module.GLEIF_DOWN_MESSAGE
        assert body["error_cs"] == app_module.GLEIF_DOWN_MESSAGE_CS
        assert (body["searched"], body["done"]) == (1, False)
    assert "failed_attempts" not in storage.get_search(job_id)["query"][1]

    # Once GLEIF lets it in, the job resumes from the saved progress.
    session.handler = lambda params: _response()
    response = client.post(f"/api/jobs/{job_id}/run")
    assert response.status_code == 200
    body = response.get_json()
    assert (body["searched"], body["done"]) == (3, True)

