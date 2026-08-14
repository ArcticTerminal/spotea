"""One-time full-history scan for a newly followed channel.

Split out of routers/feeds.py, which had grown to hold five unrelated
concerns. This one is the odd shape among them: a long-running background
job with its own progress tracking that two different callers drive
differently — a single add defers it so the response can go out
immediately, while bulk import runs it inline, one channel at a time.
"""

import logging
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Content, Feed
from app.progress import ProgressRegistry
from app.timeutil import utcnow
from app.youtube.extract import ChannelResolutionError, fetch_channel_all_videos
from app.youtube.urls import SHORT_MAX_DURATION_SECONDS

logger = logging.getLogger(__name__)


# In-memory only: fine for a single-process app, mirrors the download-progress
# pattern in content.py. Keyed by feed_id. Terminal phase is "done" (done ==
# total, possibly 0) rather than removing the entry — small/fast channels can
# finish scanning+saving in well under a second, faster than the client's
# first poll; if we just deleted the key, that client would see "nothing
# happening" and have no way to tell "finished with nothing new" apart from
# "never ran". Entries are dropped when their feed is deleted (see routers/feeds.py's delete_feed).
backfill_progress: ProgressRegistry[int, tuple[str, int, int]] = ProgressRegistry()


def run_backfill(feed_id: int, channel_id: str, db: Session) -> None:
    """One-time full-history scan for a channel, run in the background so
    add_feed can return immediately with the RSS feed's most recent videos.
    Routine refreshes stay RSS-only — see refresh_feeds.

    The uploads playlist doesn't expose per-video publish dates (yt-dlp's
    flat extraction has no timestamp field for it), so backfilled videos get
    synthetic published_at values: one second apart, counting back from the
    oldest date already known for this feed (from RSS, which does have real
    dates). That's not a real date, but it preserves the true newest-to-oldest
    order the uploads playlist is always in, so date-based sort/filter still
    behaves — only the exact date label would be wrong if ever displayed."""
    feed = db.get(Feed, feed_id)
    if feed is None:
        return

    backfill_progress.set(feed_id, ("scanning", 0, 0))

    def on_scan_progress(progress: tuple[str, int, int]) -> None:
        # progress is ("listing", page_number, 0) while yt-dlp is still
        # paging through the channel (no total known yet), or ("counting",
        # done, total) once the full list is in and it's processing entries.
        # Either way (done, total) is meaningful to show as scanning progress.
        _, done, total = progress
        backfill_progress.set(feed_id, ("scanning", done, total))

    try:
        videos = fetch_channel_all_videos(channel_id, on_progress=on_scan_progress)
    except ChannelResolutionError:
        backfill_progress.set(feed_id, ("done", 0, 0))
        return

    try:
        # user_id-scoped, not feed_id-scoped: a video can already exist under
        # a different feed_id for this user (e.g. an Explore preview added
        # before this channel was followed for real) — see the same
        # reasoning in feed_sync.apply_feed_data. Skipping it here, rather
        # than inserting a second row, avoids tripping Content's
        # (user_id, video_id) unique constraint.
        existing_ids = {
            row.video_id
            for row in db.query(Content.video_id).filter(Content.user_id == feed.user_id)
        }
        # Same defensive Shorts guard as feed_sync.apply_feed_data — the
        # Videos-tab playlist backfill relies on is supposed to exclude
        # Shorts already, but that's an unofficial convention, not a
        # guarantee.
        new_entries = [
            v
            for v in videos
            if v.video_id not in existing_ids
            and not (v.duration_seconds is not None and v.duration_seconds <= SHORT_MAX_DURATION_SECONDS)
        ]

        oldest_known = (
            db.query(func.min(Content.published_at))
            .filter(Content.feed_id == feed_id, Content.published_at.isnot(None))
            .scalar()
        )
        anchor = oldest_known or utcnow()

        total = len(new_entries)
        backfill_progress.set(feed_id, ("saving", 0, total))
        for i, entry in enumerate(new_entries, start=1):
            # Stored as-is (still a remote ytimg.com URL) — no need to fetch
            # it here. Whichever page first renders this row (channel page,
            # Library, ...) queues the same caching pages.py already does for
            # every other render (see _queue_thumbnail_caching), so eagerly
            # downloading during a full-history backfill would just be
            # blocking hundreds of inserts on network round trips nobody's
            # waiting on yet.
            db.add(
                Content(
                    feed_id=feed.id,
                    user_id=feed.user_id,
                    video_id=entry.video_id,
                    title=entry.title,
                    thumbnail_url=entry.thumbnail_url,
                    duration_seconds=entry.duration_seconds,
                    published_at=anchor - timedelta(seconds=i),
                )
            )
            backfill_progress.set(feed_id, ("saving", i, total))

        db.commit()
        backfill_progress.set(feed_id, ("done", total, total))
    except Exception:
        # Whatever went wrong (DB, disk, anything else), the polling UI must
        # still terminate instead of spinning on "scanning"/"saving" forever.
        db.rollback()
        backfill_progress.set(feed_id, ("done", 0, 0))
        logger.exception("Backfill failed for feed %s (%s)", feed_id, feed.channel_title)


def run_backfill_task(feed_id: int, channel_id: str) -> None:
    """BackgroundTasks entry point for _run_backfill, on a session of its own.

    The request's `Depends(get_db)` session must not be used here: since
    FastAPI 0.106 a yield-dependency's exit code (get_db's `db.close()`)
    runs before the response is sent, so it's already closed by the time a
    background task starts. That matters more for a backfill than anywhere
    else — this can run for minutes on a worker thread, long after the
    request that scheduled it is gone. Bulk import calls _run_backfill
    directly instead, since it already owns a session of its own (see
    bulk_import.run_bulk_import) and wants each channel's backfill to finish before
    starting the next."""
    with SessionLocal() as db:
        run_backfill(feed_id, channel_id, db)
