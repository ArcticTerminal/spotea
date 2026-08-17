"""The feedparser half of YouTube access: a channel's RSS feed.

This is the cheap, well-behaved path — a plain XML fetch of the ~15 most
recent uploads, no yt-dlp involved. It's what routine refreshes use (see
feed_sync.py). Anything RSS doesn't expose — durations, avatars, a channel's
full history — needs extract.py instead.

The HTTP fetch is done here rather than by handing feedparser a URL, for one
reason: `feedparser.parse(url)` takes no timeout, and there is no setting for
one. It also never raises — a DNS failure, a refused connection, a timeout and
YouTube's 429 all come back as a perfectly ordinary empty feed with `bozo`
set, which is how every one of those used to be reported to the user as "URL
is not a valid YouTube channel RSS feed". Fetching first and parsing bytes
makes network failure an exception again, and puts a bound on it.
"""

import http.client
import time
import urllib.request
from calendar import timegm
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser

from app.youtube.urls import VIDEO_ID_RE, is_youtube_url


class FeedError(Exception):
    """Anything that stopped a channel's RSS feed from being read.

    Callers that only need "this feed didn't work" (feed_sync's refresh pool,
    bulk import's per-line result) catch this; the HTTP layer distinguishes
    the two subclasses below, because one is the user's fault and the other
    isn't.
    """


class InvalidFeedError(FeedError):
    """The response arrived and isn't a YouTube channel feed. The URL is
    wrong; retrying it will produce the same answer."""


class FeedUnavailableError(FeedError):
    """The feed could not be fetched at all — DNS, connection, timeout, or an
    HTTP error status such as the 429 YouTube answers a too-eager refresh
    with. Nothing is wrong with the URL, so this is worth retrying later."""


# Long enough that a slow-but-alive YouTube still answers, short enough that a
# host which accepts the connection and then goes quiet doesn't hold the
# thread for the OS TCP timeout — which is what happened before, on a request
# thread for POST /feeds and on one of feed_sync.REFRESH_POOL_SIZE pool
# threads for a refresh. Same value and same reasoning as downloader.py's
# SOCKET_TIMEOUT_SECONDS.
FETCH_TIMEOUT_SECONDS = 10

# A channel feed is ~15 entries and well under 50 KB. The cap exists because
# read() is otherwise unbounded and this is now our socket rather than
# feedparser's; it is deliberately far above any real feed, so hitting it
# means the response isn't a feed at all.
MAX_FEED_BYTES = 4 * 1024 * 1024

FETCH_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1


@dataclass
class ParsedEntry:
    video_id: str
    title: str
    thumbnail_url: str | None
    published_at: datetime | None


@dataclass
class ParsedFeed:
    channel_title: str
    entries: list[ParsedEntry]


def _parse_published(entry) -> datetime | None:
    parsed = entry.get("published_parsed")
    if parsed is None:
        return None
    # Naive UTC, matching app.timeutil.utcnow — see its docstring for why
    # every datetime reaching a model column has to agree on this.
    return datetime.fromtimestamp(timegm(parsed), tz=UTC).replace(tzinfo=None)


def _fetch_bytes(rss_url: str) -> bytes:
    request = urllib.request.Request(
        rss_url,
        headers={
            # feedparser's own User-Agent, which is what YouTube has been
            # answering all along. Moving the fetch into this module is not a
            # reason for the request on the wire to look different.
            "User-Agent": feedparser.USER_AGENT,
            "Accept": "application/atom+xml, application/xml, text/xml",
        },
    )

    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_FEED_BYTES + 1)

    if len(body) > MAX_FEED_BYTES:
        raise FeedUnavailableError(f"RSS feed is larger than {MAX_FEED_BYTES} bytes")
    return body


def _fetch_with_retry(rss_url: str) -> bytes:
    """One retry after a beat: a momentary network blip or a YouTube-side
    hiccup is common enough to be worth a second attempt and cheap enough to
    pay for. Only *fetch* failures retry — a response that arrived and isn't a
    channel feed will say exactly the same thing next time, which is why the
    parse below sits outside this loop."""
    last_error: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_DELAY_SECONDS)
        try:
            return _fetch_bytes(rss_url)
        except (OSError, http.client.HTTPException) as exc:
            # urlopen raises URLError (an OSError) for DNS/connection/timeout
            # and HTTPError for a 4xx/5xx; http.client contributes the
            # protocol-level ones, e.g. IncompleteRead, which is not an
            # OSError.
            last_error = exc

    raise FeedUnavailableError(f"Could not fetch RSS feed: {last_error}") from last_error


def fetch_feed(rss_url: str) -> ParsedFeed:
    if not is_youtube_url(rss_url):
        raise InvalidFeedError("Only youtube.com RSS feeds can be fetched")

    parsed = feedparser.parse(_fetch_with_retry(rss_url))

    if not parsed.get("feed") or not parsed.feed.get("yt_channelid"):
        # feedparser reports a parse failure as `bozo` instead of raising, so
        # without this the reason — an HTML error page, a truncated document —
        # was discarded and every case read the same.
        detail = f" ({parsed.get('bozo_exception')})" if parsed.bozo else ""
        raise InvalidFeedError(f"URL is not a valid YouTube channel RSS feed{detail}")

    entries: list[ParsedEntry] = []
    for entry in parsed.entries:
        video_id = entry.get("yt_videoid")
        if not video_id or not VIDEO_ID_RE.match(video_id):
            continue

        thumbnails = entry.get("media_thumbnail") or []
        thumbnail_url = thumbnails[0]["url"] if thumbnails else None

        entries.append(
            ParsedEntry(
                video_id=video_id,
                title=entry.get("title", "Untitled"),
                thumbnail_url=thumbnail_url,
                published_at=_parse_published(entry),
            )
        )

    return ParsedFeed(channel_title=parsed.feed.get("title", rss_url), entries=entries)
