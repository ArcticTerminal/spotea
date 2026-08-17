import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.deps import NotAuthenticated, require_login
from app.middleware import install as install_middleware
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

# uvicorn configures only its own loggers, leaving the root at WARNING, so
# every logger.info() this app makes went nowhere. That's not cosmetic: the
# download ladder logged which attempt failed and why at INFO, and a day of
# "why does it always take three tries?" was spent without that line ever
# reaching a handler. uvicorn's own loggers don't propagate, so this doesn't
# duplicate the access log.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)

    # Started/stopped through the scheduler module rather than held as a local
    # task here, so /health can ask it whether the loop is still alive.
    scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="Spotea", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="spotea_session",
    same_site="lax",
    # Off by default because the app is reachable over plain HTTP on a LAN,
    # which is how most installs run it; set SESSION_HTTPS_ONLY=true when it's
    # behind a TLS-terminating proxy so the session cookie can't leak over an
    # unencrypted hop.
    https_only=settings.session_https_only,
)

# Added after SessionMiddleware, so these run outside it — see middleware.install.
install_middleware(app)

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
def health(response: Response) -> dict[str, object]:
    """Liveness that can actually fail.

    This used to return a hardcoded {"status": "ok"} — true whether or not the
    database was reachable and whether or not background refreshing had
    stopped. A probe that cannot fail is not a probe; it reported healthy
    through exactly the outage it existed to catch (see scheduler.run_scheduler).

    Deliberately cheap enough for a container healthcheck to poll: one
    `SELECT 1` and an in-process flag, touching no user data and doing no
    per-profile work.
    """
    checks = {"database": _database_reachable(), "scheduler": scheduler.is_alive()}
    healthy = all(checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", **checks}


def _database_reachable() -> bool:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Health check: database unreachable")
        return False


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
