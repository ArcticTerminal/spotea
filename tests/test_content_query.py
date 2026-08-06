from datetime import datetime, timedelta

from app.content_query import CHANNEL_FILTER_PREFIX, query_content_page
from app.models import Content, Feed

USER_ID = 1


def _seed(db_session, *, channels=("Alpha Channel", "Beta Channel"), count=25):
    """`count` items alternating across `channels`, newest (index 0) first."""
    feeds = [
        Feed(user_id=USER_ID, rss_url=f"https://example.com/feed{i}", channel_title=title)
        for i, title in enumerate(channels)
    ]
    db_session.add_all(feeds)
    db_session.commit()
    for f in feeds:
        db_session.refresh(f)

    now = datetime(2026, 1, 1)
    items = []
    for i in range(count):
        item = Content(
            feed_id=feeds[i % len(feeds)].id,
            user_id=USER_ID,
            video_id=f"vid{i:04d}"[:11],
            title=f"Title {count - i:03d}",
            published_at=now - timedelta(days=i),
            is_favorite=(i % 5 == 0),
            is_saved=(i % 4 == 0),
            status="ready" if i < 3 else "not_downloaded",
        )
        items.append(item)
    db_session.add_all(items)
    db_session.commit()
    return feeds, items


def test_default_pagination_is_newest_first_20_per_page(db_session):
    _seed(db_session, count=25)

    items, page, total_pages = query_content_page(db_session, USER_ID)

    assert page == 1
    assert total_pages == 2
    assert len(items) == 20
    assert items[0].title == "Title 025"  # most recently published
    assert items[-1].title == "Title 006"


def test_second_page_has_the_remainder(db_session):
    _seed(db_session, count=25)

    items, page, total_pages = query_content_page(db_session, USER_ID, page=2)

    assert page == 2
    assert total_pages == 2
    assert len(items) == 5
    assert items[0].title == "Title 005"


def test_page_zero_and_negative_clamp_to_first_page(db_session):
    _seed(db_session, count=25)

    for requested in (0, -5, -1):
        items, page, total_pages = query_content_page(db_session, USER_ID, page=requested)
        assert page == 1
        assert len(items) == 20


def test_page_past_the_end_clamps_to_last_page(db_session):
    _seed(db_session, count=25)

    items, page, total_pages = query_content_page(db_session, USER_ID, page=9999)

    assert page == total_pages == 2
    assert len(items) == 5


def test_empty_library_is_one_empty_page(db_session):
    items, page, total_pages = query_content_page(db_session, USER_ID)

    assert items == []
    assert page == 1
    assert total_pages == 1


def test_filter_favorites(db_session):
    _seed(db_session, count=25)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="__favorites__", page_size=100)

    assert total_pages == 1
    assert items  # at least one favorite (i % 5 == 0 in the seed)
    assert all(i.is_favorite for i in items)


def test_filter_saved(db_session):
    _seed(db_session, count=25)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="__saved__", page_size=100)

    assert items
    assert all(i.is_saved for i in items)


def test_filter_by_channel_title(db_session):
    _seed(db_session, channels=("Alpha Channel", "Beta Channel"), count=10)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="Beta Channel", page_size=100)

    assert items
    assert all(i.feed.channel_title == "Beta Channel" for i in items)


def test_filter_exact_channel_excludes_title_matches(db_session):
    """The exact-channel filter (CHANNEL_FILTER_PREFIX) must not pick up a
    video from a *different* channel just because its title happens to
    mention the target channel's name — that's what the plain substring
    filter below is for, and conflating the two was the actual bug this
    guards against."""
    feeds = [
        Feed(user_id=USER_ID, rss_url="https://example.com/feedA", channel_title="Linus Tech Tips"),
        Feed(user_id=USER_ID, rss_url="https://example.com/feedB", channel_title="Brodie Robertson"),
    ]
    db_session.add_all(feeds)
    db_session.commit()
    for f in feeds:
        db_session.refresh(f)

    now = datetime(2026, 1, 1)
    db_session.add_all(
        [
            Content(feed_id=feeds[0].id, user_id=USER_ID, video_id="vid00000001", title="A regular video", published_at=now),
            Content(
                feed_id=feeds[1].id,
                user_id=USER_ID,
                video_id="vid00000002",
                title="Thank You Linus Tech Tips",
                published_at=now,
            ),
        ]
    )
    db_session.commit()

    exact_items, _, _ = query_content_page(
        db_session, USER_ID, filter=f"{CHANNEL_FILTER_PREFIX}Linus Tech Tips", page_size=100
    )
    assert [i.feed.channel_title for i in exact_items] == ["Linus Tech Tips"]

    substring_items, _, _ = query_content_page(db_session, USER_ID, filter="Linus Tech Tips", page_size=100)
    assert {i.feed.channel_title for i in substring_items} == {"Linus Tech Tips", "Brodie Robertson"}


def test_filter_with_no_matches_is_a_valid_empty_page(db_session):
    _seed(db_session, count=5)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="Nonexistent Channel")

    assert items == []
    assert page == 1
    assert total_pages == 1


def test_only_returns_the_requesting_users_content(db_session):
    _seed(db_session, count=5)

    items, page, total_pages = query_content_page(db_session, USER_ID + 999)

    assert items == []
