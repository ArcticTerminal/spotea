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
from app.youtube.music import fetch_artist
from app.youtube.search import (
    cached_avatar_or_hotlink,
    fetch_channel_avatar,
    fetch_channel_uploads,
    fetch_playlist,
    proxied_avatar_url,
)
from app.youtube.urls import CHANNEL_PAGE_URL_TEMPLATE, channel_feed_url

# What a caller-supplied avatar_url (see remote_channel_context) is allowed
# to be: one of this app's own same-origin image routes, never an arbitrary
# URL — the client only ever has one of these two to send in the first place
# (see youtube/search.py's _cached_avatar_or_hotlink), so anything else means
# a tampered query string, not a real avatar.
_TRUSTED_AVATAR_URL_PREFIXES = ("/avatar-proxy?u=", "/avatars/")


def _trusted_avatar_url(avatar_url: str | None) -> str | None:
    if avatar_url and avatar_url.startswith(_TRUSTED_AVATAR_URL_PREFIXES):
        return avatar_url
    return None


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


def _followed_feed_id(db: Session, user_id: int, channel_id: str) -> int | None:
    followed = (
        db.query(Feed)
        .filter(
            Feed.user_id == user_id,
            Feed.rss_url == channel_feed_url(channel_id),
            Feed.followed.is_(True),
        )
        .first()
    )
    return followed.id if followed else None


def remote_artist_context(
    db: Session, user_id: int, browse_id: str, avatar_url: str | None = None
) -> dict | None:
    """An artist's page as YouTube Music knows it — their tracks, and a
    Follow that lands on the right channel.

    That last part is the reason this exists as its own kind rather than
    reusing remote_channel_context. Every song YouTube Music returns is
    attributed to the artist's auto-generated "<Artist> - Topic" channel,
    and the charts name artists the same way; following one of those gets
    you an automated container, not the channel the artist actually uploads
    to. The artist page is the only place that reports both ids, so this is
    where the swap can be made — the panel plays the Topic channel's tracks
    and follows the official channel, which is what someone pressing that
    button means.

    **This is what a channel result opens too**, not just an artist card,
    which is why the fallback below matters as much as the happy path. An
    artist whose YouTube channel is mostly vlogs — Shirin David is the case
    that prompted this — has a channel listing where the music is buried,
    and the same id opens a clean track list here (YouTube Music accepts the
    official channel id as a browse id; see fetch_artist). So the id is
    tried as an artist first, and anything that isn't one — a podcast, a
    tech channel, an artist page with nothing playable on it — falls through
    to the plain channel listing it would have shown anyway. The cost of
    that fallback is one extra YouTube Music request on a click the user
    made deliberately, and it never touches youtube.com.
    """
    artist = fetch_artist(browse_id)
    if artist is None or not artist.tracks:
        return remote_channel_context(db, user_id, browse_id, avatar_url=avatar_url)

    # The official channel where there is one. Where there isn't — a handful
    # of artists exist on YouTube Music with no channel behind them — the
    # browse id is still a real channel with a working RSS feed, so following
    # it is a worse answer than the right one but a better answer than a
    # button that does nothing.
    follow_channel_id = artist.channel_id or browse_id

    context = _base_context("yt-artist", browse_id, artist.name, artist.tracks)
    context.update(
        {
            "video_count": len(artist.tracks),
            "count_label": (
                f"{len(artist.tracks)} tracks · {artist.monthly_listeners} monthly listeners"
                if artist.monthly_listeners
                else f"{len(artist.tracks)} tracks"
            ),
            # Routed through /avatar-proxy rather than hotlinked: artist
            # portraits come off lh3.googleusercontent.com, which the
            # yt3.ggpht.com rewrite doesn't cover (see urls.py).
            "hero_image": cached_avatar_or_hotlink(follow_channel_id, artist.avatar_url),
            "hero_is_avatar": True,
            "channel_url": CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=follow_channel_id),
            "followed_feed_id": _followed_feed_id(db, user_id, follow_channel_id),
        }
    )
    return context


def remote_channel_context(
    db: Session, user_id: int, channel_id: str, avatar_url: str | None = None
) -> dict | None:
    """A channel's latest uploads, for deciding whether to follow it.

    Also reports whether this profile *already* follows the channel — the
    hero's action is "Follow" if not, and a way back to the real (local)
    channel page if so, since the library copy is the more useful of the two.

    `avatar_url` is whatever the client already had rendered on the card it
    clicked through from (see routers/partials.py's remote_channel_fragment)
    — cheaper than fetching one here, since fetch_channel_uploads' uploads-
    playlist read carries no channel-level avatar of its own. Absent (a hard
    refresh, a deep link, browser back/forward landing here with no card
    behind it) or untrusted, and with no local copy either, this falls back
    to fetch_channel_avatar's own live fetch rather than rendering with
    none — a second yt-dlp call, so it's only paid on that cold-entry path.
    """
    uploads = fetch_channel_uploads(channel_id)
    if not uploads.items:
        return None

    hero_image = cached_avatar_path(channel_id) or _trusted_avatar_url(avatar_url)
    if hero_image is None:
        fetched_avatar = fetch_channel_avatar(channel_id)
        hero_image = proxied_avatar_url(fetched_avatar) if fetched_avatar else None

    context = _base_context(
        "yt-channel", channel_id, uploads.title or "Channel", uploads.items
    )
    context.update(
        {
            "video_count": len(uploads.items),
            "count_label": f"Latest {len(uploads.items)} uploads",
            "hero_image": hero_image,
            "hero_is_avatar": True,
            "channel_url": CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=channel_id),
            "followed_feed_id": _followed_feed_id(db, user_id, channel_id),
        }
    )
    return context
