from datetime import datetime, timedelta

from app.models import Content, Feed

USER_ID = 1


def _seed(db_session, count=25):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/feed", channel_title="Test Channel")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    now = datetime(2026, 1, 1)
    items = [
        Content(
            feed_id=feed.id,
            user_id=USER_ID,
            video_id=f"vid{i:04d}"[:11],
            title=f"Title {count - i:03d}",
            published_at=now - timedelta(days=i),
            duration_seconds=120 + i,
            is_favorite=(i == 0),
            status="not_downloaded",
        )
        for i in range(count)
    ]
    db_session.add_all(items)
    db_session.commit()
    return feed, items


def test_get_content_default_page_shape(client, db_session):
    _seed(db_session, count=25)

    res = client.get("/content")
    assert res.status_code == 200

    body = res.json()
    assert set(body.keys()) == {"items", "page", "total_pages"}
    assert body["page"] == 1
    assert body["total_pages"] == 2
    assert len(body["items"]) == 20

    first = body["items"][0]
    assert set(first.keys()) == {
        "id",
        "feed_id",
        "channel_title",
        "video_id",
        "title",
        "thumbnail_url",
        "duration_seconds",
        "published_at",
        "status",
        "added_at",
        "is_favorite",
        "is_saved",
    }
    assert first["channel_title"] == "Test Channel"
    assert first["title"] == "Title 025"


def test_get_content_out_of_range_page_clamps_instead_of_erroring(client, db_session):
    _seed(db_session, count=5)

    res = client.get("/content", params={"page": 999})
    assert res.status_code == 200

    body = res.json()
    assert body["page"] == 1
    assert body["total_pages"] == 1
    assert len(body["items"]) == 5


def test_get_content_favorites_filter(client, db_session):
    _seed(db_session, count=25)

    res = client.get("/content", params={"filter": "__favorites__"})
    assert res.status_code == 200

    body = res.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["is_favorite"] is True


def test_get_content_channel_filter(client, db_session):
    _seed(db_session, count=5)

    res = client.get("/content", params={"filter": "Test Channel"})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 5

    res = client.get("/content", params={"filter": "Nonexistent Channel"})
    assert res.status_code == 200
    assert res.json()["items"] == []
