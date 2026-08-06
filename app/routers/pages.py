from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.content_query import query_content_page
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


def _home_shelf_query(db: Session):
    return (
        db.query(Content).options(joinedload(Content.feed)).filter(Content.user_id == DEFAULT_USER_ID)
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    feeds = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).order_by(Feed.added_at.desc()).all()
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

    library_items, library_page, library_total_pages = query_content_page(db, DEFAULT_USER_ID)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "feeds": feeds,
            "content": library_items,
            "library_page": library_page,
            "library_total_pages": library_total_pages,
            "usage": usage,
            "audio_quality": user.audio_quality,
            "home_recently_played": home_recently_played,
            "home_new_uploads": home_new_uploads,
            "home_favorites": home_favorites,
            "home_saved": home_saved,
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
