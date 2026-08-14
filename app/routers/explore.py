"""Explore: searching YouTube and grabbing a single video from it.

Kept under the /feeds prefix rather than its own, because that's the URL
shape the client already speaks — this is a code-organisation split, not an
API change. What makes these routes a group is that none of them involve
following a channel: search returns things the user doesn't have yet, and
"listen" adds exactly one video behind a placeholder feed.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_current_profile, get_db, require_login
from app.models import Content, Feed, User
from app.schemas import (
    ChannelSearchResultOut,
    VideoAddCreate,
    VideoAddResult,
    VideoSearchResultOut,
)
from app.storage import purge_content
from app.timeutil import utcnow
from app.youtube.extract import resolve_video_channel
from app.youtube.search import search_channels, search_videos
from app.youtube.urls import channel_feed_url

router = APIRouter(prefix="/feeds", tags=["explore"], dependencies=[Depends(require_login)])


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
    refresh scheduler (see content_query.followed_feeds) until the user
    follows the channel for real, which upgrades this same row in place (see
    services/feed_add.py's create_feed_from_rss_url) instead of creating a duplicate — rss_url must
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
        published_at=utcnow(),
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
    purge_content(db, content)
    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.feed_id == feed_id).scalar()
    if remaining == 0:
        db.query(Feed).filter(Feed.id == feed_id).delete()
        db.commit()
