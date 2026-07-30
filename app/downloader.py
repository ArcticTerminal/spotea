from pathlib import Path

import yt_dlp

from app.config import settings

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


class DownloadError(Exception):
    pass


def download_audio(video_id: str) -> Path:
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(settings.storage_dir / f"{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
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
