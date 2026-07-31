from pathlib import Path
from typing import Callable

import yt_dlp

from app.config import settings

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"

ProgressCallback = Callable[[str, int | None], None]


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


def download_audio(video_id: str, on_progress: ProgressCallback | None = None) -> Path:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(settings.storage_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        # Preferring the native m4a/AAC stream (present on virtually every video)
        # lets FFmpegExtractAudio remux instead of re-encode when the target
        # format is also m4a — turns a duration-scaled transcode into a near
        # instant container fixup.
        "format": "bestaudio[acodec^=mp4a]/bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": settings.audio_format,
            }
        ],
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

    final_path = settings.storage_dir / f"{video_id}.{settings.audio_format}"
    if not final_path.exists():
        raise DownloadError("Download completed but output file was not found")

    return final_path
