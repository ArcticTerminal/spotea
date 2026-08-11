import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

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

    ydl_opts = {
        "format": FORMAT_BY_QUALITY.get(quality, FORMAT_BY_QUALITY["high"]),
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [postprocessor],
    }

    if on_progress is not None:
        ydl_opts["progress_hooks"] = [lambda event: _progress_hook(on_progress, event)]
        ydl_opts["postprocessor_hooks"] = [lambda event: _postprocessor_hook(on_progress, event)]

    url = YOUTUBE_WATCH_URL.format(video_id=video_id)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(str(exc)) from exc

    final_path = settings.storage_dir / f"{video_id}.{codec}"
    if not final_path.exists():
        raise DownloadError("Download completed but output file was not found")

    return final_path


def download_avatar(channel_id: str, avatar_url: str) -> str | None:
    """Fetch a channel avatar's bytes once and save them locally, returning a
    same-origin path to re-serve it from. Hotlinking Google's image CDN
    directly from the browser turned out to be unreliable — Chrome's Opaque
    Response Blocking (ORB) intermittently rejects it even for a URL that
    loaded fine moments earlier from the same page — so the app fetches once
    server-side (where that doesn't apply) instead of trusting the browser to
    load Google's URL every time."""
    settings.avatars_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.avatars_dir / f"{channel_id}.jpg"

    try:
        req = urllib.request.Request(avatar_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            dest.write_bytes(resp.read())
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    return f"/avatars/{dest.name}"
