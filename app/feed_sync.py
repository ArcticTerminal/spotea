import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.images import download_avatar, download_thumbnail
from app.models import Content, Feed
from app.youtube.extract import fetch_channel_avatar_url, fetch_channel_video_durations
from app.youtube.rss import InvalidFeedError, ParsedFeed, fetch_feed
from app.youtube.urls import SHORT_MAX_DURATION_SECONDS, extract_channel_id, longform_feed_url

logger = logging.getLogger(__name__)

# Feed refresh is network-bound (RSS parse + a yt-dlp call per channel), so
# refresh_feeds fans those out across threads rather than doing them one
# channel at a time. Kept modest to stay polite to YouTube's servers — this
# is unauthenticated scraping, and a burst of dozens of concurrent requests
# risks 429s. DB writes never happen inside the pool (see apply_feed_data).
REFRESH_POOL_SIZE = 8


@dataclass
class FeedFetchResult:
    parsed: ParsedFeed | None
    durations: dict[str, int]
    channel_id: str | None
    avatar_url: str | None = None


def fetch_feed_data(feed_id: int, rss_url: str, avatar_url: str | None) -> FeedFetchResult:
    """The network half of a feed sync: RSS fetch plus, when needed, a
    duration lookup and a channel-avatar lookup. Safe to run off the main
    thread and in parallel across feeds — touches no SQLAlchemy state but
    its own throwaway read session.

    Fetches via the channel's Videos-tab playlist (UULF) when its channel ID
    is known, rather than rss_url's plain channel feed — Shorts are excluded
    there for free, no separate Shorts-tab fetch needed."""
    channel_id = extract_channel_id(rss_url)
    fetch_url = longform_feed_url(channel_id) if channel_id else rss_url

    try:
        parsed = fetch_feed(fetch_url)
    except InvalidFeedError:
        return FeedFetchResult(parsed=None, durations={}, channel_id=channel_id)

    durations: dict[str, int] = {}
    if channel_id and parsed.entries:
        incoming_ids = [entry.video_id for entry in parsed.entries]
        with SessionLocal() as read_db:
            existing = read_db.query(Content.video_id, Content.duration_seconds).filter(
                Content.feed_id == feed_id, Content.video_id.in_(incoming_ids)
            ).all()
        existing_ids = {video_id for video_id, _ in existing}
        needs_durations = any(vid not in existing_ids for vid in incoming_ids) or any(
            duration is None for _, duration in existing
        )
        if needs_durations:
            durations = fetch_channel_video_durations(channel_id)

    # Fetched (and downloaded) once per channel, ever — skipped as soon as a
    # feed has one, so this never adds a call to the steady-state per-session
    # refresh.
    fetched_avatar_url = None
    if not avatar_url and channel_id:
        remote_avatar_url = fetch_channel_avatar_url(channel_id)
        if remote_avatar_url:
            fetched_avatar_url = download_avatar(channel_id, remote_avatar_url)

    return FeedFetchResult(
        parsed=parsed, durations=durations, channel_id=channel_id, avatar_url=fetched_avatar_url
    )


def apply_feed_data(db: Session, feed: Feed, result: FeedFetchResult) -> int:
    """The DB half of a feed sync: insert new content rows and backfill
    missing durations from an already-fetched FeedFetchResult. Must run on
    the caller's own session, so always sequential (never in the pool)."""
    parsed = result.parsed
    if parsed is None:
        return 0

    # parsed.channel_title is "Videos" (the UULF playlist's own title, not the
    # channel's) whenever the fetch went through the Videos-tab playlist, so
    # only trust it as a channel_title backfill on the plain-feed fallback path.
    if not feed.channel_title and parsed.channel_title and not result.channel_id:
        feed.channel_title = parsed.channel_title

    if result.avatar_url:
        feed.avatar_url = result.avatar_url

    incoming_ids = [entry.video_id for entry in parsed.entries]
    # user_id-scoped, not feed_id-scoped: a video can already exist under a
    # different feed_id for this user (e.g. an Explore preview added before
    # this channel was followed for real, or the same video credited to two
    # of the user's followed channels) — Content's (user_id, video_id)
    # unique constraint is global, so inserting it again under this feed
    # would violate it. Treating it as "already have it" and skipping is
    # correct here, matching add_single_video's existing user-scoped check.
    existing_rows = {
        row.video_id: row
        for row in db.query(Content).filter(
            Content.user_id == feed.user_id, Content.video_id.in_(incoming_ids)
        )
    }
    missing_duration_ids = {
        video_id for video_id, row in existing_rows.items() if row.duration_seconds is None
    }
    new_entries = [entry for entry in parsed.entries if entry.video_id not in existing_rows]

    new_count = 0
    for entry in new_entries:
        duration = result.durations.get(entry.video_id)
        if duration is not None and duration <= SHORT_MAX_DURATION_SECONDS:
            continue  # likely a Short that slipped through the Videos-tab fetch
        db.add(
            Content(
                feed_id=feed.id,
                user_id=feed.user_id,
                video_id=entry.video_id,
                title=entry.title,
                thumbnail_url=entry.thumbnail_url,
                published_at=entry.published_at,
                duration_seconds=duration,
                # This is the only place an RSS-parsed row is ever inserted —
                # both a channel's initial fetch (when followed) and every
                # later routine refresh come through here, and both count as
                # a "new upload" (see Content.is_new_upload's docstring).
                # _run_backfill's own inserts bypass this function entirely,
                # which is what keeps historical backfill out.
                is_new_upload=True,
            )
        )
        new_count += 1

    for video_id in missing_duration_ids:
        if video_id in result.durations:
            existing_rows[video_id].duration_seconds = result.durations[video_id]

    # A video already in the DB (however it originally got there) that's
    # still part of the channel's current RSS window is, in the sense that
    # actually matters to a user, a "new upload" too — this is what makes
    # New Uploads self-heal/populate from whatever a channel's feed
    # currently shows on the next refresh, rather than staying permanently
    # empty for every video that existed before this column did. Same
    # Shorts guard as new inserts, using whatever duration is on file.
    for row in existing_rows.values():
        if row.is_new_upload:
            continue
        if row.duration_seconds is not None and row.duration_seconds <= SHORT_MAX_DURATION_SECONDS:
            continue
        row.is_new_upload = True

    db.commit()
    return new_count


def refresh_feeds(db: Session, feeds: list[Feed]) -> int:
    """Fetch and apply every given feed's latest RSS data — the fetch half
    fanned out across a thread pool, the DB half applied back sequentially
    on the caller's session. Shared by the on-demand /feeds/refresh endpoint
    (profile-scoped) and the background scheduler (every feed across every
    profile, no filter) — one feed's apply_feed_data failing must not abort
    every other feed's refresh in the same call, so each is isolated below
    rather than summed in one expression."""
    if not feeds:
        return 0

    with ThreadPoolExecutor(max_workers=min(len(feeds), REFRESH_POOL_SIZE)) as pool:
        results = list(pool.map(lambda f: fetch_feed_data(f.id, f.rss_url, f.avatar_url), feeds))

    new_count = 0
    for feed, result in zip(feeds, results, strict=True):
        try:
            new_count += apply_feed_data(db, feed, result)
        except Exception:
            db.rollback()
            logger.exception("Failed to apply feed data for feed %s (%s)", feed.id, feed.channel_title)
    return new_count


def cache_thumbnail(video_id: str, thumbnail_url: str) -> None:
    """The actual work behind pages.py's queue_thumbnail_caching — fetches
    once and rewrites every Content row sharing this video_id (there can be
    more than one: the same video followed/played/favorited under more than
    one profile). Meant to run as a FastAPI BackgroundTask, after the
    response that triggered it has already gone out with the original
    (still remote) URL — this call only ever affects the *next* render of
    this content, never the one that queued it."""
    local_url = download_thumbnail(video_id, thumbnail_url)
    if local_url and local_url != thumbnail_url:
        with SessionLocal() as db:
            db.query(Content).filter(
                Content.video_id == video_id, Content.thumbnail_url.like("%ytimg.com%")
            ).update({"thumbnail_url": local_url}, synchronize_session=False)
            db.commit()
