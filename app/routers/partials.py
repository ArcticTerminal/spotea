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
    storage_summary_context,
)
from app.services.remote_detail import remote_channel_context, remote_playlist_context
from app.templating import templates
from app.youtube.urls import CHANNEL_ID_RE, PLAYLIST_ID_RE

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
    # The Downloads modal's own full list — deliberately not part of the
    # default refreshFragments() sweep, see fragments.js's
    # refreshDownloadsBody. Fetched only when the modal is actually opened
    # or an action inside it changes what it shows.
    return templates.TemplateResponse(
        request, "_fragment_downloads.html", downloads_context(db, profile.id)
    )


@router.get("/storage-summary", response_class=HTMLResponse)
def storage_summary_fragment(
    request: Request,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    # The cheap half of what downloads_fragment above used to return in one
    # response — just the Settings "Storage used" line, via
    # storage.usage_summary rather than collect_usage's per-row work. This is
    # what refreshFragments() actually calls after every save/favorite/play.
    return templates.TemplateResponse(
        request, "_fragment_storage_summary.html", storage_summary_context(db, profile.id)
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


# The two below render the same panel from YouTube rather than the database —
# Explore's recommendations drill into them (see services/remote_detail.py).
# No queue_thumbnail_caching: that works on Content rows, and nothing here has
# one yet. Both are slow (a live yt-dlp read) where the three above are not,
# which is why the client shows its loading state for them the same way.


@router.get("/detail/yt-playlist/{playlist_id}", response_class=HTMLResponse)
def remote_playlist_fragment(playlist_id: str, request: Request) -> HTMLResponse:
    # Validated before it's interpolated into a youtube.com URL — it arrives
    # as a raw path parameter.
    if not PLAYLIST_ID_RE.match(playlist_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playlist not found")

    context = remote_playlist_context(playlist_id)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this playlist")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)


@router.get("/detail/yt-channel/{channel_id}", response_class=HTMLResponse)
def remote_channel_fragment(
    channel_id: str,
    request: Request,
    avatar: str | None = None,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not CHANNEL_ID_RE.match(channel_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    # Whatever avatar the client already had rendered on the card it clicked
    # through from — see remote_channel_context's own docstring on why this
    # route has no avatar of its own to fetch. Untrusted input either way
    # (remote_channel_context re-validates it against this app's own
    # same-origin image routes before using it).
    context = remote_channel_context(db, profile.id, channel_id, avatar_url=avatar)
    if context is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Could not open this channel")
    return templates.TemplateResponse(request, "_fragment_detail.html", context)
