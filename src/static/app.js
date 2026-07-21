"use strict";

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

// ---- Detailed-results summary card ----
// The search runs on the detail page: the card streams live progress, then
// the page reloads as /results?job=... so the tables render underneath.

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

// Move the card into its "working" state: spinner spinning, counters reset.
function startResults(els) {
    if (!els.card) {
        return;
    }
    els.card.hidden = false;
    els.card.classList.remove("is-idle", "has-error", "no-matches");
    els.card.classList.add("is-running");
    els.card.setAttribute("aria-busy", "true");
    if (els.state) {
        els.state.textContent = "Searching…";
    }
    if (els.searched) {
        els.searched.textContent = "0 / 0";
    }
    if (els.matched) {
        els.matched.textContent = "0";
    }
    if (els.validate) {
        els.validate.textContent = "0";
    }
    if (els.unmatched) {
        els.unmatched.textContent = "0";
    }
}

// Settle the card in place. Only reached as a fallback: normally a completed
// search carries a job id and we reload the page instead (see handleEvent).
function finishResults(els) {
    if (!els.card) {
        return;
    }
    els.card.classList.remove("is-running");
    els.card.setAttribute("aria-busy", "false");
    if (els.state) {
        els.state.textContent = "Search complete";
    }
}

// Show an error (e.g. GLEIF down, or the request failed) in the card.
function showResultsError(els, message) {
    if (!els.card) {
        return;
    }
    els.card.hidden = false;
    els.card.classList.remove("is-running", "no-matches", "is-idle");
    els.card.classList.add("has-error");
    els.card.setAttribute("aria-busy", "false");
    if (els.state) {
        els.state.textContent = message;
    }
}

// Apply one progress event from the stream to the card.
function handleEvent(els, event) {
    if (event.error) {
        showResultsError(els, event.error);
        return;
    }
    if (els.searched) {
        els.searched.textContent = `${event.searched} / ${event.total}`;
    }
    if (els.matched && event.matched != null) {
        els.matched.textContent = event.matched;
    }
    if (els.validate && event.need_validation != null) {
        els.validate.textContent = event.need_validation;
    }
    if (els.unmatched && event.unmatched != null) {
        els.unmatched.textContent = event.unmatched;
    }
    if (event.done) {
        // Reload the detail page in its final, server-rendered form so the
        // matched / no-match / validation tables appear under the card.
        if (event.job_id) {
            window.location.replace(
                "/results?job=" + encodeURIComponent(event.job_id),
            );
            return;
        }
        // No job id came back (should not happen): settle the card in place.
        finishResults(els);
    }
}

// Rebuild the FormData for the search carried over from the search page. A
// single lookup carries its text fields; a bulk lookup carries the file as a
// data: URL, which fetch() decodes back into a Blob here.
async function buildSearchFormData(pending) {
    const formData = new FormData();
    formData.set("mode", pending.mode);

    if (pending.mode === "bulk") {
        const response = await fetch(pending.dataUrl);
        const blob = await response.blob();
        formData.set(
            "file_upload", new File([blob], pending.filename || "upload"),
        );
        return formData;
    }

    for (const [key, value] of Object.entries(pending.fields || {})) {
        if (value) {
            formData.set(key, value);
        }
    }
    return formData;
}

// Run the carried-over search, streaming NDJSON progress into the card.
async function runPendingSearch(pending) {
    const els = getResultEls();
    startResults(els);

    let formData;
    try {
        formData = await buildSearchFormData(pending);
    } catch (error) {
        showResultsError(
            els, "Could not read the uploaded file. Please try again.",
        );
        return;
    }

    try {
        const response = await fetch("/api/search", {
            method: "POST",
            body: formData,
        });

        // fetch() only rejects on a network failure, not on a 4xx/5xx status,
        // so check the status ourselves to report a server error as one.
        if (!response.ok) {
            // 413 is the size cap (MAX_CONTENT_LENGTH) rejecting a large upload.
            showResultsError(
                els,
                response.status === 413
                    ? "That file is too large. The maximum upload size is 5 MB."
                    : "The search failed on the server. Please try again.",
            );
            return;
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        for (;;) {
            const { value, done } = await reader.read();
            if (done) {
                break;
            }
            buffer += decoder.decode(value, { stream: true });

            let newline;
            while ((newline = buffer.indexOf("\n")) >= 0) {
                const line = buffer.slice(0, newline).trim();
                buffer = buffer.slice(newline + 1);
                if (line) {
                    handleEvent(els, JSON.parse(line));
                }
            }
        }

        const rest = buffer.trim();
        if (rest) {
            handleEvent(els, JSON.parse(rest));
        }
    } catch (error) {
        showResultsError(els, "Could not reach the server. Please try again.");
    }
}

// On the detail page, either run the search handed over from the search page
// or, if there is nothing to show, reveal the empty-state note. No-ops on any
// page without the card (e.g. the search page itself).
function initResultsPage() {
    const card = document.getElementById("results-card");
    if (!card) {
        return;
    }
    // A stored job is already rendered server-side; nothing to stream.
    if (card.dataset.hasJob === "true") {
        return;
    }

    // Read the pending search once, so a reload does not re-run it.
    const raw = sessionStorage.getItem("pendingSearch");
    if (raw) {
        sessionStorage.removeItem("pendingSearch");
    }
    let pending = null;
    if (raw) {
        try {
            pending = JSON.parse(raw);
        } catch (error) {
            pending = null;
        }
    }

    if (!pending) {
        // Reached /results with nothing to search (e.g. an expired link).
        card.hidden = true;
        const note = document.getElementById("empty-note");
        if (note) {
            note.hidden = false;
        }
        return;
    }

    runPendingSearch(pending);
}

// ---- Search-page forms ----

// Show/clear the red validation message that sits next to a Search button.
function showFormError(form, message) {
    const errorEl = form.querySelector(".form-error");
    if (errorEl) {
        errorEl.textContent = message;
    }
}

function clearFormError(form) {
    showFormError(form, "");
}

// Hand a search off to the detail page: stash the input in sessionStorage
// (which survives the navigation) and go to /results, where the streaming
// search actually runs.
function startSearch(pending) {
    sessionStorage.setItem("pendingSearch", JSON.stringify(pending));
    window.location.href = "/results";
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
            showFormError(form, "Please enter an entity name or an ISIN");
            if (nameInput) {
                nameInput.focus();
            }
            return;
        }
        clearFormError(form);
        const fields = Object.fromEntries(new FormData(form));
        startSearch({ mode: "single", fields });
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

    // Only .xlsx and .csv are accepted. The input's "accept" attribute is just
    // a picker hint, and drag-and-drop ignores it, so check the name ourselves.
    function isAllowedFile(file) {
        return /\.(xlsx|csv)$/i.test(file.name);
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
            empty.textContent = "No files selected yet";
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
        remove.setAttribute("aria-label", "Remove file");
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
                showFormError(form, "Please select a .xlsx or .csv file");
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
                showFormError(form, "Please select a .xlsx or .csv file");
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

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const file = fileInput && fileInput.files[0];
        if (!file) {
            showFormError(form, "Please select a file first");
            return;
        }
        clearFormError(form);

        // Ask the server to check the file before we leave this page, so a
        // problem (wrong type, empty, no usable rows, too many rows) shows
        // here next to Search rather than on the results page. Counting the
        // rows means reading the file, and an .xlsx cannot be read in the
        // browser without an extra library, so the server (which already
        // has the parser) does the check. Disable Search meanwhile so a
        // second click cannot fire a duplicate request.
        const submitBtn = form.querySelector("button[type=submit]");
        if (submitBtn) {
            submitBtn.disabled = true;
        }
        try {
            const check = new FormData();
            check.set("file_upload", file);
            const response = await fetch("/api/validate-upload", {
                method: "POST",
                body: check,
            });
            if (response.status === 413) {
                showFormError(
                    form,
                    "That file is too large. The maximum upload size is 5 MB.",
                );
                return;
            }
            if (!response.ok) {
                showFormError(form, "Could not check the file. Please try again.");
                return;
            }
            const result = await response.json();
            if (!result.ok) {
                showFormError(form, result.error);
                return;
            }
        } catch (error) {
            showFormError(form, "Could not reach the server. Please try again.");
            return;
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
            }
        }

        // The file is valid: read it into a data: URL so it survives the
        // navigation to the detail page (a File object cannot be stored
        // directly).
        const reader = new FileReader();
        reader.onload = () => {
            try {
                startSearch({
                    mode: "bulk",
                    filename: file.name,
                    dataUrl: reader.result,
                });
            } catch (error) {
                showFormError(
                    form, "That file is too large to open in the browser.",
                );
            }
        };
        reader.onerror = () => {
            showFormError(form, "Could not read the file. Please try again.");
        };
        reader.readAsDataURL(file);
    });
}

// ---- Detailed-results validation stepper (the /results page) ----
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
        if (positionEl) {
            positionEl.textContent =
                `Record ${current + 1} of ${records.length}`;
        }
        if (prevBtn) {
            prevBtn.disabled = current === 0;
        }
        if (nextBtn) {
            nextBtn.disabled = current === records.length - 1;
        }
    }

    // Save a decision for a record, then advance to the next one.
    async function saveDecision(record, choice) {
        const index = Number(record.dataset.index);
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
            if (current < records.length - 1) {
                show(current + 1, "next");
            }
        } catch (error) {
            // Network hiccup: leave the record unchanged so it can retry.
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
}

document.addEventListener("DOMContentLoaded", () => {
    setupDialog("help-btn", "help-dialog");
    setupDialog("report-btn", "report-dialog");
    setupSingleForm();
    setupBulkForm();
    initResultsPage();
    setupValidation();
});
