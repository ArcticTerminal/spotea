"""yt-dlp audio extraction. Image caching lives in app/images.py."""

import logging
import re
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

# A single request that stops answering shouldn't hold the whole ladder open —
# the next rung is a fresh extraction that usually just works, so failing over
# beats waiting. Long enough that a slow-but-alive response still completes.
SOCKET_TIMEOUT_SECONDS = 10


class DownloadError(Exception):
    pass


class VideoUnavailableError(DownloadError):
    """YouTube itself won't serve this video to us, and no retry will change
    that — see is_permanent_failure below."""


# YouTube answers an extraction with a `playabilityStatus`, and only some of
# its outcomes are worth another attempt. These are the ones that aren't: the
# video is gone, private, members-only, age-gated behind a sign-in we don't
# have, or — by far the most common case here — simply not licensed in this
# country.
#
# That last one is not a corner case. Nearly every music track in a library
# like this comes from a "<Artist> - Topic" channel, which is YouTube Music's
# auto-generated art-track upload, and those are licensed *per country*. A
# track whose id isn't licensed here answers UNPLAYABLE / "Video unavailable"
# to every client there is — confirmed against android_vr, tv_simply, tv,
# tv_embedded, web, web_safari, web_embedded, web_music, web_creator, mweb,
# ios, ios_music, android and android_music, and against a plain browser
# request for the watch page, which returns the same status with no yt-dlp in
# the picture at all. The video record still exists (oEmbed answers 200), so
# nothing upstream of here can tell it apart from a healthy one.
#
# Recognising these matters for two reasons. Running the rest of the ladder
# spends two more extractions to be told the same thing, and that request
# volume is itself a contributor to the 403/bot-check failures the ladder
# exists for. And it lets everything above this treat them as settled rather
# than as "failed, try again next time you open it" — see Content.is_unavailable.
_PERMANENT_FAILURE_PATTERNS = (
    r"video unavailable",
    r"this video is not available",
    r"not made this video available in your country",
    r"no longer available",
    r"private video",
    r"removed by the uploader",
    r"account associated with this video has been terminated",
    r"members[- ]only",
    r"join this channel",
    r"sign in to confirm your age",
    r"age[- ]restricted",
)
_PERMANENT_FAILURE_RE = re.compile("|".join(_PERMANENT_FAILURE_PATTERNS), re.IGNORECASE)


def is_permanent_failure(message: str | None) -> bool:
    """Whether a yt-dlp error message means "don't bother trying again"."""
    return bool(message) and _PERMANENT_FAILURE_RE.search(message) is not None


@dataclass(frozen=True)
class Attempt:
    """One shot at fetching a video: which YouTube client to ask as."""

    player_clients: tuple[str, ...]


# The failure this ladder exists for is never extraction — it's YouTube
# resolving a media URL and then refusing to serve it
# (`unable to download video data: HTTP Error 403`). Which client resolved
# that URL is what decides whether it gets served, so the rungs are client
# changes, not waits.
#
# Measured per client against the live instance, one extraction each plus a
# 2-byte range GET on the URL it produced:
#
#   client        extract   usable mp4a URL             range GET
#   android_vr    ~1.4s     yes, no PO token            206 (but 403s at random)
#   tv_simply     ~2.9s     yes, PO-token-bound         206
#   web_safari    ~1.5s     NONE — SABR-forced, no URLs  -
#   mweb          ~3.7s     yes, PO-token-bound         403
#   web_embedded  ~3.3s     yes, no PO token            403
#   ios           ~1.3s     NONE — needs a GVS token     -
#
# Two things that had been assumed here turned out to be false:
#
#   - web_safari was carried as android_vr's fallback. It has none to give:
#     YouTube forces SABR on it (yt-dlp#12482), so every https format comes
#     back without a URL and yt-dlp drops all of them. With `no_warnings`
#     on, it said so silently. Every download this app has ever served came
#     from android_vr alone, which is exactly why a 403 from android_vr had
#     nothing to fall back to.
#   - mweb was the "genuinely different client family" last rung. Its URL is
#     PO-token-bound and still 403s, so it was costing 3.7s and a JS solver
#     fetch to fail.
#
# tv_simply replaces both. It is the only client measured whose media URL
# carries a GVS PO token *and* gets served — which is the mechanism, not a
# coincidence: the token is what YouTube checks. It needs a JS runtime for
# nsig, but the image already has Deno, so no `remote_components` fetch.
#
# android_vr stays in front because it's twice as fast when it works, and it
# works most of the time. When it doesn't, the cost of finding out is ~1.4s,
# and tv_simply picks up immediately — the same format 140 at the same
# bitrate, so nothing about the audio changes.
#
# There are no sleeps between rungs. The refusals are per-URL: a fresh
# extraction produces a fresh URL, and waiting first doesn't make that URL
# any more acceptable. The old 0s/2s/5s ladder spent its time proving that.
_ATTEMPTS = (
    Attempt(player_clients=("android_vr",)),
    Attempt(player_clients=("tv_simply",)),
    Attempt(player_clients=("tv_simply",)),
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
        return {
            "format": FORMAT_BY_QUALITY.get(quality, FORMAT_BY_QUALITY["high"]),
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": SOCKET_TIMEOUT_SECONDS,
            "postprocessors": [postprocessor],
            # YouTube requires a PO (Proof-of-Origin) token for several
            # clients' formats — pot-provider (see docker-compose.yml)
            # generates these on request. Safe if pot-provider is unreachable
            # (e.g. running outside compose): yt-dlp drops the formats that
            # needed a token it couldn't get rather than failing outright.
            # That degrades tv_simply to nothing, so android_vr — which uses
            # no token at all — is what still works without the container.
            "extractor_args": {
                "youtube": {"player_client": list(attempt.player_clients)},
                "youtubepot-bgutilhttp": {"base_url": ["http://pot-provider:4416"]},
            },
        }

    if on_progress is not None:
        progress_hooks = [lambda event: _progress_hook(on_progress, event)]
        postprocessor_hooks = [lambda event: _postprocessor_hook(on_progress, event)]
    else:
        progress_hooks = postprocessor_hooks = None

    url = YOUTUBE_WATCH_URL.format(video_id=video_id)

    last_exc: yt_dlp.utils.DownloadError | None = None
    for number, attempt in enumerate(_ATTEMPTS, start=1):
        # Resolving a URL YouTube will honour is the slow part (1.4-3s) and
        # produces no byte progress of its own, so without this the client
        # has nothing to show between "download started" and the first
        # percentage — see player.js's checkStatus.
        if on_progress is not None:
            on_progress("extracting", None)

        ydl_opts = build_ydl_opts(attempt)
        if progress_hooks is not None:
            ydl_opts["progress_hooks"] = progress_hooks
            ydl_opts["postprocessor_hooks"] = postprocessor_hooks
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if number > 1:
                logger.info(
                    "Download of %s recovered on attempt %d/%d (clients=%s)",
                    video_id, number, len(_ATTEMPTS), ",".join(attempt.player_clients),
                )
            last_exc = None
            break
        except yt_dlp.utils.DownloadError as exc:
            last_exc = exc
            # WARNING, not INFO: nothing configures the root logger below
            # WARNING under uvicorn, and this ladder spent a day retrying
            # invisibly because the old INFO call here never reached a
            # handler. Failures are rare enough to be worth the level.
            logger.warning(
                "Download attempt %d/%d failed for %s (clients=%s): %s",
                number, len(_ATTEMPTS), video_id, ",".join(attempt.player_clients), str(exc)[:200],
            )
            # The rungs are client changes, and this class of failure is the
            # same on every client (see is_permanent_failure) — so there is
            # nothing left to try. Stop rather than spend the remaining
            # attempts confirming it.
            if is_permanent_failure(str(exc)):
                logger.warning("Giving up on %s: unavailable to every client", video_id)
                raise VideoUnavailableError(str(exc)) from exc

    if last_exc is not None:
        raise DownloadError(str(last_exc)) from last_exc

    final_path = settings.storage_dir / f"{video_id}.{codec}"
    if not final_path.exists():
        raise DownloadError("Download completed but output file was not found")

    return final_path
