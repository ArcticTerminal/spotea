"""Fetching and caching remote images (channel avatars, video thumbnails).

Split out of downloader.py, which is otherwise entirely about yt-dlp and
audio extraction. The two had nothing in common but the word "download",
and keeping them together meant app.youtube — a metadata layer that has no
business knowing about audio downloads — had to import from the audio
downloader just to cache an avatar.
"""

import os
import urllib.error
import urllib.request
from pathlib import Path

from app.config import settings

FETCH_TIMEOUT_SECONDS = 10

# Far above any real thumbnail (~30 KB) or avatar (~50 KB). It exists because
# read() is otherwise unbounded and this writes straight to disk.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Renamed into place from this, rather than written to the destination directly.
# The suffix is distinct from storage.py's EXPORT_TEMP_SUFFIX so a sweeper can
# tell the two apart.
_TEMP_SUFFIX = ".download.tmp"


def _download_image(directory: Path, filename: str, image_url: str, url_prefix: str) -> str | None:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / filename
    if dest.is_file():
        return f"{url_prefix}/{dest.name}"

    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    if not body or len(body) > MAX_IMAGE_BYTES:
        return None

    # dest.is_file() above is the *only* cache check there is, and nothing ever
    # re-downloads an image that exists — so a write cut short (a container
    # killed by `docker compose up --build` mid-refresh, a dropped connection)
    # used to leave a truncated JPEG that every later call accepted as a
    # complete cached image, permanently. os.replace is atomic within a
    # directory, so the destination only ever appears whole.
    temp = dest.with_name(dest.name + _TEMP_SUFFIX)
    try:
        temp.write_bytes(body)
        os.replace(temp, dest)
    except OSError:
        temp.unlink(missing_ok=True)
        return None

    return f"{url_prefix}/{dest.name}"


def fetch_image_bytes(image_url: str) -> tuple[bytes, str] | None:
    """A remote image's bytes and content-type, fetched once and handed back
    in memory rather than written to disk — see routers using this for why
    (proxying an avatar that's never been followed/searched-for before, so it
    doesn't earn a permanent local copy the way download_avatar's callers do,
    but still needs a same-origin response to dodge Chrome's ORB — see
    download_avatar's docstring)."""
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read(MAX_IMAGE_BYTES + 1)
            content_type = resp.headers.get_content_type()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    if not body or len(body) > MAX_IMAGE_BYTES or not content_type.startswith("image/"):
        return None
    return body, content_type


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
