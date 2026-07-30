import re
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")


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
    return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)


def fetch_feed(rss_url: str) -> ParsedFeed:
    try:
        parsed = feedparser.parse(rss_url)
    except Exception as exc:
        raise InvalidFeedError(f"Could not fetch RSS feed: {exc}") from exc

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
