"""Explore: searching YouTube, and turning what comes back into something
playable without following anything.

Kept under the /artists prefix rather than its own, because that's the URL
shape the client already speaks — this is a code-organisation split, not an
API change. What makes these routes a group is that none of them involve
following a channel: search returns things the user doesn't have yet, and
both "listen" endpoints attach their rows to placeholder artists.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.models import Artist, Content, User
from app.schemas import (
    ChannelSearchResultOut,
    VideoAddCreate,
    VideoAddResult,
    VideoBatchCreate,
    VideoBatchResult,
    VideoSearchResultOut,
)
from app.storage import purge_content
from app.timeutil import utcnow
from app.youtube.music import search_artists, search_songs
from app.youtube.urls import CHANNEL_ID_RE

router = APIRouter(prefix="/explore", tags=["explore"], dependencies=[Depends(require_login)])


@router.get("/artists", response_model=list[ChannelSearchResultOut])
def search_feeds(q: str) -> list[ChannelSearchResultOut]:
    """Artists to follow.

    Asks the music catalogue for musicians rather than youtube.com for
    channels, which is the whole reason the "<Artist> - Topic" containers
    that search used to filter back out by name never turn up here.
    """
    query = q.strip()
    if not query:
        return []

    return [ChannelSearchResultOut(**result.__dict__) for result in search_artists(query)]


@router.get("/songs", response_model=list[VideoSearchResultOut])
def search_video_feeds(q: str) -> list[VideoSearchResultOut]:
    """Songs, from YouTube Music rather than youtube.com.

    Same endpoint, same response shape, different index — and the index is
    the point. youtube.com ranks a music query against everything it has,
    so an artist's name returns reaction videos and hour-long compilations
    alongside the tracks; YouTube Music only has tracks, and hands back the
    artist, album and duration already attached instead of leaving the row
    to say nothing but a title.

    There is no youtube.com fallback any more: this app only holds music,
    so a query YouTube Music has no answer for is a query with no answer
    here either, and an empty list says that honestly.
    """
    query = q.strip()
    if not query:
        return []

    return [VideoSearchResultOut(**result.__dict__) for result in search_songs(query)]


def _get_or_create_placeholder(
    db: Session, channel_id: str, channel_title: str | None, user_id: int
) -> Artist:
    """An Artist row for someone the user hasn't actually followed — exists
    only so a single track added via Explore has somewhere to attach
    (Content.artist_id is required). followed=False keeps it out of Library
    and the background refresh (see content_query.followed_artists) until the
    user follows them for real, which upgrades this same row in place (see
    services/artist_follow.py) instead of creating a duplicate. Keyed by the
    bare channel id, which is what both paths agree on.

    No avatar fetch: a placeholder artist's avatar is never displayed anywhere
    (Library and the channel-hero page are the only avatar consumers, and
    both are followed-only surfaces)."""
    existing = db.query(Artist).filter(Artist.user_id == user_id, Artist.channel_id == channel_id).first()
    if existing:
        return existing

    artist = Artist(user_id=user_id, channel_id=channel_id, name=channel_title, followed=False)
    db.add(artist)
    db.commit()
    db.refresh(artist)
    return artist


@router.post("/tracks", response_model=VideoAddResult, status_code=status.HTTP_201_CREATED)
def add_single_video(
    payload: VideoAddCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VideoAddResult:
    """Explore's "listen" action — adds exactly one video without following
    its channel. Always created as a preview (Content.is_preview=True): it
    plays through the normal player like any other content, but stays out of
    Library/New releases until the user favorites or saves it (see
    routers/content.py's add_favorite/add_saved).

    If this video already has a Content row for this user — a previous
    Explore preview, or a real upload from a followed channel — this isn't a
    conflict: it just means there's nothing to add, so hand back that row's
    id and let the player match/replay whatever was already downloaded."""
    existing_content = (
        db.query(Content)
        .filter(Content.user_id == user.id, Content.video_id == payload.video_id)
        .first()
    )
    if existing_content:
        return VideoAddResult(content_id=existing_content.id)

    artist = _get_or_create_placeholder(db, payload.channel_id, payload.channel_title, user.id)

    content = Content(
        artist_id=artist.id,
        user_id=user.id,
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


def _preview_content(artist_id: int, user_id: int, item) -> Content:
    """A preview row from something Explore already has full metadata for.
    Same shape add_single_video builds — see its comments for why the
    thumbnail is stored as-is and why published_at is "now"."""
    return Content(
        artist_id=artist_id,
        user_id=user_id,
        video_id=item.video_id,
        title=item.title,
        thumbnail_url=item.thumbnail_url,
        duration_seconds=item.duration_seconds,
        published_at=utcnow(),
        is_preview=True,
    )


@router.post("/tracks/batch", response_model=VideoBatchResult, status_code=status.HTTP_201_CREATED)
def add_video_batch(
    payload: VideoBatchCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VideoBatchResult:
    """Turns a whole remote playlist or channel listing into playable rows in
    one go — what "Play all" (and clicking any row) on those pages calls
    before handing the queue its ids.

    Makes **no network calls at all**, which is the reason it can be a single
    synchronous request over fifty tracks: unlike add_single_video, every
    field is already known. The client got them from the same fetch that
    rendered the list, `channel_id` included — and a playlist/channel page's
    per-entry channel attribution is the real uploader, so there's nothing to
    resolve.

    Rows that already exist (an earlier preview, or a real
    upload from a followed channel) are reused rather than duplicated, the
    same way add_single_video treats them. Order in equals order out: the
    caller uses it directly as the play queue.
    """
    items = [item for item in payload.items if CHANNEL_ID_RE.match(item.channel_id)]
    if not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No playable videos given")

    # Three bulk lookups instead of two queries per track — this runs over a
    # whole playlist, and the per-item version of it was the only thing that
    # made a fifty-track "Play all" slow.
    existing_content = {
        content.video_id: content
        for content in db.query(Content).filter(
            Content.user_id == user.id,
            Content.video_id.in_([item.video_id for item in items]),
        )
    }
    wanted_channel_ids = {item.channel_id for item in items}
    artists_by_channel = {
        artist.channel_id: artist
        for artist in db.query(Artist).filter(
            Artist.user_id == user.id, Artist.channel_id.in_(wanted_channel_ids)
        )
    }

    for item in items:
        if item.channel_id not in artists_by_channel:
            # Same placeholder contract as _get_or_create_placeholder above;
            # built inline here so the whole batch is one flush.
            artist = Artist(
                user_id=user.id,
                channel_id=item.channel_id,
                name=item.channel_title,
                followed=False,
            )
            db.add(artist)
            artists_by_channel[item.channel_id] = artist
    db.flush()

    created: dict[str, Content] = {}
    for item in items:
        if item.video_id in existing_content or item.video_id in created:
            continue
        artist = artists_by_channel[item.channel_id]
        content = _preview_content(artist.id, user.id, item)
        db.add(content)
        created[item.video_id] = content

    db.commit()

    resolved = {**{k: v.id for k, v in existing_content.items()}, **{k: v.id for k, v in created.items()}}
    return VideoBatchResult(content_ids=[resolved[item.video_id] for item in items])


@router.delete("/tracks/{content_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_single_video(
    content_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    """Removes a video added via Explore outright (unlike DELETE
    /content/{id}, which only resets download status) — used both to dismiss
    a preview early and to remove something already kept. Only for content on
    a followed=False artist; a real follow's content comes off through
    unfollowing the channel, not this."""
    content = (
        db.query(Content)
        .join(Artist)
        .filter(Content.id == content_id, Content.user_id == user.id, Artist.followed.is_(False))
        .first()
    )
    if not content:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    artist_id = content.artist_id
    purge_content(db, content)
    db.commit()

    remaining = db.query(func.count(Content.id)).filter(Content.artist_id == artist_id).scalar()
    if remaining == 0:
        db.query(Artist).filter(Artist.id == artist_id).delete()
        db.commit()
