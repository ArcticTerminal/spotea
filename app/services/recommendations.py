"""Explore's browse shelves: songs, channels and playlists picked from the
interests a profile listed in Settings, plus the charts and one mood shelf,
which belong to nobody in particular.

There is no recommender model here and no user-behaviour signal — an interest
is a free-text phrase, and a recommendation is what YouTube search returns for
it. That's deliberate: Spotea knows what a profile follows and plays, but it
has no way to turn that into *new* discovery on its own, whereas "tell me what
you like" turns straight into a query. The charts and mood shelves are the
answer to the other case: a profile that has said nothing about itself yet,
which until they existed had an empty Explore tab and a nag.

The interesting part is therefore not the ranking, it's the request budget.
One batch is a dozen live searches, each of them seconds long, against a
service that rate-limits an unauthenticated residential IP. So:

  * a batch is cached in the database (models.RecommendationCache), keyed by
    what the interests hashed to, and reused until it expires or the interests
    change — opening the Explore tab costs nothing;
  * "expires" means the same interval the user already chose for background
    feed refreshes (Settings → Feed updates), rather than a second cadence
    nobody asked for. There is deliberately no separate refresh control:
    recommendations go stale on that interval, when the interest list is
    edited, and when the app-wide Refresh button is pressed — the same three
    moments everything else on the page does;
  * a run samples INTERESTS_PER_RUN of the profile's interests rather than
    searching all of them, which bounds the cost of a run regardless of how
    many interests are listed (and makes "Refresh" surface different corners
    of the list, which is the behaviour you want anyway);
  * only one run happens at a time process-wide, and a run that finds a fresh
    batch already waiting reuses it instead of repeating the work.
"""

import json
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.interests import interests_signature, parse_interests
from app.models import Content, Feed, RecommendationCache, User
from app.timeutil import utcnow
from app.youtube.music import fetch_charts, fetch_mood_categories, fetch_mood_playlists, search_songs
from app.youtube.music import search_playlists as search_music_playlists
from app.youtube.search import search_channels
from app.youtube.urls import extract_channel_id

logger = logging.getLogger(__name__)

# Floor on how old a batch can be before the next visit to Explore rebuilds
# it. Not a constant of its own: callers pass the profile's account's
# configured feed refresh interval (see Account.feed_refresh_interval_minutes),
# so "how often does this app go and look at YouTube again" stays one setting
# rather than two. This is only the fallback for a caller that passes no ttl.
DEFAULT_TTL = timedelta(minutes=30)

# Interests sampled per run. Each one costs three searches (songs, channels,
# playlists), so this is the knob that decides a run's request count — nine
# searches, run in parallel, is a few seconds of wall time and a request burst
# YouTube tolerates.
INTERESTS_PER_RUN = 3

# Wide enough for every sampled interest's three searches at once, plus the
# two browse jobs (see _BROWSE_BUILDERS) that don't depend on interests at
# all. Nothing else here overlaps.
_POOL_SIZE = INTERESTS_PER_RUN * 3 + 2

# Per shelf, after merging every sampled interest's results. Roughly a shelf
# and a half of horizontal scrolling, which is as far as anyone browses one.
RESULTS_PER_SHELF = 12

# How many mood categories a run will try before giving up on that shelf.
# Only the first is paid for on a normal run — see _mood_shelf.
MOOD_SHELF_ATTEMPTS = 3

# Bumped whenever the shape of a stored batch changes. It rides along on the
# cached signature, so every existing row stops matching and rebuilds on its
# own — which is what keeps this module free to add a shelf without a
# migration, and without a reader having to defend against a payload written
# by an older version of itself.
PAYLOAD_VERSION = "v2"

# Songs and playlists come from YouTube Music (app/youtube/music.py), which
# indexes tracks rather than videos — the same reason Explore's search box
# moved. Channels deliberately do not: that shelf is where a podcast gets
# found, and YouTube Music is measurably worse at finding one (see
# routers/explore.py's search_feeds).
_SEARCHERS = {
    "videos": search_songs,
    "channels": search_channels,
    "playlists": search_music_playlists,
}

# Result key -> the field that identifies one result, for cross-interest
# deduplication. Two interests in the same genre routinely return the same
# channel; showing it twice in one shelf reads as a bug.
_IDENTITY_FIELDS = {"videos": "video_id", "channels": "channel_id", "playlists": "playlist_id"}

# Serializes batch building process-wide. Not about data races (each run
# writes only its own profile's row) — it's a second, cruder brake on how many
# yt-dlp searches can be in flight at once, so two profiles refreshing
# together can't double the burst _POOL_SIZE is sized for.
_build_lock = threading.Lock()


def empty_batch() -> dict:
    """Every shelf, empty. The starting point a build fills in, and what a
    run that came back with nothing at all looks like."""
    return {
        "interests_used": [],
        "videos": [],
        "channels": [],
        "playlists": [],
        "charts": [],
        "chart_artists": [],
        "mood": None,
    }


def _charts_shelves() -> dict:
    """The chart pair, which has nothing to do with anyone's interests — it
    is what everyone in a country is listening to this week. Included in the
    batch rather than served from a route of its own so it shares the cache,
    the TTL and the refresh button that already exist for the shelves beside
    it; the cost of that is one duplicated copy per profile, which is a few
    kilobytes of JSON against a whole second endpoint."""
    charts = fetch_charts(settings.music_chart_country)
    return {
        "charts": [asdict(playlist) for playlist in charts.playlists],
        "chart_artists": [asdict(artist) for artist in charts.artists],
    }


def _mood_shelf() -> dict:
    """One of YouTube Music's forty-odd moods and genres, picked at random,
    with its playlists.

    One rather than all of them for the same reason interests are sampled:
    a run should cost a bounded number of requests, and refreshing should
    land somewhere different rather than redrawing the same shelf. The
    category's own name is the shelf title — "Chill" and "Bollywood &
    Indian" say what they are, and a generic "Moods" heading over one of
    them would not.
    """
    categories = fetch_mood_categories()
    # A second and third pick, only if the first comes back empty. Even
    # inside the section that mostly works, the occasional category still
    # trips ytmusicapi's playlist parser (see music.MOOD_SECTION), and
    # losing the whole shelf to one of those is a worse outcome than one
    # extra request on the rare run that hits one.
    for category in random.sample(categories, min(len(categories), MOOD_SHELF_ATTEMPTS)):
        playlists = fetch_mood_playlists(category.params, RESULTS_PER_SHELF)
        if playlists:
            return {
                "mood": {
                    "title": category.title,
                    "section": category.section,
                    "playlists": [asdict(playlist) for playlist in playlists],
                }
            }
    return {"mood": None}


# Shelves built without reference to the profile's interests, so they fill in
# even for a profile that has listed none.
_BROWSE_BUILDERS = (_charts_shelves, _mood_shelf)


def _sample(interests: list[str]) -> list[str]:
    """Which interests this run searches on. Random rather than "the first
    few" so that refreshing works its way around a long list instead of
    rebuilding the same batch."""
    if len(interests) <= INTERESTS_PER_RUN:
        return list(interests)
    return random.sample(interests, INTERESTS_PER_RUN)


def _interleave(per_interest: list[list[dict]], identity_field: str) -> list[dict]:
    """Merges one shelf's per-interest result lists round-robin, so the front
    of the shelf represents every sampled interest rather than exhausting the
    first one. Deduplicated by identity, and capped."""
    merged: list[dict] = []
    seen: set[str] = set()
    for rank in range(max((len(results) for results in per_interest), default=0)):
        for results in per_interest:
            if rank >= len(results):
                continue
            identity = results[rank][identity_field]
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(results[rank])
            if len(merged) == RESULTS_PER_SHELF:
                return merged
    return merged


def build_batch(interests: list[str]) -> dict:
    """Runs the searches for a fresh batch. Pure — no database, no caching —
    so the caching policy above stays in one place (get_recommendations).

    A profile with no interests still gets a batch: the interest searches
    are simply skipped and the charts and mood shelves are the whole of it.
    That is the case Explore used to have nothing at all to show.
    """
    sampled = _sample(interests)
    jobs = [(kind, interest) for interest in sampled for kind in _SEARCHERS]

    batch = empty_batch()
    with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
        # Submitted before the interest searches so they overlap with them
        # rather than queueing behind a full pool.
        browsing = [pool.submit(build) for build in _BROWSE_BUILDERS]
        results = list(pool.map(lambda job: _SEARCHERS[job[0]](job[1]), jobs))
        for shelves in browsing:
            batch.update(shelves.result())

    # Rebuilt into {kind: [per-interest result lists]} in the sampled order,
    # which is what _interleave's round-robin walks across.
    by_kind: dict[str, list[list[dict]]] = {kind: [] for kind in _SEARCHERS}
    for (kind, _), found in zip(jobs, results, strict=True):
        by_kind[kind].append([asdict(item) for item in found])

    batch["interests_used"] = sampled
    for kind, per_interest in by_kind.items():
        batch[kind] = _interleave(per_interest, _IDENTITY_FIELDS[kind])

    logger.info(
        "Built recommendations for %s: %d songs, %d channels, %d playlists, "
        "%d charts, %d charting artists, mood %s",
        ", ".join(sampled) or "no interests",
        len(batch["videos"]),
        len(batch["channels"]),
        len(batch["playlists"]),
        len(batch["charts"]),
        len(batch["chart_artists"]),
        (batch["mood"] or {}).get("title", "—"),
    )
    return batch


def _cached_batch(
    cache: RecommendationCache | None, signature: str, *, not_before: datetime
) -> tuple[dict, datetime] | None:
    """The cached batch if this caller can use it, else None.

    `not_before` is the oldest build this caller accepts, and it's the only
    thing that separates a plain read from a refresh: an ordinary read will
    take anything inside the TTL, while a refresh only accepts a batch built
    after it started — which can only be one another request built while this
    one waited for the lock.
    """
    if cache is None or cache.interests_signature != signature:
        return None
    if cache.generated_at < not_before:
        return None
    try:
        return json.loads(cache.payload), cache.generated_at
    except json.JSONDecodeError:
        # A payload written by an older version of this module (or truncated
        # somehow) is a cache miss, not a 500.
        return None


def _drop_already_in_library(db: Session, profile: User, batch: dict) -> dict:
    """Recommending something already in the library adds nothing — a
    followed channel or an added video isn't new. Applied here, at read
    time, against every batch (freshly built or cached) rather than inside
    build_batch: a cached batch can outlive what's added to the library
    after it was built, and re-checking on every read is what makes
    following a recommended channel make it disappear from the shelf on the
    very next load, rather than waiting for the batch to expire and rebuild.
    Playlists aren't filtered — YouTube's search results don't expose enough
    to match one against the library reliably.

    Observed live before this: a profile whose interests included "devops"
    was recommended a channel it already followed.

    The charting-artists shelf is filtered on the same rule as the channels
    one: it is a list of channels to follow, and one already followed isn't.
    """
    owned_video_ids = {
        video_id for (video_id,) in db.query(Content.video_id).filter(Content.user_id == profile.id)
    }
    followed_channel_ids = {
        channel_id
        for (channel_id,) in (
            db.query(Feed.rss_url).filter(Feed.user_id == profile.id, Feed.followed.is_(True))
        )
        for channel_id in [extract_channel_id(channel_id)]
        if channel_id is not None
    }

    return {
        **batch,
        "videos": [v for v in batch["videos"] if v["video_id"] not in owned_video_ids],
        "channels": [c for c in batch["channels"] if c["channel_id"] not in followed_channel_ids],
        "chart_artists": [
            c for c in batch["chart_artists"] if c["channel_id"] not in followed_channel_ids
        ],
    }


def get_recommendations(
    db: Session, profile: User, *, ttl: timedelta = DEFAULT_TTL, force: bool = False
) -> tuple[dict, datetime]:
    """One profile's current batch plus when it was built, rebuilding only if
    it's missing, older than `ttl`, built from different interests, or
    `force`d — then filtered against the current library (see
    _drop_already_in_library), always fresh regardless of whether the batch
    itself came from cache.

    Every profile has a batch, including one with no interests listed: the
    charts and mood shelves don't depend on any (see build_batch), so
    generated_at is never None.
    """
    batch, generated_at = _get_or_build_batch(db, profile, ttl=ttl, force=force)
    return _drop_already_in_library(db, profile, batch), generated_at


def _cache_signature(interests: list[str]) -> str:
    """What a cached row has to match to be usable: the interests it was
    built from *and* the payload shape this version of the module writes.
    See PAYLOAD_VERSION."""
    return f"{PAYLOAD_VERSION}:{interests_signature(interests)}"


def _get_or_build_batch(
    db: Session, profile: User, *, ttl: timedelta, force: bool
) -> tuple[dict, datetime]:
    """The caching/locking half of get_recommendations, unfiltered — split
    out so the library filter above wraps every return path (three of them,
    below) from a single point instead of needing to be threaded through
    each one."""
    interests = parse_interests(profile.interests)

    # Read before the lock, so a refresh that waits behind another one can
    # still tell "built while I waited" from "already there when I arrived".
    started = utcnow()
    signature = _cache_signature(interests)
    if not force:
        cached = _cached_batch(
            profile.recommendation_cache, signature, not_before=started - ttl
        )
        if cached:
            return cached

    with _build_lock:
        # Ends this request's read transaction before looking again, so the
        # re-read can actually see a batch another request committed while
        # this one was queued on the lock — without it SQLite keeps serving
        # this session's older snapshot. Safe to roll back: nothing of this
        # request's own is pending at this point.
        db.rollback()
        cache = db.get(RecommendationCache, profile.id)
        cached = _cached_batch(
            cache, signature, not_before=started if force else utcnow() - ttl
        )
        if cached:
            return cached

        batch = build_batch(interests)
        generated_at = utcnow()
        if cache is None:
            db.add(
                RecommendationCache(
                    user_id=profile.id,
                    interests_signature=signature,
                    payload=json.dumps(batch),
                    generated_at=generated_at,
                )
            )
        else:
            cache.interests_signature = signature
            cache.payload = json.dumps(batch)
            cache.generated_at = generated_at
        db.commit()

    return batch, generated_at
