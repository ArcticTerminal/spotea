// Shown once, right after registering, to a profile with neither an
// interest nor a followed channel — everything Explore's "For you" shelves
// and the library need to have anything worth showing (see needs_onboarding
// in routers/pages.py). Three steps: pick what this profile is for (music
// or podcasts — which decides the chip set and header question on the next
// step), pick a few genres/topics (saved through the same PUT /settings
// path Settings' own interests editor uses), then follow a few channels —
// suggested from those picks, searched for directly, or bulk-imported —
// all through endpoints and components that already exist elsewhere in the
// app rather than anything built new for this. Both kinds share one
// suggestion source: genre_artists caches music genres and podcast
// categories in the same table (hand-curated seeds — see
// scripts/seed_music_artists.py and scripts/seed_podcast_channels.py), so
// the channels step doesn't care which kind was picked.
//
// No "seen it" flag: closing the overlay without adding either an interest
// or a channel just means needs_onboarding is still true next login, and it
// opens again. That's a deliberate nudge, not a bug.

import { api, debounce, setupOverlay } from "../core.js";
import { renderChannelResults } from "./explore.js";
import { followChannel } from "./remote.js";
import { saveInterests } from "./settings.js";

const STEP_IDS = ["onboarding-step-kind", "onboarding-step-genres", "onboarding-step-channels"];

// The header's question, per step — the chip step's phrasing depends on
// which kind was picked, so the title can't live statically in the template.
const TITLES = {
  kind: "What do you want to listen to?",
  music: "What kind of music are you into?",
  podcast: "What are you curious about?",
  channels: "Follow a few channels",
};

function showStep(id) {
  for (const stepId of STEP_IDS) {
    const el = document.getElementById(stepId);
    if (el) el.hidden = stepId !== id;
  }
}

function setTitle(text) {
  const title = document.getElementById("onboarding-title");
  if (title) title.textContent = text;
}

// `onPick(kind)` fires with "music" or "podcast".
function setupKindStep(onPick) {
  const step = document.getElementById("onboarding-step-kind");
  if (!step) return;
  for (const card of step.querySelectorAll(".kind-card")) {
    card.addEventListener("click", () => onPick(card.dataset.kind));
  }
}

// `onDone(genres)` fires once interests are settled — an empty array for
// Skip, whatever was picked/typed for Next (already saved by then). The
// returned handle's `applyKind` swaps which chip set is visible before the
// step is shown.
function setupGenreStep(onDone, onBack) {
  const step = document.getElementById("onboarding-step-genres");
  const form = document.getElementById("onboarding-genre-form");
  const input = document.getElementById("onboarding-genre-input");
  const nextBtn = document.getElementById("onboarding-genres-next");
  const skipBtn = document.getElementById("onboarding-genres-skip");
  const backBtn = document.getElementById("onboarding-genres-back");
  if (!step || !form || !input || !nextBtn || !skipBtn || !backBtn) return null;

  // lowercase -> the exact spelling that gets saved/searched with, so typing
  // a duplicate of an already-selected chip (any casing) collapses into the
  // same entry instead of the free-text form producing a second one.
  const selected = new Map();

  // Both kinds' chips — only one grid is ever visible, so a click can only
  // come from the active kind's set.
  const chips = [...step.querySelectorAll(".genre-chip")];

  // Free-typed text matching a predefined chip (any casing) highlights that
  // chip instead of just adding a second, invisible copy of it.
  function addGenre(genre) {
    const key = genre.toLowerCase();
    if (selected.has(key)) return;
    selected.set(key, genre);
    nextBtn.disabled = false;
    chips.find((chip) => chip.dataset.genre.toLowerCase() === key)?.classList.add("is-selected");
  }

  for (const chip of chips) {
    chip.addEventListener("click", () => {
      const genre = chip.dataset.genre;
      const key = genre.toLowerCase();
      if (selected.has(key)) {
        selected.delete(key);
        chip.classList.remove("is-selected");
        nextBtn.disabled = selected.size === 0;
      } else {
        addGenre(genre);
      }
    });
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value.trim();
    input.value = "";
    if (value) addGenre(value);
  });

  skipBtn.addEventListener("click", () => onDone([]));
  backBtn.addEventListener("click", onBack);

  nextBtn.addEventListener("click", async () => {
    const genres = [...selected.values()];
    nextBtn.disabled = true;
    nextBtn.textContent = "Saving…";
    await saveInterests(genres, "Could not save your interests");
    nextBtn.disabled = false;
    nextBtn.textContent = "Next";
    onDone(genres);
  });

  return {
    // Also clears any previous picks — a selection carried across a kind
    // switch would mean invisible chips from the other taxonomy silently
    // riding along into "Next".
    applyKind(kind) {
      for (const grid of step.querySelectorAll("[data-kind-grid]")) {
        grid.hidden = grid.dataset.kindGrid !== kind;
      }
      for (const copy of step.querySelectorAll("[data-kind-copy]")) {
        copy.hidden = copy.dataset.kindCopy !== kind;
      }
      selected.clear();
      for (const chip of chips) chip.classList.remove("is-selected");
      nextBtn.disabled = true;
      input.value = "";
    },
  };
}

// Returns a `loadSuggestions(genres)` function the genre step calls once it
// hands off — channel suggestions can't be fetched until interests are
// actually saved, so this step doesn't fetch anything on its own.
function setupChannelStep() {
  const container = document.getElementById("onboarding-step-channels");
  const searchInput = document.getElementById("onboarding-search-input");
  const searchResults = document.getElementById("onboarding-search-results");
  const suggested = document.getElementById("onboarding-suggested-channels");
  if (!container || !searchInput || !searchResults || !suggested) return null;

  // "Add" is the only action wired here — unlike Explore's own channel
  // search, a row click doesn't open the channel preview: that's a
  // full-panel navigation, and doing it out from under this modal would
  // just strand the wizard half-finished behind it. `announce: false` for
  // the same reason on the "Add" click itself — followChannel's default
  // behavior is to navigate to the newly-followed channel once its backfill
  // finishes (see home/detail.js's CHANNEL_FOLLOWED listener), which is
  // right for Explore's search but would silently yank someone out of the
  // wizard mid-flow here, right as they're trying to add a few more.
  container.addEventListener("click", (event) => {
    const btn = event.target.closest(".btn-add-channel");
    if (btn) followChannel(btn.dataset.channelUrl, btn, { announce: false });
  });

  const runSearch = debounce(async (query) => {
    if (!query) {
      searchResults.innerHTML = "";
      return;
    }
    const { ok, data } = await api(`/feeds/search?q=${encodeURIComponent(query)}`);
    if (ok) renderChannelResults(data, "onboarding-search-results");
  }, 400);
  searchInput.addEventListener("input", () => runSearch(searchInput.value.trim()));

  return async function loadSuggestions(genres) {
    if (!genres.length) {
      suggested.innerHTML = "";
      return;
    }
    suggested.innerHTML = `<li class="search-loading"><span class="spinner"></span>Finding channels…</li>`;
    // Real artists/shows, not a generic "<genre> music" search — see
    // services/genre_artists.py and scripts/seed_podcast_channels.py. An
    // entry with nothing seeded for it (a free-typed one, or one the caches
    // just don't cover) comes back an empty list rather than an error; the
    // search box below still covers it.
    const { ok, data } = await api(
      `/onboarding/suggested-channels?genres=${encodeURIComponent(genres.join(","))}`
    );
    suggested.innerHTML = "";
    if (ok) renderChannelResults(data, "onboarding-suggested-channels");
  };
}

export function setupOnboarding() {
  const overlay = document.getElementById("onboarding-overlay");
  if (!overlay) return;

  const handle = setupOverlay("onboarding-overlay", "onboarding-close", []);
  const loadSuggestions = setupChannelStep();
  const genreStep = setupGenreStep(
    (genres) => {
      setTitle(TITLES.channels);
      showStep("onboarding-step-channels");
      loadSuggestions?.(genres);
    },
    () => {
      setTitle(TITLES.kind);
      showStep("onboarding-step-kind");
    }
  );
  setupKindStep((kind) => {
    genreStep?.applyKind(kind);
    setTitle(TITLES[kind] ?? TITLES.music);
    showStep("onboarding-step-genres");
  });
  document.getElementById("onboarding-finish")?.addEventListener("click", () => handle?.close());

  if (overlay.dataset.needsOnboarding === "true") handle?.open();
}
