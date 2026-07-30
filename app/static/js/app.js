const POLL_INTERVAL_MS = 1500;

const TRASH_ICON_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>`;

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function cardActionHtml(contentId, status, errorMessage) {
  if (status === "ready") {
    return `
      <a class="btn-play" href="/player/${contentId}">▶ Play</a>
      <button type="button" class="btn-delete" data-content-id="${contentId}" aria-label="Delete download">${TRASH_ICON_SVG}</button>
    `;
  }
  if (status === "downloading") {
    return `<span class="spinner" role="status" aria-label="Downloading"></span>`;
  }
  if (status === "error") {
    return `
      <span class="status-error" title="${escapeHtml(errorMessage)}">Failed</span>
      <button type="button" class="btn-download" data-content-id="${contentId}">Retry</button>
    `;
  }
  return `<button type="button" class="btn-download" data-content-id="${contentId}">Download</button>`;
}

function updateCard(contentId, status, errorMessage) {
  const card = document.querySelector(`.card[data-content-id="${contentId}"]`);
  if (!card) return;
  card.dataset.status = status;
  const action = card.querySelector(".card-action");
  if (action) action.innerHTML = cardActionHtml(contentId, status, errorMessage);
}

function pollStatus(contentId) {
  const timer = setInterval(async () => {
    try {
      const res = await fetch(`/content/${contentId}/status`);
      if (!res.ok) throw new Error("status check failed");
      const data = await res.json();
      updateCard(contentId, data.status, data.error_message);
      if (data.status === "ready" || data.status === "error") {
        clearInterval(timer);
      }
    } catch (err) {
      clearInterval(timer);
    }
  }, POLL_INTERVAL_MS);
}

async function startDownload(contentId) {
  updateCard(contentId, "downloading");
  try {
    const res = await fetch(`/content/${contentId}/download`, { method: "POST" });
    if (!res.ok && res.status !== 409) {
      const data = await res.json().catch(() => ({}));
      updateCard(contentId, "error", data.detail || "Could not start download");
      return;
    }
    pollStatus(contentId);
  } catch (err) {
    updateCard(contentId, "error", "Could not start download");
  }
}

async function deleteContent(contentId) {
  if (!confirm("Delete the downloaded audio? You can re-download it later.")) return;
  const res = await fetch(`/content/${contentId}`, { method: "DELETE" });
  if (!res.ok) return;
  const data = await res.json();
  updateCard(contentId, data.status);
}

async function toggleFavorite(contentId, button) {
  const card = document.querySelector(`.card[data-content-id="${contentId}"]`);
  const isFavorite = card?.dataset.favorite === "true";

  try {
    const res = await fetch(`/content/${contentId}/favorite`, {
      method: isFavorite ? "DELETE" : "POST",
    });
    if (!res.ok) return;
    const data = await res.json();

    if (card) card.dataset.favorite = String(data.is_favorite);
    if (button) {
      button.classList.toggle("is-favorite", data.is_favorite);
      button.setAttribute("aria-pressed", String(data.is_favorite));
    }

    if (document.getElementById("channel-filter")?.value === FAVORITES_FILTER_VALUE) {
      refreshGridView();
    }
  } catch (err) {
    // ignore transient errors
  }
}

function setupContentGrid() {
  const grid = document.getElementById("content-grid");
  if (!grid) return;

  grid.addEventListener("click", (event) => {
    const downloadBtn = event.target.closest(".btn-download");
    if (downloadBtn) {
      startDownload(downloadBtn.dataset.contentId);
      return;
    }

    const deleteBtn = event.target.closest(".btn-delete");
    if (deleteBtn) {
      deleteContent(deleteBtn.dataset.contentId);
      return;
    }

    const favoriteBtn = event.target.closest(".btn-favorite");
    if (favoriteBtn) {
      toggleFavorite(favoriteBtn.dataset.contentId, favoriteBtn);
    }
  });

  grid.querySelectorAll('.card[data-status="downloading"]').forEach((card) => {
    pollStatus(card.dataset.contentId);
  });
}

const SORT_STORAGE_KEY = "spotifrei-sort";
const FILTER_STORAGE_KEY = "spotifrei-channel-filter";
const FAVORITES_FILTER_VALUE = "__favorites__";
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
  const favoritesOnly = filterValue === FAVORITES_FILTER_VALUE;
  const sortValue = document.getElementById("sort-select")?.value || "date-desc";
  const comparator = SORT_COMPARATORS[sortValue] || SORT_COMPARATORS["date-desc"];

  const allCards = Array.from(grid.querySelectorAll(".card"));
  const filtered = allCards
    .filter((card) =>
      favoritesOnly ? card.dataset.favorite === "true" : !filterValue || card.dataset.channel === filterValue
    )
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
    alert(data.detail || "Could not add channel");
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

function setupUnfollowButtons() {
  document.querySelectorAll(".unfollow").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Unfollow this channel?")) return;
      const res = await fetch(`/feeds/${btn.dataset.feedId}`, { method: "DELETE" });
      if (res.ok) window.location.reload();
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupChannelSearch();
  setupFeedForm();
  setupUnfollowButtons();
  setupContentGrid();
  setupSorting();
  setupChannelFilter();
  setupPagination();
  refreshGridView();
  refreshFeeds();
});
