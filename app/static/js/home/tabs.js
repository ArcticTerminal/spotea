// Home/Library/Explore/Settings are panels in one document, not four pages.
// The fifth panel, "detail" (a channel or a pinned playlist), is driven by
// home/detail.js instead — it has its own popstate listener for that; this
// one only acts when the hash resolves to one of the four tabs above, or to
// neither a tab nor a detail/player route.

import { classifyHash } from "../core.js";

const TAB_STORAGE_KEY = "spotea-active-tab";

// Exported so home/detail.js can switch to the "detail" panel the same way a
// tab click does — but it never carries "detail" itself into the URL or
// localStorage (there's no #detail button to reselect, and the URL for a
// detail view is a channel/playlist route detail.js owns, not this). Callers
// that already know the URL is correct (initial sync, popstate) pass
// updateHistory: false so this doesn't stomp it with a redundant replaceState.
export function activate(tabName, { updateHistory = true } = {}) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const isActive = btn.dataset.tab === tabName;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-selected", String(isActive));
  });
  // Panel visibility itself is driven by the data-active-tab attribute via
  // CSS (see style.css) — this is what index.html's inline head script also
  // sets, so both the first paint and later clicks go through the same path.
  document.documentElement.dataset.activeTab = tabName;
  if (tabName !== "detail") localStorage.setItem(TAB_STORAGE_KEY, tabName);
  // replaceState (not pushState) so cycling through tabs doesn't spam the
  // back-button history — the URL just needs to be right for a refresh.
  if (updateHistory && tabName !== "detail") history.replaceState(null, "", `#${tabName}`);
}

export function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  if (!tabButtons.length) return;

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  });

  // Needed because most tab switches only replaceState (see above), but
  // leaving Home via a channel/playlist link is a real navigation — without
  // this, pressing back would update the URL's hash without the visible
  // panel following it. Detail/player routes are home/detail.js's own
  // popstate listener's job, not this one's.
  window.addEventListener("popstate", () => {
    const info = classifyHash(location.hash.slice(1));
    if (info.type === "tab") activate(info.tab);
    else if (info.type === "unknown") activate("home");
  });

  // The inline head script already resolved the correct initial tab (URL
  // hash, falling back to localStorage) and set it on <html> before first
  // paint — this just syncs the button/UI state to match, without rewriting
  // a URL that's already right (and, for a detail route, isn't this
  // module's to rewrite at all).
  activate(document.documentElement.dataset.activeTab || "home", { updateHistory: false });
}
