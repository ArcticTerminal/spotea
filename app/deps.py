from collections.abc import Generator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import ACCOUNT_SESSION_KEY, PROFILE_SESSION_KEY
from app.database import SessionLocal
from app.models import Account, User


class NotAuthenticated(Exception):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_login(request: Request) -> None:
    if not request.session.get(ACCOUNT_SESSION_KEY):
        raise NotAuthenticated()


def get_current_account(request: Request, db: Session = Depends(get_db)) -> Account:
    """Resolves the logged-in account from the session. Deliberately never
    self-heals to "some other account" the way get_current_profile below
    self-heals across profiles within one account — a missing/stale/forged
    account id is just not authenticated, full stop."""
    account_id = request.session.get(ACCOUNT_SESSION_KEY)
    account = db.get(Account, account_id) if account_id else None
    if account is None:
        raise NotAuthenticated()
    return account


def get_current_profile(
    request: Request, account: Account = Depends(get_current_account), db: Session = Depends(get_db)
) -> User:
    """Resolves the active profile from the session, scoped to the current
    account and self-healing whenever the stored id is missing, stale, or —
    critically, now that accounts are real — belongs to a *different*
    account than the one logged in (a forged/stale cookie must never resolve
    to another account's profile). Falls back to that account's first
    profile, no forced re-login needed."""
    profile_id = request.session.get(PROFILE_SESSION_KEY)
    profile = db.get(User, profile_id) if profile_id else None
    if profile is None or profile.account_id != account.id:
        profile = db.query(User).filter(User.account_id == account.id).order_by(User.id).first()
        if profile is None:
            # Shouldn't happen: registration always creates one profile, and
            # delete_profile refuses to delete an account's last one.
            raise NotAuthenticated()
        request.session[PROFILE_SESSION_KEY] = profile.id
    return profile
