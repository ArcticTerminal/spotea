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
import { paintRange, prepareAudio, whenVisible } from "../player.js";
import { clearResumeState, readResumeState } from "../resume.js";
import {
  QUEUE_CHANGED,
  clearQueue,
  isShuffled,
  nextId,
  noteCurrent,
  peekNextId,
  peekPreviousId,
  previousId,
  toggleShuffle,
} from "./queue.js";

// How much of the current track has to have actually played before the next
// one is pulled down in the background (see setupPlayerOverlay's timeupdate
// handler for why it isn't immediate).
const PREFETCH_AFTER_SECONDS = 8;

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

  // Every route into the player lands here, so this is the one place that can
  // keep the queue pointer honest — including the routes that have nothing to
  // do with a queue (a Home shelf, an Explore result), which is exactly when
  // the queue has to be dropped rather than left to advance into a list the
  // user has moved on from. See queue.js's noteCurrent.
  noteCurrent(contentId);

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

  // Waits out a prerender or a ctrl/cmd-clicked background tab (see
  // player.js's whenVisible) — resolves immediately for a real click, since
  // the page is already visible by then.
  whenVisible(() => {
    prepareAudio(audio, () => {
      // The server records the play when /stream is requested, which only
      // happens after audio.src is assigned — refreshing right here would race
      // it and re-render shelves that don't know about this play yet.
      // loadedmetadata fires once the first bytes are back, by which point the
      // server has already written last_played_at.
      audio.addEventListener("loadedmetadata", refreshFragments, { once: true });
    });
  });
}

/**
 * Opens a track the queue handed us, keeping the overlay however the user
 * left it. A fixed `expanded: true` would be right for a tapped next button
 * and wrong for everything else — auto-advance and the lock-screen/headset
 * controls both fire while the app is collapsed to the mini bar or not on
 * screen at all, and throwing the full "now playing" view up in those cases
 * hijacks whatever the user was actually doing.
 */
function playFromQueue(contentId) {
  if (contentId == null) return;
  openPlayer(contentId, { expanded: !document.getElementById("player-overlay").hidden });
}

/**
 * Mirrors the queue into every control that depends on it: the overlay's
 * previous/next buttons, the mini bar's skip button, the shuffle toggle's
 * on-state, and the lock-screen transport. Driven by queue.js's
 * QUEUE_CHANGED event rather than called from each mutation site, so a new
 * way of changing the queue can't forget to update the UI.
 */
function syncQueueControls() {
  const hasNext = peekNextId() !== null;
  const hasPrevious = peekPreviousId() !== null;

  document.getElementById("next-track").disabled = !hasNext;
  document.getElementById("prev-track").disabled = !hasPrevious;
  document.getElementById("mini-player-next").hidden = !hasNext;

  const shuffleBtn = document.getElementById("player-shuffle");
  shuffleBtn.classList.toggle("is-on", isShuffled());
  shuffleBtn.setAttribute("aria-pressed", String(isShuffled()));

  if (!("mediaSession" in navigator)) return;
  // Nulled rather than left registered when there's nowhere to skip to: the
  // handler's presence is what decides whether the OS draws the button at
  // all, so a no-op handler would put a dead control on the lock screen.
  // Wrapped because a browser that doesn't implement these actions throws
  // rather than ignoring them, which would take the rest of this sync with it.
  try {
    navigator.mediaSession.setActionHandler("nexttrack", hasNext ? () => playFromQueue(nextId()) : null);
    navigator.mediaSession.setActionHandler(
      "previoustrack",
      hasPrevious ? () => playFromQueue(previousId()) : null
    );
  } catch (err) {
    /* Not supported here — the in-page transport still works. */
  }
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

  // Dismissing the player dismisses what it was working through. Leaving the
  // queue loaded would mean the next single track opened from a Home shelf
  // silently inherited a list the user has already closed.
  clearQueue();

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

  // A track running out is the whole point of having a queue; with none
  // loaded nextId() is null and playback simply stops, as it always did.
  audio.addEventListener("ended", () => playFromQueue(nextId()));

  // Downloads are triggered by playing something, so without this every
  // track change in a queue costs the same 2-4s "Preparing audio…" wait as
  // the first one — on a "Play all" that's a gap between every pair of
  // tracks. Fetching one ahead covers it, since a track that's already on
  // disk starts instantly.
  //
  // Deliberately not fired at the moment playback starts: skipping quickly
  // through a queue would then kick off a download per track passed over.
  // Waiting until the current track has genuinely been listened to for a
  // few seconds means a skipped-past track never pulls its successor down,
  // while a track anyone is actually hearing still leaves minutes of lead
  // time. Server-side the request is a no-op for anything already on disk
  // (see routers/content.py's start_download).
  let prefetchedFor = null;
  audio.addEventListener("timeupdate", () => {
    const playing = document.getElementById("player-root").dataset.contentId;
    if (!playing || prefetchedFor === playing) return;
    if (audio.currentTime < PREFETCH_AFTER_SECONDS) return;
    prefetchedFor = playing;
    const upcoming = peekNextId();
    if (upcoming != null) api(`/content/${upcoming}/download`, { method: "POST" });
  });

  document.getElementById("prev-track").addEventListener("click", () => playFromQueue(previousId()));
  document.getElementById("next-track").addEventListener("click", () => playFromQueue(nextId()));
  document.getElementById("mini-player-next").addEventListener("click", () => playFromQueue(nextId()));
  document.getElementById("player-shuffle").addEventListener("click", () => toggleShuffle());

  document.addEventListener(QUEUE_CHANGED, syncQueueControls);
  syncQueueControls();

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
