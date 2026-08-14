import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import yt_dlp

from app.config import settings

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

ProgressCallback = Callable[[str, int | None], None]

# Both tiers stick to the mp4a/AAC family so FFmpegExtractAudio can always
# remux into the m4a target instead of re-encoding — no local transcoding
# either way. "low" adds a <=64kbps cap, which YouTube already serves as a
# separate, much smaller pre-encoded stream (~48kbps) on virtually every
# video.
FORMAT_BY_QUALITY = {
    "high": "bestaudio[acodec^=mp4a]/bestaudio/best",
    "low": "bestaudio[acodec^=mp4a][abr<=64]/bestaudio[acodec^=mp4a]/bestaudio/best",
}


class DownloadError(Exception):
    pass


def _progress_hook(on_progress: ProgressCallback, event: dict) -> None:
    if event["status"] != "downloading":
        return
    total = event.get("total_bytes") or event.get("total_bytes_estimate")
    downloaded = event.get("downloaded_bytes")
    percent = int(downloaded / total * 100) if total and downloaded is not None else None
    on_progress("downloading", percent)


def _postprocessor_hook(on_progress: ProgressCallback, event: dict) -> None:
    # yt-dlp's pp_key() strips the "FFmpeg" prefix from postprocessor class
    # names, so FFmpegExtractAudioPP reports itself as "ExtractAudio" here.
    if event.get("postprocessor") == "ExtractAudio" and event["status"] == "started":
        on_progress("converting", None)


def download_audio(video_id: str, quality: str = "high", on_progress: ProgressCallback | None = None) -> Path:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(settings.storage_dir / f"{video_id}.%(ext)s")

    codec = settings.audio_format
    postprocessor = {"key": "FFmpegExtractAudio", "preferredcodec": codec}

    def build_ydl_opts(include_mweb: bool) -> dict:
        # android_vr alone covers the common case — every request this makes
        # to YouTube is doubled (a full extra player-API round trip) the
        # moment mweb joins the client list, and mweb's SABR formats also
        # drag in the remote_components fetch below. Both stay off the fast
        # path and only get pulled in from the second attempt onward, once
        # android_vr alone has actually failed once — a real fallback, not
        # something paid for on every single play.
        player_clients = ["android_vr", "mweb"] if include_mweb else ["android_vr"]
        opts = {
            "format": FORMAT_BY_QUALITY.get(quality, FORMAT_BY_QUALITY["high"]),
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [postprocessor],
            # A small per-request pause during extraction (yt-dlp's own
            # --sleep-requests) — a handful of formats/player-API calls
            # arriving back-to-back with zero delay reads as scripted in a
            # way spacing them out by a beat doesn't.
            "sleep_interval_requests": 1.5,
            # YouTube increasingly requires a PO (Proof-of-Origin) token even
            # for clients that never used to need one (android_vr's audio
            # formats, notably) — pot-provider (see docker-compose.yml)
            # generates these on request. Safe if pot-provider is unreachable
            # (e.g. running outside compose) — yt-dlp just drops the formats
            # that needed a token it couldn't get, rather than failing
            # outright.
            "extractor_args": {
                "youtube": {"player_client": player_clients},
                "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]},
            },
        }
        if include_mweb:
            # mweb's formats are SABR-gated and need this JS challenge solver
            # to descramble — downloads yt-dlp's solver script from GitHub on
            # first use and caches it (see Dockerfile's XDG_CACHE_HOME).
            opts["remote_components"] = ["ejs:github"]
        return opts

    if on_progress is not None:
        progress_hooks = [lambda event: _progress_hook(on_progress, event)]
        postprocessor_hooks = [lambda event: _postprocessor_hook(on_progress, event)]
    else:
        progress_hooks = postprocessor_hooks = None

    url = YOUTUBE_WATCH_URL.format(video_id=video_id)

    # Retry with growing backoff: the googlevideo playback URL yt-dlp resolves
    # can 403 (observed even without any code/version change between
    # attempts, both intermittently and — during a stretch of heavy same-IP
    # traffic — consistently for several attempts in a row). That pattern
    # points at short-lived, IP-level rate-limiting on YouTube's end as much
    # as anything client-side, so a single immediate retry often just re-hits
    # the same throttling window; these delays are long enough to plausibly
    # clear it.
    RETRY_DELAYS_SECONDS = [5, 15, 30]
    last_exc: yt_dlp.utils.DownloadError | None = None
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        if attempt:
            time.sleep(RETRY_DELAYS_SECONDS[attempt - 1])
        ydl_opts = build_ydl_opts(include_mweb=attempt > 0)
        if progress_hooks is not None:
            ydl_opts["progress_hooks"] = progress_hooks
            ydl_opts["postprocessor_hooks"] = postprocessor_hooks
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            last_exc = None
            break
        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc

    if last_exc is not None:
        raise DownloadError(str(last_exc)) from last_exc

    final_path = settings.storage_dir / f"{video_id}.{codec}"
    if not final_path.exists():
        raise DownloadError("Download completed but output file was not found")

    return final_path


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
