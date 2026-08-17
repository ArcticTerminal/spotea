"""Detail-panel context for content that isn't in the library yet — a
recommended YouTube playlist, or a channel nobody here follows.

These render through the *same* `_detail_panel.html` a followed channel and a
pinned playlist do (see page_context.py for those two). That's the whole
point: Explore doesn't get a second, parallel "list of tracks" page. What
differs is only where the rows come from (a live yt-dlp read, not the
database) and therefore what a row can offer — no save toggle, no
downloaded badge, no per-page pagination, because none of that exists for a
video with no `Content` row.

The rows are still playable, and "Play all" still works: the client
materializes the list into preview `Content` rows in one batch when playback
actually starts (POST /feeds/videos/batch), which costs no YouTube requests
at all because every field those rows need is already in the response below.

Kept out of page_context.py deliberately — everything there is DB queries,
and these two builders make network calls that take seconds.
"""

from sqlalchemy.orm import Session

from app.images import cached_avatar_path
from app.models import Feed
from app.youtube.search import fetch_channel_uploads, fetch_playlist
from app.youtube.urls import CHANNEL_PAGE_URL_TEMPLATE, channel_feed_url


def _base_context(kind: str, remote_id: str, title: str, items: list) -> dict:
    return {
        "kind": kind,
        "remote": True,
        "feed": None,
        "title": title,
        "content": items,
        "empty_message": "Nothing playable here.",
        # Explore is where every route into one of these starts, so that's
        # where "Back" means, not Library.
        "back_label": "Explore",
        # One page, always: the fetch is capped (see PLAYLIST_ITEM_LIMIT) and
        # there's no cheap way to ask YouTube for "page 3" of a flat read, so
        # the panel shows what it got and _pagination.html renders nothing.
        "page": 1,
        "total_pages": 1,
        "start_index": 1,
        "base_url": f"/#{kind}/{remote_id}",
    }


def remote_playlist_context(playlist_id: str) -> dict | None:
    """A YouTube playlist's tracks. Returns None when yt-dlp couldn't read it
    at all (deleted, private, or a failed request — search.fetch_playlist
    flattens all three into an empty result), so the caller can 404."""
    playlist = fetch_playlist(playlist_id)
    if not playlist.items:
        return None

    total = playlist.video_count or len(playlist.items)
    context = _base_context(
        "yt-playlist", playlist_id, playlist.title or "Playlist", playlist.items
    )
    context.update(
        {
            "video_count": total,
            # Says so explicitly when the fetch was capped, rather than
            # implying these are all of them.
            "count_label": (
                f"First {len(playlist.items)} of {total} tracks"
                if total > len(playlist.items)
                else f"{len(playlist.items)} track{'' if len(playlist.items) == 1 else 's'}"
            ),
            "hero_image": playlist.items[0].thumbnail_url,
        }
    )
    return context


def remote_channel_context(db: Session, user_id: int, channel_id: str) -> dict | None:
    """A channel's latest uploads, for deciding whether to follow it.

    Also reports whether this profile *already* follows the channel — the
    hero's action is "Follow" if not, and a way back to the real (local)
    channel page if so, since the library copy is the more useful of the two.
    """
    uploads = fetch_channel_uploads(channel_id)
    if not uploads.items:
        return None

    followed = (
        db.query(Feed)
        .filter(
            Feed.user_id == user_id,
            Feed.rss_url == channel_feed_url(channel_id),
            Feed.followed.is_(True),
        )
        .first()
    )

    context = _base_context(
        "yt-channel", channel_id, uploads.title or "Channel", uploads.items
    )
    context.update(
        {
            "video_count": len(uploads.items),
            "count_label": f"Latest {len(uploads.items)} uploads",
            # Only a channel already followed has a local avatar to reuse
            # (see youtube/search.py's _cached_avatar_or_hotlink) — this
            # route has no remote thumbnail_url to hotlink as a fallback the
            # way a search result does, so a channel that's never been
            # followed just renders with no avatar at all.
            "hero_image": cached_avatar_path(channel_id),
            "hero_is_avatar": True,
            "channel_url": CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=channel_id),
            "followed_feed_id": followed.id if followed else None,
        }
    )
    return context
