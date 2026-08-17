import asyncio
import contextlib
import logging

from sqlalchemy.orm import Session

from app.content_query import followed_feeds
from app.database import SessionLocal
from app.feed_sync import refresh_feeds
from app.models import Account
from app.storage import sweep_orphans, sweep_stale_previews
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

# The running loop's task, owned here rather than by main.py's lifespan so
# that /health can ask whether background refreshing is actually still
# happening — see is_alive below.
_task: asyncio.Task | None = None

# How long to wait before starting the cycle over after an unexpected failure.
# Without a pause, a persistent error (a locked database) would turn the loop
# into a tight spin that logs a traceback per iteration; with one, the loop
# keeps trying at a rate that stays readable in the log.
ERROR_BACKOFF_SECONDS = 60

# How often the loop wakes to check which accounts are due. The cadence a
# given account actually experiences is still whatever it chose in Settings
# (Account.feed_refresh_interval_minutes) — this is just the clock that
# choice is checked against, now that the interval is per-account rather
# than one shared AppSettings row read once at the top of the old loop.
# Short enough that even the shortest preset (15 minutes) is checked several
# times within its own window, so a settings change takes effect within a
# few minutes rather than needing an interrupt; long enough that an account
# on the longest preset (2 hours) isn't polled for nothing 24 times before
# it's ever due.
TICK_SECONDS = 5 * 60


def _due_accounts(db: Session) -> list[Account]:
    """Accounts whose own interval has elapsed since their own last refresh.

    A never-refreshed account (feeds_refreshed_at is None — brand new, or
    freshly migrated off the old shared AppSettings row) is always due.
    """
    now = utcnow()
    due = []
    for account in db.query(Account).all():
        if account.feeds_refreshed_at is None:
            due.append(account)
            continue
        elapsed_minutes = (now - account.feeds_refreshed_at).total_seconds() / 60
        if elapsed_minutes >= account.feed_refresh_interval_minutes:
            due.append(account)
    return due


def _refresh_due_accounts() -> None:
    with SessionLocal() as db:
        for account in _due_accounts(db):
            feeds = followed_feeds(db, account_id=account.id).all()
            new_count = refresh_feeds(db, feeds)
            account.feeds_refreshed_at = utcnow()
            db.commit()
            if new_count:
                logger.info(
                    "Background refresh added %d new item(s) across %d feed(s) for account %d",
                    new_count,
                    len(feeds),
                    account.id,
                )


def _sweep_disk() -> None:
    """The other half of every tick — file and row cleanup that has nothing
    to do with any one account, so it runs unconditionally rather than
    gated on _due_accounts. See storage.py's module docstring for why these
    two (and not a ".part" sweep) are safe to run on this cadence."""
    with SessionLocal() as db:
        sweep_orphans(db)
        sweep_stale_previews(db)


async def run_scheduler() -> None:
    """Runs for the lifetime of the app (started/stopped via start/stop
    below), checking on TICK_SECONDS which accounts are due for their own
    configured interval and refreshing just those — see _due_accounts and
    Account.feed_refresh_interval_minutes — then sweeping disk/DB leftovers
    (see _sweep_disk) regardless of which, if any, accounts were due.

    The whole cycle is guarded, not just the refresh. It used to be only the
    refresh: reading the (then-singleton) interval setting sat outside any
    try, so a single "database is locked" there — entirely plausible while
    eight refresh threads are writing — killed this task outright. Nothing
    noticed. The lifespan only awaits it at shutdown, so from the outside the
    app was healthy and simply never fetched a new upload again; the only
    symptom was "no new videos", which reads as a YouTube or RSS problem. See
    is_alive below for the other half of the fix.
    """
    while True:
        # CancelledError is a BaseException, so `except Exception` below lets
        # a shutdown through untouched.
        try:
            await asyncio.to_thread(_refresh_due_accounts)
            await asyncio.to_thread(_sweep_disk)
        except Exception:
            logger.exception("Feed refresh cycle failed; retrying in %ds", ERROR_BACKOFF_SECONDS)
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)
            continue
        await asyncio.sleep(TICK_SECONDS)


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
