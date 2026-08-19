"""Creating (or re-following) a Feed row from a channel URL.

Shared by the single-add route and bulk import so the two can never
diverge on what following a channel actually does — including, since
_as_artist_follow below, the decision of *what* actually gets followed when
the channel belongs to a musician.
"""

from sqlalchemy.orm import Session

from app.feed_sync import apply_feed_data, fetch_feed_data
from app.models import Feed
from app.youtube.extract import resolve_feed_url
from app.youtube.music import fetch_artist
from app.youtube.rss import fetch_feed
from app.youtube.urls import (
    CHANNEL_ID_RE,
    channel_feed_url,
    extract_channel_id,
    strip_topic_suffix,
)


class FeedAlreadyExistsError(Exception):
    def __init__(self, rss_url: str, channel_title: str | None):
        super().__init__(rss_url)
        self.channel_title = channel_title


def _as_artist_follow(rss_url: str, artist_browse_id: str | None) -> tuple[str, str | None]:
    """Redirects a channel follow onto the artist's "<Artist> - Topic"
    channel, when the channel turns out to belong to a musician.

    This is the server answering "is this an artist?", and it lives here
    because this is the one function every way of following something goes
    through. It used to be the *client's* answer: `artist_browse_id` arrives
    as a request field, and the only client that can fill it in is one that
    already opened the artist's profile and read the id off it. So the
    detail panel's Follow button got this right and nothing else did — the
    onboarding wizard, Explore's Add button and bulk import all followed the
    channel the artist also vlogs on. The wizard could not have got it right
    either: it deliberately never opens the profile, because a full-panel
    navigation out from under its modal would strand it half-finished.

    Costs one request, and only when the caller hasn't already answered. A
    channel that isn't a musician's pays that request and nothing else:
    YouTube Music answers a non-artist with a page its parser cannot read,
    which fetch_artist flattens to None (measured on a podcast and on a tech
    channel), and the caller carries on with the URL it came in with. That
    is why following a podcast is untouched by any of this.

    For the artists it does catch it *replaces* work rather than adding it:
    an artist follow skips the one-time history scan (see routers/feeds.py),
    and that scan reads the channel's entire upload list.
    """
    if artist_browse_id:
        return rss_url, artist_browse_id

    # A playlist feed (playlist_id=UULF.., what longform_feed_url builds) has
    # no channel id in it and is nothing YouTube Music could answer for, so
    # it never reaches the network.
    channel_id = extract_channel_id(rss_url)
    if not channel_id or not CHANNEL_ID_RE.match(channel_id):
        return rss_url, None

    # all_songs=False: all this needs is the ids off the page header, and the
    # "Top songs" playlist behind them is a second request (see _artist_songs).
    artist = fetch_artist(channel_id, all_songs=False)
    if artist is None or not artist.topic_channel_id:
        return rss_url, None

    # Recorded as the browse id: the id that was followed, not the Topic
    # channel it resolved to. get_artist accepts either (measured — an
    # artist's official channel id opens their page too), and this is the one
    # that reopens the profile from the library card.
    return channel_feed_url(artist.topic_channel_id), artist.browse_id


def create_feed_from_rss_url(
    db: Session,
    rss_url: str,
    user_id: int,
    artist_browse_id: str | None = None,
    sync: bool = True,
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
    the artist, so it comes off here. A caller that already knows passes it
    in; every other caller gets the same answer worked out for it by
    _as_artist_follow, which is why the two branches below stopped being
    "the artist path" and "the channel path".

    That resolution runs before the duplicate check on purpose, because it
    can change which feed this even is: following an artist's official
    channel when their Topic channel is already followed is a duplicate, and
    only says so once both have been reduced to the same RSS URL.

    A matching Feed can already exist with followed=False — a placeholder
    created by add_single_video for a channel the user only grabbed one
    video from (see routers/explore.py's _get_or_create_placeholder_feed). Actually following it
    now means upgrading that row in place (flip followed, run the same
    fetch/backfill a brand-new feed gets) rather than bouncing the user with
    "already exists" for a feed they never knowingly added.

    `sync=False` stops after the row exists, leaving the content sync to
    services/backfill.run_initial_sync — what a request-serving caller wants,
    since the durations and avatar behind that sync are two yt-dlp calls and
    two seconds (measured) that nothing waiting on the response needs. Bulk
    import keeps the default: it is already off the request thread and wants
    each channel finished before starting the next."""
    rss_url, artist_browse_id = _as_artist_follow(rss_url, artist_browse_id)

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

    channel_id = extract_channel_id(rss_url)
    if not sync:
        return feed, 0, channel_id

    result = fetch_feed_data(feed.id, feed.rss_url, feed.avatar_url)
    new_count = apply_feed_data(db, feed, result)
    return feed, new_count, channel_id


def add_feed_core(
    db: Session,
    channel_url: str,
    user_id: int,
    artist_browse_id: str | None = None,
    sync: bool = True,
) -> tuple[Feed, int, str | None]:
    """Resolve, validate, save a feed, and (unless `sync=False`) apply its
    first RSS parse. Shared by the single-add route and bulk import so the
    two never diverge."""
    rss_url = resolve_feed_url(channel_url.strip())
    return create_feed_from_rss_url(
        db, rss_url, user_id, artist_browse_id=artist_browse_id, sync=sync
    )
