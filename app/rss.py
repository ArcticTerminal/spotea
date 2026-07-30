import re
import urllib.parse
from calendar import timegm
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import yt_dlp

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
CHANNEL_ID_URL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
CHANNEL_ID_PARAM_RE = re.compile(r"channel_id=([\w-]+)")

RSS_FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
UPLOADS_PLAYLIST_URL_TEMPLATE = "https://www.youtube.com/playlist?list={playlist_id}"
CHANNEL_SHORTS_URL_TEMPLATE = "https://www.youtube.com/channel/{channel_id}/shorts"

# sp=EgIQAg%3D%3D restricts YouTube search results to the "Channel" type.
CHANNEL_SEARCH_URL_TEMPLATE = "https://www.youtube.com/results?search_query={query}&sp=EgIQAg%3D%3D"

_CHANNEL_RESOLVE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "0",
}

_CHANNEL_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-8",
}

_DURATION_FETCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
}

_SHORTS_FETCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
}


def extract_channel_id(rss_url: str) -> str | None:
    match = CHANNEL_ID_PARAM_RE.search(rss_url)
    return match.group(1) if match else None


class InvalidFeedError(Exception):
    pass


class ChannelResolutionError(Exception):
    pass


def resolve_feed_url(url: str) -> str:
    """Turn a YouTube channel URL (any form: @handle, /channel/UC.., /c/.., /user/..)
    or an already-direct RSS feed URL into a usable RSS feed URL."""
    if "feeds/videos.xml" in url:
        return url

    direct_match = CHANNEL_ID_URL_RE.search(url)
    if direct_match:
        return RSS_FEED_URL_TEMPLATE.format(channel_id=direct_match.group(1))

    try:
        with yt_dlp.YoutubeDL(_CHANNEL_RESOLVE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ChannelResolutionError(f"Could not resolve channel URL: {exc}") from exc

    channel_id = info.get("channel_id") if info else None
    if not channel_id:
        raise ChannelResolutionError("Could not determine a channel ID from this URL")

    return RSS_FEED_URL_TEMPLATE.format(channel_id=channel_id)


@dataclass
class ChannelSearchResult:
    channel_id: str
    title: str
    thumbnail_url: str | None
    subscriber_count: int | None
    channel_url: str


def _absolute_thumbnail_url(raw: str | None) -> str | None:
    if not raw:
        return None
    return f"https:{raw}" if raw.startswith("//") else raw


def search_channels(query: str) -> list[ChannelSearchResult]:
    search_url = CHANNEL_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    try:
        with yt_dlp.YoutubeDL(_CHANNEL_SEARCH_OPTS) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except yt_dlp.utils.DownloadError:
        return []

    results: list[ChannelSearchResult] = []
    for entry in (info or {}).get("entries") or []:
        channel_id = entry.get("channel_id") or entry.get("id")
        if not channel_id:
            continue

        thumbnails = entry.get("thumbnails") or []
        thumbnail_url = _absolute_thumbnail_url(thumbnails[-1].get("url")) if thumbnails else None

        results.append(
            ChannelSearchResult(
                channel_id=channel_id,
                title=entry.get("title") or entry.get("channel") or channel_id,
                thumbnail_url=thumbnail_url,
                subscriber_count=entry.get("channel_follower_count"),
                channel_url=entry.get("channel_url")
                or entry.get("url")
                or f"https://www.youtube.com/channel/{channel_id}",
            )
        )

    return results


def fetch_channel_video_durations(channel_id: str) -> dict[str, int]:
    """Video durations aren't in YouTube's channel RSS feed. yt-dlp's flat
    extraction of the channel's uploads playlist includes them cheaply though —
    no per-video fetch needed. The uploads playlist (id: "UU" + channel_id[2:])
    is always newest-first, unlike the /videos tab, whose sort order some
    channels override (e.g. "Popular"), which can leave recent uploads out of
    a bounded flat fetch."""
    uploads_playlist_id = "UU" + channel_id[2:] if channel_id.startswith("UC") else channel_id
    url = UPLOADS_PLAYLIST_URL_TEMPLATE.format(playlist_id=uploads_playlist_id)

    try:
        with yt_dlp.YoutubeDL(_DURATION_FETCH_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return {}

    durations: dict[str, int] = {}
    for entry in (info or {}).get("entries") or []:
        video_id = entry.get("id")
        duration = entry.get("duration")
        if video_id and isinstance(duration, (int, float)):
            durations[video_id] = int(duration)
    return durations


def fetch_channel_shorts_ids(channel_id: str) -> set[str]:
    """Video IDs YouTube itself classifies as Shorts for this channel (its
    dedicated Shorts tab), so they can be excluded — no duration heuristic,
    since Shorts length isn't strictly bounded to 60s."""
    url = CHANNEL_SHORTS_URL_TEMPLATE.format(channel_id=channel_id)

    try:
        with yt_dlp.YoutubeDL(_SHORTS_FETCH_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return set()

    return {entry["id"] for entry in (info or {}).get("entries") or [] if entry.get("id")}


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
