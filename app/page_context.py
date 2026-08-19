"""Template context for the regions of index.html that can be re-rendered.

Each builder here backs two callers: the full page render (routers/pages.py)
and a fragment endpoint (routers/partials.py) that re-renders just that
region after something changes. Sharing the builder is the point — a shelf
that means one thing on first load and another on refresh is exactly the
class of bug this replaced.
"""

from collections.abc import Iterable

from fastapi import BackgroundTasks
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.content_query import (
    DEFAULT_PAGE_SIZE,
    count_content,
    followed_artists,
    new_upload_filter,
    query_content_page,
)
from app.images import needs_thumbnail_caching
from app.models import Content
from app.services.artist_sync import cache_thumbnail
from app.services.initial_sync import syncing_artist_ids
from app.storage import collect_usage, usage_summary

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def queue_thumbnail_caching(background_tasks: BackgroundTasks, items: Iterable[Content]) -> None:
    """Caches thumbnails for whatever's actually being rendered in this
    response — a Home shelf, a Library list's current page, a channel page's
    current page — rather than eagerly sweeping ahead of actual browsing (a
    prior version of this did that at startup; on a large library it meant
    downloading and storing thumbnails for things nobody was looking at yet).
    This render still goes out with the original YouTube URL for anything
    not yet cached — the queued task (see artist_sync.cache_thumbnail) only
    ever benefits the *next* time this same content is rendered, anywhere.
    Deduped per call so a video appearing in more than one shelf here isn't
    queued twice."""
    seen: set[str] = set()
    for item in items:
        if not needs_thumbnail_caching(item.thumbnail_url):
            continue
        if item.video_id in seen:
            continue
        seen.add(item.video_id)
        background_tasks.add_task(cache_thumbnail, item.video_id, item.thumbnail_url)


def _shelf_query(db: Session, user_id: int):
    # is_preview excludes Explore videos not yet favorited/saved — see
    # routers/explore.py's add_single_video and routers/content.py's
    # add_favorite/add_saved. Listening to one shouldn't look like it's
    # already saved.
    return (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.user_id == user_id, Content.is_preview.is_(False))
    )


def home_context(db: Session, user_id: int) -> dict:
    """Home's channel chips and its four shelves.

    Each shelf is its own bounded query. This used to be one `.all()` over
    every content row the user had ever had, sliced per shelf in Python,
    which got very slow once backfilling full channel histories pushed that
    past a few thousand rows.
    """
    # Newest-first already; the chip row is just the most recently followed
    # few (with 100+ channels followed, the full list made that row an
    # endless horizontal scroll).
    recent_artists = followed_artists(db, user_id).limit(HOME_CHANNEL_LIMIT).all()

    return {
        "home_recent_artists": recent_artists,
        # Drives the "nothing here yet" branch — a cheap existence check
        # rather than counting anything.
        "has_content": db.query(Content.id).filter(Content.user_id == user_id).first() is not None,
        "home_new_uploads": (
            _shelf_query(db, user_id)
            .filter(new_upload_filter())
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        # Not built on _shelf_query: an Explore preview that's actually been
        # played earns a spot here even though it's still is_preview (never
        # favorited/saved) — otherwise playing something from Explore and
        # coming back to Home would make it look like nothing happened. New
        # uploads/Favorites/Saved have no such case, since none of those imply
        # the user ever listened.
        "home_recently_played": (
            db.query(Content)
            .options(joinedload(Content.artist))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .order_by(Content.last_played_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        "home_favorites": (
            _shelf_query(db, user_id)
            .filter(Content.is_favorite.is_(True))
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
        "home_saved": (
            _shelf_query(db, user_id)
            .filter(Content.is_saved.is_(True))
            .order_by(Content.published_at.desc())
            .limit(HOME_SHELF_LIMIT)
            .all()
        ),
    }


HOME_SHELF_KEYS = ("home_new_uploads", "home_recently_played", "home_favorites", "home_saved")


def home_shelf_items(context: dict) -> list[Content]:
    """Every Content row home_context put on a shelf — what the caller hands
    to _queue_thumbnail_caching. Listed explicitly so adding a shelf is a
    deliberate edit here rather than something that silently starts (or stops)
    getting its thumbnails cached."""
    return [item for key in HOME_SHELF_KEYS for item in context[key]]


def library_context(db: Session, user_id: int) -> dict:
    """Library's channel grid: per-channel counts plus the four pinned
    virtual-playlist tiles.

    Each count matches its page's own filter exactly (see content_query.py's
    query_content_page) — a tile saying "12 videos" that opens onto a list of
    9 is worse than no count at all.
    """
    artists = followed_artists(db, user_id).all()
    return {
        "artists": artists,
        # Which cards say "Preparing…" — a channel whose one-time history scan
        # is still running (services/initial_sync.py). Read straight off the
        # in-memory registry, so this costs a dict lookup per card and no
        # query at all. It is the whole reason the onboarding wizard no
        # longer makes anyone wait for a backfill: the wait moved onto the
        # card of the channel it actually belongs to, where it can be ignored.
        "preparing_artist_ids": syncing_artist_ids(artist.id for artist in artists),
        # One grouped count covers every channel's card, rather than a
        # per-artist query each — count_content can't be reused directly here
        # for that reason (it's one artist_id at a time), but the filter has to
        # match it anyway: is_preview excludes Explore videos not yet
        # favorited/saved, same as content_query._content_query, or a tile
        # can read a higher count than the channel page it opens onto lists
        # (measured live: 156 vs 154).
        "artist_track_counts": dict(
            db.query(Content.artist_id, func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_preview.is_(False))
            .group_by(Content.artist_id)
            .all()
        ),
        "favorites_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_favorite.is_(True))
            .scalar()
        ),
        "saved_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_saved.is_(True))
            .scalar()
        ),
        "new_uploads_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.is_preview.is_(False), new_upload_filter())
            .scalar()
        ),
        "recently_played_count": (
            db.query(func.count(Content.id))
            .filter(Content.user_id == user_id, Content.last_played_at.isnot(None))
            .scalar()
        ),
    }


def downloads_context(db: Session, user_id: int) -> dict:
    """What's on disk, item by item — the Downloads modal's own list, plus
    (at initial page load only, see pages.py's home()) the Settings summary
    line rendered from the same object. The fragment endpoints that refresh
    these afterwards (routers/partials.py) split apart: the modal's full list
    is fetched only when actually opened (home/settings.js's
    setupDownloadsOverlay), the summary line on every refreshFragments()
    sweep — see storage_summary_context below for that cheaper path.
    """
    return {"usage": collect_usage(db, user_id)}


def storage_summary_context(db: Session, user_id: int) -> dict:
    """Just the Settings summary line's two numbers, via
    storage.usage_summary rather than collect_usage — no per-row
    materialization, no joinedload. What refreshFragments() actually needs
    after a save/favorite/play; the full item list it used to also refetch
    was 86.5KB and, on the household's real library, 57% of the whole page's
    initial payload for a modal closed the vast majority of the time.
    """
    return {"usage": usage_summary(db, user_id)}


# kind -> (filter_value, title, empty_message) for Library's four pinned
# virtual playlists. filter_value is what _content_query (content_query.py)
# actually dispatches on — the original fourth tuple element here was a
# duplicate of that same filter, spelled out a second time as a lambda, and
# had no reader left once count_content replaced every hand-rolled
# video_count query.
PLAYLIST_KINDS: dict[str, tuple] = {
    "favorites": ("__favorites__", "Favorites", "No favorites yet."),
    "saved": ("__saved__", "Saved for later", "Nothing saved yet."),
    "new-uploads": ("__new_uploads__", "New releases", "Nothing new yet."),
    "recently-played": ("__played__", "Recently Played", "Nothing played yet."),
}


def playlist_filter(kind: str) -> str | None:
    """The query_content_page/query_content_ids filter one virtual playlist
    (pinned or smart) means, or None for an unknown kind.

    Read off PLAYLIST_KINDS rather than spelled out again: the queue endpoint
    (routers/content.py) has to select exactly the rows the same playlist's
    detail panel renders, and a second copy of this mapping is how "Play all"
    would end up playing a different list than the one on screen.
    """
    config = PLAYLIST_KINDS.get(kind)
    return config[0] if config else None


def playlist_detail_context(db: Session, user_id: int, kind: str, page: int) -> dict | None:
    """One of Library's four pinned virtual playlists (see PLAYLIST_KINDS),
    rendered through the same track-list/pagination markup a channel page
    uses. Returns None for an unknown kind so the caller can 404."""
    config = PLAYLIST_KINDS.get(kind)
    if config is None:
        return None
    filter_value, title, empty_message = config

    video_count = count_content(db, user_id, filter=filter_value)
    items, page, total_pages = query_content_page(db, user_id, page=page, filter=filter_value)

    return {
        "kind": kind,
        "artist": None,
        "title": title,
        "empty_message": empty_message,
        "video_count": video_count,
        "content": items,
        "page": page,
        "total_pages": total_pages,
        "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
        # A hash route, not the /partials/... fetch URL itself: this is what
        # _pagination.html's <a> actually points at, and home/detail.js's
        # click interception aside, it has to be a real navigable URL on its
        # own (ctrl-click, a JS-disabled fallback).
        "base_url": f"/#{kind}",
    }
