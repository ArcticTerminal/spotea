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

from app.content_query import DEFAULT_PAGE_SIZE, followed_feeds, new_upload_filter, query_content_page
from app.feed_sync import cache_thumbnail
from app.models import Content, Feed
from app.storage import collect_usage

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def queue_thumbnail_caching(background_tasks: BackgroundTasks, items: Iterable[Content]) -> None:
    """Caches thumbnails for whatever's actually being rendered in this
    response — a Home shelf, a Library list's current page, a channel page's
    current page — rather than eagerly sweeping ahead of actual browsing (a
    prior version of this did that at startup; on a large library it meant
    downloading and storing thumbnails for things nobody was looking at yet).
    This render still goes out with the original YouTube URL for anything
    not yet cached — the queued task (see feed_sync.cache_thumbnail) only
    ever benefits the *next* time this same content is rendered, anywhere.
    Deduped per call so a video appearing in more than one shelf here isn't
    queued twice."""
    seen: set[str] = set()
    for item in items:
        if not item.thumbnail_url or "ytimg.com" not in item.thumbnail_url:
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
        .options(joinedload(Content.feed))
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
    recent_channels = followed_feeds(db, user_id).limit(HOME_CHANNEL_LIMIT).all()

    return {
        "home_recent_channels": recent_channels,
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
            .options(joinedload(Content.feed))
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
    return {
        "feeds": followed_feeds(db, user_id).all(),
        # One grouped count covers every channel's card, rather than a
        # per-feed query each.
        "channel_video_counts": dict(
            db.query(Content.feed_id, func.count(Content.id))
            .filter(Content.user_id == user_id)
            .group_by(Content.feed_id)
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
    """What's on disk — shown both in the Downloads modal and as a one-line
    summary in Settings."""
    return {"usage": collect_usage(db, user_id)}


# kind -> (is_match, filter_value, title, empty_message) for the four pinned
# virtual playlists. is_match is a zero-arg callable rather than a bare
# expression because new_upload_filter() has to be evaluated fresh on every
# call (it embeds "now").
PLAYLIST_KINDS: dict[str, tuple] = {
    "favorites": (lambda: Content.is_favorite.is_(True), "__favorites__", "Favorites", "No favorites yet."),
    "saved": (lambda: Content.is_saved.is_(True), "__saved__", "Saved for later", "Nothing saved yet."),
    "new-uploads": (new_upload_filter, "__new_uploads__", "New Uploads", "No new uploads yet."),
    "recently-played": (
        lambda: Content.last_played_at.isnot(None),
        "__played__",
        "Recently Played",
        "Nothing played yet.",
    ),
}


def playlist_detail_context(db: Session, user_id: int, kind: str, page: int) -> dict | None:
    """One of the four pinned virtual playlists (Favorites/Saved/New
    Uploads/Recently Played), rendered through the same track-list/
    pagination markup a channel page uses. Returns None for an unknown kind
    so the caller can 404."""
    config = PLAYLIST_KINDS.get(kind)
    if config is None:
        return None
    is_match, filter_value, title, empty_message = config

    video_count = db.query(func.count(Content.id)).filter(Content.user_id == user_id, is_match()).scalar()
    items, page, total_pages = query_content_page(db, user_id, page=page, filter=filter_value)

    return {
        "kind": kind,
        "feed": None,
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


def channel_detail_context(db: Session, user_id: int, feed_id: int, page: int) -> dict | None:
    """One followed channel's track list. Returns None if the feed doesn't
    exist or isn't this user's, so the caller can 404."""
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == user_id).first()
    if feed is None:
        return None

    video_count = db.query(func.count(Content.id)).filter(
        Content.feed_id == feed_id, Content.user_id == user_id
    ).scalar()
    items, page, total_pages = query_content_page(db, user_id, page=page, feed_id=feed_id)

    return {
        "kind": "channel",
        "feed": feed,
        "title": feed.channel_title or feed.rss_url,
        "empty_message": "No videos from this channel yet.",
        "video_count": video_count,
        "content": items,
        "page": page,
        "total_pages": total_pages,
        "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
        "base_url": f"/#channel/{feed_id}",
    }
