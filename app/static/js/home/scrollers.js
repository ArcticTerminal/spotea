// Drag-to-scroll for the app's horizontal rows.
//
// Its own module rather than part of home/library.js because three unrelated
// surfaces build rows client-side and need to wire them afterwards — Explore's
// shelves, the onboarding wizard's, and the artist profile's — and one of
// them lives in home/detail.js, which library.js already imports from. Left
// where it was, that would have made the app's first import cycle.

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
