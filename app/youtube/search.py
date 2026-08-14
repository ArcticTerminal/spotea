"""YouTube search, for Explore's one search box.

Same tool as extract.py (yt-dlp, flat extraction) but a different trigger:
this runs while someone is typing, so it's debounced client-side, capped at
8 results, and returns [] rather than raising when YouTube says no — a
failed search should show "nothing found", not an error page.
"""

import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import yt_dlp

from app.images import cached_avatar_path, download_avatar
from app.youtube.urls import (
    CHANNEL_SEARCH_URL_TEMPLATE,
    VIDEO_ID_RE,
    VIDEO_SEARCH_URL_TEMPLATE,
    absolute_thumbnail_url,
)

_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "1-8",
}

# Avatar downloads run in parallel — sequentially, ~8 results would add
# several seconds to every keystroke's search request.
_AVATAR_POOL_SIZE = 8


@dataclass
class ChannelSearchResult:
    channel_id: str
    title: str
    thumbnail_url: str | None
    subscriber_count: int | None
    channel_url: str


@dataclass
class VideoSearchResult:
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    channel_title: str | None


def _search_entries(search_url: str) -> list[dict]:
    try:
        with yt_dlp.YoutubeDL(_SEARCH_OPTS) as ydl:
            info = ydl.extract_info(search_url, download=False)
    except yt_dlp.utils.DownloadError:
        return []
    return (info or {}).get("entries") or []


def _cached_or_downloaded_avatar(channel_id: str, remote_url: str | None) -> str | None:
    """A search result's avatar as a same-origin path. Hotlinked avatars are
    exposed to Chrome's Opaque Response Blocking the same way a followed
    channel's is (see images.download_avatar), and the cache is keyed by
    channel id alone — so a channel already followed, or searched for
    earlier, costs nothing here."""
    if not remote_url:
        return None
    return cached_avatar_path(channel_id) or download_avatar(channel_id, remote_url)


def search_channels(query: str) -> list[ChannelSearchResult]:
    search_url = CHANNEL_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    entries = []
    for entry in _search_entries(search_url):
        channel_id = entry.get("channel_id") or entry.get("id")
        if not channel_id:
            continue
        thumbnails = entry.get("thumbnails") or []
        remote_thumbnail_url = absolute_thumbnail_url(thumbnails[-1].get("url")) if thumbnails else None
        entries.append((channel_id, entry, remote_thumbnail_url))

    if not entries:
        return []

    with ThreadPoolExecutor(max_workers=min(len(entries), _AVATAR_POOL_SIZE)) as pool:
        thumbnail_urls = list(pool.map(lambda e: _cached_or_downloaded_avatar(e[0], e[2]), entries))

    return [
        ChannelSearchResult(
            channel_id=channel_id,
            title=entry.get("title") or entry.get("channel") or channel_id,
            thumbnail_url=thumbnail_url,
            subscriber_count=entry.get("channel_follower_count"),
            channel_url=entry.get("channel_url")
            or entry.get("url")
            or f"https://www.youtube.com/channel/{channel_id}",
        )
        for (channel_id, entry, _), thumbnail_url in zip(entries, thumbnail_urls, strict=True)
    ]


def search_videos(query: str) -> list[VideoSearchResult]:
    """Finds a specific video/song rather than a channel to follow. Unlike
    search_channels, no avatar download step: video thumbnails are already
    hotlinked directly from i.ytimg.com elsewhere in the app (see
    _content_card.html), with none of the ORB blocking that makes channel
    avatars need the ggpht.com rewrite/local download."""
    search_url = VIDEO_SEARCH_URL_TEMPLATE.format(query=urllib.parse.quote(query))

    results: list[VideoSearchResult] = []
    for entry in _search_entries(search_url):
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
                thumbnail_url=absolute_thumbnail_url(thumbnails[-1]["url"]) if thumbnails else None,
                duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
                channel_title=entry.get("channel"),
            )
        )

    return results
