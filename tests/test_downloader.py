"""The retry ladder in app/downloader.py.

YouTube itself is never contacted here — yt_dlp.YoutubeDL is replaced. What's
being pinned down is the ladder's shape: which clients each attempt asks for,
that a later attempt can rescue an earlier failure, and that the whole thing
gives up quickly. The measurements behind the client choices are in the
comment on _ATTEMPTS.
"""

import time

import pytest
import yt_dlp

from app import downloader


class _FakeYDL:
    """Stands in for yt_dlp.YoutubeDL, recording the options it was built
    with and failing (or not) as the test dictates."""

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        _FakeYDL.calls.append(self.opts)
        outcome = _FakeYDL.outcomes[len(_FakeYDL.calls) - 1]
        if outcome is not None:
            raise yt_dlp.utils.DownloadError(outcome)


@pytest.fixture
def fake_ydl(monkeypatch, tmp_path):
    """Swaps in _FakeYDL and points storage at tmp_path."""
    _FakeYDL.calls = []
    _FakeYDL.outcomes = []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(downloader.settings, "storage_dir", tmp_path)
    return _FakeYDL


def _clients_of(opts):
    return opts["extractor_args"]["youtube"]["player_client"]


def test_the_fast_client_goes_first(fake_ydl, tmp_path):
    """android_vr resolves in ~1.4s against tv_simply's ~2.9s and works most
    of the time, so it's what a play pays for in the common case."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 1
    assert _clients_of(fake_ydl.calls[0]) == ["android_vr"]


def test_web_safari_is_not_asked_for_anywhere(fake_ydl):
    """It was carried as android_vr's fallback and never was one: YouTube
    forces SABR on it, so every https format comes back without a URL and
    yt-dlp drops all of them (silently, under no_warnings). Keeping it looked
    like redundancy while providing none."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    for call in fake_ydl.calls:
        assert "web_safari" not in _clients_of(call)
        assert "mweb" not in _clients_of(call)  # measured: PO-bound URL, still 403s


def test_a_refused_url_falls_back_to_the_po_token_client(fake_ydl, tmp_path):
    """The whole point of the ladder: android_vr's media URL carries no PO
    token and gets refused at random, tv_simply's is token-bound and gets
    served."""
    fake_ydl.outcomes = ["ERROR: unable to download video data: HTTP Error 403: Forbidden", None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    path = downloader.download_audio("vid00000001")

    assert path.name == "vid00000001.m4a"
    assert len(fake_ydl.calls) == 2
    assert _clients_of(fake_ydl.calls[1]) == ["tv_simply"]


def test_no_attempt_waits_before_taking_its_shot(fake_ydl, monkeypatch):
    """The refusals are per-URL, and a fresh extraction produces a fresh URL
    — sitting still first doesn't make the next one more acceptable. The
    ladder this replaced slept 2s then 5s to prove that."""
    slept = []
    monkeypatch.setattr(time, "sleep", slept.append)
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert slept == []


def test_every_attempt_reports_that_it_started_resolving(fake_ydl, tmp_path):
    """Extraction is the slow part and moves no bytes, so without this the
    client has nothing to show for the first 1.4-3s of a play."""
    fake_ydl.outcomes = ["403", None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")
    events = []

    downloader.download_audio("vid00000001", on_progress=lambda phase, pct: events.append((phase, pct)))

    assert events == [("extracting", None), ("extracting", None)]


def test_a_hung_request_cannot_hold_the_ladder_open(fake_ydl, tmp_path):
    """Without a socket timeout, one request that stops answering blocks
    every remaining rung — which is the failure the client's old 3s restart
    watchdog was built to paper over."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert fake_ydl.calls[0]["socket_timeout"] == downloader.SOCKET_TIMEOUT_SECONDS


def test_giving_up_takes_three_attempts(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 3


def test_the_error_surfaced_is_the_last_one_seen(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "ERROR: Video unavailable"]

    with pytest.raises(downloader.DownloadError, match="Video unavailable"):
        downloader.download_audio("vid00000001")


def test_a_download_that_produces_no_file_is_an_error(fake_ydl):
    """yt-dlp reporting success without the expected output would otherwise
    mark the row ready and leave playback 404ing on a missing file."""
    fake_ydl.outcomes = [None]

    with pytest.raises(downloader.DownloadError, match="output file was not found"):
        downloader.download_audio("vid00000001")


def test_no_extraction_level_sleep_is_configured(fake_ydl, tmp_path):
    """sleep_interval_requests spaced out yt-dlp's own internal requests,
    costing 1.5s on every play — including the ones that worked — with no
    measured effect on how often YouTube refused."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert "sleep_interval_requests" not in fake_ydl.calls[0]


def test_an_unavailable_video_stops_the_ladder_on_the_first_attempt(fake_ydl):
    """Every rung is a different client, and this class of refusal is the
    same on all of them — so the remaining two attempts can only re-confirm
    it, at the cost of two more extractions against a YouTube that rate-limits
    on request volume."""
    fake_ydl.outcomes = ["ERROR: [youtube] abc: Video unavailable. This video is not available"]

    with pytest.raises(downloader.VideoUnavailableError):
        downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 1


def test_a_refusal_that_might_pass_next_time_still_uses_the_whole_ladder(fake_ydl):
    """The counterpart to the test above: a 403 is precisely the failure a
    different client can get past, so nothing about it should short-circuit."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError) as raised:
        downloader.download_audio("vid00000001")

    assert not isinstance(raised.value, downloader.VideoUnavailableError)
    assert len(fake_ydl.calls) == 3


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] x: Video unavailable. This video is not available",
        "ERROR: [youtube] x: The uploader has not made this video available in your country",
        "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
        "ERROR: [youtube] x: This video is no longer available because the uploader has closed",
        "ERROR: [youtube] x: Join this channel to get access to members-only content",
        "ERROR: [youtube] x: Sign in to confirm your age",
    ],
)
def test_settled_refusals_are_recognised(message):
    assert downloader.is_permanent_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "ERROR: [youtube] x: Sign in to confirm you're not a bot",
        "ERROR: Unable to download webpage: timed out",
        "",
        None,
    ],
)
def test_retryable_refusals_are_left_alone(message):
    """The bot check especially: it means YouTube is refusing *us* right now,
    not that the video is unplayable — treating it as settled would write off
    a whole library's worth of perfectly good tracks during one bad minute."""
    assert not downloader.is_permanent_failure(message)


def test_quality_selects_the_capped_format(fake_ydl, tmp_path):
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001", quality="low")

    assert "abr<=64" in fake_ydl.calls[0]["format"]
