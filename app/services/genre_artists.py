"""Onboarding's channel suggestions: real artists tagged with one of the
predefined genres (see app/genres.py), whose MusicBrainz record links
straight to a YouTube channel — see app/models.py's GenreArtist for why
that's normalized into one row per artist rather than a RecommendationCache-
style JSON blob.

Two distinct phases, run at very different times against very different
budgets:

  * seed_genre() — MusicBrainz only, no YouTube calls at all. Run once,
    offline, by scripts/seed_genre_artists.py for every predefined genre.
    MusicBrainz is free and keyless but rate-limits anonymous callers to
    roughly one request a second and will temporarily block a burst past
    that (hit this live once already during development — TLS handshakes
    to musicbrainz.org started failing outright for a few minutes).

  * get_suggested_channels() — what the onboarding wizard actually calls.
    Reads the cached rows for the requested genres and resolves whichever
    haven't been looked up on YouTube yet (title, avatar, subscriber count)
    right there in the request. The first profile ever to pick a given
    genre pays that YouTube cost; every profile after it, for that genre,
    reads a fully-resolved row and pays nothing — the same "first load is
    slow, that's expected" shape GET /recommendations already has.
"""

import logging
import re
import time

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GenreArtist
from app.timeutil import utcnow
from app.youtube.search import cached_avatar_or_hotlink, fetch_channel_avatar, fetch_channel_uploads

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
# MusicBrainz's documented limit for anonymous callers is ~1 request/second;
# a burst past that got this exact IP temporarily blocked during development
# (see module docstring). 1.1s keeps a safety margin.
MB_REQUEST_DELAY_SECONDS = 1.1
# How many MusicBrainz artist-search candidates to walk through per genre
# looking for a usable youtube relation before settling for fewer than
# ARTISTS_PER_GENRE rows. Not every artist has one linked, so this has to be
# comfortably more than the target.
MB_CANDIDATES_PER_GENRE = 40
# Matches RESULTS_PER_SHELF in services/recommendations.py — this shelf is
# the same shape, just sourced differently.
ARTISTS_PER_GENRE = 12

# MusicBrainz's youtube relation is a full profile URL. Only the plain
# /channel/UC.../ shape resolves straight to a channel_id for free; a
# @handle or /c/Name link would need its own live YouTube resolve to turn
# into one, which defeats the point of getting this from MusicBrainz — those
# are skipped in _musicbrainz_youtube_channel below.
_CHANNEL_URL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{10,})")


MB_MAX_ATTEMPTS = 4


def _mb_get(path: str, params: dict) -> dict:
    """A transient 503/429 is routine for MusicBrainz's public instance
    under load, observed live during development — worth a few retries with
    backoff rather than letting one hiccup fail an entire genre's seed
    (or, worse, a real onboarding request's lazy resolve)."""
    last_error: httpx.HTTPStatusError | None = None
    for attempt in range(MB_MAX_ATTEMPTS):
        if attempt:
            time.sleep(MB_REQUEST_DELAY_SECONDS * 2**attempt)
        else:
            time.sleep(MB_REQUEST_DELAY_SECONDS)
        response = httpx.get(
            f"{MB_BASE}{path}",
            params={**params, "fmt": "json"},
            headers={"User-Agent": settings.musicbrainz_user_agent},
            timeout=10,
        )
        if response.status_code in (429, 503):
            last_error = httpx.HTTPStatusError(
                f"{response.status_code} from MusicBrainz", request=response.request, response=response
            )
            continue
        response.raise_for_status()
        return response.json()
    raise last_error


def _musicbrainz_artist_candidates(genre: str, limit: int) -> list[dict]:
    """MusicBrainz artists tagged with `genre`, ranked by MusicBrainz's own
    search relevance — id + name only. Relations (the youtube link, if any)
    cost a separate request per artist; see _musicbrainz_youtube_channel."""
    data = _mb_get("/artist", {"query": f'tag:"{genre}"', "limit": limit})
    return data.get("artists", [])


def _musicbrainz_youtube_channel(mbid: str) -> tuple[str, str] | None:
    """This artist's YouTube channel, straight from MusicBrainz's own
    editor-curated url-relationships — (channel_id, channel_url), or None if
    it has none, or its youtube link isn't a plain /channel/UC.../ one."""
    data = _mb_get(f"/artist/{mbid}", {"inc": "url-rels"})
    for relation in data.get("relations", []):
        if relation.get("type") != "youtube":
            continue
        url = relation.get("url", {}).get("resource", "")
        match = _CHANNEL_URL_RE.search(url)
        if match:
            return match.group(1), url
    return None


def seed_genre(genre: str, db: Session) -> int:
    """Populates GenreArtist rows for `genre` up to ARTISTS_PER_GENRE, from
    MusicBrainz alone — no YouTube calls here (see module docstring). A
    no-op if the genre already has enough rows, so this is safe to re-run
    (scripts/seed_genre_artists.py does exactly that, unconditionally, for
    every genre). Returns how many rows were added; does not commit — the
    caller decides the transaction boundary."""
    existing = db.query(func.count(GenreArtist.id)).filter(GenreArtist.genre == genre).scalar()
    if existing >= ARTISTS_PER_GENRE:
        return 0

    known_channel_ids = {
        channel_id
        for (channel_id,) in db.query(GenreArtist.channel_id).filter(GenreArtist.genre == genre)
    }
    added = 0
    for artist in _musicbrainz_artist_candidates(genre, MB_CANDIDATES_PER_GENRE):
        if existing + added >= ARTISTS_PER_GENRE:
            break
        mbid = artist.get("id")
        name = artist.get("name")
        if not mbid or not name:
            continue
        resolved = _musicbrainz_youtube_channel(mbid)
        if resolved is None:
            continue
        channel_id, channel_url = resolved
        if channel_id in known_channel_ids:
            continue
        known_channel_ids.add(channel_id)
        db.add(GenreArtist(genre=genre, artist_name=name, channel_id=channel_id, channel_url=channel_url))
        added += 1

    if added:
        logger.info("Seeded %d artist(s) for genre %r", added, genre)
    return added


def _resolve(row: GenreArtist) -> None:
    """Fills in one row's display metadata via YouTube, in place — the one
    live YouTube cost this whole feature has, paid once per artist ever.

    A channel MusicBrainz still links but YouTube no longer serves (taken
    down, renamed since the link was added) shouldn't 500 someone's
    onboarding — stamped resolved with just the artist's name as a fallback
    title instead, the same "show something rather than fail" approach
    search.py's own search functions take, so it isn't retried forever.
    """
    try:
        uploads = fetch_channel_uploads(row.channel_id)
        avatar = fetch_channel_avatar(row.channel_id)
    except Exception:
        logger.warning(
            "Could not resolve %s (%s) against YouTube", row.channel_id, row.artist_name, exc_info=True
        )
        row.title = row.artist_name
        row.resolved_at = utcnow()
        return

    row.title = uploads.title or row.artist_name
    row.subscriber_count = uploads.subscriber_count
    row.thumbnail_url = cached_avatar_or_hotlink(row.channel_id, avatar)
    row.resolved_at = utcnow()


def _as_channel_dict(row: GenreArtist) -> dict:
    return {
        "channel_id": row.channel_id,
        "title": row.title or row.artist_name,
        "thumbnail_url": row.thumbnail_url,
        "subscriber_count": row.subscriber_count,
        "channel_url": row.channel_url,
    }


def get_suggested_channels(db: Session, genres: list[str]) -> list[dict]:
    """Real-artist channel suggestions for the onboarding wizard's step 2,
    for whichever of `genres` were seeded (see seed_genre) — a free-typed
    genre with no seeded rows just contributes nothing, not an error, since
    the search box next to these suggestions already covers that case.
    """
    if not genres:
        return []

    rows_by_genre: dict[str, list[GenreArtist]] = {}
    for genre in genres:
        rows = (
            db.query(GenreArtist)
            .filter(func.lower(GenreArtist.genre) == genre.lower())
            .order_by(GenreArtist.id)
            .all()
        )
        if rows:
            rows_by_genre[genre] = rows

    # Which rows will actually appear in the result, decided *before* any
    # YouTube resolution — round-robin across genres so a profile that
    # picked several sees all of them represented (same reasoning as
    # recommendations.py's own _interleave, a smaller local version here
    # since this is keyed by genre rather than interest-and-result-kind),
    # deduped by channel, capped at ARTISTS_PER_GENRE. Only *these* get
    # resolved below — picking three genres with nothing resolved yet would
    # otherwise mean paying the YouTube round trip for up to 36 artists
    # (3 genres × ARTISTS_PER_GENRE cached rows each) just to show 12 of
    # them, which is exactly what made a multi-genre pick feel stuck on
    # "Finding channels…" for minutes instead of the expected few seconds.
    selected: list[GenreArtist] = []
    seen: set[str] = set()
    max_len = max((len(rows) for rows in rows_by_genre.values()), default=0)
    for rank in range(max_len):
        for rows in rows_by_genre.values():
            if rank >= len(rows):
                continue
            row = rows[rank]
            if row.channel_id in seen:
                continue
            seen.add(row.channel_id)
            selected.append(row)
            if len(selected) == ARTISTS_PER_GENRE:
                break
        if len(selected) == ARTISTS_PER_GENRE:
            break

    resolved_any = False
    for row in selected:
        if row.resolved_at is None:
            _resolve(row)
            resolved_any = True
    if resolved_any:
        db.commit()

    return [_as_channel_dict(row) for row in selected]
