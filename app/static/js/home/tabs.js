// Home/Library/Explore/Settings are panels in one document, not four pages.

const TAB_STORAGE_KEY = "spotea-active-tab";

export function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  if (!tabButtons.length) return;

  function activate(tabName) {
    tabButtons.forEach((btn) => {
      const isActive = btn.dataset.tab === tabName;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", String(isActive));
    });
    // Panel visibility itself is driven by the data-active-tab attribute via
    // CSS (see style.css) — this is what index.html's inline head script also
    // sets, so both the first paint and later clicks go through the same path.
    document.documentElement.dataset.activeTab = tabName;
    localStorage.setItem(TAB_STORAGE_KEY, tabName);
    // replaceState (not pushState) so cycling through tabs doesn't spam the
    // back-button history — the URL just needs to be right for a refresh.
    history.replaceState(null, "", `#${tabName}`);
  }

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  });

  // Needed because most tab switches only replaceState (see above), but
  // leaving Home via a channel chip is a real navigation — without this,
  // pressing back would update the URL's hash without the visible panel
  // following it.
  window.addEventListener("popstate", () => {
    const requested = location.hash.slice(1);
    const isValid = Array.from(tabButtons).some((btn) => btn.dataset.tab === requested);
    activate(isValid ? requested : "home");
  });

  // The inline head script already resolved the correct initial tab (URL
  // hash, falling back to localStorage) and set it on <html> before first
  // paint — this just syncs the button/URL state to match.
  activate(document.documentElement.dataset.activeTab || "home");
}
