from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.deps import get_current_profile, get_db, require_login
from app.models import User
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_login)])

AUDIO_QUALITIES = ("high", "medium", "low")


@router.get("", response_model=SettingsOut)
def get_settings(profile: User = Depends(get_current_profile)) -> SettingsOut:
    return SettingsOut(audio_quality=profile.audio_quality)


@router.put("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> SettingsOut:
    if payload.audio_quality not in AUDIO_QUALITIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audio quality")

    profile.audio_quality = payload.audio_quality
    db.commit()
    return SettingsOut(audio_quality=profile.audio_quality)
