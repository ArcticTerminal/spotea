// The Settings tab's controls, plus the Downloads modal they open.

import { api, confirmDialog, setupOverlay } from "../core.js";
import { refreshFragments } from "../fragments.js";

export function setupDownloadsOverlay() {
  setupOverlay("downloads-overlay", "downloads-close", ["open-downloads"]);
}

/** Confirm, then call, then re-render whatever it changed. */
async function confirmedAction(message, confirmLabel, url, { method, errorMessage }) {
  if (!(await confirmDialog(message, confirmLabel))) return;
  const { ok } = await api(url, { method, errorMessage });
  // A fragment refresh rather than a reload: these run from inside the
  // Downloads modal, and reloading closed it out from under the user.
  if (ok) refreshFragments();
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
