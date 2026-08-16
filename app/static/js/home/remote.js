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
async function waitForBackfill(feedId, title) {
  setBackfillOverlayText(`${title} — Fetching channel history…`, "");

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
        setBackfillOverlayText(`${title} — ${parts.title}`, parts.detail);
      } else if (data.phase === "done") {
        break;
      } else if (!sawActivity && Date.now() - start > NEVER_STARTED_GRACE_MS) {
        break; // no channel id to resolve, or it never got scheduled — nothing to wait for
      }
    }
    await new Promise((r) => setTimeout(r, 400));
  }

  hideBackfillOverlay();
  // The old page-navigation version of this got Library's grid for free (it
  // was a full reload away). Staying on the same document means asking for
  // it explicitly, so "back" from the new channel doesn't land on a Library
  // tab that still looks like the channel was never added.
  refreshFragments();
  document.dispatchEvent(new CustomEvent(CHANNEL_FOLLOWED, { detail: { feedId, title } }));
}

export async function followChannel(channelUrl, button) {
  const originalLabel = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Adding…";
  }
  // Shown immediately, before the request even starts: add_feed's RSS sync
  // alone can take a couple of seconds, and leaving the screen looking idle
  // for that stretch reads as nothing happening yet.
  showBackfillOverlay("Fetching RSS feed…", "");

  const { ok, status, data } = await api("/feeds", {
    method: "POST",
    body: { channel_url: channelUrl },
  });

  if (ok) {
    if (data?.feed?.id != null) await waitForBackfill(data.feed.id, data.feed.channel_title || channelUrl);
    else window.location.reload();
    return;
  }

  hideBackfillOverlay();
  if (status === 409) {
    if (button) button.textContent = "Already added";
    return;
  }
  if (button) {
    button.disabled = false;
    button.textContent = originalLabel;
  }
  if (status !== 0) showToast(data?.detail || "Could not add channel");
}
