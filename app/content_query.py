from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import Content, Feed

DEFAULT_PAGE_SIZE = 20

_ORDER_MAP = {
    "date-desc": Content.published_at.desc(),
    "date-asc": Content.published_at.asc(),
    "title-asc": func.lower(Content.title).asc(),
    "title-desc": func.lower(Content.title).desc(),
    "channel-asc": func.lower(Feed.channel_title).asc(),
}


def query_content_page(
    db: Session,
    user_id: int,
    page: int = 1,
    sort: str = "date-desc",
    filter: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Content], int, int]:
    """A page of a user's content, filtered and sorted. Shared by the
    Library grid's server-rendered first page (pages.py) and the AJAX
    endpoint that serves every subsequent page/sort/filter change
    (routers/content.py), so the two never disagree on what "page 1,
    date-desc, no filter" actually contains.

    Returns (items, clamped page, total_pages).
    """
    query = db.query(Content).options(joinedload(Content.feed)).filter(Content.user_id == user_id)

    needs_feed_join = filter not in ("", "__favorites__", "__saved__") or sort == "channel-asc"
    if needs_feed_join:
        query = query.join(Feed)

    if filter == "__favorites__":
        query = query.filter(Content.is_favorite.is_(True))
    elif filter == "__saved__":
        query = query.filter(Content.is_saved.is_(True))
    elif filter:
        query = query.filter(Feed.channel_title == filter)

    query = query.order_by(_ORDER_MAP.get(sort, _ORDER_MAP["date-desc"]))

    total_items = query.count()
    total_pages = max(1, -(-total_items // page_size))
    page = min(max(1, page), total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return items, page, total_pages
