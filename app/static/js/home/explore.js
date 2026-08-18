// Explore: one search box over both songs and channels, and the
// interest-based "For you" shelves under it.
//
// What happens when you act on a result lives in home/remote.js (playing a
// song, following a channel) or in the detail panel (opening a playlist or an
// unfollowed channel drills into home/detail.js's yt-playlist/yt-channel
// kinds, rather than Explore growing a track list of its own).
//
// The tab shows one of two regions at a time: the browse/recommendations
// panel by default, and the search results while there's a query.

import { api, debounce, escapeHtml, formatDuration } from "../core.js";
import { openDetail } from "./detail.js";
import { wireScrollers } from "./library.js";
import { followChannel, playRemoteVideo } from "./remote.js";
import { activate, onTabActivated } from "./tabs.js";

function formatSubscribers(count) {
  if (count == null) return "";
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M subscribers`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}K subscribers`;
  return `${count} subscribers`;
}

export function renderChannelResults(results, containerId = "channel-search-results") {
  const list = document.getElementById(containerId);
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
      // Two targets on one row: the row itself previews the channel (its
      // latest uploads, in the detail panel), the button follows it outright
      // for when you already know what you're adding.
      return `
        <li
          class="search-result search-result-channel"
          data-channel-id="${escapeHtml(r.channel_id)}"
          data-thumbnail-url="${escapeHtml(r.thumbnail_url || "")}"
        >
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

// Rows, not a whole list, so the caller owns the empty state.
function videoRowsHtml(results) {
  return results
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

// ---------------------------------------------------------------------------
// "For you": interest-based recommendations
// ---------------------------------------------------------------------------

// Every card here is a thing the user doesn't have yet, so none of them carry
// a content id, a save button or a download badge — they reuse .card/.thumb/
// .card-body purely for the shelf geometry Home already defines.

function recVideoCardHtml(video) {
  const thumb = video.thumbnail_url
    ? `<img src="${escapeHtml(video.thumbnail_url)}" alt="" loading="lazy" />`
    : "";
  const duration =
    video.duration_seconds != null
      ? `<span class="duration-badge">${formatDuration(video.duration_seconds)}</span>`
      : "";
  return `
    <article
      class="card rec-card"
      data-video-id="${escapeHtml(video.video_id)}"
      data-title="${escapeHtml(video.title)}"
      data-thumbnail-url="${escapeHtml(video.thumbnail_url || "")}"
      data-duration-seconds="${video.duration_seconds ?? ""}"
      data-channel-title="${escapeHtml(video.channel_title || "")}"
    >
      <button type="button" class="thumb rec-play" aria-label="Play ${escapeHtml(video.title)}">
        ${thumb}${duration}
      </button>
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(video.title)}">${escapeHtml(video.title)}</h3>
        <p class="card-channel">${escapeHtml(video.channel_title || "")}</p>
      </div>
    </article>
  `;
}

function recChannelCardHtml(channel) {
  const avatar = channel.thumbnail_url
    ? `<img class="rec-channel-avatar" src="${escapeHtml(channel.thumbnail_url)}" alt="" loading="lazy" />`
    : `<span class="rec-channel-avatar"></span>`;
  // Same two targets as a channel search result: the card previews, the
  // button follows without looking first.
  return `
    <article
      class="card rec-channel-card"
      data-channel-id="${escapeHtml(channel.channel_id)}"
      data-thumbnail-url="${escapeHtml(channel.thumbnail_url || "")}"
    >
      ${avatar}
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(channel.title)}">${escapeHtml(channel.title)}</h3>
        <p class="card-date">${escapeHtml(formatSubscribers(channel.subscriber_count))}</p>
        <button type="button" class="btn-add-channel" data-channel-url="${escapeHtml(channel.channel_url)}">Add</button>
      </div>
    </article>
  `;
}

function recPlaylistCardHtml(playlist) {
  const thumb = playlist.thumbnail_url
    ? `<img src="${escapeHtml(playlist.thumbnail_url)}" alt="" loading="lazy" />`
    : "";
  return `
    <article
      class="card rec-card"
      data-playlist-id="${escapeHtml(playlist.playlist_id)}"
      data-title="${escapeHtml(playlist.title)}"
    >
      <button type="button" class="thumb" aria-label="Open ${escapeHtml(playlist.title)}">
        ${thumb}
      </button>
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(playlist.title)}">${escapeHtml(playlist.title)}</h3>
        <p class="card-channel">${escapeHtml(playlist.channel_title || "Playlist")}</p>
      </div>
    </article>
  `;
}

// Above this, a video goes on the "Long form" shelf instead of "Contents" —
// matches the ≤10-minute line the audit measured against (Music profile:
// 11/11 recommended videos ran over this; Podcast: 10/12, median 2:18:19).
// Naming is deliberately content-type-neutral rather than "Songs"/"Mixes":
// the Podcast profile's own interests (linux, devops) mean hour-long content
// there is the *correct* result, not noise to filter out — this only sorts
// it onto its own shelf instead of hiding it.
const LONG_FORM_THRESHOLD_SECONDS = 10 * 60;

/** Splits a video list into { contents, longForm } by LONG_FORM_THRESHOLD_
 *  SECONDS. A video with no known duration goes to "Contents" — most
 *  recommended content is short-form, and an unknown duration is rare
 *  (search results carry it from yt-dlp's flat extraction almost always). */
function splitByDuration(videos) {
  const contents = [];
  const longForm = [];
  for (const video of videos) {
    const isLongForm = video.duration_seconds != null && video.duration_seconds > LONG_FORM_THRESHOLD_SECONDS;
    (isLongForm ? longForm : contents).push(video);
  }
  return { contents, longForm };
}

/** A whole shelf, or nothing at all when that kind came back empty — an
 *  empty "Playlists" heading is worse than no heading. */
function recShelfHtml(title, items, cardHtml) {
  if (!items.length) return "";
  return `
    <div class="shelf">
      <div class="shelf-header"><h3 class="shelf-title">${title}</h3></div>
      <div class="shelf-row">${items.map(cardHtml).join("")}</div>
    </div>
  `;
}

function renderRecommendations(data) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  if (!data.interests.length) {
    body.innerHTML = `
      <p class="muted">
        Tell Spotea what you're into — add a few interests in Settings and this fills up with
        channels, songs and playlists worth trying.
      </p>
      <button type="button" id="recommendations-open-settings" class="btn-primary">Add interests</button>
    `;
    return;
  }

  const { contents, longForm } = splitByDuration(data.videos);
  const shelves = [
    recShelfHtml("Contents", contents, recVideoCardHtml),
    recShelfHtml("Long form", longForm, recVideoCardHtml),
    recShelfHtml("Channels", data.channels, recChannelCardHtml),
    recShelfHtml("Playlists", data.playlists, recPlaylistCardHtml),
  ].join("");

  body.innerHTML =
    shelves ||
    `<p class="muted">Nothing came back this time. Try refreshing, or add a different interest in Settings.</p>`;
  // Shelves built here never pass through the fragment swap that normally
  // wires drag-scrolling, so they have to ask for it themselves.
  wireScrollers();
}

/**
 * `quiet` skips the loading placeholder — used for the boot fetch and for
 * re-checking on later visits to Explore, where the shelves are already on
 * screen and swapping in a spinner over them would just be a flicker for
 * content that, most of the time, turns out unchanged (the GET route only
 * rebuilds once the profile's chosen refresh interval has actually elapsed;
 * see services/recommendations.py).
 */
async function loadRecommendations({ force = false, quiet = false } = {}) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  if (!quiet) {
    body.innerHTML = `<p class="search-loading"><span class="spinner"></span>Finding things you might like…</p>`;
  }

  const { ok, data } = await api(force ? "/recommendations/refresh" : "/recommendations", {
    method: force ? "POST" : "GET",
    errorMessage: "Could not load recommendations",
  });

  if (!ok) {
    if (!quiet) body.innerHTML = `<p class="muted">Couldn't reach YouTube for recommendations just now.</p>`;
    return;
  }
  recommendationsLoaded = true;
  renderRecommendations(data);
}

let recommendationsLoaded = false;

/**
 * Marks the shelves stale, so the next visit to Explore shows the loading
 * spinner instead of quietly swapping in the rebuilt batch. Called by the
 * interests editor (see home/settings.js) — without it, adding your first
 * interest and coming back here would still show the "no interests yet"
 * prompt until a reload (the re-check on tab entry would fetch the new
 * batch either way, but silently). Not an immediate fetch itself: interests
 * are only editable from Settings, so there's nothing on screen to update
 * yet.
 */
export function invalidateRecommendations() {
  recommendationsLoaded = false;
}

/**
 * Rebuilds the shelves now, ignoring the cache. There is no dedicated button
 * for this in Explore: it's what the app-wide "Refresh feeds" control calls
 * (wired in pages/index.js), so one press means "go and look at everything
 * again" rather than the tab carrying a second, competing refresh of its own.
 */
export async function refreshRecommendations() {
  await loadRecommendations({ force: true });
}

export function setupRecommendations() {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  // Fetched as soon as the app loads, not deferred until Explore is opened —
  // the shelves are ready to show the first time someone switches to the
  // tab instead of making that switch pay for the YouTube round trip. Quiet:
  // nothing is on screen yet either way, so there's nothing to show a
  // spinner over, and a toast for a boot-time hiccup nobody asked to see
  // would just be noise.
  loadRecommendations({ quiet: true });

  // Re-checked on every later switch to Explore too, so a profile that keeps
  // the tab around gets a fresh batch once its chosen interval elapses
  // without needing a reload. Quiet once something has already rendered —
  // only the very first check (racing the boot fetch above) shows the
  // spinner.
  onTabActivated((tab) => {
    if (tab === "explore") loadRecommendations({ quiet: recommendationsLoaded });
  });

  body.addEventListener("click", (event) => {
    if (event.target.closest("#recommendations-open-settings")) {
      activate("settings");
      document.getElementById("open-interests")?.click();
      return;
    }

    // Checked before the card itself: a channel card is clickable as a whole
    // (preview it) but carries its own Add button (follow it outright).
    const addButton = event.target.closest(".btn-add-channel");
    if (addButton) {
      followChannel(addButton.dataset.channelUrl, addButton);
      return;
    }

    const channelCard = event.target.closest(".rec-channel-card");
    if (channelCard) {
      openDetail("yt-channel", channelCard.dataset.channelId, { avatar: channelCard.dataset.thumbnailUrl || null });
      return;
    }

    // Song and playlist cards are clickable anywhere, not just on the button
    // in their artwork — the button is there for keyboard and screen-reader
    // users, and which dataset the card carries says what it is.
    const card = event.target.closest(".rec-card");
    if (!card) return;
    if (card.dataset.videoId) playRemoteVideo(card.dataset, card.querySelector(".rec-play"));
    else if (card.dataset.playlistId) openDetail("yt-playlist", card.dataset.playlistId);
  });
}

// One of Explore's two regions is visible at a time: the browse panel
// (recommendations) by default, the search results while there's a query.
function showExplorePanel(visibleId) {
  for (const panelId of ["explore-browse-panel", "explore-results-panel"]) {
    const panel = document.getElementById(panelId);
    if (panel) panel.hidden = panelId !== visibleId;
  }
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
      showExplorePanel("explore-browse-panel");
      videoResults.innerHTML = "";
      channelResults.innerHTML = "";
      return;
    }

    showExplorePanel("explore-results-panel");
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

    if (videos.ok) {
      videoResults.innerHTML =
        videoRowsHtml(videos.data) || `<li class="search-empty">No songs found</li>`;
    }
    if (channels.ok) renderChannelResults(channels.data);
  }, 400);

  input.addEventListener("input", () => runSearch(input.value.trim()));

  channelResults.addEventListener("click", (event) => {
    // Add first: the row around it opens a preview instead, and the button
    // sits inside that row.
    const btn = event.target.closest(".btn-add-channel");
    if (btn) {
      followChannel(btn.dataset.channelUrl, btn);
      return;
    }

    const row = event.target.closest(".search-result-channel");
    if (row?.dataset.channelId) {
      openDetail("yt-channel", row.dataset.channelId, { avatar: row.dataset.thumbnailUrl || null });
    }
  });

  videoResults.addEventListener("click", (event) => {
    const button = event.target.closest(".video-search-play");
    if (!button) return;
    playRemoteVideo(button.closest(".video-search-result").dataset, button);
  });
}
