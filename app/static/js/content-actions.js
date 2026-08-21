// Actions on something in the library that aren't tied to one page's own
// script. Loaded on every page that shows content, which is why it lives at
// the top level rather than under home/.
//
// Save-for-later used to live here too and was the reason for the "content
// actions" name; it's gone, along with its column and its API. What's left
// is the unfollow confirmation, shared by Library and the artist page.

import { api, confirmDialog } from "./core.js";

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
