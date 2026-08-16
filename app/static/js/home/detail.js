// The "detail" panel: a channel or one of the four pinned playlists
// (Favorites/Saved/New Uploads/Recently Played), drilled into from Library
// in place — a panel swap, not a navigation. Replaces the old standalone
// channel.html/content_list.html pages; folding them in here (rather than
// leaving them as separate documents) is what keeps the player overlay and
// mini-bar alive while browsing one, since there's no longer a document
// boundary for that DOM to fall off of.

import { unfollowChannel } from "../content-actions.js";
import { classifyHash, showToast } from "../core.js";
import { refreshFragments, swapFragmentHtml } from "../fragments.js";
import { openPlayer } from "./overlay.js";
import { QUEUE_CHANGED, isShuffled, loadQueue, queueSource, toggleShuffle } from "./queue.js";
import { activate } from "./tabs.js";

// What's currently open, so a pagination click knows what to re-fetch
// without re-parsing the URL, and so "Back to Library" knows whether there's
// actually a Library entry behind this one to pop back to (pushed) or
// whether this view was reached without one — a deep link, a fresh tab, a
// reload — in which case history.back() would leave the app entirely rather
// than showing Library. { kind, id, page, pushed } | null.
let current = null;

function detailUrl(kind, id, page) {
  const base = kind === "channel" ? `/partials/detail/channel/${id}` : `/partials/detail/playlist/${kind}`;
  return page > 1 ? `${base}?page=${page}` : base;
}

function hashFor(kind, id, page) {
  const path = kind === "channel" ? `channel/${id}` : kind;
  return page > 1 ? `#${path}?page=${page}` : `#${path}`;
}

function showLoading() {
  const panel = document.getElementById("detail-panel");
  if (panel) {
    panel.innerHTML = `<div class="detail-loading"><span class="spinner spinner-lg" role="status" aria-label="Loading"></span></div>`;
  }
}

/**
 * Opens a channel (kind: "channel", id: feed id) or a pinned playlist
 * (kind: one of favorites/saved/new-uploads/recently-played, id: null).
 * `replace` is for callers syncing to a URL that's already current (initial
 * boot, popstate, a pagination click within the same detail view) — a fresh
 * "the user just clicked into this" open pushes a new history entry instead,
 * so the back button can return to Library.
 */
export async function openDetail(kind, id, { page = 1, replace = false } = {}) {
  // A pagination click within the view that's already open keeps whatever
  // "is there a Library entry behind this" answer the original open
  // established — pagination itself always replaces, so it must not flip a
  // real push back to false.
  const isSameTarget = current && current.kind === kind && String(current.id) === String(id);
  const pushed = isSameTarget ? current.pushed : !replace;
  current = { kind, id, page, pushed };
  activate("detail", { updateHistory: false });
  const hash = hashFor(kind, id, page);
  if (replace) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);

  showLoading();

  let res;
  try {
    res = await fetch(detailUrl(kind, id, page));
  } catch (err) {
    showToast("Could not load this page");
    return;
  }
  if (!res.ok) {
    showToast(res.status === 404 ? "That's gone." : "Could not load this page");
    return;
  }
  swapFragmentHtml(await res.text());
  // The swap brings in a brand-new shuffle button, which knows nothing about
  // a preference set on some other view or in the player.
  syncShuffleButton();
}

// What the panel currently shows, in the shape queue.js takes: the channel
// or playlist "Play all" would fill the queue from.
function currentSource() {
  return current && { kind: current.kind, id: current.id ?? null };
}

function isSameSource(a, b) {
  return Boolean(a && b && a.kind === b.kind && String(a.id) === String(b.id));
}

function syncShuffleButton() {
  const btn = document.getElementById("detail-shuffle");
  if (!btn) return;
  btn.classList.toggle("is-on", isShuffled());
  btn.setAttribute("aria-pressed", String(isShuffled()));
}

/**
 * "Play all": fills the queue from the whole channel/playlist — every page of
 * it, not the twenty rows on screen — and starts on whichever track that
 * order puts first, which is a random one while shuffle is on.
 */
async function playAll(button) {
  const source = currentSource();
  if (!source) return;
  // The fetch is a round trip and the button is the kind people press twice;
  // without this the second press would build a second queue and jump
  // playback back to its start.
  button.disabled = true;
  try {
    const startId = await loadQueue(source);
    if (startId == null) {
      showToast("Nothing to play here");
      return;
    }
    openPlayer(startId);
  } finally {
    button.disabled = false;
  }
}

// The in-panel "Back to Library" control ends up here (the browser's own
// back button doesn't — that's the popstate listener below). Prefers
// history.back() so forward still works afterward, same as any other real
// navigation, but falls back to switching to Library in place when this view
// was never pushed (see `current.pushed` above) — otherwise back() would pop
// past the app entirely.
function closeDetail() {
  const pushed = current?.pushed;
  current = null;
  if (pushed) history.back();
  else activate("library");
}

async function handleUnfollow(feedId, button) {
  button.disabled = true;
  const ok = await unfollowChannel(feedId);
  if (!ok) {
    button.disabled = false;
    return;
  }
  // This detail view no longer means anything — back to Library, whose grid
  // needs to lose the tile too.
  closeDetail();
  refreshFragments();
}

function pageFromHref(href) {
  const match = href.match(/[?&]page=(\d+)/);
  return match ? Number(match[1]) : 1;
}

export function setupDetailPanel() {
  const panel = document.getElementById("detail-panel");
  if (!panel) return;

  // Delegated, not bound to specific ids: swapFragmentHtml replaces
  // #detail-panel's children wholesale on every open/paginate, which would
  // take a direct listener with it.
  panel.addEventListener("click", (event) => {
    if (event.target.closest("#detail-back-btn")) {
      closeDetail();
      return;
    }

    const unfollowBtn = event.target.closest("#unfollow-channel-btn");
    if (unfollowBtn) {
      handleUnfollow(unfollowBtn.dataset.feedId, unfollowBtn);
      return;
    }

    const playAllBtn = event.target.closest("#detail-play-all");
    if (playAllBtn) {
      playAll(playAllBtn);
      return;
    }

    // Shuffle is a toggle, not a second play button: it reorders a queue
    // already running through this view in place (keeping the current track
    // playing) and otherwise just decides the order the next Play builds. So
    // pressing it while something plays doesn't interrupt anything, and
    // pressing it on a view nobody is listening to doesn't start anything.
    if (event.target.closest("#detail-shuffle")) {
      toggleShuffle();
      return;
    }

    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;

    const pageLink = event.target.closest(".pagination-btn:not(.is-disabled)");
    if (pageLink && current) {
      event.preventDefault();
      openDetail(current.kind, current.id, { page: pageFromHref(pageLink.getAttribute("href")), replace: true });
      return;
    }

    const trackLink = event.target.closest(".track-row .track-link");
    if (trackLink) {
      event.preventDefault();
      const contentId = trackLink.closest(".track-row").dataset.contentId;
      const source = currentSource();
      // Clicking a row means "start here and keep going", the same as it does
      // in any music app — so the rest of this channel/playlist becomes the
      // queue, not just this one track. Checked before openPlayer, which
      // clears a queue the clicked track isn't part of (see queue.js's
      // noteCurrent) and would otherwise make this always look like a miss.
      const alreadyQueued = isSameSource(queueSource(), source);
      // Not awaited: the queue is only needed when this track ends, minutes
      // from now, and holding playback for a request that has nothing to do
      // with it would put a round trip in front of every single play.
      openPlayer(contentId);
      if (!alreadyQueued && source) loadQueue(source, { startId: contentId });
    }
  });

  // The player has its own shuffle toggle, and both drive the same
  // preference — whichever one is pressed, this panel's button has to follow.
  document.addEventListener(QUEUE_CHANGED, syncShuffleButton);

  window.addEventListener("popstate", () => {
    const info = classifyHash(location.hash.slice(1));
    if (info.type === "detail") openDetail(info.kind, info.id, { page: info.page, replace: true });
    else if (info.type === "player") openPlayer(info.id);
    else current = null;
  });
}

// Resolves whatever hash the page loaded with — called once at boot, after
// the pre-paint script has already gotten first paint right (see
// index.html) and setupPlayerOverlay/resumeOverlayIfNeeded are ready to
// receive an openPlayer call.
export function handleInitialRoute() {
  const info = classifyHash(location.hash.slice(1));
  if (info.type === "detail") openDetail(info.kind, info.id, { page: info.page, replace: true });
  else if (info.type === "player") openPlayer(info.id);
}
