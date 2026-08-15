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
      openPlayer(trackLink.closest(".track-row").dataset.contentId);
    }
  });

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
