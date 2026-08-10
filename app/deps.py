from collections.abc import Generator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import PROFILE_SESSION_KEY, SESSION_KEY
from app.database import SessionLocal
from app.models import User


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


def get_current_profile(request: Request, db: Session = Depends(get_db)) -> User:
    """Resolves the active profile from the session, self-healing whenever the
    stored id is missing or stale (pre-upgrade session, or the active profile
    was deleted from another device) by falling back to the first profile —
    no forced re-login or explicit upgrade step needed."""
    profile_id = request.session.get(PROFILE_SESSION_KEY)
    profile = db.get(User, profile_id) if profile_id else None
    if profile is None:
        profile = db.query(User).order_by(User.id).first()
        request.session[PROFILE_SESSION_KEY] = profile.id
    return profile
