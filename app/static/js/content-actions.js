// Actions available on a piece of content wherever it's rendered — a Home
// shelf card, a channel page's track row, a virtual-playlist row. Loaded on
// every page that shows content, which is why it lives at the top level
// rather than under home/.

import { api, confirmDialog } from "./core.js";
import { refreshFragments } from "./fragments.js";

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

  const { ok, data } = await api(`/content/${contentId}/save`, {
    method: isSaved ? "DELETE" : "POST",
    errorMessage: "Could not update saved items",
  });
  if (!ok) return;

  // Applied immediately so the button the user pressed responds at once;
  // the shelves and counts follow from the server's own render below.
  cards.forEach((card) => {
    card.dataset.saved = String(data.is_saved);
    const saveBtn = card.querySelector(".btn-save");
    if (saveBtn) applySavedState(saveBtn, data.is_saved);
  });

  refreshFragments();
}

/**
 * Confirm, then unfollow. What happens afterward is left to the caller via
 * the boolean return — the channel page navigates away, Library reloads in
 * place — and `onConfirmed` fires right after the user confirms but before
 * the request, so a caller can put up a covering overlay first.
 */
export async function unfollowArtist(artistId, onConfirmed) {
  const confirmed = await confirmDialog(
    "Unfollow this artist? Their songs will be removed from your library.",
    "Unfollow"
  );
  if (!confirmed) return false;

  if (onConfirmed) onConfirmed();

  const { ok } = await api(`/artists/${artistId}`, {
    method: "DELETE",
    errorMessage: "Could not unfollow this channel",
  });
  return ok;
}

/** Delegated so it covers cards added to the DOM later (cloned shelf cards). */
export function setupSaveButtons() {
  document.addEventListener("click", (event) => {
    const saveBtn = event.target.closest(".btn-save");
    if (saveBtn) toggleSaved(saveBtn.dataset.contentId, saveBtn);
  });
}
