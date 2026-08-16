import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.app_settings import get_app_settings
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.deps import NotAuthenticated, require_login
from app.migrations import run_migrations
from app.routers import auth as auth_router
from app.routers import content as content_router
from app.routers import debug as debug_router
from app.routers import explore as explore_router
from app.routers import feeds as feeds_router
from app.routers import pages as pages_router
from app.routers import partials as partials_router
from app.routers import profiles as profiles_router
from app.routers import recommendations as recommendations_router
from app.routers import settings as settings_router
from app.routers import storage as storage_router
from app.scheduler import run_scheduler

# uvicorn configures only its own loggers, leaving the root at WARNING, so
# every logger.info() this app makes went nowhere. That's not cosmetic: the
# download ladder logged which attempt failed and why at INFO, and a day of
# "why does it always take three tries?" was spent without that line ever
# reaching a handler. uvicorn's own loggers don't propagate, so this doesn't
# duplicate the access log.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")


def _ensure_app_settings() -> None:
    # get_app_settings creates the row if it's missing, so this is just
    # "do that once at startup" rather than leaving the first request to
    # pay for it.
    with SessionLocal() as db:
        get_app_settings(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    _ensure_app_settings()

    scheduler_task = asyncio.create_task(run_scheduler())
    try:
        yield
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task


app = FastAPI(title="Spotea", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="spotea_session",
    same_site="lax",
)

class RevalidatingStaticFiles(StaticFiles):
    """Static files that must be revalidated before reuse.

    Starlette's StaticFiles sends ETag/Last-Modified but no Cache-Control, and
    browsers then fall back to *heuristic* freshness: they serve the cached copy
    for a while without asking the server at all. After an upgrade
    (`docker compose up -d --build`) that means users can keep running stale
    CSS/JS against new templates — which renders as a subtly (or completely)
    broken UI. "no-cache" still allows caching, it just forces a revalidation
    request; unchanged files come back as a cheap 304.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory="app/static"), name="static")

app.include_router(auth_router.router)
app.include_router(feeds_router.router)
app.include_router(explore_router.router)
app.include_router(content_router.router)
app.include_router(storage_router.router)
app.include_router(settings_router.router)
app.include_router(recommendations_router.router)
app.include_router(profiles_router.router)
app.include_router(debug_router.router)
app.include_router(partials_router.router)
app.include_router(pages_router.router)


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Served from the root rather than under /static/js/ — a service worker
    # can only ever control paths at or below its own URL, so /static/js/sw.js
    # would default to a /static/js/ scope instead of the whole app. No
    # require_login: the browser's installability checks may fetch this
    # before there's a session to send.
    return FileResponse(
        "app/static/js/sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache"},
    )


# Both avatars and thumbnails are content-addressed (filename is the
# channel/video id, and download_avatar/download_thumbnail never overwrite
# an existing file — see their on-disk check) — so unlike RevalidatingStaticFiles'
# app code, a cached copy is never stale and can be trusted indefinitely
# without a revalidation round trip.
_IMAGE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/avatars/{filename}", dependencies=[Depends(require_login)])
def get_avatar(filename: str) -> FileResponse:
    # Downloaded and re-served from our own origin rather than hotlinked
    # from Google's CDN — see downloader.download_avatar for why. filename
    # is always "{channel_id}.jpg" from that function, but guard against
    # path traversal since it still arrives as attacker-controlled input.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    path = settings.avatars_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE_HEADERS)


@app.get("/thumbnails/{filename}", dependencies=[Depends(require_login)])
def get_thumbnail(filename: str) -> FileResponse:
    # Same deal as get_avatar above — see downloader.download_thumbnail.
    # filename is always "{video_id}.jpg" from that function, but guard
    # against path traversal since it still arrives as attacker-controlled
    # input.
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    path = settings.thumbnails_dir / filename
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE_HEADERS)
