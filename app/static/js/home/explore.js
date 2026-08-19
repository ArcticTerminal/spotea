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
        ? `<img class="search-result-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" loading="lazy" />`
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

/**
 * One channel as a shelf card — avatar, name, Add. Exported because the
 * onboarding wizard's per-genre shelves are the same component (see
 * home/onboarding.js): same markup, same shelf geometry, same Add button, so
 * a channel reads identically wherever the app offers one.
 *
 * The subscriber line is dropped entirely when there is no count rather than
 * left empty — onboarding's suggestions never carry one (see
 * services/genre_artists._as_channel_dict), and an empty <p> there would
 * reserve a line of space for nothing.
 */
export function channelCardHtml(channel) {
  const avatar = channel.thumbnail_url
    ? `<img class="shelf-channel-avatar" src="${escapeHtml(channel.thumbnail_url)}" alt="" loading="lazy" />`
    : `<span class="shelf-channel-avatar"></span>`;
  const subs =
    channel.subscriber_count != null
      ? `<p class="card-date">${escapeHtml(formatSubscribers(channel.subscriber_count))}</p>`
      : "";
  // Same two targets as a channel search result: the card previews, the
  // button follows without looking first. (Explore wires both; the wizard
  // wires only the button — a full-panel preview out from under that modal
  // would strand it half-finished.)
  return `
    <article
      class="card shelf-channel-card"
      data-channel-id="${escapeHtml(channel.channel_id)}"
      data-thumbnail-url="${escapeHtml(channel.thumbnail_url || "")}"
    >
      ${avatar}
      <div class="card-body">
        <h3 class="card-title" title="${escapeHtml(channel.title)}">${escapeHtml(channel.title)}</h3>
        ${subs}
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
 *  empty "Playlists" heading is worse than no heading. Exported for the
 *  onboarding wizard, which builds one per picked genre. */
export function shelfHtml(title, items, cardHtml) {
  if (!items.length) return "";
  return `
    <div class="shelf">
      <div class="shelf-header"><h3 class="shelf-title">${escapeHtml(title)}</h3></div>
      <div class="shelf-row">${items.map(cardHtml).join("")}</div>
    </div>
  `;
}

/** The nudge to go and list some interests. Shown above the shelves rather
 *  than instead of them: the charts and the mood shelf don't come from the
 *  interest list, so a profile that has listed none still has plenty to
 *  look at — it just isn't personal yet. */
function interestsPromptHtml() {
  return `
    <p class="muted">
      Tell Spotea what you're into — add a few interests in Settings and this fills up with
      channels, songs and playlists picked for you.
    </p>
    <button type="button" id="recommendations-open-settings" class="btn-primary">Add interests</button>
  `;
}

function renderRecommendations(data) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  const { contents, longForm } = splitByDuration(data.videos);
  const shelves = [
    // Interest-based first, because they're the ones about this profile.
    shelfHtml("Contents", contents, recVideoCardHtml),
    shelfHtml("Long form", longForm, recVideoCardHtml),
    shelfHtml("Channels", data.channels, channelCardHtml),
    shelfHtml("Playlists", data.playlists, recPlaylistCardHtml),
    // Then what everyone gets: this week's charts and one rotating mood.
    shelfHtml("Charts", data.charts, recPlaylistCardHtml),
    shelfHtml("Charting artists", data.chart_artists, channelCardHtml),
    // The only shelf whose heading comes from YouTube rather than from this
    // file, so the only one that needs escaping.
    data.mood
      ? shelfHtml(escapeHtml(data.mood.title), data.mood.playlists, recPlaylistCardHtml)
      : "",
  ].join("");

  const prompt = data.interests.length ? "" : interestsPromptHtml();
  body.innerHTML =
    prompt +
    (shelves ||
      `<p class="muted">Nothing came back this time. Try refreshing, or add a different interest in Settings.</p>`);
  // Shelves built here never pass through the fragment swap that normally
  // wires drag-scrolling, so they have to ask for it themselves.
  wireScrollers();
}

// The payload of the batch currently rendered, as JSON. Re-rendering shelves
// that came back identical replaces every card — and every <img> in them —
// with a fresh copy of itself, which is a visible flash of empty thumbnails
// in exchange for nothing. Identical is the normal case: the server only
// rebuilds a batch once the profile's chosen refresh interval has elapsed
// (see services/recommendations.py), so most re-checks answer with exactly
// what is already on screen.
let renderedPayload = null;

// The load currently in flight, so a second caller joins it instead of
// paying for its own round trip. The tab-activation check in particular
// fires while the boot fetch is still running on any slow first load.
let inFlight = null;

/**
 * Fetches the current batch and swaps the shelves in only if it differs from
 * what is already rendered.
 *
 * `placeholder` is the only thing that ever puts a loading line into the
 * panel, and only two callers pass it: the boot fetch (nothing is on screen
 * yet, and the panel is usually not even the visible tab) and the app-wide
 * Refresh. Opening Explore deliberately never does — the batch is fetched in
 * the background at boot and re-checked quietly afterwards, so the tab is
 * something you enter, not something you wait on.
 */
async function loadRecommendations({ force = false, placeholder = false } = {}) {
  const body = document.getElementById("recommendations-body");
  if (!body) return;
  if (inFlight && !force) return inFlight;

  if (placeholder) {
    body.innerHTML = `<p class="search-loading"><span class="spinner"></span>Finding things you might like…</p>`;
  }

  const load = (async () => {
    const { ok, data } = await api(force ? "/recommendations/refresh" : "/recommendations", {
      method: force ? "POST" : "GET",
      errorMessage: "Could not load recommendations",
    });

    if (!ok) {
      // Only when this call is what put the placeholder there — a background
      // re-check that fails leaves the shelves it could not improve on, which
      // is the right outcome and needs no announcement.
      if (placeholder) {
        body.innerHTML = `<p class="muted">Couldn't reach YouTube for recommendations just now.</p>`;
      }
      return;
    }

    const payload = JSON.stringify(data);
    if (payload === renderedPayload) return;
    renderedPayload = payload;
    renderRecommendations(data);
  })();

  if (!force) inFlight = load;
  try {
    await load;
  } finally {
    if (inFlight === load) inFlight = null;
  }
}

/**
 * Re-fetches in the background, swapping the shelves in only if they changed.
 *
 * Called after the interest list is saved (see home/settings.js, which the
 * onboarding wizard's genre step also goes through) and by that wizard just
 * before it closes. Both are moments the cached batch became an answer to a
 * question nobody asked — it is keyed to the interest list, so a changed list
 * means the next read rebuilds it, and a rebuild is several live YouTube
 * searches. Doing it here means that cost is paid while the user is still
 * somewhere else, rather than by whoever opens Explore next and has to sit in
 * front of a spinner for it.
 */
export function reloadRecommendations() {
  return loadRecommendations();
}

/**
 * Rebuilds the shelves now, ignoring the cache. There is no dedicated button
 * for this in Explore: it's what the app-wide "Refresh feeds" control calls
 * (wired in pages/index.js), so one press means "go and look at everything
 * again" rather than the tab carrying a second, competing refresh of its own.
 */
export async function refreshRecommendations() {
  await loadRecommendations({ force: true, placeholder: true });
}

export function setupRecommendations() {
  const body = document.getElementById("recommendations-body");
  if (!body) return;

  // Fetched as soon as the app loads, not deferred until Explore is opened —
  // the shelves are ready to show the first time someone switches to the tab
  // instead of making that switch pay for the YouTube round trip. The
  // placeholder goes into a panel that is almost always the hidden tab; it is
  // there for the one case where it isn't (a #explore deep link on a cold
  // cache), so that tab has something to say for itself while the first batch
  // is being built.
  loadRecommendations({ placeholder: true });

  // Re-checked on every later switch to Explore too, so a profile that keeps
  // the tab around gets a fresh batch once its chosen interval elapses
  // without needing a reload. Never with a placeholder, and never re-rendering
  // an unchanged batch: entering the tab should show what's there, not blank
  // it out and rebuild it in front of the person who just arrived.
  onTabActivated((tab) => {
    if (tab === "explore") loadRecommendations();
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

    const channelCard = event.target.closest(".shelf-channel-card");
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
