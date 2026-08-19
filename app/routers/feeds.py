"""Following, unfollowing and refreshing channels.

The work itself lives in app/services (feed creation, the one-time history
backfill, bulk import) — this file is the HTTP surface over it: validation,
status codes, and deciding how each service call gets run (deferred to a
background task for a single add, inline for bulk import).
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.content_query import followed_feeds
from app.deps import get_current_profile, get_db, require_login
from app.feed_sync import refresh_feeds as sync_refresh_feeds
from app.models import Content, Feed, User
from app.schemas import (
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
        feed, new_count, _channel_id = add_feed_core(
            db,
            payload.channel_url,
            profile.id,
            artist_browse_id=payload.artist_browse_id,
            # Answer as soon as the feed row exists. Everything after it —
            # the content, the durations, the avatar — is what
            # run_initial_sync does in the background, and what Library's
            # card reports while it happens.
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

    # Marked here rather than inside the task, so the card the client is
    # about to render cannot beat it to the question — see mark_syncing.
    mark_syncing(feed.id)
    background_tasks.add_task(run_initial_sync_task, feed.id)

    # Always 0: nothing has been fetched yet. Kept on the response for the
    # shape's sake; no caller of this route reads it.
    return FeedAddResult(feed=FeedOut.model_validate(feed), new_content_count=new_count)

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
