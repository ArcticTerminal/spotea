from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models import Content, Feed

DEFAULT_PAGE_SIZE = 20

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
) -> tuple[list[Content], int, int]:
    """A page of a user's content, newest first, optionally filtered. Shared
    by the Library grid's server-rendered first page (pages.py) and the AJAX
    endpoint that serves every subsequent page/filter change
    (routers/content.py), so the two never disagree on what "page 1, no
    filter" actually contains.

    Returns (items, clamped page, total_pages).
    """
    query = db.query(Content).options(joinedload(Content.feed)).filter(Content.user_id == user_id)

    needs_feed_join = filter not in ("", "__favorites__", "__saved__", "__played__")
    if needs_feed_join:
        query = query.join(Feed)

    if filter == "__favorites__":
        query = query.filter(Content.is_favorite.is_(True))
    elif filter == "__saved__":
        query = query.filter(Content.is_saved.is_(True))
    elif filter == "__played__":
        query = query.filter(Content.last_played_at.isnot(None))
    elif filter.startswith(CHANNEL_FILTER_PREFIX):
        channel_title = filter[len(CHANNEL_FILTER_PREFIX) :]
        query = query.filter(Feed.channel_title == channel_title)
    elif filter:
        # Substring, case-insensitive, against either field: this is the
        # free-text search box, not a channel picklist, so a search for a
        # video title shouldn't require also typing its channel.
        pattern = f"%{filter}%"
        query = query.filter(or_(Feed.channel_title.ilike(pattern), Content.title.ilike(pattern)))

    query = query.order_by(Content.published_at.desc())

    total_items = query.count()
    total_pages = max(1, -(-total_items // page_size))
    page = min(max(1, page), total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return items, page, total_pages
