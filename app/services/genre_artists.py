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
    Pure database read. No YouTube, no MusicBrainz, no network at all.

That second point used to be the opposite, and the difference is the whole
reason this module reads the way it does. Display metadata was resolved
lazily, in-request, the first time any profile picked a given genre: two
live yt-dlp calls per channel, twelve channels per pick. A cold genre took
minutes on "Finding channels…", and the cost repeated on every self-hosted
install, since each one started with an empty cache of its own.

So resolution moved out of the request entirely, and — because this is
self-hosted software that other people run on their own servers — out of
any given deployment. scripts/resolve_genre_artists.py does it once, and
writes the answers to scripts/channel_profiles.py, which is committed. The
seed scripts read that file and insert rows already carrying their title
and avatar URL, so a fresh clone serves a full wizard on first boot having
never contacted YouTube. Nobody running this pays the resolution cost; it
was paid once, upstream, and shipped.
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
from app.youtube.search import ChannelProfile, cached_avatar_or_hotlink, fetch_channel_profile
from app.youtube.urls import avatar_url_at_size

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

# The wizard draws these in a 36px circle (.search-result-thumb), so 176
# covers it at 2x for a high-DPI screen. Not cosmetic: the URLs YouTube
# reports are "=s0", the original upload, and a shelf of twelve of those
# measured about 4.7 MB against roughly 180 KB here — on a modal whose whole
# point is to appear instantly. See youtube/urls.avatar_url_at_size.
SUGGESTION_AVATAR_SIZE = 176

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


def build_row(
    genre: str, artist_name: str, channel_id: str, profile: tuple[str, str] | None
) -> GenreArtist:
    """One seedable row, with its committed display metadata already applied.

    Shared by both curated seed scripts so the resolved_at convention lives
    in one place: it means "this row has usable display metadata", which is
    what get_suggested_channels orders on. A channel with no profile yet
    (just added to a curated list, generator not re-run) is left unresolved
    rather than stamped with a half-filled row — it still appears in the
    wizard, just last and without an avatar.

    `profile` is the (title, avatar_url) pair from scripts/channel_profiles.py.
    The scripts pass it in rather than this reaching for it, so app code
    stays unaware of scripts/.
    """
    title, avatar_url = profile if profile else (None, None)
    return GenreArtist(
        genre=genre,
        artist_name=artist_name,
        channel_id=channel_id,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
        title=title or artist_name,
        thumbnail_url=avatar_url,
        resolved_at=utcnow() if avatar_url else None,
    )


def fetch_profile(channel_id: str) -> ChannelProfile | None:
    """One channel's live metadata, or None if it didn't answer.

    Network only — no database, no shared state, so the generator can run
    several of these at once (see scripts/resolve_genre_artists.py). Its
    partner apply_profile does all the writing, back on the thread that owns
    the Session.

    A channel that no longer answers (taken down, or renamed since it was
    curated) comes back None rather than raising: one dead entry shouldn't
    end a run over several hundred of them.
    """
    try:
        return fetch_channel_profile(channel_id)
    except Exception:
        logger.warning("Could not resolve %s against YouTube", channel_id, exc_info=True)
        return None


def apply_profile(row: GenreArtist, profile: ChannelProfile | None) -> bool:
    """Writes a fetched profile onto its row, in place. Returns whether the
    channel actually answered with an avatar.

    thumbnail_url takes the *remote* avatar URL, not a display-ready one.
    Those URLs are content-addressed (a hash path, no signature or expiry —
    checked against a live extraction), which is what makes them committable;
    turning one into something the browser can load is a read-time concern,
    handled in _as_channel_dict.

    A channel that didn't answer is still stamped resolved, with just the
    curated name as its title — the same "show something rather than fail"
    approach search.py's own search functions take, so the generator doesn't
    retry it on every future run.
    """
    if profile is None or profile.avatar_url is None:
        row.title = row.artist_name
        row.resolved_at = utcnow()
        return False

    row.title = profile.title or row.artist_name
    row.thumbnail_url = profile.avatar_url
    row.resolved_at = utcnow()
    return True


def _as_channel_dict(row: GenreArtist) -> dict:
    return {
        "channel_id": row.channel_id,
        "title": row.title or row.artist_name,
        # Reuses whatever local copy exists (a channel this profile already
        # follows) and proxies the remote URL otherwise — the same treatment
        # Explore's own search results get, applied here at read time because
        # what's stored is the raw upstream URL.
        "thumbnail_url": cached_avatar_or_hotlink(
            row.channel_id, avatar_url_at_size(row.thumbnail_url, SUGGESTION_AVATAR_SIZE)
        ),
        # Deliberately never populated. The lazy resolution this replaced read
        # it off the uploads playlist, which carries no channel_follower_count
        # at all, so every row had None here and the wizard's cards have never
        # rendered a count. The channel page does carry one, so filling it in
        # would silently *add* a number nobody asked for — one that would then
        # be frozen at whatever it was when scripts/channel_profiles.py was
        # last regenerated. The curated list is famous acts by construction,
        # so the signal is worth little here; Explore's search, where results
        # are arbitrary channels, still resolves and shows it live.
        "subscriber_count": None,
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
            # Resolved rows first, so the ones that can render a title and an
            # avatar fill the shelf before any row still waiting on
            # scripts/resolve_genre_artists.py. A hand-added channel not yet
            # in the committed profiles still shows up rather than vanishing,
            # just last and bare — it renders its curated artist_name in the
            # same grey circle a channel with no avatar gets.
            .order_by(GenreArtist.resolved_at.is_(None), GenreArtist.id)
            .all()
        )
        if rows:
            rows_by_genre[genre] = rows

    # Round-robin across genres so a profile that picked several sees all of
    # them represented (same reasoning as recommendations.py's own
    # _interleave, a smaller local version here since this is keyed by genre
    # rather than interest-and-result-kind), deduped by channel, capped at
    # ARTISTS_PER_GENRE.
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

    return [_as_channel_dict(row) for row in selected]
