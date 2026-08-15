"""yt-dlp audio extraction. Image caching lives in app/images.py."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yt_dlp

from app.config import settings
from app.youtube.urls import YOUTUBE_WATCH_URL

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Attempt:
    """One shot at fetching a video, and how long to wait before taking it."""

    player_clients: tuple[str, ...]
    delay_seconds: float
    needs_js_challenge_solver: bool = False


# A failed play costs the user the whole ladder, so it is deliberately
# short. It used to be four attempts with 5s/15s/30s between them — around
# 70 seconds, nearly all of it sleeping — on the theory that these 403s are
# IP-level rate-limiting that only time clears.
#
# Measured against the live instance, the waiting was doing almost none of
# the work:
#   - The refusals are per-request, not per-IP and not per-video. In one
#     minute, one video downloaded end to end in 2.2s while another 403'd on
#     four consecutive attempts across four different client and network
#     configurations (android_vr alone, yt-dlp's default pair, forced IPv4,
#     Chrome TLS impersonation). Half an hour later the video that had
#     worked first time needed a second attempt itself.
#   - When a retry does help, it helps immediately: that second attempt
#     succeeded after a 2s wait, for 5.9s in total. Nothing observed
#     suggested 15s or 30s buys anything a couple of seconds doesn't.
#   - The transfer itself is never the problem — 5.4MB arrived in under a
#     second at 11MB/s. The cost is entirely in resolving a URL YouTube is
#     willing to honour.
#
# So: still three attempts, because retrying genuinely works, but the whole
# ladder now gives up in well under twenty seconds instead of seventy.
#
# The client lists matter more than the waiting:
#   - android_vr + web_safari is yt-dlp's own default pairing for this
#     version (_DEFAULT_CLIENTS). web_safari costs no extra round trip —
#     it's also yt-dlp's webpage client (_DEFAULT_WEBPAGE_CLIENT), so its
#     player response comes from the watch page that was fetched anyway.
#     Pinning android_vr alone, as this used to, threw that fallback away
#     for nothing — and android_vr on its own has been erratic since March
#     2026 (yt-dlp#16150), sometimes returning only format 18.
#   - mweb is a genuinely different client family, worth one last try. It
#     needs a GVS PO token and the JS challenge solver, which is why it
#     isn't on the fast path.
_ATTEMPTS = (
    Attempt(player_clients=("android_vr", "web_safari"), delay_seconds=0),
    Attempt(player_clients=("android_vr", "web_safari"), delay_seconds=2),
    Attempt(player_clients=("mweb",), delay_seconds=5, needs_js_challenge_solver=True),
)


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

    def build_ydl_opts(attempt: "Attempt") -> dict:
        opts = {
            "format": FORMAT_BY_QUALITY.get(quality, FORMAT_BY_QUALITY["high"]),
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [postprocessor],
            # YouTube requires a PO (Proof-of-Origin) token for several
            # clients' formats — pot-provider (see docker-compose.yml)
            # generates these on request. Safe if pot-provider is unreachable
            # (e.g. running outside compose): yt-dlp drops the formats that
            # needed a token it couldn't get rather than failing outright.
            "extractor_args": {
                "youtube": {"player_client": list(attempt.player_clients)},
                "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]},
            },
        }
        if attempt.needs_js_challenge_solver:
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

    last_exc: yt_dlp.utils.DownloadError | None = None
    for attempt in _ATTEMPTS:
        if attempt.delay_seconds:
            time.sleep(attempt.delay_seconds)
        ydl_opts = build_ydl_opts(attempt)
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
            logger.info(
                "Download attempt failed for %s (clients=%s): %s",
                video_id, ",".join(attempt.player_clients), str(exc)[:200],
            )

    if last_exc is not None:
        raise DownloadError(str(last_exc)) from last_exc

    final_path = settings.storage_dir / f"{video_id}.{codec}"
    if not final_path.exists():
        raise DownloadError("Download completed but output file was not found")

    return final_path
