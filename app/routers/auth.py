import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import ACCOUNT_SESSION_KEY, PROFILE_SESSION_KEY, hash_password, verify_password
from app.deps import get_db
from app.models import Account, User
from app.templating import templates

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
# bcrypt silently truncates/ignores anything past 72 bytes — two passwords
# sharing that prefix would otherwise verify as equal.
MAX_PASSWORD_LENGTH = 72


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get(ACCOUNT_SESSION_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "email": ""})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)
):
    normalized_email = email.strip().lower()
    account = db.query(Account).filter(Account.email == normalized_email).first()
    # Same generic message either way — doesn't reveal whether the email
    # itself is registered.
    if account is None or not verify_password(password, account.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password", "email": email}, status_code=401
        )
    request.session[ACCOUNT_SESSION_KEY] = account.id
    # Land back on whichever profile was active before logout — logout
    # clears the whole session, so PROFILE_SESSION_KEY itself never survives
    # it. If this is unset (never switched, or that profile's since been
    # deleted), get_current_profile (deps.py) self-heals to this account's
    # first profile on the first profile-scoped request, same as it always did.
    if account.last_active_profile_id is not None:
        request.session[PROFILE_SESSION_KEY] = account.last_active_profile_id
    # #home overrides whatever tab localStorage remembers from a previous
    # session (see index.html's inline head script) — a fresh login should
    # always land on Home, not wherever this browser last happened to be.
    return RedirectResponse(url="/#home", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.session.get(ACCOUNT_SESSION_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"error": None, "email": ""})


def _validate_registration(email: str, password: str, confirm_password: str, db: Session) -> str | None:
    if not EMAIL_RE.match(email):
        return "Enter a valid email address"
    if not (MIN_PASSWORD_LENGTH <= len(password.encode("utf-8")) <= MAX_PASSWORD_LENGTH):
        return f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters"
    if password != confirm_password:
        return "Passwords do not match"
    if db.query(Account).filter(Account.email == email).first() is not None:
        return "Email already registered"
    return None


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_email = email.strip().lower()
    error = _validate_registration(normalized_email, password, confirm_password, db)
    if error:
        return templates.TemplateResponse(
            request, "register.html", {"error": error, "email": email}, status_code=400
        )

    account = Account(email=normalized_email, password_hash=hash_password(password))
    db.add(account)
    db.flush()  # populates account.id within this transaction, before commit
    profile = User(name="My Profile", account_id=account.id)
    db.add(profile)
    db.flush()  # populates profile.id
    account.last_active_profile_id = profile.id
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent registrations for the same email both passing the
        # pre-check above — the unique constraint is the real guarantee.
        db.rollback()
        return templates.TemplateResponse(
            request, "register.html", {"error": "Email already registered", "email": email}, status_code=400
        )
    db.refresh(profile)

    request.session[ACCOUNT_SESSION_KEY] = account.id
    request.session[PROFILE_SESSION_KEY] = profile.id
    return RedirectResponse(url="/#home", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
