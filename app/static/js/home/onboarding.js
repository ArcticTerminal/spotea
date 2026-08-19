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
// has chosen six channels, the first few are usually already done. It used
// to hold the button on "Adding…" for the whole round trip, so picking six
// channels meant six waits stacked on top of each other, and pressing
// Finish early meant watching the app redraw itself afterwards.
//
// A job is done once the feed row exists. Everything that fills it — the RSS
// content, the durations, the avatar, the one-time full-history scan — runs
// server-side afterwards (see services/backfill.run_initial_sync), and
// nothing here waits on any of it. Library says which channels are still
// filling in, on their own cards (see page_context.library_context), so the
// wait lives where it can be ignored rather than in front of someone who
// just wants to start listening.
//
// Finish therefore waits on nothing at all: it closes on the press. It used
// to await the queue, and that was the last real wait in the flow — measured
// per channel, 0.90s to recognise an artist, 1.32s of yt-dlp for the
// durations and 0.84s for the avatar, drained one channel at a time, so six
// channels was twenty seconds of "Finishing…". Moving that work behind the
// answer is what made the button honest; the final refresh is chained off
// the queue instead, and each add sweeps the fragments on its own way
// through.

import { api, debounce, setupOverlay } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { BULK_IMPORT_FINISHED } from "./bulk-import.js";
import { channelCardHtml, reloadRecommendations, renderChannelResults, shelfHtml } from "./explore.js";
import { wireScrollers } from "./scrollers.js";
import { followChannel } from "./remote.js";
import { saveInterests } from "./settings.js";

const STEP_IDS = ["onboarding-step-kind", "onboarding-step-genres", "onboarding-step-channels"];

// How many channels the last step asks for before Finish unlocks. One is
// enough to satisfy needs_onboarding but not enough to fill anything: Home's
// shelves, Library's grid and Explore's "For you" all read as broken with a
// single channel behind them. The step offers three ways to get there
// (suggestions, search, bulk import), so this is a handful of clicks.
const REQUIRED_CHANNELS = 6;

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
  // Which step is showing, stamped where CSS can see it: the channels step
  // takes Explore's full width for its shelves while the two picking steps
  // stay a narrow reading column, and the modal's full-bleed header and
  // footer align their contents to whichever column is current (see
  // --onboarding-column in style.css).
  const modal = document.querySelector(".modal-onboarding");
  if (modal) modal.dataset.step = id;
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

// One shelf per picked genre, built from Explore's own shelf and channel
// card (see explore.js's shelfHtml/channelCardHtml) rather than anything of
// this step's own: the same avatar-name-Add card the Explore tab offers a
// channel with, in the same horizontally scrolling row.
//
// A shelf carries the genre's whole seeded catalogue and scrolls sideways
// rather than being trimmed to what fits (see
// services/genre_artists.get_suggested_channels_by_genre) — the step is
// something to browse across, so the picks read as "here is Jazz, here is
// Metal" instead of a dozen anonymous rows stacked down the page.
//
// Only the Add button is wired, by the delegated handler below. Explore's
// cards also open the channel preview when clicked, but that is a full-panel
// navigation, and doing it out from under this modal would strand the wizard
// half-finished behind it — the same reason the search rows here don't
// preview either.
function renderSuggestionShelves(groups, container) {
  if (!groups.length) {
    // Only reachable when nothing the user picked was seeded — every
    // predefined chip is, so this is the free-typed case, which the search
    // box right below already covers.
    container.innerHTML = `<p class="muted">Nothing curated for those picks yet — search for a channel below.</p>`;
    return;
  }

  container.innerHTML = groups
    .map((group) => shelfHtml(group.genre, group.channels, channelCardHtml))
    .join("");
  // Shelves built here never pass through the fragment swap that normally
  // wires drag-scrolling, so — exactly like Explore's own — they have to ask
  // for it themselves.
  wireScrollers();
}

// One job per channel the user pressed Add on, in the order they pressed
// them. `status` walks queued -> working -> done | failed.
//
// They run one at a time rather than all at once: several channel resolutions in
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
 * Returns { loadSuggestions, refreshGate, settled }. The genre step calls
 * `loadSuggestions(genres)` once it hands off (suggestions can't be fetched
 * until interests are actually saved, so this step fetches nothing on its
 * own). `settled` is no longer something Finish waits on — it closes on the
 * press — only something it chains a final refresh onto.
 *
 * A failed add therefore reports itself, through followChannel's own toast,
 * rather than bouncing anyone back into a wizard they have already left.
 */
function setupChannelStep(onBack) {
  const container = document.getElementById("onboarding-step-channels");
  const searchInput = document.getElementById("onboarding-search-input");
  const searchResults = document.getElementById("onboarding-search-results");
  const suggested = document.getElementById("onboarding-suggested-channels");
  const finishBtn = document.getElementById("onboarding-finish");
  const progress = document.getElementById("onboarding-channels-progress");
  const backBtn = document.getElementById("onboarding-channels-back");
  if (!container || !searchInput || !searchResults || !suggested) return null;

  // Back re-opens the genre step with its picks still selected — going back
  // to widen or narrow them is the point, so nothing is reset here (unlike
  // the kind fork above, where a reset is what stops two taxonomies mixing).
  // Channels already added stay added: the queue behind them has been running
  // since the press, and un-following them is not what "back" means.
  backBtn?.addEventListener("click", () => onBack?.());

  // Counted optimistically, the moment Add is pressed — the gate is about
  // what the user has chosen, and making them wait on six round trips
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
      suggested.innerHTML = `<p class="search-loading"><span class="spinner"></span>Finding channels…</p>`;
      // Real artists/shows, not a generic "<genre> music" search — see
      // services/genre_artists.py and scripts/seed_podcast_channels.py. An
      // entry with nothing seeded for it (a free-typed one, or one the caches
      // just don't cover) simply contributes no block rather than an error;
      // the search box below still covers it.
      const { ok, data } = await api(
        `/onboarding/suggested-channels?genres=${encodeURIComponent(genres.join(","))}`
      );
      suggested.innerHTML = "";
      if (ok) renderSuggestionShelves(data, suggested);
    },
    refreshGate,
    settled: () => drained,
  };
}

export function setupOnboarding() {
  const overlay = document.getElementById("onboarding-overlay");
  if (!overlay) return;

  const handle = setupOverlay("onboarding-overlay", null, [], { dismissible: false });

  // Which fork the flow is in, so stepping *back* into the chip step can ask
  // the same question it asked on the way down — the title is per-kind there
  // (see TITLES) and there is nothing else on screen that still says which
  // one was picked.
  let pickedKind = "music";

  const showGenreStep = () => {
    setTitle(TITLES[pickedKind] ?? TITLES.music);
    showStep("onboarding-step-genres");
  };

  const channelStep = setupChannelStep(showGenreStep);
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
    pickedKind = kind;
    genreStep?.applyKind(kind);
    showGenreStep();
  });

  const finishBtn = document.getElementById("onboarding-finish");
  finishBtn?.addEventListener("click", () => {
    // Closes on the press, without waiting for the queue. Each add is a
    // round trip that ends in a feed row, and the queue drains one at a time
    // on purpose (see setupChannelStep) — six of them is six seconds of
    // "Finishing…" for work whose result is a card the user is about to be
    // shown anyway. The card itself reports the rest: a feed still being
    // filled in says "Fetching uploads…" from the moment it appears (see
    // services/backfill.run_initial_sync).
    //
    // The queue lives in a closure and the overlay only hides, so nothing
    // here stops when this closes.
    handle?.close();

    // Both of these are the app the user has just been handed: Home's
    // shelves and Library's grid now that there are channels, and Explore's
    // shelves with the just-followed ones filtered back out of them (see
    // _drop_already_in_library). Deliberately chained off the queue rather
    // than awaited in front of the user — the last channel to land is the
    // only one that makes either of them wrong, and every add refreshes the
    // fragments on its own way through.
    channelStep?.settled().then(() => {
      refreshFragments();
      reloadRecommendations();
    });
  });

  if (overlay.dataset.needsOnboarding === "true") handle?.open();
}
