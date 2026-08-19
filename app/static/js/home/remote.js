// Acting on music the library doesn't have yet: an Explore search result, a
// recommended song, a track of a YouTube Music playlist, or a whole artist.
//
// Split out of home/explore.js because the detail panel needs the same three
// actions (its remote rows are Explore results rendered somewhere else), and
// this module can be imported by both without a cycle: it never imports
// home/detail.js. Where it would need to — opening the artist's page once a
// follow finishes — it fires ARTIST_FOLLOWED and lets home/detail.js react,
// the same one-way arrangement home/queue.js uses for QUEUE_CHANGED.

export const ARTIST_FOLLOWED = "spotea:artist-followed";

import { api, showToast } from "../core.js";
import { refreshFragments } from "../fragments.js";
import { openPlayer } from "./overlay.js";
import { queueSource, setQueue } from "./queue.js";

/**
 * Follows an artist and reports back as { added, status }: `added` is whether
 * they are now followed — true for a fresh follow, and also for a 409, which
 * means some earlier action already added them.
 *
 * Nothing is waited on. POST /artists answers as soon as the row exists, and
 * the first sync behind it is a background task the Library card reports on
 * itself (see page_context.library_context and home/library.js). The
 * full-screen overlay this used to put up existed for a history scan that
 * could run for minutes; there is no such scan any more.
 */
export async function followArtist(channelUrl, button, { announce = true } = {}) {
  const originalLabel = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "Adding…";
  }

  const { ok, status, data } = await api("/artists", {
    method: "POST",
    // Only the channel: which artist it is, and whether it is one at all, is
    // worked out server-side (see services/artist_follow.py).
    body: { channel_url: channelUrl },
  });

  if (ok) {
    if (data?.artist?.id == null) {
      window.location.reload();
      return { added: false, status };
    }
    if (button) button.textContent = "Added";
    // The grid needs re-rendering to pick up the new card and its
    // "still fetching" state.
    refreshFragments();
    if (announce) {
      document.dispatchEvent(
        new CustomEvent(ARTIST_FOLLOWED, {
          detail: {
            artistId: data.artist.id,
            title: data.artist.name || channelUrl,
            browseId: data.artist.browse_id || null,
          },
        })
      );
    }
    return { added: true, status };
  }

  if (status === 409) {
    if (button) button.textContent = "Already added";
    return { added: true, status };
  }
  if (button) {
    button.disabled = false;
    button.textContent = originalLabel;
  }
  if (status !== 0) showToast(data?.detail || "Could not follow this artist");
  return { added: false, status };
}
