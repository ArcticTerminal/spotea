// The Library tab's channel grid and search, Home's channel chips, the
// drag-to-scroll shelves, the collapsed mobile menu, and the manual feed
// refresh those last two both trigger.

import { api, showToast } from "../core.js";
import { saveResumeState } from "../resume.js";

export function setupHomeChannels() {
  const row = document.getElementById("home-channel-row");
  if (!row) return;

  row.addEventListener("click", (event) => {
    const chip = event.target.closest(".channel-chip");
    if (!chip) return;
    window.location.href = `/channel/${chip.dataset.feedId}`;
  });
}

// Every card is already server-rendered in the DOM, so filtering is just a
// show/hide over what's there — no round trip needed.
export function setupLibrarySearch() {
  const input = document.getElementById("library-search-input");
  const grid = document.querySelector("#tab-library .channel-grid");
  const emptyState = document.getElementById("channel-search-empty");
  if (!input || !grid) return;

  const cards = Array.from(grid.querySelectorAll(".channel-card"));

  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    let visibleCount = 0;

    cards.forEach((card) => {
      const title = card.querySelector(".channel-card-title")?.textContent.toLowerCase() ?? "";
      const matches = !query || title.includes(query);
      card.hidden = !matches;
      if (matches) visibleCount++;
    });

    if (emptyState) emptyState.hidden = visibleCount > 0;
  });
}

export function setupHorizontalScrollers() {
  // Shelf/channel rows are wider than their container by design. Genuine
  // horizontal gestures (trackpad two-finger swipe, shift+wheel, a tilt
  // wheel) already scroll these natively via overflow-x:auto — no JS needed.
  // What's missing is click-and-drag for plain mouse users, which this adds.
  // (An earlier version also redirected plain vertical wheel scroll into
  // horizontal movement, but that hijacked normal page scrolling anywhere the
  // cursor was over a row, effectively freezing it — removed.)
  document.querySelectorAll(".shelf-row, .channel-row").forEach((row) => {
    // The grab cursor (and drag-to-scroll below) should only kick in once a
    // row actually has overflow to scroll through — otherwise it's a false
    // affordance for a gesture that does nothing. Card widths are fixed by
    // CSS, so only the row's own box size (i.e. viewport width) can change
    // whether it overflows, which is exactly what ResizeObserver reports.
    const updateScrollable = () => {
      row.classList.toggle("is-scrollable", row.scrollWidth > row.clientWidth + 1);
    };
    updateScrollable();
    new ResizeObserver(updateScrollable).observe(row);

    // Links and images are natively draggable — without this, pressing down
    // on a card's thumbnail and moving the mouse makes the browser start its
    // own "drag this link/image out" gesture instead of firing the mousemove
    // events below, so the custom scroll never happens.
    row.addEventListener("dragstart", (event) => event.preventDefault());

    let isDown = false;
    let dragged = false;
    let startX = 0;
    let startScroll = 0;

    row.addEventListener("mousedown", (event) => {
      if (!row.classList.contains("is-scrollable")) return;
      isDown = true;
      dragged = false;
      startX = event.pageX;
      startScroll = row.scrollLeft;
      row.classList.add("dragging");
    });

    window.addEventListener("mouseup", () => {
      isDown = false;
      row.classList.remove("dragging");
    });

    row.addEventListener("mouseleave", () => {
      isDown = false;
      row.classList.remove("dragging");
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
async function refreshFeeds() {
  const overlay = document.getElementById("refresh-overlay");
  const btn = document.getElementById("refresh-feeds-btn");
  if (overlay) overlay.hidden = false;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("is-spinning");
  }

  const { ok } = await api("/feeds/refresh", { method: "POST" });

  if (ok) {
    // Always reload, regardless of new_content_count: that figure only counts
    // rows this exact call inserted, not rows apply_feed_data re-marked
    // is_new_upload on an already-existing row (its "self-heal" path — see
    // feed_sync.py), nor content some other trigger (the background refresh
    // job, another tab, another device) had already added since this page was
    // rendered. Gating the reload on it left New Uploads showing stale data
    // after an explicit refresh whenever either of those applied.
    // saveResumeState preserves playback across the reload.
    saveResumeState();
    window.location.reload();
    return;
  }

  showToast("Could not refresh feeds");
  if (overlay) overlay.hidden = true;
  if (btn) {
    btn.disabled = false;
    btn.classList.remove("is-spinning");
  }
}

export function setupRefreshButton() {
  document.getElementById("refresh-feeds-btn")?.addEventListener("click", () => refreshFeeds());
}

// Below the mobile-menu-btn breakpoint (see style.css), the profile/refresh/
// logout row collapses into this single hamburger dropdown instead — same
// underlying actions, just consolidated so the topbar doesn't have to fit
// three separate controls (and any more added later) on one narrow line.
export function setupMobileMenu(openProfileSwitcher) {
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
    refreshFeeds();
  });

  document.getElementById("mobile-menu-profile")?.addEventListener("click", () => {
    setOpen(false);
    openProfileSwitcher();
  });
}
