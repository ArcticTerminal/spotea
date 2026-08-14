from collections.abc import Iterable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.app_settings import get_app_settings
from app.content_query import (
    DEFAULT_PAGE_SIZE,
    followed_feeds,
    new_upload_filter,
    query_content_page,
)
from app.deps import get_current_profile, get_db, require_login
from app.feed_sync import cache_thumbnail
from app.models import Content, Feed, User
from app.storage import collect_usage
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def _queue_thumbnail_caching(background_tasks: BackgroundTasks, items: Iterable[Content]) -> None:
    """Caches thumbnails for whatever's actually being rendered in this
    response — a Home shelf, a Library list's current page, a channel page's
    current page — rather than eagerly sweeping ahead of actual browsing (a
    prior version of this did that at startup; on a large library it meant
    downloading and storing thumbnails for things nobody was looking at yet).
    This render still goes out with the original YouTube URL for anything
    not yet cached — the queued task (see feed_sync.cache_thumbnail) only
    ever benefits the *next* time this same content is rendered, anywhere.
    Deduped per call so a video appearing in more than one shelf here isn't
    queued twice."""
    seen: set[str] = set()
    for item in items:
        if not item.thumbnail_url or "ytimg.com" not in item.thumbnail_url:
            continue
        if item.video_id in seen:
            continue
        seen.add(item.video_id)
        background_tasks.add_task(cache_thumbnail, item.video_id, item.thumbnail_url)


def _home_shelf_query(db: Session, user_id: int):
    # is_preview excludes Explore videos not yet favorited/saved — see
    # routers/explore.py's add_single_video and routers/content.py's
    # add_favorite/add_saved. Listening to one shouldn't look like it's
    # already saved.
    return (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.user_id == user_id, Content.is_preview.is_(False))
    )


def _home_shelves(db: Session, user_id: int) -> dict[str, list[Content]]:
    """The four horizontal rows on the Home tab.

    Each is its own bounded query. This used to be one `.all()` over every
    content row the user had ever had, sliced per shelf in Python, which got
    very slow once backfilling full channel histories pushed that past a few
    thousand rows.
    """
    return {
        "home_new_uploads": (
            _home_shelf_query(db, user_id)
            .filter(new_upload_filter())
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        # Not built on _home_shelf_query: an Explore preview that's actually
        # been played earns a spot here even though it's still is_preview
        # (never favorited/saved) — otherwise playing something from Explore
        # and coming back to Home would make it look like nothing happened.
        # New uploads/Favorites/Saved have no such case, since none of those
        # imply the user ever listened.
        "home_recently_played": (
            db.query(Content)
            .options(joinedload(Content.feed))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .order_by(Content.last_played_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        "home_favorites": (
            _home_shelf_query(db, user_id)
            .filter(Content.is_favorite.is_(True))
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        "home_saved": (
            _home_shelf_query(db, user_id)
            .filter(Content.is_saved.is_(True))
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
    }


def _library_counts(db: Session, user_id: int) -> dict:
    """Per-channel video counts for Library's grid, plus the four pinned
    virtual-playlist tiles' counts.

    Each count matches its page's own filter exactly (see content_query.py's
    query_content_page) — a tile saying "12 videos" that opens onto a list
    of 9 is worse than no count at all.
    """
    return {
        # One grouped count covers every channel's card, rather than a
        # per-feed query each.
        "channel_video_counts": dict(
            db.query(Content.feed_id, func.count(Content.id))
            .filter(Content.user_id == user_id)
            .group_by(Content.feed_id)
            .all()
        ),
        "favorites_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_favorite.is_(True))
            .scalar()
        ),
        "saved_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_saved.is_(True))
            .scalar()
        ),
        "new_uploads_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_preview.is_(False), new_upload_filter())
            .scalar()
        ),
        "recently_played_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .scalar()
        ),
    }


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """index.html is the whole app — Home, Library, Explore and Settings are
    tab panels in one document (see its inline head script), so this single
    route builds the context for all of them at once."""
    feeds = followed_feeds(db, profile.id).all()
    shelves = _home_shelves(db, profile.id)

    _queue_thumbnail_caching(background_tasks, [item for shelf in shelves.values() for item in shelf])

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "feeds": feeds,
            # feeds is already newest-first — Home's chip row is just the
            # most recently followed few (with 100+ channels followed, the
            # full list made that row an endless horizontal scroll);
            # Library's grid below still gets every channel via `feeds`.
            "home_recent_channels": feeds[:HOME_CHANNEL_LIMIT],
            "usage": collect_usage(db, profile.id),
            "audio_quality": profile.audio_quality,
            "feed_refresh_interval_minutes": get_app_settings(db).feed_refresh_interval_minutes,
            **shelves,
            **_library_counts(db, profile.id),
        },
    )


def _content_list_page(
    request: Request,
    db: Session,
    background_tasks: BackgroundTasks,
    *,
    user_id: int,
    kind: str,
    is_match: ColumnElement[bool],
    filter_value: str,
    title: str,
    empty_message: str,
    page: int,
) -> HTMLResponse:
    """Shared by /favorites, /saved, /new-uploads and /recently-played —
    each is just query_content_page with a fixed filter, rendered through
    content_list.html (the same
    track-list/pagination partials channel.html uses, minus its
    single-channel avatar hero)."""
    video_count = db.query(func.count(Content.id)).filter(Content.user_id == user_id, is_match).scalar()
    items, page, total_pages = query_content_page(db, user_id, page=page, filter=filter_value)
    # Only ever this page's items — paginating to page 2 queues page 2's
    # thumbnails, not the whole list, matching how a user actually browses.
    _queue_thumbnail_caching(background_tasks, items)

    return templates.TemplateResponse(
        request,
        "content_list.html",
        {
            "kind": kind,
            "title": title,
            "empty_message": empty_message,
            "video_count": video_count,
            "content": items,
            "page": page,
            "total_pages": total_pages,
            "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
            "base_url": f"/{kind}",
        },
    )


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        background_tasks,
        user_id=profile.id,
        kind="favorites",
        is_match=Content.is_favorite.is_(True),
        filter_value="__favorites__",
        title="Favorites",
        empty_message="No favorites yet.",
        page=page,
    )


@router.get("/saved", response_class=HTMLResponse)
def saved_page(
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        background_tasks,
        user_id=profile.id,
        kind="saved",
        is_match=Content.is_saved.is_(True),
        filter_value="__saved__",
        title="Saved for later",
        empty_message="Nothing saved yet.",
        page=page,
    )


@router.get("/new-uploads", response_class=HTMLResponse)
def new_uploads_page(
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        background_tasks,
        user_id=profile.id,
        kind="new-uploads",
        is_match=new_upload_filter(),
        filter_value="__new_uploads__",
        title="New Uploads",
        empty_message="No new uploads yet.",
        page=page,
    )


@router.get("/recently-played", response_class=HTMLResponse)
def recently_played_page(
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        background_tasks,
        user_id=profile.id,
        kind="recently-played",
        is_match=Content.last_played_at.isnot(None),
        filter_value="__played__",
        title="Recently Played",
        empty_message="Nothing played yet.",
        page=page,
    )


@router.get("/channel/{feed_id}", response_class=HTMLResponse)
def channel_page(
    feed_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == profile.id).first()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    video_count = db.query(func.count(Content.id)).filter(
        Content.feed_id == feed_id, Content.user_id == profile.id
    ).scalar()
    items, page, total_pages = query_content_page(db, profile.id, page=page, feed_id=feed_id)
    _queue_thumbnail_caching(background_tasks, items)

    return templates.TemplateResponse(
        request,
        "channel.html",
        {
            "feed": feed,
            "video_count": video_count,
            "content": items,
            "page": page,
            "total_pages": total_pages,
            "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
        },
    )


@router.get("/player/{content_id}", response_class=HTMLResponse)
def player_page(
    content_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    content = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.id == content_id, Content.user_id == profile.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    _queue_thumbnail_caching(background_tasks, [content])

    # No redirect for not-yet-downloaded content: the player itself kicks off the
    # download and shows a preparing state until the audio is ready.
    return templates.TemplateResponse(request, "player.html", {"content": content})
