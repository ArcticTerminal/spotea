// Acting on content the library doesn't have yet: an Explore search result, a
// recommended song, a track of a YouTube playlist, or a whole unfollowed
// channel.
//
// Split out of home/explore.js because the detail panel needs the same three
// actions (its remote rows are Explore results rendered somewhere else), and
// this module can be imported by both without a cycle: it never imports
// home/detail.js. Where it would need to — opening the real channel page once
// a follow finishes — it fires CHANNEL_FOLLOWED and lets home/detail.js react,
// the same one-way arrangement home/queue.js uses for QUEUE_CHANGED.

import { api, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { openPlayer } from "./overlay.js";
import { queueSource, setQueue } from "./queue.js";

/** Fired once a newly followed channel has finished its history backfill.
 *  detail: { feedId, title }. */
export const CHANNEL_FOLLOWED = "spotea:channelfollowed";

/**
 * Explore's "listen" action: adds the video (always as an unkept preview —
 * see routers/explore.py's add_single_video) and jumps straight to its
 * player. If this video already has a Content row, add_single_video hands
 * back that row's id instead of erroring, so this just replays whatever was
 * already downloaded. No backfill-overlay wait here — unlike followChannel
 * this is a single insert, not a channel sync, so it should feel instant.
 */
export async function playRemoteVideo(dataset, button) {
  if (button) button.disabled = true;
  try {
    const { ok, data } = await api("/feeds/videos", {
      method: "POST",
      body: {
        video_id: dataset.videoId,
        title: dataset.title,
        thumbnail_url: dataset.thumbnailUrl || null,
        duration_seconds: dataset.durationSeconds ? Number(dataset.durationSeconds) : null,
        channel_title: dataset.channelTitle || null,
      },
      errorMessage: "Could not add this song",
    });
    if (ok) openPlayer(data.content_id);
  } finally {
    if (button) button.disabled = false;
  }
}

/** The remote track rows currently rendered in the detail panel, in order. */
function remoteRows() {
  return [...document.querySelectorAll("#detail-panel .track-row-remote")];
}

/**
 * Plays a whole remote playlist/channel listing: turns every row into a
 * preview Content row in one request, hands the resulting ids to the queue,
 * and starts on `startVideoId` (a clicked row) or wherever the queue's order
 * begins (Play all, which under shuffle is a random track).
 *
 * The batch costs no YouTube requests — every field those rows need came back
 * with the listing itself (see routers/explore.py's add_video_batch) — so
 * materializing the list up front is cheap, and everything downstream (the
 * one-track-ahead download prefetch, auto-advance, prev/next, shuffle) is the
 * ordinary queue with nothing special about it.
 *
 * The rows are read from the DOM rather than re-fetched: a remote listing has
 * no second page and no /content/queue/... endpoint, so what's on screen is
 * by definition the whole list.
 */
export async function playRemoteList(source, { startVideoId = null, button = null } = {}) {
  const rows = remoteRows();
  if (!rows.length) return;

  if (button) button.disabled = true;
  try {
    const items = rows
      .filter((row) => row.dataset.channelId)
      .map((row) => ({
        video_id: row.dataset.videoId,
        channel_id: row.dataset.channelId,
        title: row.dataset.title,
        thumbnail_url: row.dataset.thumbnailUrl || null,
        duration_seconds: row.dataset.durationSeconds ? Number(row.dataset.durationSeconds) : null,
        channel_title: row.dataset.channelTitle || null,
      }));
    if (!items.length) {
      showToast("Nothing to play here");
      return;
    }

    const { ok, data } = await api("/feeds/videos/batch", {
      method: "POST",
      body: { items },
      errorMessage: "Could not start this list",
    });
    if (!ok) return;

    // Positional: add_video_batch answers in exactly the order it was sent,
    // which is what lets a clicked row be found without matching on ids.
    const startIndex = startVideoId
      ? items.findIndex((item) => item.video_id === startVideoId)
      : -1;
    const clickedId = startIndex === -1 ? null : data.content_ids[startIndex];

    // Clicking a second row of a list that's already playing shouldn't
    // rebuild the queue — under shuffle that would reshuffle the rest around
    // the new track. Same reasoning as home/detail.js's alreadyQueued check
    // for local rows. "Play all" (no clicked row) always rebuilds, which is
    // what makes pressing it again start the list over.
    const queued = queueSource();
    if (clickedId != null && queued && queued.kind === source.kind && String(queued.id) === String(source.id)) {
      openPlayer(clickedId);
      return;
    }

    const startId = setQueue(source, data.content_ids, { startId: clickedId });
    if (startId != null) openPlayer(startId);
  } finally {
    if (button) button.disabled = false;
  }
}

// --- Following a channel ---------------------------------------------------

function setBackfillOverlayText(title, detail) {
  const titleEl = document.getElementById("backfill-overlay-title");
  const detailEl = document.getElementById("backfill-overlay-detail");
  if (titleEl) titleEl.textContent = title;
  if (detailEl) detailEl.textContent = detail || "";
}

function showBackfillOverlay(title, detail) {
  const overlay = document.getElementById("backfill-overlay");
  if (overlay) overlay.hidden = false;
  setBackfillOverlayText(title, detail);
}

function hideBackfillOverlay() {
  const overlay = document.getElementById("backfill-overlay");
  if (overlay) overlay.hidden = true;
}

// Split into a title (what's happening) and a short, single-line detail (the
// count) instead of one string — concatenating them let the browser wrap
// mid-phrase wherever it pleased (e.g. "…page" on one line, "7" on the next),
// which read as broken. Keeping the count in its own nowrap element keeps it
// atomic no matter how the title line wraps.
function backfillPhaseParts(phase, done, total) {
  if (phase === "scanning") {
    if (total > 0) return { title: "Fetching channel history…", detail: `${done}/${total} videos found` };
    if (done > 0) return { title: "Fetching channel history…", detail: `Page ${done}` };
    return { title: "Fetching channel history…", detail: "" };
  }
  if (phase === "saving") return { title: "Processing videos…", detail: `${done}/${total}` };
  return { title: "", detail: "" };
}

const isActiveBackfillPhase = (phase) => phase === "scanning" || phase === "saving";

// Polls until a just-added channel's backfill is done, then announces it.
// Assumes showBackfillOverlay() is already up (callers show it when the add
// starts, before the POST even resolves, so there's no gap where the screen
// looks idle while the RSS sync — itself a couple of seconds — is in flight).
async function waitForBackfill(feedId, title, { announce = true, showOverlay = true, onProgress } = {}) {
  // Two ways to report the same thing. showOverlay drives the app-wide
  // backfill overlay; onProgress hands the same phase/count to a caller with
  // its own place to put it — the onboarding wizard, whose full-screen modal
  // the overlay would be stacked underneath anyway, and which shows a row
  // per channel rather than one line for the whole app.
  const report = (phase, detail) => {
    if (showOverlay) setBackfillOverlayText(`${title} — ${phase}`, detail);
    onProgress?.({ phase, detail });
  };
  report("Fetching channel history…", "");

  const NEVER_STARTED_GRACE_MS = 4000;
  const MAX_WAIT_MS = 10 * 60 * 1000; // safety valve so a stuck check can't trap the user forever
  const start = Date.now();
  let sawActivity = false;

  while (Date.now() - start < MAX_WAIT_MS) {
    const { ok, data } = await api(`/feeds/${feedId}/backfill-status`);
    if (ok) {
      if (isActiveBackfillPhase(data.phase)) {
        sawActivity = true;
        const parts = backfillPhaseParts(data.phase, data.done, data.total);
        report(parts.title, parts.detail);
      } else if (data.phase === "done") {
        break;
      } else if (!sawActivity && Date.now() - start > NEVER_STARTED_GRACE_MS) {
        break; // no channel id to resolve, or it never got scheduled — nothing to wait for
      }
    }
    await new Promise((r) => setTimeout(r, 400));
  }

  if (showOverlay) hideBackfillOverlay();
  // The old page-navigation version of this got Library's grid for free (it
  // was a full reload away). Staying on the same document means asking for
  // it explicitly, so "back" from the new channel doesn't land on a Library
  // tab that still looks like the channel was never added.
  refreshFragments();
  // Skippable: home/detail.js's CHANNEL_FOLLOWED listener navigates to the
  // channel's real page, which is exactly what a search result or a
  // preview's Follow button wants but not what the onboarding wizard's
  // "Add" wants — that's a modal you're meant to stay inside while adding
  // several channels in a row, not something a follow should silently
  // navigate you out from under.
  if (announce) document.dispatchEvent(new CustomEvent(CHANNEL_FOLLOWED, { detail: { feedId, title } }));
}

/**
 * Follows a channel and waits out its history backfill. Resolves to
 * { added, status }: `added` is whether the channel is now followed —
 * true for a fresh follow, and also for a 409, which means some earlier
 * action already added it — and `status` is the HTTP status for a caller
 * that needs to tell those two apart or report a failure.
 *
 * `showOverlay: false` keeps the full-screen backfill overlay out of it, for
 * a caller that has its own modal up; `onProgress` hands that caller the
 * phase/count it would otherwise have shown there.
 *
 * `waitForHistory: false` returns as soon as the channel exists — that is,
 * once POST /feeds has resolved it and applied its RSS feed, which is what
 * puts its recent uploads in the library. The one-time full-history scan
 * behind it keeps running server-side either way (it is a background task,
 * not something the client holds open), and for a large channel it is
 * minutes long. Nothing on the screen a new profile lands on needs it, so
 * the onboarding wizard doesn't wait: Library's own card says the channel is
 * still filling in (see page_context.library_context).
 */
export async function followChannel(
  channelUrl,
  button,
  { announce = true, showOverlay = true, waitForHistory = true, onProgress } = {}
) {
  const originalLabel = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Adding…";
  }
  // Shown immediately, before the request even starts: add_feed's RSS sync
  // alone can take a couple of seconds, and leaving the screen looking idle
  // for that stretch reads as nothing happening yet.
  if (showOverlay) showBackfillOverlay("Fetching RSS feed…", "");
  // add_feed resolves the channel and syncs its RSS before it answers, which
  // is seconds on its own — a caller watching onProgress would otherwise see
  // nothing at all until the backfill starts.
  onProgress?.({ phase: "Fetching RSS feed…", detail: "" });

  const { ok, status, data } = await api("/feeds", {
    method: "POST",
    body: { channel_url: channelUrl },
  });

  if (ok) {
    if (data?.feed?.id == null) {
      window.location.reload();
      return { added: false, status };
    }
    if (!waitForHistory) {
      if (showOverlay) hideBackfillOverlay();
      // The feed row and its RSS videos exist now, which is what Library and
      // Home render from — and the grid needs re-rendering anyway to pick up
      // the new card's "still fetching" state.
      refreshFragments();
      if (button) button.textContent = "Added";
      return { added: true, status };
    }
    await waitForBackfill(data.feed.id, data.feed.channel_title || channelUrl, {
      announce,
      showOverlay,
      onProgress,
    });
    // The button is the only report a caller that suppressed the overlay
    // gets: without this its row sat on "Adding…" for as long as the modal
    // stayed open, with nothing to say the add had landed. Left disabled —
    // the channel is followed now, so there is nothing left to press.
    if (button) button.textContent = "Added";
    return { added: true, status };
  }

  if (showOverlay) hideBackfillOverlay();
  if (status === 409) {
    if (button) button.textContent = "Already added";
    return { added: true, status };
  }
  if (button) {
    button.disabled = false;
    button.textContent = originalLabel;
  }
  if (status !== 0) showToast(data?.detail || "Could not add channel");
  return { added: false, status };
}
