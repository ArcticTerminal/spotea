// The Library tab's channel grid and search, Home's channel chips, the
// drag-to-scroll shelves, the collapsed mobile menu, and the manual feed
// refresh those last two both trigger.

import { api, showToast } from "../core.js";
import { onFragmentsSwapped, refreshFragments } from "../fragments.js";
import { openDetail } from "./detail.js";

// Delegated from the panel rather than the chip row/see-more links
// themselves: both live inside the Home fragment and are replaced wholesale
// on every refresh, which would take a directly-bound listener with them.
export function setupHomeChannels() {
  document.getElementById("tab-home")?.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    const chip = event.target.closest(".channel-chip");
    if (chip) {
      openDetail("channel", chip.dataset.feedId);
      return;
    }

    const seeMore = event.target.closest(".shelf-see-more[data-detail-kind]");
    if (seeMore) {
      event.preventDefault();
      openDetail(seeMore.dataset.detailKind, null);
    }
  });
}

// Same idea for the Library grid's pinned playlist tiles and per-channel
// cards — delegated from the panel (rather than bound per-card) because
// #library-grid's contents are replaced wholesale on every fragment refresh.
export function setupLibraryChannelGrid() {
  document.getElementById("tab-library")?.addEventListener("click", (event) => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    const card = event.target.closest(".channel-card");
    if (!card) return;
    event.preventDefault();
    openDetail(card.dataset.detailKind, card.dataset.detailId || null);
  });
}

// Every card is already server-rendered in the DOM, so filtering is just a
// show/hide over what's there — no round trip needed.
//
// The cards are looked up on each keystroke rather than captured once: the
// grid is inside the Library fragment and is replaced on every refresh, so a
// captured list would go stale (and the grid is a handful of nodes, so
// re-querying costs nothing).
function applyLibraryFilter() {
  const input = document.getElementById("library-search-input");
  const grid = document.querySelector("#library-grid .channel-grid");
  const emptyState = document.getElementById("channel-search-empty");
  if (!input || !grid) return;

  const query = input.value.trim().toLowerCase();
  let visibleCount = 0;

  grid.querySelectorAll(".channel-card").forEach((card) => {
    const title = card.querySelector(".channel-card-title")?.textContent.toLowerCase() ?? "";
    const matches = !query || title.includes(query);
    card.hidden = !matches;
    if (matches) visibleCount++;
  });

  if (emptyState) emptyState.hidden = visibleCount > 0;
}

export function setupLibrarySearch() {
  const input = document.getElementById("library-search-input");
  if (!input) return;
  input.addEventListener("input", applyLibraryFilter);
  // A refreshed grid comes back unfiltered, so whatever is in the box has to
  // be applied again.
  onFragmentsSwapped(applyLibraryFilter);
}

export function setupHorizontalScrollers() {
  wireScrollers();
  // Rows inside the Home fragment are replaced on every refresh, so newly
  // swapped-in ones need wiring too. Registered here, once, rather than from
  // inside wireScrollers — doing it there would add another callback on every
  // swap.
  onFragmentsSwapped(wireScrollers);
}

// Every row currently under active drag-scroll wiring, so a fragment swap
// that removes it (replaceChildren, not row.remove()) can have its
// ResizeObserver disconnected. Nothing else ever called .disconnect(): each
// wireScrollers() call after a swap wired the *new* rows the server sent
// back, but the ResizeObserver watching the *old*, now-detached ones just
// kept existing — measured live, 5 observers at boot became 105 after 20
// refreshes, one leaked per row per swap. Keyed by row rather than pruned
// eagerly at swap time because Explore's recommendation shelves call
// wireScrollers() directly for rows that were never part of a fragment swap
// at all — "is this row still in the document?" is the only check that's
// right either way.
const wiredRows = new Map();

function pruneDetachedObservers() {
  for (const [row, observer] of wiredRows) {
    if (document.contains(row)) continue;
    observer.disconnect();
    wiredRows.delete(row);
  }
}

// Whichever row's mousedown most recently fired, so the single window-level
// mouseup listener below knows which row to reset — see wireScrollers' own
// comment for why this listener exists at module scope instead of one per
// row. `dragged` deliberately isn't reset here: it stays a private variable
// inside each row's own closure below, exactly as it was before this leak
// fix, only reset by that row's own next mousedown. A shared/reset-on-mouseup
// `dragged` would go stale across rows — mouseup fires before click, so
// resetting it here would already be false by the time a row's own click
// handler checks it below, defeating the suppression entirely.
let activeDragReset = null;

window.addEventListener("mouseup", () => {
  activeDragReset?.();
  activeDragReset = null;
});

/**
 * Makes every not-yet-wired `.shelf-row`/`.channel-row` on the page
 * drag-scrollable. Idempotent (each row is marked once), so anything that
 * builds a new row client-side — Explore's recommendation shelves — can just
 * call this afterwards rather than repeating the behaviour.
 */
export function wireScrollers() {
  pruneDetachedObservers();

  // Shelf/channel rows are wider than their container by design. Genuine
  // horizontal gestures (trackpad two-finger swipe, shift+wheel, a tilt
  // wheel) already scroll these natively via overflow-x:auto — no JS needed.
  // What's missing is click-and-drag for plain mouse users, which this adds.
  // (An earlier version also redirected plain vertical wheel scroll into
  // horizontal movement, but that hijacked normal page scrolling anywhere the
  // cursor was over a row, effectively freezing it — removed.)
  document.querySelectorAll(".shelf-row, .channel-row").forEach((row) => {
    if (row.dataset.scrollerReady) return;
    row.dataset.scrollerReady = "true";

    // The grab cursor (and drag-to-scroll below) should only kick in once a
    // row actually has overflow to scroll through — otherwise it's a false
    // affordance for a gesture that does nothing. Card widths are fixed by
    // CSS, so only the row's own box size (i.e. viewport width) can change
    // whether it overflows, which is exactly what ResizeObserver reports.
    // Same signal also decides whether this shelf's "See more" link is
    // worth showing: if every card already fits, there's nothing more to
    // scroll to.
    const seeMore = row.closest(".shelf")?.querySelector(".shelf-see-more");
    const updateScrollable = () => {
      const isScrollable = row.scrollWidth > row.clientWidth + 1;
      row.classList.toggle("is-scrollable", isScrollable);
      if (seeMore) seeMore.hidden = !isScrollable;
    };
    updateScrollable();
    const observer = new ResizeObserver(updateScrollable);
    observer.observe(row);
    wiredRows.set(row, observer);

    // Links and images are natively draggable — without this, pressing down
    // on a card's thumbnail and moving the mouse makes the browser start its
    // own "drag this link/image out" gesture instead of firing the mousemove
    // events below, so the custom scroll never happens.
    row.addEventListener("dragstart", (event) => event.preventDefault());

    let isDown = false;
    let dragged = false;
    let startX = 0;
    let startScroll = 0;

    function resetDrag() {
      isDown = false;
      row.classList.remove("dragging");
    }

    row.addEventListener("mousedown", (event) => {
      if (!row.classList.contains("is-scrollable")) return;
      isDown = true;
      dragged = false;
      startX = event.pageX;
      startScroll = row.scrollLeft;
      row.classList.add("dragging");
      activeDragReset = resetDrag;
    });

    row.addEventListener("mouseleave", () => {
      resetDrag();
      if (activeDragReset === resetDrag) activeDragReset = null;
    });

    row.addEventListener("mousemove", (event) => {
      if (!isDown) return;
      const delta = event.pageX - startX;
      if (Math.abs(delta) > 5) dragged = true;
      row.scrollLeft = startScroll - delta;
      event.preventDefault();
    });

    // A drag that happened to pass over a card/chip shouldn't also trigger
    // its click (playing a video, following a channel filter, etc.).
    row.addEventListener(
      "click",
      (event) => {
        if (dragged) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
  });
}

// Feeds are also kept fresh by a server-side background job on a schedule set
// in Settings (see routers/settings.py) — this is just for "I want it now".
// The overlay (rather than just the button's own spin state) is the feedback
// here because refresh-feeds-btn itself is hidden under the mobile-menu
// breakpoint (see style.css); the overlay covers that entry point too.
async function refreshFeeds(alsoRefresh) {
  const overlay = document.getElementById("refresh-overlay");
  const btn = document.getElementById("refresh-feeds-btn");
  if (overlay) overlay.hidden = false;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("is-spinning");
  }

  // Explore's recommendations are rebuilt alongside the feeds rather than
  // having a refresh control of their own — one button means "go and look at
  // everything again". Run together, since both are slow and independent.
  const [{ ok }] = await Promise.all([
    api("/feeds/refresh", { method: "POST" }),
    alsoRefresh ? alsoRefresh() : Promise.resolve(),
  ]);

  // Always re-render, regardless of new_content_count: that figure only
  // counts rows this exact call inserted, not rows apply_feed_data re-marked
  // is_new_upload on an already-existing row (its "self-heal" path — see
  // feed_sync.py), nor content some other trigger (the background refresh
  // job, another tab, another device) had already added since this page was
  // rendered. This used to be a full page reload, which meant saving and
  // restoring playback around it; re-rendering the shelves in place leaves
  // the player alone entirely.
  if (ok) await refreshFragments();
  else showToast("Could not refresh feeds");

  if (overlay) overlay.hidden = true;
  if (btn) {
    btn.disabled = false;
    btn.classList.remove("is-spinning");
  }
}

// `alsoRefresh` is injected rather than imported (pages/index.js passes
// home/explore.js's refreshRecommendations) — importing it here would make
// library.js and explore.js import each other, since explore.js already takes
// wireScrollers from this module. Same arrangement as setupMobileMenu's
// openProfileSwitcher below.
export function setupRefreshButton(alsoRefresh) {
  document
    .getElementById("refresh-feeds-btn")
    ?.addEventListener("click", () => refreshFeeds(alsoRefresh));
}

// Below the mobile-menu-btn breakpoint (see style.css), the profile/refresh/
// logout row collapses into this single hamburger dropdown instead — same
// underlying actions, just consolidated so the topbar doesn't have to fit
// three separate controls (and any more added later) on one narrow line.
export function setupMobileMenu(openProfileSwitcher, alsoRefresh) {
  const btn = document.getElementById("mobile-menu-btn");
  const menu = document.getElementById("mobile-menu");
  if (!btn || !menu) return;

  const setOpen = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };

  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });

  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== btn) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setOpen(false);
  });

  document.getElementById("mobile-menu-refresh")?.addEventListener("click", () => {
    setOpen(false);
    refreshFeeds(alsoRefresh);
  });

  document.getElementById("mobile-menu-profile")?.addEventListener("click", () => {
    setOpen(false);
    openProfileSwitcher();
  });
}
