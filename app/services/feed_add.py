"""Creating (or re-following) a Feed row from a channel URL.

Shared by the single-add route and bulk import so the two can never
diverge on what following a channel actually does.
"""

from sqlalchemy.orm import Session

from app.feed_sync import apply_feed_data, fetch_feed_data
from app.models import Feed
from app.youtube.extract import resolve_feed_url
from app.youtube.rss import fetch_feed
from app.youtube.urls import extract_channel_id, strip_topic_suffix


class FeedAlreadyExistsError(Exception):
    def __init__(self, rss_url: str, channel_title: str | None):
        super().__init__(rss_url)
        self.channel_title = channel_title


def create_feed_from_rss_url(
    db: Session, rss_url: str, user_id: int, artist_browse_id: str | None = None
) -> tuple[Feed, int, str | None]:
    """DB-and-remaining-fetch half of adding a feed, given an already-resolved
    RSS URL. Split out from add_feed_core so bulk import can resolve many
    URLs in parallel first (see services/bulk_import.py) and then only run this,
    strictly sequential, part per line. Callers decide how to run the
    returned channel_id's backfill — deferred via BackgroundTasks for a
    single add (keeps the response fast), inline for bulk import (which is
    already running off the request thread).

    `artist_browse_id` marks this as an artist follow (see
    Feed.artist_browse_id): the RSS URL then points at their "<Artist> -
    Topic" channel, whose own title carries a suffix that would make the
    library card read "Shirin David - Topic" — the card is meant to read as
    the artist, so it comes off here.

    A matching Feed can already exist with followed=False — a placeholder
    created by add_single_video for a channel the user only grabbed one
    video from (see routers/explore.py's _get_or_create_placeholder_feed). Actually following it
    now means upgrading that row in place (flip followed, run the same
    fetch/backfill a brand-new feed gets) rather than bouncing the user with
    "already exists" for a feed they never knowingly added."""
    existing = db.query(Feed).filter(Feed.user_id == user_id, Feed.rss_url == rss_url).first()
    if existing and existing.followed:
        raise FeedAlreadyExistsError(rss_url, existing.channel_title)

    parsed = fetch_feed(rss_url)
    channel_title = parsed.channel_title
    if artist_browse_id:
        channel_title = strip_topic_suffix(channel_title)

    if existing:
        feed = existing
        feed.followed = True
        if channel_title:
            feed.channel_title = channel_title
        feed.artist_browse_id = artist_browse_id or feed.artist_browse_id
        db.commit()
    else:
        feed = Feed(
            user_id=user_id,
            rss_url=rss_url,
            channel_title=channel_title,
            artist_browse_id=artist_browse_id,
        )
        db.add(feed)
        db.commit()
        db.refresh(feed)

    result = fetch_feed_data(feed.id, feed.rss_url, feed.avatar_url)
    new_count = apply_feed_data(db, feed, result)

    channel_id = extract_channel_id(rss_url)
    return feed, new_count, channel_id


def add_feed_core(
    db: Session, channel_url: str, user_id: int, artist_browse_id: str | None = None
) -> tuple[Feed, int, str | None]:
    """Resolve, validate, save a feed, and apply its first RSS parse. Shared
    by the single-add route and bulk import so the two never diverge."""
    rss_url = resolve_feed_url(channel_url.strip())
    return create_feed_from_rss_url(db, rss_url, user_id, artist_browse_id=artist_browse_id)
