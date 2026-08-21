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
# either way. "low" adds a <=64kbps cap, matching YouTube's separately
# pre-encoded itag 139 (~49kbps m4a) instead of the itag 140 (~130kbps) the
# high tier gets. Measured end to end: 3.78 MB against 1.43 MB.
#
# Whether that cap can be met is a property of the client, not of the video.
# Only some clients list itag 139 at all, and while the ladder was pinned to
# tv_simply — which doesn't — the first selector matched nothing on every
# video tried, "low" silently fell through to the same itag 140 as "high",
# and the setting did nothing. The formats under 64kbps that tv_simply does
# offer are all Opus, which `acodec^=mp4a` correctly excludes: taking one
# would mean transcoding to AAC on every download. So this setting only
# works as long as _ATTEMPTS leads with a client that carries itag 139.
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


class _YtdlpLogger:
    """Keeps yt-dlp's own output out of the container's stderr.

    `quiet` and `no_warnings` silence yt-dlp's progress and warnings but not
    its errors, which it writes straight to stderr regardless. So a rung
    that failed and was immediately recovered from still printed a bare
    `ERROR: unable to download video data: HTTP Error 403: Forbidden` with
    no video id and no attempt number beside it — indistinguishable, in the
    log, from a download that actually failed. Nothing was wrong; it just
    read as though something was.

    Nothing is lost by demoting these: the same message comes back on the
    DownloadError this raises, and the loop below logs it at WARNING with
    the video id and which rung it was. This is that message a second time,
    without the context, which is exactly what made it misleading.

    Warnings go to DEBUG rather than nowhere, which is not a detail. yt-dlp
    explains *why* it dropped formats only in a warning — "android_vr client
    https formats require a GVS PO Token which was not provided", "YouTube is
    forcing SABR streaming for this client" — and dropping those on the floor
    turned a one-line diagnosis into days of guessing at the ladder from 403s
    alone. Nothing routine is logged at DEBUG here, so turning it on when a
    client starts failing costs nothing the rest of the time.
    """

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp: %s", msg)


@dataclass(frozen=True)
class Attempt:
    """One shot at fetching a video: which YouTube client to ask as."""

    player_clients: tuple[str, ...]


# The failure this ladder exists for is rarely extraction — it's YouTube
# resolving a media URL and then refusing to serve it
# (`unable to download video data: HTTP Error 403`). Which client resolved
# that URL is what decides whether it gets served, so the rungs are client
# changes, not waits.
#
# Measured per client against the live instance, one extraction each plus a
# 1KB range GET on the mp4a URL it produced:
#
#   client        extract   audio-only mp4a       range GET   PO token
#   visionos      ~1.6s     itag 139 + 140        206         not needed
#   tv_simply     ~3.3s     itag 140              206         required
#   web_embedded  ~3.6s     itag 140              206         not needed
#   mweb          ~3.7s     itag 140 + 599        403 (2/3)   required
#   android_vr    ~1.5s     none offered          -           required
#   web/web_safari/ios/tv   nothing usable        -           -
#
# What this ladder got wrong before was not the measurement but the pinning.
# android_vr was pinned here on 14 Aug for being the fastest. YouTube began
# refusing it on 17 Aug — first everything but itag 18 (yt-dlp#17348), then
# itag 18 too — and yt-dlp dropped it from its own defaults the next day
# (yt-dlp#17461), shipping visionos in its place in 2026.08.19. The Dockerfile
# installs yt-dlp unpinned, so that fix was already in the image while this
# pin was still overriding it. The pin, not the client, is what cost three
# days of 403s. Hence test_downloader's assertion that visionos is still in
# yt-dlp's own _DEFAULT_CLIENTS: when upstream moves on again, the next image
# rebuild fails a test instead of quietly serving refusals.
#
# Two corrections to what was written here on 21 Aug, both from reading the
# warnings this module had been discarding:
#
#   - android_vr does return audio formats. yt-dlp *skips* them, on purpose,
#     because YouTube now demands a GVS PO token for them and bgutil can't
#     mint one for an Android client. Only the legacy muxed itag 18 survived
#     the cull, `bestaudio[...]` skipped it for having video, and the `/best`
#     tail matched it anyway — which is why a missing-format problem reached
#     the log as `unable to download video data: HTTP Error 403`.
#   - mweb offers the *most* audio formats of any client here, itag 599
#     included. Excluding it is still right, but for the other reason: its
#     URLs 403 even with a valid token, which is upstream's bug, not ours
#     (yt-dlp#17389).
#
# So the ladder leads with visionos, twice. It is the fastest measured, it is
# the only client offering itag 139 (which is what makes the "low" quality
# tier mean anything — see FORMAT_BY_QUALITY), and it needs no PO token —
# which is what let the token provider this used to depend on be deleted
# outright. Verified 9/9 across the live library: five audio-only formats
# every time, 206 every time.
#
# web_embedded is the last rung because it fails differently: a separate
# client family, also PO-token-free, and the fallback yt-dlp's own maintainers
# recommend (`player_client=default,web_embedded`). Asking yt-dlp for
# `default` instead would drag in `web`, which SABR has made useless, for
# 3x the extraction time.
#
# There are no sleeps between rungs — waiting does not make an
# already-rejected URL any more acceptable, and the refusals this exists for
# are per-URL, so a fresh extraction is the retry. The old 0s/2s/5s ladder
# spent its time proving that.
_ATTEMPTS = (
    Attempt(player_clients=("visionos",)),
    Attempt(player_clients=("visionos",)),
    Attempt(player_clients=("web_embedded",)),
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
            # Neither of the two above covers errors — see _YtdlpLogger.
            "logger": _YtdlpLogger(),
            "socket_timeout": SOCKET_TIMEOUT_SECONDS,
            "postprocessors": [postprocessor],
            # A second entry used to sit here pointing yt-dlp at a
            # PO-token provider running as a compose service. Neither client
            # in _ATTEMPTS needs one — both were measured serving audio with
            # that provider pointed at a dead port — so it and its container
            # are gone; docker-compose.yml records how to restore the pair if
            # YouTube extends the requirement to them.
            #
            # Should that happen, the symptom to expect is not an exception.
            # yt-dlp drops the formats it couldn't get a token for and carries
            # on, so a client that suddenly needs one goes quiet rather than
            # loud: `bestaudio[...]` matches nothing, FORMAT_BY_QUALITY's
            # `/best` tail picks whatever muxed stream is left, and YouTube
            # refuses *that* — surfacing as a 403 on download rather than as
            # the missing format it actually is. The reason is in a yt-dlp
            # warning, which _YtdlpLogger keeps at DEBUG.
            "extractor_args": {
                "youtube": {"player_client": list(attempt.player_clients)},
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
