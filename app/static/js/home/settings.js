// The Settings tab's controls, plus the Downloads modal they open.

import { api, confirmDialog, escapeHtml, setupOverlay } from "../core.js";

// The storage figures in Settings and the Downloads modal are computed at
// render time (see pages.py's `usage`), so a fresh download leaves both stuck
// at their pre-download numbers. Refetches and re-renders from scratch rather
// than patching byte counts in place — the list is short, and this stays
// correct even if items were added or removed some other way (another tab)
// since this page loaded. Silent on failure: it's a background refresh, and a
// stale number is better than a toast nobody asked for.
export async function refreshStorageUsage() {
  const { ok, data } = await api("/storage");
  if (!ok) return;

  const desc = document.getElementById("settings-storage-desc");
  if (desc) desc.textContent = `${data.total_formatted} across ${data.count} item${data.count === 1 ? "" : "s"}`;

  const total = document.getElementById("storage-total");
  if (total) total.textContent = data.total_formatted;
  const count = document.getElementById("storage-count");
  if (count) count.textContent = `across ${data.count} item${data.count === 1 ? "" : "s"}`;

  const actions = document.getElementById("storage-summary-actions");
  if (actions) actions.hidden = data.count === 0;

  const list = document.getElementById("storage-list");
  const empty = document.getElementById("storage-empty");
  if (list) {
    list.hidden = data.count === 0;
    list.innerHTML = data.items
      .map(
        (item) => `
      <li data-content-id="${item.id}">
        <div class="storage-item-info">
          <span class="storage-item-title">${escapeHtml(item.title)}</span>
          <span class="storage-item-meta">${escapeHtml(item.channel_title || "")} · ${item.size_formatted}</span>
        </div>
        <a class="storage-export" href="/content/${item.id}/stream?download=1" download aria-label="Export">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-download" /></svg>
        </a>
        <button type="button" class="storage-remove" data-content-id="${item.id}" aria-label="Remove download">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-trash" /></svg>
        </button>
      </li>`
      )
      .join("");
  }
  if (empty) empty.hidden = data.count !== 0;
}

export function setupDownloadsOverlay() {
  setupOverlay("downloads-overlay", "downloads-close", ["open-downloads"]);
}

/** Confirm, then call, then reload if it worked. */
async function confirmedAction(message, confirmLabel, url, { method, errorMessage }) {
  if (!(await confirmDialog(message, confirmLabel))) return;
  const { ok } = await api(url, { method, errorMessage });
  if (ok) window.location.reload();
}

export function setupStorage() {
  document.getElementById("clear-storage")?.addEventListener("click", () =>
    confirmedAction(
      "Delete all downloaded audio? Your channels and saved items stay — you can download anything again by playing it.",
      "Clear all",
      "/storage",
      { method: "DELETE", errorMessage: "Could not clear downloads" }
    )
  );

  document.getElementById("clear-recently-played")?.addEventListener("click", () =>
    confirmedAction(
      "Clear your recently played history? This only affects the Home shelf — nothing gets deleted.",
      "Clear",
      "/content/recently-played",
      { method: "DELETE", errorMessage: "Could not clear recently played" }
    )
  );

  // Delegated: refreshStorageUsage rewrites this list wholesale, so per-button
  // listeners wouldn't survive.
  document.getElementById("storage-list")?.addEventListener("click", (event) => {
    const btn = event.target.closest(".storage-remove");
    if (!btn) return;
    confirmedAction(
      "Remove this download? You can get it back by playing it again.",
      "Remove",
      `/content/${btn.dataset.contentId}`,
      { method: "DELETE", errorMessage: "Could not remove this download" }
    );
  });
}

export function setupSettings() {
  const qualitySelect = document.getElementById("audio-quality-select");
  qualitySelect?.addEventListener("change", () => {
    api("/settings", {
      method: "PUT",
      body: { audio_quality: qualitySelect.value },
      errorMessage: "Could not update audio quality",
    });
  });

  const refreshIntervalSelect = document.getElementById("refresh-interval-select");
  refreshIntervalSelect?.addEventListener("change", () => {
    api("/settings", {
      method: "PUT",
      body: { feed_refresh_interval_minutes: Number(refreshIntervalSelect.value) },
      errorMessage: "Could not update refresh interval",
    });
  });
}
