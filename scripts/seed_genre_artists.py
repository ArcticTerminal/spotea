"""One-off (but safely re-runnable) seed for the onboarding wizard's
genre-artist cache — see app/services/genre_artists.py for what this
populates and why, and app/models.py's GenreArtist for the table shape.

MusicBrainz traffic only, paced to its own ~1 request/second limit — no
YouTube calls happen here (those are resolved lazily, per artist, the first
time a real onboarding session actually needs one; see get_suggested_channels_by_genre).

Run from the repo root:
    .venv/bin/python -m scripts.seed_genre_artists

Safe to re-run: seed_genre() is a no-op for any genre that already has
enough rows, so this only ever does new work (a genre added to
app/genres.py's MUSIC_GENRES since the last run, or one that came up short
of the target last time).
"""

from app.database import SessionLocal
from app.genres import MUSIC_GENRES
from app.services.genre_artists import ARTISTS_PER_GENRE, seed_genre


def main() -> None:
    with SessionLocal() as db:
        for genre in MUSIC_GENRES:
            added = seed_genre(genre, db)
            db.commit()
            print(f"{genre}: +{added} (target {ARTISTS_PER_GENRE})" if added else f"{genre}: already seeded")


if __name__ == "__main__":
    main()
