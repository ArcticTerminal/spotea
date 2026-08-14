from collections.abc import Iterable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.app_settings import get_app_settings
from app.content_query import DEFAULT_PAGE_SIZE, new_upload_filter, query_content_page
from app.deps import get_current_profile, get_db, require_login
from app.feed_sync import cache_thumbnail
from app.models import Content, Feed, User
from app.page_context import (
    downloads_context,
    home_context,
    home_shelf_items,
    library_context,
)
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])

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


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """index.html is the whole app — Home, Library, Explore and Settings are
    tab panels in one document (see its inline head script), so this single
    route builds the context for all of them at once.

    Each region's context comes from app/page_context.py, shared with the
    fragment endpoints (routers/partials.py) that re-render that same region
    later. The full page and a refresh of one part of it therefore can't
    disagree about what it contains."""
    home = home_context(db, profile.id)
    _queue_thumbnail_caching(background_tasks, home_shelf_items(home))

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "audio_quality": profile.audio_quality,
            "feed_refresh_interval_minutes": get_app_settings(db).feed_refresh_interval_minutes,
            **home,
            **library_context(db, profile.id),
            **downloads_context(db, profile.id),
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
