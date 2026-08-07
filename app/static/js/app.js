function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// Home's "Saved for later" shelf is only populated at page render, so a
// save/un-save needs to patch it in live or it wouldn't show up until the
// next full reload. The whole shelf (title included) stays hidden while empty.
function syncSavedShelf(contentId, isSaved) {
  const shelf = document.getElementById("home-shelf-saved");
  const row = document.getElementById("home-saved-row");
  if (!shelf || !row) return;

  const existing = row.querySelector(`.card[data-content-id="${contentId}"]`);

  if (isSaved && !existing) {
    const source = document.querySelector(`.card[data-content-id="${contentId}"]`);
    if (source) {
      const clone = source.cloneNode(true);
      clone.hidden = false; // the source may be paginated out of view in Library
      row.prepend(clone);
    }
  } else if (!isSaved && existing) {
    existing.remove();
  }

  shelf.hidden = row.children.length === 0;
}

function setupHomeChannels() {
  const row = document.getElementById("home-channel-row");
  if (!row) return;

  row.addEventListener("click", (event) => {
    const chip = event.target.closest(".channel-chip");
    if (!chip) return;
    window.location.href = `/channel/${chip.dataset.feedId}`;
  });
}

// Every card (including the pinned Favorites/Saved tiles) is already
// server-rendered in the DOM, so filtering is just a show/hide over what's
// there — no round trip needed the way the old video-grid search had one.
function setupLibrarySearch() {
  const input = document.getElementById("library-search-input");
  const grid = document.querySelector("#tab-library .channel-grid");
  const emptyState = document.getElementById("channel-search-empty");
  if (!input || !grid) return;

  const cards = Array.from(grid.querySelectorAll(".channel-card"));

  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    let visibleCount = 0;

    cards.forEach((card) => {
      const title = card.querySelector(".channel-card-title")?.textContent.toLowerCase() ?? "";
      const matches = !query || title.includes(query);
      card.hidden = !matches;
      if (matches) visibleCount++;
    });

    if (emptyState) emptyState.hidden = visibleCount > 0;
  });
}

function setupHorizontalScrollers() {
  // Shelf/channel rows are wider than their container by design. Genuine
  // horizontal gestures (trackpad two-finger swipe, shift+wheel, a tilt
  // wheel) already scroll these natively via overflow-x:auto — no JS
  // needed. What's missing is click-and-drag for plain mouse users, which
  // this adds. (An earlier version also redirected plain vertical wheel
  // scroll into horizontal movement, but that hijacked normal page
  // scrolling anywhere the cursor was over a row, effectively freezing it —
  // removed.)
  document.querySelectorAll(".shelf-row, .channel-row").forEach((row) => {
    // The grab cursor (and drag-to-scroll below) should only kick in once a
    // row actually has overflow to scroll through — otherwise it's a false
    // affordance for a gesture that does nothing. Card widths are fixed by
    // CSS, so only the row's own box size (i.e. viewport width) can change
    // whether it overflows, which is exactly what ResizeObserver reports.
    const updateScrollable = () => {
      row.classList.toggle("is-scrollable", row.scrollWidth > row.clientWidth + 1);
    };
    updateScrollable();
    new ResizeObserver(updateScrollable).observe(row);

    // Links and images are natively draggable — without this, pressing down
    // on a card's thumbnail and moving the mouse makes the browser start its
    // own "drag this link/image out" gesture instead of firing the mousemove
    // events below, so the custom scroll never happens.
    row.addEventListener("dragstart", (event) => event.preventDefault());

    let isDown = false;
    let dragged = false;
    let startX = 0;
    let startScroll = 0;

    row.addEventListener("mousedown", (event) => {
      if (!row.classList.contains("is-scrollable")) return;
      isDown = true;
      dragged = false;
      startX = event.pageX;
      startScroll = row.scrollLeft;
      row.classList.add("dragging");
    });

    window.addEventListener("mouseup", () => {
      isDown = false;
      row.classList.remove("dragging");
    });

    row.addEventListener("mouseleave", () => {
      isDown = false;
      row.classList.remove("dragging");
    });

    row.addEventListener("mousemove", (event) => {
      if (!isDown) return;
      const delta = event.pageX - startX;
      if (Math.abs(delta) > 5) dragged = true;
      row.scrollLeft = startScroll - delta;
      event.preventDefault();
    });

    // A drag that happened to pass over a card/chip shouldn't also trigger
    // its click (playing a video, following a channel filter, etc.).
    row.addEventListener(
      "click",
      (event) => {
        if (dragged) {
          event.preventDefault();
          event.stopPropagation();
        }
      },
      true
    );
  });
}

async function refreshFeeds() {
  const overlay = document.getElementById("refresh-overlay");
  const btn = document.getElementById("refresh-feeds-btn");
  if (overlay) overlay.hidden = false;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("is-spinning");
  }

  try {
    const res = await fetch("/feeds/refresh", { method: "POST" });
    if (!res.ok) throw new Error("refresh failed");
    const data = await res.json();
    if (data.new_content_count > 0) {
      window.location.reload();
      return;
    }
  } catch (err) {
    showToast("Could not refresh feeds");
  } finally {
    if (overlay) overlay.hidden = true;
    if (btn) {
      btn.disabled = false;
      btn.classList.remove("is-spinning");
    }
  }
}

// Refreshing every followed channel's RSS is several network calls per channel,
// so it's only worth doing once when the app is first opened in a browser
// session — not on every reload. A manual button covers everything else.
const SESSION_REFRESH_KEY = "spotea-session-refreshed";

function maybeAutoRefresh() {
  if (sessionStorage.getItem(SESSION_REFRESH_KEY)) return;
  sessionStorage.setItem(SESSION_REFRESH_KEY, "1");
  refreshFeeds();
}

function setupRefreshButton() {
  const btn = document.getElementById("refresh-feeds-btn");
  if (!btn) return;
  btn.addEventListener("click", () => refreshFeeds());
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
  // Shown immediately, before the request even starts: add_feed's RSS sync
  // alone can take a couple of seconds, and leaving the screen looking idle
  // for that stretch (only the button says anything) reads as nothing
  // happening yet.
  showBackfillOverlay("Fetching RSS feed…", "");

  try {
    const res = await fetch("/feeds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_url: channelUrl }),
    });

    if (res.ok) {
      const data = await res.json().catch(() => null);
      if (data?.feed?.id != null) {
        await waitForBackfillThenReload(data.feed.id, data.feed.channel_title || channelUrl);
      } else {
        window.location.reload();
      }
      return;
    }

    hideBackfillOverlay();
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
    hideBackfillOverlay();
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

const TAB_STORAGE_KEY = "spotea-active-tab";

function setupTabs() {
  const tabButtons = document.querySelectorAll(".tab-btn");
  if (!tabButtons.length) return;

  function activate(tabName) {
    tabButtons.forEach((btn) => {
      const isActive = btn.dataset.tab === tabName;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-selected", String(isActive));
    });
    // Panel visibility itself is driven by the data-active-tab attribute via
    // CSS (see style.css) — this is what the inline head script also sets,
    // so both the first paint and later clicks go through the same path.
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
  // setupHomeChannels() does pushState once when leaving Home via a channel
  // chip — without this, pressing back would update the URL's hash without
  // the visible panel following it.
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

function setupDownloadsOverlay() {
  const overlay = document.getElementById("downloads-overlay");
  const openBtn = document.getElementById("open-downloads");
  const closeBtn = document.getElementById("downloads-close");
  if (!overlay || !openBtn) return;

  const open = () => {
    overlay.hidden = false;
  };
  const close = () => {
    overlay.hidden = true;
  };

  openBtn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !overlay.hidden) close();
  });
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

  const clearPlayedBtn = document.getElementById("clear-recently-played");
  if (clearPlayedBtn) {
    clearPlayedBtn.addEventListener("click", async () => {
      const confirmed = await confirmDialog(
        "Clear your recently played history? This only affects the Home shelf — nothing gets deleted.",
        "Clear"
      );
      if (!confirmed) return;

      const res = await fetch("/content/recently-played", { method: "DELETE" });
      if (res.ok) window.location.reload();
      else showToast("Could not clear recently played");
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

function setupSettings() {
  const form = document.getElementById("settings-form");
  if (!form) return;

  form.addEventListener("change", async (event) => {
    const input = event.target.closest('input[name="audio_quality"]');
    if (!input) return;

    try {
      const res = await fetch("/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audio_quality: input.value }),
      });
      if (!res.ok) throw new Error("update failed");
    } catch (err) {
      showToast("Could not update audio quality");
    }
  });
}

function isActiveBackfillPhase(phase) {
  return phase === "scanning" || phase === "saving";
}

// Split into a title (what's happening) and a short, single-line detail (the
// count) instead of one string — concatenating them let the browser wrap
// mid-phrase wherever it pleased (e.g. "…page" on one line, "7" on the
// next), which read as broken. Keeping the count in its own nowrap element
// keeps it atomic no matter how the title line wraps.
function backfillPhaseParts(phase, done, total) {
  if (phase === "scanning") {
    if (total > 0) return { title: "Fetching channel history…", detail: `${done}/${total} videos found` };
    if (done > 0) return { title: "Fetching channel history…", detail: `Page ${done}` };
    return { title: "Fetching channel history…", detail: "" };
  }
  if (phase === "saving") return { title: "Processing videos…", detail: `${done}/${total}` };
  return { title: "", detail: "" };
}

function showBackfillOverlay(title, detail) {
  const overlay = document.getElementById("backfill-overlay");
  if (overlay) overlay.hidden = false;
  setBackfillOverlayText(title, detail);
}

function hideBackfillOverlay() {
  const overlay = document.getElementById("backfill-overlay");
  if (overlay) overlay.hidden = true;
}

function setBackfillOverlayText(title, detail) {
  const titleEl = document.getElementById("backfill-overlay-title");
  const detailEl = document.getElementById("backfill-overlay-detail");
  if (titleEl) titleEl.textContent = title;
  if (detailEl) detailEl.textContent = detail || "";
}

// Polls until a just-added channel's backfill is fully done, then reloads
// once. Assumes showBackfillOverlay() is already up (callers show it right
// when the add starts, before the POST even resolves, so there's no gap
// where the screen looks idle while the RSS sync — which can itself take a
// couple of seconds — is still in flight).
async function waitForBackfillThenReload(feedId, title) {
  setBackfillOverlayText(`${title} — Fetching channel history…`, "");

  const NEVER_STARTED_GRACE_MS = 4000;
  const MAX_WAIT_MS = 10 * 60 * 1000; // safety valve so a stuck check can't trap the user forever
  const start = Date.now();
  let sawActivity = false;

  while (Date.now() - start < MAX_WAIT_MS) {
    try {
      const res = await fetch(`/feeds/${feedId}/backfill-status`);
      if (res.ok) {
        const data = await res.json();
        if (isActiveBackfillPhase(data.phase)) {
          sawActivity = true;
          const parts = backfillPhaseParts(data.phase, data.done, data.total);
          setBackfillOverlayText(`${title} — ${parts.title}`, parts.detail);
        } else if (data.phase === "done") {
          break;
        } else if (!sawActivity && Date.now() - start > NEVER_STARTED_GRACE_MS) {
          break; // no channel id to resolve, or it never got scheduled — nothing to wait for
        }
      }
    } catch (err) {
      // transient network hiccup — keep polling until MAX_WAIT_MS
    }
    await new Promise((r) => setTimeout(r, 400));
  }

  window.location.reload();
}

function bulkImportStatusMeta(status) {
  if (status === "added") return { cls: "is-added", icon: "✓" };
  if (status === "duplicate") return { cls: "is-duplicate", icon: "•" };
  return { cls: "is-error", icon: "✗" };
}

function renderBulkImportResults(results) {
  const list = document.getElementById("bulk-import-results");
  if (!list) return;

  list.innerHTML = results
    .map((r) => {
      const { cls, icon } = bulkImportStatusMeta(r.status);
      const label = r.channel_title || r.url;
      let detail = "";
      if (r.status === "duplicate") detail = " — already following";
      else if (r.status === "error" && r.error) detail = ` — ${r.error}`;
      const full = `${label}${detail}`;
      return `
        <li>
          <span class="bulk-import-status ${cls}">${icon}</span>
          <span class="bulk-import-line" title="${escapeHtml(full)}">${escapeHtml(full)}</span>
        </li>
      `;
    })
    .join("");
}

// Inline on the Manage tab now (was a modal) — Search channels and Bulk
// import sit on the page together instead of the latter hiding behind a
// button, which also means there's no "close" moment to hang a reload off
// of. Reload is instead an explicit "Refresh page" button once an import
// finishes with at least one channel actually added, so the user still gets
// to read the per-line results (including failures) before anything
// refreshes out from under them.
function setupBulkImport() {
  const startBtn = document.getElementById("bulk-import-start");
  const againBtn = document.getElementById("bulk-import-again");
  const reloadBtn = document.getElementById("bulk-import-reload");
  const input = document.getElementById("bulk-import-input");
  const formSection = document.getElementById("bulk-import-form-section");
  const progressSection = document.getElementById("bulk-import-progress-section");
  const progressText = document.getElementById("bulk-import-progress-text");
  const resultsList = document.getElementById("bulk-import-results");
  if (!startBtn || !input) return;

  const resetForm = () => {
    input.value = "";
    formSection.hidden = false;
    progressSection.hidden = true;
    resultsList.innerHTML = "";
    progressText.textContent = "";
    startBtn.disabled = false;
    startBtn.textContent = "Import";
    againBtn.hidden = true;
    reloadBtn.hidden = true;
  };

  againBtn.addEventListener("click", resetForm);
  reloadBtn.addEventListener("click", () => window.location.reload());

  startBtn.addEventListener("click", async () => {
    const urls = input.value.trim();
    if (!urls) return;

    startBtn.disabled = true;
    startBtn.textContent = "Starting…";

    let jobId;
    let total;
    try {
      const res = await fetch("/feeds/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        showToast(data.detail || "Could not start import");
        startBtn.disabled = false;
        startBtn.textContent = "Import";
        return;
      }
      ({ job_id: jobId, total } = await res.json());
    } catch (err) {
      showToast("Could not start import");
      startBtn.disabled = false;
      startBtn.textContent = "Import";
      return;
    }

    formSection.hidden = true;
    progressSection.hidden = false;
    progressText.textContent = `Resolving channels… 0/${total}`;

    // Large channels' backfills run inline, one at a time, inside the same
    // job (see _run_bulk_import) — the counter can sit still for a while on
    // a channel with a long upload history. Polling just keeps asking, same
    // as waitForBackfillThenReload does for a single add.
    while (true) {
      let data;
      try {
        const statusRes = await fetch(`/feeds/import/${jobId}/status`);
        if (!statusRes.ok) throw new Error("status fetch failed");
        data = await statusRes.json();
      } catch (err) {
        progressText.textContent = "Lost track of the import — check Followed channels.";
        break;
      }

      renderBulkImportResults(data.results);

      if (data.done >= data.total) {
        const added = data.results.filter((r) => r.status === "added").length;
        const skipped = data.total - added;
        progressText.textContent = `Done — ${added} added${skipped ? `, ${skipped} skipped` : ""}.`;
        againBtn.hidden = false;
        reloadBtn.hidden = added === 0;
        break;
      }

      // Channels resolve in parallel first (see _run_bulk_import), then get
      // created one at a time — two distinct stages, so the counter doesn't
      // sit at 0 for however long that whole parallel batch takes.
      progressText.textContent =
        data.resolved < data.total
          ? `Resolving channels… ${data.resolved}/${data.total}`
          : `Importing… ${data.done}/${data.total}`;
      await new Promise((r) => setTimeout(r, 1000));
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupTabs();
  setupChannelSearch();
  setupDownloadsOverlay();
  setupBulkImport();
  setupStorage();
  setupSettings();
  setupHomeChannels();
  setupLibrarySearch();
  setupHorizontalScrollers();
  setupRefreshButton();
  maybeAutoRefresh();
});
