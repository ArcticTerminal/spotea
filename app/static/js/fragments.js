// Re-rendering parts of the page from the server instead of patching them by
// hand.
//
// Home's shelves, Library's grid and the Downloads list are all derived from
// the database, and all of them used to be updated in place by bespoke JS
// after every action that could have changed them — six patchers, each one
// having to know which actions applied to it. Now the server re-renders the
// region (see routers/partials.py) and this swaps it in.
//
// refreshFragments() deliberately takes no arguments: "which parts did this
// action invalidate?" is precisely the question that kept being answered
// wrong, so it isn't asked. Every present region is refreshed. That's three
// small requests, on a household app, in exchange for the whole class of
// "stale until reload" bugs — and it also picks up changes this tab never
// made (the background feed refresh, another device).

const FRAGMENTS = [
  { name: "home", targets: ["home-shelves"] },
  { name: "library", targets: ["library-grid"] },
  { name: "downloads", targets: ["downloads-body", "settings-storage-desc"] },
];

const afterSwapCallbacks = [];

/**
 * Register work that has to run again whenever swapped-in markup replaces
 * elements a module had already wired up. Keep these idempotent — a
 * fragment can be swapped any number of times.
 */
export function onFragmentsSwapped(callback) {
  afterSwapCallbacks.push(callback);
}

// Horizontal scroll position lives on the DOM node, so it's lost when a shelf
// row is replaced. Without this, saving something from a shelf you'd scrolled
// halfway along would silently jump it back to the start.
function captureScroll(root) {
  const positions = new Map();
  root.querySelectorAll("[id]").forEach((el) => {
    if (el.scrollLeft > 0) positions.set(el.id, el.scrollLeft);
  });
  return positions;
}

function restoreScroll(positions) {
  positions.forEach((scrollLeft, id) => {
    const el = document.getElementById(id);
    if (el) el.scrollLeft = scrollLeft;
  });
}

// Parses a fragment response's <template data-target="…"> block(s) and
// swaps each into the matching element by id, preserving that element's
// horizontal scroll position across the swap. Shared with home/detail.js,
// which fetches /partials/detail/... the same way but drives its own
// loading state and error handling instead of refreshOne's silent-fail one
// (a failed background refresh is invisible by design; a failed detail
// panel load is the entire thing the user just asked for).
export function swapFragmentHtml(html) {
  const holder = document.createElement("div");
  holder.innerHTML = html;

  let swapped = false;
  holder.querySelectorAll("template[data-target]").forEach((template) => {
    const target = document.getElementById(template.dataset.target);
    if (!target) return;
    const positions = captureScroll(target);
    target.replaceChildren(template.content.cloneNode(true));
    restoreScroll(positions);
    swapped = true;
  });
  return swapped;
}

async function refreshOne({ name, targets }) {
  // Skip regions this page doesn't have — content-actions.js runs while a
  // detail panel is open too, where none of these exist.
  const present = targets.filter((id) => document.getElementById(id));
  if (!present.length) return false;

  let html;
  try {
    const res = await fetch(`/partials/${name}`);
    if (!res.ok) return false;
    html = await res.text();
  } catch (err) {
    // A failed refresh leaves the previous markup in place, which is exactly
    // the state the page was already in. Nothing to tell the user.
    return false;
  }

  return swapFragmentHtml(html);
}

export async function refreshFragments() {
  const results = await Promise.all(FRAGMENTS.map(refreshOne));
  if (results.some(Boolean)) {
    for (const callback of afterSwapCallbacks) callback();
  }
}
