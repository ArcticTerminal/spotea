"""Smoke tests for the server-rendered pages (routers/pages.py).

Every other test file exercises JSON endpoints, so nothing covered the
Jinja templates at all: a renamed context key, a filter dropped from
`templates.env.filters`, a `{% include %}` referencing a variable a caller
stopped passing — all of it would only surface by opening the app in a
browser. These are deliberately shallow (status code plus one marker string
that could only come from the template actually rendering) — they're here
to catch "the page 500s", not to assert layout.
"""

from datetime import datetime, timedelta

from app.models import Content, Feed
from app.timeutil import utcnow

USER_ID = 1


def _seed(db_session, *, followed=True):
    """One followed channel with three items, each hitting a different
    Library surface: a new upload, a favorite, and a saved+downloaded one."""
    feed = Feed(
        user_id=USER_ID,
        rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCpagetest00000000000000",
        channel_title="Page Test Channel",
        followed=followed,
    )
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    now = utcnow()
    items = [
        Content(
            feed_id=feed.id,
            user_id=USER_ID,
            video_id="newupload01",
            title="Fresh Upload",
            published_at=now - timedelta(days=1),
            duration_seconds=305,
            is_new_upload=True,
        ),
        Content(
            feed_id=feed.id,
            user_id=USER_ID,
            video_id="favorite001",
            title="A Favorite",
            published_at=datetime(2026, 1, 1),
            duration_seconds=61,
            is_favorite=True,
        ),
        Content(
            feed_id=feed.id,
            user_id=USER_ID,
            video_id="savedplay01",
            title="Saved And Played",
            published_at=datetime(2025, 12, 1),
            is_saved=True,
            last_played_at=now,
            status="ready",
            file_path="/nonexistent/savedplay01.m4a",
        ),
    ]
    db_session.add_all(items)
    db_session.commit()
    return feed, items


def test_home_renders_every_shelf_and_the_library_grid(client, db_session):
    _seed(db_session)

    res = client.get("/")

    assert res.status_code == 200
    body = res.text
    assert "Fresh Upload" in body  # New uploads shelf
    assert "A Favorite" in body  # Favorites shelf
    assert "Saved And Played" in body  # Saved + Recently played shelves
    assert "Page Test Channel" in body  # Library channel card + Home chip


def test_home_renders_for_an_empty_library(client):
    """The "no content yet" path is a different branch of index.html than
    the one every seeded test takes."""
    res = client.get("/")

    assert res.status_code == 200
    assert "add a channel in the Explore tab" in res.text


def test_home_applies_the_duration_and_filesize_template_filters(client, db_session):
    """Both filters are registered on pages.py's own Jinja2Templates
    instance — a second instance (routers/auth.py has one) doesn't have
    them, so rendering a page through the wrong one raises."""
    _seed(db_session)

    body = client.get("/").text

    assert "5:05" in body  # duration filter, from duration_seconds=305
    assert "MB" in body  # filesize filter, from the storage summary


def test_favorites_page(client, db_session):
    _seed(db_session)

    res = client.get("/favorites")

    assert res.status_code == 200
    assert "A Favorite" in res.text
    assert "Fresh Upload" not in res.text


def test_saved_page(client, db_session):
    _seed(db_session)

    res = client.get("/saved")

    assert res.status_code == 200
    assert "Saved And Played" in res.text
    assert "A Favorite" not in res.text


def test_new_uploads_page(client, db_session):
    _seed(db_session)

    res = client.get("/new-uploads")

    assert res.status_code == 200
    assert "Fresh Upload" in res.text
    assert "A Favorite" not in res.text


def test_recently_played_page(client, db_session):
    _seed(db_session)

    res = client.get("/recently-played")

    assert res.status_code == 200
    assert "Saved And Played" in res.text
    assert "Fresh Upload" not in res.text


def test_empty_list_pages_render_their_empty_message(client):
    for path, message in [
        ("/favorites", "No favorites yet."),
        ("/saved", "Nothing saved yet."),
        ("/new-uploads", "No new uploads yet."),
        ("/recently-played", "Nothing played yet."),
    ]:
        res = client.get(path)
        assert res.status_code == 200, path
        assert message in res.text, path


def test_channel_page(client, db_session):
    feed, _items = _seed(db_session)

    res = client.get(f"/channel/{feed.id}")

    assert res.status_code == 200
    assert "Page Test Channel" in res.text
    assert "Fresh Upload" in res.text
    assert "3 videos" in res.text


def test_channel_page_404s_for_an_unknown_feed(client):
    assert client.get("/channel/9999").status_code == 404


def test_player_page(client, db_session):
    _feed, items = _seed(db_session)

    res = client.get(f"/player/{items[0].id}")

    assert res.status_code == 200
    assert "Fresh Upload" in res.text
    assert f'data-stream="/content/{items[0].id}/stream"' in res.text


def test_player_page_404s_for_an_unknown_content_id(client):
    assert client.get("/player/9999").status_code == 404


def test_pagination_appears_only_once_there_is_a_second_page(client, db_session):
    """query_content_page's DEFAULT_PAGE_SIZE is 20, so 25 items is two
    pages — and _pagination.html renders nothing at all below that."""
    feed, _items = _seed(db_session)
    db_session.add_all(
        [
            Content(
                feed_id=feed.id,
                user_id=USER_ID,
                video_id=f"bulkvid{i:04d}"[:11],
                title=f"Bulk {i}",
                published_at=datetime(2025, 6, 1) - timedelta(days=i),
                is_favorite=True,
            )
            for i in range(25)
        ]
    )
    db_session.commit()

    first_page = client.get("/favorites")
    assert "Page 1 of 2" in first_page.text

    second_page = client.get("/favorites?page=2")
    assert second_page.status_code == 200
    assert "Page 2 of 2" in second_page.text


def test_page_routes_require_login():
    """Every route in pages.py sits behind require_login — an unauthenticated
    request has to land on /login, not render someone's library."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for path in ["/", "/favorites", "/saved", "/new-uploads", "/recently-played", "/channel/1"]:
            res = anonymous.get(path, follow_redirects=False)
            assert res.status_code == 303, path
            assert res.headers["location"] == "/login", path
