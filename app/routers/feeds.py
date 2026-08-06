import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_db, require_login
from app.downloader import download_avatar
from app.models import Content, Feed
from app.rss import (
    ChannelResolutionError,
    InvalidFeedError,
    ParsedFeed,
    extract_channel_id,
    fetch_channel_all_videos,
    fetch_channel_avatar_url,
    fetch_channel_video_durations,
    fetch_feed,
    longform_feed_url,
    resolve_feed_url,
    search_channels,
)
from app.schemas import (
    BackfillStatusOut,
    BulkImportCreate,
    BulkImportResultOut,
    BulkImportStartOut,
    BulkImportStatusOut,
    ChannelSearchResultOut,
    FeedAddResult,
    FeedCreate,
    FeedOut,
    RefreshResult,
)
from app.storage import delete_files_for_feed

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(require_login)])

DEFAULT_USER_ID = 1

# In-memory only: fine for a single-process app, mirrors the download-progress
# pattern in content.py. Keyed by feed_id. Terminal phase is "done" (done ==
# total, possibly 0) rather than removing the entry — small/fast channels can
# finish scanning+saving in well under a second, faster than the client's
# first poll; if we just deleted the key, that client would see "nothing
# happening" and have no way to tell "finished with nothing new" apart from
# "never ran". Entries are dropped when their feed is deleted (see delete_feed).
_backfill_progress: dict[int, tuple[str, int, int]] = {}


def _run_backfill(feed_id: int, channel_id: str, db: Session) -> None:
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

    _backfill_progress[feed_id] = ("scanning", 0, 0)

    def on_scan_progress(progress: tuple[str, int, int]) -> None:
        # progress is ("listing", page_number, 0) while yt-dlp is still
        # paging through the channel (no total known yet), or ("counting",
        # done, total) once the full list is in and it's processing entries.
        # Either way (done, total) is meaningful to show as scanning progress.
        _, done, total = progress
        _backfill_progress[feed_id] = ("scanning", done, total)

    try:
        videos = fetch_channel_all_videos(channel_id, on_progress=on_scan_progress)
    except ChannelResolutionError:
        _backfill_progress[feed_id] = ("done", 0, 0)
        return

    existing_ids = {
        row.video_id for row in db.query(Content.video_id).filter(Content.feed_id == feed_id)
    }
    new_entries = [v for v in videos if v.video_id not in existing_ids]

    oldest_known = (
        db.query(func.min(Content.published_at))
        .filter(Content.feed_id == feed_id, Content.published_at.isnot(None))
        .scalar()
    )
    anchor = oldest_known or datetime.utcnow()

    total = len(new_entries)
    _backfill_progress[feed_id] = ("saving", 0, total)
    for i, entry in enumerate(new_entries, start=1):
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
        _backfill_progress[feed_id] = ("saving", i, total)

    db.commit()
    _backfill_progress[feed_id] = ("done", total, total)


# Feed refresh is network-bound (RSS parse + a yt-dlp call per channel), so
# refresh_feeds fans those out across threads rather than doing them one
# channel at a time. Kept modest to stay polite to YouTube's servers — this
# is unauthenticated scraping, and a burst of dozens of concurrent requests
# risks 429s. DB writes never happen inside the pool (see _apply_feed_data).
_REFRESH_POOL_SIZE = 8


@dataclass
class _FeedFetchResult:
    parsed: ParsedFeed | None
    durations: dict[str, int]
    channel_id: str | None
    avatar_url: str | None = None


def _fetch_feed_data(feed_id: int, rss_url: str, avatar_url: str | None) -> _FeedFetchResult:
    """The network half of a feed sync: RSS fetch plus, when needed, a
    duration lookup and a channel-avatar lookup. Safe to run off the main
    thread and in parallel across feeds — touches no SQLAlchemy state but
    its own throwaway read session.

    Fetches via the channel's Videos-tab playlist (UULF) when its channel ID
    is known, rather than rss_url's plain channel feed — Shorts are excluded
    there for free, no separate Shorts-tab fetch needed."""
    channel_id = extract_channel_id(rss_url)
    fetch_url = longform_feed_url(channel_id) if channel_id else rss_url

    try:
        parsed = fetch_feed(fetch_url)
    except InvalidFeedError:
        return _FeedFetchResult(parsed=None, durations={}, channel_id=channel_id)

    durations: dict[str, int] = {}
    if channel_id and parsed.entries:
        incoming_ids = [entry.video_id for entry in parsed.entries]
        with SessionLocal() as read_db:
            existing = read_db.query(Content.video_id, Content.duration_seconds).filter(
                Content.feed_id == feed_id, Content.video_id.in_(incoming_ids)
            ).all()
        existing_ids = {video_id for video_id, _ in existing}
        needs_durations = any(vid not in existing_ids for vid in incoming_ids) or any(
            duration is None for _, duration in existing
        )
        if needs_durations:
            durations = fetch_channel_video_durations(channel_id)

    # Fetched (and downloaded) once per channel, ever — skipped as soon as a
    # feed has one, so this never adds a call to the steady-state per-session
    # refresh.
    fetched_avatar_url = None
    if not avatar_url and channel_id:
        remote_avatar_url = fetch_channel_avatar_url(channel_id)
        if remote_avatar_url:
            fetched_avatar_url = download_avatar(channel_id, remote_avatar_url)

    return _FeedFetchResult(
        parsed=parsed, durations=durations, channel_id=channel_id, avatar_url=fetched_avatar_url
    )


def _apply_feed_data(db: Session, feed: Feed, result: _FeedFetchResult) -> int:
    """The DB half of a feed sync: insert new content rows and backfill
    missing durations from an already-fetched _FeedFetchResult. Must run on
    the request's own session, so always sequential (never in the pool)."""
    parsed = result.parsed
    if parsed is None:
        return 0

    # parsed.channel_title is "Videos" (the UULF playlist's own title, not the
    # channel's) whenever the fetch went through the Videos-tab playlist, so
    # only trust it as a channel_title backfill on the plain-feed fallback path.
    if not feed.channel_title and parsed.channel_title and not result.channel_id:
        feed.channel_title = parsed.channel_title

    if result.avatar_url:
        feed.avatar_url = result.avatar_url

    incoming_ids = [entry.video_id for entry in parsed.entries]
    existing_rows = {
        row.video_id: row
        for row in db.query(Content).filter(
            Content.feed_id == feed.id, Content.video_id.in_(incoming_ids)
        )
    }
    missing_duration_ids = {
        video_id for video_id, row in existing_rows.items() if row.duration_seconds is None
    }
    new_entries = [entry for entry in parsed.entries if entry.video_id not in existing_rows]

    new_count = 0
    for entry in new_entries:
        db.add(
            Content(
                feed_id=feed.id,
                user_id=feed.user_id,
                video_id=entry.video_id,
                title=entry.title,
                thumbnail_url=entry.thumbnail_url,
                published_at=entry.published_at,
                duration_seconds=result.durations.get(entry.video_id),
            )
        )
        new_count += 1

    for video_id in missing_duration_ids:
        if video_id in result.durations:
            existing_rows[video_id].duration_seconds = result.durations[video_id]

    db.commit()
    return new_count


class FeedAlreadyExistsError(Exception):
    def __init__(self, rss_url: str, channel_title: str | None):
        super().__init__(rss_url)
        self.channel_title = channel_title


def _create_feed_from_rss_url(db: Session, rss_url: str) -> tuple[Feed, int, str | None]:
    """DB-and-remaining-fetch half of adding a feed, given an already-resolved
    RSS URL. Split out from _add_feed_core so bulk import can resolve many
    URLs in parallel first (see _resolve_bulk_entry) and then only run this,
    strictly sequential, part per line. Callers decide how to run the
    returned channel_id's backfill — deferred via BackgroundTasks for a
    single add (keeps the response fast), inline for bulk import (which is
    already running off the request thread)."""
    existing = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID, Feed.rss_url == rss_url).first()
    if existing:
        raise FeedAlreadyExistsError(rss_url, existing.channel_title)

    parsed = fetch_feed(rss_url)

    feed = Feed(user_id=DEFAULT_USER_ID, rss_url=rss_url, channel_title=parsed.channel_title)
    db.add(feed)
    db.commit()
    db.refresh(feed)

    result = _fetch_feed_data(feed.id, feed.rss_url, feed.avatar_url)
    new_count = _apply_feed_data(db, feed, result)

    channel_id = extract_channel_id(rss_url)
    return feed, new_count, channel_id


def _add_feed_core(db: Session, channel_url: str) -> tuple[Feed, int, str | None]:
    """Resolve, validate, save a feed, and apply its first RSS parse. Shared
    by the single-add route and bulk import so the two never diverge."""
    rss_url = resolve_feed_url(channel_url.strip())
    return _create_feed_from_rss_url(db, rss_url)


@router.post("", response_model=FeedAddResult, status_code=status.HTTP_201_CREATED)
def add_feed(
    payload: FeedCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> FeedAddResult:
    try:
        feed, new_count, channel_id = _add_feed_core(db, payload.channel_url)
    except FeedAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Feed already added") from exc
    except ChannelResolutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except InvalidFeedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if channel_id:
        background_tasks.add_task(_run_backfill, feed.id, channel_id, db)

    return FeedAddResult(feed=FeedOut.model_validate(feed), new_content_count=new_count)


# In-memory only, same rationale as _backfill_progress above. Keyed by a
# random job id (not a feed id — one job spans many feeds).
_import_progress: dict[str, dict] = {}


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
    same reasoning _fetch_feed_data's docstring gives for parallelizing
    refresh_feeds."""
    try:
        rss_url = resolve_feed_url(_normalize_bulk_entry(line))
        return {"line": line, "rss_url": rss_url, "error": None}
    except ChannelResolutionError as exc:
        return {"line": line, "rss_url": None, "error": str(exc)}


def _run_bulk_import(job_id: str, lines: list[str]) -> None:
    progress = _import_progress[job_id]

    # Phase 1: resolve every line in parallel (see _resolve_bulk_entry) —
    # capped the same as _REFRESH_POOL_SIZE, for the same reason (stay polite
    # to YouTube's unauthenticated scraping).
    resolved_by_line: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(lines), _REFRESH_POOL_SIZE)) as pool:
        futures = [pool.submit(_resolve_bulk_entry, line) for line in lines]
        for future in as_completed(futures):
            resolved = future.result()
            resolved_by_line[resolved["line"]] = resolved
            progress["resolved"] += 1

    # Phase 2: create feeds and run each one's initial parse + backfill
    # sequentially, on a single session — SQLite doesn't handle concurrent
    # writers well, and this is also where duplicate detection naturally
    # lives (_create_feed_from_rss_url's existence check sees every feed
    # already committed earlier in this same batch, not just pre-existing
    # ones). Original line order is preserved regardless of resolution order.
    with SessionLocal() as db:
        for raw_line in lines:
            resolved = resolved_by_line[raw_line]
            entry = {"url": raw_line, "status": "error", "channel_title": None, "error": resolved["error"]}

            if resolved["error"] is None:
                try:
                    feed, _new_count, channel_id = _create_feed_from_rss_url(db, resolved["rss_url"])
                    entry["status"] = "added"
                    entry["error"] = None
                    entry["channel_title"] = feed.channel_title
                    if channel_id:
                        _run_backfill(feed.id, channel_id, db)
                except FeedAlreadyExistsError as exc:
                    entry["status"] = "duplicate"
                    entry["error"] = None
                    entry["channel_title"] = exc.channel_title
                except InvalidFeedError as exc:
                    entry["error"] = str(exc)

            progress["results"].append(entry)
            progress["done"] += 1


@router.post("/import", response_model=BulkImportStartOut, status_code=status.HTTP_202_ACCEPTED)
def start_bulk_import(payload: BulkImportCreate, background_tasks: BackgroundTasks) -> BulkImportStartOut:
    lines = [line.strip() for line in payload.urls.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No channels given")

    job_id = secrets.token_urlsafe(8)
    _import_progress[job_id] = {"total": len(lines), "resolved": 0, "done": 0, "results": []}
    background_tasks.add_task(_run_bulk_import, job_id, lines)
    return BulkImportStartOut(job_id=job_id, total=len(lines))


@router.get("/import/{job_id}/status", response_model=BulkImportStatusOut)
def get_bulk_import_status(job_id: str) -> BulkImportStatusOut:
    progress = _import_progress.get(job_id)
    if progress is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found")
    return BulkImportStatusOut(
        total=progress["total"],
        resolved=progress["resolved"],
        done=progress["done"],
        results=[BulkImportResultOut(**r) for r in progress["results"]],
    )


@router.get("/search", response_model=list[ChannelSearchResultOut])
def search_feeds(q: str) -> list[ChannelSearchResultOut]:
    query = q.strip()
    if not query:
        return []

    return [ChannelSearchResultOut(**result.__dict__) for result in search_channels(query)]


@router.get("", response_model=list[FeedOut])
def list_feeds(db: Session = Depends(get_db)) -> list[Feed]:
    return db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).order_by(Feed.added_at.desc()).all()


@router.get("/{feed_id}/backfill-status", response_model=BackfillStatusOut)
def get_backfill_status(feed_id: int, db: Session = Depends(get_db)) -> BackfillStatusOut:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == DEFAULT_USER_ID).first()
    if not feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    phase, done, total = _backfill_progress.get(feed_id, (None, 0, 0))
    return BackfillStatusOut(feed_id=feed_id, phase=phase, done=done, total=total)


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(feed_id: int, db: Session = Depends(get_db)) -> None:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == DEFAULT_USER_ID).first()
    if not feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    # Content rows cascade with the feed, but their files on disk don't.
    delete_files_for_feed(db, feed.id)

    db.delete(feed)
    db.commit()
    _backfill_progress.pop(feed_id, None)


@router.post("/refresh", response_model=RefreshResult)
def refresh_feeds(db: Session = Depends(get_db)) -> RefreshResult:
    feeds = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).all()
    if not feeds:
        return RefreshResult(new_content_count=0)

    with ThreadPoolExecutor(max_workers=min(len(feeds), _REFRESH_POOL_SIZE)) as pool:
        results = list(pool.map(lambda f: _fetch_feed_data(f.id, f.rss_url, f.avatar_url), feeds))

    total_new = sum(
        _apply_feed_data(db, feed, result) for feed, result in zip(feeds, results)
    )
    return RefreshResult(new_content_count=total_new)
