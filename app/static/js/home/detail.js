// The "detail" panel: a track list with a hero above it, drilled into in
// place — a panel swap, not a navigation. Replaces the old standalone
// channel.html/content_list.html pages; folding them in here (rather than
// leaving them as separate documents) is what keeps the player overlay and
// mini-bar alive while browsing one, since there's no longer a document
// boundary for that DOM to fall off of.
//
// Four things open it, and they differ only in where the rows come from:
//   channel/{feed_id}     a followed channel        (from Library)
//   {playlist kind}       one of the four pinned    (from Library)
//   yt-playlist/{id}      a YouTube playlist        (from Explore)
//   yt-channel/{id}       an unfollowed channel     (from Explore)
// The last two are "remote": their rows have no Content row yet, so playing
// from them goes through home/remote.js, which materializes the list first.

import { unfollowChannel } from "../content-actions.js";
import { classifyHash, showToast } from "../core.js";
import { refreshFragments, swapFragmentHtml } from "../fragments.js";
import { openPlayer } from "./overlay.js";
import { QUEUE_CHANGED, isShuffled, loadQueue, queueSource, toggleShuffle } from "./queue.js";
import { CHANNEL_FOLLOWED, followChannel, playRemoteList } from "./remote.js";
import { activate } from "./tabs.js";

// Detail kinds whose rows come from YouTube rather than the database.
const REMOTE_KINDS = ["yt-channel", "yt-playlist"];
const isRemoteKind = (kind) => REMOTE_KINDS.includes(kind);

// Remote fragment HTML already fetched this session, keyed by the exact
// detailUrl() it came from (page and avatar included, so a different avatar
// hint or page is correctly a cache miss rather than stale content). Local
// kinds (channel, the four pinned playlists) always refetch instead — those
// are a cheap DB read to begin with, and other actions in this app (saving,
// favoriting) can change what one shows. A remote kind costs a live yt-dlp
// read (see routers/partials.py) and nothing in this app mutates a listing
// it reads, so re-opening one already visited this session has nothing to
// gain from asking again. Capped so a long browsing session doesn't grow
// this forever; Map preserves insertion order, so the oldest entry is
// whatever .keys().next() gives back.
const REMOTE_CACHE_LIMIT = 20;
const remoteFragmentCache = new Map();

// Which tab this view belongs to. Drives both the no-history fallback in
// closeDetail and, via a data attribute on <html>, which tab button stays
// visually selected while the panel is open (see style.css) — the detail
// panel has no .tab-btn of its own.
const detailHome = (kind) => (isRemoteKind(kind) ? "explore" : "library");

// What's currently open, so a pagination click knows what to re-fetch
// without re-parsing the URL, and so "Back to Library" knows whether there's
// actually a Library entry behind this one to pop back to (pushed) or
// whether this view was reached without one — a deep link, a fresh tab, a
// reload — in which case history.back() would leave the app entirely rather
// than showing Library. { kind, id, page, pushed } | null.
let current = null;

// Kinds that carry an id put it in the path; the four pinned playlists are
// the kind itself, and take the /playlist/{kind} route.
const hasId = (kind) => kind === "channel" || isRemoteKind(kind);

function detailUrl(kind, id, page, avatar) {
  const base = hasId(kind) ? `/partials/detail/${kind}/${id}` : `/partials/detail/playlist/${kind}`;
  const params = new URLSearchParams();
  if (page > 1) params.set("page", page);
  // yt-channel only: what the card the user clicked through from already
  // had rendered, forwarded so the detail hero doesn't render blank for a
  // channel nobody's followed yet — see remote_channel_context's docstring.
  if (kind === "yt-channel" && avatar) params.set("avatar", avatar);
  const query = params.toString();
  return query ? `${base}?${query}` : base;
}

function hashFor(kind, id, page) {
  const path = hasId(kind) ? `${kind}/${id}` : kind;
  return page > 1 ? `#${path}?page=${page}` : `#${path}`;
}

function showLoading() {
  const panel = document.getElementById("detail-panel");
  if (panel) {
    panel.innerHTML = `<div class="detail-loading"><span class="spinner spinner-lg" role="status" aria-label="Loading"></span></div>`;
  }
}

/**
 * Opens one of the four sources listed at the top of this file. `id` is a
 * feed id for "channel", a YouTube id for the two remote kinds, and null for
 * a pinned playlist (whose kind is its whole identity).
 *
 * `replace` is for callers syncing to a URL that's already current (initial
 * boot, popstate, a pagination click within the same detail view) — a fresh
 * "the user just clicked into this" open pushes a new history entry instead,
 * so the back button can return where it came from.
 *
 * `avatar` (yt-channel only) is the thumbnail_url already on the card being
 * opened, if any — see detailUrl. Not part of the hash/history state, so a
 * reload or back/forward into this view loses it same as before; it only
 * saves a blank hero on the click path that actually has one to offer.
 */
export async function openDetail(kind, id, { page = 1, replace = false, avatar = null } = {}) {
  // A pagination click within the view that's already open keeps whatever
  // "is there a Library entry behind this" answer the original open
  // established — pagination itself always replaces, so it must not flip a
  // real push back to false.
  const isSameTarget = current && current.kind === kind && String(current.id) === String(id);
  const pushed = isSameTarget ? current.pushed : !replace;
  current = { kind, id, page, pushed };
  document.documentElement.dataset.detailHome = detailHome(kind);
  activate("detail", { updateHistory: false });
  const hash = hashFor(kind, id, page);
  if (replace) history.replaceState(null, "", hash);
  else history.pushState(null, "", hash);

  const url = detailUrl(kind, id, page, avatar);
  const cached = isRemoteKind(kind) ? remoteFragmentCache.get(url) : undefined;
  if (cached !== undefined) {
    swapFragmentHtml(cached);
    syncShuffleButton();
    return;
  }

  showLoading();

  let res;
  try {
    res = await fetch(url);
  } catch (err) {
    showToast("Could not load this page");
    return;
  }
  if (!res.ok) {
    showToast(res.status === 404 ? "That's gone." : "Could not load this page");
    return;
  }
  const html = await res.text();
  if (isRemoteKind(kind)) {
    remoteFragmentCache.set(url, html);
    if (remoteFragmentCache.size > REMOTE_CACHE_LIMIT) {
      remoteFragmentCache.delete(remoteFragmentCache.keys().next().value);
    }
  }
  swapFragmentHtml(html);
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
 *
 * A remote list has no server-side queue to fetch and no second page, so
 * home/remote.js builds it from the rows on screen instead; either way the
 * queue that comes out is the same thing.
 */
async function playAll(button) {
  const source = currentSource();
  if (!source) return;
  if (isRemoteKind(source.kind)) {
    playRemoteList(source, { button });
    return;
  }
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
  const home = detailHome(current?.kind);
  current = null;
  if (pushed) history.back();
  else activate(home);
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

    const followBtn = event.target.closest("#follow-channel-btn");
    if (followBtn) {
      // Doesn't navigate here — the CHANNEL_FOLLOWED listener below opens the
      // real (library) channel page once the history backfill has finished,
      // which is the same place adding from a search result lands.
      followChannel(followBtn.dataset.channelUrl, followBtn);
      return;
    }

    const openFollowedBtn = event.target.closest("#open-followed-channel-btn");
    if (openFollowedBtn) {
      openDetail("channel", openFollowedBtn.dataset.feedId);
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
      const row = trackLink.closest(".track-row");
      const source = currentSource();

      // A remote row has no content id yet — clicking it means the same
      // thing ("start here and keep going"), it just has to materialize the
      // list before there's a queue to do it with.
      if (row.dataset.videoId) {
        if (source) playRemoteList(source, { startVideoId: row.dataset.videoId });
        return;
      }

      const contentId = row.dataset.contentId;
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

  // A channel just finished being followed — from an Explore search result, a
  // recommendation card, or the Follow button on its own preview page. All
  // three land on the real channel page, which is this module's to open (see
  // home/remote.js for why it announces rather than calls).
  document.addEventListener(CHANNEL_FOLLOWED, (event) => {
    // A cached yt-channel view's Follow button would otherwise still say
    // "Follow" if reopened later — the event only carries the new feed id,
    // not the remote channel_id its cache entry would be keyed on, so
    // dropping the whole cache is simpler than tracking that mapping for a
    // follow action that isn't a frequent occurrence to begin with.
    remoteFragmentCache.clear();
    openDetail("channel", event.detail.feedId);
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
