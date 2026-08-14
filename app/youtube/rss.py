"""The feedparser half of YouTube access: a channel's RSS feed.

This is the cheap, well-behaved path — a plain XML fetch of the ~15 most
recent uploads, no yt-dlp involved. It's what routine refreshes use (see
feed_sync.py). Anything RSS doesn't expose — durations, avatars, a channel's
full history — needs extract.py instead.
"""

import time
from calendar import timegm
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser

from app.youtube.urls import VIDEO_ID_RE


class InvalidFeedError(Exception):
    pass


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


def fetch_feed(rss_url: str) -> ParsedFeed:
    # One retry after a beat: feedparser has no timeout/retry of its own, and
    # a momentary network blip or YouTube-side hiccup fetching the XML looks
    # identical to a genuinely invalid feed (empty/unparseable response) —
    # this gives transient failures a second chance before surfacing the
    # error to the user.
    last_exc: Exception | None = None
    parsed = None
    for attempt in range(2):
        if attempt:
            time.sleep(1)
        try:
            parsed = feedparser.parse(rss_url)
            last_exc = None
        except Exception as exc:
            last_exc = exc
            continue
        if parsed.get("feed") and parsed.feed.get("yt_channelid"):
            break

    if last_exc is not None:
        raise InvalidFeedError(f"Could not fetch RSS feed: {last_exc}") from last_exc

    if not parsed.get("feed") or not parsed.feed.get("yt_channelid"):
        raise InvalidFeedError("URL is not a valid YouTube channel RSS feed")

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
