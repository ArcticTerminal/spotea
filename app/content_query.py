"""Shared query building blocks.

Anything a query means in more than one place belongs here rather than
being spelled out again at each call site — the Home shelf, the Library
tile's count and the full list page all have to agree on what "a new
upload" is, and they used to say so in four separate expressions that
could drift apart independently.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Query, Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.models import Artist, Content
from app.timeutil import utcnow

DEFAULT_PAGE_SIZE = 50

# How far back "New releases" reaches — is_new_upload alone (see models.py's
# Content.is_new_upload) only means "RSS-sourced, not backfilled," which
# without this stays true forever, so a channel's uploads from months ago
# never age out of the shelf/list.
NEW_UPLOAD_MAX_AGE = timedelta(days=14)


def new_upload_cutoff() -> datetime:
    # Naive UTC, matching Content.published_at's own convention (see e.g.
    # routers/explore.py's add_single_video) — SQLite has no timezone type, and
    # mixing naive/aware datetimes in the same column makes string comparison
    # unreliable.
    return utcnow() - NEW_UPLOAD_MAX_AGE


def new_upload_filter() -> ColumnElement[bool]:
    """What "New releases" means, in one place: sync-sourced, recent, and from
    a channel still followed.

    All three conditions are load-bearing and none is implied by the others.
    is_new_upload alone never expires (see NEW_UPLOAD_MAX_AGE). The
    Artist.followed check covers two separate cases: Explore-added content
    keeps a live artist_id even after being favorited/saved, but that artist is
    only a placeholder (see routers/explore.py's _get_or_create_placeholder);
    and a channel that was unfollowed while some of its content was kept
    (see routers/artists.py's delete_feed) leaves is_new_upload=True rows
    behind that shouldn't keep surfacing as if still followed.

    Expressed as `Content.artist.has(...)` rather than a join so it composes
    into any query over Content without the caller having to arrange for
    Artist to be joined first.
    """
    return and_(
        Content.is_new_upload.is_(True),
        Content.published_at >= new_upload_cutoff(),
        Content.artist.has(Artist.followed.is_(True)),
    )


def followed_artists(db: Session, user_id: int | None = None) -> Query[Artist]:
    """Feeds actually followed, newest first.

    followed=False rows are Explore placeholders auto-created to hold a
    single video (see routers/explore.py's _get_or_create_placeholder)
    or artists unfollowed while keeping some content (see delete_feed).
    Either way they're invisible in Library and skipped by the background
    refresh, so every "the user's artists" query has to exclude them —
    omitting the filter is what would silently turn "I grabbed one song"
    into "I follow this artist now".

    `user_id` omitted means every user's, which is what the background
    scheduler walks.
    """
    query = db.query(Artist).filter(Artist.followed.is_(True))
    if user_id is not None:
        query = query.filter(Artist.user_id == user_id)
    return query.order_by(Artist.added_at.desc())


def _content_query(
    db: Session, user_id: int, filter: str = "", artist_id: int | None = None
) -> Query[Content]:
    """What one filter/artist selection *means*, ordered but unpaginated.

    Split out because two callers need the same selection at different
    granularities: query_content_page below wants one page of full rows,
    query_content_ids wants every id in the same order (the play queue). A
    second spelling of these filters is exactly the drift this module exists
    to prevent — a "Play all" that quietly played a different set than the
    list it was launched from would be indistinguishable from a shuffle bug.
    """
    # is_preview excludes Explore videos not yet favorited/saved — see
    # routers/explore.py's add_single_video and routers/content.py's
    # add_favorite/add_saved. Favorites/Saved never actually hit this in
    # practice (favoriting/saving already clears is_preview as a side
    # effect), but the channel-detail page (artist_id) could otherwise be
    # reached directly for a placeholder artist, so it's filtered here for
    # every caller, not just some — except __played__ (Recently Played),
    # where a preview that's actually been listened to still belongs on the
    # list; same carve-out pages.py's home_recently_played shelf documents.
    query = db.query(Content).filter(Content.user_id == user_id)
    if filter != "__played__":
        query = query.filter(Content.is_preview.is_(False))

    if artist_id is not None:
        query = query.filter(Content.artist_id == artist_id)

    # Only the filters that actually match on a Artist column need the join.
    # __new_uploads__ used to be in here too, back when it spelled its
    # follow check out as `Artist.followed.is_(True)`; new_upload_filter()
    # carries its own EXISTS instead, so it composes without one.
    needs_feed_join = filter not in (
        "",
        "__favorites__",
        "__saved__",
        "__played__",
        "__new_uploads__",
    )
    if needs_feed_join:
        query = query.join(Artist)

    if filter == "__favorites__":
        query = query.filter(Content.is_favorite.is_(True))
    elif filter == "__saved__":
        query = query.filter(Content.is_saved.is_(True))
    elif filter == "__played__":
        query = query.filter(Content.last_played_at.isnot(None))
    elif filter == "__new_uploads__":
        query = query.filter(new_upload_filter())
    elif filter:
        # Substring, case-insensitive, against either field: this is the
        # free-text search box, not a channel picklist, so a search for a
        # video title shouldn't require also typing its channel.
        pattern = f"%{filter}%"
        query = query.filter(or_(Artist.name.ilike(pattern), Content.title.ilike(pattern)))

    # Most filters sort by publish date; __played__ reads differently enough
    # that publish date wouldn't make sense as its order.
    order_column = {
        "__played__": Content.last_played_at,  # most recently played, not published
    }.get(filter, Content.published_at)
    return query.order_by(order_column.desc())


def query_content_page(
    db: Session,
    user_id: int,
    page: int = 1,
    filter: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    artist_id: int | None = None,
) -> tuple[list[Content], int, int]:
    """A page of a user's content, newest first, optionally filtered. Shared
    by the Library grid's server-rendered first page (pages.py) and the
    channel/playlist detail fragments that serve every subsequent page
    (routers/partials.py), so the two never disagree on what "page 1, no
    filter" actually contains.

    artist_id restricts to a single channel — used by the channel detail page,
    which has no other filter UI, so it's applied independently of `filter`.

    Returns (items, clamped page, total_pages).
    """
    # joinedload lives here rather than in _content_query: every renderer of
    # these rows reads .artist (channel name, avatar), but query_content_ids
    # never materialises a Content object at all and would only pay for the
    # join.
    query = _content_query(db, user_id, filter=filter, artist_id=artist_id).options(
        joinedload(Content.artist)
    )

    total_items = query.count()
    total_pages = max(1, -(-total_items // page_size))
    page = min(max(1, page), total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return items, page, total_pages


def count_content(db: Session, user_id: int, filter: str = "", artist_id: int | None = None) -> int:
    """The exact count query_content_page's own page would have, without
    paginating it — for a tile or header count that has to agree with the
    list it describes.

    Not a second, hand-rolled `func.count` for the same reason
    query_content_page and query_content_ids share _content_query rather than
    each spelling the filters out again: page_context.py's channel and
    playlist detail counts used to do exactly that, and drifted from the list
    below them the moment is_preview needed excluding — measured live as a
    channel tile reading 156 while its own page listed 154.
    """
    return _content_query(db, user_id, filter=filter, artist_id=artist_id).count()


# How far a "Play all" reaches. A followed channel that's been backfilled can
# be several thousand videos deep, and a queue that long is neither something
# anyone listens through nor something worth shipping to the client and
# keeping in sessionStorage. 1000 tracks is on the order of three days of
# continuous playback — past the point where the cap is what limits the
# session.
QUEUE_MAX_ITEMS = 1000


def query_content_ids(
    db: Session, user_id: int, filter: str = "", artist_id: int | None = None
) -> list[int]:
    """Every content id one channel/playlist selects, in the same order its
    track list shows them — the play queue behind "Play all" (see
    routers/content.py's queue endpoints and static/js/home/queue.js).

    Ids only: the client already has titles and artwork for the page it's
    looking at, and fetches the rest one track at a time as it plays them, so
    shipping full rows for a thousand-track queue would be almost entirely
    waste.
    """
    query = _content_query(db, user_id, filter=filter, artist_id=artist_id)
    return [row[0] for row in query.with_entities(Content.id).limit(QUEUE_MAX_ITEMS).all()]
