"""Re-rendering one region of index.html on demand.

index.html is rendered once, server-side, and nothing revalidates it. Every
action that changes what it shows — saving, favoriting, playing, finishing a
download, clearing storage — therefore had to be reflected into the DOM by
hand, and the app had accumulated six separate hand-written patchers doing
exactly that. Each new surface needed another one, and each was free to miss
a case; a shelf that only updated on some of the paths that should have
touched it looked identical to one that worked.

These endpoints return the same markup index.html renders for that region,
from the same context functions (see app/page_context.py). The client swaps
it in wholesale. That trades a small request for not having to know, per
action, which parts of the page it invalidated — and it picks up changes
this tab didn't make at all (the background refresh, another device, another
tab) for free.

Each response is one or more <template data-target="…"> blocks, so a single
render can update several places at once.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.deps import get_current_profile, get_db, require_login
from app.models import User
from app.page_context import (
    channel_detail_context,
    downloads_context,
    home_context,
    library_context,
    playlist_detail_context,
    queue_thumbnail_caching,
)
from app.templating import templates

router = APIRouter(prefix="/partials", tags=["partials"], dependencies=[Depends(require_login)])


@router.get("/home", response_class=HTMLResponse)
def home_fragment(
    request: Request,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # No thumbnail caching queued here, unlike the full page render: a
    # fragment refresh only ever shows rows some earlier render already
    # queued, so doing it again would be a second pass over the same videos.
    return templates.TemplateResponse(request, "_fragment_home.html", home_context(db, profile.id))


@router.get("/library", response_class=HTMLResponse)
def library_fragment(
    request: Request,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(request, "_fragment_library.html", library_context(db, profile.id))


@router.get("/downloads", response_class=HTMLResponse)
def downloads_fragment(
    request: Request,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_fragment_downloads.html", downloads_context(db, profile.id)
    )


@router.get("/detail/channel/{feed_id}", response_class=HTMLResponse)
def channel_detail_fragment(
    feed_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = channel_detail_context(db, profile.id, feed_id, page)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    queue_thumbnail_caching(background_tasks, context["content"])
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


@router.get("/detail/playlist/{kind}", response_class=HTMLResponse)
def playlist_detail_fragment(
    kind: str,
    request: Request,
    background_tasks: BackgroundTasks,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    context = playlist_detail_context(db, profile.id, kind, page)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown playlist")
    queue_thumbnail_caching(background_tasks, context["content"])
    return templates.TemplateResponse(request, "_fragment_detail.html", context)
