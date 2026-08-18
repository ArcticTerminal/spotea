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
// Adding a channel is not a wait. "Add" says "Added" the moment it is
// pressed and the real work — resolving the channel, syncing its RSS,
// backfilling its upload history — goes into a queue that drains behind the
// step while the user keeps picking. Which is the point: by the time anyone
// has chosen five channels, the first few are usually already done. It used
// to hold the button on "Adding…" for the whole round trip, so picking five
// channels meant five waits stacked on top of each other, and pressing
// Finish early meant watching the app redraw itself afterwards.
//
// A job is done once the channel exists — POST /feeds resolves it and applies
// its RSS feed before answering, so its recent uploads are already in the
// library at that point. The one-time full-history scan behind it is not
// waited on by anyone: it runs server-side, it is minutes long for a big
// channel, and nothing on the screen a new profile lands on needs it. Library
// says which channels are still filling in, on their own cards (see
// page_context.library_context) — the wait lives where it can be ignored
// rather than in front of someone who just wants to start listening.
//
// Finish therefore waits only on RSS syncs still in flight, which is a second
// or two and usually nothing at all. Everything it changes is still settled
// before it closes — each add ends in a refreshFragments() sweep, and Finish
// re-checks Explore's shelves too — and all of it happens behind a
// full-screen modal, so the app the user is handed is already up to date
// instead of visibly redrawing itself a second later.

import { api, debounce, setupOverlay, showToast } from "../core.js";
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

// One job per channel the user pressed Add on, in the order they pressed
// them. `status` walks queued -> working -> done | failed.
//
// They run one at a time rather than all at once: five channel resolutions in
// flight together is a request burst against a service that rate-limits an
// unauthenticated residential IP (see services/recommendations.py's
// request-budget note for the same reasoning elsewhere), and the server's own
// bulk importer creates channels serially for exactly this reason.
function channelJob(channelUrl, button) {
  return { channelUrl, button, status: "queued" };
}

/**
 * The channels step: the Add queue, the counter, and the gate on Finish.
 *
 * Returns { loadSuggestions, refreshGate, settled, failedCount,
 * followedCount }. The genre step calls `loadSuggestions(genres)` once it
 * hands off (suggestions can't be fetched until interests are actually saved,
 * so this step fetches nothing on its own); the rest is what Finish needs to
 * know once the queue has drained.
 */
function setupChannelStep() {
  const container = document.getElementById("onboarding-step-channels");
  const searchInput = document.getElementById("onboarding-search-input");
  const searchResults = document.getElementById("onboarding-search-results");
  const suggested = document.getElementById("onboarding-suggested-channels");
  const finishBtn = document.getElementById("onboarding-finish");
  const progress = document.getElementById("onboarding-channels-progress");
  if (!container || !searchInput || !searchResults || !suggested) return null;

  // Counted optimistically, the moment Add is pressed — the gate is about
  // what the user has chosen, and making them wait on five round trips
  // before the button unlocks is the wait this step exists to remove. A job
  // that turns out to fail takes its count back with it (see runQueue).
  let followed = 0;
  const jobs = [];
  let draining = false;
  let drained = Promise.resolve();

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

  async function runQueue() {
    draining = true;
    try {
      let job;
      while ((job = jobs.find((candidate) => candidate.status === "queued"))) {
        job.status = "working";

        // No button handed over: this step owns the row's label (it said
        // "Added" back when the click happened), and followChannel's own
        // labelling would fight it. The three options are all about not
        // making anyone wait or leave — see the click handler below and
        // followChannel's own docs.
        const { added } = await followChannel(job.channelUrl, null, {
          announce: false,
          showOverlay: false,
          waitForHistory: false,
        });

        job.status = added ? "done" : "failed";
        if (!added) {
          // Take the optimistic count back and put the row's button back the
          // way it was, so it can be tried again. followChannel has already
          // toasted why.
          followed -= 1;
          if (job.button) {
            job.button.disabled = false;
            job.button.textContent = "Add";
          }
        }
        refreshGate();
      }
    } finally {
      draining = false;
    }
  }

  function enqueue(channelUrl, button) {
    jobs.push(channelJob(channelUrl, button));
    followed += 1;
    refreshGate();
    // A queue already draining picks the new job up on its next pass; only a
    // stopped one needs starting, and `drained` has to keep pointing at
    // whichever run is current so Finish can wait on it.
    if (!draining) drained = runQueue();
  }

  // "Add" is the only action wired here — unlike Explore's own channel
  // search, a row click doesn't open the channel preview: that's a
  // full-panel navigation, and doing it out from under this modal would just
  // strand the wizard half-finished behind it. The two options passed to
  // followChannel in runQueue are about staying inside the wizard too:
  // `announce: false` because home/detail.js's CHANNEL_FOLLOWED listener
  // navigates to the newly-followed channel, which is right for Explore's
  // search but would yank someone out mid-flow here; `showOverlay: false`
  // because the full-screen backfill overlay sits *under* this modal and so
  // reports progress to nobody.
  container.addEventListener("click", (event) => {
    const btn = event.target.closest(".btn-add-channel");
    if (!btn || btn.disabled) return;
    // Said before anything is requested, not after it all finishes: the work
    // is queued, and the user has no reason to stand in front of it.
    btn.disabled = true;
    btn.textContent = "Added";
    enqueue(btn.dataset.channelUrl, btn);
  });

  // An import run started from this step's "Import many at once" counts the
  // same as its own Add buttons — it's the same action taken in bulk, and it
  // has already finished by the time it says so, so it needs no job of its
  // own.
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
    failedCount: () => jobs.filter((job) => job.status === "failed").length,
    followedCount: () => followed,
    settled: () => drained,
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

    // The only wait left in the flow, and it is a short one: a job is done
    // once its channel exists, and the queue has been draining since the
    // first Add. Nothing here waits on a history scan — see the module
    // comment above.
    await channelStep?.settled();

    // Both of these are the app the user is about to be handed: Home's
    // shelves and Library's grid now that there are channels, and Explore's
    // shelves with the just-followed ones filtered back out of them (see
    // _drop_already_in_library). Cheap — the batch itself was rebuilt in the
    // background back on the genre step — and still behind the overlay, so
    // neither is a swap anyone sees.
    await Promise.all([refreshFragments(), reloadRecommendations()]);

    // Adds are counted when they're pressed, so a failure that came back
    // while the user was still picking can leave the gate unmet by the time
    // the queue drains. Landing back on the step with the failed rows
    // pressable again beats closing on a profile that didn't get what it
    // was told it needed.
    if (channelStep && channelStep.followedCount() < REQUIRED_CHANNELS) {
      const failed = channelStep.failedCount();
      finishBtn.textContent = "Finish";
      channelStep.refreshGate();
      showToast(
        failed === 1 ? "One channel couldn't be added" : `${failed} channels couldn't be added`
      );
      return;
    }

    handle?.close();
  });

  if (overlay.dataset.needsOnboarding === "true") handle?.open();
}
