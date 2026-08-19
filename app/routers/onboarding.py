"""Real-artist channel suggestions for the onboarding wizard's last step
(see home/onboarding.js) — one route over services/genre_artists.py, which
owns the MusicBrainz/YouTube split; this just adapts a query string into a
list and the result into GenreSuggestionsOut.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, require_login
from app.schemas import GenreSuggestionsOut
from app.services.genre_artists import get_suggested_channels_by_genre

router = APIRouter(prefix="/onboarding", tags=["onboarding"], dependencies=[Depends(require_login)])


@router.get("/suggested-channels", response_model=list[GenreSuggestionsOut])
def suggested_channels(genres: str, db: Session = Depends(get_db)) -> list[dict]:
    """Comma-separated `genres` — whichever of them have been seeded (see
    services/genre_artists.seed_genre) contribute a group of suggestions each,
    in the order they were picked; a free-typed genre with nothing seeded for
    it just contributes no group, not an error."""
    requested = [genre.strip() for genre in genres.split(",") if genre.strip()]
    return get_suggested_channels_by_genre(db, requested)
