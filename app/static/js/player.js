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

// Breadcrumbs for the one class of failure the server can't see: playback
// that doesn't happen. A track that never advanced, a download that finished
// but never started playing, a play() the browser refused — from the
// server's side all of those look exactly like "the user stopped listening",
// because nothing gets requested. See routers/debug.py.
//
// sendBeacon rather than fetch: these are posted at precisely the moments a
// mobile browser is most likely to freeze or discard the page, and a beacon
// is handed to the browser to deliver rather than depending on this page
// still running. Failures here are swallowed entirely — diagnostics that can
// break playback are worse than no diagnostics.
export function reportPlayback(event, detail = {}) {
  try {
    const body = JSON.stringify([
      { event, visibility: document.visibilityState, at: new Date().toISOString(), ...detail },
    ]);
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/debug/playback", new Blob([body], { type: "application/json" }));
    } else {
      fetch("/debug/playback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        keepalive: true,
      }).catch(() => {});
    }
  } catch (err) {
    /* Never let a breadcrumb take playback down with it. */
  }
}

// A one-second clip of 20Hz at roughly -40dBFS: a whole number of cycles, so
// it loops without a click, and low and quiet enough to be inaudible in
// practice. Not digital silence, deliberately — a browser deciding whether a
// background tab is "playing audio" measures the signal, and an all-zero
// buffer can read as nothing playing, which is the entire thing this is for.
function buildKeepAliveClip() {
  const sampleRate = 8000;
  const frames = sampleRate;
  const dataBytes = frames * 2;
  const bytes = new Uint8Array(44 + dataBytes);
  const view = new DataView(bytes.buffer);
  const ascii = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) bytes[offset + i] = text.charCodeAt(i);
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  ascii(8, "WAVEfmt ");
  view.setUint32(16, 16, true); // PCM fmt chunk size
  view.setUint16(20, 1, true); // PCM, uncompressed
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  ascii(36, "data");
  view.setUint32(40, dataBytes, true);
  for (let i = 0; i < frames; i += 1) {
    view.setInt16(44 + i * 2, Math.round(Math.sin((2 * Math.PI * 20 * i) / sampleRate) * 300), true);
  }
  return URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
}

let keepAliveUrl = null;

/**
 * Keeps the page's audio session open through a gap in real playback —
 * while a track downloads, and across the handoff from one track to the next.
 *
 * A mobile browser leaves a backgrounded page alone for as long as it's
 * playing audio, and starts throttling its timers and deferring its network
 * the moment it isn't. Every gap here is therefore self-inflicted: the app
 * stops the audio, then depends on a setTimeout poll and a fetch to start it
 * again — the two things it just gave the browser permission to freeze. That
 * is why a track would sit there doing nothing until the app was brought to
 * the foreground, at which point the frozen work resumed and it started
 * playing "by itself".
 *
 * Playing something inaudible for the length of the gap costs nothing and
 * removes the whole class of failure. It runs on its own element, so the
 * transport's listeners never see it — see _player_controls.html.
 */
export function startKeepAlive() {
  const keepAlive = document.getElementById("keepalive");
  if (!keepAlive) return;
  if (!keepAliveUrl) keepAliveUrl = buildKeepAliveClip();
  if (!keepAlive.src) keepAlive.src = keepAliveUrl;
  // Already holding the session open — leave it alone. Seeking a playing
  // element back to 0 makes it stop for the length of the seek, which is
  // exactly the silence this exists to prevent.
  if (!keepAlive.paused) return;
  // Restarted rather than resumed: a paused element that has been sitting
  // for a while may have had its buffer discarded, and this costs nothing.
  keepAlive.currentTime = 0;
  // Reported because this element is also the cleanest probe there is for the
  // rule the pre-roll below is built around: it is asked to start at exactly
  // the moments nothing else is playing. A `keepalive-requested` with no
  // `keepalive-playing` after it, while hidden, is a backgrounded app being
  // refused a start outright rather than merely being slow.
  reportPlayback("keepalive-requested");
  keepAlive.addEventListener("playing", () => reportPlayback("keepalive-playing"), { once: true });
  keepAlive.play().catch((err) => {
    // Blocked before the first user gesture, which is fine — nothing is
    // waiting on audio at that point either.
    reportPlayback("keepalive-rejected", { error: String(err?.name || err) });
  });
}

export function stopKeepAlive() {
  document.getElementById("keepalive")?.pause();
}

// ---------------------------------------------------------------------------
// Decks
//
// Playback runs on two interchangeable <audio> elements rather than on one
// whose src is reassigned per track. The reason is iOS-specific and load-
// bearing: iOS suspends a backgrounded web app the moment it stops emitting
// audio, and a suspended process runs nothing — no timers, no fetches, and
// above all no media loading. Assigning a fresh src at the instant a track
// ends therefore starts a network load the OS is about to freeze. The play()
// call neither resolves nor rejects; it simply hangs, and the track starts
// seconds or minutes later, at the exact moment the app is brought back to
// the foreground. The breadcrumb log showed precisely that: `play-requested`
// at 20:30:22 while hidden, `playing` at 20:34:31 on the way back in.
//
// So the next track is loaded into the *other* deck while the current one is
// still playing — the app is awake then, by definition — and the handoff
// itself is a bare play() on an already-buffered element. No resource
// selection, no request, nothing left for a suspended process to defer.
//
// Which deck is live is carried by the element's id rather than by a variable
// here: every module in the app (resume.js, home/overlay.js, this file) finds
// the player as #audio, and exchanging the two ids moves all of them at once.
// Listeners are registered on both elements and filtered by id, so they
// follow the swap too — see onPlayerEvent.
// ---------------------------------------------------------------------------

/** The deck currently driving the transport. */
export function activeAudio() {
  return document.getElementById("audio");
}

function standbyAudio() {
  return document.getElementById("audio-standby");
}

function swapDecks(volume) {
  const active = activeAudio();
  const standby = standbyAudio();
  if (!active || !standby) return;
  // Volume and mute live on the element, not on the page, so they have to be
  // carried across or every handoff would silently reset them to full.
  // `volume`, when given, overrides copying it from `active` — needed for a
  // crossfaded handoff, where `active` (the outgoing deck) has just been
  // faded down to near 0 and copying it would hand the incoming deck that
  // same near-silence instead of the level it was actually faded up to.
  standby.volume = volume ?? active.volume;
  standby.muted = active.muted;
  active.id = "audio-standby";
  standby.id = "audio";
}

/**
 * Registers a media listener on both decks, firing only for whichever is live.
 *
 * Both, because the pair swap roles on every queue handoff and a listener
 * bound to just the element that happened to be live at page load would go
 * quiet the first time a track ended. Filtered, because the standby deck
 * genuinely fires events of its own while it preloads — `loadedmetadata`
 * above all — and rendering those would paint the *next* track's duration
 * over the one that's playing.
 */
export function onPlayerEvent(type, handler) {
  for (const deck of [activeAudio(), standbyAudio()]) {
    if (!deck) continue;
    deck.addEventListener(type, (event) => {
      if (event.currentTarget.id !== "audio") return;
      handler(event);
    });
  }
}

/**
 * Loads a track into the standby deck so that starting it later costs no
 * network at all.
 *
 * Called while the current track is still playing, which is the whole point:
 * the app is awake then and the fetch goes through, so by the time the handoff
 * needs the bytes they are already here.
 *
 * `?preload=1` keeps the server from recording this as a play (see
 * routers/content.py's stream_content) — it isn't one yet, and a preloaded
 * track the listener skips past should not turn up in Recently Played. The
 * play gets recorded separately if and when one actually starts.
 */
export function preloadNext(contentId) {
  const deck = standbyAudio();
  if (!deck) return;
  const id = String(contentId);
  if (deck.dataset.preloadFor === id) return;
  deck.dataset.preloadFor = id;
  deck.src = `/content/${id}/stream?preload=1`;
  deck.load();
  reportPlayback("preload-armed", { contentId: id });

  // Disarmed on failure rather than left in place: adoptStandby only checks
  // *which* track a deck is holding, so a preload that 404'd or was refused
  // would otherwise be handed the next track change and play nothing at all —
  // strictly worse than never having armed, since it also displaces the
  // ordinary load that would have worked.
  deck.addEventListener(
    "error",
    () => {
      if (deck.dataset.preloadFor !== id) return;
      delete deck.dataset.preloadFor;
      reportPlayback("preload-failed", { contentId: id });
    },
    { once: true }
  );

  // Not decorative: `preload="auto"` is a hint, and whether iOS honours it
  // for a backgrounded page is the single assumption this whole arrangement
  // rests on. This is what says, from the device itself, that the bytes were
  // really here before the handoff needed them.
  deck.addEventListener(
    "canplaythrough",
    () => {
      // Guarded like the error case: by the time this fires the deck may
      // already have gone live, and reporting it then would read as a
      // preload that landed in time when it did no such thing.
      if (deck.dataset.preloadFor !== id) return;
      reportPlayback("preload-buffered", { contentId: id, readyState: deck.readyState });
    },
    { once: true }
  );
}

// ---------------------------------------------------------------------------
// Pre-roll
//
// Preloading the bytes turned out not to be enough, and the breadcrumb log is
// what said so. On the device, a handoff looked like this:
//
//   track-ended       hidden   43957 -> 43958 (prepared)
//   play-requested    hidden   43958  preloaded=true readyState=4
//   playback-stalled  hidden   43958  readyState=4      (+3.0s)
//   playing           visible  43958                    (+7.7s)
//
// Two things in there rule out most explanations. readyState 4 means the whole
// next track was already in memory, so nothing was waiting on the network. And
// the stall watchdog — an ordinary setTimeout — *fired* three seconds in while
// still hidden, so the page was not frozen and neither were its timers. The
// app was awake, the audio was in hand, and play() still did nothing: it
// neither threw nor started, and the element stayed paused until the app came
// back on screen.
//
// That is WebKit refusing a playback-state change rather than anything being
// slow or suspended. A backgrounded iOS app may keep playing what is already
// playing — the previous track ran to its end off screen, as it always did —
// but it may not *start* a paused element, and the refusal is silent.
//
// So the next track has to already be playing before it is needed. It gets
// started a few seconds early, inaudibly, while the current track is still
// going and playback is therefore still allowed to begin. The handoff itself
// then asks for nothing: a volume change, which no policy can refuse — and,
// now, not even a seek. It used to also snap the incoming deck's position
// back to zero at the handoff instant, on the theory that pre-roll was only
// ever meant to warm up the audio session and the listener should hear the
// track from its actual start. In practice that meant the track played
// audibly for up to PREROLL_SECONDS during the fade-up below and then visibly
// jumped backwards the moment it became "the" track — indistinguishable from
// a real rewind, because it was one. Removed: the position pre-roll reaches
// by the time of the handoff **is** the track's actual start, just a few
// seconds further into it, exactly as if it had been playing the whole time —
// which, audibly, it has.
//
// Crossfade rides the same window: as the pre-rolled deck fades up from
// PREROLL_VOLUME to the listener's real volume, the outgoing deck fades down
// from it to 0 in lockstep, so the handoff has no audible seam at all rather
// than a click from one deck's volume to the other's.
// ---------------------------------------------------------------------------

// How long before the end of a track its successor is quietly started, and
// the width of the crossfade that runs across that same window. Long enough
// that `timeupdate` (which fires roughly four times a second, and less often
// on a backgrounded page) is certain to land inside the window and give the
// fade several steps to work with, short enough to keep two decoders running
// together to a few seconds per track.
const PREROLL_SECONDS = 4;

// Inaudible, but deliberately not silent. WebKit decides whether a media
// element gets a real audio session by measuring its output, and a muted
// element (or one at volume 0) can be written off as silent — which would
// leave the pre-rolled deck holding no session to take over with, putting the
// handoff right back to acquiring one in the background. Also doubles as the
// crossfade's floor, for the same reason: never let the fade-up dip back to
// true silence.
const PREROLL_VOLUME = 0.0001;

// The listener's real volume for the deck currently crossfading, captured the
// instant pre-roll arms so the fade has a fixed target to ramp toward — reading
// the live deck's own .volume on every tick would be reading a value this same
// code is in the middle of changing.
let crossfadeBaseVolume = null;

/**
 * Starts the standby deck, quietly, once the live one is nearly out, and
 * carries the crossfade forward on every tick after that.
 *
 * Driven straight off `timeupdate` and safe to call on every one: it owns the
 * "is it time yet?" decision, so no caller has to keep a copy of the window
 * in step with this one.
 */
export function prerollStandby() {
  const deck = standbyAudio();
  if (!deck) return;
  const id = deck.dataset.preloadFor;
  if (!id) return;

  const live = activeAudio();
  const remaining = (live.duration || 0) - live.currentTime;

  if (deck.dataset.prerolled !== "1") {
    if (!(remaining > 0 && remaining <= PREROLL_SECONDS)) return;
    // Nothing to start yet. The ordinary play() path is a better answer than
    // a pre-roll that has to buffer first, since that's a load, and a load is
    // the thing this is trying to keep out of the handoff.
    if (deck.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) return;

    deck.dataset.prerolled = "1";
    deck.currentTime = 0;
    deck.muted = false;
    deck.volume = PREROLL_VOLUME;
    crossfadeBaseVolume = live.volume;
    reportPlayback("preroll-requested", { contentId: id, readyState: deck.readyState });
    deck.addEventListener(
      "playing",
      () => reportPlayback("preroll-playing", { contentId: id }),
      { once: true }
    );
    deck.play().catch((err) =>
      reportPlayback("preroll-rejected", { contentId: id, error: String(err?.name || err) })
    );
    return;
  }

  // Already running: ride the crossfade. Muted has nothing to fade — leave it
  // alone rather than do silent volume arithmetic that will never be heard.
  if (crossfadeBaseVolume == null || live.muted) return;
  const t = remaining > 0 ? Math.min(1, Math.max(0, 1 - remaining / PREROLL_SECONDS)) : 1;
  live.volume = crossfadeBaseVolume * (1 - t);
  deck.volume = Math.max(PREROLL_VOLUME, crossfadeBaseVolume * t);
}

/**
 * Stops a pre-roll that isn't going to be handed over after all — the listener
 * paused, or skipped somewhere else entirely. Without this the standby deck
 * would keep running inaudibly for the rest of the track it was meant to
 * follow, and then past it.
 */
export function stopPreroll() {
  const deck = standbyAudio();
  if (!deck || deck.dataset.prerolled !== "1") return;
  delete deck.dataset.prerolled;
  deck.pause();
  deck.currentTime = 0;
  // Undo whatever the crossfade had faded the live deck down to — it was
  // mid-fade, not mid-handoff, so the track that's staying deserves its real
  // volume back rather than whatever level it happened to be passing through.
  const live = activeAudio();
  if (crossfadeBaseVolume != null) live.volume = crossfadeBaseVolume;
  crossfadeBaseVolume = null;
}

/**
 * Whether the standby deck is already playing this track, quietly, ready to
 * be handed over. The caller's cue that this switch needs no bridging: the
 * audio isn't going to stop, so nothing has to be held open across it.
 */
export function standbyIsRunningFor(contentId) {
  const deck = standbyAudio();
  if (!deck || deck.dataset.preloadFor !== String(contentId)) return false;
  return deck.dataset.prerolled === "1" && !deck.paused;
}

/**
 * Makes the standby deck the live one when it's holding this exact track.
 *
 * Returns null when it isn't, meaning the caller loads the track the ordinary
 * way. Otherwise `{ running }` says whether the deck was pre-rolled and is
 * genuinely playing right now — the difference between a handoff that needs
 * no permission at all and one that still has to call play().
 */
function adoptStandby(contentId) {
  const deck = standbyAudio();
  if (!deck || deck.dataset.preloadFor !== String(contentId)) return null;
  const running = deck.dataset.prerolled === "1" && !deck.paused;
  reportPlayback("handoff-adopt", {
    contentId: String(contentId),
    running,
    prerolled: deck.dataset.prerolled ?? null,
    paused: deck.paused,
    position: deck.currentTime,
    readyState: deck.readyState,
  });
  delete deck.dataset.preloadFor;
  delete deck.dataset.prerolled;
  // Hand the crossfade's target volume to the swap explicitly when this deck
  // was actually faded in — copying it from the outgoing deck (swapDecks'
  // ordinary behaviour) would hand over the near-silence it was just faded
  // down to instead.
  swapDecks(running ? crossfadeBaseVolume : undefined);
  if (running) crossfadeBaseVolume = null;
  return { running };
}

/** Drops a preload nothing is going to play, and the fetch behind it. */
export function clearPreload() {
  const deck = standbyAudio();
  if (!deck || (!deck.dataset.preloadFor && deck.dataset.prerolled !== "1")) return;
  const wasCrossfading = deck.dataset.prerolled === "1";
  delete deck.dataset.preloadFor;
  delete deck.dataset.prerolled;
  deck.pause();
  deck.removeAttribute("src");
  deck.load();
  // Same reason as stopPreroll: closing mid-crossfade shouldn't leave the
  // deck that's staying live faded down.
  if (wasCrossfading && crossfadeBaseVolume != null) activeAudio().volume = crossfadeBaseVolume;
  crossfadeBaseVolume = null;
}

// A single-sample silent WAV, used only as a throwaway source for
// unlockAudio() below.
const SILENT_AUDIO_DATA_URI =
  "data:audio/wav;base64,UklGRiUAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQEAAACA";

// WebKit (every iOS browser — Apple requires them all to use its engine)
// only allows a script-initiated play() when it directly, synchronously
// results from a user gesture. Our actual playback start happens later —
// after an `await` for track metadata and, for a fresh download, a
// setTimeout-driven status poll — which breaks that chain and gets silently
// blocked, most visibly on iPhone (Chrome's autoplay policy is stickier
// about "the user interacted with this page at some point" and usually
// tolerates the delay fine).
//
// WebKit's unlocked state sticks to the *element*, not the gesture that
// earned it, though: play it once, synchronously, inside a real tap, and
// every later play() call on that same element succeeds without a fresh
// gesture — even after swapping .src out from under it. So do that once,
// on the very first real click anywhere in the page (setupPlayer wires this
// up, below) — deliberately not tied to a specific control (a track row,
// a nav tab, anything) and deliberately not called from openPlayer() itself,
// since openPlayer() also runs from places with no gesture behind them at
// all (a boot-time resume, a remote-triggered open, the "ended" handler's
// auto-advance) — calling it from there risked spending this exactly when
// there was no real gesture to spend, permanently starving every later
// real tap of the one attempt that could have actually unlocked anything.
let audioUnlocked = false;
function unlockAudio() {
  if (audioUnlocked) return;
  audioUnlocked = true;

  // Both decks, not just the live one. WebKit's unlocked state is per
  // element, and the restriction it lifts covers loading as well as playing
  // (RequireUserGestureForLoad, not only ...ForRateChange) — so leaving the
  // standby deck locked wouldn't merely block its play(), it would make the
  // preload itself a no-op, which is the one thing this whole arrangement
  // exists to make possible.
  for (const deck of [activeAudio(), standbyAudio()]) {
    // No element, or something is already loaded on it — either nothing to
    // do, or a play() already ran on it some other way. Either way, don't
    // stomp a loaded (possibly playing) source with the silent clip.
    if (!deck || deck.src) continue;
    // What WebKit needs is the *call* happening synchronously inside the
    // click — not that the promise resolves, which is why this doesn't wait
    // on .then() before pausing.
    deck.src = SILENT_AUDIO_DATA_URI;
    deck.play().catch(() => {});
    deck.pause();
  }

  // The keep-alive element needs the same treatment for the same reason:
  // every one of its start() calls happens on a download poll or a track
  // handoff, never inside a gesture. Its own clip is the throwaway source
  // here, so unlike the decks there's nothing to swap back out afterwards.
  startKeepAlive();
  stopKeepAlive();
}

// Range inputs can't style their "already played" portion natively, so paint it
// with a gradient that tracks the current value.
export function paintRange(input) {
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const pct = max > min ? ((Number(input.value) - min) / (max - min)) * 100 : 0;
  input.style.setProperty("--fill", `${pct}%`);
}

export function setupPlayer() {
  if (!activeAudio()) return;

  // See unlockAudio above for why this has to be a page-wide "first click,
  // whatever it is" listener rather than something openPlayer() calls.
  document.addEventListener("click", unlockAudio);

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
    const paused = activeAudio().paused;
    showIcon(iconPlay, paused);
    showIcon(iconPause, !paused);
    playBtn.setAttribute("aria-label", paused ? "Play" : "Pause");
  }

  function syncMuteIcon() {
    const audio = activeAudio();
    const silent = audio.muted || audio.volume === 0;
    showIcon(iconVolume, !silent);
    showIcon(iconMuted, silent);
    muteBtn.setAttribute("aria-label", silent ? "Unmute" : "Mute");
  }

  playBtn.addEventListener("click", () => {
    const audio = activeAudio();
    if (audio.paused) audio.play().catch(() => showToast("Playback was blocked by the browser"));
    else audio.pause();
  });

  // Every media listener from here down goes through onPlayerEvent rather
  // than binding to one element: the two decks trade places on each queue
  // handoff, and a listener attached to whichever was live at page load
  // would stop firing the first time a track ended. Nothing below may close
  // over the element either, for the same reason — activeAudio(), every time.
  onPlayerEvent("play", syncPlayIcon);
  onPlayerEvent("pause", syncPlayIcon);
  onPlayerEvent("ended", syncPlayIcon);

  document.getElementById("back15").addEventListener("click", () => {
    const audio = activeAudio();
    audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
  });
  document.getElementById("fwd15").addEventListener("click", () => {
    const audio = activeAudio();
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
  });

  onPlayerEvent("loadedmetadata", () => {
    const audio = activeAudio();
    seek.max = audio.duration || 0;
    durationEl.textContent = formatDuration(audio.duration);
    paintRange(seek);
  });

  onPlayerEvent("timeupdate", () => {
    if (scrubbing) return;
    const audio = activeAudio();
    seek.value = audio.currentTime;
    currentTimeEl.textContent = formatDuration(audio.currentTime);
    paintRange(seek);
  });

  onPlayerEvent("error", () => {
    // unlockAudio() above loads a throwaway silent clip on the page's first
    // click purely to unlock WebKit's autoplay gate — nothing the user asked
    // to hear. A failure there is invisible and harmless by design; without
    // this guard it fires this exact same toast, which reads as "your song
    // won't play" when no real track was ever involved.
    if (activeAudio().src === SILENT_AUDIO_DATA_URI) return;
    showToast("Could not load the audio for this track");
  });

  seek.addEventListener("input", () => {
    scrubbing = true;
    currentTimeEl.textContent = formatDuration(Number(seek.value));
    paintRange(seek);
  });
  seek.addEventListener("change", () => {
    activeAudio().currentTime = Number(seek.value);
    scrubbing = false;
  });

  volume.addEventListener("input", () => {
    // Set on both decks, not just the live one: the standby deck is already
    // buffering the next track and becomes the live one at the next handoff,
    // where a volume it never heard about would come back as a jump to full.
    for (const deck of [activeAudio(), standbyAudio()]) {
      if (!deck) continue;
      deck.volume = Number(volume.value) / 100;
      deck.muted = false;
    }
    paintRange(volume);
    syncMuteIcon();
  });

  muteBtn.addEventListener("click", () => {
    const muted = !activeAudio().muted;
    for (const deck of [activeAudio(), standbyAudio()]) {
      if (deck) deck.muted = muted;
    }
    syncMuteIcon();
  });

  // Space toggles playback, arrows scrub — as long as focus isn't in a control.
  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, button, a")) return;
    const audio = activeAudio();
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
  setupMediaSession();
}

// Lock-screen/notification-shade transport controls and Bluetooth/headset
// buttons all route through this — without it, playback is only
// controllable while this tab is in the foreground.
function setupMediaSession() {
  if (!("mediaSession" in navigator)) return;

  const title = document.querySelector(".player-title")?.textContent || "";
  const artist = document.querySelector(".player-channel")?.textContent || "";
  const artworkSrc = document.querySelector(".player-art img")?.src;

  navigator.mediaSession.metadata = new MediaMetadata({
    title,
    artist,
    artwork: artworkSrc ? [{ src: artworkSrc }] : [],
  });

  navigator.mediaSession.setActionHandler("play", () => activeAudio().play().catch(() => {}));
  navigator.mediaSession.setActionHandler("pause", () => activeAudio().pause());
  navigator.mediaSession.setActionHandler("seekbackward", () => {
    const audio = activeAudio();
    audio.currentTime = Math.max(0, audio.currentTime - SKIP_SECONDS);
  });
  navigator.mediaSession.setActionHandler("seekforward", () => {
    const audio = activeAudio();
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + SKIP_SECONDS);
  });
  navigator.mediaSession.setActionHandler("seekto", (details) => {
    if (details.seekTime != null) activeAudio().currentTime = details.seekTime;
  });

  onPlayerEvent("play", () => {
    navigator.mediaSession.playbackState = "playing";
  });
  onPlayerEvent("pause", () => {
    navigator.mediaSession.playbackState = "paused";
  });

  const syncPositionState = () => {
    const audio = activeAudio();
    if (!audio.duration || Number.isNaN(audio.duration)) return;
    try {
      navigator.mediaSession.setPositionState({ duration: audio.duration, position: audio.currentTime });
    } catch (err) {
      /* Throws if position momentarily exceeds duration mid-seek; harmless to skip. */
    }
  };
  onPlayerEvent("loadedmetadata", syncPositionState);
  onPlayerEvent("timeupdate", syncPositionState);
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
 * onStart (optional) fires exactly once, right as this track becomes the one
 * loaded in the live deck. It receives `{ preloaded }` — whether that came
 * from adopting an already-buffered standby deck rather than assigning a
 * fresh src — because the two differ in how the server learns about the play:
 * a fresh src records it on the /stream request itself (see
 * routers/content.py's stream_content), while a preloaded one was fetched
 * with ?preload=1 precisely so it wouldn't, and has to be reported.
 *
 * onFail (optional) fires every time this ends in the disabled "Download
 * failed" state — a track pulled from a queue (e.g. one YouTube has since
 * made private) shouldn't just die there while the app is backgrounded; the
 * overlay uses this to skip on to whatever's next instead. The inline error
 * still gets drawn regardless, since a track opened with nothing queued
 * after it (or opened directly, not through a queue) has nowhere to skip to.
 * It receives `{ permanent }`: a track YouTube won't serve to anyone (see
 * Content.is_unavailable) is a settled fact about that one track, whereas
 * any other failure might be a sign that YouTube is refusing us in general —
 * and the overlay's skip limits treat those two very differently.
 */
export async function prepareAudio(onStart, onFail) {
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

    // The fast path, and the only one that can be trusted while the app is
    // backgrounded: the standby deck already holds this track's bytes, so
    // going live on it is a bare play() with no resource load behind it —
    // nothing a suspended iOS process can defer. Falling through to a fresh
    // src is the ordinary first-track path, and the fallback whenever a
    // preload didn't happen or hadn't landed in time.
    const adopted = adoptStandby(contentId);
    const preloaded = adopted !== null;
    const audio = activeAudio();
    if (!preloaded) audio.src = streamUrl;

    // An adopted deck fired loadedmetadata back when it was preloading, on
    // the standby element, where onPlayerEvent correctly dropped it — so the
    // seek bar's range, the duration readout and the media-session position
    // would all still be describing the track that just finished. Replaying
    // the event now runs that wiring against the deck that's live, without
    // any of it needing to know a handoff is a special case.
    if (preloaded && audio.readyState >= HTMLMediaElement.HAVE_METADATA) {
      audio.dispatchEvent(new Event("loadedmetadata"));
    }

    // Deliberately NOT stopped here, which is where it used to be. The
    // keep-alive is what holds the audio session — and with it, on iOS, the
    // whole app — open across the gap, and the gap does not end when
    // playback is *asked for*, it ends when playback actually starts.
    // Pausing first left the page silent for exactly the window a media load
    // has to happen in, which is the window a backgrounded iOS app gets
    // suspended in.
    //
    // Bound straight to the element rather than through onPlayerEvent: this
    // is a one-shot belonging to this one prepare, on the deck that is live
    // right now, and it must not survive to fire again on some later track.
    audio.addEventListener("playing", stopKeepAlive, { once: true });

    if (onStart) onStart({ preloaded });

    const resume = consumeResumeState(contentId);
    if (resume) {
      // Waiting for loadedmetadata on a deck that has already loaded would
      // wait forever, so a ready one is seeked outright.
      if (audio.readyState >= HTMLMediaElement.HAVE_METADATA) audio.currentTime = resume.currentTime;
      else audio.addEventListener("loadedmetadata", () => { audio.currentTime = resume.currentTime; }, { once: true });
    }
    if (adopted?.running && (!resume || resume.wasPlaying)) {
      // Already playing, quietly, since a few seconds before the previous
      // track ended, and already crossfaded up to its real volume by
      // prerollStandby — see the pre-roll notes above. There is nothing left
      // to do here at all: no seek (its position is genuinely where the
      // track has been playing since pre-roll started — snapping it back to
      // 0 here is what used to make every handoff sound like a rewind, because
      // it was one) and no volume change (swapDecks already carried the
      // crossfade's target volume onto this deck). The handoff asks
      // permission for nothing, which is the entire point: a backgrounded iOS
      // app is not allowed to start a paused element.
      reportPlayback("handoff-preroll", { contentId, readyState: audio.readyState });
      // Same reasoning as the loadedmetadata replay above, for the same
      // reason: the audio never stopped, so no native `play` event fires here
      // for anything bound through onPlayerEvent to catch — it already fired
      // once, back when this deck was still the standby one, and
      // onPlayerEvent dropped it because it wasn't `#audio` yet. Left
      // unreplayed, the play/pause button and mini-bar icon are stuck showing
      // "paused" (both are wired to onPlayerEvent("play"/"pause", ...), and
      // the last real event either saw was the outgoing track's pre-`ended`
      // `pause`) and mediaSession.playbackState is stuck at "paused" too —
      // metadata is still correct (home/overlay.js's openPlayer sets it
      // separately, every track), so the lock-screen widget looks fine, but
      // iOS treats the session as not actively playing, which is what drops
      // it out of the Dynamic Island specifically.
      audio.dispatchEvent(new Event("play"));
      stopKeepAlive();
      watchPlaybackStarted(contentId);
    } else if (!resume || resume.wasPlaying) {
      reportPlayback("play-requested", { contentId, preloaded, readyState: audio.readyState });
      audio.play().then(
        () => reportPlayback("playing", { contentId }),
        (err) => reportPlayback("play-rejected", { contentId, error: String(err?.name || err) })
      );
      watchPlaybackStarted(contentId);
    } else {
      // Restored deliberately paused: nothing is going to start, so nothing
      // should be holding the session open waiting for it — and a deck that
      // was pre-rolled into playing has to be stopped, since the reason it
      // was running was to hand over to playback that isn't happening.
      audio.pause();
      stopKeepAlive();
    }
  };

  const fail = (message, { permanent = false } = {}) => {
    prepare.hidden = false;
    prepare.classList.add("is-error");
    prepare.querySelector(".spinner").hidden = true;
    prepareText.textContent = message;
    transport.classList.add("is-disabled");
    reportPlayback("prepare-failed", { contentId, message, permanent });
    // The keep-alive is deliberately left running. onFail's usual answer is
    // to skip straight on to the next track, and silencing the app here
    // would put a gap in exactly the handoff that most needs to not have one
    // — this is reached with nobody watching, which is the whole reason the
    // skip exists. Whoever decides *not* to skip stops it instead; see
    // home/overlay.js.
    if (onFail) onFail(message, { permanent });
  };

  if (root.dataset.status === "ready") {
    startPlayback();
    return;
  }

  // YouTube has already refused this one to every client there is, and the
  // server won't attempt it again either (see routers/content.py's
  // start_download). Answering from what we already know turns a four-second
  // wait on a foregone conclusion into an instant skip — which matters most
  // in a queue, where this is otherwise a stall the user is not there to see.
  if (root.dataset.unavailable === "true") {
    fail("Not available on YouTube", { permanent: true });
    return;
  }

  prepare.hidden = false;
  transport.classList.add("is-disabled");
  prepareText.textContent = "Preparing audio…";
  // Everything from here to startPlayback runs on a poll and a fetch, both
  // of which a backgrounded browser is free to freeze while nothing is
  // playing — see startKeepAlive.
  startKeepAlive();

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
    } else if (data.status === "error" && data.is_unavailable) {
      stopPolling();
      fail("Not available on YouTube", { permanent: true });
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

// How long to give a play() call before concluding it didn't take. Long
// enough to cover a slow first byte off disk, short enough that a listener
// isn't left in silence wondering.
const PLAYBACK_WATCHDOG_MS = 3000;

let playbackWatchdogTimer = null;

/**
 * Last line of defence for a play() that neither threw nor started.
 *
 * The promise rejecting is the documented way a blocked play() reports
 * itself, and the paths above handle that — but it isn't the only way to
 * end up silent. A play() issued while the browser is in the middle of
 * suspending the page can be left pending indefinitely, resolving only once
 * the page is looked at again, and there is no event for that. This notices
 * (and says so, in the log) rather than leaving it to be reported as "it
 * just stopped".
 *
 * One retry, not a loop: if a second attempt three seconds later also
 * doesn't take, the cause isn't something retrying will fix, and the
 * transport is right there.
 */
function watchPlaybackStarted(contentId) {
  clearTimeout(playbackWatchdogTimer);
  playbackWatchdogTimer = setTimeout(() => {
    const root = document.getElementById("player-root");
    // A different track since then, or it's playing — either way, done here.
    if (!root || root.dataset.contentId !== contentId) return;
    const audio = activeAudio();
    if (!audio.paused || audio.currentTime > 0) return;
    reportPlayback("playback-stalled", { contentId, readyState: audio.readyState });
    audio.play().catch((err) => reportPlayback("retry-rejected", { contentId, error: String(err?.name || err) }));
  }, PLAYBACK_WATCHDOG_MS);
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
