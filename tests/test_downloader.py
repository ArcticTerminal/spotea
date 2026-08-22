"""The retry ladder in app/downloader.py.

YouTube itself is never contacted here — yt_dlp.YoutubeDL is replaced. What's
being pinned down is the ladder's shape: which clients each attempt asks for,
that a later attempt can rescue an earlier failure, and that the whole thing
gives up quickly. The measurements behind the client choices are in the
comment on _ATTEMPTS.
"""

import logging
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


def test_the_client_that_gets_served_goes_first(fake_ydl, tmp_path):
    """visionos was the fastest client measured (~1.6s against tv_simply's
    ~3.3s), served 9/9 tracks from the live library, and needs no PO token,
    so it's what a play pays for from the first attempt."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 1
    assert _clients_of(fake_ydl.calls[0]) == ["visionos"]


def test_the_pinned_client_is_one_upstream_still_defends(fake_ydl):
    """The guard this ladder didn't have. android_vr was pinned here for
    being fastest; YouTube broke it, yt-dlp dropped it from its own defaults
    and shipped visionos in its place the next day, and because the
    Dockerfile installs yt-dlp unpinned that fix was sitting in the image
    while this pin overrode it. Nothing said so — downloads just 403'd.

    Asserting the lead client is still in yt-dlp's default list turns the
    next such move into a failed test on the rebuild that introduces it,
    which is the only part of that episode worth preventing: the pin was
    fine, its silence wasn't."""
    from yt_dlp.extractor.youtube import YoutubeIE

    lead = downloader._ATTEMPTS[0].player_clients
    assert set(lead) <= set(YoutubeIE._DEFAULT_CLIENTS), (
        f"_ATTEMPTS leads with {lead}, which yt-dlp {yt_dlp.version.__version__} no longer "
        f"defaults to ({YoutubeIE._DEFAULT_CLIENTS}). Either upstream dropped it and this "
        f"ladder needs re-measuring, or the yt-dlp here is older than the image's."
    )


def test_the_clients_measured_not_to_deliver_are_not_asked_for(fake_ydl):
    """Each was on this ladder at some point and each was measured failing,
    though not all for the reason once written here. android_vr does return
    audio formats — yt-dlp skips them, deliberately, because YouTube demands
    a GVS PO token bgutil can't mint for an Android client, leaving only the
    muxed itag 18 that FORMAT_BY_QUALITY's `/best` tail then picked up. And
    mweb offers *more* audio formats than any client here; its URLs simply
    403 anyway, with a valid token, which is an upstream bug."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    for call in fake_ydl.calls:
        clients = _clients_of(call)
        assert "android_vr" not in clients  # measured: every format 403s, itag 18 included
        assert "web_safari" not in clients  # measured: SABR-forced, no URLs at all
        assert "mweb" not in clients  # measured: valid token, still 403 on 2 of 3


def test_the_last_rung_is_a_different_client_from_the_first(fake_ydl):
    """Repeating the lead client is worth a rung because refusals are
    per-URL, but not the last one: if visionos itself is what's broken,
    a third go at it is the one thing guaranteed not to help. web_embedded
    is a separate client family, also PO-token-free, and the fallback
    yt-dlp's own maintainers recommend."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert _clients_of(fake_ydl.calls[-1]) != _clients_of(fake_ydl.calls[0])


def test_ytdlp_does_not_write_its_own_errors_to_stderr(fake_ydl):
    """`quiet` and `no_warnings` don't cover errors — yt-dlp prints those
    regardless. A rung that failed and was recovered from on the next one
    still left a bare `ERROR: ...` in the log with no video id beside it,
    which read as a failed download when nothing had failed. Handing yt-dlp
    a logger is the only way to stop that; the loop's own WARNING, which has
    the id and the attempt number, is where that message belongs."""
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    for call in fake_ydl.calls:
        assert isinstance(call["logger"], downloader._YtdlpLogger)


def test_the_reason_a_format_was_dropped_is_still_recoverable(caplog):
    """Warnings are where yt-dlp puts the only explanation of a missing
    format — "requires a GVS PO Token which was not provided", "forcing SABR
    streaming for this client". Discarding them left this ladder to be
    diagnosed from bare 403s, which took days and reached the wrong cause.
    They stay off the container's stderr, but at DEBUG rather than nowhere."""
    with caplog.at_level(logging.DEBUG, logger=downloader.__name__):
        downloader._YtdlpLogger().warning("android_vr client https formats require a GVS PO Token")

    assert "GVS PO Token" in caplog.text


def test_a_refused_url_gets_a_second_and_third_extraction(fake_ydl, tmp_path):
    """The whole point of the ladder, and the reason repeating the lead
    client on the second rung is not a pointless repeat: the refusal is
    per-URL, so a fresh extraction of the same video by the same client
    produces a URL that can be served even though the last one wasn't."""
    fake_ydl.outcomes = [
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        None,
    ]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    path = downloader.download_audio("vid00000001")

    assert path.name == "vid00000001.m4a"
    assert len(fake_ydl.calls) == 3


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
    """Asking for the cap is only half of it — whether anything matches it
    depends on the client. Under tv_simply nothing did, on any video tried,
    and "low" silently downloaded the same ~130kbps stream as "high" for as
    long as that client led the ladder. visionos carries the ~49kbps itag
    139, which is what makes the setting mean something (3.78 MB against
    1.43 MB, measured end to end)."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001", quality="low")

    assert "abr<=64" in fake_ydl.calls[0]["format"]


def test_the_file_is_looked_for_where_it_was_written(fake_ydl, tmp_path):
    """Audio goes into a directory per user, and the "did it land?" check has
    to follow it there.

    It didn't, at first: out_template moved and the final path stayed derived
    from storage_dir, so every download raised "Download completed but output
    file was not found" with the file sitting one level down. It reached the
    running app — three tracks in a row failed that way before it was caught.
    """
    fake_ydl.outcomes = [None]
    written = tmp_path / "7" / "vid00000001.m4a"
    written.parent.mkdir()
    written.write_bytes(b"audio")

    assert downloader.download_audio("vid00000001", user_id=7) == written

    # And the template yt-dlp was handed names the same place.
    assert str(tmp_path / "7") in fake_ydl.calls[0]["outtmpl"]


def test_a_download_with_no_user_keeps_the_flat_layout(fake_ydl, tmp_path):
    """Older rows name their file at the top level and still have to resolve."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    assert downloader.download_audio("vid00000001") == tmp_path / "vid00000001.m4a"
