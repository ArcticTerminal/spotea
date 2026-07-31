from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.deps import get_db, require_login
from app.formatting import format_duration, format_size
from app.models import Content, Feed, User
from app.storage import collect_usage

router = APIRouter(dependencies=[Depends(require_login)])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size

DEFAULT_USER_ID = 1


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    feeds = db.query(Feed).filter(Feed.user_id == DEFAULT_USER_ID).order_by(Feed.added_at.desc()).all()
    content = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.user_id == DEFAULT_USER_ID)
        .order_by(Content.published_at.desc())
        .all()
    )
    usage = collect_usage(db, DEFAULT_USER_ID)
    user = db.get(User, DEFAULT_USER_ID)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"feeds": feeds, "content": content, "usage": usage, "audio_quality": user.audio_quality},
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
