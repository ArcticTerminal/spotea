// The in-page player: a full "now playing" overlay plus a mini bar that stays
// visible while it's collapsed. Every surface opens tracks through this —
// Home's shelves here, the channel/playlist detail panel and Explore's
// results from home/detail.js and home/explore.js respectively — since
// there's no longer a separate standalone player page for any of them to
// navigate to instead.
//
// player.js's setupPlayer/prepareAudio/setupMediaSession/setupFavorite run
// against this markup unmodified (it renders the same _player_controls.html
// partial); everything here is the glue specific to reusing that DOM across
// several tracks in one page load instead of once per load.

import { api, formatDuration, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { paintRange, prepareAudio } from "../player.js";
import { clearResumeState, readResumeState } from "../resume.js";

function expandPlayer() {
  document.getElementById("player-overlay").hidden = false;
}

function collapsePlayer() {
  document.getElementById("player-overlay").hidden = true;
}

function syncMiniPlayerInfo(data) {
  document.getElementById("mini-player-title").textContent = data.title;
  document.getElementById("mini-player-channel").textContent = data.channel_title || "";
  const img = document.getElementById("mini-player-art-img");
  if (data.thumbnail_url) {
    img.src = data.thumbnail_url;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

export async function openPlayer(contentId, { expanded = true } = {}) {
  contentId = String(contentId);
  const root = document.getElementById("player-root");
  const audio = document.getElementById("audio");

  if (root.dataset.contentId === contentId) {
    // Same track already loaded — just surface it, don't touch playback.
    expandPlayer();
    return;
  }

  // Switching tracks (or starting fresh): stop whatever's currently loaded
  // immediately, rather than leaving it playing underneath the new track's
  // own "Downloading audio…" state until that one's ready.
  audio.pause();
  const seekBar = document.getElementById("seek-bar");
  seekBar.value = 0;
  document.getElementById("current-time").textContent = "0:00";
  paintRange(seekBar);
  document.getElementById("mini-player-progress-fill").style.width = "0%";

  const { ok, data } = await api(`/content/${contentId}`);
  if (!ok) {
    showToast("Could not load this track");
    // If this call came from resumeOverlayIfNeeded, the sessionStorage record
    // it read is exactly what just failed to load (e.g. a profile switch made
    // the old content id inaccessible) — consumeResumeState only ever clears
    // it on a *successful* startPlayback, so without this a permanently
    // invalid record would re-trigger this same failure on every page load.
    clearResumeState();
    return;
  }

  document.querySelector(".player-title").textContent = data.title;
  document.querySelector(".player-channel").textContent = data.channel_title || "";
  const artImg = document.getElementById("player-art-img");
  if (data.thumbnail_url) {
    artImg.src = data.thumbnail_url;
    artImg.hidden = false;
  } else {
    artImg.removeAttribute("src");
    artImg.hidden = true;
  }
  document.getElementById("duration-time").textContent = data.duration_seconds
    ? formatDuration(data.duration_seconds)
    : "0:00";

  const favBtn = document.getElementById("favorite-btn");
  favBtn.dataset.contentId = data.id;
  favBtn.dataset.favorite = String(data.is_favorite);
  favBtn.classList.toggle("is-on", data.is_favorite);
  favBtn.setAttribute("aria-pressed", String(data.is_favorite));
  favBtn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

  root.dataset.contentId = String(data.id);
  root.dataset.status = data.status;
  root.dataset.stream = `/content/${data.id}/stream`;

  syncMiniPlayerInfo(data);

  // setupMediaSession (player.js) only reads the DOM once, at page-load time
  // — on index.html that's before any track has ever been opened, so it can't
  // be what keeps lock-screen/notification metadata current across repeated
  // openPlayer() calls. This has to do it explicitly, every time.
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: data.title,
      artist: data.channel_title || "",
      artwork: data.thumbnail_url ? [{ src: data.thumbnail_url }] : [],
    });
  }

  // The mini bar always surfaces — only whether the full "now playing" view
  // is what's on top depends on the caller (resumeOverlayIfNeeded passes
  // expanded: false to put a track back exactly how it was left).
  document.getElementById("player-overlay").hidden = !expanded;
  document.getElementById("mini-player").hidden = false;
  document.body.classList.add("has-mini-player");

  prepareAudio(audio, () => {
    // The server records the play when /stream is requested, which only
    // happens after audio.src is assigned — refreshing right here would race
    // it and re-render shelves that don't know about this play yet.
    // loadedmetadata fires once the first bytes are back, by which point the
    // server has already written last_played_at.
    audio.addEventListener("loadedmetadata", refreshFragments, { once: true });
  });
}

export function closePlayer() {
  const audio = document.getElementById("audio");
  audio.pause();
  audio.removeAttribute("src");
  audio.load();

  const root = document.getElementById("player-root");
  root.dataset.contentId = "";
  root.dataset.status = "";
  root.dataset.stream = "";

  document.getElementById("player-overlay").hidden = true;
  document.getElementById("mini-player").hidden = true;
  document.body.classList.remove("has-mini-player");

  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  }
}

export function setupPlayerOverlay() {
  const overlay = document.getElementById("player-overlay");
  if (!overlay) return;

  const audio = document.getElementById("audio");
  const miniPlayBtn = document.getElementById("mini-player-playpause");
  const miniIconPlay = document.getElementById("mini-icon-play");
  const miniIconPause = document.getElementById("mini-icon-pause");

  const syncMiniIcon = () => {
    miniIconPlay.toggleAttribute("hidden", !audio.paused);
    miniIconPause.toggleAttribute("hidden", audio.paused);
    miniPlayBtn.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
  };

  // Thin passive progress line along the mini-bar's top edge — not
  // interactive, just a glance-able sense of how far into the track you are
  // without expanding the overlay.
  const miniProgressFill = document.getElementById("mini-player-progress-fill");
  const syncMiniProgress = () => {
    const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    miniProgressFill.style.width = `${pct}%`;
  };

  miniPlayBtn.addEventListener("click", () => {
    if (audio.paused) audio.play().catch(() => {});
    else audio.pause();
  });
  audio.addEventListener("play", syncMiniIcon);
  audio.addEventListener("pause", syncMiniIcon);
  audio.addEventListener("ended", syncMiniIcon);
  audio.addEventListener("timeupdate", syncMiniProgress);
  audio.addEventListener("loadedmetadata", syncMiniProgress);

  document.getElementById("mini-player-expand").addEventListener("click", expandPlayer);
  document.getElementById("overlay-collapse-btn").addEventListener("click", (event) => {
    event.preventDefault();
    collapsePlayer();
  });
  document.getElementById("mini-player-close").addEventListener("click", closePlayer);

  // Home's shelves only — Library's grid links to channels/playlists, not
  // tracks (home/library.js handles those, via home/detail.js), and
  // Explore's results and the detail panel's track rows route through
  // playSearchedVideo and home/detail.js respectively instead.
  const homeTab = document.getElementById("tab-home");
  if (!homeTab) return;

  homeTab.addEventListener("click", (event) => {
    // Let ctrl/cmd/shift-click and middle-click behave natively (open the
    // standalone /player/{id} page in a new tab) instead of hijacking them.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    if (event.target.closest(".btn-save")) return; // content-actions.js owns this

    const link = event.target.closest("a");
    if (!link) return;
    const card = event.target.closest(".card");
    if (!card) return;

    event.preventDefault();
    openPlayer(card.dataset.contentId);
  });
}

// Unlike player.html (whose #player-root always has a server-rendered content
// id, so prepareAudio's own resume logic just works after the bfcache-driven
// reload), the overlay starts every fresh page load closed and empty — it has
// to be explicitly reopened before there's anything for that logic to attach to.
export function resumeOverlayIfNeeded() {
  const root = document.getElementById("player-root");
  if (!root || root.dataset.contentId) return;
  const saved = readResumeState();
  // wasExpanded !== false (not just "if true") so an older resume record
  // written before this flag existed still defaults to expanded.
  if (saved?.contentId) openPlayer(saved.contentId, { expanded: saved.wasExpanded !== false });
}
