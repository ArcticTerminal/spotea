from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import scheduler
from app.app_settings import get_app_settings
from app.deps import get_current_profile, get_db, require_login
from app.interests import parse_interests, serialize_interests
from app.models import AppSettings, User
from app.schemas import SettingsOut, SettingsUpdate

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_login)])

AUDIO_QUALITIES = ("high", "low")

# Presets rather than a free-form number — keeps the control simple (matches
# the audio-quality radios) and rules out a value aggressive enough to risk
# YouTube rate-limiting the unauthenticated RSS/yt-dlp calls in feed_sync.
FEED_REFRESH_INTERVALS = (15, 30, 60, 120)


def _settings_out(profile: User, app_settings: AppSettings) -> SettingsOut:
    """Both endpoints answer with the same full settings shape — a PUT that
    replied with only the fields it changed would leave the client guessing
    what the rest ended up as (interests in particular, which the server
    normalizes on the way in)."""
    return SettingsOut(
        audio_quality=profile.audio_quality,
        feed_refresh_interval_minutes=app_settings.feed_refresh_interval_minutes,
        interests=parse_interests(profile.interests),
    )


@router.get("", response_model=SettingsOut)
def get_settings(profile: User = Depends(get_current_profile), db: Session = Depends(get_db)) -> SettingsOut:
    return _settings_out(profile, get_app_settings(db))


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

    if payload.interests is not None:
        # Normalized rather than validated: an interest is free text, so
        # there's nothing to reject — over-long or duplicate tags are
        # something to clean up, not a 400. The client is told what actually
        # got stored by the SettingsOut it gets back.
        profile.interests = serialize_interests(payload.interests)
        # The recommendation cache isn't cleared here — it's keyed by an
        # interests hash and goes stale on its own the moment this list
        # changes (see services/recommendations.py). Leaving the row alone
        # means editing interests back to a previous set still hits its
        # cached batch instead of paying for a rebuild.

    app_settings = get_app_settings(db)
    if payload.feed_refresh_interval_minutes is not None:
        if payload.feed_refresh_interval_minutes not in FEED_REFRESH_INTERVALS:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid refresh interval")
        app_settings.feed_refresh_interval_minutes = payload.feed_refresh_interval_minutes
        # Cuts short the scheduler's current sleep so the new interval takes
        # effect immediately instead of after the old one finishes.
        scheduler.request_reschedule()

    db.commit()
    return _settings_out(profile, app_settings)
