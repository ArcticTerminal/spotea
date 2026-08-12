import re
import time
import urllib.parse
from calendar import timegm
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
import yt_dlp

from app.config import settings
from app.downloader import YOUTUBE_WATCH_URL, download_avatar

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Fetching a channel's Videos-tab playlist (UULF, see longform_feed_url)
# instead of its full-uploads feed is supposed to exclude Shorts entirely —
# but that's an unofficial, undocumented YouTube convention, and in practice
# a Short occasionally still turns up there anyway. This is a defensive
# second check applied at insert time (see feed_sync.apply_feed_data and
# routers/feeds.py's _run_backfill) using each entry's already-fetched
# duration, not a replacement for the UULF trick — classic Shorts are ≤60s;
# YouTube widened the format to up to 3 minutes in 2024, but that upper
# range overlaps with plenty of legitimate short-form long-content videos,
# so this stays conservative rather than risk dropping real content.
SHORT_MAX_DURATION_SECONDS = 60
CHANNEL_ID_URL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
CHANNEL_ID_PARAM_RE = re.compile(r"channel_id=([\w-]+)")

RSS_FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
RSS_FEED_PLAYLIST_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?playlist_id={playlist_id}"
UPLOADS_PLAYLIST_URL_TEMPLATE = "https://www.youtube.com/playlist?list={playlist_id}"

# sp=EgIQAg%3D%3D restricts YouTube search results to the "Channel" type.
CHANNEL_SEARCH_URL_TEMPLATE = "https://www.youtube.com/results?search_query={query}&sp=EgIQAg%3D%3D"

# No type filter — Explore is searching for a specific video/song, and
# YouTube's default mixed results are already mostly videos. Adding a
# "Video" type filter isn't needed on top of that.
VIDEO_SEARCH_URL_TEMPLATE = "https://www.youtube.com/results?search_query={query}"

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

_VIDEO_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-8",
}

# Deliberately not flat: a flat search result's channel_id can be missing or
# ambiguous for a video credited to multiple channels (e.g. a feature) — see
# search_videos. This runs once, only on the single video the user picks.
_VIDEO_CHANNEL_RESOLVE_OPTS = {
    "quiet": True,
    "no_warnings": True,
}

_DURATION_FETCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
}

_BACKFILL_FETCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
}


def extract_channel_id(rss_url: str) -> str | None:
    match = CHANNEL_ID_PARAM_RE.search(rss_url)
    return match.group(1) if match else None


def _playlist_id(channel_id: str, prefix: str) -> str:
    """YouTube maps a channel to several special playlists by swapping the
    "UC" prefix of its channel ID: "UULF" is the Videos tab (long-form only,
    Shorts and Lives excluded), "UU" is all uploads mixed together. This is
    an undocumented but long-stable YouTube convention, not a public API."""
    return prefix + channel_id[2:] if channel_id.startswith("UC") else channel_id


def channel_feed_url(channel_id: str) -> str:
    """Canonical RSS feed URL for a channel_id — the single place this shape
    is built, so every code path that creates or upgrades a Feed row for a
    given channel_id agrees on its rss_url (see resolve_feed_url and
    routers/feeds.py's _get_or_create_placeholder_feed, whose dedup lookup
    depends on this being consistent)."""
    return RSS_FEED_URL_TEMPLATE.format(channel_id=channel_id)


def longform_feed_url(channel_id: str) -> str:
    """RSS feed scoped to a channel's Videos tab (UULF playlist) instead of
    all uploads — entries here are never Shorts, so callers don't need a
    separate Shorts-tab fetch to filter them out."""
    return RSS_FEED_PLAYLIST_URL_TEMPLATE.format(playlist_id=_playlist_id(channel_id, "UULF"))


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
        return channel_feed_url(direct_match.group(1))

    try:
        with yt_dlp.YoutubeDL(_CHANNEL_RESOLVE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ChannelResolutionError(f"Could not resolve channel URL: {exc}") from exc

    channel_id = info.get("channel_id") if info else None
    if not channel_id:
        raise ChannelResolutionError("Could not determine a channel ID from this URL")

    return channel_feed_url(channel_id)


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
    url = f"https:{raw}" if raw.startswith("//") else raw
    # yt3.googleusercontent.com serves the same images (same path) as
    # yt3.ggpht.com, but browsers hotlinking it cross-origin in an <img> tag
    # get net::ERR_BLOCKED_BY_ORB (Chrome's Opaque Response Blocking) — the
    # googleusercontent.com response is missing the headers ORB wants to
    # confirm it's really an image. ggpht.com (YouTube's dedicated image
    # CDN) sends them and renders fine, so rewrite to that host.
    return url.replace("//yt3.googleusercontent.com/", "//yt3.ggpht.com/")


def _cached_or_downloaded_avatar(channel_id: str, remote_url: str | None) -> str | None:
    """Same treatment as a followed channel's avatar (see download_avatar) —
    search result thumbnails are hotlinked from the browser too, and are just
    as exposed to Chrome's Opaque Response Blocking. Reuses the followed-
    channel cache by filename, so a channel already followed (or searched
    before) skips the download entirely."""
    if not remote_url:
        return None
    cached = settings.avatars_dir / f"{channel_id}.jpg"
    if cached.is_file():
        return f"/avatars/{channel_id}.jpg"
    return download_avatar(channel_id, remote_url)


def search_channels(query: str) -> list[ChannelSearchResult]:
    search_url = CHANNEL_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    try:
        with yt_dlp.YoutubeDL(_CHANNEL_SEARCH_OPTS) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except yt_dlp.utils.DownloadError:
        return []

    entries = []
    for entry in (info or {}).get("entries") or []:
        channel_id = entry.get("channel_id") or entry.get("id")
        if not channel_id:
            continue
        thumbnails = entry.get("thumbnails") or []
        remote_thumbnail_url = _absolute_thumbnail_url(thumbnails[-1].get("url")) if thumbnails else None
        entries.append((channel_id, entry, remote_thumbnail_url))

    # Downloaded in parallel — sequentially, ~10 results would add several
    # seconds to every keystroke's search request.
    with ThreadPoolExecutor(max_workers=min(len(entries), 8) or 1) as pool:
        thumbnail_urls = list(
            pool.map(lambda e: _cached_or_downloaded_avatar(e[0], e[2]), entries)
        )

    results: list[ChannelSearchResult] = []
    for (channel_id, entry, _), thumbnail_url in zip(entries, thumbnail_urls):
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


@dataclass
class VideoSearchResult:
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    channel_title: str | None


def search_videos(query: str) -> list[VideoSearchResult]:
    """Explore's search — finds a specific video/song rather than a channel
    to follow. Unlike search_channels, no avatar download step: video
    thumbnails are already hotlinked directly from i.ytimg.com elsewhere in
    the app (see _content_card.html), with none of the ORB blocking that
    makes channel avatars need the ggpht.com rewrite/local download."""
    search_url = VIDEO_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    try:
        with yt_dlp.YoutubeDL(_VIDEO_SEARCH_OPTS) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except yt_dlp.utils.DownloadError:
        return []

    results: list[VideoSearchResult] = []
    for entry in (info or {}).get("entries") or []:
        video_id = entry.get("id")
        if not video_id or not VIDEO_ID_RE.match(video_id):
            # Drops non-video results for free — Mix/Radio pseudo-entries
            # come back with "RD"-prefixed ids, never 11 chars.
            continue

        thumbnails = entry.get("thumbnails") or []
        duration = entry.get("duration")

        results.append(
            VideoSearchResult(
                video_id=video_id,
                title=entry.get("title") or "Untitled",
                thumbnail_url=_absolute_thumbnail_url(thumbnails[-1]["url"]) if thumbnails else None,
                duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
                channel_title=entry.get("channel"),
            )
        )

    return results


def resolve_video_channel(video_id: str) -> str | None:
    """The authoritative channel_id for one specific video — a flat search
    result's channel_id (see search_videos) can be missing or ambiguous for
    a video credited to multiple channels (e.g. "feat." collaborations), so
    adding a video to the library re-resolves it with a real, non-flat
    lookup first. Only ever called once, on the video the user picks."""
    url = YOUTUBE_WATCH_URL.format(video_id=video_id)

    try:
        with yt_dlp.YoutubeDL(_VIDEO_CHANNEL_RESOLVE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return None

    return (info or {}).get("channel_id")


def fetch_channel_avatar_url(channel_id: str) -> str | None:
    """Channel avatar (profile picture) — not in the RSS feed or any playlist
    extraction, so a separate lightweight fetch of the channel page itself is
    needed. playlist_items=0 (the same _CHANNEL_RESOLVE_OPTS resolve_feed_url
    uses) skips fetching any videos, just the channel-level metadata. Only
    called once per channel — see _fetch_feed_data in routers/feeds.py,
    which skips this once a feed already has an avatar_url."""
    url = f"https://www.youtube.com/channel/{channel_id}"

    try:
        with yt_dlp.YoutubeDL(_CHANNEL_RESOLVE_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return None

    thumbnails = (info or {}).get("thumbnails") or []
    # The last entry is normally the highest-resolution one, but here it's
    # "avatar_uncropped" — a raw googleusercontent URL (bare "=s0") that
    # browsers won't render when hotlinked cross-origin. The entry just
    # before it is the same image through Google's cropped/processed image
    # pipeline ("=s900-c-k-c0x00ffffff-no-rj"), which does render — the same
    # URL shape search_channels' results already use successfully.
    candidates = [t for t in thumbnails if "uncropped" not in (t.get("id") or "")]
    pool = candidates or thumbnails
    return _absolute_thumbnail_url(pool[-1]["url"]) if pool else None


def fetch_channel_video_durations(channel_id: str) -> dict[str, int]:
    """Video durations aren't in YouTube's channel RSS feed. yt-dlp's flat
    extraction of the channel's Videos-tab playlist (UULF, see _playlist_id)
    includes them cheaply though — no per-video fetch needed, and Shorts are
    excluded for free since UULF never contains them. That playlist is
    always newest-first, unlike the /videos tab, whose sort order some
    channels override (e.g. "Popular"), which can leave recent uploads out
    of a bounded flat fetch."""
    url = UPLOADS_PLAYLIST_URL_TEMPLATE.format(playlist_id=_playlist_id(channel_id, "UULF"))

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


@dataclass
class BackfillEntry:
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None


_PAGE_LOG_RE = re.compile(r"page (\d+): Downloading API JSON")
_ITEM_LOG_RE = re.compile(r"Downloading item (\d+) of (\d+)")

# BackfillProgress: ("listing", page_number, 0) while yt-dlp is still paging
# through the channel (this is the slow, network-bound part — each page is a
# request), or ("counting", done, total) once the full item count is known
# and it's just iterating over already-fetched data (fast).
BackfillProgress = tuple[str, int, int]


class _ScanProgressLogger:
    """Feeds yt-dlp's own debug log lines — which already say "page 7:
    Downloading API JSON" and "Downloading item 412 of 1037" — into an
    on_progress callback. There's no public progress-hook API for playlist
    listing (progress_hooks is download-only), so this is the only way to
    get any signal during what can be a genuinely slow scan for a channel
    with a very long history."""

    def __init__(self, on_progress: Callable[[BackfillProgress], None]):
        self._on_progress = on_progress

    def debug(self, msg: str) -> None:
        item_match = _ITEM_LOG_RE.search(msg)
        if item_match:
            self._on_progress(("counting", int(item_match.group(1)), int(item_match.group(2))))
            return
        page_match = _PAGE_LOG_RE.search(msg)
        if page_match:
            self._on_progress(("listing", int(page_match.group(1)), 0))

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def fetch_channel_all_videos(
    channel_id: str, on_progress: Callable[[BackfillProgress], None] | None = None
) -> list[BackfillEntry]:
    """Every long-form video in the channel's Videos tab (UULF playlist), not
    just the ~15 most recent ones the RSS feed exposes. A single flat
    extraction covers id, title, thumbnail and duration together, newest
    first, with Shorts excluded — meant for a one-time backfill when a
    channel is first added, not routine refreshes."""
    url = UPLOADS_PLAYLIST_URL_TEMPLATE.format(playlist_id=_playlist_id(channel_id, "UULF"))

    opts = _BACKFILL_FETCH_OPTS
    if on_progress:
        opts = {**_BACKFILL_FETCH_OPTS, "logger": _ScanProgressLogger(on_progress)}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ChannelResolutionError(f"Could not list channel videos: {exc}") from exc

    entries: list[BackfillEntry] = []
    for entry in (info or {}).get("entries") or []:
        video_id = entry.get("id")
        if not video_id or not VIDEO_ID_RE.match(video_id):
            continue

        thumbnails = entry.get("thumbnails") or []
        thumbnail_url = thumbnails[-1]["url"] if thumbnails else None
        duration = entry.get("duration")

        entries.append(
            BackfillEntry(
                video_id=video_id,
                title=entry.get("title") or "Untitled",
                thumbnail_url=thumbnail_url,
                duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
            )
        )

    return entries


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
