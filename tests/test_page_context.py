"""Counts that have to agree with the list they describe (app/page_context.py).

Three call sites used to hand-roll their own `func.count(Content.id))` query
instead of going through content_query's shared filter logic — each one drifted
from the paginated list next to it the moment is_preview needed excluding.
Measured live on the real library: a channel tile read 156 while its own page
listed 154 (Travis Scott), 365/362 (Young Thug), 465/463 (Future). These pin
the fix: an is_preview=True row (an Explore result never favorited/saved)
must not inflate a count whose matching list excludes it.
"""

from datetime import timedelta

from app.content_query import RECENTLY_ADDED_MAX_AGE, count_content
from app.models import Content, Feed
from app.page_context import (
    PLAYLIST_KINDS,
    channel_detail_context,
    library_context,
    playlist_detail_context,
    smart_playlists_context,
)
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


def test_on_repeat_includes_only_played_content_ordered_by_play_count(db_session):
    """published_at is deliberately set to *contradict* play_count order
    (playedonce01 is the most recently published, playedlots01 the least) —
    insertion order alone would sort correctly by coincidence even if the
    fix regressed to ordering by published_at instead of play_count."""
    feed = _feed(db_session, "https://example.com/on-repeat")
    db_session.add_all(
        [
            _content(feed, "neverplayed1", play_count=0, published_at=utcnow()),
            _content(feed, "playedonce01", play_count=1, published_at=utcnow()),
            _content(feed, "playedlots01", play_count=9, published_at=utcnow() - timedelta(days=1)),
        ]
    )
    db_session.commit()

    context = playlist_detail_context(db_session, USER_ID, "on-repeat", page=1)

    assert [c.video_id for c in context["content"]] == ["playedlots01", "playedonce01"]


def test_recently_added_excludes_content_older_than_the_window(db_session):
    feed = _feed(db_session, "https://example.com/recently-added")
    db_session.add_all(
        [
            _content(feed, "addedtoday1", added_at=utcnow()),
            _content(feed, "addedoldish1", added_at=utcnow() - RECENTLY_ADDED_MAX_AGE - timedelta(days=1)),
        ]
    )
    db_session.commit()

    context = playlist_detail_context(db_session, USER_ID, "recently-added", page=1)

    assert [c.video_id for c in context["content"]] == ["addedtoday1"]


def test_forgotten_favorites_excludes_favorites_that_have_been_played(db_session):
    feed = _feed(db_session, "https://example.com/forgotten-favorites")
    db_session.add_all(
        [
            _content(feed, "neverplayedfav", is_favorite=True, last_played_at=None),
            _content(feed, "playedfav00001", is_favorite=True, last_played_at=utcnow()),
            _content(feed, "notfavorited01", is_favorite=False),
        ]
    )
    db_session.commit()

    context = playlist_detail_context(db_session, USER_ID, "forgotten-favorites", page=1)

    assert [c.video_id for c in context["content"]] == ["neverplayedfav"]


def test_saved_mix_is_not_a_distinct_kind_it_reuses_saved(db_session):
    """Locked design: Explore's "Saved Mix" card reuses Library's own "Saved
    for later" list verbatim rather than being a distinct smart playlist —
    there's no "saved-mix" kind in PLAYLIST_KINDS at all."""
    assert "saved-mix" not in PLAYLIST_KINDS
    assert "saved" in PLAYLIST_KINDS


def test_smart_playlist_detail_panels_back_link_says_explore(db_session):
    """remote_detail.py already sets back_label="Explore" for yt-channel/
    yt-playlist; the three local smart-playlist kinds need the same override
    or their detail panel's back-link reads "Library" while the tab bar
    (driven by home/detail.js's own EXPLORE_OWNED_LOCAL_KINDS) highlights
    Explore — caught live via a Playwright click-through, not by reading the
    code, since _detail_panel.html's `back_label | default("Library")` fails
    silent rather than raising for a context that never sets it."""
    feed = _feed(db_session, "https://example.com/smart-back-label")
    db_session.add(_content(feed, "backlabeltest", play_count=1))
    db_session.commit()

    for kind in ("on-repeat", "recently-added", "forgotten-favorites"):
        context = playlist_detail_context(db_session, USER_ID, kind, page=1)
        assert context.get("back_label") == "Explore", f"{kind} detail panel doesn't say back_label=Explore"

    # "saved" is Library's own list reused as-is (Explore's "Saved Mix" card
    # links to it), so its detail panel keeps the default "Library" back-link.
    assert "back_label" not in playlist_detail_context(db_session, USER_ID, "saved", page=1)


def test_smart_playlists_shelf_is_empty_with_no_data(db_session):
    assert smart_playlists_context(db_session, USER_ID) == {"smart_playlists": []}


def test_smart_playlists_shelf_only_shows_non_empty_playlists(db_session):
    """A freshly-inserted row defaults added_at=utcnow(), which would also
    qualify it for Recently Added — back-date it so only On Repeat matches."""
    feed = _feed(db_session, "https://example.com/smart-shelf-partial")
    db_session.add(
        _content(feed, "onrepeatcard", play_count=3, added_at=utcnow() - RECENTLY_ADDED_MAX_AGE - timedelta(days=1))
    )
    db_session.commit()

    context = smart_playlists_context(db_session, USER_ID)

    assert [c["kind"] for c in context["smart_playlists"]] == ["on-repeat"]


def test_smart_playlists_shelf_shows_all_four_kinds_with_correct_counts_and_titles(db_session):
    feed = _feed(db_session, "https://example.com/smart-shelf-full")
    db_session.add_all(
        [
            _content(feed, "onrepeat0001", play_count=5, thumbnail_url="https://i.ytimg.com/1.jpg"),
            _content(feed, "recentadded1", added_at=utcnow()),
            _content(feed, "forgottenfav", is_favorite=True, last_played_at=None),
            _content(feed, "savedmixcard", is_saved=True),
        ]
    )
    db_session.commit()

    context = smart_playlists_context(db_session, USER_ID)
    by_kind = {c["kind"]: c for c in context["smart_playlists"]}

    assert set(by_kind) == {"on-repeat", "recently-added", "forgotten-favorites", "saved"}
    assert by_kind["on-repeat"]["title"] == "On Repeat"
    assert by_kind["on-repeat"]["count"] == 1
    assert by_kind["on-repeat"]["thumbnail_url"] == "https://i.ytimg.com/1.jpg"
    # The one presentational override: Library calls this same kind
    # "Saved for later", but on Explore's shelf it's "Saved Mix".
    assert by_kind["saved"]["title"] == "Saved Mix"
