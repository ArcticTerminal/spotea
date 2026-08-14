"""Fetching and caching remote images (channel avatars, video thumbnails).

Split out of downloader.py, which is otherwise entirely about yt-dlp and
audio extraction. The two had nothing in common but the word "download",
and keeping them together meant app.youtube — a metadata layer that has no
business knowing about audio downloads — had to import from the audio
downloader just to cache an avatar.
"""

import urllib.error
import urllib.request
from pathlib import Path

from app.config import settings


def _download_image(directory: Path, filename: str, image_url: str, url_prefix: str) -> str | None:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / filename
    if dest.is_file():
        return f"{url_prefix}/{dest.name}"

    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            dest.write_bytes(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    return f"{url_prefix}/{dest.name}"


def download_avatar(channel_id: str, avatar_url: str) -> str | None:
    """Fetch a channel avatar's bytes once and save them locally, returning a
    same-origin path to re-serve it from. Hotlinking Google's image CDN
    directly from the browser turned out to be unreliable — Chrome's Opaque
    Response Blocking (ORB) intermittently rejects it even for a URL that
    loaded fine moments earlier from the same page — so the app fetches once
    server-side (where that doesn't apply) instead of trusting the browser to
    load Google's URL every time."""
    return _download_image(settings.avatars_dir, f"{channel_id}.jpg", avatar_url, "/avatars")


def download_thumbnail(video_id: str, thumbnail_url: str) -> str | None:
    """Same deal as download_avatar, for a video's thumbnail — re-served from
    our own origin instead of every Home/Library/Explore render hitting
    i*.ytimg.com directly for every card on screen. Safe to call for videos
    already known (e.g. every entry in a freshly-fetched RSS feed, not just
    new ones) — _download_image's on-disk check makes repeat calls a no-op
    file stat rather than a redundant fetch."""
    return _download_image(settings.thumbnails_dir, f"{video_id}.jpg", thumbnail_url, "/thumbnails")


def cached_avatar_path(channel_id: str) -> str | None:
    """Same-origin path for an avatar already on disk, or None. Lets a caller
    skip a download for a channel that's already been followed or searched
    for before (see youtube/search.py)."""
    if (settings.avatars_dir / f"{channel_id}.jpg").is_file():
        return f"/avatars/{channel_id}.jpg"
    return None
