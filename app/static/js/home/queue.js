// The play queue: what "Play all" builds, what the player's previous/next
// controls move through, and what a finished track advances into.
//
// State only — this module never touches the player. Everything that reacts
// to a queue change (home/overlay.js's transport buttons and auto-advance,
// home/detail.js's shuffle button) listens for the QUEUE_CHANGED event
// below instead of being called directly. That's what keeps the dependency
// one-way: queue.js is imported by the player and the detail panel, and
// imports neither.
//
// Two orderings are kept, not one. `ids` is the channel/playlist's own
// order, exactly as its track list shows it; `order` is the order playback
// actually follows, which is a permutation of `ids` while shuffle is on.
// Collapsing them into a single shuffled array would make turning shuffle
// back off mid-queue impossible — there'd be nothing left that remembered
// where the list really started.

import { api } from "../core.js";

export const QUEUE_CHANGED = "spotea:queuechange";

const QUEUE_KEY = "spotea-queue";

// Survives the reload that resume.js forces on every bfcache restore (which
// on an iOS PWA is every trip to the home screen and back). Without this the
// queue would silently evaporate mid-listen and the track that was playing
// would simply be the last one — same failure the resume record exists to
// prevent, one level up.
let state = { source: null, ids: [], order: [], position: -1, shuffle: false };

function persist() {
  try {
    sessionStorage.setItem(QUEUE_KEY, JSON.stringify(state));
  } catch (err) {
    /* sessionStorage unavailable (private browsing) — the queue just won't outlive the page. */
  }
}

function restore() {
  let saved;
  try {
    saved = JSON.parse(sessionStorage.getItem(QUEUE_KEY) || "null");
  } catch (err) {
    return;
  }
  // Shape-checked rather than trusted: a record written by an older version
  // of this module (or a half-written one) would otherwise turn every
  // nextId() into a crash rather than a missing queue.
  if (!saved || !Array.isArray(saved.ids) || !Array.isArray(saved.order)) return;
  state = {
    source: saved.source ?? null,
    ids: saved.ids,
    order: saved.order,
    position: Number.isInteger(saved.position) ? saved.position : -1,
    shuffle: saved.shuffle === true,
  };
}

restore();

function announce() {
  persist();
  document.dispatchEvent(new CustomEvent(QUEUE_CHANGED));
}

function shuffled(ids) {
  const out = ids.slice();
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

/**
 * Rebuilds `order` for the current shuffle setting, keeping `keepId` where
 * playback already is: first in a freshly shuffled order, or at its real
 * position in the list once shuffle is turned back off. Re-shuffling from
 * scratch and dropping back to position 0 would restart whatever's playing.
 */
function reorder(keepId) {
  if (!state.shuffle) {
    state.order = state.ids.slice();
  } else {
    const rest = state.ids.filter((id) => id !== keepId);
    state.order = keepId == null ? shuffled(state.ids) : [keepId, ...shuffled(rest)];
  }
  state.position = keepId == null ? -1 : state.order.indexOf(keepId);
}

function queueUrl(source) {
  return source.kind === "channel"
    ? `/content/queue/channel/${source.id}`
    : `/content/queue/playlist/${source.kind}`;
}

/** The channel/playlist the loaded queue came from, or null. */
export function queueSource() {
  return state.source;
}

/** The id `offset` steps from the current one, without moving the pointer. */
function peek(offset) {
  const index = state.position + offset;
  if (state.position < 0 || index < 0 || index >= state.order.length) return null;
  return state.order[index];
}

/** Read-only lookahead — drives both the transport's disabled state and the
 *  one-track-ahead download prefetch, neither of which may advance playback. */
export function peekNextId() {
  return peek(1);
}

export function peekPreviousId() {
  return peek(-1);
}

export function isShuffled() {
  return state.shuffle;
}

function step(offset) {
  const id = peek(offset);
  if (id === null) return null;
  state.position += offset;
  announce();
  return id;
}

export function nextId() {
  return step(1);
}

export function previousId() {
  return step(-1);
}

/**
 * Keeps the pointer honest about what's actually playing.
 *
 * Called for every track the player opens, however it was opened. A track
 * inside the queue just moves the pointer (this is what makes clicking row
 * 12 of a playlist continue from row 13). A track from somewhere else — a
 * Home shelf, an Explore result, a resumed session — means the queue no
 * longer describes what's playing, so it's dropped rather than left to
 * advance into an unrelated list the next time a track ends.
 */
export function noteCurrent(contentId) {
  const id = Number(contentId);
  const index = state.order.indexOf(id);
  if (index === -1) {
    if (state.order.length) clearQueue();
    return;
  }
  if (index === state.position) return;
  state.position = index;
  announce();
}

export function clearQueue() {
  state = { source: null, ids: [], order: [], position: -1, shuffle: state.shuffle };
  announce();
}

/**
 * Turns shuffle on or off. A standing preference, not an action: it holds
 * with no queue loaded and decides the order the next loadQueue() builds,
 * which is what lets the detail panel's shuffle button mean the same thing
 * before and during playback.
 */
export function toggleShuffle() {
  state.shuffle = !state.shuffle;
  if (state.order.length) reorder(state.order[state.position] ?? null);
  announce();
  return state.shuffle;
}

/**
 * Loads one channel's or playlist's full track order as the queue.
 *
 * `startId` is the track playback is already on (a track row was clicked and
 * the player is loading it right now) — it becomes the queue's current
 * position rather than restarting from the top, and under shuffle it stays
 * first so the click isn't overridden by the reshuffle.
 *
 * Returns the id to start playing, or null if the request failed or the
 * source turned out to be empty.
 */
export async function loadQueue(source, { startId = null } = {}) {
  const { ok, data } = await api(queueUrl(source), { errorMessage: "Could not load the queue" });
  if (!ok || !data?.ids?.length) return null;
  return setQueue(source, data.ids, { startId });
}

/**
 * The same thing with the ids already in hand.
 *
 * Explore's remote channel/playlist pages have no /content/queue/... endpoint
 * to ask: their rows only become Content rows at the moment playback starts,
 * and the request that creates them hands back the ids in list order (see
 * home/remote.js). Everything after that is an ordinary queue.
 */
export function setQueue(source, ids, { startId = null } = {}) {
  if (!ids?.length) return null;

  state.source = { kind: source.kind, id: source.id ?? null };
  state.ids = ids;
  // Number(): startId reaches here as a string from dataset reads, and the
  // ids are JSON numbers — indexOf across the two would never match.
  const start = startId == null ? null : Number(startId);
  reorder(state.ids.includes(start) ? start : null);
  if (state.position === -1) state.position = 0;
  announce();
  return state.order[state.position];
}
