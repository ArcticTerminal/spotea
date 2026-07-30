const SKIP_SECONDS = 15;
const STATUS_POLL_MS = 1500;

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

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
    durationEl.textContent = formatTime(audio.duration);
    paintRange(seek);
  });

  audio.addEventListener("timeupdate", () => {
    if (scrubbing) return;
    seek.value = audio.currentTime;
    currentTimeEl.textContent = formatTime(audio.currentTime);
    paintRange(seek);
  });

  audio.addEventListener("error", () => showToast("Could not load the audio for this track"));

  seek.addEventListener("input", () => {
    scrubbing = true;
    currentTimeEl.textContent = formatTime(Number(seek.value));
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

  // Browsers speculatively load links (prerender runs the page's JS), so a card
  // link can open this page without the user ever clicking it. Since opening
  // the player is what triggers a download, kicking one off on plain page load
  // would let mere prefetching fill the user's disk. Wait until the page is
  // genuinely being viewed.
  whenVisible(() => prepareAudio(audio));
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

  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/content/${contentId}/status`);
      if (!res.ok) throw new Error("status check failed");
      const data = await res.json();

      if (data.status === "ready") {
        clearInterval(timer);
        root.dataset.status = "ready";
        startPlayback();
      } else if (data.status === "error") {
        clearInterval(timer);
        fail(data.error_message ? "Download failed" : "Download failed");
      }
    } catch (err) {
      clearInterval(timer);
      fail("Lost connection while downloading");
    }
  }, STATUS_POLL_MS);
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

document.addEventListener("DOMContentLoaded", () => {
  setupPlayer();
  setupFavorite();
});
