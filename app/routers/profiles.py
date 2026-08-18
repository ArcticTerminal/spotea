from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import PROFILE_SESSION_KEY
from app.deps import get_current_account, get_current_profile, get_db, require_login
from app.models import Account, Content, Feed, User
from app.schemas import ProfileCreate, ProfileOut, ProfileUpdate
from app.storage import delete_files_for_profile

router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(require_login)])


def _to_out(profile: User, current_profile_id: int) -> ProfileOut:
    return ProfileOut(
        id=profile.id,
        name=profile.name,
        is_current=profile.id == current_profile_id,
    )


@router.get("", response_model=list[ProfileOut])
def list_profiles(
    current: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> list[ProfileOut]:
    # Routed through get_current_profile (not a raw session read) so a
    # session that has never resolved a profile yet still self-heals here —
    # otherwise the very first /profiles call after login would show no
    # profile as current at all. Scoped to the caller's account — profiles
    # belonging to other accounts must never appear here.
    profiles = db.query(User).filter(User.account_id == current.account_id).order_by(User.id).all()
    return [_to_out(p, current.id) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreate,
    request: Request,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ProfileOut:
    profile = User(name=payload.name, account_id=current_account.id)
    db.add(profile)
    db.flush()  # populates profile.id
    current_account.last_active_profile_id = profile.id
    db.commit()
    db.refresh(profile)

    # Auto-switch into the newly created profile — "create and go" rather
    # than an extra manual switch step.
    request.session[PROFILE_SESSION_KEY] = profile.id
    return _to_out(profile, profile.id)


@router.put("/{profile_id}", response_model=ProfileOut)
def update_profile(
    profile_id: int,
    payload: ProfileUpdate,
    current: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> ProfileOut:
    profile = db.get(User, profile_id)
    # 404 (never 403) for a profile belonging to another account — doesn't
    # leak whether that id exists at all.
    if profile is None or profile.account_id != current.account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    profile.name = payload.name
    db.commit()
    return _to_out(profile, current.id)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(
    profile_id: int,
    request: Request,
    current: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> None:
    profile = db.get(User, profile_id)
    if profile is None or profile.account_id != current.account_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    # Scoped to the caller's account — counting every account's profiles
    # here would let another account's profile count block (or not block)
    # this account's own last-profile deletion.
    remaining = (
        db.query(func.count(User.id)).filter(User.account_id == current.account_id).scalar()
    )
    if remaining <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Can't delete the last profile")

    # Files on disk don't cascade with the DB rows (see storage.py) — clean
    # them up before the profile (and its feeds/content, via cascade) is gone.
    delete_files_for_profile(db, profile_id)

    was_current = current.id == profile_id

    if was_current:
        # Clears the account-level "land here after login" pointer too, not
        # just the session — otherwise the next login would try to resolve
        # a profile id that no longer exists (harmless, get_current_profile
        # falls back safely, but pointing at a live profile is tidier).
        current.account.last_active_profile_id = None

    # The profile's two big collections go in one statement each rather than
    # through the ORM cascade, which loads every child row into the session to
    # delete it one at a time: 1.7 of the 2.3 seconds a real 28,866-row
    # profile took, all of it spent building objects that exist only to be
    # thrown away. Order matters — content references feeds, and foreign keys
    # are enforced (see database.py's PRAGMA).
    #
    # db.delete() below still does the rest (the profile row itself, its
    # recommendation_cache), so a child relationship added later is still
    # covered by the cascade; only these two are shortcut. A future table
    # referencing content.id would fail loudly here rather than silently, for
    # the same reason.
    db.query(Content).filter(Content.user_id == profile_id).delete(synchronize_session=False)
    db.query(Feed).filter(Feed.user_id == profile_id).delete(synchronize_session=False)

    db.delete(profile)
    db.commit()

    if was_current:
        request.session.pop(PROFILE_SESSION_KEY, None)


@router.post("/{profile_id}/switch", response_model=ProfileOut)
def switch_profile(
    profile_id: int,
    request: Request,
    current_account: Account = Depends(get_current_account),
    db: Session = Depends(get_db),
) -> ProfileOut:
    profile = db.get(User, profile_id)
    if profile is None or profile.account_id != current_account.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    current_account.last_active_profile_id = profile.id
    db.commit()

    request.session[PROFILE_SESSION_KEY] = profile.id
    return _to_out(profile, profile.id)
