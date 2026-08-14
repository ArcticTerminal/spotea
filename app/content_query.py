from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models import Content, Feed
from app.timeutil import utcnow

DEFAULT_PAGE_SIZE = 20

# How far back "New Uploads" reaches — is_new_upload alone (see models.py's
# Content.is_new_upload) only means "RSS-sourced, not backfilled," which
# without this stays true forever, so a channel's uploads from months ago
# never age out of the shelf/list. Shared by every place that means "New
# Uploads" (this file, routers/pages.py's home shelf, count, and video_count)
# so they can't drift apart and disagree on what counts as new.
NEW_UPLOAD_MAX_AGE = timedelta(days=14)


def new_upload_cutoff() -> datetime:
    # Naive UTC, matching Content.published_at's own convention (see e.g.
    # routers/feeds.py's add_single_video) — SQLite has no timezone type, and
    # mixing naive/aware datetimes in the same column makes string comparison
    # unreliable.
    return utcnow() - NEW_UPLOAD_MAX_AGE

# Distinct from a plain free-text filter: this is an *exact* channel match
# (picked from a suggestion, e.g. clicking a Home channel chip), so a video
# from a different channel whose title happens to contain the channel's name
# (e.g. a reaction video titled "... Linus Tech Tips ...") can't sneak in
# the way it would under the substring title-or-channel search below.
CHANNEL_FILTER_PREFIX = "__channel__:"


def query_content_page(
    db: Session,
    user_id: int,
    page: int = 1,
    filter: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    feed_id: int | None = None,
) -> tuple[list[Content], int, int]:
    """A page of a user's content, newest first, optionally filtered. Shared
    by the Library grid's server-rendered first page (pages.py) and the AJAX
    endpoint that serves every subsequent page/filter change
    (routers/content.py), so the two never disagree on what "page 1, no
    filter" actually contains.

    feed_id restricts to a single channel — used by the channel detail page,
    which has no other filter UI, so it's applied independently of `filter`.

    Returns (items, clamped page, total_pages).
    """
    # is_preview excludes Explore videos not yet favorited/saved — see
    # routers/feeds.py's add_single_video and routers/content.py's
    # add_favorite/add_saved. Favorites/Saved never actually hit this in
    # practice (favoriting/saving already clears is_preview as a side
    # effect), but the channel-detail page (feed_id) could otherwise be
    # reached directly for a placeholder feed, so it's filtered here for
    # every caller, not just some — except __played__ (Recently Played),
    # where a preview that's actually been listened to still belongs on the
    # list; same carve-out pages.py's home_recently_played shelf documents.
    query = db.query(Content).options(joinedload(Content.feed)).filter(Content.user_id == user_id)
    if filter != "__played__":
        query = query.filter(Content.is_preview.is_(False))

    if feed_id is not None:
        query = query.filter(Content.feed_id == feed_id)

    needs_feed_join = filter not in ("", "__favorites__", "__saved__", "__played__")
    if needs_feed_join:
        query = query.join(Feed)

    if filter == "__favorites__":
        query = query.filter(Content.is_favorite.is_(True))
    elif filter == "__saved__":
        query = query.filter(Content.is_saved.is_(True))
    elif filter == "__played__":
        query = query.filter(Content.last_played_at.isnot(None))
    elif filter == "__new_uploads__":
        # Unfollowing a channel keeps content that was played/favorited/
        # saved/downloaded (see routers/feeds.py's delete_feed) but is
        # explicitly meant to drop it out of New Uploads — Feed.followed
        # (not just is_new_upload) has to hold for this filter, matching the
        # Home shelf's own Content.feed.has(Feed.followed.is_(True)) check
        # (routers/pages.py's home_new_uploads).
        query = query.filter(
            Content.is_new_upload.is_(True),
            Content.published_at >= new_upload_cutoff(),
            Feed.followed.is_(True),
        )
    elif filter.startswith(CHANNEL_FILTER_PREFIX):
        channel_title = filter[len(CHANNEL_FILTER_PREFIX) :]
        query = query.filter(Feed.channel_title == channel_title)
    elif filter:
        # Substring, case-insensitive, against either field: this is the
        # free-text search box, not a channel picklist, so a search for a
        # video title shouldn't require also typing its channel.
        pattern = f"%{filter}%"
        query = query.filter(or_(Feed.channel_title.ilike(pattern), Content.title.ilike(pattern)))

    # Recently Played means "most recently played," not "most recently
    # published" — every other filter sorts by publish date.
    order_column = Content.last_played_at if filter == "__played__" else Content.published_at
    query = query.order_by(order_column.desc())

    total_items = query.count()
    total_pages = max(1, -(-total_items // page_size))
    page = min(max(1, page), total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return items, page, total_pages
