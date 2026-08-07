from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.content_query import DEFAULT_PAGE_SIZE, query_content_page
from app.deps import get_db, require_login
from app.formatting import format_duration, format_size
from app.models import Content, Feed, User
from app.storage import collect_usage

router = APIRouter(dependencies=[Depends(require_login)])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size

DEFAULT_USER_ID = 1
HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def _home_shelf_query(db: Session):
    return (
        db.query(Content).options(joinedload(Content.feed)).filter(Content.user_id == DEFAULT_USER_ID)
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    feeds = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).order_by(Feed.added_at.desc()).all()
    # feeds is already newest-first — Home's chip row is just the most
    # recently followed few (with 100+ channels followed, the full list made
    # that row an endless horizontal scroll); Library's grid below still
    # gets every channel via `feeds` itself.
    home_recent_channels = feeds[:HOME_CHANNEL_LIMIT]
    usage = collect_usage(db, DEFAULT_USER_ID)
    user = db.get(User, DEFAULT_USER_ID)

    # Each shelf (and the Library grid below) is its own bounded query now —
    # this used to be one `.all()` over every content row the user has ever
    # had (sliced in Python per shelf), which got very slow once backfilling
    # full channel histories pushed that past a few thousand rows.
    home_new_uploads = (
        _home_shelf_query(db).order_by(Content.published_at.desc()).limit(HOME_SHELF_LIMIT).all()
    )
    home_recently_played = (
        _home_shelf_query(db)
        .filter(Content.last_played_at.isnot(None))
        .order_by(Content.last_played_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )
    home_favorites = (
        _home_shelf_query(db)
        .filter(Content.is_favorite.is_(True))
        .order_by(Content.published_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )
    home_saved = (
        _home_shelf_query(db)
        .filter(Content.is_saved.is_(True))
        .order_by(Content.published_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )

    # Library is a grid of channels, not videos — one cheap grouped count
    # query covers every channel's card instead of a per-feed query each.
    channel_video_counts = dict(
        db.query(Content.feed_id, func.count(Content.id))
        .filter(Content.user_id == DEFAULT_USER_ID)
        .group_by(Content.feed_id)
        .all()
    )
    favorites_count = (
        db.query(func.count(Content.id))
        .filter(Content.user_id == DEFAULT_USER_ID, Content.is_favorite.is_(True))
        .scalar()
    )
    saved_count = (
        db.query(func.count(Content.id))
        .filter(Content.user_id == DEFAULT_USER_ID, Content.is_saved.is_(True))
        .scalar()
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "feeds": feeds,
            "home_recent_channels": home_recent_channels,
            "channel_video_counts": channel_video_counts,
            "favorites_count": favorites_count,
            "saved_count": saved_count,
            "usage": usage,
            "audio_quality": user.audio_quality,
            "home_recently_played": home_recently_played,
            "home_new_uploads": home_new_uploads,
            "home_favorites": home_favorites,
            "home_saved": home_saved,
        },
    )


def _content_list_page(
    request: Request,
    db: Session,
    *,
    kind: str,
    is_match: ColumnElement[bool],
    filter_value: str,
    title: str,
    empty_message: str,
    page: int,
) -> HTMLResponse:
    """Shared by /favorites and /saved — both are just query_content_page
    with a fixed filter, rendered through content_list.html (the same
    track-list/pagination partials channel.html uses, minus its
    single-channel avatar hero)."""
    video_count = db.query(func.count(Content.id)).filter(Content.user_id == DEFAULT_USER_ID, is_match).scalar()
    items, page, total_pages = query_content_page(db, DEFAULT_USER_ID, page=page, filter=filter_value)

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
def favorites_page(request: Request, page: int = 1, db: Session = Depends(get_db)) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        kind="favorites",
        is_match=Content.is_favorite.is_(True),
        filter_value="__favorites__",
        title="Favorites",
        empty_message="No favorites yet.",
        page=page,
    )


@router.get("/saved", response_class=HTMLResponse)
def saved_page(request: Request, page: int = 1, db: Session = Depends(get_db)) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        kind="saved",
        is_match=Content.is_saved.is_(True),
        filter_value="__saved__",
        title="Saved for later",
        empty_message="Nothing saved yet.",
        page=page,
    )


@router.get("/channel/{feed_id}", response_class=HTMLResponse)
def channel_page(
    feed_id: int, request: Request, page: int = 1, db: Session = Depends(get_db)
) -> HTMLResponse:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == DEFAULT_USER_ID).first()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    video_count = db.query(func.count(Content.id)).filter(
        Content.feed_id == feed_id, Content.user_id == DEFAULT_USER_ID
    ).scalar()
    items, page, total_pages = query_content_page(db, DEFAULT_USER_ID, page=page, feed_id=feed_id)

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
def player_page(content_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    content = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.id == content_id, Content.user_id == DEFAULT_USER_ID)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    # No redirect for not-yet-downloaded content: the player itself kicks off the
    # download and shows a preparing state until the audio is ready.
    return templates.TemplateResponse(request, "player.html", {"content": content})
