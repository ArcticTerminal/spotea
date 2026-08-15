// The audio player itself. Drives the in-page overlay (_player_overlay.html,
// which renders _player_controls.html) — its only caller.

import { api, formatDuration, showToast } from "./core.js";
import { refreshFragments } from "./fragments.js";
import { consumeResumeState } from "./resume.js";

const SKIP_SECONDS = 15;

// A download that works settles in roughly two seconds end to end, so a flat
// 1500ms poll — which is what this used to be — spent most of a second of
// dead air after the file was already on disk. Start tight and back off, so
// the common case feels immediate without a genuinely slow download turning
// into a request every 300ms for minutes on end.
// Poll tightly through the window where downloads actually land, then back
// off. Measured end to end, server side: ~1.8s when android_vr resolves a URL
// YouTube honours, ~3.5s when the ladder falls through to tv_simply.
//
// The schedule this replaces ramped 250ms up to 2500ms, which put its widest
// gaps exactly where those two land — a 1s gap between 2s and 3s, a 1.5s gap
// between 3s and 4.5s. A file ready at 3.2s wasn't picked up until 4.5s.
// Across the plausible range the average dead air was ~700ms: more than a
// third of the wait was the client simply not asking yet, after the audio was
// already on disk.
//
// 200ms costs 20 requests over four seconds, each a single indexed SQLite
// read on localhost. That is nothing next to the two thirds of a second it
// gives back, and the tight window is bounded so a genuinely slow download
// doesn't keep it up for minutes.
const POLL_TIGHT_MS = 200;
const POLL_TIGHT_UNTIL_MS = 4000;
const POLL_RELAXED_MS = 500;
const POLL_RELAXED_UNTIL_MS = 12000;
const POLL_STEADY_MS = 2000;

// Driven by elapsed time rather than a step counter, so a slow response
// can't shift the whole schedule out from under the window it's aimed at.
function nextPollDelay(elapsedMs) {
  if (elapsedMs < POLL_TIGHT_UNTIL_MS) return POLL_TIGHT_MS;
  if (elapsedMs < POLL_RELAXED_UNTIL_MS) return POLL_RELAXED_MS;
  return POLL_STEADY_MS;
}

// There used to be a 3s "stall watchdog" here: if no byte progress had shown
// up by then it POSTed .../download/restart, up to three rounds, and showed
// "(attempt 2 of 3)" in the status text. It was making things worse, not
// better, and the whole mechanism is gone:
//
//   - 3s was shorter than a healthy attempt. Resolving a URL takes 1.4-3s
//     and produces no byte progress at all, so the watchdog fired on
//     downloads that were working.
//   - It couldn't cancel what it abandoned. yt-dlp can't be interrupted, so
//     a restart left the old attempt running and started a second one beside
//     it — two, then three, concurrent yt-dlp runs per play, all writing the
//     same .part file (one play in the logs died on "Unable to rename file").
//   - It threw away successes. The abandoned attempts often finished fine,
//     but a superseded generation's result was discarded on arrival, so the
//     user waited for a later attempt to redo work already done. That is
//     exactly why "the third attempt" appeared to be the one that worked.
//
// Retrying now lives entirely in downloader.py's ladder, which doesn't have
// to guess: it sees the failure itself, in ~1.4s, and moves to the next
// client immediately. This side just polls.

// prepareAudio() can be called repeatedly for different tracks in the same
// page load (switching tracks in the overlay) — this tracks the in-flight
// download-status poll across those calls so a later call can cancel a
// still-running earlier one instead of leaving it to eventually hijack
// playback once its download finishes.
let activePollTimer = null;

// Identifies the prepareAudio call that owns the current poll chain. The
// chain re-arms itself with setTimeout rather than running on a fixed
// setInterval, so "is this still the live track?" has to be checked between
// ticks — a stale chain that kept going would eventually see its own old
// track go ready and hijack playback.
let activePollToken = null;

// Same problem, for the visibilitychange listener prepareAudio registers:
// without tracking and removing the previous call's listener, every track
// that was ever mid-download during this page session leaves a permanent
// zombie handler on document. Later, any visibilitychange fires all of them
// — and a stale one whose track has since finished downloading server-side
// would call its own startPlayback() and hijack the audio element back to
// that old track, regardless of what's actually loaded now.
let activeVisibilityHandler = null;

// Range inputs can't style their "already played" portion natively, so paint it
// with a gradient that tracks the current value.
export function paintRange(input) {
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--fill", `${pct}%`);
}

export function setupPlayer() {
  const audio = document.getElementById("audio");
  if (!audio) return;

  const playBtn = document.getElementById("play-pause");
  const iconPlay = document.getElementById("icon-play");
  const iconPause = document.getElementById("icon-pause");
  const seek = document.getElementById("seek-bar");
  const currentTimeEl = document.getElementById("current-time");
  const durationEl = document.getElementById("duration-time");
  const volume = document.getElementById("volume-bar");
  const muteBtn = document.getElementById("mute-btn");
  const iconVolume = document.getElementById("icon-volume");
  const iconMuted = document.getElementById("icon-muted");

  let scrubbing = false;

  // These icons are <svg>, i.e. SVGElement — which has no `hidden` IDL
  // property (that lives on HTMLElement). Assigning `.hidden` on them silently
  // creates a plain JS property and never touches the attribute, so CSS
  // `[hidden]` never matches. toggleAttribute() is on Element and works here.
  function showIcon(el, visible) {
    el.toggleAttribute("hidden", !visible);
  }

  function syncPlayIcon() {
    showIcon(iconPlay, audio.paused);
    showIcon(iconPause, !audio.paused);
    playBtn.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
  }

  function syncMuteIcon() {
    const silent = audio.muted || audio.volume === 0;
    showIcon(iconVolume, !silent);
    showIcon(iconMuted, silent);
    muteBtn.setAttribute("aria-label", silent ? "Unmute" : "Mute");
  }

  playBtn.addEventListener("click", () => {
    if (audio.paused) audio.play().catch(() => showToast("Playback was blocked by the browser"));
    else audio.pause();
  });
  audio.addEventListener("play", syncPlayIcon);
  audio.addEventListener("pause", syncPlayIcon);
  audio.addEventListener("ended", syncPlayIcon);

  document.getElementById("back15").addEventListener("click", () => {
    audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
  });
  document.getElementById("fwd15").addEventListener("click", () => {
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
  });

  audio.addEventListener("loadedmetadata", () => {
    seek.max = audio.duration || 0;
    durationEl.textContent = formatDuration(audio.duration);
    paintRange(seek);
  });

  audio.addEventListener("timeupdate", () => {
    if (scrubbing) return;
    seek.value = audio.currentTime;
    currentTimeEl.textContent = formatDuration(audio.currentTime);
    paintRange(seek);
  });

  audio.addEventListener("error", () => showToast("Could not load the audio for this track"));

  seek.addEventListener("input", () => {
    scrubbing = true;
    currentTimeEl.textContent = formatDuration(Number(seek.value));
    paintRange(seek);
  });
  seek.addEventListener("change", () => {
    audio.currentTime = Number(seek.value);
    scrubbing = false;
  });

  volume.addEventListener("input", () => {
    audio.volume = Number(volume.value) / 100;
    audio.muted = false;
    paintRange(volume);
    syncMuteIcon();
  });

  muteBtn.addEventListener("click", () => {
    audio.muted = !audio.muted;
    syncMuteIcon();
  });

  // Space toggles playback, arrows scrub — as long as focus isn't in a control.
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, button, a")) return;
    if (event.code === "Space") {
      event.preventDefault();
      playBtn.click();
    } else if (event.code === "ArrowLeft") {
      audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
    } else if (event.code === "ArrowRight") {
      audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
    }
  });

  syncPlayIcon();
  syncMuteIcon();
  paintRange(seek);
  paintRange(volume);
  setupMediaSession(audio);
}

// Lock-screen/notification-shade transport controls and Bluetooth/headset
// buttons all route through this — without it, playback is only
// controllable while this tab is in the foreground.
function setupMediaSession(audio) {
  if (!("mediaSession" in navigator)) return;

  const title = document.querySelector(".player-title")?.textContent || "";
  const artist = document.querySelector(".player-channel")?.textContent || "";
  const artworkSrc = document.querySelector(".player-art img")?.src;

  navigator.mediaSession.metadata = new MediaMetadata({
    title,
    artist,
    artwork: artworkSrc ? [{ src: artworkSrc }] : [],
  });

  navigator.mediaSession.setActionHandler("play", () => audio.play().catch(() => {}));
  navigator.mediaSession.setActionHandler("pause", () => audio.pause());
  navigator.mediaSession.setActionHandler("seekbackward", () => {
    audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
  });
  navigator.mediaSession.setActionHandler("seekforward", () => {
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
  });
  navigator.mediaSession.setActionHandler("seekto", (details) => {
    if (details.seekTime != null) audio.currentTime = details.seekTime;
  });

  audio.addEventListener("play", () => {
    navigator.mediaSession.playbackState = "playing";
  });
  audio.addEventListener("pause", () => {
    navigator.mediaSession.playbackState = "paused";
  });

  const syncPositionState = () => {
    if (!audio.duration || Number.isNaN(audio.duration)) return;
    try {
      navigator.mediaSession.setPositionState({ duration: audio.duration, position: audio.currentTime });
    } catch (err) {
      /* Throws if position momentarily exceeds duration mid-seek; harmless to skip. */
    }
  };
  audio.addEventListener("loadedmetadata", syncPositionState);
  audio.addEventListener("timeupdate", syncPositionState);
}

// Browsers speculatively load links (prerender runs the page's JS) and a
// ctrl/cmd-clicked track can open a genuinely backgrounded new tab — either
// way, the page's JS runs before the user has actually looked at it. Since
// opening a track is what triggers a download, calling prepareAudio()
// without this guard would let mere prefetching or an unfocused background
// tab fill the user's disk. home/overlay.js's openPlayer() is the only
// caller now that player.html (which had its own page-load call to guard)
// is gone — wrapping there covers a real click just as harmlessly (the page
// is already visible by then, so this resolves immediately) as it covers
// the boot-time resume/deep-link paths that don't involve a click at all.
export function whenVisible(run) {
  if (document.prerendering) {
    document.addEventListener("prerenderingchange", () => whenVisible(run), { once: true });
    return;
  }
  if (document.visibilityState !== "visible") {
    const onChange = () => {
      if (document.visibilityState === "visible") {
        document.removeEventListener("visibilitychange", onChange);
        run();
      }
    };
    document.addEventListener("visibilitychange", onChange);
    return;
  }
  run();
}

/**
 * Downloads are triggered by playing something, not by a separate button on
 * the card. If the audio isn't on disk yet, kick off the download here and
 * hold the transport disabled until the file is ready.
 *
 * onStart (optional) fires exactly once, right as audio.src is actually
 * assigned — the moment the server records this as a play (see
 * routers/content.py's stream_content, which sets last_played_at on that
 * same request). It's how the overlay knows to live-patch the Recently
 * Played shelf instead of leaving it stuck at whatever it was at page load.
 */
export async function prepareAudio(audio, onStart) {
  const root = document.getElementById("player-root");
  const prepare = document.getElementById("prepare-state");
  const prepareText = document.getElementById("prepare-text");
  const transport = document.querySelector(".transport");
  const streamUrl = root.dataset.stream;
  const contentId = root.dataset.contentId;

  // Both only matter because this can run more than once per page load — the
  // overlay calls it again for each new track. Without clearing the
  // previous call's poll, a still-downloading earlier track can finish later
  // and hijack playback out from under whatever's loaded now. Without
  // resetting the error styling, a track opened after an earlier one failed
  // would inherit its stale "Download failed" look.
  stopPolling();
  prepare.classList.remove("is-error");
  prepare.querySelector(".spinner").hidden = false;

  const startPlayback = () => {
    prepare.hidden = true;
    transport.classList.remove("is-disabled");
    audio.src = streamUrl;
    if (onStart) onStart();

    const resume = consumeResumeState(contentId);
    if (resume) {
      audio.addEventListener("loadedmetadata", () => { audio.currentTime = resume.currentTime; }, { once: true });
    }
    if (!resume || resume.wasPlaying) {
      audio.play().catch(() => {
        /* Autoplay may be blocked; the play button still works. */
      });
    }
  };

  const fail = (message) => {
    prepare.hidden = false;
    prepare.classList.add("is-error");
    prepare.querySelector(".spinner").hidden = true;
    prepareText.textContent = message;
    transport.classList.add("is-disabled");
  };

  if (root.dataset.status === "ready") {
    startPlayback();
    return;
  }

  prepare.hidden = false;
  transport.classList.add("is-disabled");
  prepareText.textContent = "Preparing audio…";

  if (root.dataset.status !== "downloading") {
    // 409 just means another tab already started it; keep polling either way.
    const { ok, status } = await api(`/content/${contentId}/download`, { method: "POST" });
    if (!ok && status !== 409) {
      fail("Could not start the download");
      return;
    }
  }

  // The download itself is a server-side background task, independent of
  // whether this tab can currently reach the server — a single missed poll
  // (a Wi-Fi blip, a backgrounded mobile tab getting its timers/network
  // throttled, a momentary server hiccup) doesn't mean the download failed,
  // just that this one check-in did. Only give up after several consecutive
  // misses; a lone one is silently retried on the next tick.
  const MAX_CONSECUTIVE_POLL_FAILURES = 4;
  let consecutiveFailures = 0;

  const checkStatus = async () => {
    // This track may no longer be the one loaded (a later openPlayer() call
    // superseded it) even if this call's timer/listener somehow still fired —
    // belt-and-suspenders alongside stopPolling above.
    if (root.dataset.contentId !== contentId) return;

    const { ok, data } = await api(`/content/${contentId}/status`);
    if (!ok) {
      consecutiveFailures += 1;
      if (consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        stopPolling();
        fail("Lost connection while downloading");
      }
      return;
    }
    consecutiveFailures = 0;

    if (data.status === "ready") {
      stopPolling();
      root.dataset.status = "ready";
      startPlayback();
    } else if (data.status === "error") {
      stopPolling();
      // A 403 here means YouTube resolved a media URL and then refused it.
      // By the time this shows, downloader.py has already been through every
      // rung of its ladder — android_vr and then tv_simply twice — so trying
      // again right now is worth a shot but not something to promise.
      const refused = data.error_message && /\b403\b|Forbidden/i.test(data.error_message);
      fail(refused ? "YouTube wouldn't serve this track — try again" : "Download failed");
    } else if (data.phase === "converting") {
      prepareText.textContent = "Converting…";
    } else if (data.phase === "downloading") {
      // The percentage is absent when YouTube serves no length to divide by,
      // which is a reason to drop the number — not to say nothing and leave
      // the text on the previous phase.
      prepareText.textContent =
        data.progress_percent != null ? `Downloading audio… ${data.progress_percent}%` : "Downloading audio…";
    } else if (data.phase === "extracting") {
      // The 1.4-3s where the server is resolving a URL YouTube will honour.
      // No bytes move yet, so without this the text would sit on "Preparing
      // audio…" for the entire slowest part of a play.
      prepareText.textContent = "Finding audio…";
    }
  };

  const pollToken = {};
  activePollToken = pollToken;
  const startedAt = Date.now();

  const poll = async () => {
    if (activePollToken !== pollToken) return;
    await checkStatus();
    if (activePollToken !== pollToken) return; // checkStatus stopped us, or a newer track took over
    activePollTimer = setTimeout(poll, nextPollDelay(Date.now() - startedAt));
  };
  activePollTimer = setTimeout(poll, nextPollDelay(0));

  // Mobile browsers throttle/suspend timers for a backgrounded tab, so the
  // interval above may not have ticked in a while by the time the user
  // switches back — check in immediately instead of waiting for the next
  // scheduled tick.
  activeVisibilityHandler = () => {
    if (document.visibilityState === "visible") checkStatus();
  };
  document.addEventListener("visibilitychange", activeVisibilityHandler);
}

// Stops both the poll and the visibilitychange check-in — anything that ends
// a track's polling (ready, error, or a new track superseding it) needs both
// gone. Leaving the visibilitychange listener behind after the timer is
// cleared turns it into a zombie: the next foreground/background cycle would
// still fire it, see "ready" again, and call startPlayback() a second time —
// restarting a track that was already playing fine from 0:00.
function stopPolling() {
  activePollToken = null;
  if (activePollTimer) {
    clearTimeout(activePollTimer);
    activePollTimer = null;
  }
  if (activeVisibilityHandler) {
    document.removeEventListener("visibilitychange", activeVisibilityHandler);
    activeVisibilityHandler = null;
  }
}

export function setupFavorite() {
  const btn = document.getElementById("favorite-btn");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    const on = btn.dataset.favorite === "true";
    btn.disabled = true;
    try {
      const { ok, data } = await api(`/content/${btn.dataset.contentId}/favorite`, {
        method: on ? "DELETE" : "POST",
        errorMessage: "Could not update favorite",
      });
      if (!ok) return;

      btn.dataset.favorite = String(data.is_favorite);
      btn.classList.toggle("is-on", data.is_favorite);
      btn.setAttribute("aria-pressed", String(data.is_favorite));
      btn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

      refreshFragments();
    } finally {
      btn.disabled = false;
    }
  });
}
