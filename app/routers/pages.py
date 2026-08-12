from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from sqlalchemy.sql.elements import ColumnElement

from app.content_query import DEFAULT_PAGE_SIZE, new_upload_cutoff, query_content_page
from app.deps import get_current_profile, get_db, require_login
from app.formatting import format_duration, format_size
from app.models import AppSettings, Content, Feed, User
from app.storage import collect_usage

router = APIRouter(dependencies=[Depends(require_login)])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size

HOME_SHELF_LIMIT = 12
HOME_CHANNEL_LIMIT = 8


def _home_shelf_query(db: Session, user_id: int):
    # is_preview excludes Explore videos not yet favorited/saved — see
    # routers/feeds.py's add_single_video and routers/content.py's
    # add_favorite/add_saved. Listening to one shouldn't look like it's
    # already saved.
    return (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.user_id == user_id, Content.is_preview.is_(False))
    )


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> HTMLResponse:
    # Fixed id — see app.models.AppSettings and main._ensure_app_settings,
    # which guarantees this row exists before any request can reach here.
    app_settings = db.get(AppSettings, 1)

    # followed=False excludes Explore's placeholder feeds (see
    # routers/feeds.py's _get_or_create_placeholder_feed) — never a real
    # follow, so it shouldn't show up as one in Library or here.
    feeds = (
        db.query(Feed)
        .filter(Feed.user_id == profile.id, Feed.followed.is_(True))
        .order_by(Feed.added_at.desc())
        .all()
    )
    # feeds is already newest-first — Home's chip row is just the most
    # recently followed few (with 100+ channels followed, the full list made
    # that row an endless horizontal scroll); Library's grid below still
    # gets every channel via `feeds` itself.
    home_recent_channels = feeds[:HOME_CHANNEL_LIMIT]
    usage = collect_usage(db, profile.id)

    # Each shelf (and the Library grid below) is its own bounded query now —
    # this used to be one `.all()` over every content row the user has ever
    # had (sliced in Python per shelf), which got very slow once backfilling
    # full channel histories pushed that past a few thousand rows.
    # is_new_upload (set in feed_sync.apply_feed_data) is what actually
    # means "new upload" here — an RSS-sourced row, as opposed to one from
    # _run_backfill's full-history scan or an Explore preview. The
    # feed.followed check on top of it is still needed as a safety net: it's
    # the same reasoning content_query.py's is_preview carve-out documents —
    # Explore-added content keeps a live feed_id even after being
    # favorited/saved, but that feed is only a placeholder (followed=False)
    # unless the user actually follows the channel (see
    # routers/feeds.py's _get_or_create_placeholder_feed) — and it also
    # covers a channel that was later unfollowed but had some content kept
    # (see routers/feeds.py's delete_feed), whose is_new_upload=True rows
    # shouldn't keep showing here as if still followed.
    home_new_uploads = (
        _home_shelf_query(db, profile.id)
        .filter(
            Content.is_new_upload.is_(True),
            Content.published_at >= new_upload_cutoff(),
            Content.feed.has(Feed.followed.is_(True)),
        )
        .order_by(Content.published_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )
    # Not built on _home_shelf_query: an Explore preview that's actually been
    # played earns a spot here even though it's still is_preview (never
    # favorited/saved) — otherwise playing something from Explore and coming
    # back to Home would make it look like nothing happened. New
    # uploads/Favorites/Saved have no such case, since none of those imply
    # the user ever listened.
    home_recently_played = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.user_id == profile.id, Content.last_played_at.isnot(None))
        .order_by(Content.last_played_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )
    home_favorites = (
        _home_shelf_query(db, profile.id)
        .filter(Content.is_favorite.is_(True))
        .order_by(Content.published_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )
    home_saved = (
        _home_shelf_query(db, profile.id)
        .filter(Content.is_saved.is_(True))
        .order_by(Content.published_at.desc())
        .limit(HOME_SHELF_LIMIT)
        .all()
    )

    # Library is a grid of channels, not videos — one cheap grouped count
    # query covers every channel's card instead of a per-feed query each.
    channel_video_counts = dict(
        db.query(Content.feed_id, func.count(Content.id))
        .filter(Content.user_id == profile.id)
        .group_by(Content.feed_id)
        .all()
    )
    favorites_count = (
        db.query(func.count(Content.id))
        .filter(Content.user_id == profile.id, Content.is_favorite.is_(True))
        .scalar()
    )
    saved_count = (
        db.query(func.count(Content.id))
        .filter(Content.user_id == profile.id, Content.is_saved.is_(True))
        .scalar()
    )
    # Matches query_content_page's __new_uploads__/__played__ filters exactly
    # (see content_query.py) so these Library-card counts never disagree
    # with what the page they link to actually lists.
    new_uploads_count = (
        db.query(func.count(Content.id))
        .filter(
            Content.user_id == profile.id,
            Content.is_new_upload.is_(True),
            Content.is_preview.is_(False),
            Content.published_at >= new_upload_cutoff(),
            Content.feed.has(Feed.followed.is_(True)),
        )
        .scalar()
    )
    recently_played_count = (
        db.query(func.count(Content.id))
        .filter(Content.user_id == profile.id, Content.last_played_at.isnot(None))
        .scalar()
    )

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "feeds": feeds,
            "home_recent_channels": home_recent_channels,
            "channel_video_counts": channel_video_counts,
            "favorites_count": favorites_count,
            "saved_count": saved_count,
            "new_uploads_count": new_uploads_count,
            "recently_played_count": recently_played_count,
            "usage": usage,
            "audio_quality": profile.audio_quality,
            "feed_refresh_interval_minutes": app_settings.feed_refresh_interval_minutes,
            "home_recently_played": home_recently_played,
            "home_new_uploads": home_new_uploads,
            "home_favorites": home_favorites,
            "home_saved": home_saved,
        },
    )


def _content_list_page(
    request: Request,
    db: Session,
    *,
    user_id: int,
    kind: str,
    is_match: ColumnElement[bool],
    filter_value: str,
    title: str,
    empty_message: str,
    page: int,
) -> HTMLResponse:
    """Shared by /favorites and /saved — both are just query_content_page
    with a fixed filter, rendered through content_list.html (the same
    track-list/pagination partials channel.html uses, minus its
    single-channel avatar hero)."""
    video_count = db.query(func.count(Content.id)).filter(Content.user_id == user_id, is_match).scalar()
    items, page, total_pages = query_content_page(db, user_id, page=page, filter=filter_value)

    return templates.TemplateResponse(
        request,
        "content_list.html",
        {
            "kind": kind,
            "title": title,
            "empty_message": empty_message,
            "video_count": video_count,
            "content": items,
            "page": page,
            "total_pages": total_pages,
            "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
            "base_url": f"/{kind}",
        },
    )


@router.get("/favorites", response_class=HTMLResponse)
def favorites_page(
    request: Request,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        user_id=profile.id,
        kind="favorites",
        is_match=Content.is_favorite.is_(True),
        filter_value="__favorites__",
        title="Favorites",
        empty_message="No favorites yet.",
        page=page,
    )


@router.get("/saved", response_class=HTMLResponse)
def saved_page(
    request: Request,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        user_id=profile.id,
        kind="saved",
        is_match=Content.is_saved.is_(True),
        filter_value="__saved__",
        title="Saved for later",
        empty_message="Nothing saved yet.",
        page=page,
    )


@router.get("/new-uploads", response_class=HTMLResponse)
def new_uploads_page(
    request: Request,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        user_id=profile.id,
        kind="new-uploads",
        is_match=(Content.is_new_upload.is_(True))
        & (Content.published_at >= new_upload_cutoff())
        & (Content.feed.has(Feed.followed.is_(True))),
        filter_value="__new_uploads__",
        title="New Uploads",
        empty_message="No new uploads yet.",
        page=page,
    )


@router.get("/recently-played", response_class=HTMLResponse)
def recently_played_page(
    request: Request,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    return _content_list_page(
        request,
        db,
        user_id=profile.id,
        kind="recently-played",
        is_match=Content.last_played_at.isnot(None),
        filter_value="__played__",
        title="Recently Played",
        empty_message="Nothing played yet.",
        page=page,
    )


@router.get("/channel/{feed_id}", response_class=HTMLResponse)
def channel_page(
    feed_id: int,
    request: Request,
    page: int = 1,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    feed = db.query(Feed).filter(Feed.id == feed_id, Feed.user_id == profile.id).first()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")

    video_count = db.query(func.count(Content.id)).filter(
        Content.feed_id == feed_id, Content.user_id == profile.id
    ).scalar()
    items, page, total_pages = query_content_page(db, profile.id, page=page, feed_id=feed_id)

    return templates.TemplateResponse(
        request,
        "channel.html",
        {
            "feed": feed,
            "video_count": video_count,
            "content": items,
            "page": page,
            "total_pages": total_pages,
            "start_index": (page - 1) * DEFAULT_PAGE_SIZE + 1,
        },
    )


@router.get("/player/{content_id}", response_class=HTMLResponse)
def player_page(
    content_id: int,
    request: Request,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    content = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.id == content_id, Content.user_id == profile.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    # No redirect for not-yet-downloaded content: the player itself kicks off the
    # download and shows a preparing state until the audio is ready.
    return templates.TemplateResponse(request, "player.html", {"content": content})
