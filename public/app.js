"use strict";

// ---- Theme and language (ported from the Finance RB CZ gateway) ----
// The theme lives on <html data-theme> and the language on <html lang>; both
// are resolved before first paint by the inline script in base.html. This
// file only switches them and remembers an explicit choice.

const THEME_KEY = "leiTheme";
const LANG_KEY = "leiLang";

// Strings the script writes itself (server-rendered text carries its own
// data-cs / data-en attributes instead).
const STRINGS = {
    en: {
        needNameOrIsin: "Please enter an entity name or an ISIN",
        selectFile: "Please select a .xlsx, .csv, .tsv or .txt file",
        selectFileFirst: "Please select a file first",
        tooLarge: "That file is too large. The maximum upload size is 4 MB.",
        couldNotStart: "The search could not be started. Please try again.",
        unreachable: "Could not reach the server. Please try again.",
        unreachableResume: "Could not reach the server. Reload the page to resume.",
        serverFailed: "The search failed on the server.",
        reloadToResume: "Reload the page to resume.",
        throttled: (seconds) =>
            `GLEIF is limiting requests. Continuing in ${seconds} s…`,
        noFiles: "No files selected yet",
        removeFile: "Remove file",
        toDark: "Switch to dark mode",
        toLight: "Switch to light mode",
        record: (index, total) => `Record ${index} of ${total}`,
    },
    cs: {
        needNameOrIsin: "Zadejte název subjektu nebo ISIN",
        selectFile: "Vyberte soubor .xlsx, .csv, .tsv nebo .txt",
        selectFileFirst: "Nejprve vyberte soubor",
        tooLarge: "Soubor je příliš velký. Maximální velikost je 4 MB.",
        couldNotStart: "Vyhledávání se nepodařilo spustit. Zkuste to prosím znovu.",
        unreachable: "Server není dostupný. Zkuste to prosím znovu.",
        unreachableResume: "Server není dostupný. Obnovte stránku pro pokračování.",
        serverFailed: "Vyhledávání na serveru selhalo.",
        reloadToResume: "Obnovte stránku pro pokračování.",
        throttled: (seconds) =>
            `GLEIF omezuje počet dotazů. Pokračuji za ${seconds} s…`,
        noFiles: "Zatím není vybrán žádný soubor",
        removeFile: "Odebrat soubor",
        toDark: "Přepnout na tmavý režim",
        toLight: "Přepnout na světlý režim",
        record: (index, total) => `Záznam ${index} z ${total}`,
    },
};

// Callbacks that re-render script-written text after a language switch.
const langListeners = [];

function currentLang() {
    return document.documentElement.lang === "cs" ? "cs" : "en";
}

function t(key, ...args) {
    const value = STRINGS[currentLang()][key];
    return typeof value === "function" ? value(...args) : value;
}

// The server's JSON error replies carry the message in English ("error")
// and Czech ("error_cs"); return the one for the current language, if any.
function serverError(data) {
    if (!data) {
        return null;
    }
    return (currentLang() === "cs" && data.error_cs) || data.error || null;
}

function themeIsDark() {
    return document.documentElement.getAttribute("data-theme") === "dark";
}

function applyThemeLabel() {
    const button = document.getElementById("theme-toggle");
    if (!button) {
        return;
    }
    const text = t(themeIsDark() ? "toLight" : "toDark");
    button.setAttribute("title", text);
    button.setAttribute("aria-label", text);
}

function setTheme(next, remember) {
    document.documentElement.setAttribute("data-theme", next);
    if (remember) {
        try {
            localStorage.setItem(THEME_KEY, next);
        } catch (error) {
            // Storage blocked: the choice simply lasts for this page.
        }
    }
    applyThemeLabel();
}

// Swap every translated text node and attribute to the current language.
// Elements carry data-cs / data-en for their text, and data-cs-<attr> /
// data-en-<attr> for placeholder, title, and aria-label.
function applyLang() {
    const lang = currentLang();
    document.querySelectorAll("[data-cs]").forEach((el) => {
        const text = el.getAttribute("data-" + lang);
        if (text !== null) {
            el.textContent = text;
        }
    });
    ["placeholder", "title", "aria-label"].forEach((attr) => {
        document.querySelectorAll(`[data-cs-${attr}]`).forEach((el) => {
            const text = el.getAttribute(`data-${lang}-${attr}`);
            if (text !== null) {
                el.setAttribute(attr, text);
            }
        });
    });
    document.querySelectorAll(".lang-btn").forEach((button) => {
        const isActive = button.getAttribute("data-setlang") === lang;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
    applyThemeLabel();
    langListeners.forEach((listener) => listener());
}

function setLang(next) {
    document.documentElement.lang = next;
    try {
        localStorage.setItem(LANG_KEY, next);
    } catch (error) {
        // Storage blocked: the choice simply lasts for this page.
    }
    applyLang();
}

function setupThemeAndLang() {
    const themeButton = document.getElementById("theme-toggle");
    if (themeButton) {
        themeButton.addEventListener("click", () => {
            setTheme(themeIsDark() ? "light" : "dark", true);
        });
    }

    // Until the user makes an explicit choice, keep following the OS.
    let stored = null;
    try {
        stored = localStorage.getItem(THEME_KEY);
    } catch (error) {
        stored = null;
    }
    if (stored !== "light" && stored !== "dark" && window.matchMedia) {
        const query = window.matchMedia("(prefers-color-scheme: dark)");
        query.addEventListener("change", (event) => {
            setTheme(event.matches ? "dark" : "light", false);
        });
    }

    document.querySelectorAll(".lang-btn").forEach((button) => {
        button.addEventListener("click", () => {
            setLang(button.getAttribute("data-setlang") === "cs" ? "cs" : "en");
        });
    });

    const year = document.getElementById("year");
    if (year) {
        year.textContent = String(new Date().getFullYear());
    }

    applyLang();
}

// Wire the header buttons to their popups. Each <dialog> closes via its own
// form method="dialog" (the X button) or by clicking the backdrop.
function setupDialog(buttonId, dialogId) {
    const button = document.getElementById(buttonId);
    const dialog = document.getElementById(dialogId);
    if (!button || !dialog) {
        return;
    }

    button.addEventListener("click", () => dialog.showModal());

    // Close when the click lands on the backdrop (outside the dialog content).
    dialog.addEventListener("click", (event) => {
        if (event.target === dialog) {
            dialog.close();
        }
    });
}

// ---- Search-page forms ----

// Show/clear the validation message that sits next to a Search button.
function showFormError(form, message) {
    const errorEl = form.querySelector(".form-error");
    if (errorEl) {
        errorEl.textContent = message;
    }
}

function clearFormError(form) {
    showFormError(form, "");
}

// Create the search job on the server and go to its results page, where the
// lookup itself runs step by step (see runJob). Input the server rejects
// (400) shows its message next to the Search button instead. Search is
// disabled meanwhile so a second click cannot create a duplicate job.
async function submitSearch(form, formData) {
    const submitBtn = form.querySelector("button[type=submit]");
    if (submitBtn) {
        submitBtn.disabled = true;
    }
    try {
        const response = await fetch("/api/jobs", {
            method: "POST",
            body: formData,
        });
        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            data = null;
        }
        // 413 is a size limit refusing the request: the app's own reply
        // is JSON with the message to show, while Vercel's request body
        // limit (4.5 MB) answers before the app with plain text.
        if (response.status === 413) {
            showFormError(form, serverError(data) || t("tooLarge"));
            return;
        }
        if (!response.ok || !data || !data.job_id) {
            showFormError(form, serverError(data) || t("couldNotStart"));
            return;
        }
        window.location.href =
            "/results?job=" + encodeURIComponent(data.job_id);
    } catch (error) {
        showFormError(form, t("unreachable"));
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
        }
    }
}

// Single lookup: a name or an ISIN is required (either is enough). We validate
// in JS (the form is marked novalidate) so the message matches the bulk form's
// style and place.
function setupSingleForm() {
    const form = document.querySelector(".lookup-form");
    if (!form) {
        return;
    }

    const nameInput = form.querySelector("#entity_name");
    const isinInput = form.querySelector("#isin");

    // Clear the error as soon as either identifier has a value.
    [nameInput, isinInput].forEach((input) => {
        if (input) {
            input.addEventListener("input", () => {
                if ((nameInput && nameInput.value.trim())
                    || (isinInput && isinInput.value.trim())) {
                    clearFormError(form);
                }
            });
        }
    });

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const hasName = nameInput && nameInput.value.trim();
        const hasIsin = isinInput && isinInput.value.trim();
        if (!hasName && !hasIsin) {
            showFormError(form, t("needNameOrIsin"));
            if (nameInput) {
                nameInput.focus();
            }
            return;
        }
        clearFormError(form);
        const formData = new FormData(form);
        formData.set("mode", "single");
        submitSearch(form, formData);
    });
}

// Bulk lookup: a file must be selected. Reflect the chosen filename in the
// file-list box and block submission when nothing is picked.
function setupBulkForm() {
    const form = document.querySelector(".upload-form");
    if (!form) {
        return;
    }

    const fileInput = form.querySelector("#file_upload");
    const fileCount = form.querySelector(".file-count");
    const fileItems = form.querySelector(".file-list-items");
    const dropzone = form.querySelector(".dropzone");

    // Only .xlsx, .csv, .tsv and .txt are accepted. The input's "accept"
    // attribute is just a picker hint, and drag-and-drop ignores it, so check
    // the name ourselves.
    function isAllowedFile(file) {
        return /\.(xlsx|csv|tsv|txt)$/i.test(file.name);
    }

    // Draw the file-list box for the current selection: a "n/1" counter on the
    // left and, when a file is picked, its name with a remove (x) button.
    function renderFiles() {
        const file = fileInput && fileInput.files[0];

        if (fileCount) {
            fileCount.textContent = file ? "1/1" : "0/1";
        }
        if (!fileItems) {
            return;
        }

        fileItems.replaceChildren();

        if (!file) {
            const empty = document.createElement("span");
            empty.className = "file-list-empty";
            empty.setAttribute("data-cs", STRINGS.cs.noFiles);
            empty.setAttribute("data-en", STRINGS.en.noFiles);
            empty.textContent = t("noFiles");
            fileItems.appendChild(empty);
            return;
        }

        const item = document.createElement("span");
        item.className = "file-list-item";

        const name = document.createElement("span");
        name.className = "file-name";
        name.textContent = file.name;

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "file-remove";
        remove.setAttribute("aria-label", t("removeFile"));
        remove.setAttribute("data-cs-aria-label", STRINGS.cs.removeFile);
        remove.setAttribute("data-en-aria-label", STRINGS.en.removeFile);
        remove.textContent = "×";
        remove.addEventListener("click", () => {
            fileInput.value = "";
            renderFiles();
        });

        item.append(name, remove);
        fileItems.appendChild(item);
        clearFormError(form);
    }

    if (fileInput) {
        fileInput.addEventListener("change", () => {
            const file = fileInput.files[0];
            if (file && !isAllowedFile(file)) {
                fileInput.value = "";
                showFormError(form, t("selectFile"));
            }
            renderFiles();
        });
    }

    // Drag-and-drop onto the dropzone. dragover must preventDefault, or the
    // browser opens the dropped file as a page and "drop" never fires.
    if (dropzone && fileInput) {
        ["dragenter", "dragover"].forEach((name) => {
            dropzone.addEventListener(name, (event) => {
                event.preventDefault();
                dropzone.classList.add("is-dragover");
            });
        });
        ["dragleave", "dragend", "drop"].forEach((name) => {
            dropzone.addEventListener(name, () => {
                dropzone.classList.remove("is-dragover");
            });
        });
        dropzone.addEventListener("drop", (event) => {
            event.preventDefault();
            const file = event.dataTransfer.files[0];
            if (!file) {
                return;
            }
            if (!isAllowedFile(file)) {
                showFormError(form, t("selectFile"));
                return;
            }
            // Keep the one-file cap: hand only the first dropped file to the
            // input via a DataTransfer (the only way to set input.files).
            const data = new DataTransfer();
            data.items.add(file);
            fileInput.files = data.files;
            renderFiles();
        });
    }

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const file = fileInput && fileInput.files[0];
        if (!file) {
            showFormError(form, t("selectFileFirst"));
            return;
        }
        clearFormError(form);
        // The server parses the file when it creates the job, so a problem
        // (wrong type, empty, no usable rows, too many rows) comes straight
        // back as the 400 message shown next to Search.
        const formData = new FormData();
        formData.set("mode", "bulk");
        formData.set("file_upload", file);
        submitSearch(form, formData);
    });
}

// ---- Results summary card (the /results page) ----
// A job whose lookup is not finished renders the card in its running state;
// the page then drives the lookup step by step and reloads once it is done so
// the server renders the tables underneath.

// Handles to the summary card and its parts.
function getResultEls() {
    return {
        card: document.getElementById("results-card"),
        state: document.querySelector(".results-state-text"),
        searched: document.getElementById("metric-searched"),
        matched: document.getElementById("metric-matched"),
        validate: document.getElementById("metric-validate"),
        unmatched: document.getElementById("metric-unmatched"),
    };
}

// Apply one progress response to the card's counters.
function applyProgress(els, progress) {
    if (els.searched) {
        els.searched.textContent = `${progress.searched} / ${progress.total}`;
    }
    if (els.matched && progress.matched != null) {
        els.matched.textContent = progress.matched;
    }
    if (els.validate && progress.need_validation != null) {
        els.validate.textContent = progress.need_validation;
    }
    if (els.unmatched && progress.unmatched != null) {
        els.unmatched.textContent = progress.unmatched;
    }
}

// Show an error (e.g. GLEIF down, or the request failed) in the card. The
// job keeps its saved progress, so reloading the page resumes the lookup.
function showResultsError(els, message) {
    if (!els.card) {
        return;
    }
    els.card.classList.remove("is-running", "no-matches");
    els.card.classList.add("has-error");
    els.card.setAttribute("aria-busy", "false");
    if (els.state) {
        // Freeze the message: it must not be swapped by a language switch.
        els.state.removeAttribute("data-cs");
        els.state.removeAttribute("data-en");
        els.state.textContent = message;
    }
}

// Bounds, in seconds, of the pause after GLEIF rate-limited a /run call:
// the server suggests the wait, the page keeps it reasonable.
const THROTTLE_MIN_SECONDS = 2;
const THROTTLE_MAX_SECONDS = 30;

// Wait out GLEIF's rate limit before the next /run call, counting down in
// the card's state line. Both languages are kept on the element, so a
// language switch during the wait shows the countdown in the new one.
async function waitOutThrottle(els, retryAfter) {
    const seconds = Math.min(
        Math.max(Math.ceil(Number(retryAfter)) || 0, THROTTLE_MIN_SECONDS),
        THROTTLE_MAX_SECONDS,
    );
    const state = els.state;
    const saved = state && {
        en: state.getAttribute("data-en"),
        cs: state.getAttribute("data-cs"),
    };
    for (let left = seconds; left > 0; left -= 1) {
        if (state) {
            state.setAttribute("data-en", STRINGS.en.throttled(left));
            state.setAttribute("data-cs", STRINGS.cs.throttled(left));
            state.textContent = t("throttled", left);
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    if (state) {
        state.setAttribute("data-en", saved.en);
        state.setAttribute("data-cs", saved.cs);
        state.textContent = saved[currentLang()];
    }
}

// Drive a job to completion: each /run call looks up the next few entities
// and returns the counts so far. When done, reload the page in its final,
// server-rendered form so the tables appear under the card. A call GLEIF
// rate-limited says so ("throttled"); the page then pauses as suggested and
// carries on by itself.
async function runJob(jobId) {
    const els = getResultEls();
    const url = "/api/jobs/" + encodeURIComponent(jobId) + "/run";

    for (;;) {
        let response;
        try {
            response = await fetch(url, { method: "POST" });
        } catch (error) {
            showResultsError(els, t("unreachableResume"));
            return;
        }

        let data = null;
        try {
            data = await response.json();
        } catch (error) {
            data = null;
        }
        if (data && data.searched != null) {
            applyProgress(els, data);
        }

        // fetch() only rejects on a network failure, not on a 4xx/5xx status,
        // so check the status ourselves to report a server error as one.
        if (!response.ok || !data) {
            const reason = serverError(data) || t("serverFailed");
            showResultsError(els, reason + " " + t("reloadToResume"));
            return;
        }

        if (data.done) {
            window.location.replace(
                "/results?job=" + encodeURIComponent(jobId),
            );
            return;
        }

        if (data.throttled) {
            await waitOutThrottle(els, data.retry_after);
        }
    }
}

// On the results page, continue a job whose lookup is still running. No-ops
// on a finished job (its tables are already rendered) and on pages without
// the card (e.g. the search page itself).
function initResultsPage() {
    const card = document.getElementById("results-card");
    if (!card || card.dataset.running !== "true") {
        return;
    }
    runJob(card.dataset.job);
}

// ---- Validation stepper (the /results page) ----
// Review one near-miss record at a time: pick the correct candidate (or
// "None of these"), which is saved to the server so the tables and the
// downloads reflect it. Arrows move between records. No-ops on any page
// that has no stepper (e.g. the main search page).
function setupValidation() {
    const section = document.querySelector(".validation");
    if (!section) {
        return;
    }
    const records = Array.from(section.querySelectorAll(".validate-record"));
    if (records.length === 0) {
        return;
    }

    const jobId = section.dataset.job;
    const prevBtn = section.querySelector(".validation-prev");
    const nextBtn = section.querySelector(".validation-next");
    const positionEl = section.querySelector(".validation-position");
    const remainingEl = document.querySelector(".validate-remaining");
    let current = 0;

    // A record is "decided" once it carries a saved decision.
    function isDecided(record) {
        return Boolean(record.dataset.decision);
    }

    // Show how many records still need a decision in the zone heading.
    function updateCounter() {
        if (remainingEl) {
            remainingEl.textContent =
                records.filter((record) => !isDecided(record)).length;
        }
    }

    function updatePosition() {
        if (positionEl) {
            positionEl.textContent = t("record", current + 1, records.length);
        }
    }

    // Highlight a record's saved choice: the chosen candidate row, or
    // the "None of these" button. Also used to restore state on load.
    function paintDecision(record) {
        const status = record.dataset.decision;
        const chosen = record.dataset.chosen;
        record.querySelectorAll(".candidate-row").forEach((row) => {
            row.classList.toggle(
                "is-chosen",
                status === "confirmed" && row.dataset.lei === chosen,
            );
            // "None of these" chosen: grey out the match buttons.
            const confirmBtn = row.querySelector(".candidate-confirm");
            if (confirmBtn) {
                confirmBtn.classList.toggle("is-muted", status === "none");
            }
        });
        const noneBtn = record.querySelector(".validate-none");
        if (noneBtn) {
            noneBtn.classList.toggle("is-chosen", status === "none");
        }
    }

    // Show only the record at `index`, sliding in from `direction`
    // ("next", "prev", or null for no animation).
    function show(index, direction) {
        current = Math.max(0, Math.min(index, records.length - 1));
        records.forEach((record, i) => {
            record.classList.toggle("is-active", i === current);
            record.classList.remove("slide-left", "slide-right");
        });
        const active = records[current];
        if (direction) {
            void active.offsetWidth; // restart the slide animation
            active.classList.add(
                direction === "prev" ? "slide-left" : "slide-right",
            );
        }
        updatePosition();
        if (prevBtn) {
            prevBtn.disabled = current === 0;
        }
        if (nextBtn) {
            nextBtn.disabled = current === records.length - 1;
        }
    }

    // Every record's decision buttons, disabled while a save is pending
    // so a second choice cannot race the first one.
    const decisionBtns = Array.from(
        section.querySelectorAll(".candidate-confirm, .validate-none"),
    );

    // Save a decision for a record, then advance to the next one.
    async function saveDecision(record, choice) {
        const index = Number(record.dataset.index);
        decisionBtns.forEach((btn) => {
            btn.disabled = true;
        });
        try {
            const response = await fetch("/api/decision", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ job_id: jobId, index, choice }),
            });
            if (!response.ok) {
                return;
            }
            const data = await response.json();
            record.dataset.decision = data.decision.status;
            record.dataset.chosen =
                data.decision.status === "confirmed" ? data.decision.lei : "";
            paintDecision(record);
            updateCounter();
            // Advance only from the saved record: if the arrows moved
            // on while the save was pending, stay where the user went.
            if (records[current] === record && current < records.length - 1) {
                show(current + 1, "next");
            }
        } catch (error) {
            // Network hiccup: leave the record unchanged so it can retry.
        } finally {
            decisionBtns.forEach((btn) => {
                btn.disabled = false;
            });
        }
    }

    // Wire each record's expand arrows and decision buttons.
    records.forEach((record) => {
        record.querySelectorAll(".candidate-row").forEach((row) => {
            const expandBtn = row.querySelector(".candidate-expand");
            const detail = row.nextElementSibling; // its .candidate-detail
            if (expandBtn && detail) {
                expandBtn.addEventListener("click", () => {
                    const opening = detail.hidden;
                    detail.hidden = !opening;
                    expandBtn.setAttribute("aria-expanded", String(opening));
                });
            }
            const confirmBtn = row.querySelector(".candidate-confirm");
            if (confirmBtn) {
                confirmBtn.addEventListener("click", () => {
                    saveDecision(record, row.dataset.lei);
                });
            }
        });
        const noneBtn = record.querySelector(".validate-none");
        if (noneBtn) {
            noneBtn.addEventListener("click", () => {
                saveDecision(record, "none");
            });
        }
        paintDecision(record); // restore any saved decision on load
    });

    if (prevBtn) {
        prevBtn.addEventListener("click", () => show(current - 1, "prev"));
    }
    if (nextBtn) {
        nextBtn.addEventListener("click", () => show(current + 1, "next"));
    }

    // Start on the first undecided record, where the work is.
    const firstUndecided = records.findIndex((record) => !isDecided(record));
    updateCounter();
    show(firstUndecided === -1 ? 0 : firstUndecided, null);
    langListeners.push(updatePosition);
}

document.addEventListener("DOMContentLoaded", () => {
    setupDialog("help-btn", "help-dialog");
    setupDialog("report-btn", "report-dialog");
    setupSingleForm();
    setupBulkForm();
    initResultsPage();
    setupValidation();
    // Last: translates everything the setups above rendered.
    setupThemeAndLang();
});
