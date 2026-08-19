"""Following, unfollowing and refreshing channels.

The work itself lives in app/services (feed creation, the one-time history
backfill, bulk import) — this file is the HTTP surface over it: validation,
status codes, and deciding how each service call gets run (deferred to a
background task for a single add, inline for bulk import).
"""

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.content_query import followed_feeds
from app.deps import get_current_profile, get_db, require_login
from app.feed_sync import refresh_feeds as sync_refresh_feeds
from app.models import Content, Feed, User
from app.schemas import (
    BackfillStatusOut,
    BulkImportCreate,
    BulkImportResultOut,
    BulkImportStartOut,
    BulkImportStatusOut,
    FeedAddResult,
    FeedCreate,
    FeedOut,
    RefreshResult,
)
from app.services.backfill import (
    backfill_progress,
    backfilling_feed_ids,
    mark_syncing,
    run_initial_sync_task,
)
from app.services.bulk_import import import_progress, run_bulk_import
from app.services.feed_add import FeedAlreadyExistsError, add_feed_core
from app.storage import purge_content
from app.youtube.extract import ChannelResolutionError
from app.youtube.rss import FeedUnavailableError, InvalidFeedError

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(require_login)])


@router.post("", response_model=FeedAddResult, status_code=status.HTTP_201_CREATED)
def add_feed(
    payload: FeedCreate,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> FeedAddResult:
    try:
        feed, new_count, channel_id = add_feed_core(
            db,
            payload.channel_url,
            profile.id,
            artist_browse_id=payload.artist_browse_id,
            # Answer as soon as the feed row exists. Everything after it —
            # the RSS content, the durations, the avatar, the history scan —
            # is what run_initial_sync does in the background, and what
            # Library's card reports while it happens.
            sync=False,
        )
    except FeedAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Feed already added") from exc
    except ChannelResolutionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    # A feed YouTube wouldn't serve us is not a malformed request, and saying
    # 400 to it tells the user their URL is wrong when it isn't — which is what
    # rss.FeedError's split exists to stop. Sibling classes, so the order of
    # these two handlers doesn't matter; the status codes do.
    except FeedUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except InvalidFeedError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Whether this ends in a history scan is run_initial_sync's decision now,
    # not this route's — it reads the saved feed, which is the only place that
    # knows whether the server recognised an artist (see feed_add._as_artist_follow).
    # Marked here rather than inside the task, so the card the client is
    # about to render cannot beat it to the question — see mark_syncing.
    mark_syncing(feed.id)
    background_tasks.add_task(run_initial_sync_task, feed.id, channel_id)

    # Always 0: nothing has been fetched yet. Kept on the response because a
    # bulk import's per-line result still means something by it, and no
    # caller of this route reads it.
    return FeedAddResult(feed=FeedOut.model_validate(feed), new_content_count=new_count)

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
    progress = {"total": len(lines), "resolved": 0, "done": 0, "results": []}
    import_progress.set(job_id, progress)
    background_tasks.add_task(run_bulk_import, job_id, progress, lines, profile.id)
    return BulkImportStartOut(job_id=job_id, total=len(lines))


@router.get("/import/{job_id}/status", response_model=BulkImportStatusOut)
def get_bulk_import_status(job_id: str) -> BulkImportStatusOut:
    progress = import_progress.get(job_id)
    if progress is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found")
    # `progress` is the exact dict object run_bulk_import's background-task
    # thread is still mutating — ProgressRegistry's lock only guards the
    # registry's own bookkeeping (set/get/sweep), not a value handed back by
    # get(). list(...) snapshots "results" once, up front, instead of the
    # list comprehension below iterating the live list directly across
    # however many BulkImportResultOut(**r) calls that takes — each one a
    # window for the worker thread to append behind this request's back.
    results = list(progress["results"])
    return BulkImportStatusOut(
        total=progress["total"],
        resolved=progress["resolved"],
        done=progress["done"],
        results=[BulkImportResultOut(**r) for r in results],
    )

@router.get("/backfilling", response_model=list[int])
def list_backfilling_feeds(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> list[int]:
    """This profile's feeds still being filled in right now — their first
    RSS sync, or the one-time history scan behind it (see
    services/backfill.ACTIVE_PHASES).

    What Library's "Fetching uploads…" cards poll on, so they can turn
    back into a video count once the work behind them finishes (see
    home/library.js). Since POST /feeds stopped syncing inline, a brand-new
    card is in this list from the moment it appears rather than showing a
    confident "0 videos" for the couple of seconds that took. One call for the whole grid rather than one
    backfill-status call per card, and it costs a dict lookup each — the
    registry is in memory.

    Declared above /{feed_id}/backfill-status only for readability; the two
    paths can't collide.
    """
    feed_ids = [feed_id for (feed_id,) in db.query(Feed.id).filter(Feed.user_id == profile.id)]
    return sorted(backfilling_feed_ids(feed_ids))


@router.get("/{feed_id}/backfill-status", response_model=BackfillStatusOut)
def get_backfill_status(
    feed_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> BackfillStatusOut:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == profile.id).first()
    if not feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    phase, done, total = backfill_progress.get(feed_id, (None, 0, 0))
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
    this same row back up via services/feed_add.py's create_feed_from_rss_url lookup
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
            purge_content(db, content)

    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.feed_id == feed_id).scalar()
    if remaining == 0:
        db.delete(feed)
    else:
        feed.followed = False
    db.commit()

    backfill_progress.discard(feed_id)


@router.post("/refresh", response_model=RefreshResult)
def refresh_feeds(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> RefreshResult:
    feeds = followed_feeds(db, profile.id).all()
    return RefreshResult(new_content_count=sync_refresh_feeds(db, feeds))
