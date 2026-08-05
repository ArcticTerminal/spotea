from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.deps import NotAuthenticated, require_login
from app.migrations import run_migrations
from app.models import User
from app.routers import auth as auth_router
from app.routers import content as content_router
from app.routers import feeds as feeds_router
from app.routers import pages as pages_router
from app.routers import settings as settings_router
from app.routers import storage as storage_router

DEFAULT_USER_ID = 1


def _ensure_default_user() -> None:
    with SessionLocal() as db:
        if db.get(User, DEFAULT_USER_ID) is None:
            db.add(User(id=DEFAULT_USER_ID, name="local"))
            db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    _ensure_default_user()
    yield


app = FastAPI(title="spotifrei", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="spotifrei_session",
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
app.include_router(content_router.router)
app.include_router(storage_router.router)
app.include_router(settings_router.router)
app.include_router(pages_router.router)


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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

    return FileResponse(path, media_type="image/jpeg")
