from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.deps import NotAuthenticated
from app.models import User
from app.routers import auth as auth_router
from app.routers import content as content_router
from app.routers import feeds as feeds_router
from app.routers import pages as pages_router

DEFAULT_USER_ID = 1


def _ensure_default_user() -> None:
    with SessionLocal() as db:
        if db.get(User, DEFAULT_USER_ID) is None:
            db.add(User(id=DEFAULT_USER_ID, name="local"))
            db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_default_user()
    yield


app = FastAPI(title="spotifrei", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="spotifrei_session",
    same_site="lax",
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router.router)
app.include_router(feeds_router.router)
app.include_router(content_router.router)
app.include_router(pages_router.router)


@app.exception_handler(NotAuthenticated)
async def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
