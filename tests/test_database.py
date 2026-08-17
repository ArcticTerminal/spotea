"""Connection settings, indexes, and the one-off data sweeps in migrations.py.

The indexes here are not cosmetic: measured on a real 30k-row library, the ten
hottest queries went from 81.7ms of SQLite time to 3.8ms, and unfollowing a
6,540-video channel from ~35 seconds to ~0.13 — purge_content does two
previously-unindexed lookups per row.
"""

from sqlalchemy import inspect

from app.database import engine
from app.migrations import run_migrations
from app.models import Content, Feed
from app.timeutil import utcnow
from app.youtube.urls import SHORT_MAX_DURATION_SECONDS

USER_ID = 1


def _feed(db_session, rss_url: str) -> Feed:
    feed = Feed(user_id=USER_ID, rss_url=rss_url, channel_title="Shorts Channel")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    return feed


def test_sqlite_runs_in_wal_mode_with_foreign_keys_on():
    """WAL because a writer in rollback-journal mode blocks readers outright,
    and the background refresh commits once per channel across dozens of them.
    foreign_keys because SQLite leaves them off per connection, so the FKs the
    schema declares were never actually enforced."""
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_migrations_add_indexes_missing_from_an_existing_database():
    """create_all() skips a table that already exists, and its indexes with it.

    So the install that most needs these — one with enough rows for a SCAN to
    hurt — is exactly the one create_all() would never give them to.
    """
    target = "ix_content_user_feed_published"
    with engine.begin() as conn:
        conn.exec_driver_sql(f"DROP INDEX IF EXISTS {target}")
    assert target not in {index["name"] for index in inspect(engine).get_indexes("content")}

    run_migrations(engine)

    assert target in {index["name"] for index in inspect(engine).get_indexes("content")}


def test_the_hot_queries_use_an_index():
    """A plan check, because the point of the indexes is the plan, not the DDL.

    The channel query answered by walking every row the user had; the shared-file
    lookup was a full table SCAN, once per file removed.
    """
    with engine.connect() as conn:
        channel_plan = "\n".join(
            str(row)
            for row in conn.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT * FROM content "
                "WHERE user_id = 1 AND feed_id = 1 ORDER BY published_at DESC LIMIT 20"
            ).all()
        )
        shared_file_plan = "\n".join(
            str(row)
            for row in conn.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT id FROM content WHERE file_path = 'x' AND status = 'ready'"
            ).all()
        )

    assert "ix_content_user_feed_published" in channel_plan, channel_plan
    assert "SCAN content" not in shared_file_plan, shared_file_plan


def test_legacy_shorts_are_swept_but_only_the_untouched_ones(db_session):
    """Shorts imported before the duration filter existed stay in the library
    forever otherwise — 124 of them on the real database, all from the first
    bulk import.

    Anything the user actually did something with is theirs to keep, however it
    got there: the same test delete_feed applies when deciding what an unfollow
    may remove.
    """
    feed = _feed(db_session, "https://example.com/shorts")
    short = SHORT_MAX_DURATION_SECONDS - 10
    rows = {
        "untouched": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacyshrt1", title="Untouched Short",
            duration_seconds=short,
        ),
        "played": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacyshrt2", title="Played Short",
            duration_seconds=short, last_played_at=utcnow(),
        ),
        "favorite": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacyshrt3", title="Favorite Short",
            duration_seconds=short, is_favorite=True,
        ),
        "saved": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacyshrt4", title="Saved Short",
            duration_seconds=short, is_saved=True,
        ),
        "downloaded": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacyshrt5", title="Downloaded Short",
            duration_seconds=short, status="ready", file_path="/tmp/legacyshrt5.m4a",
        ),
        "long": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacylong1", title="A real video",
            duration_seconds=600,
        ),
        "unmeasured": Content(
            feed_id=feed.id, user_id=USER_ID, video_id="legacynodur", title="No duration yet",
            duration_seconds=None,
        ),
    }
    db_session.add_all(rows.values())
    db_session.commit()

    run_migrations(engine)
    db_session.expire_all()

    surviving = {
        video_id
        for (video_id,) in db_session.query(Content.video_id).filter(Content.feed_id == feed.id)
    }

    assert "legacyshrt1" not in surviving, "an untouched Short should have been swept"
    # Everything the user touched, plus anything that isn't a Short at all.
    assert surviving == {
        "legacyshrt2",
        "legacyshrt3",
        "legacyshrt4",
        "legacyshrt5",
        "legacylong1",
        "legacynodur",
    }


def test_the_sweep_is_idempotent(db_session):
    """It runs on every startup, so a second pass must be a no-op rather than
    finding new things to delete."""
    feed = _feed(db_session, "https://example.com/shorts-again")
    db_session.add(
        Content(
            feed_id=feed.id,
            user_id=USER_ID,
            video_id="idempotent1",
            title="A real video",
            duration_seconds=900,
        )
    )
    db_session.commit()

    run_migrations(engine)
    run_migrations(engine)
    db_session.expire_all()

    assert db_session.query(Content).filter(Content.feed_id == feed.id).count() == 1
