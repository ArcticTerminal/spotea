"""Noticing that a followed artist released something.

Was an RSS read of the artist's "<Artist> - Topic" channel. That worked, but
it answered the wrong question for a music app: RSS reports *uploads*, and a
release reaches a Topic channel as one upload per track with no album behind
them, no cover art, and no duration — which is why the old sync paid a
separate yt-dlp call per channel just to find out how long anything was.

This asks YouTube Music what the artist has released instead. The artist's
page carries their albums and singles, newest first, in the same response
that already answers every other question about them; a set-diff against
what we saw last time is the whole change-detection mechanism. Each genuinely
new release is then opened once for its tracks, which arrive with real
durations and square cover art.

Measured live, per artist per refresh: 0.38-0.76s for the page (RSS was
0.11s, but needed a 1.32s duration call behind it), plus 0.09-0.20s for each
new release — and there is usually no new release at all.

What this gives up is the exact publish timestamp RSS carried: YouTube Music
reports a year and nothing finer. So a new release is stamped with when we
first saw it, which on a 30-minute refresh is within half an hour of the
truth. What it gains is that "New" now means *released since you followed*,
rather than "appeared in a 15-entry upload window" — and it catches a guest
verse on someone else's record, which never reaches the artist's own Topic
channel at all (measured: 3 of Drake's 45).
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.images import download_avatar, download_thumbnail
from app.models import Artist, Content
from app.timeutil import utcnow
from app.youtube.models import ChannelSearchResult, VideoSearchResult
from app.youtube.music import fetch_artist, fetch_release

logger = logging.getLogger(__name__)

# Refresh is network-bound, so refresh_feeds fans out across threads rather
# than doing one artist at a time. Kept modest to stay polite: this is
# unauthenticated, and a burst of dozens of concurrent requests risks 429s.
# DB writes never happen inside the pool (see apply_artist_data).
REFRESH_POOL_SIZE = 8


@dataclass
class ArtistFetchResult:
    """What one artist's network round trip found, ready to be applied on the
    caller's own session.

    `release_ids` is every release the page currently lists — the snapshot to
    store, not just the new ones. `tracks` are the tracks of the releases
    that turned out to be new; empty on a first sync, which deliberately
    records the snapshot without importing anything (see fetch_artist_data).
    `top_tracks` is unrelated to either — the artist's own page-preview
    songs, present on every successful fetch regardless of what's new.
    """

    ok: bool
    name: str | None = None
    avatar_url: str | None = None
    monthly_listeners: str | None = None
    related: list[ChannelSearchResult] | None = None
    # The artist's own page-preview songs (see Artist.top_tracks) — distinct
    # from `tracks` below, which is new releases' tracks to insert into
    # Content, not this artist's popular songs.
    top_tracks: list[VideoSearchResult] | None = None
    release_ids: list[str] = field(default_factory=list)
    tracks: list[VideoSearchResult] = field(default_factory=list)


def fetch_artist_data(browse_id: str, snapshot: str | None, avatar_url: str | None) -> ArtistFetchResult:
    """The network half of a sync. Safe to run off the main thread and in
    parallel across artists — touches no SQLAlchemy state.

    A first sync (`snapshot is None`) records what exists and imports none of
    it. Following an artist means "tell me what they put out from now on";
    their back catalogue is a click away on their profile, and importing it
    would bury the thing the follow was actually for. Every sync after that
    imports exactly what appeared since.
    """
    artist = fetch_artist(browse_id, all_songs=False)
    if artist is None:
        # Not an error worth failing a whole refresh over — YouTube Music
        # answers a page it can't parse the same way it answers a bad id, and
        # a refresh is meant to survive one artist.
        logger.warning("Artist %s: no page to read", browse_id)
        return ArtistFetchResult(ok=False)

    releases = [*artist.albums, *artist.singles]
    release_ids = [release.browse_id for release in releases]

    fetched_avatar_url = None
    if not avatar_url and artist.avatar_url:
        # Once per artist, ever — skipped as soon as the artist has one, so
        # this never adds a call to the steady-state refresh.
        fetched_avatar_url = download_avatar(browse_id, artist.avatar_url)

    if snapshot is None:
        return ArtistFetchResult(
            ok=True,
            name=artist.name,
            avatar_url=fetched_avatar_url,
            monthly_listeners=artist.monthly_listeners,
            related=artist.related,
            top_tracks=artist.tracks,
            release_ids=release_ids,
        )

    known = set(json.loads(snapshot))
    tracks: list[VideoSearchResult] = []
    for release in releases:
        if release.browse_id in known:
            continue
        detail = fetch_release(release.browse_id)
        if detail is None:
            # Leave it out of the snapshot too, so the next refresh tries
            # again rather than writing it off on one bad response.
            release_ids.remove(release.browse_id)
            continue
        tracks.extend(detail.tracks)

    return ArtistFetchResult(
        ok=True,
        name=artist.name,
        avatar_url=fetched_avatar_url,
        monthly_listeners=artist.monthly_listeners,
        related=artist.related,
        top_tracks=artist.tracks,
        release_ids=release_ids,
        tracks=tracks,
    )


def apply_artist_data(db: Session, artist: Artist, result: ArtistFetchResult) -> int:
    """The DB half of a sync: store the new snapshot and insert whatever the
    fetch found. Must run on the caller's own session, so always sequential
    (never in the pool)."""
    if not result.ok:
        return 0

    if result.name and not artist.name:
        artist.name = result.name
    if result.avatar_url:
        artist.avatar_url = result.avatar_url
    # Unlike name/avatar_url above, overwritten every sync rather than only
    # once — it's a moving count, not a fact settled the first time we see
    # it.
    if result.monthly_listeners:
        artist.monthly_listeners = result.monthly_listeners
    # Same as monthly_listeners above: a moving list, not a fact settled
    # once, so a later sync overwrites it rather than keeping the first
    # one ever seen. `is not None` rather than truthy — an artist with
    # genuinely no related artists (rare, but real) should clear a stale
    # list from before, not keep showing it forever.
    if result.related is not None:
        artist.related_artists = json.dumps([asdict(c) for c in result.related])
    # Same reasoning again: refreshed every sync, cleared rather than kept
    # stale if an artist's page genuinely has no songs listed right now.
    if result.top_tracks is not None:
        artist.top_tracks = json.dumps([asdict(t) for t in result.top_tracks])

    new_count = 0
    if result.tracks:
        # user_id-scoped, not artist_id-scoped: a track can already exist under
        # a different artist for this user (an Explore preview added before the
        # artist was followed, or a collaboration credited to two followed
        # artists) — Content's (user_id, video_id) unique constraint is
        # global, so inserting it again would violate it.
        incoming_ids = [track.video_id for track in result.tracks]
        existing_ids = {
            video_id
            for (video_id,) in db.query(Content.video_id).filter(
                Content.user_id == artist.user_id, Content.video_id.in_(incoming_ids)
            )
        }
        seen = set(existing_ids)
        for track in result.tracks:
            if track.video_id in seen:
                continue
            seen.add(track.video_id)
            db.add(
                Content(
                    artist_id=artist.id,
                    user_id=artist.user_id,
                    video_id=track.video_id,
                    title=track.title,
                    thumbnail_url=track.thumbnail_url,
                    duration_seconds=track.duration_seconds,
                    # YouTube Music reports a release year and nothing finer,
                    # so "when we first saw it" is the most precise honest
                    # answer — and on a refresh interval measured in minutes
                    # it is close enough to be the right one.
                    published_at=utcnow(),
                    is_new_upload=True,
                )
            )
            new_count += 1

    artist.release_snapshot = json.dumps(result.release_ids)
    db.commit()
    return new_count


def refresh_feeds(db: Session, artists: list[Artist]) -> int:
    """Sync every given artist — the fetch half fanned out across a thread
    pool, the DB half applied back sequentially on the caller's session.

    Shared by the on-demand /artists/refresh endpoint (user-scoped) and the
    background scheduler (every artist of every due user). One artist's
    apply failing must not abort every other artist's refresh in the same
    call, so each is isolated below rather than summed in one expression.
    """
    syncable = [artist for artist in artists if artist.browse_id]
    if not syncable:
        return 0

    with ThreadPoolExecutor(max_workers=min(len(syncable), REFRESH_POOL_SIZE)) as pool:
        results = list(
            pool.map(
                lambda f: fetch_artist_data(f.browse_id, f.release_snapshot, f.avatar_url),
                syncable,
            )
        )

    new_count = 0
    for artist, result in zip(syncable, results, strict=True):
        try:
            new_count += apply_artist_data(db, artist, result)
        except Exception:
            db.rollback()
            logger.exception("Failed to apply artist data for artist %s (%s)", artist.id, artist.name)
    return new_count


def cache_thumbnail(video_id: str, thumbnail_url: str) -> None:
    """The actual work behind pages.py's queue_thumbnail_caching — fetches
    once and rewrites every Content row sharing this video_id (there can be
    more than one: the same track under more than one user). Meant to run as
    a FastAPI BackgroundTask, after the response that triggered it has
    already gone out with the original (still remote) URL — this call only
    ever affects the *next* render of this content, never the one that
    queued it."""
    local_url = download_thumbnail(video_id, thumbnail_url)
    if local_url and local_url != thumbnail_url:
        with SessionLocal() as db:
            db.query(Content).filter(
                Content.video_id == video_id,
                ~Content.thumbnail_url.like("/thumbnails/%"),
            ).update({"thumbnail_url": local_url}, synchronize_session=False)
            db.commit()
