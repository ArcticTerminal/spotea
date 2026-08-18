"""Real-artist channel suggestions for the onboarding wizard's step 2 (see
home/onboarding.js) — one route over services/genre_artists.py, which owns
the MusicBrainz/YouTube split; this just adapts a query string into a list
and the result into ChannelSearchResultOut.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, require_login
from app.schemas import ChannelSearchResultOut
from app.services.genre_artists import get_suggested_channels

router = APIRouter(prefix="/onboarding", tags=["onboarding"], dependencies=[Depends(require_login)])


@router.get("/suggested-channels", response_model=list[ChannelSearchResultOut])
def suggested_channels(genres: str, db: Session = Depends(get_db)) -> list[dict]:
    """Comma-separated `genres` — whichever of them have been seeded (see
    services/genre_artists.seed_genre) contribute suggestions; a free-typed
    genre with nothing seeded for it just contributes none, not an error."""
    requested = [genre.strip() for genre in genres.split(",") if genre.strip()]
    return get_suggested_channels(db, requested)
