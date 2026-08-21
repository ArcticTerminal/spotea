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
    Library surface: a new upload, a favorite, and a played+downloaded one."""
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
            video_id="playedone1",
            title="Played And Downloaded",
            published_at=datetime(2025, 12, 1),
            last_played_at=now,
            status="ready",
            file_path="/nonexistent/playedone1.m4a",
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
    # Not "Fresh Upload": Home's "New releases" shelf is releases read off
    # Artist.release_snapshot now, not is_new_upload Content rows — see
    # page_context._new_releases. That row is still reachable through
    # Library's own New releases playlist.
    assert "A Favorite" in body  # Favorites shelf
    assert "Played And Downloaded" in body  # Recently played shelf
    assert "Page Test Channel" in body  # Library channel card + Home chip


def test_home_offers_onboarding_to_a_brand_new_library(client):
    """Nothing followed and no interests listed: every shelf on this page and
    most of Explore has nothing to build from, so Home asks what the user
    listens to instead of naming a tab and leaving it there. Interests are
    what Explore's Playlists shelf is built from — see interests.py."""
    res = client.get("/")

    assert res.status_code == 200
    assert 'id="onboarding"' in res.text
    assert "What do you listen to?" in res.text
    assert 'data-genre="Rock"' in res.text


def test_a_library_that_has_been_started_gets_no_onboarding(client, db_session):
    """One interest is enough to mean "this person has been here" — the panel
    would otherwise come back on every visit until something is played."""
    from app.models import User

    db_session.query(User).filter(User.id == 1).update({"interests": "rock"})
    db_session.commit()

    body = client.get("/").text

    assert 'id="onboarding"' not in body
    assert "Nothing played yet" in body

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
    """Favorites/New releases/Recently Played/a track all moved
    in-page (see app/static/js/home/detail.js, home/overlay.js) — these
    routes exist only so an old link or bookmark still lands somewhere real.
    The actual rendering is now GET /partials/detail/... — see
    test_partials.py."""
    for path, expected_hash in [
        ("/favorites", "/#favorites"),
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
        for path in ["/", "/favorites", "/new-uploads", "/recently-played", "/player/1"]:
            res = anonymous.get(path, follow_redirects=False)
            assert res.status_code == 303, path
            assert res.headers["location"] == "/login", path


def test_the_player_overlay_renders_every_element_its_script_binds(client):
    """A structural regression guard.

    home/overlay.js and player.js bind these by id at boot and, for most of
    them, without a null check — dropping one from the template doesn't
    degrade the player, it throws during setup and takes every later
    setup call in pages/index.js down with it. The symptom is a blank app,
    and nothing else in this suite would notice: every route still answers
    200 with markup that merely happens to be missing a div.
    """
    body = client.get("/").text

    for element_id in [
        "player-overlay",
        "player-root",  # the card. Dropped once; nothing failed but the app.
        "player-art-img",
        "queue-panel",
        "queue-panel-body",
        "lyrics-panel-body",
        "panel-tab-queue",
        "panel-tab-lyrics",
        "queue-toggle",
        "overlay-collapse-btn",
        "mini-player",
        "mini-player-progress",
        "mini-player-playpause",
    ]:
        assert f'id="{element_id}"' in body, element_id


def test_the_player_card_wraps_its_column_and_its_panel(client):
    """The desktop layout puts .player-main and #queue-panel side by side
    inside #player-root (see style.css's min-width: 900px block). If the
    panel ends up inside .player-main, or the card stops wrapping both,
    the two-column layout silently becomes one column again."""
    body = client.get("/").text

    card = body.index('id="player-root"')
    main = body.index('class="player-main"')
    panel = body.index('id="queue-panel"')
    assert card < main < panel
