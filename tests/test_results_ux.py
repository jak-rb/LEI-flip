
"""Tests of the results page UX: Czech notes, decisions, the stepper.

Covers the notes shown in both languages (every note the lookup code
can write has a Czech version), the "You searched" line, the summary
card counts returned by /api/decision, the bilingual 404 replies, and
the stepper's handling of a failed save (driven in Node when it is on
PATH). GLEIF and OpenFIGI are faked; nothing touches the network.
"""

import html
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import app as flask_app
from main import routes as app_module
from core import isin as isin_module
from core import lookup as lookup_module
from core.models import (
    CandidateSummary,
    GleifAddress,
    GleifCandidate,
    InputEntity,
    LookupResult,
)
from core.notes import czech_note

ISIN = "US0378331005"
REVIEW_LEI = "R" * 20
ROOT = Path(__file__).resolve().parent.parent


# ---- Czech notes: every note the lookup code writes ----

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


def _candidate(status="ISSUED", name="Alpha Holding a.s.", lei="A" * 20):
    """A candidate whose legal seat is in Brno and HQ in Praha."""
    return GleifCandidate(
        lei=lei, legal_name=name, status=status,
        legal_address=GleifAddress(
            country="CZ", city="Brno", address_lines=["Legal 1"],
        ),
        hq_address=GleifAddress(
            country="CZ", city="Praha", address_lines=["Hq 2"],
        ),
    )


def _real_notes(monkeypatch):
    """Every note the lookup code can write, with its parameters.

    Each note comes out of the real code path that writes it, so a
    change to an English text shows up here as an untranslated note.
    """
    name = "Alpha Holding a.s."
    in_brno = InputEntity(name=name, town="Brno", country="CZ")
    in_praha = InputEntity(name=name, town="Praha", country="CZ")
    no_address = InputEntity(name=name)
    with_isin = InputEntity(name=name, isin=ISIN)
    isin_only = InputEntity(isin=ISIN)
    bare = GleifCandidate(lei="B" * 20, legal_name=name, status="ISSUED")
    lapsed = _candidate("LAPSED")
    retired = _candidate("RETIRED")

    def entity_note(entity, client):
        return lookup_module.lookup_entity(entity, client)[0].notes

    notes = [
        (entity_note(in_brno, _CannedGleif([_candidate()])), []),
        (entity_note(no_address, _CannedGleif()), []),
        (
            entity_note(in_praha, _CannedGleif([_candidate()])),
            [name, "Hq 2, Praha, CZ", "Legal 1, Brno, CZ"],
        ),
        (
            entity_note(in_praha, _CannedGleif([lapsed])),
            [name, "Hq 2, Praha, CZ", "Legal 1, Brno, CZ", "LAPSED"],
        ),
        (entity_note(no_address, _CannedGleif([bare])), ["100", name]),
        (
            isin_module.resolve_via_isin(
                with_isin, _CannedGleif(by_isin=[_candidate()]),
            ).notes,
            [ISIN],
        ),
        (
            isin_module._isin_confirms_hq(
                in_praha, ISIN, [retired], retired,
            ).notes,
            [ISIN, name, "RETIRED"],
        ),
        (
            isin_module._isin_confirms_name(
                no_address, ISIN, [lapsed], lapsed,
            ).notes,
            ["100", name, ISIN, "LAPSED"],
        ),
        (
            isin_module.resolve_isin_only(
                InputEntity(isin="XX12"), _CannedGleif(),
            ).notes,
            [],
        ),
        (
            isin_module.resolve_isin_only(
                isin_only, _CannedGleif(by_isin=[_candidate(), bare]),
            ).notes,
            [ISIN],
        ),
        (
            isin_module.resolve_isin_only(
                isin_only, _CannedGleif(by_isin=[retired]),
            ).notes,
            [ISIN, "RETIRED"],
        ),
        (
            isin_module.resolve_isin_only(
                isin_only, _CannedGleif(by_isin=[_candidate()]),
            ).notes,
            [ISIN],
        ),
    ]

    figi_name = "ALPHA HOLDING AS"
    monkeypatch.setattr(
        isin_module, "resolve_isin_to_names", lambda *a, **k: [figi_name],
    )
    notes.append((
        isin_module.resolve_via_isin(
            with_isin, _CannedGleif(by_name=[lapsed]),
        ).notes,
        [ISIN, figi_name, "LAPSED"],
    ))
    monkeypatch.setattr(
        lookup_module, "resolve_isin_to_names", lambda *a, **k: [],
    )
    notes.append((entity_note(isin_only, _CannedGleif()), [ISIN]))
    monkeypatch.setattr(
        lookup_module, "resolve_isin_to_names", lambda *a, **k: [figi_name],
    )
    notes.append((
        entity_note(isin_only, _CannedGleif(by_name=[_candidate()])),
        [ISIN],
    ))

    notes += [
        (app_module.LOOKUP_ERROR_NOTE, []),
        (app_module.GLEIF_REFUSED_NOTE, []),
        (app_module.GLEIF_ERRORS_NOTE, []),
        (app_module.GLEIF_TOO_SLOW_NOTE, []),
    ]
    return notes


# English phrases no Czech note may keep.
_ENGLISH_LEFTOVERS = (
    "LEI not assigned", "WARNING", "LEI status", "matches", "found in",
    "review", "Lookup failed", "Please", " the ",
)


def test_every_lookup_note_has_a_czech_version(monkeypatch):
    notes = _real_notes(monkeypatch)
    # Every distinct note text the code has (the LAPSED/RETIRED
    # variants add their status sentence to a note of their own).
    assert len({note for note, _ in notes}) == 19

    untranslated = []
    for note, params in notes:
        czech = czech_note(note)
        if (
            not note
            or czech == note
            or any(word in czech for word in _ENGLISH_LEFTOVERS)
            or any(param not in czech for param in params)
        ):
            untranslated.append((note, czech))
    assert untranslated == []


def test_czech_note_keeps_the_parameters_in_place():
    assert czech_note(
        "Strong name match (97%) with Beta & Co. (Praha), but the address "
        "was not verified - LEI not assigned. Manual review recommended."
    ) == (
        "Silná shoda názvu (97 %) se subjektem Beta & Co. (Praha), ale "
        "adresa nebyla ověřena - LEI nebylo přiřazeno. Doporučujeme ruční "
        "kontrolu."
    )
    assert czech_note(
        f"By ISIN {ISIN} the issuer is Beta SE - matches the HQ address "
        f"in GLEIF. WARNING: the LEI has status MERGED (not maintained)."
    ) == (
        f"Podle ISIN {ISIN} je emitentem Beta SE - shoduje se s adresou "
        f"centrály v GLEIF. UPOZORNĚNÍ: LEI má stav MERGED (není "
        f"udržováno)."
    )


@pytest.mark.parametrize("note", [
    "Strong name match, unverified",
    "Manually confirmed",
    # A known status sentence after an unknown note: all stays English.
    "Something else entirely. LEI status: LAPSED.",
    "No LEI found in the GLEIF database. And more.",
])
def test_unknown_note_falls_back_to_english(note):
    assert czech_note(note) == note


def test_missing_note_is_empty():
    assert czech_note(None) == ""
    assert czech_note("") == ""


# ---- The results page, with the lookup faked ----

class _FakeGleifClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


#: Notes the fake lookup gives the entities named after the keys.
_FAKE_NOTES = {
    "nothing": "No LEI found in the GLEIF database.",
    "refused": app_module.GLEIF_REFUSED_NOTE,
    "odd": "Strong name match, unverified",
    "tricky": (
        'Strong name match (93%) with <b>"Q&A"</b> s.r.o., but the '
        "address was not verified - LEI not assigned. Manual review "
        "recommended."
    ),
}


def _fake_lookup(entity, client):
    """Names starting "review" are near-misses; others no-matches."""
    name = (entity.name or "").lower()
    if name.startswith("review"):
        candidate = CandidateSummary(
            legal_name="Review Ltd", lei=REVIEW_LEI, status="ISSUED",
        )
        return LookupResult(notes=_FAKE_NOTES["tricky"]), [candidate]
    return LookupResult(notes=_FAKE_NOTES.get(name, "")), []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "lookup_entity", _fake_lookup)
    monkeypatch.setattr(app_module, "GleifClient", _FakeGleifClient)
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def _finished_job(client, lines):
    """Create a bulk job of these CSV lines and run it to the end."""
    content = "\n".join(lines).encode()
    created = client.post(
        "/api/jobs",
        data={"mode": "bulk", "file_upload": (io.BytesIO(content), "in.csv")},
        content_type="multipart/form-data",
    )
    assert created.status_code == 200, created.get_json()
    job_id = created.get_json()["job_id"]
    while not client.post(f"/api/jobs/{job_id}/run").get_json()["done"]:
        pass
    return job_id


def _page(client, job_id):
    return client.get(f"/results?job={job_id}").get_data(as_text=True)


def _no_match_notes(page):
    """(English, Czech, shown text) of each no-match Notes cell."""
    table = page.split('class="results-table table-nomatch"', 1)[1]
    cells = re.findall(
        r'<td data-en="([^"]*)"\s+data-cs="([^"]*)">([^<]*)</td>', table,
    )
    return [tuple(html.unescape(part) for part in cell) for cell in cells]


def test_no_match_table_shows_notes_in_both_languages(client):
    job_id = _finished_job(
        client, ["Nothing", "Refused", "Odd", "Blank", "Review A"],
    )
    client.post(
        "/api/decision",
        json={"job_id": job_id, "index": 4, "choice": "none"},
    )

    assert _no_match_notes(_page(client, job_id)) == [
        (
            "No LEI found in the GLEIF database.",
            "V databázi GLEIF nebylo nalezeno žádné LEI.",
            "No LEI found in the GLEIF database.",
        ),
        (
            app_module.GLEIF_REFUSED_NOTE,
            "Vyhledání selhalo: GLEIF dotaz odmítl - LEI nebylo "
            "přiřazeno. Zkontrolujte prosím zadané hodnoty.",
            app_module.GLEIF_REFUSED_NOTE,
        ),
        (
            "Strong name match, unverified",
            "Strong name match, unverified",
            "Strong name match, unverified",
        ),
        ("", "", ""),
        (
            _FAKE_NOTES["tricky"],
            'Silná shoda názvu (93 %) se subjektem <b>"Q&A"</b> s.r.o., '
            "ale adresa nebyla ověřena - LEI nebylo přiřazeno. "
            "Doporučujeme ruční kontrolu.",
            _FAKE_NOTES["tricky"],
        ),
    ]


def test_downloads_keep_the_english_notes(client):
    job_id = _finished_job(client, ["Nothing", "Refused"])
    csv = client.get(f"/download/csv?job={job_id}").get_data(as_text=True)
    assert "No LEI found in the GLEIF database." in csv
    assert "GLEIF refused the query" in csv
    assert "nebylo" not in csv


def test_you_searched_shows_only_the_given_place_parts(client):
    job_id = _finished_job(client, [
        "Review City,,,Praha",
        "Review Country,,CZ",
        "Review Both,,CZ,Praha",
        "Review Neither",
    ])
    lines = [
        " ".join(re.sub(r"<[^>]+>", " ", block).split())
        for block in re.findall(
            r'<p class="detail-searched">(.*?)</p>', _page(client, job_id),
            re.DOTALL,
        )
    ]
    assert lines == [
        "You searched: Review City (Praha)",
        "You searched: Review Country (CZ)",
        "You searched: Review Both (CZ, Praha)",
        "You searched: Review Neither",
    ]


@pytest.mark.parametrize(
    ("country", "city", "expected"),
    [
        ("   ", "Praha", "You searched: Review Single (Praha)"),
        ("CZ", "  ", "You searched: Review Single (CZ)"),
        (" ", " ", "You searched: Review Single"),
        (" CZ ", " Praha ", "You searched: Review Single (CZ, Praha)"),
    ],
)
def test_you_searched_skips_blank_place_parts_of_the_form(
    client, country, city, expected,
):
    # The single form stores country and city as typed, so a part
    # made only of spaces must count as not given.
    created = client.post("/api/jobs", data={
        "mode": "single", "entity_name": "Review Single",
        "country": country, "city": city,
    })
    assert created.status_code == 200, created.get_json()
    job_id = created.get_json()["job_id"]
    while not client.post(f"/api/jobs/{job_id}/run").get_json()["done"]:
        pass

    block = re.search(
        r'<p class="detail-searched">(.*?)</p>', _page(client, job_id),
        re.DOTALL,
    ).group(1)
    assert " ".join(re.sub(r"<[^>]+>", " ", block).split()) == expected


def _card_counts(page):
    """The Matched / Need validation / Unmatched numbers on the card."""
    return {
        key: int(re.search(
            rf'id="metric-{metric}">(\d+)<', page,
        ).group(1))
        for key, metric in (
            ("matched", "matched"),
            ("need_validation", "validate"),
            ("unmatched", "unmatched"),
        )
    }


def test_decision_reply_carries_the_card_counts(client):
    job_id = _finished_job(client, ["Review A", "Review B", "Nothing"])
    assert _card_counts(_page(client, job_id)) == {
        "matched": 0, "need_validation": 2, "unmatched": 1,
    }

    steps = [
        (0, REVIEW_LEI, {"matched": 1, "need_validation": 1, "unmatched": 1}),
        (1, "none", {"matched": 1, "need_validation": 0, "unmatched": 2}),
        (0, "none", {"matched": 0, "need_validation": 0, "unmatched": 3}),
    ]
    for index, choice, expected in steps:
        reply = client.post(
            "/api/decision",
            json={"job_id": job_id, "index": index, "choice": choice},
        )
        assert reply.status_code == 200
        assert reply.get_json()["counts"] == expected
        # The same numbers the page shows once reloaded.
        assert _card_counts(_page(client, job_id)) == expected


def test_stepper_has_a_save_error_line_and_a_reload_hint(client):
    job_id = _finished_job(client, ["Review A"])
    page = _page(client, job_id)
    stepper = page.split('<section class="validation"', 1)[1]
    stepper = stepper.split("</section>", 1)[0]
    assert re.search(r'<p class="validation-error" role="alert">\s*</p>',
                     stepper)

    hint = re.search(r'<p class="results-page-note tables-reload-hint"'
                     r'([^>]*)>', page)
    assert hint and " hidden" in hint.group(1)
    assert 'data-en="Reload the page to update the tables below."' in (
        hint.group(1)
    )
    assert 'data-cs="Tabulky níže se aktualizují po obnovení stránky."' in (
        hint.group(1)
    )


def test_not_found_replies_are_bilingual(client):
    unknown = "0" * 32
    for response in (
        client.post(f"/api/jobs/{unknown}/run"),
        client.post(
            "/api/decision",
            json={"job_id": unknown, "index": 0, "choice": "none"},
        ),
    ):
        assert response.status_code == 404
        body = response.get_json()
        assert body["error"] and body["error_cs"]
        assert body["error"] != body["error_cs"]
        assert body["error"] != "not found"


def test_decision_buttons_have_a_disabled_look():
    css = (ROOT / "src" / "main" / "static" / "styles.css").read_text(
        encoding="utf-8"
    )
    rules = re.findall(r"([^{}]+)\{([^}]*)\}", css)
    looks = [
        body for selectors, body in rules
        if ".candidate-confirm:disabled" in selectors
        and ".validate-none:disabled" in selectors
    ]
    assert looks and "cursor: progress" in looks[0]
    assert "opacity" in looks[0]


# ---- The stepper's saves, driven in Node ----

_APP_JS = ROOT / "src" / "main" / "static" / "app.js"
_SAVE_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

class ClassList {
    constructor(names) { this.names = new Set(names); }
    add(...names) { names.forEach((n) => this.names.add(n)); }
    remove(...names) { names.forEach((n) => this.names.delete(n)); }
    contains(name) { return this.names.has(name); }
    toggle(name, force) {
        const on = force === undefined ? !this.names.has(name) : !!force;
        if (on) { this.names.add(name); } else { this.names.delete(name); }
        return on;
    }
}

class Element {
    constructor(className, options = {}, children = []) {
        this.classList = new ClassList(className.split(" "));
        this.dataset = options.dataset || {};
        this.attributes = Object.assign({}, options.attributes);
        this.id = options.id || null;
        this.textContent = options.text || "";
        this.hidden = Boolean(options.hidden);
        this.children = children;
        this.parent = null;
        this.listeners = [];
        this.disabled = false;
        this.offsetWidth = 0;
        children.forEach((child) => { child.parent = this; });
    }
    // As in a browser, whatever is assigned is read back as a string.
    get textContent() { return this.text; }
    set textContent(value) { this.text = String(value); }
    get nextElementSibling() {
        const siblings = this.parent ? this.parent.children : [];
        return siblings[siblings.indexOf(this) + 1] || null;
    }
    walk(visit) {
        this.children.forEach((child) => {
            visit(child);
            child.walk(visit);
        });
    }
    querySelectorAll(selector) {
        const names = selector.split(",").map((s) => s.trim().slice(1));
        const found = [];
        this.walk((el) => {
            if (names.some((n) => el.classList.contains(n))) {
                found.push(el);
            }
        });
        return found;
    }
    querySelector(selector) {
        return this.querySelectorAll(selector)[0] || null;
    }
    getAttribute(name) {
        return name in this.attributes ? this.attributes[name] : null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(type, listener) { this.listeners.push(listener); }
    // A disabled button ignores clicks, as in a browser.
    click() {
        if (!this.disabled) { this.listeners.forEach((fn) => fn({})); }
    }
}

const records = [0, 1].map((i) => new Element(
    "validate-record",
    { dataset: { index: String(i), decision: "", chosen: "" } },
    [
        new Element("candidate-row", { dataset: { lei: `LEI${i}` } }, [
            new Element("btn candidate-confirm"),
        ]),
        new Element("candidate-detail"),
        new Element("btn validate-none"),
    ],
));
const lang = process.argv[3];
const errorLine = new Element("validation-error");
const job = "a".repeat(32);
const section = new Element("validation", { dataset: { job } }, [
    new Element("validation-prev"),
    new Element("validation-position"),
    new Element("validation-next"),
    ...records,
    errorLine,
]);
const metric = (id, text) => new Element("metric-value", { id, text });
const state = new Element("results-state-text", {
    text: lang === "cs" ? "Vyžaduje kontrolu" : "Needs review",
    attributes: { "data-en": "Needs review", "data-cs": "Vyžaduje kontrolu" },
});
const card = new Element("results-card", { id: "results-card" }, [
    state,
    metric("metric-searched", "3 / 3"),
    metric("metric-matched", "0"),
    metric("metric-validate", "2"),
    metric("metric-unmatched", "1"),
]);
const hint = new Element("results-page-note tables-reload-hint", {
    hidden: true,
});
const page = new Element("page", {}, [
    card, new Element("validate-remaining"), section, hint,
]);
let found = null;
const document = {
    documentElement: { lang, getAttribute: () => null },
    querySelector: (selector) => page.querySelector(selector),
    getElementById: (id) => {
        found = null;
        page.walk((el) => { if (el.id === id && !found) { found = el; } });
        return found;
    },
    addEventListener() {},
};

const steps = JSON.parse(process.argv[4]);
let step = null;
function fetch(url, options) {
    const choice = JSON.parse(options.body).choice;
    if (step.reply === "network") {
        return Promise.reject(new TypeError("Failed to fetch"));
    }
    if (step.reply === "404") {
        return Promise.resolve({
            ok: false,
            status: 404,
            json: async () => ({ error: "x", error_cs: "y" }),
        });
    }
    if (step.reply === "badjson") {
        return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => { throw new SyntaxError("Unexpected token"); },
        });
    }
    const status = choice === "none" ? "none" : "confirmed";
    return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
            ok: true,
            decision: status === "none" ? { status } : { status, lei: choice },
            counts: step.counts,
        }),
    });
}

(async () => {
    const context = vm.createContext({ document, fetch, console });
    vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
    vm.runInContext("setupValidation()", context);
    const buttons = section.querySelectorAll(
        ".candidate-confirm, .validate-none",
    );
    const seen = [];
    for (step of steps) {
        const record = records[step.record];
        const button = step.click === "none"
            ? record.querySelector(".validate-none")
            : record.querySelector(".candidate-confirm");
        button.click();
        await new Promise((resolve) => setImmediate(resolve));
        seen.push({
            error: errorLine.textContent,
            errorEn: errorLine.getAttribute("data-en") || "",
            errorCs: errorLine.getAttribute("data-cs") || "",
            decision: record.dataset.decision,
            enabled: buttons.every((btn) => !btn.disabled),
            matched: document.getElementById("metric-matched").textContent,
            validate: document.getElementById("metric-validate").textContent,
            unmatched: document.getElementById("metric-unmatched").textContent,
            searched: document.getElementById("metric-searched").textContent,
            state: state.textContent,
            stateEn: state.getAttribute("data-en"),
            stateCs: state.getAttribute("data-cs"),
            hintHidden: hint.hidden,
        });
    }
    console.log(JSON.stringify(seen));
})();
"""

_SAVE_FAILED_EN = "Saving failed. Reload the page and try again."
_SAVE_FAILED_CS = "Uložení se nezdařilo. Obnovte stránku a zkuste to znovu."

_needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node.js on PATH",
)


def _run_saves(tmp_path, lang, steps):
    """Run the save harness on app.js; return each step seen."""
    harness = tmp_path / "save_harness.js"
    harness.write_text(_SAVE_HARNESS, encoding="utf-8")
    finished = subprocess.run(
        ["node", str(harness), str(_APP_JS), lang, json.dumps(steps)],
        capture_output=True, text=True, timeout=60, check=True,
        encoding="utf-8",
    )
    return json.loads(finished.stdout)


@_needs_node
@pytest.mark.parametrize("reply", ["404", "network", "badjson"])
def test_failed_save_says_so_and_leaves_the_record_undecided(
    tmp_path, reply,
):
    seen = _run_saves(tmp_path, "en", [{"record": 0, "reply": reply}])
    assert seen == [{
        "error": _SAVE_FAILED_EN,
        "errorEn": _SAVE_FAILED_EN,
        "errorCs": _SAVE_FAILED_CS,
        "decision": "",
        "enabled": True,
        "matched": "0",
        "validate": "2",
        "unmatched": "1",
        "searched": "3 / 3",
        "state": "Needs review",
        "stateEn": "Needs review",
        "stateCs": "Vyžaduje kontrolu",
        "hintHidden": True,
    }]


@_needs_node
def test_failed_save_in_czech_then_a_good_save_updates_the_card(tmp_path):
    seen = _run_saves(tmp_path, "cs", [
        {"record": 0, "reply": "404"},
        {
            "record": 0, "reply": "ok",
            "counts": {"matched": 1, "need_validation": 1, "unmatched": 1},
        },
        {
            "record": 1, "click": "none", "reply": "ok",
            "counts": {"matched": 1, "need_validation": 0, "unmatched": 2},
        },
        {
            "record": 0, "click": "none", "reply": "ok",
            "counts": {"matched": 0, "need_validation": 0, "unmatched": 3},
        },
    ])

    assert seen[0]["error"] == _SAVE_FAILED_CS
    assert seen[0]["decision"] == ""

    # A good save clears the message and brings the card up to date.
    assert [
        (view["error"], view["errorEn"], view["errorCs"]) for view in seen[1:]
    ] == [("", "", "")] * 3
    assert [
        (view["matched"], view["validate"], view["unmatched"])
        for view in seen
    ] == [("0", "2", "1"), ("1", "1", "1"), ("1", "0", "2"), ("0", "0", "3")]
    assert all(view["searched"] == "3 / 3" for view in seen)
    assert [(view["state"], view["stateEn"]) for view in seen] == [
        ("Vyžaduje kontrolu", "Needs review"),
        ("Vyhledávání dokončeno", "Search complete"),
        ("Vyhledávání dokončeno", "Search complete"),
        ("Nenalezeny žádné shody", "No matches found"),
    ]
    assert seen[3]["stateCs"] == "Nenalezeny žádné shody"
    assert [view["hintHidden"] for view in seen] == [True, False, False, False]
    assert [view["decision"] for view in seen] == [
        "", "confirmed", "none", "none",
    ]
    assert all(view["enabled"] for view in seen)


def test_card_state_texts_match_the_template():
    # The script words the card's state line as results.html does.
    template = (
        ROOT / "src" / "main" / "templates" / "results.html"
    ).read_text(encoding="utf-8")
    script = _APP_JS.read_text(encoding="utf-8")
    for key, state in (
        ("stateReview", "review"),
        ("stateNone", "none"),
        ("stateComplete", "complete"),
    ):
        english, czech = re.search(
            rf"'{state}': \('([^']+)', '([^']+)'\)", template,
        ).groups()
        assert f'{key}: "{english}"' in script
        assert f'{key}: "{czech}"' in script

