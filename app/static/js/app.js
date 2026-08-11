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

// Same problem as syncSavedShelf above, for a different reason: this shelf
// used to always end up fresh "for free" because playing anything meant
// navigating to /player/{id} and back, which re-rendered the whole page.
// Now that Home/Library/Explore play through the in-page overlay (see
// openPlayer) instead of navigating away at all, nothing re-runs the server
// route afterward — a replay has to patch this shelf in live or it just
// never updates for the rest of the session. Only handles moving a card
// that's rendered *somewhere* on Home to the front of this shelf (cloning,
// like syncSavedShelf) — a track that's never appeared as a card here (e.g.
// played once via Explore search and never shown on Home) is left for the
// next real page load to pick up, rather than hand-building the card's
// markup a second time in JS just for that edge case.
function syncRecentlyPlayedShelf(contentId) {
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

// Feeds are also kept fresh by a server-side background job on a schedule
// set in Settings (see routers/settings.py) — this is just for "I want it
// now". The overlay (rather than just the button's own spin state) is the
// feedback here because refresh-feeds-btn itself is hidden under the
// mobile-menu breakpoint (see style.css); the overlay covers that entry
// point too.
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

function setupRefreshButton() {
  const btn = document.getElementById("refresh-feeds-btn");
  if (!btn) return;
  btn.addEventListener("click", () => refreshFeeds());
}

// Below the mobile-menu-btn breakpoint (see style.css), the profile/refresh/
// logout row collapses into this single hamburger dropdown instead — same
// underlying actions, just consolidated so the topbar doesn't have to fit
// three separate controls (and any more added later) on one narrow line.
function setupMobileMenu() {
  const btn = document.getElementById("mobile-menu-btn");
  const menu = document.getElementById("mobile-menu");
  if (!btn || !menu) return;

  const setOpen = (open) => {
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };

  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    setOpen(menu.hidden);
  });

  document.addEventListener("click", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && event.target !== btn) setOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) setOpen(false);
  });

  document.getElementById("mobile-menu-refresh")?.addEventListener("click", () => {
    setOpen(false);
    refreshFeeds();
  });

  document.getElementById("mobile-menu-profile")?.addEventListener("click", () => {
    setOpen(false);
    const overlay = document.getElementById("profiles-overlay");
    if (overlay) overlay.hidden = false;
  });
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

// Explore's search box covers both songs and channels at once — search-videos
// and search-channels are still two separate endpoints/result lists under the
// hood (see renderVideoSearchResults/renderSearchResults below), but one input
// drives both in parallel instead of showing two permanently-visible boxes.
function setupExploreSearch() {
  const input = document.getElementById("explore-search-input");
  const resultsPanel = document.getElementById("explore-results-panel");
  const browsePanel = document.getElementById("explore-browse-panel");
  const videoResults = document.getElementById("video-search-results");
  const channelResults = document.getElementById("channel-search-results");
  if (!input || !resultsPanel || !browsePanel) return;

  const runSearch = debounce(async (query) => {
    if (!query) {
      resultsPanel.hidden = true;
      browsePanel.hidden = false;
      videoResults.innerHTML = "";
      channelResults.innerHTML = "";
      return;
    }

    resultsPanel.hidden = false;
    browsePanel.hidden = true;
    // Shown immediately, before either fetch resolves — without this, the
    // Songs/Channels headings pop into view over empty lists the instant the
    // debounce fires, which reads as broken results rather than a pending
    // search. renderVideoSearchResults/renderSearchResults overwrite this
    // per-section as each fetch settles (video/channel search run in
    // parallel and don't necessarily resolve together).
    const loadingHtml = `<li class="search-loading"><span class="spinner"></span>Searching…</li>`;
    videoResults.innerHTML = loadingHtml;
    channelResults.innerHTML = loadingHtml;

    const [videoRes, channelRes] = await Promise.allSettled([
      fetch(`/feeds/search-videos?q=${encodeURIComponent(query)}`),
      fetch(`/feeds/search?q=${encodeURIComponent(query)}`),
    ]);

    if (videoRes.status === "fulfilled" && videoRes.value.ok) {
      renderVideoSearchResults(await videoRes.value.json());
    }
    if (channelRes.status === "fulfilled" && channelRes.value.ok) {
      renderSearchResults(await channelRes.value.json());
    }
  }, 400);

  input.addEventListener("input", () => runSearch(input.value.trim()));

  videoResults.addEventListener("click", (event) => {
    const button = event.target.closest(".video-search-play");
    if (!button) return;
    const row = button.closest(".video-search-result");
    playSearchedVideo(row.dataset, button);
  });

  channelResults.addEventListener("click", (event) => {
    const btn = event.target.closest(".btn-add-channel");
    if (!btn) return;
    addChannel(btn.dataset.channelUrl, btn);
  });
}

function renderVideoSearchResults(results) {
  const list = document.getElementById("video-search-results");
  if (!list) return;

  if (!results.length) {
    list.innerHTML = `<li class="search-empty">No songs found</li>`;
    return;
  }

  list.innerHTML = results
    .map((r) => {
      const thumb = r.thumbnail_url
        ? `<img class="video-search-thumb" src="${escapeHtml(r.thumbnail_url)}" alt="" />`
        : `<span class="video-search-thumb"></span>`;
      const duration = r.duration_seconds != null ? formatDuration(r.duration_seconds) : "";
      const channel = r.channel_title ? escapeHtml(r.channel_title) : "";
      const meta = [channel, duration].filter(Boolean).join(" · ");
      return `
        <li
          class="search-result video-search-result"
          data-video-id="${escapeHtml(r.video_id)}"
          data-title="${escapeHtml(r.title)}"
          data-thumbnail-url="${escapeHtml(r.thumbnail_url || "")}"
          data-duration-seconds="${r.duration_seconds ?? ""}"
          data-channel-title="${escapeHtml(r.channel_title || "")}"
        >
          ${thumb}
          <div class="search-result-info">
            <span class="search-result-title">${escapeHtml(r.title)}</span>
            <span class="search-result-subs">${meta}</span>
          </div>
          <button type="button" class="btn-icon video-search-play" aria-label="Play">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"></path></svg>
          </button>
        </li>
      `;
    })
    .join("");
}

// Explore's "listen" action: adds the video (always as an unkept preview —
// see routers/feeds.py's add_single_video) and jumps straight to its player,
// same as tapping any other card. If this video already has a Content row
// (an earlier Explore preview, or an upload from a followed channel),
// add_single_video hands back that row's id instead of erroring, so this
// just replays whatever was already downloaded. No backfill-overlay wait
// here — unlike addChannel, this is a single insert, not a channel sync, so
// it should feel instant.
async function playSearchedVideo(dataset, button) {
  if (button) button.disabled = true;

  try {
    const res = await fetch("/feeds/videos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        video_id: dataset.videoId,
        title: dataset.title,
        thumbnail_url: dataset.thumbnailUrl || null,
        duration_seconds: dataset.durationSeconds ? Number(dataset.durationSeconds) : null,
        channel_title: dataset.channelTitle || null,
      }),
    });

    if (res.ok) {
      const data = await res.json();
      openPlayer(data.content_id);
      return;
    }

    const data = await res.json().catch(() => ({}));
    showToast(data.detail || "Could not add this song");
  } catch (err) {
    showToast("Could not add this song");
  } finally {
    if (button) button.disabled = false;
  }
}

// Home/Library/Explore's in-page player — see app/templates/_player_overlay.html.
// player.js's setupPlayer/prepareAudio/setupMediaSession/setupFavorite already
// run against this markup unmodified (same element ids as the standalone
// /player/{id} page); everything here is the glue that's specific to reusing
// that DOM across multiple tracks in one page load instead of once per load.

function expandPlayer() {
  document.getElementById("player-overlay").hidden = false;
}

function collapsePlayer() {
  document.getElementById("player-overlay").hidden = true;
}

function syncMiniPlayerInfo(data) {
  document.getElementById("mini-player-title").textContent = data.title;
  document.getElementById("mini-player-channel").textContent = data.channel_title || "";
  const img = document.getElementById("mini-player-art-img");
  if (data.thumbnail_url) {
    img.src = data.thumbnail_url;
    img.hidden = false;
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

async function openPlayer(contentId) {
  contentId = String(contentId);
  const root = document.getElementById("player-root");
  const audio = document.getElementById("audio");

  if (root.dataset.contentId === contentId) {
    // Same track already loaded — just surface it, don't touch playback.
    expandPlayer();
    return;
  }

  // Switching tracks (or starting fresh): stop whatever's currently loaded
  // immediately, rather than leaving it playing underneath the new track's
  // own "Downloading audio…" state until that one's ready.
  audio.pause();
  const seekBar = document.getElementById("seek-bar");
  seekBar.value = 0;
  document.getElementById("current-time").textContent = "0:00";
  paintRange(seekBar);
  document.getElementById("mini-player-progress-fill").style.width = "0%";

  let data;
  try {
    const res = await fetch(`/content/${contentId}`);
    if (!res.ok) throw new Error("not found");
    data = await res.json();
  } catch (err) {
    showToast("Could not load this track");
    return;
  }

  document.querySelector(".player-title").textContent = data.title;
  document.querySelector(".player-channel").textContent = data.channel_title || "";
  const artImg = document.getElementById("player-art-img");
  if (data.thumbnail_url) {
    artImg.src = data.thumbnail_url;
    artImg.hidden = false;
  } else {
    artImg.removeAttribute("src");
    artImg.hidden = true;
  }
  document.getElementById("duration-time").textContent = data.duration_seconds
    ? formatDuration(data.duration_seconds)
    : "0:00";

  const favBtn = document.getElementById("favorite-btn");
  favBtn.dataset.contentId = data.id;
  favBtn.dataset.favorite = String(data.is_favorite);
  favBtn.classList.toggle("is-on", data.is_favorite);
  favBtn.setAttribute("aria-pressed", String(data.is_favorite));
  favBtn.querySelector("svg").setAttribute("fill", data.is_favorite ? "currentColor" : "none");

  root.dataset.contentId = String(data.id);
  root.dataset.status = data.status;
  root.dataset.stream = `/content/${data.id}/stream`;

  syncMiniPlayerInfo(data);

  // setupMediaSession (player.js) only reads the DOM once, at page-load
  // time — on index.html that's before any track has ever been opened, so
  // it can't be what keeps lock-screen/notification metadata current across
  // repeated openPlayer() calls. This has to do it explicitly, every time.
  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: data.title,
      artist: data.channel_title || "",
      artwork: data.thumbnail_url ? [{ src: data.thumbnail_url }] : [],
    });
  }

  document.getElementById("player-overlay").hidden = false;
  document.getElementById("mini-player").hidden = false;
  document.body.classList.add("has-mini-player");

  prepareAudio(audio, () => syncRecentlyPlayedShelf(contentId));
}

function closePlayer() {
  const audio = document.getElementById("audio");
  audio.pause();
  audio.removeAttribute("src");
  audio.load();

  const root = document.getElementById("player-root");
  root.dataset.contentId = "";
  root.dataset.status = "";
  root.dataset.stream = "";

  document.getElementById("player-overlay").hidden = true;
  document.getElementById("mini-player").hidden = true;
  document.body.classList.remove("has-mini-player");

  if ("mediaSession" in navigator) {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  }
}

function setupPlayerOverlay() {
  const overlay = document.getElementById("player-overlay");
  if (!overlay) return; // channel.html/content_list.html don't include it

  const audio = document.getElementById("audio");
  const miniPlayBtn = document.getElementById("mini-player-playpause");
  const miniIconPlay = document.getElementById("mini-icon-play");
  const miniIconPause = document.getElementById("mini-icon-pause");

  const syncMiniIcon = () => {
    miniIconPlay.toggleAttribute("hidden", !audio.paused);
    miniIconPause.toggleAttribute("hidden", audio.paused);
    miniPlayBtn.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
  };

  // Thin passive progress line along the mini-bar's top edge (Spotify/Apple
  // Music/YouTube Music all have one) — not interactive, just a glance-able
  // sense of how far into the track you are without expanding the overlay.
  const miniProgressFill = document.getElementById("mini-player-progress-fill");
  const syncMiniProgress = () => {
    const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
    miniProgressFill.style.width = `${pct}%`;
  };

  miniPlayBtn.addEventListener("click", () => {
    if (audio.paused) audio.play().catch(() => {});
    else audio.pause();
  });
  audio.addEventListener("play", syncMiniIcon);
  audio.addEventListener("pause", syncMiniIcon);
  audio.addEventListener("ended", syncMiniIcon);
  audio.addEventListener("timeupdate", syncMiniProgress);
  audio.addEventListener("loadedmetadata", syncMiniProgress);

  document.getElementById("mini-player-expand").addEventListener("click", expandPlayer);
  document.getElementById("overlay-collapse-btn").addEventListener("click", (event) => {
    event.preventDefault();
    collapsePlayer();
  });
  document.getElementById("mini-player-close").addEventListener("click", closePlayer);

  // Home's shelves only — Library has no .card elements at all (it's a grid
  // of channel tiles linking to /channel/{id}, a real page, out of scope),
  // and Explore's results route through playSearchedVideo instead.
  const homeTab = document.getElementById("tab-home");
  if (!homeTab) return;

  homeTab.addEventListener("click", (event) => {
    // Let ctrl/cmd/shift-click and middle-click behave natively (open the
    // standalone /player/{id} page in a new tab) instead of hijacking them.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.button !== 0) return;
    if (event.target.closest(".btn-save")) return; // toggleSaved's own handler (ui.js) owns this

    const link = event.target.closest("a");
    if (!link) return;
    const card = event.target.closest(".card");
    if (!card) return;

    event.preventDefault();
    openPlayer(card.dataset.contentId);
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

function setupBulkImportOverlay() {
  const overlay = document.getElementById("bulk-import-overlay");
  const openBtn = document.getElementById("open-bulk-import");
  const closeBtn = document.getElementById("bulk-import-close");
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

async function putSetting(body, errorMessage) {
  try {
    const res = await fetch("/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error("update failed");
  } catch (err) {
    showToast(errorMessage);
  }
}

function setupSettings() {
  const qualitySelect = document.getElementById("audio-quality-select");
  qualitySelect?.addEventListener("change", () => {
    putSetting({ audio_quality: qualitySelect.value }, "Could not update audio quality");
  });

  const refreshIntervalSelect = document.getElementById("refresh-interval-select");
  refreshIntervalSelect?.addEventListener("change", () => {
    putSetting(
      { feed_refresh_interval_minutes: Number(refreshIntervalSelect.value) },
      "Could not update refresh interval"
    );
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

  window.location.href = `/channel/${feedId}`;
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

// Lives in the #bulk-import-overlay modal (see setupBulkImportOverlay above).
// Reload is an explicit "Refresh page" button, not something that fires on
// modal close — closing the modal (× / backdrop / Escape) is also how you'd
// dismiss it after a successful import, so tying a reload to that would fire
// it unexpectedly. This way the user reads the per-line results (including
// any failures) before anything refreshes out from under them.
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
  setupPlayerOverlay();
  setupExploreSearch();
  setupDownloadsOverlay();
  setupBulkImportOverlay();
  setupBulkImport();
  setupStorage();
  setupSettings();
  setupHomeChannels();
  setupLibrarySearch();
  setupHorizontalScrollers();
  setupRefreshButton();
  setupMobileMenu();
});
