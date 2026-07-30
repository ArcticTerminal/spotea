function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

async function toggleSaved(contentId, button) {
  const card = document.querySelector(`.card[data-content-id="${contentId}"]`);
  const isSaved = card?.dataset.saved === "true";

  try {
    const res = await fetch(`/content/${contentId}/save`, {
      method: isSaved ? "DELETE" : "POST",
    });
    if (!res.ok) {
      showToast("Could not update saved items");
      return;
    }
    const data = await res.json();

    if (card) card.dataset.saved = String(data.is_saved);
    if (button) applySavedState(button, data.is_saved);

    // Un-saving while the Saved filter is active should drop it from view.
    if (document.getElementById("channel-filter")?.value === SAVED_FILTER_VALUE) {
      refreshGridView();
    }
  } catch (err) {
    showToast("Could not update saved items");
  }
}

function applySavedState(button, isSaved) {
  button.classList.toggle("is-on", isSaved);
  button.setAttribute("aria-pressed", String(isSaved));
  button.title = isSaved ? "Saved for later" : "Save for later";
  const svg = button.querySelector("svg");
  if (svg) svg.setAttribute("fill", isSaved ? "currentColor" : "none");
}

function setupContentGrid() {
  const grid = document.getElementById("content-grid");
  if (!grid) return;

  // Downloading is no longer a card-level action — playing something is what
  // fetches it — so the only interactive control left here is the save toggle.
  grid.addEventListener("click", (event) => {
    const saveBtn = event.target.closest(".btn-save");
    if (saveBtn) toggleSaved(saveBtn.dataset.contentId, saveBtn);
  });
}

const SORT_STORAGE_KEY = "spotifrei-sort";
const FILTER_STORAGE_KEY = "spotifrei-channel-filter";
const FAVORITES_FILTER_VALUE = "__favorites__";
const SAVED_FILTER_VALUE = "__saved__";
const PAGE_SIZE = 20;

let currentPage = 1;

const SORT_COMPARATORS = {
  "date-desc": (a, b) => (b.dataset.published || "").localeCompare(a.dataset.published || ""),
  "date-asc": (a, b) => (a.dataset.published || "").localeCompare(b.dataset.published || ""),
  "title-asc": (a, b) => a.dataset.title.localeCompare(b.dataset.title, undefined, { sensitivity: "base" }),
  "title-desc": (a, b) => b.dataset.title.localeCompare(a.dataset.title, undefined, { sensitivity: "base" }),
  "channel-asc": (a, b) =>
    a.dataset.channel.localeCompare(b.dataset.channel, undefined, { sensitivity: "base" }),
};

function updatePaginationControls(totalPages, totalItems) {
  const pagination = document.getElementById("pagination");
  const indicator = document.getElementById("page-indicator");
  const prevBtn = document.getElementById("prev-page");
  const nextBtn = document.getElementById("next-page");
  if (!pagination || !indicator || !prevBtn || !nextBtn) return;

  if (totalItems === 0) {
    pagination.hidden = false;
    indicator.textContent = "No matches";
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }

  pagination.hidden = totalPages <= 1;
  indicator.textContent = `Page ${currentPage} of ${totalPages}`;
  prevBtn.disabled = currentPage <= 1;
  nextBtn.disabled = currentPage >= totalPages;
}

function refreshGridView() {
  const grid = document.getElementById("content-grid");
  if (!grid) return;

  const filterValue = document.getElementById("channel-filter")?.value || "";
  const sortValue = document.getElementById("sort-select")?.value || "date-desc";
  const comparator = SORT_COMPARATORS[sortValue] || SORT_COMPARATORS["date-desc"];

  const allCards = Array.from(grid.querySelectorAll(".card"));
  const filtered = allCards
    .filter((card) => {
      if (filterValue === FAVORITES_FILTER_VALUE) return card.dataset.favorite === "true";
      if (filterValue === SAVED_FILTER_VALUE) return card.dataset.saved === "true";
      return !filterValue || card.dataset.channel === filterValue;
    })
    .sort(comparator);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  currentPage = Math.min(Math.max(1, currentPage), totalPages);

  const start = (currentPage - 1) * PAGE_SIZE;
  const pageItems = filtered.slice(start, start + PAGE_SIZE);
  const visibleIds = new Set(pageItems.map((card) => card.dataset.contentId));

  allCards.forEach((card) => {
    card.hidden = !visibleIds.has(card.dataset.contentId);
  });
  pageItems.forEach((card) => grid.appendChild(card));

  updatePaginationControls(totalPages, filtered.length);
}

function setupSorting() {
  const select = document.getElementById("sort-select");
  if (!select) return;

  const saved = localStorage.getItem(SORT_STORAGE_KEY);
  if (saved && SORT_COMPARATORS[saved]) {
    select.value = saved;
  }

  select.addEventListener("change", () => {
    localStorage.setItem(SORT_STORAGE_KEY, select.value);
    currentPage = 1;
    refreshGridView();
  });
}

function setupChannelFilter() {
  const select = document.getElementById("channel-filter");
  if (!select) return;

  const saved = localStorage.getItem(FILTER_STORAGE_KEY);
  if (saved && Array.from(select.options).some((opt) => opt.value === saved)) {
    select.value = saved;
  }

  select.addEventListener("change", () => {
    localStorage.setItem(FILTER_STORAGE_KEY, select.value);
    currentPage = 1;
    refreshGridView();
  });
}

function setupPagination() {
  const prevBtn = document.getElementById("prev-page");
  const nextBtn = document.getElementById("next-page");
  if (!prevBtn || !nextBtn) return;

  prevBtn.addEventListener("click", () => {
    currentPage -= 1;
    refreshGridView();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  nextBtn.addEventListener("click", () => {
    currentPage += 1;
    refreshGridView();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}

async function refreshFeeds() {
  const overlay = document.getElementById("refresh-overlay");
  if (overlay) overlay.hidden = false;

  try {
    const res = await fetch("/feeds/refresh", { method: "POST" });
    if (!res.ok) throw new Error("refresh failed");
    const data = await res.json();
    if (data.new_content_count > 0) {
      window.location.reload();
      return;
    }
  } catch (err) {
    // Existing content stays on screen; a failed background refresh isn't critical.
  } finally {
    if (overlay) overlay.hidden = true;
  }
}

function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

function formatSubscribers(count) {
  if (count == null) return "";
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M subscribers`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}K subscribers`;
  return `${count} subscribers`;
}

function renderSearchResults(results) {
  const list = document.getElementById("channel-search-results");
  if (!list) return;

  if (!results.length) {
    list.innerHTML = `<li class="search-empty">No channels found</li>`;
    return;
  }

  list.innerHTML = results
    .map((r) => {
      const thumb = r.thumbnail_url
        ? `<img class="search-result-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" />`
        : `<span class="search-result-thumb"></span>`;
      const subs =
        r.subscriber_count != null
          ? `<span class="search-result-subs">${formatSubscribers(r.subscriber_count)}</span>`
          : "";
      return `
        <li class="search-result">
          ${thumb}
          <div class="search-result-info">
            <span class="search-result-title">${escapeHtml(r.title)}</span>
            ${subs}
          </div>
          <button type="button" class="btn-add-channel" data-channel-url="${escapeHtml(r.channel_url)}">Add</button>
        </li>
      `;
    })
    .join("");
}

async function addChannel(channelUrl, button) {
  if (button) {
    button.disabled = true;
    button.textContent = "Adding…";
  }

  try {
    const res = await fetch("/feeds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_url: channelUrl }),
    });

    if (res.ok) {
      window.location.reload();
      return;
    }

    if (res.status === 409) {
      if (button) button.textContent = "Already added";
      return;
    }

    const data = await res.json().catch(() => ({}));
    if (button) {
      button.disabled = false;
      button.textContent = "Add";
    }
    showToast(data.detail || "Could not add channel");
  } catch (err) {
    if (button) {
      button.disabled = false;
      button.textContent = "Add";
    }
  }
}

function setupChannelSearch() {
  const input = document.getElementById("channel-search-input");
  const resultsList = document.getElementById("channel-search-results");
  if (!input || !resultsList) return;

  const runSearch = debounce(async (query) => {
    if (!query) {
      resultsList.innerHTML = "";
      return;
    }
    try {
      const res = await fetch(`/feeds/search?q=${encodeURIComponent(query)}`);
      if (!res.ok) return;
      renderSearchResults(await res.json());
    } catch (err) {
      // ignore transient search errors
    }
  }, 400);

  input.addEventListener("input", () => runSearch(input.value.trim()));

  resultsList.addEventListener("click", (event) => {
    const btn = event.target.closest(".btn-add-channel");
    if (!btn) return;
    addChannel(btn.dataset.channelUrl, btn);
  });
}

function setupFeedForm() {
  const form = document.getElementById("feed-form");
  const errorEl = document.getElementById("feed-error");
  if (!form) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.hidden = true;

    const channelUrl = document.getElementById("feed-url").value.trim();
    const submitBtn = form.querySelector("button[type=submit]");
    submitBtn.disabled = true;

    try {
      const res = await fetch("/feeds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel_url: channelUrl }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        errorEl.textContent = data.detail || "Could not add feed";
        errorEl.hidden = false;
        return;
      }

      window.location.reload();
    } finally {
      submitBtn.disabled = false;
    }
  });
}

const TAB_STORAGE_KEY = "spotifrei-active-tab";

function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const panels = document.querySelectorAll(".tab-panel");
  if (!tabButtons.length) return;

  function activate(tabName) {
    tabButtons.forEach((btn) => {
      const isActive = btn.dataset.tab === tabName;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", String(isActive));
    });
    panels.forEach((panel) => {
      panel.hidden = panel.dataset.tabPanel !== tabName;
    });
    localStorage.setItem(TAB_STORAGE_KEY, tabName);
  }

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => activate(btn.dataset.tab));
  });

  const saved = localStorage.getItem(TAB_STORAGE_KEY);
  const savedIsValid = saved && document.querySelector(`.tab-btn[data-tab="${saved}"]`);
  activate(savedIsValid ? saved : "library");
}

function setupStorage() {
  const clearBtn = document.getElementById("clear-storage");
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      const confirmed = await confirmDialog(
        "Delete all downloaded audio? Your channels and saved items stay — you can download anything again by playing it.",
        "Clear all"
      );
      if (!confirmed) return;

      const res = await fetch("/storage", { method: "DELETE" });
      if (res.ok) window.location.reload();
      else showToast("Could not clear downloads");
    });
  }

  const list = document.getElementById("storage-list");
  if (!list) return;

  list.addEventListener("click", async (event) => {
    const btn = event.target.closest(".storage-remove");
    if (!btn) return;

    const confirmed = await confirmDialog(
      "Remove this download? You can get it back by playing it again.",
      "Remove"
    );
    if (!confirmed) return;

    const res = await fetch(`/content/${btn.dataset.contentId}`, { method: "DELETE" });
    if (res.ok) window.location.reload();
    else showToast("Could not remove this download");
  });
}

function setupUnfollowButtons() {
  document.querySelectorAll(".unfollow").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const confirmed = await confirmDialog(
        "Unfollow this channel? Its videos will be removed from your library.",
        "Unfollow"
      );
      if (!confirmed) return;

      const res = await fetch(`/feeds/${btn.dataset.feedId}`, { method: "DELETE" });
      if (res.ok) window.location.reload();
      else showToast("Could not unfollow this channel");
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupChannelSearch();
  setupFeedForm();
  setupUnfollowButtons();
  setupStorage();
  setupContentGrid();
  setupSorting();
  setupChannelFilter();
  setupPagination();
  refreshGridView();
  refreshFeeds();
});
