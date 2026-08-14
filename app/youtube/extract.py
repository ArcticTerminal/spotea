"""The yt-dlp half of YouTube access: everything RSS doesn't expose.

Channel resolution, per-video durations, channel avatars and the one-time
full-history scan. All of it is unauthenticated scraping, so every call here
is one YouTube requests from this IP — callers are expected to avoid asking
twice for the same thing (see feed_sync.fetch_feed_data, which skips the
avatar lookup once a feed has one, and the duration lookup unless something
is actually missing).

Search lives in search.py rather than here: it's the same tool, but it's
driven by a user typing rather than by a feed being synced.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass

import yt_dlp

from app.youtube.urls import (
    CHANNEL_ID_URL_RE,
    CHANNEL_PAGE_URL_TEMPLATE,
    VIDEO_ID_RE,
    YOUTUBE_WATCH_URL,
    absolute_thumbnail_url,
    channel_feed_url,
    longform_playlist_url,
)

# playlist_items="0" fetches channel-level metadata without any videos.
_CHANNEL_RESOLVE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": "in_playlist",
    "playlist_items": "0",
}

# Deliberately not flat: a flat search result's channel_id can be missing or
# ambiguous for a video credited to multiple channels (e.g. a feature) — see
# resolve_video_channel. This runs once, only on the single video the user picks.
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


def resolve_video_channel(video_id: str) -> str | None:
    """The authoritative channel_id for one specific video — a flat search
    result's channel_id (see search.search_videos) can be missing or
    ambiguous for a video credited to multiple channels (e.g. "feat."
    collaborations), so adding a video to the library re-resolves it with a
    real, non-flat lookup first. Only ever called once, on the video the
    user picks."""
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
    needed. Only called once per channel — see feed_sync.fetch_feed_data,
    which skips this once a feed already has an avatar_url."""
    url = CHANNEL_PAGE_URL_TEMPLATE.format(channel_id=channel_id)

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
    # URL shape search results already use successfully.
    candidates = [t for t in thumbnails if "uncropped" not in (t.get("id") or "")]
    pool = candidates or thumbnails
    return absolute_thumbnail_url(pool[-1]["url"]) if pool else None


def fetch_channel_video_durations(channel_id: str) -> dict[str, int]:
    """Video durations aren't in YouTube's channel RSS feed. yt-dlp's flat
    extraction of the channel's Videos-tab playlist includes them cheaply
    though — no per-video fetch needed, and Shorts are excluded for free."""
    try:
        with yt_dlp.YoutubeDL(_DURATION_FETCH_OPTS) as ydl:
            info = ydl.extract_info(longform_playlist_url(channel_id), download=False)
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
    """Every long-form video in the channel's Videos tab, not just the ~15
    most recent ones the RSS feed exposes. A single flat extraction covers
    id, title, thumbnail and duration together, newest first, with Shorts
    excluded — meant for a one-time backfill when a channel is first added,
    not routine refreshes."""
    opts = _BACKFILL_FETCH_OPTS
    if on_progress:
        opts = {**_BACKFILL_FETCH_OPTS, "logger": _ScanProgressLogger(on_progress)}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(longform_playlist_url(channel_id), download=False)
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
