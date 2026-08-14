// The audio player itself. Drives both the standalone /player/{id} page and
// Home/Library/Explore's in-page overlay — both render _player_controls.html,
// so the same element ids are present either way and nothing here needs to
// know which one it's running in.

import { api, formatDuration, showToast } from "./core.js";
import { refreshFragments } from "./fragments.js";
import { consumeResumeState } from "./resume.js";

const SKIP_SECONDS = 15;
const STATUS_POLL_MS = 1500;

// prepareAudio() used to only ever run once per page load (player.html
// renders exactly one track). It now also drives the overlay, which can call
// it repeatedly for different tracks in the same page — this tracks the
// in-flight download-status poll across those calls so a later call can
// cancel a still-running earlier one instead of leaving it to eventually
// hijack playback once its download finishes.
let activePollTimer = null;

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

  // Browsers speculatively load links (prerender runs the page's JS), so a card
  // link can open this page without the user ever clicking it. Since opening
  // the player is what triggers a download, kicking one off on plain page load
  // would let mere prefetching fill the user's disk. Wait until the page is
  // genuinely being viewed.
  //
  // Guarded on a real content id being present: true on player.html (Jinja
  // fills #player-root's dataset server-side), false on index.html's overlay
  // until a track is actually opened (see home/overlay.js's openPlayer, which
  // sets the dataset itself and calls prepareAudio directly once it does).
  if (document.getElementById("player-root").dataset.contentId) {
    whenVisible(() => prepareAudio(audio));
  }
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

function whenVisible(run) {
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
 * same request). player.html has nothing to do with this; it's how the
 * overlay knows to live-patch the Recently Played shelf instead of leaving
 * it stuck at whatever it was at page load.
 */
export async function prepareAudio(audio, onStart) {
  const root = document.getElementById("player-root");
  const prepare = document.getElementById("prepare-state");
  const prepareText = document.getElementById("prepare-text");
  const transport = document.querySelector(".transport");
  const streamUrl = root.dataset.stream;
  const contentId = root.dataset.contentId;

  // Both only matter once this can run more than once per page (player.html
  // never does; the overlay does, switching tracks). Without clearing the
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
  prepareText.textContent =
    root.dataset.status === "downloading" ? "Downloading audio…" : "Preparing audio…";

  if (root.dataset.status !== "downloading") {
    // 409 just means another tab already started it; keep polling either way.
    const { ok, status } = await api(`/content/${contentId}/download`, { method: "POST" });
    if (!ok && status !== 409) {
      fail("Could not start the download");
      return;
    }
    prepareText.textContent = "Downloading audio…";
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
      // 403s here are usually YouTube rate-limiting this server rather than
      // anything wrong with the video — downloader.py already retries a few
      // times with backoff before giving up, so if it's still failing after
      // that, waiting longer and trying again is the honest advice.
      const isRateLimited = data.error_message && /\b403\b|Forbidden/i.test(data.error_message);
      fail(isRateLimited ? "YouTube is rate-limiting downloads right now — try again in a bit" : "Download failed");
    } else if (data.phase === "converting") {
      prepareText.textContent = "Converting…";
    } else if (data.progress_percent != null) {
      prepareText.textContent = `Downloading audio… ${data.progress_percent}%`;
    }
  };

  activePollTimer = setInterval(checkStatus, STATUS_POLL_MS);

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
// a track's polling (ready, error, giving up, or a new track superseding it)
// needs both gone. Leaving the visibilitychange listener behind after the
// interval is cleared turns it into a zombie: the next foreground/background
// cycle would still fire it, see "ready" again, and call startPlayback() a
// second time — restarting a track that was already playing fine from 0:00.
function stopPolling() {
  if (activePollTimer) {
    clearInterval(activePollTimer);
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

// The player is reachable from many different pages (a channel, Favorites,
// Saved, a Home shelf) — unlike channel.html/content_list.html's back-link,
// which always means "back to Library", there's no single right fallback
// destination here. Real browser history is, so prefer it whenever there's a
// same-origin previous page to return to; the plain href="/" stays as the
// no-JS/no-referrer fallback (e.g. the player opened directly, a fresh tab).
export function setupBackLink() {
  const link = document.getElementById("player-back-link");
  if (!link) return;

  link.addEventListener("click", (event) => {
    if (document.referrer && new URL(document.referrer).origin === window.location.origin) {
      event.preventDefault();
      history.back();
    }
  });
}
