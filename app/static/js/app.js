const POLL_INTERVAL_MS = 1500;

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function cardActionHtml(contentId, status, errorMessage) {
  if (status === "ready") {
    return `
      <a class="btn-play" href="/player/${contentId}">▶ Play</a>
      <button type="button" class="btn-delete" data-content-id="${contentId}" aria-label="Delete download">🗑</button>
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
    }
  });

  grid.querySelectorAll('.card[data-status="downloading"]').forEach((card) => {
    pollStatus(card.dataset.contentId);
  });
}

async function refreshFeeds() {
  const statusEl = document.getElementById("refresh-status");
  if (statusEl) statusEl.textContent = "Refreshing…";

  try {
    const res = await fetch("/feeds/refresh", { method: "POST" });
    if (!res.ok) throw new Error("refresh failed");
    const data = await res.json();
    if (statusEl) statusEl.textContent = "";
    if (data.new_content_count > 0) {
      window.location.reload();
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = "Refresh failed";
  }
}

function setupFeedForm() {
  const form = document.getElementById("feed-form");
  const errorEl = document.getElementById("feed-error");
  if (!form) return;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorEl.hidden = true;

    const rssUrl = document.getElementById("feed-url").value.trim();
    const submitBtn = form.querySelector("button[type=submit]");
    submitBtn.disabled = true;

    try {
      const res = await fetch("/feeds", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rss_url: rssUrl }),
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
  setupFeedForm();
  setupUnfollowButtons();
  setupContentGrid();
  refreshFeeds();
});
