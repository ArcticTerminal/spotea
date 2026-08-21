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

from app.models import Artist, Content
from app.timeutil import utcnow

USER_ID = 1


def _seed(db_session, *, followed=True):
    """One followed channel with three items, each hitting a different
    Library surface: a new upload, a favorite, and a saved+downloaded one."""
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCpagetest00000000000000",
        name="Page Test Channel",
        followed=followed,
    )
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    now = utcnow()
    items = [
        Content(
            artist_id=artist.id,
            user_id=USER_ID,
            video_id="newupload01",
            title="Fresh Upload",
            published_at=now - timedelta(days=1),
            duration_seconds=305,
            is_new_upload=True,
        ),
        Content(
            artist_id=artist.id,
            user_id=USER_ID,
            video_id="favorite001",
            title="A Favorite",
            published_at=datetime(2026, 1, 1),
            duration_seconds=61,
            is_favorite=True,
        ),
        Content(
            artist_id=artist.id,
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
    return artist, items


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

def test_channel_avatars_are_lazy_loaded(client, db_session):
    """70 eager image requests were measured on a real, heavy library —
    avatars in the Library grid and Home's "Recently followed" chips were
    the two spots that never got the loading="lazy" every content thumbnail
    already has."""
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCavatarlazy00000000000",
        browse_id="UCavatarlazy00000000000",
        name="Avatar Lazy Artist",
        avatar_url="/avatars/UCavatarlazy00000000000.jpg",
    )
    db_session.add(artist)
    db_session.commit()

    body = client.get("/").text

    assert '<img class="channel-chip-avatar" src="/avatars/UCavatarlazy00000000000.jpg" alt="" loading="lazy" />' in body
    assert '<img class="channel-card-avatar" src="/avatars/UCavatarlazy00000000000.jpg" alt="" loading="lazy" />' in body


def test_the_duration_and_filesize_template_filters_are_registered(client, db_session):
    """Both filters live on the one shared Jinja2Templates instance (see
    app/templating.py). A template rendered through an environment missing
    one raises at render time, and nothing else would catch it.

    They're checked on two surfaces because that's where each one actually
    renders: filesize in Home's storage summary, duration in a track row.
    Cards used to stamp the duration over the artwork, which put both on
    Home — that badge is gone (a video convention on a music cover), so the
    duration is now asserted where track durations live."""
    _seed(db_session)

    assert "MB" in client.get("/").text  # filesize, from the storage summary

    rows = client.get("/partials/detail/playlist/new-uploads").text
    assert "5:05" in rows  # duration, from duration_seconds=305


def test_channel_and_playlist_pages_redirect_to_their_hash_route(client):
    """Favorites/Saved/New releases/Recently Played/a track all moved
    in-page (see app/static/js/home/detail.js, home/overlay.js) — these
    routes exist only so an old link or bookmark still lands somewhere real.
    The actual rendering is now GET /partials/detail/... — see
    test_partials.py."""
    for path, expected_hash in [
        ("/favorites", "/#favorites"),
        ("/saved", "/#saved"),
        ("/new-uploads", "/#new-uploads"),
        ("/recently-played", "/#recently-played"),
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
        for path in ["/", "/favorites", "/saved", "/new-uploads", "/recently-played", "/player/1"]:
            res = anonymous.get(path, follow_redirects=False)
            assert res.status_code == 303, path
            assert res.headers["location"] == "/login", path
