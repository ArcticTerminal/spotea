import asyncio
import contextlib
import logging

from app.app_settings import get_app_settings
from app.content_query import followed_feeds
from app.database import SessionLocal
from app.feed_sync import refresh_feeds

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

# The running loop's task, owned here rather than by main.py's lifespan so
# that /health can ask whether background refreshing is actually still
# happening — see is_alive below.
_task: asyncio.Task | None = None

# How long to wait before starting the cycle over after an unexpected failure.
# Without a pause, a persistent error (a locked database, a missing settings
# row) would turn the loop into a tight spin that logs a traceback per
# iteration; with one, the loop keeps trying at a rate that stays readable in
# the log and leaves the interval it would otherwise have slept roughly intact.
ERROR_BACKOFF_SECONDS = 60


def request_reschedule() -> None:
    """Call after the interval setting changes so a running sleep is cut
    short and the loop re-reads the new interval right away, instead of
    finishing out however much of the old one was left."""
    if _wake_event is not None and _loop is not None:
        _loop.call_soon_threadsafe(_wake_event.set)


def _read_interval_minutes() -> int:
    with SessionLocal() as db:
        return get_app_settings(db).feed_refresh_interval_minutes


def _refresh_all_feeds() -> None:
    with SessionLocal() as db:
        # No user_id: this is the one refresh loop for the whole deployment,
        # covering every profile's feeds (see AppSettings' docstring).
        feeds = followed_feeds(db).all()
        new_count = refresh_feeds(db, feeds)
        if new_count:
            logger.info("Background refresh added %d new item(s) across %d feed(s)", new_count, len(feeds))


async def run_scheduler() -> None:
    """Runs for the lifetime of the app (started/stopped via start/stop below),
    refreshing every profile's feeds on a shared interval — see
    AppSettings.feed_refresh_interval_minutes. Keeps content fresh on its
    own, so nothing on the client needs to block on a refresh anymore.

    The whole cycle is guarded, not just the refresh. It used to be only the
    refresh: `_read_interval_minutes` sat outside any try, so a single
    "database is locked" there — entirely plausible while eight refresh
    threads are writing — killed this task outright. Nothing noticed. The
    lifespan only awaits it at shutdown, so from the outside the app was
    healthy and simply never fetched a new upload again; the only symptom was
    "no new videos", which reads as a YouTube or RSS problem. See is_alive
    below for the other half of the fix.
    """
    global _wake_event, _loop
    _loop = asyncio.get_running_loop()
    _wake_event = asyncio.Event()

    while True:
        # CancelledError is a BaseException, so `except Exception` below lets
        # a shutdown through untouched.
        try:
            interval_minutes = await asyncio.to_thread(_read_interval_minutes)
            _wake_event.clear()
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=interval_minutes * 60)
                # Woke early because the interval setting changed — reschedule
                # with the new value rather than refreshing right now.
                continue
            except TimeoutError:
                pass  # interval elapsed naturally — time to refresh

            await asyncio.to_thread(_refresh_all_feeds)
        except Exception:
            logger.exception("Feed refresh cycle failed; retrying in %ds", ERROR_BACKOFF_SECONDS)
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)


def start() -> None:
    """Begin the refresh loop, keeping a handle on it for is_alive/stop."""
    global _task
    _task = asyncio.create_task(run_scheduler())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None


def is_alive() -> bool:
    """Whether the refresh loop is still running.

    False means background refreshing has stopped for good and the process
    needs replacing — which is exactly what /health reports, so that
    compose's `restart: unless-stopped` can act on it.
    """
    return _task is not None and not _task.done()
