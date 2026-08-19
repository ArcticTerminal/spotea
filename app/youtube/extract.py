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
    CHANNEL_ID_RE,
    CHANNEL_ID_URL_RE,
    CHANNEL_PAGE_URL_TEMPLATE,
    VIDEO_ID_RE,
    YOUTUBE_WATCH_URL,
    absolute_thumbnail_url,
    channel_feed_url,
    extract_channel_id,
    is_youtube_url,
    longform_playlist_url,
)

# Same value and same reasoning as downloader.py's SOCKET_TIMEOUT_SECONDS, but
# it had never been applied to these metadata calls — which is worse than for a
# download, because these run inside a request (POST /feeds) and inside
# feed_sync's 8-thread refresh pool. With no socket_timeout yt-dlp inherits the
# OS TCP timeout, so a host that accepts the connection and stops answering
# pins the thread for minutes.
METADATA_SOCKET_TIMEOUT_SECONDS = 10

# The timeout alone isn't a bound: yt-dlp's defaults (retries=10,
# extractor_retries=3) multiply it, which is how a "10 second timeout" turns
# back into minutes. Two attempts is enough for the transient case — and unlike
# downloader.py there's no client ladder behind these to make a stubborn retry
# worthwhile.
METADATA_RETRIES = 2

# Shared by every extraction in this module and by search.py, which runs the
# same kind of call from a keystroke.
NETWORK_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": METADATA_SOCKET_TIMEOUT_SECONDS,
    "retries": METADATA_RETRIES,
    "extractor_retries": METADATA_RETRIES,
}

# playlist_items="0" fetches channel-level metadata without any videos.
_CHANNEL_RESOLVE_OPTS = {
    **NETWORK_OPTS,
    "extract_flat": "in_playlist",
    "playlist_items": "0",
}

_DURATION_FETCH_OPTS = {
    **NETWORK_OPTS,
    "extract_flat": "in_playlist",
    "playlist_items": "1-50",
}

class ChannelResolutionError(Exception):
    pass


def resolve_feed_url(url: str) -> str:
    """Turn a YouTube channel URL (any form: @handle, /channel/UC.., /c/.., /user/..)
    or an already-direct RSS feed URL into a usable RSS feed URL."""
    url = url.strip()
    # Every branch below either fetches this URL or hands it to yt-dlp, whose
    # error text reaches the client — see is_youtube_url for what that allowed.
    # Rejected here as well as in fetch_feed so the user gets the real reason
    # instead of a channel-resolution failure from yt-dlp.
    if not is_youtube_url(url):
        raise ChannelResolutionError("Only youtube.com and youtu.be URLs can be followed")

    if "feeds/videos.xml" in url:
        # Canonicalised through channel_feed_url rather than returned as typed,
        # so an "http://" or "m.youtube.com" spelling of a feed the user already
        # follows is still recognised as that feed — create_feed_from_rss_url
        # dedups on the exact rss_url string. A playlist feed (playlist_id=UULF..,
        # what longform_feed_url builds) has no channel_id to canonicalise and
        # passes through untouched.
        channel_id = extract_channel_id(url)
        if channel_id and CHANNEL_ID_RE.match(channel_id):
            return channel_feed_url(channel_id)
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
