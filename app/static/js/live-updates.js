// Patching the current page after an action changes something the server
// rendered.
//
// Everything on Home and in Library is computed once, server-side, at render
// time. Nothing revalidates it. So a save, a favorite, a first play or a
// finished download has to be reflected into the DOM by hand, or it stays
// invisible until the next full reload. These are those patches, collected
// in one place instead of scattered across whichever module happened to
// trigger the change.
//
// Every function here no-ops when the elements it patches aren't present, so
// modules shared with the standalone pages (content-actions.js, player.js)
// can call them unconditionally — that's what replaced the
// `typeof syncSavedShelf === "function"` guards this used to need.

// Library tab's summary tiles (Favorites/Saved for later/Recently Played)
// show a count that's only computed at page render. New Uploads' tile is
// deliberately left out: unlike these three, its count isn't a simple ±1 per
// user action (see refreshFeeds's comment on why it can only be trusted
// after a reload).
export function bumpLibraryCount(id, delta) {
  const el = document.getElementById(id);
  if (!el) return;
  const next = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
  el.textContent = `${next} video${next === 1 ? "" : "s"}`;
}

/**
 * Add or remove a card in one of Home's shelves, cloning an existing card
 * for this content from anywhere on the page. The whole shelf (title
 * included) stays hidden while empty.
 *
 * Cloning rather than building markup means a track that isn't rendered
 * anywhere on this page can't be added — that case is left for the next real
 * page load rather than hand-writing _content_card.html's markup a second
 * time in JS.
 */
function syncShelf(shelfId, rowId, contentId, present) {
  const shelf = document.getElementById(shelfId);
  const row = document.getElementById(rowId);
  if (!shelf || !row) return;

  const existing = row.querySelector(`.card[data-content-id="${contentId}"]`);

  if (present && !existing) {
    const source = document.querySelector(`.card[data-content-id="${contentId}"]`);
    if (source) {
      const clone = source.cloneNode(true);
      clone.hidden = false; // the source may be paginated out of view in Library
      row.prepend(clone);
    }
  } else if (!present && existing) {
    existing.remove();
  }

  shelf.hidden = row.children.length === 0;
}

export function syncSavedShelf(contentId, isSaved) {
  syncShelf("home-shelf-saved", "home-saved-row", contentId, isSaved);
}

export function syncFavoritesShelf(contentId, isFavorite) {
  syncShelf("home-shelf-favorites", "home-favorites-row", contentId, isFavorite);
}

// Not syncShelf: a replay has to *move* an already-present card back to the
// front, not leave it where it is. This shelf used to stay fresh for free,
// because playing anything meant navigating to /player/{id} and back, which
// re-rendered the whole page — now that Home/Library/Explore play through
// the in-page overlay, nothing re-runs the server route afterward.
export function syncRecentlyPlayedShelf(contentId) {
  const shelf = document.getElementById("home-shelf-recently-played");
  const row = document.getElementById("home-recently-played-row");
  if (!shelf || !row) return;

  const existingInRow = row.querySelector(`.card[data-content-id="${contentId}"]`);
  const source = existingInRow || document.querySelector(`.card[data-content-id="${contentId}"]`);
  if (!source) return;

  if (existingInRow) existingInRow.remove();
  const clone = source.cloneNode(true);
  clone.hidden = false;
  row.prepend(clone);
  shelf.hidden = false;
}

// Must match _content_card.html / _content_row.html, which render these
// server-side when the row is already downloaded — the geometry itself comes
// from the sprite in _icons.html, so only the wrapper differs between them.
const CARD_BADGE =
  '<span class="downloaded-badge" title="Downloaded — plays instantly"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><use href="#i-check" /></svg></span>';
const ROW_BADGE =
  '<svg class="track-downloaded" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" title="Downloaded — plays instantly"><use href="#i-check" /></svg>';

// The "Downloaded — plays instantly" badge is only ever written by the Jinja
// templates at render time (item.status == "ready"), so a track that was
// still downloading when its card was rendered never gets one once the
// download actually finishes mid-session. Patches every instance of this
// content currently on the page — it can appear in more than one shelf/list,
// including a card syncRecentlyPlayedShelf just cloned.
export function markContentDownloaded(contentId) {
  document.querySelectorAll(`.card[data-content-id="${contentId}"]`).forEach((card) => {
    card.dataset.status = "ready";
    const thumb = card.querySelector(".thumb");
    if (thumb && !thumb.querySelector(".downloaded-badge")) {
      thumb.insertAdjacentHTML("beforeend", CARD_BADGE);
    }
  });

  document.querySelectorAll(`.track-row[data-content-id="${contentId}"]`).forEach((row) => {
    row.dataset.status = "ready";
    const durationEl = row.querySelector(".track-duration");
    if (durationEl && !durationEl.querySelector(".track-downloaded")) {
      durationEl.insertAdjacentHTML("afterbegin", ROW_BADGE);
    }
  });
}
