"""Following an artist.

Every way of following something goes through here, so this is where "is
this actually an artist?" gets answered — once, on the server. It used to be
the client's answer, arriving as a request field only a client that had
already opened the artist's profile could fill in; the detail panel's Follow
button got it right and nothing else did.

Now it is the *only* answer: a channel YouTube Music can't read as an artist
cannot be followed at all. That is the music-only scope in one rule — the
library holds artists, and a page that isn't one has nothing this app would
sync from it.
"""

from sqlalchemy.orm import Session

from app.models import Feed
from app.youtube.music import fetch_artist
from app.youtube.urls import CHANNEL_ID_RE, channel_feed_url, extract_channel_id


class FeedAlreadyExistsError(Exception):
    def __init__(self, rss_url: str, channel_title: str | None):
        super().__init__(rss_url)
        self.channel_title = channel_title


class NotAnArtistError(Exception):
    """What came back isn't a musician's page, so there is nothing to follow."""


def _resolve_artist(channel_id: str) -> tuple[str, str, str]:
    """The feed key, browse id and display name for an artist, from any id
    that opens their page.

    `get_artist` accepts more than one id for the same person — the Topic
    channel id that song results and chart entries carry, *and* their
    official channel id (measured: both open Shirin David's page and both
    report the same channelId back). Whichever came in, what gets stored is
    the browse id off the page that actually has the music: a VEVO container
    answers with the right name and no songs, and music._redirected_artist
    walks from there to the page that does.

    all_songs=False: this needs the ids and the name off the page header,
    and the "Top songs" playlist behind them is a second request nobody here
    reads.
    """
    artist = fetch_artist(channel_id, all_songs=False)
    if artist is None:
        raise NotAnArtistError("This channel isn't an artist on YouTube Music")

    # The feed's own key stays the "<Artist> - Topic" channel where there is
    # one. Nothing fetches it any more (see services/artist_sync.py), but it
    # is what makes "follow the official channel" and "follow the Topic
    # channel" resolve to the same row rather than two.
    key_channel_id = artist.topic_channel_id or artist.channel_id or artist.browse_id
    return channel_feed_url(key_channel_id), artist.browse_id, artist.name


def create_feed_from_rss_url(
    db: Session,
    rss_url: str,
    user_id: int,
    sync: bool = True,
) -> tuple[Feed, int]:
    """DB half of following an artist, given a URL that names a channel.

    The artist resolution runs before the duplicate check on purpose, because
    it can change which feed this even is: following an artist's official
    channel when their Topic channel is already followed is a duplicate, and
    only says so once both have been reduced to the same key.

    A matching Feed can already exist with followed=False — a placeholder
    created for a track the user only grabbed one of (see routers/explore.py's
    _get_or_create_placeholder_feed), or an artist unfollowed while keeping
    some content. Following now upgrades that row in place rather than
    bouncing the user with "already exists" for a feed they never knowingly
    added.

    `sync=False` stops once the row exists, leaving the first sync to
    services/backfill.run_initial_sync — what a request-serving caller wants,
    since nothing waiting on the response needs it.
    """
    channel_id = extract_channel_id(rss_url)
    if not channel_id or not CHANNEL_ID_RE.match(channel_id):
        raise NotAnArtistError("This doesn't look like a YouTube channel")

    rss_url, artist_browse_id, name = _resolve_artist(channel_id)

    existing = db.query(Feed).filter(Feed.user_id == user_id, Feed.rss_url == rss_url).first()
    if existing and existing.followed:
        raise FeedAlreadyExistsError(rss_url, existing.channel_title)

    if existing:
        feed = existing
        feed.followed = True
        feed.channel_title = name
        feed.artist_browse_id = artist_browse_id
    else:
        feed = Feed(
            user_id=user_id,
            rss_url=rss_url,
            channel_title=name,
            artist_browse_id=artist_browse_id,
        )
        db.add(feed)
    db.commit()
    db.refresh(feed)

    if not sync:
        return feed, 0

    from app.services.artist_sync import apply_artist_data, fetch_artist_data

    result = fetch_artist_data(feed.artist_browse_id, feed.release_snapshot, feed.avatar_url)
    return feed, apply_artist_data(db, feed, result)


def add_feed_core(
    db: Session,
    channel_url: str,
    user_id: int,
    sync: bool = True,
) -> tuple[Feed, int]:
    """Follow an artist from whatever URL the caller has. The only entry
    point routes use."""
    return create_feed_from_rss_url(db, channel_url.strip(), user_id, sync=sync)
