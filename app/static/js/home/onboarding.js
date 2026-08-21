// First run: pick a few genres, and Explore has something to build from.
//
// Deliberately not a wizard. There is no step count, no separate route and
// no state of its own — the genres go into the same free-text `interests`
// field Settings writes, through the same PUT /settings that Settings uses,
// and whether this panel shows at all is derived from the library rather
// than stored (see page_context.home_context's show_onboarding). Picking
// here and typing there are one edit, not two features.
//
// The wizard this replaced was deleted on purpose in PR #20, along with the
// genre_artists table it seeded from. Nothing here brings either back.

import { api, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { refreshRecommendations } from "./explore.js";
import { activate } from "./tabs.js";

function selectedGenres() {
  return [...document.querySelectorAll('.genre-chip[aria-pressed="true"]')].map(
    (chip) => chip.dataset.genre
  );
}

export function setupOnboarding() {
  // Delegated from #tab-home, which is not one of the regions refreshFragments
  // replaces — the panel itself is, so anything bound to it directly dies on
  // the first swap. Re-binding after each swap was the first attempt and was
  // worse: the swap doesn't always replace the node, so the same element ended
  // up with two listeners and every toggle immediately undid itself, which
  // looks exactly like a click that isn't registering at all.
  const home = document.getElementById("tab-home");
  if (!home) return;

  home.addEventListener("click", (event) => {
    const chip = event.target.closest(".genre-chip");
    if (chip) {
      const on = chip.getAttribute("aria-pressed") === "true";
      chip.setAttribute("aria-pressed", String(!on));
      chip.classList.toggle("is-on", !on);
      syncSaveButton();
      return;
    }
    if (event.target.closest("#onboarding-skip")) {
      // Not persisted: there is no "skipped" flag to set, and adding one
      // would mean a column this app has no migration path for. It hides for
      // now and is gone for good the moment anything is followed or any
      // interest is typed, which is every path out of an empty library.
      document.getElementById("onboarding")?.setAttribute("hidden", "");
      return;
    }
    if (event.target.closest("#onboarding-save")) save();
  });
}

function syncSaveButton() {
  const save = document.getElementById("onboarding-save");
  // Nothing selected is a skip, not a save — the button says Continue, and
  // continuing with an empty list would store nothing and look broken.
  if (save) save.disabled = selectedGenres().length === 0;
}

async function save() {
  const button = document.getElementById("onboarding-save");
  const chosen = selectedGenres();
  if (!chosen.length || button.disabled) return;
  button.disabled = true;

  const { ok } = await api("/settings", {
    method: "PUT",
    body: { interests: chosen },
    errorMessage: "Could not save those",
  });
  if (!ok) {
    button.disabled = false;
    return;
  }

  // Explore is the payoff — its Playlists shelf is built from exactly what
  // was just saved, and its cache invalidates itself when the interest list
  // changes (see services/recommendations.py), so this asks for a fresh batch
  // rather than showing the empty one it already had.
  showToast("Saved. Have a look at Explore.");
  activate("explore");
  refreshRecommendations();
  refreshFragments();
}
