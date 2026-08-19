"""Counts that have to agree with the list they describe (app/page_context.py).

Several call sites used to hand-roll their own `func.count(Content.id))`
query instead of going through content_query's shared filter logic — each one
drifted from the list next to it the moment is_preview needed excluding.
Measured live on the real library: a tile read 156 while its own page listed
154 (Travis Scott), 365/362 (Young Thug), 465/463 (Future). These pin the
fix: an is_preview=True row (an Explore result never favorited/saved) must
not inflate a count whose matching list excludes it.
"""

from app.content_query import count_content
from app.models import Artist, Content
from app.page_context import library_context, playlist_detail_context
from app.timeutil import utcnow

USER_ID = 1


def _artist(db_session, channel_id: str, **kwargs) -> Artist:
    artist = Artist(user_id=USER_ID, channel_id=channel_id, name="Preview Test Artist", **kwargs)
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    return artist


def _content(artist: Artist, video_id: str, **kwargs) -> Content:
    return Content(artist_id=artist.id, user_id=USER_ID, video_id=video_id, title=video_id, **kwargs)


def test_count_content_excludes_previews_like_the_page_it_describes(db_session):
    artist = _artist(db_session, "https://example.com/count-previews")
    db_session.add_all(
        [
            _content(artist, "realvideo01", is_preview=False),
            _content(artist, "realvideo02", is_preview=False),
            _content(artist, "previewvid1", is_preview=True),
        ]
    )
    db_session.commit()

    assert count_content(db_session, USER_ID, artist_id=artist.id) == 2


def test_count_content_played_filter_keeps_previews_like_the_page_it_describes(db_session):
    """The one exception: a preview that's actually been listened to still
    belongs on Recently Played (see content_query._content_query's __played__
    carve-out) — the count has to keep matching that, not just the exclusion."""
    artist = _artist(db_session, "https://example.com/count-played-preview")
    db_session.add(_content(artist, "playedpreview01", is_preview=True, last_played_at=utcnow()))
    db_session.commit()

    assert count_content(db_session, USER_ID, filter="__played__") == 1


def test_the_library_tile_count_excludes_previews(db_session):
    """The tile says how many of this artist's tracks the library actually
    holds. A preview — an Explore result never favorited or saved — is not
    one of them, and counting it is what made a tile read 156 next to a list
    of 154 (measured live on Travis Scott)."""
    artist = _artist(db_session, "https://example.com/library-tile-count")
    db_session.add_all(
        [
            _content(artist, "librarytile01", is_preview=False),
            _content(artist, "librarypreview1", is_preview=True),
        ]
    )
    db_session.commit()

    assert library_context(db_session, USER_ID)["artist_track_counts"][artist.id] == 1


def test_favorites_playlist_count_excludes_previews(db_session):
    """Favoriting/saving already clears is_preview as a side effect in
    practice, so this is a defensive guard against that invariant ever
    breaking rather than a bug reproduction — but the count and the list it
    describes still have to agree either way."""
    artist = _artist(db_session, "https://example.com/favorites-preview-count")
    db_session.add_all(
        [
            _content(artist, "favreal0001", is_favorite=True, is_preview=False),
            _content(artist, "favpreview01", is_favorite=True, is_preview=True),
        ]
    )
    db_session.commit()

    context = playlist_detail_context(db_session, USER_ID, "favorites", page=1)

    assert context["video_count"] == 1
    assert context["video_count"] == len(context["content"])


