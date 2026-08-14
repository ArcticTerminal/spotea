"""Following many channels at once, from a pasted list.

Two phases with different shapes: resolving each line to an RSS URL is pure
network and fans out across threads, then creating the feeds is strictly
sequential on one session (SQLite dislikes concurrent writers, and
sequential is also what makes duplicate detection within a single batch
work).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.database import SessionLocal
from app.feed_sync import REFRESH_POOL_SIZE
from app.progress import ProgressRegistry
from app.services.backfill import run_backfill
from app.services.feed_add import FeedAlreadyExistsError, create_feed_from_rss_url
from app.youtube.extract import ChannelResolutionError, resolve_feed_url
from app.youtube.rss import InvalidFeedError

# In-memory only, same rationale as backfill.py's registry. Keyed by a
# random job id (not a feed id — one job spans many feeds). Unlike a feed's
# backfill entry there's no later event that means "this job's tracking can
# go" the way delete_feed does, so its entries live purely on the
# registry's expiry — which is the whole reason that expiry exists (a plain
# dict here grew by one entry per import, forever).
import_progress: ProgressRegistry[str, dict] = ProgressRegistry()


def _normalize_bulk_entry(line: str) -> str:
    """Accepts a bare "@handle" (as pasted from a plain list) alongside
    already-full URLs (as pasted from a browser or a Google Takeout
    subscriptions.csv) — resolve_feed_url() needs a URL, so a bare handle
    gets the channel URL prefix it's missing. Anything that already looks
    like a URL is passed through untouched."""
    if line.startswith("@") and "://" not in line and "youtube.com" not in line:
        return f"https://www.youtube.com/{line}"
    return line


def _resolve_bulk_entry(line: str) -> dict:
    """Runs in a worker thread — pure network (yt-dlp), no DB access, so it's
    safe to fan out. For a batch of bare @handles this per-line channel
    resolution is the dominant cost (each one is its own yt-dlp lookup), the
    same reasoning feed_sync.fetch_feed_data's docstring gives for parallelizing
    refresh_feeds."""
    try:
        rss_url = resolve_feed_url(_normalize_bulk_entry(line))
        return {"line": line, "rss_url": rss_url, "error": None}
    except ChannelResolutionError as exc:
        return {"line": line, "rss_url": None, "error": str(exc)}


def run_bulk_import(job_id: str, progress: dict, lines: list[str], user_id: int) -> None:
    """`progress` is the same dict start_bulk_import registered, handed over
    directly rather than looked up again — this mutates it in place and
    re-registers it after each step, which is what keeps the entry from
    expiring under a long import (see ProgressRegistry, whose expiry tracks
    `set()` calls and not in-place mutation)."""

    # Phase 1: resolve every line in parallel (see _resolve_bulk_entry) —
    # capped the same as REFRESH_POOL_SIZE, for the same reason (stay polite
    # to YouTube's unauthenticated scraping).
    resolved_by_line: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(lines), REFRESH_POOL_SIZE)) as pool:
        futures = [pool.submit(_resolve_bulk_entry, line) for line in lines]
        for future in as_completed(futures):
            resolved = future.result()
            resolved_by_line[resolved["line"]] = resolved
            progress["resolved"] += 1
            import_progress.set(job_id, progress)

    # Phase 2: create feeds and run each one's initial parse + backfill
    # sequentially, on a single session — SQLite doesn't handle concurrent
    # writers well, and this is also where duplicate detection naturally
    # lives (create_feed_from_rss_url's existence check sees every feed
    # already committed earlier in this same batch, not just pre-existing
    # ones). Original line order is preserved regardless of resolution order.
    with SessionLocal() as db:
        for raw_line in lines:
            resolved = resolved_by_line[raw_line]
            entry = {"url": raw_line, "status": "error", "channel_title": None, "error": resolved["error"]}

            if resolved["error"] is None:
                try:
                    feed, _new_count, channel_id = create_feed_from_rss_url(
                        db, resolved["rss_url"], user_id
                    )
                    entry["status"] = "added"
                    entry["error"] = None
                    entry["channel_title"] = feed.channel_title
                    if channel_id:
                        run_backfill(feed.id, channel_id, db)
                except FeedAlreadyExistsError as exc:
                    entry["status"] = "duplicate"
                    entry["error"] = None
                    entry["channel_title"] = exc.channel_title
                except InvalidFeedError as exc:
                    entry["error"] = str(exc)

            progress["results"].append(entry)
            progress["done"] += 1
            import_progress.set(job_id, progress)
