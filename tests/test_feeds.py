import logging
from datetime import datetime

import pytest

import app.feed_sync as feed_sync_module
import app.routers.feeds as feeds_router
import app.services.backfill as backfill_module
import app.services.feed_add as feed_add_module
from app.feed_sync import FeedFetchResult, apply_feed_data
from app.models import Content, Feed, User
from app.youtube.music import ArtistProfile
from app.youtube.rss import FeedUnavailableError, InvalidFeedError, ParsedEntry, ParsedFeed
from app.youtube.urls import channel_feed_url, longform_feed_url

USER_ID = 1


def _seed_feed_with_content(db_session, **content_kwargs):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/unfollow-me", channel_title="Unfollow Me")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    defaults = {"status": "not_downloaded"}
    defaults.update(content_kwargs)
    content = Content(
        feed_id=feed.id,
        user_id=USER_ID,
        video_id="untouched1",
        title="Untouched video",
        **defaults,
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return feed, content


def test_following_a_previously_previewed_channel_upgrades_the_placeholder(db_session, monkeypatch):
    """Regression test for the Ezhel bug: an Explore-added video creates a
    followed=False placeholder Feed; actually following that channel later
    must reuse that same row (not create a duplicate whose content insert
    would collide with the preview's Content row on the (user_id, video_id)
    unique constraint)."""
    channel_id = "UCabcdefghij"
    rss_url = channel_feed_url(channel_id)

    placeholder = Feed(user_id=USER_ID, rss_url=rss_url, channel_title="Ezhel", followed=False)
    db_session.add(placeholder)
    db_session.commit()
    db_session.refresh(placeholder)

    db_session.add(
        Content(
            feed_id=placeholder.id,
            user_id=USER_ID,
            video_id="preview0001",
            title="Ezhel - Kaybet",
            is_preview=True,
            status="ready",
        )
    )
    db_session.commit()

    parsed = ParsedFeed(channel_title="Ezhel", entries=[])
    monkeypatch.setattr(feed_add_module, "fetch_feed", lambda url: parsed)
    monkeypatch.setattr(
        feed_add_module,
        "fetch_feed_data",
        lambda feed_id, rss_url, avatar_url: FeedFetchResult(
            parsed=parsed, durations={}, channel_id=channel_id, avatar_url=None
        ),
    )

    feed, _new_count, resolved_channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, rss_url, USER_ID
    )

    assert resolved_channel_id == channel_id
    assert feed.id == placeholder.id
    assert feed.followed is True

    all_feeds = db_session.query(Feed).filter(Feed.user_id == USER_ID).all()
    assert len(all_feeds) == 1


def test_apply_feed_data_skips_video_already_owned_by_another_feed(db_session):
    feed_a = Feed(user_id=USER_ID, rss_url="https://example.com/a", channel_title="A")
    feed_b = Feed(user_id=USER_ID, rss_url="https://example.com/b", channel_title="B")
    db_session.add_all([feed_a, feed_b])
    db_session.commit()
    db_session.refresh(feed_a)
    db_session.refresh(feed_b)

    db_session.add(
        Content(feed_id=feed_a.id, user_id=USER_ID, video_id="shared0001", title="Shared video")
    )
    db_session.commit()

    parsed = ParsedFeed(
        channel_title="B",
        entries=[
            ParsedEntry(video_id="shared0001", title="Shared video", thumbnail_url=None, published_at=None)
        ],
    )
    result = FeedFetchResult(parsed=parsed, durations={}, channel_id=None, avatar_url=None)

    new_count = apply_feed_data(db_session, feed_b, result)

    assert new_count == 0
    rows = (
        db_session.query(Content)
        .filter(Content.user_id == USER_ID, Content.video_id == "shared0001")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].feed_id == feed_a.id


def test_apply_feed_data_marks_new_rows_as_new_upload(db_session):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/new-upload-feed", channel_title="C")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    parsed = ParsedFeed(
        channel_title="C",
        entries=[ParsedEntry(video_id="freshvid01", title="Fresh video", thumbnail_url=None, published_at=None)],
    )
    result = FeedFetchResult(parsed=parsed, durations={}, channel_id=None, avatar_url=None)

    apply_feed_data(db_session, feed, result)

    row = db_session.query(Content).filter(Content.video_id == "freshvid01").first()
    assert row.is_new_upload is True


def test_apply_feed_data_skips_likely_shorts(db_session):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/shorts-feed", channel_title="C")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    parsed = ParsedFeed(
        channel_title="C",
        entries=[
            ParsedEntry(video_id="shortvid01", title="A Short", thumbnail_url=None, published_at=None),
            ParsedEntry(video_id="longvid001", title="A regular video", thumbnail_url=None, published_at=None),
        ],
    )
    result = FeedFetchResult(
        parsed=parsed, durations={"shortvid01": 45, "longvid001": 300}, channel_id=None, avatar_url=None
    )

    new_count = apply_feed_data(db_session, feed, result)

    assert new_count == 1
    remaining = {row.video_id for row in db_session.query(Content).filter(Content.feed_id == feed.id)}
    assert remaining == {"longvid001"}


def test_apply_feed_data_remarks_existing_row_still_in_feed_as_new_upload(db_session):
    """A video already in the DB (e.g. from a past backfill) that's still
    part of the channel's current RSS window should get picked up as a new
    upload too — this is what lets New Uploads self-heal/populate on the
    next refresh instead of staying permanently empty for pre-existing rows."""
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/remark-feed", channel_title="C")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    db_session.add(
        Content(
            feed_id=feed.id, user_id=USER_ID, video_id="existingv01", title="Old row",
            duration_seconds=300, is_new_upload=False,
        )
    )
    db_session.commit()

    parsed = ParsedFeed(
        channel_title="C",
        entries=[ParsedEntry(video_id="existingv01", title="Old row", thumbnail_url=None, published_at=None)],
    )
    result = FeedFetchResult(parsed=parsed, durations={}, channel_id=None, avatar_url=None)

    new_count = apply_feed_data(db_session, feed, result)

    assert new_count == 0  # not a new row
    row = db_session.query(Content).filter(Content.video_id == "existingv01").first()
    assert row.is_new_upload is True


def test_refresh_feeds_isolates_one_failing_feed(db_session, monkeypatch):
    feed_fails = Feed(user_id=USER_ID, rss_url="https://example.com/fails", channel_title="Fails")
    feed_ok = Feed(user_id=USER_ID, rss_url="https://example.com/ok", channel_title="Ok")
    db_session.add_all([feed_fails, feed_ok])
    db_session.commit()
    db_session.refresh(feed_fails)
    db_session.refresh(feed_ok)

    monkeypatch.setattr(
        feed_sync_module,
        "fetch_feed_data",
        lambda feed_id, rss_url, avatar_url: FeedFetchResult(parsed=None, durations={}, channel_id=None),
    )

    def fake_apply_feed_data(db, feed, result):
        if feed.id == feed_fails.id:
            raise RuntimeError("simulated failure")
        db.add(Content(feed_id=feed.id, user_id=feed.user_id, video_id="ok00000001", title="Ok video"))
        db.commit()
        return 1

    monkeypatch.setattr(feed_sync_module, "apply_feed_data", fake_apply_feed_data)

    new_count = feed_sync_module.refresh_feeds(db_session, [feed_fails, feed_ok])

    assert new_count == 1
    saved = db_session.query(Content).filter(Content.feed_id == feed_ok.id).all()
    assert len(saved) == 1


def test_a_failing_feed_fetch_is_logged_rather_than_swallowed(db_session, monkeypatch, caplog):
    """fetch_feed_data turns any FeedError into "no new content", which is the
    right behaviour for a refresh spanning every channel — but it used to leave
    no trace at all. A YouTube 429, which hits every feed at once, produced a
    refresh that reported zero new uploads and said nothing about why."""
    monkeypatch.setattr(
        feed_sync_module,
        "fetch_feed",
        lambda url: (_ for _ in ()).throw(FeedUnavailableError("Could not fetch RSS feed: 429")),
    )

    with caplog.at_level(logging.WARNING, logger="app.feed_sync"):
        result = feed_sync_module.fetch_feed_data(7, channel_feed_url("UCabcdefghij"), None)

    assert result.parsed is None
    assert "429" in caplog.text
    assert "7" in caplog.text


def test_adding_a_feed_youtube_would_not_serve_is_not_a_bad_request(client, monkeypatch):
    """A 400 tells the user their URL is wrong. When YouTube rate-limits us or
    the network is down, that is both false and unactionable — the whole reason
    rss.py splits FeedUnavailableError out of InvalidFeedError."""
    monkeypatch.setattr(
        feeds_router,
        "add_feed_core",
        lambda db, url, user_id, artist_browse_id=None, sync=True: (_ for _ in ()).throw(
            FeedUnavailableError("Could not fetch RSS feed: HTTP Error 429")
        ),
    )

    res = client.post("/feeds", json={"channel_url": "https://www.youtube.com/@handle"})

    assert res.status_code == 502
    assert "429" in res.json()["detail"]


def test_adding_a_feed_that_really_is_the_wrong_url_is_still_a_bad_request(client, monkeypatch):
    monkeypatch.setattr(
        feeds_router,
        "add_feed_core",
        lambda db, url, user_id, artist_browse_id=None, sync=True: (_ for _ in ()).throw(
            InvalidFeedError("URL is not a valid YouTube channel RSS feed")
        ),
    )

    res = client.post("/feeds", json={"channel_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})

    assert res.status_code == 400


def test_unfollowing_a_channel_with_no_engaged_content_deletes_it_entirely(client, db_session):
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
    kept_content = db_session.query(Content).filter(Content.id == content.id).first()
    assert kept_content is not None


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


def test_a_short_is_removed_when_its_duration_arrives_later(db_session):
    """The Shorts guard has to apply to a backfilled duration, not just a new
    insert.

    fetch_channel_video_durations only covers a channel's first 50 videos, so
    anything further down the RSS window is inserted with no duration at all
    and passes the guard by default. When the duration does turn up on a later
    refresh, nothing re-checked it: the row kept its place in the library for
    good, and only the is_new_upload loop below it noticed the length.
    """
    feed = Feed(user_id=1, rss_url="https://example.com/late-duration", channel_title="C")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    # Already present, still unmeasured — exactly what the capped duration
    # lookup leaves behind.
    db_session.add_all(
        [
            Content(feed_id=feed.id, user_id=1, video_id="latedur0001", title="Actually a Short",
                    duration_seconds=None),
            Content(feed_id=feed.id, user_id=1, video_id="latedur0002", title="Actually a video",
                    duration_seconds=None),
        ]
    )
    db_session.commit()

    parsed = ParsedFeed(
        channel_title="C",
        entries=[
            ParsedEntry(video_id="latedur0001", title="Actually a Short", thumbnail_url=None, published_at=None),
            ParsedEntry(video_id="latedur0002", title="Actually a video", thumbnail_url=None, published_at=None),
        ],
    )
    result = FeedFetchResult(
        parsed=parsed,
        durations={"latedur0001": 30, "latedur0002": 420},
        channel_id=None,
        avatar_url=None,
    )

    apply_feed_data(db_session, feed, result)

    remaining = {row.video_id: row for row in db_session.query(Content).filter(Content.feed_id == feed.id)}
    assert set(remaining) == {"latedur0002"}
    assert remaining["latedur0002"].duration_seconds == 420


# -------------------------------------------------------- GET /feeds/backfilling


def test_backfilling_lists_only_this_users_running_syncs(client, db_session):
    """What Library's "Fetching uploads…" cards poll on.

    A newly followed artist's card appears the moment POST /feeds answers,
    before anything has been fetched, so the grid has to be able to ask what
    is still running — and it has to be scoped: another user's sync is
    none of this one's business.
    """
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

        # A finished scan keeps its registry entry readable for a while (see
        # progress.py), so "has an entry" is not "is running" — a card left
        # saying "fetching" forever is exactly what confusing the two causes.
        backfill_module.backfill_progress.set(mine.id, ("done", 0, 0))
        assert client.get("/feeds/backfilling").json() == []
    finally:
        backfill_module.backfill_progress.discard(mine.id)
        backfill_module.backfill_progress.discard(theirs.id)


def test_library_marks_a_feed_that_is_still_being_fetched(client, db_session):
    """The reason nothing waits for the first sync: the wait moved onto the
    card it belongs to, where it can be ignored."""
    feed = Feed(
        user_id=USER_ID, rss_url="https://example.com/preparing", channel_title="Still Filling In"
    )
    db_session.add(feed)
    db_session.commit()

    backfill_module.backfill_progress.set(feed.id, ("syncing", 0, 0))
    try:
        body = client.get("/partials/library").text
        assert 'data-preparing="true"' in body
        assert "Fetching uploads" in body
    finally:
        backfill_module.backfill_progress.discard(feed.id)

    body = client.get("/partials/library").text
    assert "data-preparing" not in body, "the card kept saying it was fetching after the sync ended"


# --------------------------------------------------------------------------
# Following an artist. A different thing from following a channel: the feed
# points at their "<Artist> - Topic" channel, carries their browse id, and
# skips the history scan.
# --------------------------------------------------------------------------

TOPIC_ID = "UCDdTH-sn8qG64wK5ChFDQ4Q"
ARTIST_BROWSE_ID = "UC5ZkRnYd3__WBBGnAnWO9Cg"


def _stub_topic_feed(monkeypatch, title="Shirin David - Topic"):
    parsed = ParsedFeed(channel_title=title, entries=[])
    monkeypatch.setattr(feed_add_module, "fetch_feed", lambda url: parsed)
    monkeypatch.setattr(
        feed_add_module,
        "fetch_feed_data",
        lambda feed_id, rss_url, avatar_url: FeedFetchResult(
            parsed=parsed, durations={}, channel_id=TOPIC_ID, avatar_url=None
        ),
    )


def test_following_an_artist_records_which_artist_it_is(db_session, monkeypatch):
    """The column is both the flag and the address: it says this feed is an
    artist's, and it's the id that reopens their profile."""
    _stub_topic_feed(monkeypatch)

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(TOPIC_ID), USER_ID, artist_browse_id=ARTIST_BROWSE_ID
    )

    assert feed.artist_browse_id == ARTIST_BROWSE_ID


def test_an_artists_card_is_not_titled_topic(db_session, monkeypatch):
    """The feed points at "Shirin David - Topic" and that is genuinely its
    title, but the library card is standing for the artist."""
    _stub_topic_feed(monkeypatch)

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(TOPIC_ID), USER_ID, artist_browse_id=ARTIST_BROWSE_ID
    )

    assert feed.channel_title == "Shirin David"


def test_following_a_plain_channel_keeps_its_name_and_stays_unmarked(db_session, monkeypatch):
    """The suffix strip and the marking are artist-follow behaviour, not
    something every add now does — a channel that really is called
    "… - Topic" and was added by URL keeps its name."""
    _stub_topic_feed(monkeypatch)

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(TOPIC_ID), USER_ID
    )

    assert feed.channel_title == "Shirin David - Topic"
    assert feed.artist_browse_id is None


def _syncing_feed(db_session, **kwargs):
    kwargs.setdefault("channel_title", "A Channel")
    feed = Feed(user_id=USER_ID, rss_url=channel_feed_url(TOPIC_ID), followed=True, **kwargs)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    return feed


def _stub_initial_fetch(monkeypatch, spy=None):
    """The network half of run_initial_sync. parsed=None is what
    fetch_feed_data returns for an unreadable feed, and apply_feed_data
    treats it as "no new content" — which is all these need."""

    def fake_fetch_feed_data(feed_id, rss_url, avatar_url):
        if spy is not None:
            spy()
        return FeedFetchResult(parsed=None, durations={}, channel_id=None, avatar_url=None)

    monkeypatch.setattr(backfill_module, "fetch_feed_data", fake_fetch_feed_data)


def test_a_new_feed_says_it_is_filling_in_before_it_fetches_anything(db_session, monkeypatch):
    """The reason the sync could move off the request at all. Library renders
    a card the moment POST /feeds answers, and without this it would render
    a confident "0 videos" for the two seconds the fetch takes — which reads
    as a channel that failed to add, not one still arriving."""
    seen: list[set[int]] = []
    feed = _syncing_feed(db_session)
    _stub_initial_fetch(monkeypatch, spy=lambda: seen.append(backfill_module.backfilling_feed_ids([feed.id])))

    backfill_module.run_initial_sync(feed.id, db_session)

    assert seen == [{feed.id}]


def test_a_failed_initial_sync_does_not_leave_the_card_stuck(db_session, monkeypatch):
    """"Fetching uploads…" is a phase, not a flag — a crash that never
    cleared it would leave the card spinning for the rest of the process's
    life."""
    feed = _syncing_feed(db_session)
    monkeypatch.setattr(
        backfill_module,
        "fetch_feed_data",
        lambda feed_id, rss_url, avatar_url: (_ for _ in ()).throw(RuntimeError("disk full")),
    )

    backfill_module.run_initial_sync(feed.id, db_session)

    assert backfill_module.backfilling_feed_ids([feed.id]) == set()


def test_adding_a_feed_answers_before_it_fetches_the_content(client, monkeypatch):
    """POST /feeds used to run the whole sync inline — 1.32s of yt-dlp for
    the durations and 0.84s for the avatar, measured, per channel — which is
    what the onboarding wizard's Finish button was really waiting on."""
    scheduled: list[int] = []
    fetched: list[int] = []
    monkeypatch.setattr(
        feed_add_module, "fetch_feed", lambda url: ParsedFeed(channel_title="A Channel", entries=[])
    )
    monkeypatch.setattr(
        feed_add_module,
        "fetch_feed_data",
        lambda feed_id, rss_url, avatar_url: fetched.append(feed_id),
    )
    monkeypatch.setattr(
        feeds_router,
        "run_initial_sync_task",
        lambda feed_id: scheduled.append(feed_id),
    )

    res = client.post("/feeds", json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}"})

    assert res.status_code == 201
    assert fetched == []
    assert scheduled == [res.json()["feed"]["id"]]


# --------------------------------------------------------------------------
# The server recognising an artist by itself (see feed_add._as_artist_follow).
# Above this line, "follow the Topic channel" only happened when the client
# said so — which meant only the detail panel's Follow button, since it is
# the only surface that opens an artist's profile first. The onboarding
# wizard, Explore's Add button and bulk import all followed the channel the
# artist also vlogs on. These are about the answer no longer depending on
# which button was pressed.
# --------------------------------------------------------------------------

OFFICIAL_ID = "UC5ZkRnYd3__WBBGnAnWO9Cg"


def _artist(*, topic_channel_id=TOPIC_ID, browse_id=OFFICIAL_ID):
    return ArtistProfile(
        browse_id=browse_id,
        channel_id=browse_id,
        topic_channel_id=topic_channel_id,
        name="Shirin David",
        description=None,
        subscriber_count=None,
        monthly_listeners=None,
        avatar_url=None,
        tracks=[],
    )


def _stub_artist_lookup(monkeypatch, profile, calls=None):
    """Overrides conftest's _no_artist_lookup, which answers "not an artist"
    for the whole suite."""

    def fake_fetch_artist(browse_id, all_songs=True):
        if calls is not None:
            calls.append((browse_id, all_songs))
        return profile

    monkeypatch.setattr(feed_add_module, "fetch_artist", fake_fetch_artist)


def test_following_a_musicians_own_channel_follows_their_topic_channel(db_session, monkeypatch):
    """The whole point: nothing in this call says "artist" — it is the plain
    channel URL an Add button sends — and it still ends up on the channel
    that carries their releases instead of their vlogs."""
    _stub_topic_feed(monkeypatch)
    _stub_artist_lookup(monkeypatch, _artist())

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(OFFICIAL_ID), USER_ID
    )

    assert feed.rss_url == channel_feed_url(TOPIC_ID)


def test_a_server_recognised_artist_is_marked_and_titled_like_one(db_session, monkeypatch):
    """Same two things the client-answered path does — the id that reopens
    the profile, and a card that reads as the artist rather than "… - Topic"."""
    _stub_topic_feed(monkeypatch)
    _stub_artist_lookup(monkeypatch, _artist())

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(OFFICIAL_ID), USER_ID
    )

    assert feed.artist_browse_id == OFFICIAL_ID
    assert feed.channel_title == "Shirin David"


def test_a_channel_that_is_not_a_musicians_is_followed_as_itself(db_session, monkeypatch):
    """What every podcast and tech channel does. YouTube Music is asked and
    says no (its parser cannot read a non-artist page — measured), and the
    follow proceeds exactly as it did before any of this existed."""
    calls: list[tuple[str, bool]] = []
    _stub_topic_feed(monkeypatch, title="Some Tech Channel")
    _stub_artist_lookup(monkeypatch, None, calls)

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(OFFICIAL_ID), USER_ID
    )

    assert feed.rss_url == channel_feed_url(OFFICIAL_ID)
    assert feed.artist_browse_id is None
    assert feed.channel_title == "Some Tech Channel"
    # all_songs=False: the songs themselves are a second request, and this
    # only ever wanted the ids off the page header.
    assert calls == [(OFFICIAL_ID, False)]


def test_an_artist_with_no_topic_channel_is_followed_as_a_channel(db_session, monkeypatch):
    """There is nothing better to follow: the Topic id is read off the
    artist's own tracks, so an artist page carrying none leaves this with
    only the channel it started with."""
    _stub_topic_feed(monkeypatch, title="Some Artist")
    _stub_artist_lookup(monkeypatch, _artist(topic_channel_id=None))

    feed, _new_count, _channel_id = feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(OFFICIAL_ID), USER_ID
    )

    assert feed.rss_url == channel_feed_url(OFFICIAL_ID)
    assert feed.artist_browse_id is None


def test_a_caller_that_already_knows_is_not_asked_again(db_session, monkeypatch):
    """The detail panel read the id off the profile it is already showing.
    Asking YouTube Music to work out what the request just said is a wasted
    live request on the one path that never needed it."""
    calls: list[tuple[str, bool]] = []
    _stub_topic_feed(monkeypatch)
    _stub_artist_lookup(monkeypatch, _artist(), calls)

    feed_add_module.create_feed_from_rss_url(
        db_session, channel_feed_url(TOPIC_ID), USER_ID, artist_browse_id=OFFICIAL_ID
    )

    assert calls == []


def test_a_playlist_feed_is_never_offered_to_youtube_music(db_session, monkeypatch):
    """A UULF playlist feed (see urls.longform_feed_url) carries no channel
    id, and there is nothing YouTube Music could answer for it."""
    calls: list[tuple[str, bool]] = []
    _stub_topic_feed(monkeypatch, title="Videos Tab")
    _stub_artist_lookup(monkeypatch, _artist(), calls)

    feed_add_module.create_feed_from_rss_url(
        db_session, longform_feed_url(OFFICIAL_ID), USER_ID
    )

    assert calls == []


def test_following_the_channel_of_an_artist_already_followed_is_a_duplicate(
    db_session, monkeypatch
):
    """Resolution runs before the duplicate check for this: the official
    channel and the Topic channel are two URLs for one card, and the check
    can only see that once both have been reduced to the same one."""
    _stub_topic_feed(monkeypatch)
    _stub_artist_lookup(monkeypatch, _artist())
    db_session.add(
        Feed(
            user_id=USER_ID,
            rss_url=channel_feed_url(TOPIC_ID),
            channel_title="Shirin David",
            followed=True,
        )
    )
    db_session.commit()

    with pytest.raises(feed_add_module.FeedAlreadyExistsError):
        feed_add_module.create_feed_from_rss_url(
            db_session, channel_feed_url(OFFICIAL_ID), USER_ID
        )


def test_adding_an_artist_by_plain_channel_url_lands_as_an_artist_end_to_end(
    client, db_session, monkeypatch
):
    """The onboarding wizard's Add button, in full: it sends a channel URL
    and nothing else (it never opens the profile — a full-panel navigation
    out from under its modal would strand the wizard half-finished), and the
    feed it gets back is the artist's. What happens to that feed afterwards
    is run_initial_sync's, tested above."""
    _stub_topic_feed(monkeypatch)
    _stub_artist_lookup(monkeypatch, _artist())
    monkeypatch.setattr(feeds_router, "run_initial_sync_task", lambda feed_id: None)

    res = client.post(
        "/feeds", json={"channel_url": f"https://www.youtube.com/channel/{OFFICIAL_ID}"}
    )

    assert res.status_code == 201
    feed = db_session.query(Feed).filter(Feed.id == res.json()["feed"]["id"]).one()
    assert feed.rss_url == channel_feed_url(TOPIC_ID)
    assert feed.artist_browse_id == OFFICIAL_ID


def test_the_card_is_already_filling_in_when_the_response_lands(client, monkeypatch):
    """The background task cannot be relied on to have started by the time
    the client comes back for fragments, and losing that race renders a
    confident "0 videos" that then never polls — Library only polls while a
    preparing card is on the page (see home/library.js)."""
    monkeypatch.setattr(
        feed_add_module, "fetch_feed", lambda url: ParsedFeed(channel_title="A Channel", entries=[])
    )
    # Never runs, standing in for a background task that hasn't started yet.
    monkeypatch.setattr(feeds_router, "run_initial_sync_task", lambda feed_id: None)

    res = client.post("/feeds", json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}"})

    feed_id = res.json()["feed"]["id"]
    assert backfill_module.backfilling_feed_ids([feed_id]) == {feed_id}
