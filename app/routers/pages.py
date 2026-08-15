from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.app_settings import get_app_settings
from app.deps import get_current_profile, get_db, require_login
from app.models import User
from app.page_context import (
    downloads_context,
    home_context,
    home_shelf_items,
    library_context,
    queue_thumbnail_caching,
)
from app.templating import templates

router = APIRouter(dependencies=[Depends(require_login)])


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
    queue_thumbnail_caching(background_tasks, home_shelf_items(home))

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


# Channel, the four pinned playlists, and the player all moved in-page (see
# home/detail.js, home/overlay.js) — index.html's hash router handles them
# now. These redirects exist only so a link or bookmark from before that
# change still lands somewhere real.
@router.get("/favorites")
def favorites_redirect() -> RedirectResponse:
    return RedirectResponse("/#favorites")


@router.get("/saved")
def saved_redirect() -> RedirectResponse:
    return RedirectResponse("/#saved")


@router.get("/new-uploads")
def new_uploads_redirect() -> RedirectResponse:
    return RedirectResponse("/#new-uploads")


@router.get("/recently-played")
def recently_played_redirect() -> RedirectResponse:
    return RedirectResponse("/#recently-played")


@router.get("/channel/{feed_id}")
def channel_redirect(feed_id: int) -> RedirectResponse:
    return RedirectResponse(f"/#channel/{feed_id}")


@router.get("/player/{content_id}")
def player_redirect(content_id: int) -> RedirectResponse:
    return RedirectResponse(f"/#player/{content_id}")
