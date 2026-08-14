import logging
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.deps import get_current_profile, get_db, require_login
from app.feed_sync import REFRESH_POOL_SIZE, apply_feed_data, fetch_feed_data
from app.feed_sync import refresh_feeds as sync_refresh_feeds
from app.models import Content, Feed, User
from app.rss import (
    SHORT_MAX_DURATION_SECONDS,
    ChannelResolutionError,
    InvalidFeedError,
    channel_feed_url,
    extract_channel_id,
    fetch_channel_all_videos,
    fetch_feed,
    resolve_feed_url,
    resolve_video_channel,
    search_channels,
    search_videos,
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
    VideoAddCreate,
    VideoAddResult,
    VideoSearchResultOut,
)
from app.storage import unlink_if_unshared, unlink_thumbnail_if_unshared

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(require_login)])

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
        anchor = oldest_known or datetime.utcnow()

        total = len(new_entries)
        _backfill_progress[feed_id] = ("saving", 0, total)
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
            _backfill_progress[feed_id] = ("saving", i, total)

        db.commit()
        _backfill_progress[feed_id] = ("done", total, total)
    except Exception:
        # Whatever went wrong (DB, disk, anything else), the polling UI must
        # still terminate instead of spinning on "scanning"/"saving" forever.
        db.rollback()
        _backfill_progress[feed_id] = ("done", 0, 0)
        logger.exception("Backfill failed for feed %s (%s)", feed_id, feed.channel_title)


class FeedAlreadyExistsError(Exception):
    def __init__(self, rss_url: str, channel_title: str | None):
        super().__init__(rss_url)
        self.channel_title = channel_title


def _create_feed_from_rss_url(db: Session, rss_url: str, user_id: int) -> tuple[Feed, int, str | None]:
    """DB-and-remaining-fetch half of adding a feed, given an already-resolved
    RSS URL. Split out from _add_feed_core so bulk import can resolve many
    URLs in parallel first (see _resolve_bulk_entry) and then only run this,
    strictly sequential, part per line. Callers decide how to run the
    returned channel_id's backfill — deferred via BackgroundTasks for a
    single add (keeps the response fast), inline for bulk import (which is
    already running off the request thread).

    A matching Feed can already exist with followed=False — a placeholder
    created by add_single_video for a channel the user only grabbed one
    video from (see _get_or_create_placeholder_feed). Actually following it
    now means upgrading that row in place (flip followed, run the same
    fetch/backfill a brand-new feed gets) rather than bouncing the user with
    "already exists" for a feed they never knowingly added."""
    existing = db.query(Feed).filter(Feed.user_id == user_id, Feed.rss_url == rss_url).first()
    if existing and existing.followed:
        raise FeedAlreadyExistsError(rss_url, existing.channel_title)

    parsed = fetch_feed(rss_url)

    if existing:
        feed = existing
        feed.followed = True
        if parsed.channel_title:
            feed.channel_title = parsed.channel_title
        db.commit()
    else:
        feed = Feed(user_id=user_id, rss_url=rss_url, channel_title=parsed.channel_title)
        db.add(feed)
        db.commit()
        db.refresh(feed)

    result = fetch_feed_data(feed.id, feed.rss_url, feed.avatar_url)
    new_count = apply_feed_data(db, feed, result)

    channel_id = extract_channel_id(rss_url)
    return feed, new_count, channel_id


def _add_feed_core(db: Session, channel_url: str, user_id: int) -> tuple[Feed, int, str | None]:
    """Resolve, validate, save a feed, and apply its first RSS parse. Shared
    by the single-add route and bulk import so the two never diverge."""
    rss_url = resolve_feed_url(channel_url.strip())
    return _create_feed_from_rss_url(db, rss_url, user_id)


@router.post("", response_model=FeedAddResult, status_code=status.HTTP_201_CREATED)
def add_feed(
    payload: FeedCreate,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> FeedAddResult:
    try:
        feed, new_count, channel_id = _add_feed_core(db, payload.channel_url, profile.id)
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
    same reasoning feed_sync.fetch_feed_data's docstring gives for parallelizing
    refresh_feeds."""
    try:
        rss_url = resolve_feed_url(_normalize_bulk_entry(line))
        return {"line": line, "rss_url": rss_url, "error": None}
    except ChannelResolutionError as exc:
        return {"line": line, "rss_url": None, "error": str(exc)}


def _run_bulk_import(job_id: str, lines: list[str], user_id: int) -> None:
    progress = _import_progress[job_id]

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
                    feed, _new_count, channel_id = _create_feed_from_rss_url(
                        db, resolved["rss_url"], user_id
                    )
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
def start_bulk_import(
    payload: BulkImportCreate,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
) -> BulkImportStartOut:
    lines = [line.strip() for line in payload.urls.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No channels given")

    job_id = secrets.token_urlsafe(8)
    _import_progress[job_id] = {"total": len(lines), "resolved": 0, "done": 0, "results": []}
    background_tasks.add_task(_run_bulk_import, job_id, lines, profile.id)
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


@router.get("/search-videos", response_model=list[VideoSearchResultOut])
def search_video_feeds(q: str) -> list[VideoSearchResultOut]:
    query = q.strip()
    if not query:
        return []

    return [VideoSearchResultOut(**result.__dict__) for result in search_videos(query)]


def _get_or_create_placeholder_feed(
    db: Session, channel_id: str, channel_title: str | None, user_id: int
) -> Feed:
    """Feed row for a channel the user hasn't actually followed — exists only
    so a single video added via Explore has somewhere to attach (Content.feed_id
    is required). followed=False keeps it out of Library and the background
    refresh scheduler (see feed_sync.refresh_feeds's callers) until the user
    follows the channel for real, which upgrades this same row in place (see
    _create_feed_from_rss_url) instead of creating a duplicate — rss_url must
    be built with the exact same channel_feed_url helper resolve_feed_url
    uses, since that's the dedup key that lookup checks by equality.

    No avatar fetch: a placeholder feed's avatar is never displayed anywhere
    (Library and the channel-hero page are the only avatar consumers, and
    both are followed-only surfaces)."""
    rss_url = channel_feed_url(channel_id)
    existing = db.query(Feed).filter(Feed.user_id == user_id, Feed.rss_url == rss_url).first()
    if existing:
        return existing

    feed = Feed(user_id=user_id, rss_url=rss_url, channel_title=channel_title, followed=False)
    db.add(feed)
    db.commit()
    db.refresh(feed)
    return feed


@router.post("/videos", response_model=VideoAddResult, status_code=status.HTTP_201_CREATED)
def add_single_video(
    payload: VideoAddCreate,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> VideoAddResult:
    """Explore's "listen" action — adds exactly one video without following
    its channel. Always created as a preview (Content.is_preview=True): it
    plays through the normal player like any other content, but stays out of
    Library/New Uploads until the user favorites or saves it (see
    routers/content.py's add_favorite/add_saved).

    If this video already has a Content row for this user — a previous
    Explore preview, or a real upload from a followed channel — this isn't a
    conflict: it just means there's nothing to add, so hand back that row's
    id and let the player match/replay whatever was already downloaded."""
    existing_content = (
        db.query(Content)
        .filter(Content.user_id == profile.id, Content.video_id == payload.video_id)
        .first()
    )
    if existing_content:
        return VideoAddResult(content_id=existing_content.id)

    channel_id = resolve_video_channel(payload.video_id)
    if not channel_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not resolve this video")

    feed = _get_or_create_placeholder_feed(db, channel_id, payload.channel_title, profile.id)

    content = Content(
        feed_id=feed.id,
        user_id=profile.id,
        video_id=payload.video_id,
        title=payload.title,
        # Stored as-is — see _run_backfill's comment above; the player page
        # (or wherever this ends up rendered first) queues the same lazy
        # caching, and this is a synchronous request handler so downloading
        # here would delay the "listen" click's own response for no benefit.
        thumbnail_url=payload.thumbnail_url,
        duration_seconds=payload.duration_seconds,
        # Flat search results don't reliably expose a real upload date, and
        # NULL sorts last in SQLite's ORDER BY ... DESC (every Home shelf) —
        # "just added" as the effective date is also the correct intent here.
        published_at=datetime.utcnow(),
        is_preview=True,
    )
    db.add(content)
    db.commit()
    db.refresh(content)

    return VideoAddResult(content_id=content.id)


@router.delete("/videos/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_single_video(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> None:
    """Removes a video added via Explore outright (unlike DELETE
    /content/{id}, which only resets download status) — used both to dismiss
    a preview early and to remove something already kept. Only for content on
    a followed=False feed; a real follow's content comes off through
    unfollowing the channel, not this."""
    content = (
        db.query(Content)
        .join(Feed)
        .filter(Content.id == content_id, Content.user_id == profile.id, Feed.followed.is_(False))
        .first()
    )
    if not content:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    feed_id = content.feed_id
    if content.file_path:
        unlink_if_unshared(db, content.file_path, content.id)
    unlink_thumbnail_if_unshared(db, content.video_id, content.id)
    db.delete(content)
    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.feed_id == feed_id).scalar()
    if remaining == 0:
        db.query(Feed).filter(Feed.id == feed_id).delete()
        db.commit()


@router.get("", response_model=list[FeedOut])
def list_feeds(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> list[Feed]:
    return (
        db.query(Feed)
        .filter(Feed.user_id == profile.id, Feed.followed.is_(True))
        .order_by(Feed.added_at.desc())
        .all()
    )


@router.get("/{feed_id}/backfill-status", response_model=BackfillStatusOut)
def get_backfill_status(
    feed_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> BackfillStatusOut:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == profile.id).first()
    if not feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    phase, done, total = _backfill_progress.get(feed_id, (None, 0, 0))
    return BackfillStatusOut(feed_id=feed_id, phase=phase, done=done, total=total)


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(
    feed_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> None:
    """Unfollowing isn't allowed to destroy what the user actually downloaded,
    played, favorited, or saved — only content nobody ever touched gets
    purged. Anything kept stays on the feed row, which is downgraded to
    followed=False (same state as an Explore placeholder — see
    _get_or_create_placeholder_feed) rather than deleted, so it drops out of
    Library/New Uploads/background refresh but keeps working everywhere else
    (Storage, Recently Played, Favorites/Saved, direct playback — none of
    those filter on Feed.followed). Re-following the same channel later picks
    this same row back up via _create_feed_from_rss_url's rss_url lookup
    instead of duplicating it."""
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == profile.id).first()
    if not feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    content_rows = db.query(Content).filter(Content.feed_id == feed_id).all()
    for content in content_rows:
        keep = (
            content.status == "ready"
            or content.last_played_at is not None
            or content.is_favorite
            or content.is_saved
        )
        if not keep:
            if content.file_path:
                unlink_if_unshared(db, content.file_path, content.id)
            unlink_thumbnail_if_unshared(db, content.video_id, content.id)
            db.delete(content)

    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.feed_id == feed_id).scalar()
    if remaining == 0:
        db.delete(feed)
    else:
        feed.followed = False
    db.commit()

    _backfill_progress.pop(feed_id, None)


@router.post("/refresh", response_model=RefreshResult)
def refresh_feeds(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> RefreshResult:
    feeds = db.query(Feed).filter(Feed.user_id == profile.id, Feed.followed.is_(True)).all()
    return RefreshResult(new_content_count=sync_refresh_feeds(db, feeds))
