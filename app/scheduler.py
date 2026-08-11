import asyncio
import logging

from app.database import SessionLocal
from app.feed_sync import refresh_feeds
from app.models import AppSettings, Feed

logger = logging.getLogger(__name__)

# Populated by run_scheduler() each time it starts, so request_reschedule()
# (called from a sync request-handler thread, not the event loop thread) can
# signal it safely via call_soon_threadsafe. An asyncio.Event can only be
# waited on from the loop it was created on, so it's recreated per run
# rather than held as a module-level singleton — the test suite spins up a
# fresh event loop per app lifespan, and reusing one Event across loops
# raises "bound to a different event loop".
_wake_event: asyncio.Event | None = None
_loop: asyncio.AbstractEventLoop | None = None


def request_reschedule() -> None:
    """Call after the interval setting changes so a running sleep is cut
    short and the loop re-reads the new interval right away, instead of
    finishing out however much of the old one was left."""
    if _wake_event is not None and _loop is not None:
        _loop.call_soon_threadsafe(_wake_event.set)


def _read_interval_minutes() -> int:
    with SessionLocal() as db:
        app_settings = db.get(AppSettings, 1)
        return app_settings.feed_refresh_interval_minutes if app_settings else 30


def _refresh_all_feeds() -> None:
    with SessionLocal() as db:
        # followed=False feeds are Explore placeholders (see
        # routers/feeds.py's _get_or_create_placeholder_feed) — polling them
        # would silently turn "I grabbed one song" into "I'm now following
        # this channel's every future upload", which nobody asked for.
        feeds = db.query(Feed).filter(Feed.followed.is_(True)).all()
        new_count = refresh_feeds(db, feeds)
        if new_count:
            logger.info("Background refresh added %d new item(s) across %d feed(s)", new_count, len(feeds))


async def run_scheduler() -> None:
    """Runs for the lifetime of the app (started/cancelled in main.py's
    lifespan), refreshing every profile's feeds on a shared interval — see
    AppSettings.feed_refresh_interval_minutes. Keeps content fresh on its
    own, so nothing on the client needs to block on a refresh anymore."""
    global _wake_event, _loop
    _loop = asyncio.get_running_loop()
    _wake_event = asyncio.Event()

    while True:
        interval_minutes = await asyncio.to_thread(_read_interval_minutes)
        _wake_event.clear()
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=interval_minutes * 60)
            # Woke early because the interval setting changed — reschedule
            # with the new value rather than refreshing right now.
            continue
        except asyncio.TimeoutError:
            pass  # interval elapsed naturally — time to refresh

        try:
            await asyncio.to_thread(_refresh_all_feeds)
        except Exception:
            logger.exception("Background feed refresh failed")
