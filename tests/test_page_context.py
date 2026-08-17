"""Counts that have to agree with the list they describe (app/page_context.py).

Three call sites used to hand-roll their own `func.count(Content.id))` query
instead of going through content_query's shared filter logic — each one drifted
from the paginated list next to it the moment is_preview needed excluding.
Measured live on the real library: a channel tile read 156 while its own page
listed 154 (Travis Scott), 365/362 (Young Thug), 465/463 (Future). These pin
the fix: an is_preview=True row (an Explore result never favorited/saved)
must not inflate a count whose matching list excludes it.
"""

from app.content_query import count_content
from app.models import Content, Feed
from app.page_context import channel_detail_context, library_context, playlist_detail_context
from app.timeutil import utcnow

USER_ID = 1


def _feed(db_session, rss_url: str, **kwargs) -> Feed:
    feed = Feed(user_id=USER_ID, rss_url=rss_url, channel_title="Preview Test Channel", **kwargs)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    return feed


def _content(feed: Feed, video_id: str, **kwargs) -> Content:
    return Content(feed_id=feed.id, user_id=USER_ID, video_id=video_id, title=video_id, **kwargs)


def test_count_content_excludes_previews_like_the_page_it_describes(db_session):
    feed = _feed(db_session, "https://example.com/count-previews")
    db_session.add_all(
        [
            _content(feed, "realvideo01", is_preview=False),
            _content(feed, "realvideo02", is_preview=False),
            _content(feed, "previewvid1", is_preview=True),
        ]
    )
    db_session.commit()

    assert count_content(db_session, USER_ID, feed_id=feed.id) == 2


def test_count_content_played_filter_keeps_previews_like_the_page_it_describes(db_session):
    """The one exception: a preview that's actually been listened to still
    belongs on Recently Played (see content_query._content_query's __played__
    carve-out) — the count has to keep matching that, not just the exclusion."""
    feed = _feed(db_session, "https://example.com/count-played-preview")
    db_session.add(_content(feed, "playedpreview01", is_preview=True, last_played_at=utcnow()))
    db_session.commit()

    assert count_content(db_session, USER_ID, filter="__played__") == 1


def test_channel_video_count_matches_the_channels_own_page(db_session):
    feed = _feed(db_session, "https://example.com/channel-tile-count")
    db_session.add_all(
        [
            _content(feed, "chanvid00001", is_preview=False),
            _content(feed, "chanvid00002", is_preview=False),
            _content(feed, "chanpreview01", is_preview=True),
        ]
    )
    db_session.commit()

    context = channel_detail_context(db_session, USER_ID, feed.id, page=1)

    assert context["video_count"] == 2
    assert context["video_count"] == len(context["content"])


def test_library_grid_tile_count_matches_the_channels_own_page(db_session):
    feed = _feed(db_session, "https://example.com/library-tile-count")
    db_session.add_all(
        [
            _content(feed, "librarytile01", is_preview=False),
            _content(feed, "librarypreview1", is_preview=True),
        ]
    )
    db_session.commit()

    tile_count = library_context(db_session, USER_ID)["channel_video_counts"][feed.id]
    page_count = channel_detail_context(db_session, USER_ID, feed.id, page=1)["video_count"]

    assert tile_count == page_count == 1


def test_favorites_playlist_count_excludes_previews(db_session):
    """Favoriting/saving already clears is_preview as a side effect in
    practice, so this is a defensive guard against that invariant ever
    breaking rather than a bug reproduction — but the count and the list it
    describes still have to agree either way."""
    feed = _feed(db_session, "https://example.com/favorites-preview-count")
    db_session.add_all(
        [
            _content(feed, "favreal0001", is_favorite=True, is_preview=False),
            _content(feed, "favpreview01", is_favorite=True, is_preview=True),
        ]
    )
    db_session.commit()

    context = playlist_detail_context(db_session, USER_ID, "favorites", page=1)

    assert context["video_count"] == 1
    assert context["video_count"] == len(context["content"])
