from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.deps import get_db, require_login
from app.models import Content, Feed
from app.rss import ChannelResolutionError, InvalidFeedError, fetch_feed, resolve_feed_url
from app.schemas import FeedAddResult, FeedCreate, FeedOut, RefreshResult

router = APIRouter(prefix="/feeds", tags=["feeds"], dependencies=[Depends(require_login)])

DEFAULT_USER_ID = 1


def _sync_feed_content(db: Session, feed: Feed) -> int:
    """Fetch a feed's RSS and insert any content rows not already known. Returns new-row count."""
    try:
        parsed = fetch_feed(feed.rss_url)
    except InvalidFeedError:
        return 0

    if not feed.channel_title and parsed.channel_title:
        feed.channel_title = parsed.channel_title

    incoming_ids = [entry.video_id for entry in parsed.entries]
    existing_ids = {
        video_id
        for (video_id,) in db.query(Content.video_id).filter(
            Content.feed_id == feed.id, Content.video_id.in_(incoming_ids)
        )
    }

    new_count = 0
    for entry in parsed.entries:
        if entry.video_id in existing_ids:
            continue
        db.add(
            Content(
                feed_id=feed.id,
                user_id=feed.user_id,
                video_id=entry.video_id,
                title=entry.title,
                thumbnail_url=entry.thumbnail_url,
                published_at=entry.published_at,
            )
        )
        new_count += 1

    db.commit()
    return new_count


@router.post("", response_model=FeedAddResult, status_code=status.HTTP_201_CREATED)
def add_feed(payload: FeedCreate, db: Session = Depends(get_db)) -> FeedAddResult:
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

    return FeedAddResult(feed=FeedOut.model_validate(feed), new_content_count=new_count)


@router.get("", response_model=list[FeedOut])
def list_feeds(db: Session = Depends(get_db)) -> list[Feed]:
    return db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).order_by(Feed.added_at.desc()).all()


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feed(feed_id: int, db: Session = Depends(get_db)) -> None:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == DEFAULT_USER_ID).first()
    if not feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")
    db.delete(feed)
    db.commit()


@router.post("/refresh", response_model=RefreshResult)
def refresh_feeds(db: Session = Depends(get_db)) -> RefreshResult:
    feeds = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).all()
    total_new = sum(_sync_feed_content(db, feed) for feed in feeds)
    return RefreshResult(new_content_count=total_new)
