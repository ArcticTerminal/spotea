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

from app.models import Content, Feed, User
from app.timeutil import utcnow

DEFAULT_PAGE_SIZE = 50

# How far back "New Uploads" reaches — is_new_upload alone (see models.py's
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


# How far back the "Recently Added" smart playlist reaches — everything in
# the library has an added_at, so without a window this would just be the
# whole library sorted by it, which the default (no-filter) view already is.
RECENTLY_ADDED_MAX_AGE = timedelta(days=30)


def recently_added_cutoff() -> datetime:
    return utcnow() - RECENTLY_ADDED_MAX_AGE


def new_upload_filter() -> ColumnElement[bool]:
    """What "New Uploads" means, in one place: RSS-sourced, recent, and from
    a channel still followed.

    All three conditions are load-bearing and none is implied by the others.
    is_new_upload alone never expires (see NEW_UPLOAD_MAX_AGE). The
    Feed.followed check covers two separate cases: Explore-added content
    keeps a live feed_id even after being favorited/saved, but that feed is
    only a placeholder (see routers/explore.py's _get_or_create_placeholder_feed);
    and a channel that was unfollowed while some of its content was kept
    (see routers/feeds.py's delete_feed) leaves is_new_upload=True rows
    behind that shouldn't keep surfacing as if still followed.

    Expressed as `Content.feed.has(...)` rather than a join so it composes
    into any query over Content without the caller having to arrange for
    Feed to be joined first.
    """
    return and_(
        Content.is_new_upload.is_(True),
        Content.published_at >= new_upload_cutoff(),
        Content.feed.has(Feed.followed.is_(True)),
    )


def followed_feeds(
    db: Session, user_id: int | None = None, account_id: int | None = None
) -> Query[Feed]:
    """Feeds actually followed, newest first.

    followed=False rows are Explore placeholders auto-created to hold a
    single video (see routers/explore.py's _get_or_create_placeholder_feed)
    or channels unfollowed while keeping some content (see delete_feed).
    Either way they're invisible in Library and skipped by the background
    refresh, so every "the user's channels" query has to exclude them —
    omitting the filter is what would silently turn "I grabbed one song"
    into "I follow this channel now".

    Scoped one of three ways: everything (both filters omitted), one
    profile's own (`user_id`, what a request handler scopes to), or one
    whole account's across every one of its profiles (`account_id` — the
    background scheduler refreshes per account now that the refresh
    interval is per-Account rather than one shared setting; see
    scheduler.py). `user_id` wins if both are somehow given.
    """
    query = db.query(Feed).filter(Feed.followed.is_(True))
    if user_id is not None:
        query = query.filter(Feed.user_id == user_id)
    elif account_id is not None:
        query = query.join(User, Feed.user_id == User.id).filter(User.account_id == account_id)
    return query.order_by(Feed.added_at.desc())


# Distinct from a plain free-text filter: this is an *exact* channel match
# (picked from a suggestion, e.g. clicking a Home channel chip), so a video
# from a different channel whose title happens to contain the channel's name
# (e.g. a reaction video titled "... Linus Tech Tips ...") can't sneak in
# the way it would under the substring title-or-channel search below.
CHANNEL_FILTER_PREFIX = "__channel__:"


def _content_query(
    db: Session, user_id: int, filter: str = "", feed_id: int | None = None
) -> Query[Content]:
    """What one filter/feed selection *means*, ordered but unpaginated.

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
    # effect), but the channel-detail page (feed_id) could otherwise be
    # reached directly for a placeholder feed, so it's filtered here for
    # every caller, not just some — except __played__ (Recently Played),
    # where a preview that's actually been listened to still belongs on the
    # list; same carve-out pages.py's home_recently_played shelf documents.
    query = db.query(Content).filter(Content.user_id == user_id)
    if filter != "__played__":
        query = query.filter(Content.is_preview.is_(False))

    if feed_id is not None:
        query = query.filter(Content.feed_id == feed_id)

    # Only the filters that actually match on a Feed column need the join.
    # __new_uploads__ used to be in here too, back when it spelled its
    # follow check out as `Feed.followed.is_(True)`; new_upload_filter()
    # carries its own EXISTS instead, so it composes without one.
    needs_feed_join = filter not in (
        "",
        "__favorites__",
        "__saved__",
        "__played__",
        "__new_uploads__",
        "__on_repeat__",
        "__recently_added__",
        "__forgotten_favorites__",
    )
    if needs_feed_join:
        query = query.join(Feed)

    if filter == "__favorites__":
        query = query.filter(Content.is_favorite.is_(True))
    elif filter == "__saved__":
        query = query.filter(Content.is_saved.is_(True))
    elif filter == "__played__":
        query = query.filter(Content.last_played_at.isnot(None))
    elif filter == "__new_uploads__":
        query = query.filter(new_upload_filter())
    elif filter == "__on_repeat__":
        query = query.filter(Content.play_count > 0)
    elif filter == "__recently_added__":
        query = query.filter(Content.added_at >= recently_added_cutoff())
    elif filter == "__forgotten_favorites__":
        # Favorited, but never actually played — "forgotten" in the sense
        # that matters for a smart playlist, not "favorited a while ago"
        # (there's no favorited-at timestamp to measure that from).
        query = query.filter(Content.is_favorite.is_(True), Content.last_played_at.is_(None))
    elif filter.startswith(CHANNEL_FILTER_PREFIX):
        channel_title = filter[len(CHANNEL_FILTER_PREFIX) :]
        query = query.filter(Feed.channel_title == channel_title)
    elif filter:
        # Substring, case-insensitive, against either field: this is the
        # free-text search box, not a channel picklist, so a search for a
        # video title shouldn't require also typing its channel.
        pattern = f"%{filter}%"
        query = query.filter(or_(Feed.channel_title.ilike(pattern), Content.title.ilike(pattern)))

    # Most filters sort by publish date; three read differently enough that
    # publish date wouldn't make sense as "the" order for them.
    order_column = {
        "__played__": Content.last_played_at,  # most recently played, not published
        "__on_repeat__": Content.play_count,  # most played, not published
        "__recently_added__": Content.added_at,  # most recently added, not published
    }.get(filter, Content.published_at)
    return query.order_by(order_column.desc())


def query_content_page(
    db: Session,
    user_id: int,
    page: int = 1,
    filter: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    feed_id: int | None = None,
) -> tuple[list[Content], int, int]:
    """A page of a user's content, newest first, optionally filtered. Shared
    by the Library grid's server-rendered first page (pages.py) and the
    channel/playlist detail fragments that serve every subsequent page
    (routers/partials.py), so the two never disagree on what "page 1, no
    filter" actually contains.

    feed_id restricts to a single channel — used by the channel detail page,
    which has no other filter UI, so it's applied independently of `filter`.

    Returns (items, clamped page, total_pages).
    """
    # joinedload lives here rather than in _content_query: every renderer of
    # these rows reads .feed (channel name, avatar), but query_content_ids
    # never materialises a Content object at all and would only pay for the
    # join.
    query = _content_query(db, user_id, filter=filter, feed_id=feed_id).options(
        joinedload(Content.feed)
    )

    total_items = query.count()
    total_pages = max(1, -(-total_items // page_size))
    page = min(max(1, page), total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return items, page, total_pages


def count_content(db: Session, user_id: int, filter: str = "", feed_id: int | None = None) -> int:
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
    return _content_query(db, user_id, filter=filter, feed_id=feed_id).count()


# How far a "Play all" reaches. A followed channel that's been backfilled can
# be several thousand videos deep, and a queue that long is neither something
# anyone listens through nor something worth shipping to the client and
# keeping in sessionStorage. 1000 tracks is on the order of three days of
# continuous playback — past the point where the cap is what limits the
# session.
QUEUE_MAX_ITEMS = 1000


def query_content_ids(
    db: Session, user_id: int, filter: str = "", feed_id: int | None = None
) -> list[int]:
    """Every content id one channel/playlist selects, in the same order its
    track list shows them — the play queue behind "Play all" (see
    routers/content.py's queue endpoints and static/js/home/queue.js).

    Ids only: the client already has titles and artwork for the page it's
    looking at, and fetches the rest one track at a time as it plays them, so
    shipping full rows for a thousand-track queue would be almost entirely
    waste.
    """
    query = _content_query(db, user_id, filter=filter, feed_id=feed_id)
    return [row[0] for row in query.with_entities(Content.id).limit(QUEUE_MAX_ITEMS).all()]
