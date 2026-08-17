// Keeping playback alive across page loads, plus the reload rule that makes
// those loads necessary in the first place.

const RESUME_KEY = "spotea-resume";

// Every page here is rendered once, server-side, with nothing that refreshes
// its dynamic content (Recently Played, storage usage, a channel's track
// list, ...) afterward — a normal navigation is fine since it always re-runs
// the server route, but the browser's back/forward cache can restore a
// page's exact pre-navigation DOM without contacting the server at all
// (e.g. hitting the player's back button, or any native back gesture, after
// having played/downloaded something). That silently shows stale data with
// no error or signal that anything's wrong. Forcing one real reload whenever
// a page comes back this way is simpler and more robust than trying to track
// every specific action that could have changed something while away.
export function installBfcacheReload() {
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    saveResumeState();
    window.location.reload();
  });

  // pageshow's own saveResumeState call (above) only covers the bfcache-
  // restore case — it runs *after* coming back, on the assumption there's
  // still something to snapshot at that moment. But leaving index.html for a
  // real, non-bfcache-eligible navigation (e.g. logging out — everything
  // else, channel/playlist/player included, is a hash change within this
  // same document now) never restores via pageshow at all when you return —
  // landing back on a *fresh* load of
  // index.html instead, where resumeOverlayIfNeeded (home/overlay.js) finds
  // nothing in sessionStorage to resume and the mini-player just doesn't come
  // back. pagehide fires on every departure regardless of whether the page
  // ends up bfcache-eligible, so saving here (in addition to, not instead of,
  // the pageshow save) covers that gap too.
  window.addEventListener("pagehide", saveResumeState);
}

// The reload above throws away whatever's currently loaded in the audio
// element too — a fresh audio.src always starts at 0:00, so without this,
// every backgrounding/foregrounding cycle (this fires constantly on iOS PWA,
// which aggressively reloads backgrounded standalone web apps) would restart
// the current track from the beginning. Snapshotted here, restored by
// player.js's prepareAudio and home/overlay.js's resumeOverlayIfNeeded (the
// Home/Library/Explore overlay, which starts closed on a fresh load and has
// to be reopened first).
function saveResumeState() {
  const audio = document.getElementById("audio");
  const root = document.getElementById("player-root");
  const contentId = root?.dataset.contentId;
  if (!audio || !contentId) return;
  // No src yet means the track is still downloading/converting and playback
  // never actually started — audio.paused is true here for that reason
  // alone, not because anything was deliberately paused. Saving that as
  // wasPlaying: false would wrongly suppress the auto-play that's supposed
  // to happen once the download finishes and prepareAudio's startPlayback
  // runs, leaving a freshly-downloaded track sitting paused at 0:00 instead
  // of playing — indistinguishable from "restarted from the beginning" to
  // whoever's listening.
  if (!audio.src) return;
  try {
    // The overlay element is always present in practice — resume.js only
    // runs on index.html (see pages/index.js), which always renders it —
    // so `: false` below is just a defensive fallback. This flag only
    // matters to home/overlay.js's resumeOverlayIfNeeded, and since
    // pagehide (above) saves on every departure, not just a bfcache-driven
    // reload, a real navigation away from index.html and back (e.g. logging
    // out, then back in) goes through this exact path too. Defaulting to
    // expanded there used to hijack the screen with the full "now playing"
    // overlay just from landing on the SPA with something already playing;
    // the mini bar alone is the least surprising way for that playback to
    // follow you in.
    const overlay = document.getElementById("player-overlay");
    const wasExpanded = overlay ? !overlay.hidden : false;
    sessionStorage.setItem(
      RESUME_KEY,
      JSON.stringify({ contentId, currentTime: audio.currentTime, wasPlaying: !audio.paused, wasExpanded })
    );
  } catch (err) {
    /* sessionStorage unavailable (e.g. private browsing) — losing the resume position is harmless. */
  }
}

/** The saved record, or null. Does not clear it — see consumeResumeState. */
export function readResumeState() {
  try {
    const raw = sessionStorage.getItem(RESUME_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (err) {
    return null;
  }
}

export function clearResumeState() {
  try {
    sessionStorage.removeItem(RESUME_KEY);
  } catch (err) {
    /* sessionStorage unavailable — nothing to clean up. */
  }
}

/**
 * The saved record if it's for this track, consumed on read so a stale one
 * can never apply to some later, unrelated load.
 */
export function consumeResumeState(contentId) {
  const saved = readResumeState();
  if (saved === null) return null;
  clearResumeState();
  return saved.contentId === contentId ? saved : null;
}

// Makes the app installable ("Add to Home Screen" / desktop install prompt)
// — see /sw.js and static/manifest.json. Registered from every page rather
// than just index.html so installing works no matter which page happens to
// be open when the browser offers it.
export function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Not fatal — the app works the same without it, just not installable.
    });
  });
}
