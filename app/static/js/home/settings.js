// The Settings tab's controls, plus the Downloads modal they open.

import { api, confirmDialog, escapeHtml, setupOverlay, showToast } from "../core.js";
import { refreshDownloadsBody, refreshFragments } from "../fragments.js";
import { reloadRecommendations } from "./explore.js";

export function setupDownloadsOverlay() {
  setupOverlay("downloads-overlay", "downloads-close", ["open-downloads"]);
  // The modal already has *something* to show the instant it opens — its
  // list was server-rendered at page load — so this doesn't block that open
  // on a round trip, just freshens it right after in case anything changed
  // since (another tab, the background refresh finishing a download).
  document.getElementById("open-downloads")?.addEventListener("click", () => {
    refreshDownloadsBody();
  });
}

/** Confirm, then call, then re-render whatever it changed. `alsoDownloads`
    is for the two actions below that run *from inside* the open Downloads
    modal (clear all, remove one) — refreshFragments() alone no longer
    touches that modal's list (see fragments.js), so without this a user's
    own action wouldn't appear to do anything until they closed and reopened
    it. */
async function confirmedAction(message, confirmLabel, url, { method, errorMessage }, { alsoDownloads = false } = {}) {
  if (!(await confirmDialog(message, confirmLabel))) return;
  const { ok } = await api(url, { method, errorMessage });
  if (!ok) return;
  // A fragment refresh rather than a reload: these run from inside the
  // Downloads modal, and reloading closed it out from under the user.
  refreshFragments();
  if (alsoDownloads) refreshDownloadsBody();
}

export function setupStorage() {
  document.getElementById("clear-recently-played")?.addEventListener("click", () =>
    confirmedAction(
      "Clear your recently played history? This only affects the Home shelf — nothing gets deleted.",
      "Clear",
      "/content/recently-played",
      { method: "DELETE", errorMessage: "Could not clear recently played" }
    )
  );

  // Delegated from #downloads-body, not #clear-storage/#storage-list directly:
  // refreshFragments() (see fragments.js) rewrites this whole region's
  // children — including #clear-storage and #storage-list themselves —
  // wholesale on every refresh (e.g. after any download, from player.js), so
  // listeners on those elements go stale as soon as one happens. #downloads-body
  // is the fragment's swap target, not one of the swapped nodes, so it's the
  // only element here guaranteed to still be in the DOM.
  document.getElementById("downloads-body")?.addEventListener("click", (event) => {
    if (event.target.closest("#clear-storage")) {
      confirmedAction(
        "Delete all downloaded audio? Your channels and saved items stay — you can download anything again by playing it.",
        "Clear all",
        "/storage",
        { method: "DELETE", errorMessage: "Could not clear downloads" },
        { alsoDownloads: true }
      );
      return;
    }

    const removeBtn = event.target.closest(".storage-remove");
    if (removeBtn) {
      confirmedAction(
        "Remove this download? You can get it back by playing it again.",
        "Remove",
        `/content/${removeBtn.dataset.contentId}`,
        { method: "DELETE", errorMessage: "Could not remove this download" },
        { alsoDownloads: true }
      );
    }
  });
}

// The interest tags Explore's recommendations are searched from. Kept here
// as one array and PUT whole on every change (see schemas.SettingsUpdate) —
// the server normalizes what it stores (trimming, deduping, capping), so the
// response, not this array, is what the chips are re-rendered from.
let interests = [];

function renderInterests() {
  const list = document.getElementById("interests-list");
  const empty = document.getElementById("interests-empty");
  if (!list) return;

  list.innerHTML = interests
    .map(
      (interest) => `
        <li class="interest-chip">
          <span>${escapeHtml(interest)}</span>
          <button type="button" class="interest-chip-remove" data-interest="${escapeHtml(interest)}" aria-label="Remove ${escapeHtml(interest)}">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-close" /></svg>
          </button>
        </li>
      `
    )
    .join("");
  if (empty) empty.hidden = interests.length > 0;
}

// Saves run one after another rather than overlapping. Every PUT carries the
// whole list, so two in flight at once can land in either order — adding two
// chips in quick succession left the server holding the shorter list, and the
// second response then re-rendered the chips to match it, silently undoing
// the add.
let pendingSave = Promise.resolve();

export function saveInterests(next, errorMessage) {
  const previous = interests;
  // Rendered before the request is even sent so adding a chip feels instant;
  // reverted below if the save doesn't land.
  interests = next;
  renderInterests();

  pendingSave = pendingSave.then(async () => {
    const { ok, data } = await api("/settings", {
      method: "PUT",
      body: { interests: next },
      errorMessage,
    });

    // Both branches below only act while this edit is still the current
    // state (each edit assigns a fresh array, so identity is the test). If
    // something has been added or removed since, that edit's own queued save
    // owns what the chips should say — adopting this response's list, or
    // rolling back to this edit's predecessor, would undo it.
    if (next !== interests) return;

    // On success: the server's normalized list, which can differ from what
    // was sent — an over-long or duplicate tag comes back cleaned up rather
    // than rejected.
    interests = ok ? data.interests : previous;
    renderInterests();
    // Explore's shelves were searched from the old list, and so was the
    // batch the server has cached — both are now answers to a question
    // nobody asked. Rebuilt right here, in the background, rather than
    // marked stale for whoever opens Explore next to wait on: the rebuild is
    // several live YouTube searches, and this is the moment it can happen
    // without anyone sitting in front of it. Not awaited — the chips above
    // are what this save owes the user.
    if (ok) reloadRecommendations();
  });
  // Returned so a caller that needs the save to have actually landed before
  // doing anything else (the onboarding wizard's step 2, which searches for
  // channel suggestions against these same interests) can await it — every
  // existing caller here fires it and moves on, so this is additive.
  return pendingSave;
}

export function setupInterests() {
  const list = document.getElementById("interests-list");
  const form = document.getElementById("interests-form");
  const input = document.getElementById("interests-input");
  if (!list || !form || !input) return;

  // A modal rather than an inline editor: a wrapping chip list plus its own
  // add form doesn't fit the label-left/control-right shape every other
  // Settings row has, and this way the row looks like its neighbours.
  setupOverlay("interests-overlay", "interests-close", ["open-interests"]);
  document.getElementById("open-interests")?.addEventListener("click", () => input.focus());

  // Server-rendered into the element's dataset rather than fetched on boot,
  // so the chips are already right on first paint (see index.html).
  try {
    interests = JSON.parse(list.dataset.interests || "[]");
  } catch {
    interests = [];
  }
  renderInterests();

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) return;
    if (interests.some((interest) => interest.toLowerCase() === value.toLowerCase())) {
      showToast("That's already in your interests");
      input.value = "";
      return;
    }
    input.value = "";
    saveInterests([...interests, value], "Could not save your interests");
  });

  list.addEventListener("click", (event) => {
    const button = event.target.closest(".interest-chip-remove");
    if (!button) return;
    const removed = button.dataset.interest;
    saveInterests(
      interests.filter((interest) => interest !== removed),
      "Could not remove that interest"
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
