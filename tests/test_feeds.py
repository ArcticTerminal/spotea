import logging
from datetime import datetime

import app.feed_sync as feed_sync_module
import app.routers.feeds as feeds_router
import app.services.backfill as backfill_module
import app.services.feed_add as feed_add_module
from app.feed_sync import FeedFetchResult, apply_feed_data
from app.models import Content, Feed
from app.youtube.extract import BackfillEntry
from app.youtube.rss import FeedUnavailableError, InvalidFeedError, ParsedEntry, ParsedFeed
from app.youtube.urls import channel_feed_url

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
        lambda db, url, user_id: (_ for _ in ()).throw(
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
        lambda db, url, user_id: (_ for _ in ()).throw(
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
