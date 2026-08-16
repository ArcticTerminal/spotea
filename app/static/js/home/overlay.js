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
import {
  activeAudio,
  applyNowPlayingMetadata,
  onPlayerEvent,
  paintRange,
  prepareAudio,
  reportPlayback,
  whenVisible,
} from "../player.js";
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

// How the prefetch follows its own download to completion, so the handoff
// knows whether the next track is actually playable before it gets there
// (see cacheUpcoming). Bounded well inside the length of a track — a
// download that hasn't landed by then won't be helped by asking again, and
// the handoff falls back to preparing it the ordinary way.
const UPCOMING_POLL_MS = 1500;
const UPCOMING_POLL_LIMIT = 20;

/**
 * The next track, fetched while the current one is still playing: its
 * metadata and its download status as of the last check.
 *
 * This exists so that a track ending doesn't have to ask the server anything
 * before it can start the next one. `ended` fires, and everything from there
 * to audio.play() — the queue pointer, the dataset, the artwork, the play
 * call itself — can run synchronously inside that one event, with no fetch
 * in the middle for a browser that's busy suspending the page to defer
 * indefinitely. That deferral is exactly what "it didn't move to the next
 * song until I opened the app again" was.
 */
let upcomingTrack = null;

// Caps openPlayer's auto-skip-on-failure (below) at this many failures in a
// row before it gives up instead of trying yet another track. Without a
// cap, a systemic hiccup — YouTube rate-limiting/bot-checking the IP, the
// PO token provider having a bad moment — reads as "every remaining track in
// the queue is broken" and the skip chain burns through all of them in
// seconds, each one running its own multi-attempt retry ladder against
// YouTube. That volume is itself what trips the bot check in the first
// place, which is how one bad track once took out an entire session: every
// track after it failed with the exact same "Sign in to confirm you're not
// a bot" error in under two minutes, not because they were all actually
// unavailable.
const MAX_CONSECUTIVE_AUTO_SKIPS = 3;
let consecutiveAutoSkipFailures = 0;

// A track YouTube has settled on refusing (see Content.is_unavailable) is a
// different thing entirely: skipping it costs one local lookup, hits YouTube
// zero times, and says nothing at all about whether the next one will work —
// so the reasoning behind the cap above simply doesn't apply. It gets a much
// looser limit of its own, there only so that a queue of nothing but
// unavailable tracks terminates rather than racing to the end of the list.
const MAX_CONSECUTIVE_UNAVAILABLE_SKIPS = 10;
let consecutiveUnavailableSkips = 0;

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

export async function openPlayer(contentId, { expanded = true, requireVisible = true } = {}) {
  contentId = String(contentId);
  const root = document.getElementById("player-root");

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
  //
  // Guarded because on an auto-advance there is nothing to stop — the track
  // ran out, so the element is already paused. Calling pause() there only
  // told the OS we were done with audio, at the one moment we most needed it
  // to think otherwise.
  if (!activeAudio().paused) activeAudio().pause();
  const seekBar = document.getElementById("seek-bar");
  seekBar.value = 0;
  document.getElementById("current-time").textContent = "0:00";
  paintRange(seekBar);
  document.getElementById("mini-player-progress-fill").style.width = "0%";

  // Taken, not read: the cached copy is only good for the one handoff it was
  // fetched for, and leaving it in place would let a later, unrelated open of
  // the same track run on however stale it had become by then.
  let data = null;
  if (upcomingTrack && upcomingTrack.id === contentId) {
    data = upcomingTrack.data;
    reportPlayback("handoff-cached", { contentId, status: data.status });
  }
  upcomingTrack = null;

  // Only when there was nothing prepared — this is the await the cache
  // exists to avoid, and reaching it is fine, just slower.
  if (!data) {
    const res = await api(`/content/${contentId}`);
    if (!res.ok) {
      showToast("Could not load this track");
      // If this call came from resumeOverlayIfNeeded, the sessionStorage
      // record it read is exactly what just failed to load (e.g. a profile
      // switch made the old content id inaccessible) — consumeResumeState
      // only ever clears it on a *successful* startPlayback, so without this
      // a permanently invalid record would re-trigger this same failure on
      // every page load.
      clearResumeState();
      return;
    }
    data = res.data;
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
  root.dataset.unavailable = String(data.is_unavailable === true);
  root.dataset.stream = `/content/${data.id}/stream`;

  syncMiniPlayerInfo(data);

  // setupMediaSession (player.js) only reads the DOM once, at page-load time
  // — on index.html that's before any track has ever been opened, so it can't
  // be what keeps lock-screen/notification metadata current across repeated
  // openPlayer() calls. This has to do it explicitly, every time. It runs
  // after the writes above because it reads the same DOM they just filled in.
  //
  // This publish happens in the silent gap before playback, which iOS may
  // simply drop; player.js re-publishes on `playing` for that reason. Both
  // are needed — the OS has to be told before the track starts (so the lock
  // screen isn't briefly showing the previous one) and again once it has.
  applyNowPlayingMetadata();

  // The mini bar always surfaces — only whether the full "now playing" view
  // is what's on top depends on the caller (resumeOverlayIfNeeded passes
  // expanded: false to put a track back exactly how it was left).
  document.getElementById("player-overlay").hidden = !expanded;
  document.getElementById("mini-player").hidden = false;
  document.body.classList.add("has-mini-player");

  const start = () => {
    prepareAudio(
      () => {
        // The server records the play when /stream is requested, which only
        // happens after audio.src is assigned — refreshing right here would
        // race it and re-render shelves that don't know about this play yet.
        // loadedmetadata fires once the first bytes are back, by which point
        // the server has already written last_played_at.
        activeAudio().addEventListener("loadedmetadata", refreshFragments, { once: true });
        consecutiveAutoSkipFailures = 0;
        consecutiveUnavailableSkips = 0;
      },
      (message, { permanent } = {}) => {
        // A queued track that fails to download (pulled from YouTube, gone
        // private, ...) used to just leave the transport stuck on "Download
        // failed" — most noticeably when this happens while the app is
        // backgrounded, since auto-advance into it is exactly how a
        // background "ended" handoff (below) reaches a broken track with no
        // one watching to hit "next". Skip past it instead, same as the
        // queue running out normally does nothing when there's no next id.
        //
        const upcoming = peekNextId();
        if (upcoming == null) return;

        if (permanent) {
          consecutiveUnavailableSkips += 1;
          if (consecutiveUnavailableSkips >= MAX_CONSECUTIVE_UNAVAILABLE_SKIPS) {
            showToast("Too many unavailable tracks in a row — stopping here");
            return;
          }
          showToast(`"${data.title}" isn't available on YouTube — skipping`);
          playFromQueue(nextId());
          return;
        }

        consecutiveAutoSkipFailures += 1;
        if (consecutiveAutoSkipFailures >= MAX_CONSECUTIVE_AUTO_SKIPS) {
          // See MAX_CONSECUTIVE_AUTO_SKIPS above — leave this one showing its
          // real error rather than trying yet another track.
          showToast("Several tracks in a row failed — stopping instead of skipping further");
          return;
        }
        showToast(`Couldn't play "${data.title}" — skipping to the next track`);
        playFromQueue(nextId());
      }
    );
  };

  // Waits out a prerender or a ctrl/cmd-clicked background tab (see
  // player.js's whenVisible) — resolves immediately for a real click, since
  // the page is already visible by then. Skipped entirely for a queue
  // handoff (requireVisible: false, set by playFromQueue below): a locked
  // screen or a backgrounded tab is `document.visibilityState !== "visible"`
  // exactly like an unopened prerender is, so without this every "ended" and
  // every lock-screen/headset next/previous tap while the app isn't on
  // screen would queue up behind a visibilitychange that only fires once the
  // user looks at the phone again — audio would just stop instead of
  // advancing. That's safe to skip here specifically because a queue handoff
  // can only happen after some earlier track already made it through this
  // same gate once for real (there is no queue, no "ended" event, and no
  // media-session next/previous handler until real playback has begun).
  if (requireVisible) whenVisible(start);
  else start();
}

/**
 * Pulls the next track down ahead of time and caches its metadata in
 * `upcomingTrack`, so the handoff to it doesn't have to wait on either.
 *
 * The download POST is the reason this exists at all: without it every track
 * change in a queue costs the same "Preparing audio…" wait as the first one.
 * The metadata fetch beside it and the follow-up polling are what let the
 * handoff skip its own `/content/{id}` round trip too.
 *
 * Runs while the current track is still playing, which is what makes the
 * polling affordable and reliable: the page is awake by definition, each
 * check is one indexed read on localhost, and it stops as soon as there's a
 * settled answer. Everything here is best-effort — a failure just means the
 * handoff prepares the track the ordinary way instead.
 */
async function cacheUpcoming(contentId) {
  const id = String(contentId);
  const [download, meta] = await Promise.all([
    api(`/content/${id}/download`, { method: "POST" }),
    api(`/content/${id}`),
  ]);
  if (!meta.ok) return;

  // The POST's answer is the more recent of the two, and the only one that
  // can say "already on disk" for a track that needed no download at all.
  const data = { ...meta.data };
  if (download.ok && download.data) {
    data.status = download.data.status;
    data.is_unavailable = download.data.is_unavailable === true;
  }
  upcomingTrack = { id, data };
  if (data.is_unavailable || data.status === "ready" || data.status === "error") return;

  for (let attempt = 0; attempt < UPCOMING_POLL_LIMIT; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, UPCOMING_POLL_MS));
    // Superseded (the queue moved on, or the handoff already took this) —
    // whatever comes back now belongs to nothing.
    if (upcomingTrack?.id !== id) return;
    const { ok, data: status } = await api(`/content/${id}/status`);
    if (!ok) continue;
    upcomingTrack.data.status = status.status;
    upcomingTrack.data.is_unavailable = status.is_unavailable === true;
    if (status.status === "ready" || status.status === "error") return;
  }
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
  openPlayer(contentId, {
    expanded: !document.getElementById("player-overlay").hidden,
    requireVisible: false,
  });
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
  const audio = activeAudio();
  audio.pause();
  audio.removeAttribute("src");
  audio.load();

  const root = document.getElementById("player-root");
  root.dataset.contentId = "";
  root.dataset.status = "";
  root.dataset.unavailable = "";
  root.dataset.stream = "";
  upcomingTrack = null;

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

  const miniPlayBtn = document.getElementById("mini-player-playpause");
  const miniIconPlay = document.getElementById("mini-icon-play");
  const miniIconPause = document.getElementById("mini-icon-pause");

  const syncMiniIcon = () => {
    const paused = activeAudio().paused;
    miniIconPlay.toggleAttribute("hidden", !paused);
    miniIconPause.toggleAttribute("hidden", paused);
    miniPlayBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
  };

  // Thin passive progress line along the mini-bar's top edge — not
  // interactive, just a glance-able sense of how far into the track you are
  // without expanding the overlay.
  const miniProgressFill = document.getElementById("mini-player-progress-fill");
  const syncMiniProgress = () => {
    const audio = activeAudio();
    const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    miniProgressFill.style.width = `${pct}%`;
  };

  miniPlayBtn.addEventListener("click", () => {
    const audio = activeAudio();
    if (audio.paused) audio.play().catch(() => {});
    else audio.pause();
  });
  onPlayerEvent("play", syncMiniIcon);
  onPlayerEvent("pause", syncMiniIcon);
  onPlayerEvent("ended", syncMiniIcon);
  onPlayerEvent("timeupdate", syncMiniProgress);
  onPlayerEvent("loadedmetadata", syncMiniProgress);

  // A track running out is the whole point of having a queue; with none
  // loaded nextId() is null and playback simply stops, as it always did.
  onPlayerEvent("ended", () => {
    const finished = document.getElementById("player-root").dataset.contentId;
    const next = nextId();
    // The first breadcrumb of a handoff, and the one that makes the rest
    // legible: everything after it in the log either happened in this same
    // event or didn't happen at all. See player.js's reportPlayback.
    reportPlayback("track-ended", { contentId: finished, next, prepared: upcomingTrack?.id ?? null });
    playFromQueue(next);
  });

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
  onPlayerEvent("timeupdate", () => {
    const playing = document.getElementById("player-root").dataset.contentId;
    if (!playing || prefetchedFor === playing) return;
    if (activeAudio().currentTime < PREFETCH_AFTER_SECONDS) return;
    prefetchedFor = playing;
    const upcoming = peekNextId();
    if (upcoming != null) cacheUpcoming(upcoming);
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
