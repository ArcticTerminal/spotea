// Explore: one search box over both songs and channels, plus what happens
// when you act on a result — "listen" to a song, or follow a channel (which
// kicks off a history backfill worth showing progress for).

import { api, debounce, escapeHtml, formatDuration, showToast } from "../core.js";
import { openPlayer } from "./overlay.js";

function formatSubscribers(count) {
  if (count == null) return "";
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M subscribers`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}K subscribers`;
  return `${count} subscribers`;
}

function renderChannelResults(results) {
  const list = document.getElementById("channel-search-results");
  if (!list) return;

  if (!results.length) {
    list.innerHTML = `<li class="search-empty">No channels found</li>`;
    return;
  }

  list.innerHTML = results
    .map((r) => {
      const thumb = r.thumbnail_url
        ? `<img class="search-result-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" />`
        : `<span class="search-result-thumb"></span>`;
      const subs =
        r.subscriber_count != null
          ? `<span class="search-result-subs">${formatSubscribers(r.subscriber_count)}</span>`
          : "";
      return `
        <li class="search-result">
          ${thumb}
          <div class="search-result-info">
            <span class="search-result-title">${escapeHtml(r.title)}</span>
            ${subs}
          </div>
          <button type="button" class="btn-add-channel" data-channel-url="${escapeHtml(r.channel_url)}">Add</button>
        </li>
      `;
    })
    .join("");
}

function renderVideoResults(results) {
  const list = document.getElementById("video-search-results");
  if (!list) return;

  if (!results.length) {
    list.innerHTML = `<li class="search-empty">No songs found</li>`;
    return;
  }

  list.innerHTML = results
    .map((r) => {
      const thumb = r.thumbnail_url
        ? `<img class="video-search-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" />`
        : `<span class="video-search-thumb"></span>`;
      const duration = r.duration_seconds != null ? formatDuration(r.duration_seconds) : "";
      const channel = r.channel_title ? escapeHtml(r.channel_title) : "";
      const meta = [channel, duration].filter(Boolean).join(" · ");
      return `
        <li
          class="search-result video-search-result"
          data-video-id="${escapeHtml(r.video_id)}"
          data-title="${escapeHtml(r.title)}"
          data-thumbnail-url="${escapeHtml(r.thumbnail_url || "")}"
          data-duration-seconds="${r.duration_seconds ?? ""}"
          data-channel-title="${escapeHtml(r.channel_title || "")}"
        >
          ${thumb}
          <div class="search-result-info">
            <span class="search-result-title">${escapeHtml(r.title)}</span>
            <span class="search-result-subs">${meta}</span>
          </div>
          <button type="button" class="btn-icon video-search-play" aria-label="Play">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><use href="#i-play" /></svg>
          </button>
        </li>
      `;
    })
    .join("");
}

// Explore's "listen" action: adds the video (always as an unkept preview —
// see routers/explore.py's add_single_video) and jumps straight to its
// player. If this video already has a Content row, add_single_video hands
// back that row's id instead of erroring, so this just replays whatever was
// already downloaded. No backfill-overlay wait here — unlike addChannel this
// is a single insert, not a channel sync, so it should feel instant.
async function playSearchedVideo(dataset, button) {
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

// Polls until a just-added channel's backfill is done, then navigates to it.
// Assumes showBackfillOverlay() is already up (callers show it when the add
// starts, before the POST even resolves, so there's no gap where the screen
// looks idle while the RSS sync — itself a couple of seconds — is in flight).
async function waitForBackfillThenOpen(feedId, title) {
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

  window.location.href = `/channel/${feedId}`;
}

async function addChannel(channelUrl, button) {
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
    if (data?.feed?.id != null) await waitForBackfillThenOpen(data.feed.id, data.feed.channel_title || channelUrl);
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
    button.textContent = "Add";
  }
  if (status !== 0) showToast(data?.detail || "Could not add channel");
}

// One input drives both endpoints in parallel, instead of two permanently
// visible search boxes.
export function setupExploreSearch() {
  const input = document.getElementById("explore-search-input");
  const resultsPanel = document.getElementById("explore-results-panel");
  const browsePanel = document.getElementById("explore-browse-panel");
  const videoResults = document.getElementById("video-search-results");
  const channelResults = document.getElementById("channel-search-results");
  if (!input || !resultsPanel || !browsePanel) return;

  const runSearch = debounce(async (query) => {
    if (!query) {
      resultsPanel.hidden = true;
      browsePanel.hidden = false;
      videoResults.innerHTML = "";
      channelResults.innerHTML = "";
      return;
    }

    resultsPanel.hidden = false;
    browsePanel.hidden = true;
    // Shown immediately, before either fetch resolves — without this, the
    // Songs/Channels headings pop into view over empty lists the instant the
    // debounce fires, which reads as broken results rather than a pending
    // search. The two searches run in parallel and don't necessarily resolve
    // together, so each section clears its own placeholder.
    const loadingHtml = `<li class="search-loading"><span class="spinner"></span>Searching…</li>`;
    videoResults.innerHTML = loadingHtml;
    channelResults.innerHTML = loadingHtml;

    const [videos, channels] = await Promise.all([
      api(`/feeds/search-videos?q=${encodeURIComponent(query)}`),
      api(`/feeds/search?q=${encodeURIComponent(query)}`),
    ]);

    if (videos.ok) renderVideoResults(videos.data);
    if (channels.ok) renderChannelResults(channels.data);
  }, 400);

  input.addEventListener("input", () => runSearch(input.value.trim()));

  videoResults.addEventListener("click", (event) => {
    const button = event.target.closest(".video-search-play");
    if (!button) return;
    playSearchedVideo(button.closest(".video-search-result").dataset, button);
  });

  channelResults.addEventListener("click", (event) => {
    const btn = event.target.closest(".btn-add-channel");
    if (!btn) return;
    addChannel(btn.dataset.channelUrl, btn);
  });
}
