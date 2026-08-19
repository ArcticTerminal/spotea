import logging
from datetime import datetime

import pytest

import app.feed_sync as feed_sync_module
import app.routers.feeds as feeds_router
import app.services.backfill as backfill_module
import app.services.feed_add as feed_add_module
from app.feed_sync import FeedFetchResult, apply_feed_data
from app.models import Content, Feed, User
from app.youtube.extract import BackfillEntry
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
        lambda db, url, user_id, artist_browse_id=None: (_ for _ in ()).throw(
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
        lambda db, url, user_id, artist_browse_id=None: (_ for _ in ()).throw(
            InvalidFeedError("URL is not a valid YouTube channel RSS feed")
        ),
    )

    res = client.post("/feeds", json={"channel_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"})

    assert res.status_code == 400


def test_run_backfill_marks_done_on_unexpected_failure(db_session, monkeypatch):
    channel_id = "UCfailure0001"
    feed = Feed(user_id=USER_ID, rss_url=channel_feed_url(channel_id), channel_title="Broken", followed=True)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    monkeypatch.setattr(
        backfill_module,
        "fetch_channel_all_videos",
        lambda channel_id, on_progress=None: [
            BackfillEntry(video_id="backfill01", title="T", thumbnail_url=None, duration_seconds=None)
        ],
    )

    def raise_on_commit():
        raise RuntimeError("disk full")

    monkeypatch.setattr(db_session, "commit", raise_on_commit)

    backfill_module.run_backfill(feed.id, channel_id, db_session)

    assert backfill_module.backfill_progress.get(feed.id)[0] == "done"


def test_run_backfill_does_not_mark_entries_as_new_upload(db_session, monkeypatch):
    channel_id = "UCbackfill0001"
    feed = Feed(user_id=USER_ID, rss_url=channel_feed_url(channel_id), channel_title="History", followed=True)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    monkeypatch.setattr(
        backfill_module,
        "fetch_channel_all_videos",
        lambda channel_id, on_progress=None: [
            BackfillEntry(video_id="oldvid0001", title="Old video", thumbnail_url=None, duration_seconds=None)
        ],
    )

    backfill_module.run_backfill(feed.id, channel_id, db_session)

    row = db_session.query(Content).filter(Content.video_id == "oldvid0001").first()
    assert row is not None
    assert row.is_new_upload is False


def test_run_backfill_skips_likely_shorts(db_session, monkeypatch):
    channel_id = "UCbackfillshorts"
    feed = Feed(user_id=USER_ID, rss_url=channel_feed_url(channel_id), channel_title="History", followed=True)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    monkeypatch.setattr(
        backfill_module,
        "fetch_channel_all_videos",
        lambda channel_id, on_progress=None: [
            BackfillEntry(video_id="shortvid02", title="A Short", thumbnail_url=None, duration_seconds=30),
            BackfillEntry(video_id="longvid002", title="A regular video", thumbnail_url=None, duration_seconds=600),
        ],
    )

    backfill_module.run_backfill(feed.id, channel_id, db_session)

    remaining = {row.video_id for row in db_session.query(Content).filter(Content.feed_id == feed.id)}
    assert remaining == {"longvid002"}


def test_existing_ids_check_is_scoped_to_this_channels_candidates_not_the_whole_library(db_session, monkeypatch):
    """Regression for O(channels x library): the existing-ids check used to
    query every video_id the user has, regardless of channel — bulk-
    importing 50 channels re-read the user's entire library 50 times.
    Scoped to this backfill's own candidate ids instead. An unscoped and a
    scoped query return the identical *result* here, so the only thing that
    actually distinguishes them is how much they read — verified by
    capturing the real SQL bind parameters rather than the return value."""
    from sqlalchemy import event

    from app.database import engine

    channel_id = "UCbackfillscope"
    feed = Feed(user_id=USER_ID, rss_url=channel_feed_url(channel_id), channel_title="Scope Test", followed=True)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    # A pile of unrelated library content a candidate-scoped query has no
    # reason to touch.
    other_feed = Feed(user_id=USER_ID, rss_url="https://example.com/scope-unrelated", channel_title="Unrelated")
    db_session.add(other_feed)
    db_session.commit()
    db_session.refresh(other_feed)
    db_session.add_all(
        Content(feed_id=other_feed.id, user_id=USER_ID, video_id=f"unrelated{i:04d}", title="x") for i in range(50)
    )
    db_session.commit()

    monkeypatch.setattr(
        backfill_module,
        "fetch_channel_all_videos",
        lambda channel_id, on_progress=None: [
            BackfillEntry(video_id="scopecand01", title="T", thumbnail_url=None, duration_seconds=None)
        ],
    )

    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if "video_id in" in statement.lower():
            captured.append(parameters)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        backfill_module.run_backfill(feed.id, channel_id, db_session)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert captured, "expected a Content.video_id IN (...) query"
    assert len(captured[0]) < 10, (
        f"the existing-ids query bound {len(captured[0])} parameters — it read the whole "
        "50-row unrelated library instead of just this channel's own candidate id"
    )


def test_progress_is_not_updated_on_every_single_saved_row(db_session, monkeypatch):
    """Regression: each backfill_progress.set() call takes a lock and sweeps
    the whole registry — cheap per call, but a channel with thousands of new
    videos turned that into thousands of redundant cycles for a number the
    polling UI only samples a few times a second. Verified by counting the
    actual set() calls across a backfill with more rows than one throttle
    window, and confirming the very last row still gets an accurate report."""
    channel_id = "UCbackfillthrottle"
    feed = Feed(user_id=USER_ID, rss_url=channel_feed_url(channel_id), channel_title="Throttle", followed=True)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    row_count = backfill_module.PROGRESS_UPDATE_INTERVAL * 3 + 1
    monkeypatch.setattr(
        backfill_module,
        "fetch_channel_all_videos",
        lambda channel_id, on_progress=None: [
            BackfillEntry(video_id=f"throttle{i:04d}", title="T", thumbnail_url=None, duration_seconds=None)
            for i in range(row_count)
        ],
    )

    calls = []
    original_set = backfill_module.backfill_progress.set

    def spy_set(key, value):
        calls.append(value)
        original_set(key, value)

    monkeypatch.setattr(backfill_module.backfill_progress, "set", spy_set)

    backfill_module.run_backfill(feed.id, channel_id, db_session)

    saving_calls = [v for v in calls if v[0] == "saving"]
    assert len(saving_calls) < row_count / 2, (
        f"{len(saving_calls)} 'saving' progress updates for {row_count} rows — "
        "still reporting on (close to) every row instead of throttling"
    )
    assert saving_calls[-1] == ("saving", row_count, row_count), (
        "the final row must still report exact progress even when it doesn't "
        "land on a throttle-interval boundary"
    )


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


# ------------------------------------------------------- GET /feeds/backfilling


def test_backfilling_lists_only_this_profiles_running_scans(client, db_session):
    """What Library's "Fetching uploads…" cards poll on.

    A newly followed channel is usable the moment POST /feeds answers — its
    RSS sync has already run — while its full history scan carries on in the
    background for minutes. Nothing waits for that any more, so the grid has
    to be able to ask what is still running, and it has to be scoped: another
    profile's scan is none of this one's business.
    """
    mine = Feed(user_id=USER_ID, rss_url="https://example.com/mine", channel_title="Mine")
    other_profile = User(name="Someone Else", account_id=1)
    db_session.add_all([mine, other_profile])
    db_session.commit()
    theirs = Feed(
        user_id=other_profile.id, rss_url="https://example.com/theirs", channel_title="Theirs"
    )
    db_session.add(theirs)
    db_session.commit()

    backfill_module.backfill_progress.set(mine.id, ("scanning", 3, 100))
    backfill_module.backfill_progress.set(theirs.id, ("scanning", 3, 100))
    try:
        assert client.get("/feeds/backfilling").json() == [mine.id]

        # A finished scan keeps its registry entry readable for a while (see
        # progress.py), so "has an entry" is not "is running" — a card left
        # saying "fetching" forever is exactly what confusing the two causes.
        backfill_module.backfill_progress.set(mine.id, ("done", 100, 100))
        assert client.get("/feeds/backfilling").json() == []
    finally:
        backfill_module.backfill_progress.discard(mine.id)
        backfill_module.backfill_progress.discard(theirs.id)


def test_library_marks_a_channel_whose_history_is_still_being_fetched(client, db_session):
    """The whole reason the onboarding wizard stopped waiting: the wait moved
    onto the card of the channel it belongs to, where it can be ignored."""
    feed = Feed(
        user_id=USER_ID, rss_url="https://example.com/preparing", channel_title="Still Filling In"
    )
    db_session.add(feed)
    db_session.commit()

    backfill_module.backfill_progress.set(feed.id, ("saving", 40, 900))
    try:
        body = client.get("/partials/library").text
        assert 'data-preparing="true"' in body
        assert "Fetching uploads" in body
    finally:
        backfill_module.backfill_progress.discard(feed.id)

    body = client.get("/partials/library").text
    assert "data-preparing" not in body, "the card kept saying it was fetching after the scan ended"


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


def test_following_an_artist_skips_the_history_scan(client, monkeypatch):
    """A Topic channel holds the artist's whole catalogue — 1,064 uploads
    for Drake, measured. Scanning it would import all of it to answer a
    request that only means "tell me when they release something".

    The saved feed is what says so, not the request: since
    feed_add._as_artist_follow the server recognises most artist follows on
    its own, and those arrive as a plain channel URL with nothing in the
    payload to key off."""
    scans: list[int] = []
    monkeypatch.setattr(
        feeds_router,
        "add_feed_core",
        lambda db, url, user_id, artist_browse_id=None: (
            Feed(
                id=7,
                user_id=user_id,
                rss_url=channel_feed_url(TOPIC_ID),
                channel_title="Shirin David",
                artist_browse_id=ARTIST_BROWSE_ID,
                added_at=datetime(2026, 8, 19),
            ),
            0,
            TOPIC_ID,
        ),
    )
    monkeypatch.setattr(feeds_router, "run_backfill_task", lambda feed_id, channel_id: scans.append(feed_id))

    res = client.post(
        "/feeds",
        json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}", "artist_browse_id": ARTIST_BROWSE_ID},
    )

    assert res.status_code == 201
    assert scans == []


def test_following_a_channel_still_scans_its_history(client, monkeypatch):
    scans: list[int] = []
    monkeypatch.setattr(
        feeds_router,
        "add_feed_core",
        lambda db, url, user_id, artist_browse_id=None: (
            Feed(
                id=7,
                user_id=user_id,
                rss_url=channel_feed_url(TOPIC_ID),
                channel_title="A Channel",
                added_at=datetime(2026, 8, 19),
            ),
            0,
            TOPIC_ID,
        ),
    )
    monkeypatch.setattr(feeds_router, "run_backfill_task", lambda feed_id, channel_id: scans.append(feed_id))

    res = client.post("/feeds", json={"channel_url": "https://www.youtube.com/@somebody"})

    assert res.status_code == 201
    assert scans == [7]


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


def test_adding_an_artist_by_plain_channel_url_skips_the_scan_end_to_end(
    client, db_session, monkeypatch
):
    """The onboarding wizard's Add button, in full: it sends a channel URL
    and nothing else (it never opens the profile — a full-panel navigation
    out from under its modal would strand the wizard half-finished), and the
    feed it gets back is the artist's, with no history scan behind it."""
    scans: list[int] = []
    _stub_topic_feed(monkeypatch)
    _stub_artist_lookup(monkeypatch, _artist())
    monkeypatch.setattr(
        feeds_router, "run_backfill_task", lambda feed_id, channel_id: scans.append(feed_id)
    )

    res = client.post(
        "/feeds", json={"channel_url": f"https://www.youtube.com/channel/{OFFICIAL_ID}"}
    )

    assert res.status_code == 201
    feed = db_session.query(Feed).filter(Feed.id == res.json()["feed"]["id"]).one()
    assert feed.rss_url == channel_feed_url(TOPIC_ID)
    assert feed.artist_browse_id == OFFICIAL_ID
    assert scans == []
