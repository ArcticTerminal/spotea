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


def test_channel_and_playlist_pages_redirect_to_their_hash_route(client):
    """Favorites/Saved/New Uploads/Recently Played/a channel/a track all moved
    in-page (see app/static/js/home/detail.js, home/overlay.js) — these
    routes exist only so an old link or bookmark still lands somewhere real.
    The actual rendering is now GET /partials/detail/... — see
    test_partials.py."""
    for path, expected_hash in [
        ("/favorites", "/#favorites"),
        ("/saved", "/#saved"),
        ("/new-uploads", "/#new-uploads"),
        ("/recently-played", "/#recently-played"),
        ("/channel/1", "/#channel/1"),
        ("/player/1", "/#player/1"),
    ]:
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 307, path
        assert res.headers["location"] == expected_hash, path


def test_page_routes_require_login():
    """Every route in pages.py sits behind require_login — an unauthenticated
    request has to land on /login, not render someone's library or redirect
    into the app."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for path in ["/", "/favorites", "/saved", "/new-uploads", "/recently-played", "/channel/1", "/player/1"]:
            res = anonymous.get(path, follow_redirects=False)
            assert res.status_code == 303, path
            assert res.headers["location"] == "/login", path
