// Shared vocabulary: escaping, formatting, the JSON helper, toasts, the
// confirm dialog, and the overlay open/close behaviour. Everything here is
// used from at least two other modules — anything used from only one belongs
// in that one.

// What a URL hash means: one of the four tab panels, a channel/playlist
// detail view, a player overlay request, or nothing recognized. Shared by
// home/tabs.js (plain tab switches and its popstate handler) and
// home/detail.js (detail/player routes and its own popstate handler) so the
// two agree on where the line between them is. index.html's inline
// pre-paint script runs before any module loads and can't import this, so it
// necessarily duplicates the same prefix rules in plain JS — this is the one
// place everything after that first paint agrees with it.
const VALID_TABS = ["home", "library", "explore", "settings"];
const PLAYLIST_KINDS = ["favorites", "saved", "new-uploads", "recently-played"];

export function classifyHash(hash) {
  const [path, query] = hash.split("?");
  const page = query ? Number(query.replace("page=", "")) || 1 : 1;

  if (path.startsWith("channel/")) {
    return { type: "detail", kind: "channel", id: path.slice("channel/".length), page };
  }
  if (PLAYLIST_KINDS.includes(path)) {
    return { type: "detail", kind: path, id: null, page };
  }
  if (path.startsWith("player/")) {
    return { type: "player", id: path.slice("player/".length) };
  }
  if (VALID_TABS.includes(path)) {
    return { type: "tab", tab: path };
  }
  return { type: "unknown" };
}

export function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// Mirrors format_duration() in app/formatting.py (M:SS, or H:MM:SS past an
// hour) — used by both the player's elapsed/duration display and the
// content cards' duration badge.
export function formatDuration(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function debounce(fn, delay) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

/**
 * One JSON request against our own API.
 *
 * Every call site used to write its own try/catch, `res.ok` check, JSON
 * parse and error toast — twenty-five near-identical copies, each free to
 * forget one of the four. Returns a result object rather than throwing or
 * returning bare data because callers genuinely need different parts of it:
 * most want `data`, addChannel needs `status` (409 means "already
 * following", which isn't an error worth toasting), and the pollers just
 * want `ok`.
 *
 * A network failure comes back as { ok: false, status: 0 } rather than
 * raising, so "the server said no" and "the request never arrived" are
 * handled the same way — which is what every call site wants, since neither
 * one leaves the UI with anything to show.
 *
 * `errorMessage` toasts on any failure. Omit it for polls and background
 * refreshes, where a single blip is expected and silence is correct.
 */
export async function api(url, { method = "GET", body, errorMessage } = {}) {
  let res;
  try {
    res = await fetch(url, {
      method,
      ...(body !== undefined && {
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    });
  } catch (err) {
    if (errorMessage) showToast(errorMessage);
    return { ok: false, status: 0, data: null };
  }

  // 204 (profile/feed deletes) has no body at all, and an error response may
  // carry HTML rather than JSON — neither should turn into a thrown parse
  // error on top of whatever already went wrong.
  let data = null;
  if (res.status !== 204) {
    data = await res.json().catch(() => null);
  }

  if (!res.ok && errorMessage) {
    showToast(data?.detail || errorMessage);
  }
  return { ok: res.ok, status: res.status, data };
}

export function showToast(message) {
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

export function confirmDialog(message, confirmLabel) {
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

// One Escape handler for every overlay, rather than one per overlay. Four
// separate document-level keydown listeners used to be registered (downloads,
// bulk import, switch profile, manage profiles), each re-implementing the
// same "if I'm open, close me" check.
const openableOverlays = [];
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const overlay of openableOverlays) {
    if (!overlay.hidden) overlay.hidden = true;
  }
});

/**
 * Wire an overlay: one or more triggers open it; the close button, a
 * backdrop click, or Escape close it. Returns { open, close } so a caller
 * can drive it from elsewhere too (the mobile menu opens the profile
 * switcher, for instance) or layer extra behaviour on closing.
 */
export function setupOverlay(overlayId, closeBtnId, triggerIds = []) {
  const overlay = document.getElementById(overlayId);
  if (!overlay) return null;

  const open = () => {
    overlay.hidden = false;
  };
  const close = () => {
    overlay.hidden = true;
  };

  for (const triggerId of triggerIds) {
    document.getElementById(triggerId)?.addEventListener("click", open);
  }
  document.getElementById(closeBtnId)?.addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  openableOverlays.push(overlay);

  return { open, close };
}
