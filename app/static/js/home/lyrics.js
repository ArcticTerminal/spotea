// The player panel's second view: timed lyrics that follow the track.
//
// Two rules shape this module.
//
// **Nothing is fetched until the tab is selected.** A miss costs two live
// YouTube requests and, measured, about two thirds of tracks have no lyrics
// at all (see app/services/lyrics.py) — so fetching on play would spend most
// of that request budget on answers nobody asked for. The cost of waiting
// instead is one ~1s wait the first time this tab is opened on a track.
//
// **The audio element is only ever listened to, never touched.** Following
// along is a `timeupdate` consumer and nothing more: no play(), no pause(),
// no src. The player's iOS behaviour is load-bearing and easy to break from
// the outside — see the background-playback notes in player.js.

import { api } from "../core.js";
import { activeAudio, onPlayerEvent } from "../player.js";

const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

// After a manual scroll, leave the list where the reader put it for this
// long. Without it, following along drags the view back mid-read every time
// a line changes, which makes reading ahead impossible.
const MANUAL_SCROLL_GRACE_MS = 6000;

let selected = "queue";
// The content id the rendered lyrics belong to, so a track change is
// noticed without needing an event nothing currently fires.
let renderedFor = null;
let loadingFor = null;
let lines = [];
let activeIndex = -1;
let lastManualScrollAt = 0;

function playerContentId() {
  return document.getElementById("player-root")?.dataset.contentId || "";
}

function body() {
  return document.getElementById("lyrics-panel-body");
}

function scroller() {
  return document.querySelector(".queue-panel-inner");
}

function setMessage(text) {
  const el = body();
  if (el) el.innerHTML = `<p class="lyrics-empty"></p>`;
  if (el) el.querySelector(".lyrics-empty").textContent = text;
}

function render(payload) {
  const el = body();
  if (!el) return;
  lines = payload.lines || [];
  activeIndex = -1;

  if (!lines.length) {
    // Said out loud rather than left blank. "No lyrics" is the common
    // answer here and an empty panel reads as a failure to load.
    setMessage("No lyrics for this track.");
    return;
  }

  el.innerHTML = "";
  for (const line of lines) {
    const p = document.createElement("p");
    p.className = "lyrics-line";
    // textContent, not innerHTML: this string comes from YouTube Music.
    p.textContent = line.text;
    el.append(p);
  }
  if (payload.source) {
    const credit = document.createElement("p");
    credit.className = "lyrics-source";
    credit.textContent = payload.source;
    el.append(credit);
  }
}

async function load(contentId) {
  if (!contentId || loadingFor === contentId) return;
  loadingFor = contentId;
  renderedFor = contentId;
  lines = [];
  activeIndex = -1;
  setMessage("Loading lyrics…");

  // No errorMessage: a failure belongs in the panel the reader is looking
  // at, not in a toast over the player.
  const { ok, data } = await api(`/content/${contentId}/lyrics`);

  // The track moved on while this was in flight — whatever came back
  // describes something that is no longer playing.
  if (playerContentId() !== contentId) {
    loadingFor = null;
    return;
  }
  loadingFor = null;

  if (!ok) {
    renderedFor = null; // so selecting the tab again retries
    setMessage("Couldn't load lyrics.");
    return;
  }
  render(data);
}

/** The line covering this moment, or -1 before the first one starts. */
function indexAt(ms) {
  // Backwards: the answer is almost always the current line or the one
  // after it, so this returns within a step or two of where it started.
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    if (ms >= lines[i].start_ms) return i;
  }
  return -1;
}

function scrollActiveIntoView(el) {
  const box = scroller();
  if (!box) return;
  if (Date.now() - lastManualScrollAt < MANUAL_SCROLL_GRACE_MS) return;

  // Measured rather than scrollIntoView(), which scrolls every scrollable
  // ancestor including the page behind the overlay.
  const boxRect = box.getBoundingClientRect();
  const elRect = el.getBoundingClientRect();
  const top = box.scrollTop + (elRect.top - boxRect.top) - (box.clientHeight - elRect.height) / 2;
  box.scrollTo({ top, behavior: reducedMotion.matches ? "auto" : "smooth" });
}

function syncActiveLine() {
  const seconds = activeAudio()?.currentTime;
  if (typeof seconds !== "number") return;

  const index = indexAt(Math.round(seconds * 1000));
  if (index === activeIndex) return;

  const rendered = body()?.querySelectorAll(".lyrics-line") || [];
  rendered[activeIndex]?.classList.remove("is-active");
  activeIndex = index;
  const el = rendered[index];
  if (!el) return;
  el.classList.add("is-active");
  scrollActiveIntoView(el);
}

function showTab(name) {
  selected = name;
  const isLyrics = name === "lyrics";
  for (const [tab, panel, on] of [
    ["panel-tab-queue", "queue-panel-body", !isLyrics],
    ["panel-tab-lyrics", "lyrics-panel-body", isLyrics],
  ]) {
    const tabEl = document.getElementById(tab);
    const panelEl = document.getElementById(panel);
    if (tabEl) {
      tabEl.classList.toggle("is-selected", on);
      tabEl.setAttribute("aria-selected", String(on));
    }
    if (panelEl) panelEl.hidden = !on;
  }

  // Switching views resets the scroll: the two lists have nothing to do with
  // each other, and landing halfway down a set of lyrics is disorienting.
  scroller()?.scrollTo({ top: 0 });

  if (!isLyrics) return;
  const contentId = playerContentId();
  if (contentId && contentId !== renderedFor) load(contentId);
  else if (!contentId) setMessage("Nothing playing.");
}

export function setupLyricsPanel() {
  const queueTab = document.getElementById("panel-tab-queue");
  const lyricsTab = document.getElementById("panel-tab-lyrics");
  if (!queueTab || !lyricsTab) return;

  queueTab.addEventListener("click", () => showTab("queue"));
  lyricsTab.addEventListener("click", () => showTab("lyrics"));

  scroller()?.addEventListener(
    "scroll",
    () => {
      // Only a person scrolling counts; the smooth scroll above fires this
      // too, which is why it's stamped rather than acted on directly and why
      // scrollActiveIntoView is the one thing that reads the stamp.
      if (selected === "lyrics") lastManualScrollAt = Date.now();
    },
    { passive: true }
  );

  // The only hook into playback: read the clock, move the highlight. Listened
  // to through onPlayerEvent so this never holds a reference to the audio
  // element itself.
  onPlayerEvent("timeupdate", () => {
    if (selected !== "lyrics") return;
    const contentId = playerContentId();
    if (contentId && contentId !== renderedFor) {
      load(contentId);
      return;
    }
    if (lines.length) syncActiveLine();
  });
}
