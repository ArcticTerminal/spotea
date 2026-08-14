"""Creating (or re-following) a Feed row from a channel URL.

Shared by the single-add route and bulk import so the two can never
diverge on what following a channel actually does.
"""

from sqlalchemy.orm import Session

from app.feed_sync import apply_feed_data, fetch_feed_data
from app.models import Feed
from app.youtube.extract import resolve_feed_url
from app.youtube.rss import fetch_feed
from app.youtube.urls import extract_channel_id


class FeedAlreadyExistsError(Exception):
    def __init__(self, rss_url: str, channel_title: str | None):
        super().__init__(rss_url)
        self.channel_title = channel_title


def create_feed_from_rss_url(db: Session, rss_url: str, user_id: int) -> tuple[Feed, int, str | None]:
    """DB-and-remaining-fetch half of adding a feed, given an already-resolved
    RSS URL. Split out from add_feed_core so bulk import can resolve many
    URLs in parallel first (see services/bulk_import.py) and then only run this,
    strictly sequential, part per line. Callers decide how to run the
    returned channel_id's backfill — deferred via BackgroundTasks for a
    single add (keeps the response fast), inline for bulk import (which is
    already running off the request thread).

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

    if existing:
        feed = existing
        feed.followed = True
        if parsed.channel_title:
            feed.channel_title = parsed.channel_title
        db.commit()
    else:
        feed = Feed(user_id=user_id, rss_url=rss_url, channel_title=parsed.channel_title)
        db.add(feed)
        db.commit()
        db.refresh(feed)

    result = fetch_feed_data(feed.id, feed.rss_url, feed.avatar_url)
    new_count = apply_feed_data(db, feed, result)

    channel_id = extract_channel_id(rss_url)
    return feed, new_count, channel_id


def add_feed_core(db: Session, channel_url: str, user_id: int) -> tuple[Feed, int, str | None]:
    """Resolve, validate, save a feed, and apply its first RSS parse. Shared
    by the single-add route and bulk import so the two never diverge."""
    rss_url = resolve_feed_url(channel_url.strip())
    return create_feed_from_rss_url(db, rss_url, user_id)
