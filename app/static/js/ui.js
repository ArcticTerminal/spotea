// Shared confirm dialog + toast + duration formatting. Loaded before the
// page-specific script on every page, so these are always available.

// Every page here is rendered once, server-side, with nothing that refreshes
// its dynamic content (Recently Played, storage usage, a channel's track
// list, ...) afterward — a normal navigation is fine since it always re-runs
// the server route, but the browser's back/forward cache can restore a
// page's exact pre-navigation DOM without contacting the server at all
// (e.g. hitting the player's back button, or any native back gesture, after
// having played/downloaded something). That silently shows stale data with
// no error or signal that anything's wrong. Forcing one real reload whenever
// a page comes back this way is simpler and more robust than trying to track
// every specific action that could have changed something while away.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

// Mirrors format_duration() in app/formatting.py (M:SS, or H:MM:SS past an
// hour) — used by both the player's elapsed/duration display and the
// content cards' duration badge.
function formatDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const CONFIRM_MARKUP = `
  <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modal-message">
    <p id="modal-message"></p>
    <div class="modal-actions">
      <button type="button" id="modal-cancel" class="btn-quiet">Cancel</button>
      <button type="button" id="modal-confirm" class="btn-danger">Confirm</button>
    </div>
  </div>
`;

function ensureModal() {
  let overlay = document.getElementById("modal-overlay");
  if (overlay) return overlay;

  overlay = document.createElement("div");
  overlay.id = "modal-overlay";
  overlay.className = "modal-overlay";
  overlay.hidden = true;
  overlay.innerHTML = CONFIRM_MARKUP;
  document.body.appendChild(overlay);
  return overlay;
}

function confirmDialog(message, confirmLabel) {
  const overlay = ensureModal();
  const confirmBtn = overlay.querySelector("#modal-confirm");
  const cancelBtn = overlay.querySelector("#modal-cancel");

  overlay.querySelector("#modal-message").textContent = message;
  confirmBtn.textContent = confirmLabel || "Confirm";
  overlay.hidden = false;
  // Focus the safe choice: these dialogs guard destructive actions, so a
  // reflexive Enter should cancel rather than confirm.
  cancelBtn.focus();

  return new Promise((resolve) => {
    function cleanup(result) {
      overlay.hidden = true;
      confirmBtn.removeEventListener("click", onConfirm);
      cancelBtn.removeEventListener("click", onCancel);
      overlay.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    const onConfirm = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onBackdrop = (event) => {
      if (event.target === overlay) cleanup(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") cleanup(false);
    };

    confirmBtn.addEventListener("click", onConfirm);
    cancelBtn.addEventListener("click", onCancel);
    overlay.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKey);
  });
}

function applySavedState(button, isSaved) {
  button.classList.toggle("is-on", isSaved);
  button.setAttribute("aria-pressed", String(isSaved));
  button.title = isSaved ? "Saved for later" : "Save for later";
  const svg = button.querySelector("svg");
  if (svg) svg.setAttribute("fill", isSaved ? "currentColor" : "none");
}

async function toggleSaved(contentId, button) {
  // The same item can appear in several shelves on Home plus a channel
  // page's track list at once, so every instance's state has to be kept in
  // sync, not just the one the click happened in. Matched by attribute
  // rather than a specific class (.card vs .track-row) since both item
  // containers carry data-content-id/data-saved.
  const cards = document.querySelectorAll(`[data-content-id="${contentId}"][data-saved]`);
  const isSaved = button?.closest("[data-saved]")?.dataset.saved === "true";

  try {
    const res = await fetch(`/content/${contentId}/save`, {
      method: isSaved ? "DELETE" : "POST",
    });
    if (!res.ok) {
      showToast("Could not update saved items");
      return;
    }
    const data = await res.json();

    cards.forEach((card) => {
      card.dataset.saved = String(data.is_saved);
      const saveBtn = card.querySelector(".btn-save");
      if (saveBtn) applySavedState(saveBtn, data.is_saved);
    });

    // Only defined on index.html (Home's "Saved for later" shelf) — the
    // channel page has no such shelf to patch.
    if (typeof syncSavedShelf === "function") syncSavedShelf(contentId, data.is_saved);
  } catch (err) {
    showToast("Could not update saved items");
  }
}

// Confirm + DELETE + error toast for unfollowing a channel. What happens
// after success is left to the caller via onConfirmed (fires right after the
// user confirms, before the request — e.g. to show a covering overlay) and
// the boolean return value, so a second caller can reload in place instead of
// navigating away like the channel page's own Unfollow button does.
async function unfollowChannel(feedId, onConfirmed) {
  const confirmed = await confirmDialog(
    "Unfollow this channel? Its videos will be removed from your library.",
    "Unfollow"
  );
  if (!confirmed) return false;

  if (onConfirmed) onConfirmed();

  try {
    const res = await fetch(`/feeds/${feedId}`, { method: "DELETE" });
    if (res.ok) return true;
    showToast("Could not unfollow this channel");
    return false;
  } catch (err) {
    showToast("Could not unfollow this channel");
    return false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.addEventListener("click", (event) => {
    const saveBtn = event.target.closest(".btn-save");
    if (saveBtn) toggleSaved(saveBtn.dataset.contentId, saveBtn);
  });
});

// Makes the app installable ("Add to Home Screen" / desktop install prompt)
// — see /sw.js and static/manifest.json. Registering from every page (this
// file loads on all of them) rather than just index.html means installing
// works no matter which page happens to be open when the browser offers it.
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Not fatal — the app works the same without it, just not installable.
    });
  });
}

function showToast(message) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = "toast";
  toast.setAttribute("role", "status");
  toast.textContent = message;
  container.appendChild(toast);

  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  setTimeout(() => {
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}
