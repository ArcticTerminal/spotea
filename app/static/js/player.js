const SKIP_SECONDS = 15;
const STATUS_POLL_MS = 1500;

// prepareAudio() used to only ever run once per page load (player.html
// renders exactly one track). It now also drives Home's player overlay
// (see app.js's openPlayer), which can call it repeatedly for different
// tracks in the same page — this tracks the in-flight download-status poll
// across those calls so a later call can cancel a still-running earlier one
// instead of leaving it to eventually hijack playback once its download
// finishes (see prepareAudio's own comment at the top of its body).
let _activePollTimer = null;

// Range inputs can't style their "already played" portion natively, so paint it
// with a gradient that tracks the current value.
function paintRange(input) {
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--fill", `${pct}%`);
}

function setupPlayer() {
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
  // until a track is actually opened (see app.js's openPlayer, which sets the
  // dataset itself and calls prepareAudio directly once it does).
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

// Downloads are triggered by playing something, not by a separate button on the
// card. If the audio isn't on disk yet, kick off the download here and hold the
// transport disabled until the file is ready.
async function prepareAudio(audio) {
  const root = document.getElementById("player-root");
  const prepare = document.getElementById("prepare-state");
  const prepareText = document.getElementById("prepare-text");
  const transport = document.querySelector(".transport");
  const streamUrl = root.dataset.stream;
  const contentId = root.dataset.contentId;

  // Both only matter once this can run more than once per page (player.html
  // never does; the Home overlay does, switching tracks). Without clearing
  // the previous call's poll, a still-downloading earlier track can finish
  // later and hijack playback out from under whatever's loaded now. Without
  // resetting the error styling, a track opened after an earlier one failed
  // would inherit its stale "Download failed" look before this call's own
  // state has a chance to say otherwise.
  if (_activePollTimer) {
    clearInterval(_activePollTimer);
    _activePollTimer = null;
  }
  prepare.classList.remove("is-error");
  prepare.querySelector(".spinner").hidden = false;

  const startPlayback = () => {
    prepare.hidden = true;
    transport.classList.remove("is-disabled");
    audio.src = streamUrl;
    audio.play().catch(() => {
      /* Autoplay may be blocked; the play button still works. */
    });
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
    try {
      const res = await fetch(`/content/${contentId}/download`, { method: "POST" });
      // 409 just means another tab already started it; keep polling either way.
      if (!res.ok && res.status !== 409) {
        fail("Could not start the download");
        return;
      }
      prepareText.textContent = "Downloading audio…";
    } catch (err) {
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
    try {
      const res = await fetch(`/content/${contentId}/status`);
      if (!res.ok) throw new Error("status check failed");
      const data = await res.json();
      consecutiveFailures = 0;

      if (data.status === "ready") {
        clearInterval(_activePollTimer);
        _activePollTimer = null;
        root.dataset.status = "ready";
        startPlayback();
      } else if (data.status === "error") {
        clearInterval(_activePollTimer);
        _activePollTimer = null;
        fail(data.error_message ? "Download failed" : "Download failed");
      } else if (data.phase === "converting") {
        prepareText.textContent = "Converting…";
      } else if (data.progress_percent != null) {
        prepareText.textContent = `Downloading audio… ${data.progress_percent}%`;
      }
    } catch (err) {
      consecutiveFailures += 1;
      if (consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
        clearInterval(_activePollTimer);
        _activePollTimer = null;
        fail("Lost connection while downloading");
      }
    }
  };

  _activePollTimer = setInterval(checkStatus, STATUS_POLL_MS);

  // Mobile browsers throttle/suspend timers for a backgrounded tab, so the
  // interval above may not have ticked in a while by the time the user
  // switches back — check in immediately instead of waiting for the next
  // scheduled tick.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") checkStatus();
  });
}

function setupFavorite() {
  const btn = document.getElementById("favorite-btn");
  if (!btn) return;

  btn.addEventListener("click", async () => {
    const on = btn.dataset.favorite === "true";
    btn.disabled = true;
    try {
      const res = await fetch(`/content/${btn.dataset.contentId}/favorite`, {
        method: on ? "DELETE" : "POST",
      });
      if (!res.ok) {
        showToast("Could not update favorite");
        return;
      }
      const data = await res.json();
      btn.dataset.favorite = String(data.is_favorite);
      btn.classList.toggle("is-on", data.is_favorite);
      btn.setAttribute("aria-pressed", String(data.is_favorite));
      btn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");
    } catch (err) {
      showToast("Could not update favorite");
    } finally {
      btn.disabled = false;
    }
  });
}

// Player is reachable from many different pages (a channel, Favorites,
// Saved, a Home shelf) — unlike channel.html/content_list.html's back-link,
// which always means "back to Library", there's no single right fallback
// destination here. Real browser history is, so prefer it whenever there's
// a same-origin previous page to return to; the plain href="/" stays as the
// no-JS/no-referrer fallback (e.g. the player opened directly, a fresh tab).
function setupBackLink() {
  const link = document.getElementById("player-back-link");
  if (!link) return;

  link.addEventListener("click", (event) => {
    if (document.referrer && new URL(document.referrer).origin === window.location.origin) {
      event.preventDefault();
      history.back();
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupPlayer();
  setupFavorite();
  setupBackLink();
});
