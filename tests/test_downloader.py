"""The retry ladder in app/downloader.py.

YouTube itself is never contacted here — yt_dlp.YoutubeDL is replaced. What's
being pinned down is the ladder's shape: which clients each attempt asks for,
that a later attempt can rescue an earlier failure, and that the whole thing
gives up quickly. The old ladder spent ~70s sleeping before reporting a
failure, which is the user-visible behaviour these guard against.
"""

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
    """Swaps in _FakeYDL, skips the sleeps, and points storage at tmp_path."""
    _FakeYDL.calls = []
    _FakeYDL.outcomes = []
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", _FakeYDL)
    monkeypatch.setattr(downloader.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(downloader.settings, "storage_dir", tmp_path)
    slept = []
    _FakeYDL.slept = slept
    return _FakeYDL


def _clients_of(opts):
    return opts["extractor_args"]["youtube"]["player_client"]


def test_first_attempt_uses_yt_dlps_own_default_client_pair(fake_ydl, tmp_path):
    """android_vr alone was pinned here for a while, which threw away
    web_safari for no saving — web_safari is also yt-dlp's webpage client, so
    its player response comes from a page that was fetched anyway."""
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 1
    assert _clients_of(fake_ydl.calls[0]) == ["android_vr", "web_safari"]


def test_the_happy_path_does_not_sleep_at_all(fake_ydl, tmp_path):
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert fake_ydl.slept == []


def test_a_second_attempt_rescues_a_transient_refusal(fake_ydl, tmp_path):
    """Measured behaviour: a 403 on the first try, then the same request
    succeeding a couple of seconds later."""
    fake_ydl.outcomes = ["ERROR: unable to download video data: HTTP Error 403: Forbidden", None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    path = downloader.download_audio("vid00000001")

    assert path.name == "vid00000001.m4a"
    assert len(fake_ydl.calls) == 2
    assert fake_ydl.slept == [2]


def test_the_last_attempt_switches_client_family(fake_ydl, tmp_path):
    """mweb is a different client family and needs the JS challenge solver —
    worth one try, but not on the fast path."""
    fake_ydl.outcomes = ["403", "403", None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001")

    assert _clients_of(fake_ydl.calls[2]) == ["mweb"]
    assert fake_ydl.calls[2].get("remote_components") == ["ejs:github"]
    # The fast-path attempts must not pay for the solver fetch.
    assert "remote_components" not in fake_ydl.calls[0]
    assert "remote_components" not in fake_ydl.calls[1]


def test_giving_up_takes_seconds_not_a_minute(fake_ydl):
    fake_ydl.outcomes = ["403", "403", "403"]

    with pytest.raises(downloader.DownloadError):
        downloader.download_audio("vid00000001")

    assert len(fake_ydl.calls) == 3
    # The ladder this replaced slept 5 + 15 + 30.
    assert sum(fake_ydl.slept) <= 10, fake_ydl.slept


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


def test_quality_selects_the_capped_format(fake_ydl, tmp_path):
    fake_ydl.outcomes = [None]
    (tmp_path / "vid00000001.m4a").write_bytes(b"audio")

    downloader.download_audio("vid00000001", quality="low")

    assert "abr<=64" in fake_ydl.calls[0]["format"]
