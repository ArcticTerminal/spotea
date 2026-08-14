"""Progress tracking for background work that a client polls for.

Downloads, channel backfills and bulk imports all report the same way: a
background task writes progress, a polling request reads it. Each of them
started as its own module-level dict, and each grew its own lifetime rules
— the bulk-import one had none at all, so every import a process ever ran
stayed in memory for the life of the container.

Terminal entries deliberately stay readable for a while instead of being
dropped the moment the work finishes: a small, fast job can complete before
the client's first poll, and a missing key is indistinguishable from "never
started" — the client would show nothing happening and never learn the job
was already done. Expiry is what gives that grace period a bound.

Single-process only, exactly like the dicts this replaces: a second uvicorn
worker gets its own registry and sees none of the first one's progress.
Making this survive multiple workers means moving it to the DB or a shared
cache, which this app has no other reason to need.
"""

import threading
import time

# Long enough to cover a channel backfill that a client stopped polling
# midway (so a returning tab can still see how it ended), short enough that
# an abandoned job isn't held for the container's whole lifetime.
DEFAULT_TTL_SECONDS = 60 * 60


class ProgressRegistry[K, V]:
    """Keyed progress values that expire a while after their last write.

    Note that expiry tracks `set()` calls, not reads and not in-place
    mutation of a stored mutable value — a task that keeps a long job alive
    has to keep calling `set()` (see routers/feeds.py's _run_bulk_import,
    which re-sets after each line it finishes) rather than only mutating the
    dict it stored once at the start.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[K, tuple[float, V]] = {}
        # Writers are background tasks on worker threads, readers are
        # request handlers on other threads. Individual dict operations are
        # atomic under the GIL, but the sweep below is a read-then-delete
        # over the whole dict, which isn't.
        self._lock = threading.Lock()

    def set(self, key: K, value: V) -> None:
        with self._lock:
            self._sweep()
            self._entries[key] = (time.monotonic(), value)

    def get(self, key: K, default: V | None = None) -> V | None:
        """Same shape as dict.get — an unknown (or expired) key is
        indistinguishable from one that was never written, which is exactly
        what every caller here already has to handle."""
        with self._lock:
            self._sweep()
            entry = self._entries.get(key)
            return entry[1] if entry is not None else default

    def discard(self, key: K) -> None:
        """Drop an entry ahead of its expiry — for when the thing it tracks
        no longer exists at all (e.g. its feed was just deleted), so even
        the grace period above would only ever serve progress for something
        gone."""
        with self._lock:
            self._entries.pop(key, None)

    def _sweep(self) -> None:
        # Caller holds the lock. O(entries), but "entries" is the number of
        # jobs in flight plus recently finished ones — a handful, not a
        # collection that grows with the library.
        cutoff = time.monotonic() - self._ttl_seconds
        stale = [key for key, (written_at, _) in self._entries.items() if written_at < cutoff]
        for key in stale:
            del self._entries[key]
