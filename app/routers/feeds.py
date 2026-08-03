from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_db, require_login
from app.models import Content, Feed
from app.rss import (
    ChannelResolutionError,
    InvalidFeedError,
    extract_channel_id,
    fetch_channel_all_videos,
    fetch_channel_shorts_ids,
    fetch_channel_video_durations,
    fetch_feed,
    resolve_feed_url,
    search_channels,
)
from app.schemas import (
    BackfillStatusOut,
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

    shorts_ids = fetch_channel_shorts_ids(channel_id, full=True) if videos else set()
    existing_ids = {
        row.video_id for row in db.query(Content.video_id).filter(Content.feed_id == feed_id)
    }
    new_entries = [
        v for v in videos if v.video_id not in shorts_ids and v.video_id not in existing_ids
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


def _sync_feed_content(db: Session, feed: Feed) -> int:
    """Fetch a feed's RSS and insert any content rows not already known. Returns new-row count."""
    try:
        parsed = fetch_feed(feed.rss_url)
    except InvalidFeedError:
        return 0

    if not feed.channel_title and parsed.channel_title:
        feed.channel_title = parsed.channel_title

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
    candidate_entries = [entry for entry in parsed.entries if entry.video_id not in existing_rows]

    channel_id = extract_channel_id(feed.rss_url)

    new_entries = candidate_entries
    if candidate_entries and channel_id:
        shorts_ids = fetch_channel_shorts_ids(channel_id)
        new_entries = [entry for entry in candidate_entries if entry.video_id not in shorts_ids]

    durations: dict[str, int] = {}
    if (new_entries or missing_duration_ids) and channel_id:
        durations = fetch_channel_video_durations(channel_id)

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
                duration_seconds=durations.get(entry.video_id),
            )
        )
        new_count += 1

    for video_id in missing_duration_ids:
        if video_id in durations:
            existing_rows[video_id].duration_seconds = durations[video_id]

    db.commit()
    return new_count


@router.post("", response_model=FeedAddResult, status_code=status.HTTP_201_CREATED)
def add_feed(
    payload: FeedCreate, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> FeedAddResult:
    channel_url = payload.channel_url.strip()

    try:
        rss_url = resolve_feed_url(channel_url)
    except ChannelResolutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    existing = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID, Feed.rss_url == rss_url).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Feed already added")

    try:
        parsed = fetch_feed(rss_url)
    except InvalidFeedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    feed = Feed(user_id=DEFAULT_USER_ID, rss_url=rss_url, channel_title=parsed.channel_title)
    db.add(feed)
    db.commit()
    db.refresh(feed)

    new_count = _sync_feed_content(db, feed)

    channel_id = extract_channel_id(rss_url)
    if channel_id:
        background_tasks.add_task(_run_backfill, feed.id, channel_id, db)

    return FeedAddResult(feed=FeedOut.model_validate(feed), new_content_count=new_count)


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
    total_new = sum(_sync_feed_content(db, feed) for feed in feeds)
    return RefreshResult(new_content_count=total_new)
