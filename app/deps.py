from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from app.auth import SESSION_KEY
from app.database import SessionLocal


class NotAuthenticated(Exception):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_login(request: Request) -> None:
    if not request.session.get(SESSION_KEY):
        raise NotAuthenticated()
