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
actually starts (POST /artists/videos/batch), which costs no YouTube requests
at all because every field those rows need is already in the response below.

Kept out of page_context.py deliberately — everything there is DB queries,
and these two builders make network calls that take seconds.
"""

from sqlalchemy.orm import Session

from app.images import cached_avatar_or_hotlink
from app.models import Artist
from app.timeutil import utcnow
from app.youtube.music import ARTIST_PREVIEW_SONGS, fetch_artist, fetch_playlist, fetch_release
from app.youtube.urls import CHANNEL_PAGE_URL_TEMPLATE


def _base_context(kind: str, remote_id: str, title: str, items: list) -> dict:
    return {
        "kind": kind,
        "remote": True,
        "artist": None,
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


def _followed_artist_id(db: Session, user_id: int, channel_id: str) -> int | None:
    followed = (
        db.query(Artist)
        .filter(
            Artist.user_id == user_id,
            Artist.channel_id == channel_id,
            Artist.followed.is_(True),
        )
        .first()
    )
    return followed.id if followed else None


def _artist_or_channel(db: Session, user_id: int, browse_id: str):
    """The artist behind an id, plus where a Follow on them should land.

    Returns None when the id doesn't name an artist with anything playable,
    which is a 404 on the panel — there is no channel listing to fall back
    to any more, and an id that came out of an artist search or an artist's
    own page is one YouTube Music can answer for.

    The follow target is the artist's **"<Artist> - Topic" channel**, which
    is what "following an artist" means in a music app: that channel
    carries their releases and nothing else, while their official channel's
    artist would deliver vlogs and interviews alongside it.

    Falls back to the official channel, then to the browse id, for the
    artists with no Topic channel behind them: a worse answer than the
    right one, and a better answer than a button that does nothing.
    """
    artist = fetch_artist(browse_id)
    if artist is None or not artist.tracks:
        return None

    follow_channel_id = artist.topic_channel_id or artist.channel_id or artist.browse_id
    return artist, {
        "hero_image": cached_avatar_or_hotlink(follow_channel_id, artist.avatar_url),
        "hero_is_avatar": True,
        "channel_url": CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=follow_channel_id),
        "followed_artist_id": _followed_artist_id(db, user_id, follow_channel_id),
        # Sent back with the follow so the artist can be recorded as this
        # artist's — see routers/artists.py. The browse id rather than the
        # channel it targets: it's what reopens this page.
        #
        # The profile's own browse id, not the one this was asked for. They
        # differ when a VEVO channel was followed through to the page that
        # actually has the music (see music._redirected_artist), and sending
        # the VEVO id back would record the artist against the songless page.
        "browse_id": artist.browse_id,
    }


def remote_artist_context(
    db: Session, user_id: int, browse_id: str
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
        return None
    artist, hero = resolved

    context = {
        "kind": "yt-artist",
        "remote": True,
        "artist": None,
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
        "related": artist.related,
        # See ArtistRelease: the year is the only date this surface reports,
        # so "new" can mean nothing finer than "released this year".
        "current_year": str(utcnow().year),
    }
    context.update(hero)
    return context


def remote_artist_songs_context(
    db: Session, user_id: int, browse_id: str
) -> dict | None:
    """Everything the artist has, as one track list — the profile's "See
    all". Same hero and the same Follow, so arriving here from the profile
    doesn't feel like leaving the artist."""
    resolved = _artist_or_channel(db, user_id, browse_id)
    if resolved is None:
        return None
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
