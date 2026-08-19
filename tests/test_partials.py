"""The fragment endpoints behind the in-place refresh (routers/partials.py).

The property that matters is that a fragment renders the *same* markup the
full page does for that region — a refresh that quietly disagreed with the
first render would reintroduce exactly the staleness fragments exist to
remove. These tests pin that down by rendering both and comparing.
"""

import re
from datetime import datetime, timedelta

from app.models import Content, Feed, User
from app.timeutil import utcnow

USER_ID = 1

# Must match conftest.py's own DEFAULT_USER_ID — duplicated rather than
# imported, same as test_content_api.py and test_profiles_api.py do: importing
# conftest as a module re-runs the env setup at its top, which points the
# settings singleton at a second temp directory and trips its own isolation
# guard.
DEFAULT_USER_ID = 1


def _other_user_feed(db_session) -> Feed:
    """A second profile with one channel, for the scoping tests below.

    A real `User` row rather than a made-up user_id: SQLite only enforces
    foreign keys when a connection asks it to, and the app now does (see
    app/database.py), so a feed pointing at a profile that doesn't exist is
    rejected rather than silently accepted.
    """
    other_user = User(email="other3@example.com", password_hash="x")
    db_session.add(other_user)
    db_session.commit()
    db_session.refresh(other_user)

    feed = Feed(
        user_id=other_user.id,
        rss_url="https://example.com/other",
        channel_title="Someone Else",
    )
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    return feed

FRAGMENTS = [
    ("/partials/home", ["home-shelves"]),
    ("/partials/library", ["library-grid"]),
    ("/partials/downloads", ["downloads-body"]),
    ("/partials/storage-summary", ["settings-storage-desc"]),
]


def _seed(db_session):
    feed = Feed(
        user_id=USER_ID,
        rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCpartials000000000000000",
        channel_title="Partial Channel",
    )
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    now = utcnow()
    db_session.add_all(
        [
            Content(
                feed_id=feed.id, user_id=USER_ID, video_id="partnew0001", title="Fresh Upload",
                published_at=now - timedelta(days=1), duration_seconds=300, is_new_upload=True,
            ),
            Content(
                feed_id=feed.id, user_id=USER_ID, video_id="partfav0001", title="A Favorite",
                published_at=datetime(2026, 1, 1), is_favorite=True,
                status="ready", file_path="/nonexistent.m4a", file_size_bytes=3 * 1024 * 1024,
            ),
            Content(
                feed_id=feed.id, user_id=USER_ID, video_id="partsave001", title="Saved And Played",
                published_at=datetime(2025, 12, 1), is_saved=True, last_played_at=now,
            ),
        ]
    )
    db_session.commit()
    return feed


def _normalize(html):
    return re.sub(r"\s+", " ", html).strip()


def _fragment_body(html, target):
    match = re.search(rf'<template data-target="{target}">(.*?)</template>', html, re.S)
    assert match, f"no <template data-target={target!r}> in fragment"
    return _normalize(match.group(1))


def test_every_fragment_declares_its_targets(client, db_session):
    _seed(db_session)
    for url, targets in FRAGMENTS:
        res = client.get(url)
        assert res.status_code == 200, url
        for target in targets:
            assert f'data-target="{target}"' in res.text, (url, target)


def test_fragments_match_what_the_full_page_renders(client, db_session):
    """The whole point: swapping a fragment in must leave the page in the
    state a fresh load would have produced."""
    _seed(db_session)
    page = _normalize(client.get("/").text)

    for url, targets in FRAGMENTS:
        fragment = client.get(url).text
        for target in targets:
            body = _fragment_body(fragment, target)
            assert body, (url, target)
            assert body in page, f"{url} -> {target} is not what index.html renders"


def _shelf_row(html, row_id):
    """One shelf row's markup, from its id up to the next row's. Matched on
    ids rather than visible text because "Saved for later" is both a shelf
    title and every card's save-button label."""
    start = html.index(f'id="{row_id}"')
    rest = html[start:]
    following = re.search(r'id="home-[a-z-]+-row"', rest[1:])
    return rest[: following.start() + 1] if following else rest


def test_home_fragment_reflects_a_change_without_a_page_reload(client, db_session):
    """A save has to show up in the next fragment render — this is the
    behaviour that replaced hand-patching the shelf."""
    _seed(db_session)
    content = db_session.query(Content).filter(Content.video_id == "partnew0001").first()
    marker = f'data-content-id="{content.id}"'

    before = _fragment_body(client.get("/partials/home").text, "home-shelves")
    assert marker not in _shelf_row(before, "home-saved-row")

    res = client.post(f"/content/{content.id}/save")
    assert res.status_code == 200

    after = _fragment_body(client.get("/partials/home").text, "home-shelves")
    assert marker in _shelf_row(after, "home-saved-row")


def test_library_fragment_counts_follow_the_data(client, db_session):
    _seed(db_session)
    content = db_session.query(Content).filter(Content.video_id == "partnew0001").first()

    before = _fragment_body(client.get("/partials/library").text, "library-grid")
    assert "1 video</span>" in before  # one saved item

    client.post(f"/content/{content.id}/save")

    after = _fragment_body(client.get("/partials/library").text, "library-grid")
    assert "2 videos</span>" in after


def test_an_artists_library_card_opens_their_profile(client, db_session):
    """The card stands for the artist, so it opens the artist — albums,
    singles, what they just released — rather than the track list of
    whatever has synced so far. Nothing in home/library.js decides this: the
    card already carries which kind it opens."""
    _seed(db_session)
    feed = db_session.query(Feed).first()
    feed.artist_browse_id = "UC5ZkRnYd3__WBBGnAnWO9Cg"
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert 'data-detail-kind="yt-artist"' in body
    assert 'data-detail-id="UC5ZkRnYd3__WBBGnAnWO9Cg"' in body
    assert 'href="/#yt-artist/UC5ZkRnYd3__WBBGnAnWO9Cg"' in body


def test_a_plain_channels_card_still_opens_its_track_list(client, db_session):
    _seed(db_session)

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert 'data-detail-kind="channel"' in body
    assert "yt-artist" not in body


def test_downloads_fragment_reports_stored_sizes(client, db_session):
    """The modal's own list (full collect_usage) and the Settings summary
    line (the cheaper usage_summary) are two separate fragments now — see
    page_context.storage_summary_context — but they still have to agree,
    same as before the split."""
    _seed(db_session)

    downloads_fragment = client.get("/partials/downloads").text
    summary_fragment = client.get("/partials/storage-summary").text

    assert "3.0 MB" in _fragment_body(downloads_fragment, "downloads-body")
    assert _fragment_body(summary_fragment, "settings-storage-desc") == "3.0 MB across 1 item"


def test_fragments_are_empty_but_valid_for_a_fresh_profile(client):
    """No seeded content at all — the empty branches still have to render,
    since that's what a refresh after clearing everything produces."""
    for url, targets in FRAGMENTS:
        res = client.get(url)
        assert res.status_code == 200, url
        for target in targets:
            assert f'data-target="{target}"' in res.text

    assert "add a channel in the Explore tab" in client.get("/partials/home").text
    assert "Nothing downloaded yet" in client.get("/partials/downloads").text


def test_fragments_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for url, _targets in FRAGMENTS:
            res = anonymous.get(url, follow_redirects=False)
            assert res.status_code == 303, url
            assert res.headers["location"] == "/login", url


def test_channel_detail_fragment(client, db_session):
    """The channel/playlist detail panel (home/detail.js) isn't SSR'd inside
    index.html at all — unlike FRAGMENTS above, there's no full-page render
    to compare it against, so this asserts content directly, the same way
    the old standalone channel.html page's test did before it moved here."""
    feed = _seed(db_session)

    res = client.get(f"/partials/detail/channel/{feed.id}")

    assert res.status_code == 200
    body = _fragment_body(res.text, "detail-panel")
    assert "Partial Channel" in body
    assert "Fresh Upload" in body
    assert "3 videos" in body


def test_channel_detail_fragment_404s_for_an_unknown_feed(client):
    assert client.get("/partials/detail/channel/9999").status_code == 404


def test_playlist_detail_fragments(client, db_session):
    _seed(db_session)

    for kind, expected, unexpected in [
        ("favorites", "A Favorite", "Fresh Upload"),
        ("saved", "Saved And Played", "A Favorite"),
        ("new-uploads", "Fresh Upload", "A Favorite"),
        ("recently-played", "Saved And Played", "Fresh Upload"),
    ]:
        res = client.get(f"/partials/detail/playlist/{kind}")
        assert res.status_code == 200, kind
        body = _fragment_body(res.text, "detail-panel")
        assert expected in body, kind
        assert unexpected not in body, kind


def test_detail_fragments_carry_the_play_all_controls(client, db_session):
    feed = _seed(db_session)

    for url in [f"/partials/detail/channel/{feed.id}", "/partials/detail/playlist/favorites"]:
        body = _fragment_body(client.get(url).text, "detail-panel")
        assert 'id="detail-play-all"' in body, url
        assert 'id="detail-shuffle"' in body, url


def test_empty_playlist_detail_fragments_render_their_empty_message(client):
    for kind, message in [
        ("favorites", "No favorites yet."),
        ("saved", "Nothing saved yet."),
        ("new-uploads", "Nothing new yet."),
        ("recently-played", "Nothing played yet."),
    ]:
        res = client.get(f"/partials/detail/playlist/{kind}")
        assert res.status_code == 200, kind
        assert message in res.text, kind
        # Nothing to play — a Play button on an empty list is a control that
        # can only ever do nothing.
        assert 'id="detail-play-all"' not in res.text, kind


def test_playlist_detail_fragment_404s_for_an_unknown_kind(client):
    assert client.get("/partials/detail/playlist/bogus").status_code == 404


def test_detail_pagination(client, db_session):
    """DEFAULT_PAGE_SIZE=50 — same threshold as the fragment/page tests above."""
    feed = _seed(db_session)
    db_session.add_all(
        [
            Content(
                feed_id=feed.id, user_id=USER_ID, video_id=f"bulkvid{i:04d}"[:11], title=f"Bulk {i}",
                published_at=datetime(2025, 6, 1) - timedelta(days=i), is_favorite=True,
            )
            for i in range(55)
        ]
    )
    db_session.commit()

    first_page = client.get("/partials/detail/playlist/favorites")
    assert 'aria-label="Pagination, page 1 of 2"' in first_page.text
    assert 'is-current" aria-current="page">1</span>' in first_page.text
    assert 'page=2">2</a>' in first_page.text

    second_page = client.get("/partials/detail/playlist/favorites?page=2")
    assert second_page.status_code == 200
    assert 'aria-label="Pagination, page 2 of 2"' in second_page.text
    assert 'is-current" aria-current="page">2</span>' in second_page.text


def test_pagination_numbered_links_are_windowed_around_the_current_page(client, db_session):
    """A channel/playlist with a few thousand videos runs past a thousand
    pages at DEFAULT_PAGE_SIZE — showing every page number would be its own
    scroll-forever problem, so only current ± 2 plus the first/last page (with
    an ellipsis for the gap) actually render as links. _seed() itself adds
    one favorite ("partfav0001"), so 499 more makes an even 500 — exactly 10
    pages at DEFAULT_PAGE_SIZE=50."""
    feed = _seed(db_session)
    db_session.add_all(
        [
            Content(
                feed_id=feed.id, user_id=USER_ID, video_id=f"windowvi{i:03d}"[:11], title=f"Window {i}",
                published_at=datetime(2025, 6, 1) - timedelta(days=i), is_favorite=True,
            )
            for i in range(499)
        ]
    )
    db_session.commit()

    res = client.get("/partials/detail/playlist/favorites?page=5")
    assert res.status_code == 200
    assert 'aria-label="Pagination, page 5 of 10"' in res.text
    # Window is 3-7 (current ± 2): 3, 4, [5], 6, 7 all render as links/current.
    for page in (3, 4, 6, 7):
        assert f'page={page}">{page}</a>' in res.text
    assert 'is-current" aria-current="page">5</span>' in res.text
    # First (1) and last (10) page always render, each with its own ellipsis
    # since there's a real gap on both sides of the 3-7 window.
    assert 'page=1">1</a>' in res.text
    assert 'page=10">10</a>' in res.text
    assert res.text.count('class="pagination-ellipsis"') == 2
    # Page 2 and page 8 are inside neither the window nor the first/last
    # pins, so they must not appear as their own link.
    assert 'page=2">2</a>' not in res.text
    assert 'page=8">8</a>' not in res.text


def test_detail_fragments_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for url in ["/partials/detail/channel/1", "/partials/detail/playlist/favorites"]:
            res = anonymous.get(url, follow_redirects=False)
            assert res.status_code == 303, url
            assert res.headers["location"] == "/login", url


def test_detail_fragments_are_scoped_to_the_current_profile(client, db_session):
    _seed(db_session)
    other = _other_user_feed(db_session)
    db_session.add(
        Content(
            feed_id=other.id,
            user_id=other.user_id,
            video_id="otherprof02",
            title="Not Yours",
            is_favorite=True,
        )
    )
    db_session.commit()

    # Another profile's channel isn't just filtered out of the response —
    # it's a 404, same as any other feed_id that isn't this user's.
    assert client.get(f"/partials/detail/channel/{other.id}").status_code == 404
    assert "Not Yours" not in client.get("/partials/detail/playlist/favorites").text


def test_fragments_are_scoped_to_the_current_profile(client, db_session):
    """Another profile's content must never leak into a fragment — the
    fragment endpoints resolve the profile the same way the page does."""
    _seed(db_session)
    other = _other_user_feed(db_session)
    db_session.add(
        Content(
            feed_id=other.id,
            user_id=other.user_id,
            video_id="otherprof01",
            title="Not Yours",
            is_favorite=True,
            is_saved=True,
            last_played_at=utcnow(),
        )
    )
    db_session.commit()

    for url, _targets in FRAGMENTS:
        assert "Not Yours" not in client.get(url).text, url
        assert "Someone Else" not in client.get(url).text, url
