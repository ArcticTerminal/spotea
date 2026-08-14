"""Template context for the regions of index.html that can be re-rendered.

Each builder here backs two callers: the full page render (routers/pages.py)
and a fragment endpoint (routers/partials.py) that re-renders just that
region after something changes. Sharing the builder is the point — a shelf
that means one thing on first load and another on refresh is exactly the
class of bug this replaced.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.content_query import followed_feeds, new_upload_filter
from app.models import Content
from app.storage import collect_usage

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


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
