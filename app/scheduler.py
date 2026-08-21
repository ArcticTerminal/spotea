"""The one thing that still has to happen on a clock: disk and row cleanup.

Artist refreshing used to live here too — every five minutes this asked
which users' intervals had elapsed and fetched for those, whether or not
anyone was using the app. That moved to the moment someone opens it (see
services/refresh.py). What is left is genuinely nobody's page load: orphaned
files and expired preview rows accumulate on their own and no request is
waiting on them being gone.
"""

import asyncio
import contextlib
import logging

from app.database import SessionLocal
from app.storage import sweep_orphans, sweep_stale_previews

logger = logging.getLogger(__name__)

# The running loop's task, owned here rather than by main.py's lifespan so
# that /health can ask whether the sweeper is actually still running — see
# is_alive below.
_task: asyncio.Task | None = None

# How long to wait before starting the cycle over after an unexpected failure.
# Without a pause, a persistent error (a locked database) would turn the loop
# into a tight spin that logs a traceback per iteration; with one, the loop
# keeps trying at a rate that stays readable in the log.
ERROR_BACKOFF_SECONDS = 60

# How often the sweeps run. It was five minutes when this loop also had to
# notice a user becoming due for a refresh; nothing is waiting on a sweep, so
# a slower cadence costs only that an orphaned file lingers a little longer.
TICK_SECONDS = 15 * 60


def _sweep_disk() -> None:
    """The whole of every tick now: file and row cleanup that has nothing to
    do with any one user. See storage.py's module docstring for why these two
    (and not a ".part" sweep) are safe to run on this cadence."""
    with SessionLocal() as db:
        sweep_orphans(db)
        sweep_stale_previews(db)


async def run_scheduler() -> None:
    """Runs for the lifetime of the app (started/stopped via start/stop
    below), sweeping disk and DB leftovers every TICK_SECONDS.

    The cycle is guarded because an unguarded one died silently once: a
    single "database is locked" killed this task outright, and since the
    lifespan only awaits it at shutdown, the app went on reporting itself
    healthy while nothing in the background ran again. See is_alive below for
    the other half of that fix.
    """
    while True:
        # CancelledError is a BaseException, so `except Exception` below lets
        # a shutdown through untouched.
        try:
            await asyncio.to_thread(_sweep_disk)
        except Exception:
            logger.exception("Sweep cycle failed; retrying in %ds", ERROR_BACKOFF_SECONDS)
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)
            continue
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """Begin the sweep loop, keeping a handle on it for is_alive/stop."""
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
    """Whether the sweep loop is still running.

    False means background cleanup has stopped for good and the process needs
    replacing — which is exactly what /health reports, so that compose's
    `restart: unless-stopped` can act on it.
    """
    return _task is not None and not _task.done()
