from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import PROFILE_SESSION_KEY
from app.deps import get_current_profile, get_db, require_login
from app.models import User
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
    # profile as current at all.
    profiles = db.query(User).order_by(User.id).all()
    return [_to_out(p, current.id) for p in profiles]


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
def create_profile(
    payload: ProfileCreate, request: Request, db: Session = Depends(get_db)
) -> ProfileOut:
    profile = User(name=payload.name)
    db.add(profile)
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
    if profile is None:
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
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    if db.query(func.count(User.id)).scalar() <= 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Can't delete the last profile")

    # Files on disk don't cascade with the DB rows (see storage.py) — clean
    # them up before the profile (and its feeds/content, via cascade) is gone.
    delete_files_for_profile(db, profile_id)

    was_current = current.id == profile_id

    db.delete(profile)
    db.commit()

    if was_current:
        request.session.pop(PROFILE_SESSION_KEY, None)


@router.post("/{profile_id}/switch", response_model=ProfileOut)
def switch_profile(profile_id: int, request: Request, db: Session = Depends(get_db)) -> ProfileOut:
    profile = db.get(User, profile_id)
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found")

    request.session[PROFILE_SESSION_KEY] = profile.id
    return _to_out(profile, profile.id)
