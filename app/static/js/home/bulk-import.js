// Following many channels from a pasted list, in a modal that reports
// per-line results.
//
// Reload is an explicit "Refresh page" button, not something that fires on
// modal close — closing the modal (× / backdrop / Escape) is also how you'd
// dismiss it after a successful import, so tying a reload to that would fire
// it unexpectedly. This way the user reads the per-line results (including
// any failures) before anything refreshes out from under them.

import { api, escapeHtml, setupOverlay } from "../core.js";

const POLL_INTERVAL_MS = 1000;

/** Fired when an import run finishes. detail: { added } — how many channels
 *  it newly followed (duplicates and failures aren't counted). Only the
 *  onboarding wizard listens, so its "follow a few channels" gate counts an
 *  import the same as its own Add buttons; the same one-way arrangement
 *  home/remote.js uses for CHANNEL_FOLLOWED, and for the same reason —
 *  this module has no business importing that one. */
export const BULK_IMPORT_FINISHED = "spotea:bulkimportfinished";

/** Whether the onboarding wizard is the thing this modal was opened over. */
function openedFromOnboarding() {
  return document.getElementById("onboarding-overlay")?.hidden === false;
}

function statusMeta(status) {
  if (status === "added") return { cls: "is-added", icon: "✓" };
  if (status === "duplicate") return { cls: "is-duplicate", icon: "•" };
  return { cls: "is-error", icon: "✗" };
}

function renderResults(results) {
  const list = document.getElementById("bulk-import-results");
  if (!list) return;

  list.innerHTML = results
    .map((r) => {
      const { cls, icon } = statusMeta(r.status);
      const label = r.channel_title || r.url;
      let detail = "";
      if (r.status === "duplicate") detail = " — already following";
      else if (r.status === "error" && r.error) detail = ` — ${r.error}`;
      const full = `${label}${detail}`;
      return `
        <li>
          <span class="bulk-import-status ${cls}">${icon}</span>
          <span class="bulk-import-line" title="${escapeHtml(full)}">${escapeHtml(full)}</span>
        </li>
      `;
    })
    .join("");
}

export function setupBulkImportOverlay() {
  setupOverlay("bulk-import-overlay", "bulk-import-close", [
    "open-bulk-import",
    "onboarding-open-bulk-import",
  ]);
}

export function setupBulkImport() {
  const startBtn = document.getElementById("bulk-import-start");
  const againBtn = document.getElementById("bulk-import-again");
  const reloadBtn = document.getElementById("bulk-import-reload");
  const input = document.getElementById("bulk-import-input");
  const formSection = document.getElementById("bulk-import-form-section");
  const progressSection = document.getElementById("bulk-import-progress-section");
  const progressText = document.getElementById("bulk-import-progress-text");
  const resultsList = document.getElementById("bulk-import-results");
  if (!startBtn || !input) return;

  const resetForm = () => {
    input.value = "";
    formSection.hidden = false;
    progressSection.hidden = true;
    resultsList.innerHTML = "";
    progressText.textContent = "";
    startBtn.disabled = false;
    startBtn.textContent = "Import";
    againBtn.hidden = true;
    reloadBtn.hidden = true;
  };

  const readyToStart = () => {
    startBtn.disabled = false;
    startBtn.textContent = "Import";
  };

  againBtn.addEventListener("click", resetForm);
  reloadBtn.addEventListener("click", () => window.location.reload());

  startBtn.addEventListener("click", async () => {
    const urls = input.value.trim();
    if (!urls) return;

    startBtn.disabled = true;
    startBtn.textContent = "Starting…";

    const started = await api("/feeds/import", {
      method: "POST",
      body: { urls },
      errorMessage: "Could not start import",
    });
    if (!started.ok) {
      readyToStart();
      return;
    }

    const { job_id: jobId, total } = started.data;
    formSection.hidden = true;
    progressSection.hidden = false;
    progressText.textContent = `Resolving channels… 0/${total}`;

    // Large channels' backfills run inline, one at a time, inside the same
    // job (see services/bulk_import.py) — the counter can sit still for a
    // while on a channel with a long upload history. Polling just keeps
    // asking, same as a single add's backfill wait does.
    while (true) {
      const { ok, data } = await api(`/feeds/import/${jobId}/status`);
      if (!ok) {
        progressText.textContent = "Lost track of the import — check Followed channels.";
        break;
      }

      renderResults(data.results);

      if (data.done >= data.total) {
        const added = data.results.filter((r) => r.status === "added").length;
        const skipped = data.total - added;
        progressText.textContent = `Done — ${added} added${skipped ? `, ${skipped} skipped` : ""}.`;
        againBtn.hidden = false;
        // Reloading out of a wizard the user hasn't finished would drop them
        // straight past it (needs_onboarding is false the moment one channel
        // exists), so there the wizard counts the import and refreshes the
        // page's regions itself on the way out instead.
        reloadBtn.hidden = added === 0 || openedFromOnboarding();
        document.dispatchEvent(new CustomEvent(BULK_IMPORT_FINISHED, { detail: { added } }));
        break;
      }

      // Channels resolve in parallel first, then get created one at a time —
      // two distinct stages, so the counter doesn't sit at 0 for however long
      // that whole parallel batch takes.
      progressText.textContent =
        data.resolved < data.total
          ? `Resolving channels… ${data.resolved}/${data.total}`
          : `Importing… ${data.done}/${data.total}`;
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  });
}
