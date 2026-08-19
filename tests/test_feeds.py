"""Following an artist, syncing what they release, and unfollowing.

The sync used to read the artist's "<Artist> - Topic" channel over RSS. It
now diffs YouTube Music's own release list against a stored snapshot (see
services/artist_sync.py), which is what most of these pin: a first sync
records without importing, a later one imports exactly what appeared, and a
release that fails to open is left out of the snapshot so the next refresh
tries it again.

Every network call is monkeypatched out; nothing here goes online.
"""

import logging
from datetime import datetime

import pytest

import app.routers.feeds as feeds_router
import app.services.artist_sync as artist_sync
import app.services.backfill as backfill_module
import app.services.feed_add as feed_add_module
from app.models import Content, Feed, User
from app.services.artist_sync import ArtistFetchResult, apply_artist_data
from app.services.feed_add import NotAnArtistError, create_feed_from_rss_url
from app.youtube.models import VideoSearchResult
from app.youtube.music import ArtistProfile, ArtistRelease, ReleaseDetail
from app.youtube.urls import channel_feed_url

USER_ID = 1

TOPIC_ID = "UCDdTH-sn8qG64wK5ChFDQ4Q"
OFFICIAL_ID = "UC5ZkRnYd3__WBBGnAnWO9Cg"


def _release(browse_id="MPREb_aaaaaaaaaaa", title="A Single", year="2026"):
    return ArtistRelease(
        browse_id=browse_id, title=title, year=year, kind="Single", cover_url=None
    )


def _artist(*, topic_channel_id=TOPIC_ID, browse_id=OFFICIAL_ID, albums=(), singles=()):
    return ArtistProfile(
        browse_id=browse_id,
        channel_id=OFFICIAL_ID,
        topic_channel_id=topic_channel_id,
        name="Shirin David",
        description=None,
        subscriber_count=1_000_000,
        monthly_listeners=None,
        avatar_url=None,
        tracks=[],
        track_count=0,
        albums=list(albums),
        singles=list(singles),
    )


def _track(video_id="trackaaaaaa", title="A Track"):
    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=None,
        duration_seconds=200,
        channel_title="Shirin David",
        channel_id=TOPIC_ID,
    )


def _stub_artist_lookup(monkeypatch, profile, calls=None):
    def fake(browse_id, all_songs=True):
        if calls is not None:
            calls.append((browse_id, all_songs))
        return profile

    monkeypatch.setattr(feed_add_module, "fetch_artist", fake)


# --------------------------------------------------------------------------
# Following. The library holds artists, so the one thing a follow can fail on
# is the channel not being one.
# --------------------------------------------------------------------------


def test_following_a_musicians_own_channel_keys_on_their_topic_channel(db_session, monkeypatch):
    """Their official channel and their Topic channel have to resolve to one
    row, or following the same artist twice makes two."""
    _stub_artist_lookup(monkeypatch, _artist())

    feed, _ = create_feed_from_rss_url(
        db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
    )

    assert feed.rss_url == channel_feed_url(TOPIC_ID)
    assert feed.artist_browse_id == OFFICIAL_ID
    assert feed.channel_title == "Shirin David"


def test_the_card_is_titled_with_the_artists_name(db_session, monkeypatch):
    """Not the Topic channel's own title, which would read "Shirin David -
    Topic". The name comes off the artist page now, so there is no suffix to
    strip in the first place."""
    _stub_artist_lookup(monkeypatch, _artist())

    feed, _ = create_feed_from_rss_url(
        db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
    )

    assert feed.channel_title == "Shirin David"


def test_a_channel_that_is_not_an_artist_cannot_be_followed(db_session, monkeypatch):
    """The music-only scope in one rule. YouTube Music answers a non-artist
    with a page its parser can't read, which fetch_artist flattens to None."""
    _stub_artist_lookup(monkeypatch, None)

    with pytest.raises(NotAnArtistError):
        create_feed_from_rss_url(
            db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
        )

    assert db_session.query(Feed).count() == 0


def test_a_url_with_no_channel_in_it_never_reaches_youtube_music(db_session, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not ask YouTube Music about a URL with no channel id")

    monkeypatch.setattr(feed_add_module, "fetch_artist", explode)

    with pytest.raises(NotAnArtistError):
        create_feed_from_rss_url(db_session, "https://www.youtube.com/playlist?list=PLx", USER_ID, sync=False)


def test_a_non_artist_channel_is_a_400_not_a_500(client, monkeypatch):
    monkeypatch.setattr(feed_add_module, "fetch_artist", lambda browse_id, all_songs=True: None)

    res = client.post("/feeds", json={"channel_url": f"https://www.youtube.com/channel/{OFFICIAL_ID}"})

    assert res.status_code == 400


def test_following_the_channel_of_an_artist_already_followed_is_a_duplicate(db_session, monkeypatch, client):
    """Both ids reduce to the same feed, so the second follow has to be
    caught — and it can only be caught *after* the artist resolution, which
    is why that runs before the duplicate check."""
    _stub_artist_lookup(monkeypatch, _artist())
    create_feed_from_rss_url(db_session, f"https://www.youtube.com/channel/{TOPIC_ID}", USER_ID, sync=False)

    monkeypatch.setattr(
        "app.services.feed_add.fetch_artist", lambda browse_id, all_songs=True: _artist()
    )
    res = client.post("/feeds", json={"channel_url": f"https://www.youtube.com/channel/{OFFICIAL_ID}"})

    assert res.status_code == 409
    assert db_session.query(Feed).count() == 1


def test_following_a_previously_previewed_artist_upgrades_the_placeholder(db_session, monkeypatch):
    """A track grabbed from Explore leaves a followed=False row behind (see
    _get_or_create_placeholder_feed). Following for real upgrades it in place
    rather than bouncing the user with "already exists"."""
    placeholder = Feed(user_id=USER_ID, rss_url=channel_feed_url(TOPIC_ID), followed=False)
    db_session.add(placeholder)
    db_session.commit()
    _stub_artist_lookup(monkeypatch, _artist())

    feed, _ = create_feed_from_rss_url(
        db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
    )

    assert feed.id == placeholder.id
    assert feed.followed is True
    assert db_session.query(Feed).count() == 1


def test_the_track_list_is_not_paid_for_on_a_follow(db_session, monkeypatch):
    """The follow needs the ids and the name off the page header. The "Top
    songs" playlist behind them is a second request nobody here reads."""
    calls = []
    _stub_artist_lookup(monkeypatch, _artist(), calls=calls)

    create_feed_from_rss_url(db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False)

    assert calls == [(OFFICIAL_ID, False)]


# --------------------------------------------------------------------------
# Syncing. What a release-snapshot diff does that an upload feed didn't.
# --------------------------------------------------------------------------


def _followed(db_session, **kwargs):
    defaults = {
        "user_id": USER_ID,
        "rss_url": channel_feed_url(TOPIC_ID),
        "channel_title": "Shirin David",
        "artist_browse_id": OFFICIAL_ID,
    }
    defaults.update(kwargs)
    feed = Feed(**defaults)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    return feed


def test_a_first_sync_records_the_catalogue_without_importing_it(db_session, monkeypatch):
    """Following means "tell me what they put out from now on". Importing
    the back catalogue would bury the thing the follow was for — and it is a
    click away on their profile anyway."""
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(singles=[_release(), _release("MPREb_bbbbbbbbbbb")]),
    )

    def explode(browse_id):
        raise AssertionError("a first sync must not open any release")

    monkeypatch.setattr(artist_sync, "fetch_release", explode)

    feed = _followed(db_session)
    result = artist_sync.fetch_artist_data(feed.artist_browse_id, feed.release_snapshot, None)
    new_count = apply_artist_data(db_session, feed, result)

    assert new_count == 0
    assert db_session.query(Content).count() == 0
    assert feed.release_snapshot == '["MPREb_aaaaaaaaaaa", "MPREb_bbbbbbbbbbb"]'


def test_a_later_sync_imports_only_what_appeared_since(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(
            singles=[_release("MPREb_new00000000"), _release("MPREb_aaaaaaaaaaa")]
        ),
    )
    opened = []

    def fake_release(browse_id):
        opened.append(browse_id)
        return ReleaseDetail(
            title="New Single", year="2026", kind="Single", cover_url=None,
            artist_names="Shirin David", tracks=[_track("newtrack001")],
        )

    monkeypatch.setattr(artist_sync, "fetch_release", fake_release)

    feed = _followed(db_session, release_snapshot='["MPREb_aaaaaaaaaaa"]')
    result = artist_sync.fetch_artist_data(feed.artist_browse_id, feed.release_snapshot, None)
    new_count = apply_artist_data(db_session, feed, result)

    assert opened == ["MPREb_new00000000"], "an already-known release was opened again"
    assert new_count == 1
    row = db_session.query(Content).one()
    assert row.video_id == "newtrack001"
    assert row.duration_seconds == 200, "the duration has to survive — RSS never carried one"
    assert row.is_new_upload is True
    assert row.published_at is not None


def test_a_release_that_will_not_open_is_retried_next_time(db_session, monkeypatch):
    """Left out of the stored snapshot rather than written off, so one bad
    response doesn't hide a release for good."""
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(singles=[_release("MPREb_broken00000")]),
    )
    monkeypatch.setattr(artist_sync, "fetch_release", lambda browse_id: None)

    feed = _followed(db_session, release_snapshot="[]")
    result = artist_sync.fetch_artist_data(feed.artist_browse_id, feed.release_snapshot, None)
    apply_artist_data(db_session, feed, result)

    assert feed.release_snapshot == "[]"


def test_a_track_already_in_the_library_is_not_inserted_twice(db_session, monkeypatch):
    """Content's (user_id, video_id) constraint is global — a collaboration
    can arrive on two followed artists' releases, and an Explore preview can
    predate the follow entirely."""
    other = _followed(db_session, rss_url="https://example.com/other", artist_browse_id="UCother")
    db_session.add(
        Content(feed_id=other.id, user_id=USER_ID, video_id="shared00001", title="Already here")
    )
    db_session.commit()

    monkeypatch.setattr(
        artist_sync, "fetch_artist", lambda browse_id, all_songs=True: _artist(singles=[_release()])
    )
    monkeypatch.setattr(
        artist_sync,
        "fetch_release",
        lambda browse_id: ReleaseDetail(
            title="Feature", year="2026", kind="Single", cover_url=None, artist_names="x",
            tracks=[_track("shared00001"), _track("brandnew001")],
        ),
    )

    feed = _followed(db_session, rss_url=channel_feed_url("UCanother0000000000000"))
    result = artist_sync.fetch_artist_data(feed.artist_browse_id, "[]", None)
    new_count = apply_artist_data(db_session, feed, result)

    assert new_count == 1
    assert db_session.query(Content).count() == 2


def test_an_unreadable_artist_page_is_a_skip_not_a_failure(db_session, monkeypatch, caplog):
    monkeypatch.setattr(artist_sync, "fetch_artist", lambda browse_id, all_songs=True: None)

    feed = _followed(db_session, release_snapshot="[]")
    with caplog.at_level(logging.WARNING):
        result = artist_sync.fetch_artist_data(feed.artist_browse_id, feed.release_snapshot, None)

    assert result.ok is False
    assert apply_artist_data(db_session, feed, result) == 0
    assert "no page to read" in caplog.text


def test_refresh_isolates_one_failing_artist(db_session, monkeypatch):
    """One artist's apply blowing up must not abort every other artist's
    refresh in the same call."""
    good = _followed(db_session, rss_url="https://example.com/good")
    bad = _followed(db_session, rss_url="https://example.com/bad", artist_browse_id="UCbad")

    monkeypatch.setattr(
        artist_sync,
        "fetch_artist_data",
        lambda browse_id, snapshot, avatar_url: ArtistFetchResult(ok=True, release_ids=[]),
    )

    real_apply = artist_sync.apply_artist_data

    def flaky(db, feed, result):
        if feed.id == bad.id:
            raise RuntimeError("boom")
        return real_apply(db, feed, result)

    monkeypatch.setattr(artist_sync, "apply_artist_data", flaky)

    artist_sync.refresh_feeds(db_session, [bad, good])

    db_session.expire_all()
    assert db_session.get(Feed, good.id).release_snapshot == "[]", "the good artist was skipped too"


def test_a_feed_with_no_artist_behind_it_is_skipped(db_session, monkeypatch):
    """Explore placeholders have no browse id and are not followed — there is
    nothing to sync from them."""
    placeholder = _followed(db_session, artist_browse_id=None, followed=False)

    def explode(*args, **kwargs):
        raise AssertionError("a placeholder feed must not be synced")

    monkeypatch.setattr(artist_sync, "fetch_artist_data", explode)

    assert artist_sync.refresh_feeds(db_session, [placeholder]) == 0


# --------------------------------------------------------------------------
# Unfollowing. Never destroys what the user actually engaged with.
# --------------------------------------------------------------------------


def _seed_feed_with_content(db_session, **content_kwargs):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/unfollow-me", channel_title="Unfollow Me")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    defaults = {"status": "not_downloaded"}
    defaults.update(content_kwargs)
    content = Content(
        feed_id=feed.id, user_id=USER_ID, video_id="untouched1", title="Untouched video", **defaults
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return feed, content


def test_unfollowing_an_artist_with_no_engaged_content_deletes_it_entirely(client, db_session):
    feed, _content = _seed_feed_with_content(db_session)

    res = client.delete(f"/feeds/{feed.id}")

    assert res.status_code == 204
    assert db_session.query(Feed).filter(Feed.id == feed.id).first() is None
    assert db_session.query(Content).filter(Content.feed_id == feed.id).count() == 0


def test_unfollowing_keeps_downloaded_content_and_downgrades_the_feed(client, db_session):
    feed, content = _seed_feed_with_content(db_session, status="ready", file_path=None)

    res = client.delete(f"/feeds/{feed.id}")
    # client's request runs on its own Session — db_session's identity map
    # otherwise keeps serving the pre-delete cached attribute values.
    db_session.expire_all()

    assert res.status_code == 204
    kept_feed = db_session.query(Feed).filter(Feed.id == feed.id).first()
    assert kept_feed is not None
    assert kept_feed.followed is False
    assert db_session.query(Content).filter(Content.id == content.id).first() is not None


def test_unfollowing_keeps_recently_played_content(client, db_session):
    feed, content = _seed_feed_with_content(db_session, last_played_at=datetime(2026, 1, 1))

    res = client.delete(f"/feeds/{feed.id}")

    assert res.status_code == 204
    assert db_session.query(Feed).filter(Feed.id == feed.id).first() is not None
    assert db_session.query(Content).filter(Content.id == content.id).first() is not None


def test_unfollowing_keeps_favorited_and_saved_content(client, db_session):
    feed, content = _seed_feed_with_content(db_session, is_favorite=True)

    res = client.delete(f"/feeds/{feed.id}")

    assert res.status_code == 204
    assert db_session.query(Feed).filter(Feed.id == feed.id).first() is not None
    assert db_session.query(Content).filter(Content.id == content.id).first() is not None


# --------------------------------------------------------------------------
# The card that appears before anything has been fetched.
# --------------------------------------------------------------------------


def _stub_initial_fetch(monkeypatch, spy=None):
    def fake(browse_id, snapshot, avatar_url):
        if spy:
            spy()
        return ArtistFetchResult(ok=True, release_ids=[])

    monkeypatch.setattr(backfill_module, "fetch_artist_data", fake)


def test_a_new_feed_says_it_is_filling_in_before_it_fetches_anything(db_session, monkeypatch):
    """Library renders a card the moment POST /feeds answers, and without
    this it would render a confident "0 songs" for as long as the fetch takes
    — which reads as an artist that failed to add, not one still arriving."""
    seen: list[set[int]] = []
    feed = _followed(db_session)
    backfill_module.mark_syncing(feed.id)
    _stub_initial_fetch(monkeypatch, spy=lambda: seen.append(backfill_module.backfilling_feed_ids([feed.id])))

    backfill_module.run_initial_sync(feed.id, db_session)

    assert seen == [{feed.id}]
    backfill_module.backfill_progress.discard(feed.id)


def test_a_failed_initial_sync_does_not_leave_the_card_stuck(db_session, monkeypatch):
    feed = _followed(db_session)

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(backfill_module, "fetch_artist_data", explode)

    backfill_module.run_initial_sync(feed.id, db_session)

    assert backfill_module.backfill_progress.get(feed.id)[0] == "done"
    backfill_module.backfill_progress.discard(feed.id)


def test_adding_a_feed_answers_before_it_fetches_anything(client, monkeypatch):
    """The response only needs the row to exist. Everything behind it is a
    background task."""
    fetched: list[str] = []
    scheduled: list[int] = []

    monkeypatch.setattr(
        feed_add_module, "fetch_artist", lambda browse_id, all_songs=True: _artist()
    )
    monkeypatch.setattr(
        artist_sync, "fetch_artist_data",
        lambda browse_id, snapshot, avatar_url: fetched.append(browse_id) or ArtistFetchResult(ok=True),
    )
    monkeypatch.setattr(feeds_router, "run_initial_sync_task", lambda feed_id: scheduled.append(feed_id))

    res = client.post("/feeds", json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}"})

    assert res.status_code == 201
    assert fetched == [], "the route fetched the catalogue before answering"
    assert scheduled == [res.json()["feed"]["id"]]


def test_the_card_is_already_filling_in_when_the_response_lands(client, monkeypatch):
    """The background task starting before the client's first fragment
    request is not guaranteed — losing that race would leave the card
    claiming zero and never polling."""
    monkeypatch.setattr(
        feed_add_module, "fetch_artist", lambda browse_id, all_songs=True: _artist()
    )
    monkeypatch.setattr(feeds_router, "run_initial_sync_task", lambda feed_id: None)

    feed_id = client.post(
        "/feeds", json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}"}
    ).json()["feed"]["id"]

    try:
        assert client.get("/feeds/backfilling").json() == [feed_id]
    finally:
        backfill_module.backfill_progress.discard(feed_id)


# -------------------------------------------------------- GET /feeds/backfilling


def test_backfilling_lists_only_this_users_running_syncs(client, db_session):
    mine = Feed(user_id=USER_ID, rss_url="https://example.com/mine", channel_title="Mine")
    other_user = User(email="someone-else@example.com", password_hash="x")
    db_session.add_all([mine, other_user])
    db_session.commit()
    theirs = Feed(
        user_id=other_user.id, rss_url="https://example.com/theirs", channel_title="Theirs"
    )
    db_session.add(theirs)
    db_session.commit()

    backfill_module.backfill_progress.set(mine.id, ("syncing", 0, 0))
    backfill_module.backfill_progress.set(theirs.id, ("syncing", 0, 0))
    try:
        assert client.get("/feeds/backfilling").json() == [mine.id]

        # A finished sync keeps its registry entry readable for a while (see
        # progress.py), so "has an entry" is not "is running" — a card left
        # saying "fetching" forever is exactly what confusing the two causes.
        backfill_module.backfill_progress.set(mine.id, ("done", 0, 0))
        assert client.get("/feeds/backfilling").json() == []
    finally:
        backfill_module.backfill_progress.discard(mine.id)
        backfill_module.backfill_progress.discard(theirs.id)


def test_library_marks_a_feed_that_is_still_being_fetched(client, db_session):
    feed = Feed(
        user_id=USER_ID, rss_url="https://example.com/preparing", channel_title="Still Filling In"
    )
    db_session.add(feed)
    db_session.commit()

    backfill_module.backfill_progress.set(feed.id, ("syncing", 0, 0))
    try:
        body = client.get("/partials/library").text
        assert 'data-preparing="true"' in body
        assert "Fetching releases" in body
    finally:
        backfill_module.backfill_progress.discard(feed.id)

    body = client.get("/partials/library").text
    assert "data-preparing" not in body, "the card kept saying it was fetching after the sync ended"
