
"""Tests of public/app.js outside the stepper, driven in Node.

Covers script-written messages following a CZ/EN switch (form errors,
server errors and the results card), the Search button staying
disabled once the results page is on its way, an explicit theme
choice beating a later OS colour-scheme change, the OpenFIGI pause
countdown, and the bulk form's file / pasted rows switch. A tiny fake
DOM stands in for the page; the tests are skipped when Node is not on
PATH.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_APP_JS = Path(__file__).resolve().parent.parent / "public" / "app.js"

_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

class Element {
    constructor(attributes = {}) {
        this.attributes = { ...attributes };
        this.listeners = {};
        this.textContent = "";
        this.disabled = false;
        this.classList = { toggle() {}, add() {}, remove() {} };
    }
    getAttribute(name) {
        return name in this.attributes ? this.attributes[name] : null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(type, listener) {
        (this.listeners[type] = this.listeners[type] || []).push(listener);
    }
    fire(type, event = {}) {
        (this.listeners[type] || []).forEach((listener) => listener(event));
    }
}

const errorEl = new Element({ class: "form-error" });
const submit = new Element();
const form = new Element();
form.querySelector = (selector) =>
    selector === ".form-error" ? errorEl : submit;
const state = new Element();
const card = new Element();
card.classList = { add() {}, remove() {} };
const themeButton = new Element();
const html = new Element({ "data-theme": "light" });
html.lang = process.argv[3];
const listeners = {};
const document = {
    documentElement: html,
    addEventListener() {},
    getElementById: (id) => ({ "theme-toggle": themeButton })[id] || null,
    querySelectorAll(selector) {
        // Every element that carries translations, as applyLang asks.
        if (selector === "[data-cs]") {
            return [errorEl, state].filter((el) => el.getAttribute("data-cs"));
        }
        return [];
    },
};
const media = new Element();
media.matches = false;
const window = {
    location: { href: "/" },
    matchMedia: () => media,
    addEventListener() {},
};
const stored = {};
const localStorage = {
    getItem: (key) => (key in stored ? stored[key] : null),
    setItem: (key, value) => { stored[key] = value; },
};
let reply = null;
async function fetch() {
    if (reply === "offline") { throw new TypeError("network error"); }
    return {
        ok: reply.status === 200,
        status: reply.status,
        json: async () => reply.body,
    };
}
const timers = [];
function setTimeout(callback) { timers.push(callback); }

const context = vm.createContext({
    document, window, localStorage, fetch, console, setTimeout,
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
context.els = { card, state };
context.form = form;

const steps = JSON.parse(process.argv[4]);
const seen = [];
(async () => {
    for (const step of steps) {
        if (step.do === "submit") {
            reply = step.reply;
            await vm.runInContext("submitSearch(form, {})", context);
        } else if (step.do === "formError") {
            const key = JSON.stringify(step.key);
            vm.runInContext(`showFormError(form, both(${key}))`, context);
        } else if (step.do === "cardError") {
            context.data = step.data;
            vm.runInContext(
                "showResultsError(els,"
                + ' serverMessage(data) || both("serverFailed"))',
                context,
            );
        } else if (step.do === "throttle") {
            state.setAttribute("data-en", "Searching…");
            state.setAttribute("data-cs", "Vyhledávám…");
            const waiting = vm.runInContext(
                `waitOutThrottle(els, 3, ${JSON.stringify(step.service)})`,
                context,
            );
            await Promise.resolve();
            seen.push({ countdown: state.textContent });
            while (timers.length) {
                timers.shift()();
                await Promise.resolve();
                await Promise.resolve();
            }
            await waiting;
        } else if (step.do === "lang") {
            vm.runInContext(`setLang(${JSON.stringify(step.lang)})`, context);
        } else if (step.do === "setupTheme") {
            vm.runInContext("setupThemeAndLang()", context);
        } else if (step.do === "clickTheme") {
            themeButton.fire("click");
        } else if (step.do === "osScheme") {
            media.fire("change", { matches: step.dark });
        }
        seen.push({
            error: errorEl.textContent,
            state: state.textContent,
            disabled: submit.disabled,
            href: window.location.href,
            theme: html.getAttribute("data-theme"),
        });
    }
    process.stdout.write(JSON.stringify(seen));
})().catch((error) => { console.error(error); process.exit(1); });
"""

#: The bulk form alone: its switch, both panels and the paste box.
_BULK_HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

let focused = null;
class Element {
    constructor(attributes = {}) {
        this.attributes = { ...attributes };
        this.listeners = {};
        this.textContent = "";
        this.disabled = false;
        this.hidden = false;
        this.value = "";
        this.files = [];
        this.classList = { toggle() {}, add() {}, remove() {} };
    }
    getAttribute(name) {
        return name in this.attributes ? this.attributes[name] : null;
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(type, listener) {
        (this.listeners[type] = this.listeners[type] || []).push(listener);
    }
    fire(type, event = {}) {
        (this.listeners[type] || []).forEach((listener) => listener(event));
    }
    focus() { focused = this; }
}

const els = {
    ".form-error": new Element(),
    "button[type=submit]": new Element(),
    "#file_upload": new Element(),
    "#pasted_rows": new Element({ id: "pasted_rows" }),
};
const buttons = {
    file: new Element({ "data-source": "file", "aria-pressed": "true" }),
    paste: new Element({ "data-source": "paste", "aria-pressed": "false" }),
};
const panels = {
    file: new Element({ "data-panel": "file" }),
    paste: new Element({ "data-panel": "paste" }),
};
panels.paste.hidden = true;
const form = new Element();
form.querySelector = (selector) => els[selector] || null;
form.querySelectorAll = (selector) => ({
    ".source-btn": [buttons.file, buttons.paste],
    ".source-panel": [panels.file, panels.paste],
})[selector] || [];

const html = new Element();
html.lang = "en";
const document = {
    documentElement: html,
    addEventListener() {},
    getElementById: () => null,
    querySelector: (selector) => (selector === ".upload-form" ? form : null),
    querySelectorAll: () => [],
};
const window = { location: { href: "/" }, addEventListener() {} };
let sent = null;
async function fetch(url, options) {
    sent = Object.fromEntries(options.body.entries());
    return { ok: true, status: 200, json: async () => ({ job_id: "abc" }) };
}

const context = vm.createContext({
    document, window, fetch, console, FormData,
    localStorage: { getItem: () => null, setItem() {} },
});
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), context);
vm.runInContext("setupBulkForm()", context);

const steps = JSON.parse(process.argv[3]);
const seen = [];
(async () => {
    for (const step of steps) {
        sent = null;
        if (step.do === "click") {
            buttons[step.source].fire("click");
        } else if (step.do === "paste") {
            els["#pasted_rows"].value = step.text;
        } else if (step.do === "submit") {
            form.fire("submit", { preventDefault() {} });
            await new Promise((resolve) => setImmediate(resolve));
        } else if (step.do === "lang") {
            html.lang = step.lang;
            vm.runInContext("applyLang()", context);
            const error = els[".form-error"];
            error.textContent = error.getAttribute("data-" + step.lang) || "";
        }
        seen.push({
            error: els[".form-error"].textContent,
            pressed: buttons.paste.getAttribute("aria-pressed"),
            pasteShown: !panels.paste.hidden,
            fileShown: !panels.file.hidden,
            focused: focused ? focused.getAttribute("id") : null,
            sent,
        });
    }
    process.stdout.write(JSON.stringify(seen));
})().catch((error) => { console.error(error); process.exit(1); });
"""

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node is not on PATH")


def _drive(tmp_path, lang, steps):
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness), str(_APP_JS), lang, json.dumps(steps)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(result.stdout)


def _drive_bulk(tmp_path, steps):
    harness = tmp_path / "bulk_harness.js"
    harness.write_text(_BULK_HARNESS, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness), str(_APP_JS), json.dumps(steps)],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return json.loads(result.stdout)


@needs_node
def test_a_form_error_follows_a_language_switch(tmp_path):
    seen = _drive(tmp_path, "en", [
        {"do": "formError", "key": "needNameOrIsin"},
        {"do": "lang", "lang": "cs"},
        {"do": "lang", "lang": "en"},
    ])
    assert [step["error"] for step in seen] == [
        "Please enter an entity name or an ISIN",
        "Zadejte název subjektu nebo ISIN",
        "Please enter an entity name or an ISIN",
    ]


@needs_node
def test_a_server_error_follows_a_language_switch(tmp_path):
    seen = _drive(tmp_path, "cs", [
        {"do": "submit", "reply": {"status": 400, "body": {
            "error": "The file is empty.", "error_cs": "Soubor je prázdný.",
        }}},
        {"do": "lang", "lang": "en"},
    ])
    assert [step["error"] for step in seen] == [
        "Soubor je prázdný.", "The file is empty.",
    ]
    # A refused search can be sent again.
    assert seen[0]["disabled"] is False


@needs_node
def test_the_results_card_error_follows_a_language_switch(tmp_path):
    seen = _drive(tmp_path, "en", [
        {"do": "cardError", "data": {
            "error": "GLEIF is down.", "error_cs": "GLEIF je nedostupný.",
        }},
        {"do": "lang", "lang": "cs"},
        {"do": "cardError", "data": None},
        {"do": "lang", "lang": "en"},
    ])
    assert [step["state"] for step in seen] == [
        "GLEIF is down.", "GLEIF je nedostupný.",
        "Vyhledávání na serveru selhalo.", "The search failed on the server.",
    ]


@needs_node
def test_search_stays_disabled_once_the_results_page_is_on_its_way(
    tmp_path,
):
    seen = _drive(tmp_path, "en", [
        {"do": "submit", "reply": {"status": 200, "body": {
            "job_id": "abc", "total": 1,
        }}},
    ])
    assert seen[0]["href"] == "/results?job=abc"
    assert seen[0]["disabled"] is True


@needs_node
def test_search_is_enabled_again_when_the_server_is_unreachable(tmp_path):
    seen = _drive(tmp_path, "cs", [{"do": "submit", "reply": "offline"}])
    assert seen[0]["disabled"] is False
    assert seen[0]["error"] == "Server není dostupný. Zkuste to prosím znovu."


@needs_node
def test_an_explicit_theme_choice_beats_a_later_os_change(tmp_path):
    seen = _drive(tmp_path, "en", [
        {"do": "setupTheme"},
        {"do": "osScheme", "dark": True},
        {"do": "clickTheme"},
        {"do": "osScheme", "dark": True},
        {"do": "osScheme", "dark": False},
    ])
    assert [step["theme"] for step in seen] == [
        "light", "dark", "light", "light", "light",
    ]


@needs_node
@pytest.mark.parametrize("service, countdown", [
    ("GLEIF", "GLEIF is limiting requests. Continuing in 3 s…"),
    ("OpenFIGI", "OpenFIGI is busy. Continuing in 3 s…"),
])
def test_the_pause_countdown_names_the_service(tmp_path, service, countdown):
    seen = _drive(tmp_path, "en", [{"do": "throttle", "service": service}])
    assert seen[0]["countdown"] == countdown
    # After the wait the running state is back.
    assert seen[1]["state"] == "Searching…"



@needs_node
def test_the_paste_switch_shows_the_box_and_sends_only_the_rows(tmp_path):
    seen = _drive_bulk(tmp_path, [
        {"do": "submit"},
        {"do": "click", "source": "paste"},
        {"do": "submit"},
        {"do": "lang", "lang": "cs"},
        {"do": "paste", "text": "Alfa a.s.\t\tCZ\r\n"},
        {"do": "submit"},
        {"do": "click", "source": "file"},
    ])
    # Nothing chosen yet: the file panel asks for a file.
    assert seen[0]["error"] == "Please select a file first"
    assert seen[0]["sent"] is None
    # The switch shows the paste box, ready for Ctrl+V.
    assert seen[1]["pressed"] == "true"
    assert seen[1]["pasteShown"] and not seen[1]["fileShown"]
    assert seen[1]["focused"] == "pasted_rows"
    assert seen[1]["error"] == ""
    # An empty box is not sent, and its message follows a CZ switch.
    assert seen[2]["error"] == "Please paste the rows first"
    assert seen[2]["sent"] is None
    assert seen[3]["error"] == "Nejprve vložte řádky"
    # Pasted rows go out as they are, without a file.
    assert seen[5]["sent"] == {"mode": "paste", "rows": "Alfa a.s.\t\tCZ\r\n"}
    assert seen[5]["error"] == ""
    assert seen[6]["fileShown"] and not seen[6]["pasteShown"]
    assert seen[6]["pressed"] == "false"

