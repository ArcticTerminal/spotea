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
from app.timeutil import utcnow
from app.youtube.music import ARTIST_PREVIEW_SONGS, fetch_artist, fetch_release
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


def _artist_or_channel(db: Session, user_id: int, browse_id: str):
    """The artist behind an id, plus where a Follow on them should land.

    Returns None when the id doesn't name an artist with anything playable
    — the caller falls back to the channel listing. That fallback is why
    every channel result can route through the artist surface at all: a
    podcast, a tech channel or an artist page with nothing on it comes back
    as the plain listing it would have shown anyway. It costs one extra
    YouTube Music request on a click the user made deliberately, and never
    touches youtube.com.

    The follow target is the artist's **"<Artist> - Topic" channel**, which
    is what "following an artist" means in a music app: that channel
    carries their releases and nothing else, so a new single reaches Home's
    New Uploads through the RSS sync that already runs, while their
    official channel's feed would deliver vlogs and interviews alongside
    it. Nothing else in this app follows a Topic channel — channel search
    drops them by name, since nobody browsing for a channel means to pick
    one — and this is the single place where following one is the right
    answer rather than the wrong one.

    Falls back to the official channel, then to the browse id, for the
    artists with no Topic channel behind them: a worse answer than the
    right one, and a better answer than a button that does nothing.
    """
    artist = fetch_artist(browse_id)
    if artist is None or not artist.tracks:
        return None

    follow_channel_id = artist.topic_channel_id or artist.channel_id or browse_id
    return artist, {
        "hero_image": cached_avatar_or_hotlink(follow_channel_id, artist.avatar_url),
        "hero_is_avatar": True,
        "channel_url": CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=follow_channel_id),
        "followed_feed_id": _followed_feed_id(db, user_id, follow_channel_id),
        # Sent back with the follow so the feed can be recorded as this
        # artist's — see routers/feeds.py. The browse id rather than the
        # channel it targets: it's what reopens this page.
        "artist_browse_id": browse_id,
    }


def remote_artist_context(
    db: Session, user_id: int, browse_id: str, avatar_url: str | None = None
) -> dict | None:
    """An artist's profile — what YouTube Music's own artist page shows.

    Shelves rather than one long track list, because the two answer
    different questions. "What did they just release" is a date-ordered
    question the songs list can't answer (it ranks by popularity, so a new
    single sits wherever it charts), and "what's their album called" is one
    a track list buries. Albums, singles, videos and related artists all
    arrive in the same response the songs come from, so rendering the whole
    profile costs exactly what the bare list cost.

    The songs here are a preview; remote_artist_songs_context has all of
    them.
    """
    resolved = _artist_or_channel(db, user_id, browse_id)
    if resolved is None:
        return remote_channel_context(db, user_id, browse_id, avatar_url=avatar_url)
    artist, hero = resolved

    context = {
        "kind": "yt-artist",
        "remote": True,
        "feed": None,
        "title": artist.name,
        "back_label": "Explore",
        "description": artist.description,
        "count_label": (
            f"{artist.monthly_listeners} monthly listeners"
            if artist.monthly_listeners
            else f"{artist.track_count} tracks"
        ),
        "songs": artist.tracks[:ARTIST_PREVIEW_SONGS],
        # Only worth a "See all" when there is more behind it than the
        # preview already shows.
        "songs_total": artist.track_count if artist.track_count > ARTIST_PREVIEW_SONGS else 0,
        "songs_url": f"/#yt-artist-songs/{browse_id}",
        "albums": artist.albums,
        "singles": artist.singles,
        "videos": artist.videos,
        "related": artist.related,
        # See ArtistRelease: the year is the only date this surface reports,
        # so "new" can mean nothing finer than "released this year".
        "current_year": str(utcnow().year),
    }
    context.update(hero)
    return context


def remote_artist_songs_context(
    db: Session, user_id: int, browse_id: str, avatar_url: str | None = None
) -> dict | None:
    """Everything the artist has, as one track list — the profile's "See
    all". Same hero and the same Follow, so arriving here from the profile
    doesn't feel like leaving the artist."""
    resolved = _artist_or_channel(db, user_id, browse_id)
    if resolved is None:
        return remote_channel_context(db, user_id, browse_id, avatar_url=avatar_url)
    artist, hero = resolved

    # Says so explicitly when the list is short of what YouTube Music
    # reports, the same way a remote playlist does. ARTIST_TRACK_LIMIT sits
    # above anything that surface will hand over, so this is rarely the app
    # truncating: it's the handful of entries per playlist that don't
    # survive parsing (see fetch_artist), and a page reading "143 tracks"
    # would be claiming to be the whole of a 150-track list.
    shown = len(artist.tracks)
    count_label = (
        f"First {shown} of {artist.track_count} tracks"
        if artist.track_count > shown
        else f"{shown} track{'' if shown == 1 else 's'}"
    )

    context = _base_context("yt-artist-songs", browse_id, artist.name, artist.tracks)
    context.update({"video_count": shown, "count_label": count_label, **hero})
    # Back to the artist rather than out to Explore: this view is one level
    # in, and the button pops history, which is where the profile is.
    context["back_label"] = artist.name
    return context


def remote_release_context(browse_id: str) -> dict | None:
    """One album or single. Rendered by the same panel a playlist is, since
    that is what it is once opened — a short ordered list of tracks with a
    cover."""
    release = fetch_release(browse_id)
    if release is None:
        return None

    subtitle = " · ".join(
        part for part in (release.kind, release.year, release.artist_names) if part
    )
    context = _base_context("yt-release", browse_id, release.title, release.tracks)
    context.update(
        {
            # The one view here that can't name where Back goes. Every other
            # remote kind is entered from Explore; this one is entered from
            # an artist profile — but not necessarily *this* release's
            # artist, since a collaboration single is credited to whoever
            # released it rather than to the page you clicked from. The
            # button pops history either way, so the label is all that's in
            # question, and a wrong name is worse than no name.
            "back_label": "Back",
            "video_count": len(release.tracks),
            # A single is one track, which is the common case here — "1
            # tracks" on the very shortest release reads as a bug.
            "count_label": (
                f"{subtitle} · {len(release.tracks)} track"
                f"{'' if len(release.tracks) == 1 else 's'}"
            ),
            "hero_image": release.cover_url,
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
