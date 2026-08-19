"""The background work a newly followed channel needs: its first RSS sync,
then its one-time full-history scan.

Split out of routers/feeds.py, which had grown to hold five unrelated
concerns. This one is the odd shape among them: a long-running background
job with its own progress tracking that two different callers drive
differently — a single add defers it so the response can go out
immediately, while bulk import runs it inline, one channel at a time.

The first RSS sync used to happen inline, inside POST /feeds, and that is
what the onboarding wizard's Finish button was really waiting on: measured
per channel, 1.32s to read the durations out of yt-dlp and 0.84s for the
avatar, against 0.11s for the RSS itself. Six channels, drained one at a
time, is twenty seconds of somebody looking at "Finishing…". None of it has
to happen before the answer — the feed row is what Library renders a card
from, and the card already knows how to say it is still filling in.
"""

import logging
from collections.abc import Iterable
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.feed_sync import apply_feed_data, fetch_feed_data
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

# How often the per-row saving loop below reports progress, in rows. Not 1:
# see the comment at its call site.
PROGRESS_UPDATE_INTERVAL = 25

# The phases this is actually working in. The registry keeps terminal
# entries readable for a while after the fact (see progress.py), so "has an
# entry" and "is running" are different questions.
#
# "syncing" is the first RSS read, which now runs here rather than inside
# POST /feeds — it belongs in this set for the same reason the other two do:
# it is time during which the card has nothing real to show yet.
ACTIVE_PHASES = frozenset({"syncing", "scanning", "saving"})


def backfilling_feed_ids(feed_ids: Iterable[int]) -> set[int]:
    """Which of `feed_ids` have a history scan running right now.

    A dict lookup each, no query — the registry is in-memory. Library's grid
    asks this for every card it renders (see page_context.library_context) so
    a channel still filling in can say so on its own card, which is what let
    the onboarding wizard stop making anyone wait for a backfill at all: a
    scan of a 6,500-video channel is minutes long, and nothing on the first
    screen after onboarding needs it — the RSS sync that POST /feeds already
    did before answering is what puts the channel's recent uploads there.
    """
    return {
        feed_id
        for feed_id in feed_ids
        if (backfill_progress.get(feed_id) or ("", 0, 0))[0] in ACTIVE_PHASES
    }


def mark_syncing(feed_id: int) -> None:
    """Puts a feed into the "filling in" state without doing any of the work.

    Called by POST /feeds before it answers, so the card Library renders off
    that answer is already reporting itself — a background task cannot be
    relied on to have started by the time the client comes back asking for
    fragments, and losing that race would render a confident "0 videos" and
    then never poll, because polling only happens while such a card is on
    the page (see home/library.js). run_initial_sync sets it again for every
    other caller; the registry takes the same value twice happily."""
    backfill_progress.set(feed_id, ("syncing", 0, 0))


def run_initial_sync(feed_id: int, channel_id: str | None, db: Session) -> None:
    """Everything following a channel needs that doesn't have to happen
    before the answer: its first RSS read, and then — for a channel, not an
    artist — its history scan.

    Registered as "syncing" before anything is fetched, so the card Library
    renders the moment POST /feeds answers already says "Fetching uploads…"
    instead of sitting there claiming zero videos. That claim was the reason
    this had to be inline before: a card that appears empty and stays empty
    for two seconds reads as a channel that failed to add.

    An artist is where this stops. Their feed is their "<Artist> - Topic"
    channel, which carries the whole catalogue (1,064 uploads for Drake,
    measured), and following them means "tell me when they release
    something" — see routers/feeds.py. The RSS read above has already
    brought in the recent handful.
    """
    feed = db.get(Feed, feed_id)
    if feed is None:
        return

    mark_syncing(feed_id)
    try:
        apply_feed_data(db, feed, fetch_feed_data(feed.id, feed.rss_url, feed.avatar_url))
    except Exception:
        # fetch_feed_data already swallows a FeedError into "no new content",
        # so anything reaching here is unexpected — and it must still clear
        # the phase, or the card says "Fetching uploads…" forever.
        logger.exception("Initial sync failed for feed %s", feed_id)
        backfill_progress.set(feed_id, ("done", 0, 0))
        return

    if channel_id and not feed.artist_browse_id:
        run_backfill(feed_id, channel_id, db)
    else:
        backfill_progress.set(feed_id, ("done", 0, 0))


def run_initial_sync_task(feed_id: int, channel_id: str | None) -> None:
    """BackgroundTasks entry point for run_initial_sync, on a session of its
    own — see run_backfill_task for why the request's session cannot be
    used here."""
    with SessionLocal() as db:
        run_initial_sync(feed_id, channel_id, db)


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
        #
        # Scoped to this channel's own candidate ids, not the user's whole
        # library: bulk import calls run_backfill once per channel in a
        # loop, and an unscoped query re-read the entire library (tens of
        # thousands of rows for a long-time user) on every single one —
        # O(channels x library) for a 50-channel import. The candidate set
        # is at most len(videos), which is what actually needs checking.
        candidate_ids = {v.video_id for v in videos}
        existing_ids = {
            row.video_id
            for row in db.query(Content.video_id).filter(
                Content.user_id == feed.user_id, Content.video_id.in_(candidate_ids)
            )
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
            # Every row took a lock and swept the whole registry — cheap
            # per call (a handful of in-flight jobs), but a large channel's
            # thousands of rows turned that into thousands of redundant
            # cycles for a number the polling UI only samples a few times a
            # second anyway. Always report the last row so a poll landing
            # right at the end still sees "done" progress, not a stale
            # in-between count.
            if i % PROGRESS_UPDATE_INTERVAL == 0 or i == total:
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
