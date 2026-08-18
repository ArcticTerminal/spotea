// Shown right after registering, to a profile with neither an interest nor a
// followed channel — everything Explore's "For you" shelves and the library
// need to have anything worth showing (see needs_onboarding in
// routers/pages.py). Three steps: pick what this profile is for (music or
// podcasts — which decides the chip set and header question on the next
// step), pick a few genres/topics (saved through the same PUT /settings path
// Settings' own interests editor uses), then follow a few channels —
// suggested from those picks, searched for directly, or bulk-imported — all
// through endpoints and components that already exist elsewhere in the app
// rather than anything built new for this. Both kinds share one suggestion
// source: genre_artists caches music genres and podcast categories in the
// same table (hand-curated seeds — see scripts/seed_music_artists.py and
// scripts/seed_podcast_channels.py), so the channels step doesn't care which
// kind was picked.
//
// It is a required step, not a suggestion: there is no close button, no
// backdrop click and no Escape (see core.js's setupOverlay `dismissible:
// false`), and Finish only unlocks once REQUIRED_CHANNELS channels are
// followed. Dismissing it used to leave the profile in exactly the state it
// exists to prevent — an empty library and empty shelves — and, since
// needs_onboarding goes false the moment one channel exists, a single add
// was enough to be shown the door permanently.
//
// Everything it changes is settled before it closes: each add ends in a
// refreshFragments() sweep, and Finish waits out any add still running and
// re-checks Explore's shelves before hiding the overlay. All of that happens
// behind a full-screen modal, so the app the user is handed is already
// up to date instead of visibly redrawing itself a second later.

import { api, debounce, setupOverlay } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { BULK_IMPORT_FINISHED } from "./bulk-import.js";
import { reloadRecommendations, renderChannelResults } from "./explore.js";
import { followChannel } from "./remote.js";
import { saveInterests } from "./settings.js";

const STEP_IDS = ["onboarding-step-kind", "onboarding-step-genres", "onboarding-step-channels"];

// How many channels the last step asks for before Finish unlocks. One is
// enough to satisfy needs_onboarding but not enough to fill anything: Home's
// shelves, Library's grid and Explore's "For you" all read as broken with a
// single channel behind them. The step offers three ways to get there
// (suggestions, search, bulk import), so this is a handful of clicks.
const REQUIRED_CHANNELS = 5;

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
    // Also what starts Explore's shelves rebuilding, in the background,
    // while the user moves on to the channels step — see home/settings.js.
    // The cached batch is keyed to the interest list, so this edit is what
    // makes the next read of it several live YouTube searches long, and this
    // is the last moment nobody is waiting in front of them.
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

/**
 * The channels step, which also owns the gate on Finish.
 *
 * Returns { loadSuggestions, refreshGate, settled }: the genre step calls
 * `loadSuggestions(genres)` once it hands off (suggestions can't be fetched
 * until interests are actually saved, so this step fetches nothing on its
 * own), `refreshGate()` paints the counter when the step is first shown, and
 * `settled()` resolves once no add is still running.
 */
function setupChannelStep() {
  const container = document.getElementById("onboarding-step-channels");
  const searchInput = document.getElementById("onboarding-search-input");
  const searchResults = document.getElementById("onboarding-search-results");
  const suggested = document.getElementById("onboarding-suggested-channels");
  const finishBtn = document.getElementById("onboarding-finish");
  const progress = document.getElementById("onboarding-channels-progress");
  if (!container || !searchInput || !searchResults || !suggested) return null;

  // Counted rather than read back from the server: an "Add" that resolves
  // true is one channel this wizard followed, which is the whole question.
  // An already-followed channel (409) resolves false and doesn't count.
  let followed = 0;
  // Adds still in flight. Finish waits these out — each one ends in a
  // refreshFragments() sweep, and one landing a second after the overlay
  // closed is the "the page redrew itself at me" flicker this step used to
  // hand people on the way out.
  const running = new Set();

  function refreshGate() {
    const remaining = REQUIRED_CHANNELS - followed;
    if (progress) {
      progress.textContent =
        remaining > 0
          ? `Follow ${remaining} more to continue (${followed}/${REQUIRED_CHANNELS})`
          : `${followed} channels followed`;
    }
    if (finishBtn) finishBtn.disabled = remaining > 0;
  }

  // "Add" is the only action wired here — unlike Explore's own channel
  // search, a row click doesn't open the channel preview: that's a
  // full-panel navigation, and doing it out from under this modal would
  // just strand the wizard half-finished behind it. Both options passed to
  // followChannel are about staying inside the wizard: `announce: false`
  // because home/detail.js's CHANNEL_FOLLOWED listener navigates to the
  // newly-followed channel, which is right for Explore's search but would
  // yank someone out mid-flow here; `showOverlay: false` because the
  // full-screen backfill overlay sits *under* this modal and so reports
  // progress to nobody — the row's own button says "Adding…" and then
  // "Added" instead.
  container.addEventListener("click", async (event) => {
    const btn = event.target.closest(".btn-add-channel");
    if (!btn) return;
    const add = followChannel(btn.dataset.channelUrl, btn, {
      announce: false,
      showOverlay: false,
    });
    running.add(add);
    try {
      if (await add) {
        followed += 1;
        refreshGate();
      }
    } finally {
      running.delete(add);
    }
  });

  // An import run started from this step's "Import many at once" counts the
  // same as its own Add buttons — it's the same action taken in bulk.
  document.addEventListener(BULK_IMPORT_FINISHED, (event) => {
    followed += event.detail?.added ?? 0;
    refreshGate();
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

  return {
    async loadSuggestions(genres) {
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
    },
    refreshGate,
    settled: () => Promise.all(running),
  };
}

export function setupOnboarding() {
  const overlay = document.getElementById("onboarding-overlay");
  if (!overlay) return;

  const handle = setupOverlay("onboarding-overlay", null, [], { dismissible: false });
  const channelStep = setupChannelStep();
  const genreStep = setupGenreStep(
    (genres) => {
      setTitle(TITLES.channels);
      showStep("onboarding-step-channels");
      channelStep?.refreshGate();
      channelStep?.loadSuggestions(genres);
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

  const finishBtn = document.getElementById("onboarding-finish");
  finishBtn?.addEventListener("click", async () => {
    finishBtn.disabled = true;
    finishBtn.textContent = "Finishing…";
    // Anything still adding finishes behind the wizard rather than in front
    // of it (see `running` above). Normally there's nothing to wait for: the
    // gate only unlocks on adds that have already resolved.
    await channelStep?.settled();
    // Both of these are the app the user is about to be handed: Home's
    // shelves and Library's grid now that there are channels, and Explore's
    // shelves with the just-followed ones filtered back out of them (see
    // _drop_already_in_library). Cheap — the batch itself was rebuilt in the
    // background back on the genre step — and still behind the overlay, so
    // neither is a swap anyone sees.
    await Promise.all([refreshFragments(), reloadRecommendations()]);
    finishBtn.textContent = "Finish";
    handle?.close();
  });

  if (overlay.dataset.needsOnboarding === "true") handle?.open();
}
