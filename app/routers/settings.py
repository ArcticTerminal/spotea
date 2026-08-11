from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import scheduler
from app.deps import get_current_profile, get_db, require_login
from app.models import AppSettings, User
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_login)])

AUDIO_QUALITIES = ("high", "low")

# Presets rather than a free-form number — keeps the control simple (matches
# the audio-quality radios) and rules out a value aggressive enough to risk
# YouTube rate-limiting the unauthenticated RSS/yt-dlp calls in feed_sync.
FEED_REFRESH_INTERVALS = (15, 30, 60, 120)

# Fixed id — see app.models.AppSettings and main._ensure_app_settings, which
# guarantees this row exists before any request can reach here.
APP_SETTINGS_ID = 1


def _get_app_settings(db: Session) -> AppSettings:
    return db.get(AppSettings, APP_SETTINGS_ID)


@router.get("", response_model=SettingsOut)
def get_settings(profile: User = Depends(get_current_profile), db: Session = Depends(get_db)) -> SettingsOut:
    app_settings = _get_app_settings(db)
    return SettingsOut(
        audio_quality=profile.audio_quality,
        feed_refresh_interval_minutes=app_settings.feed_refresh_interval_minutes,
    )


@router.put("", response_model=SettingsOut)
def update_settings(
    payload: SettingsUpdate,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> SettingsOut:
    if payload.audio_quality is not None:
        if payload.audio_quality not in AUDIO_QUALITIES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audio quality")
        profile.audio_quality = payload.audio_quality

    app_settings = _get_app_settings(db)
    if payload.feed_refresh_interval_minutes is not None:
        if payload.feed_refresh_interval_minutes not in FEED_REFRESH_INTERVALS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid refresh interval")
        app_settings.feed_refresh_interval_minutes = payload.feed_refresh_interval_minutes
        # Cuts short the scheduler's current sleep so the new interval takes
        # effect immediately instead of after the old one finishes.
        scheduler.request_reschedule()

    db.commit()
    return SettingsOut(
        audio_quality=profile.audio_quality,
        feed_refresh_interval_minutes=app_settings.feed_refresh_interval_minutes,
    )
