import pytest

from app.youtube import extract
from app.youtube.extract import (
    METADATA_RETRIES,
    METADATA_SOCKET_TIMEOUT_SECONDS,
    ChannelResolutionError,
    resolve_feed_url,
)
from app.youtube.urls import (
    _playlist_id,
    absolute_thumbnail_url,
    channel_feed_url,
    extract_channel_id,
    is_youtube_url,
    longform_feed_url,
)


@pytest.mark.parametrize(
    "rss_url, expected",
    [
        (
            "https://www.youtube.com/feeds/videos.xml?channel_id=UCX6OQ3DkcsbYNE6H8uQQuVA",
            "UCX6OQ3DkcsbYNE6H8uQQuVA",
        ),
        (
            "https://www.youtube.com/feeds/videos.xml?playlist_id=UULFX6OQ3DkcsbYNE6H8uQQuVA",
            None,
        ),
        ("not a url at all", None),
        ("", None),
    ],
)
def test_extract_channel_id(rss_url, expected):
    assert extract_channel_id(rss_url) == expected


def test_channel_feed_url_builds_channel_id_shaped_url():
    assert channel_feed_url("UCX6OQ3DkcsbYNE6H8uQQuVA") == (
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCX6OQ3DkcsbYNE6H8uQQuVA"
    )


def test_resolve_feed_url_direct_channel_url_matches_channel_feed_url():
    # Placeholder feeds (routers/explore.py's _get_or_create_placeholder_feed)
    # must build their rss_url identically to a real follow's, or the
    # upgrade-in-place dedup lookup in create_feed_from_rss_url silently
    # misses — this pins resolve_feed_url's direct-match branch to the same
    # helper channel_feed_url exposes.
    channel_id = "UCX6OQ3DkcsbYNE6H8uQQuVA"
    url = f"https://www.youtube.com/channel/{channel_id}"
    assert resolve_feed_url(url) == channel_feed_url(channel_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@handle",
        "http://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA",
        # Scheme-less, because that's what a browser address bar shows and so
        # what people paste.
        "youtube.com/@handle",
        "m.youtube.com/@handle",
        "https://music.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA",
        "https://youtu.be/dQw4w9WgXcQ",
    ],
)
def test_is_youtube_url_accepts_the_shapes_people_paste(url):
    assert is_youtube_url(url)


@pytest.mark.parametrize(
    "url",
    [
        # The reason the check exists: POST /feeds fetches whatever it's given
        # and hands the failure text back, which turns "follow a channel" into
        # a LAN port scanner for an authenticated user.
        "http://127.0.0.1:8000/feeds/videos.xml",
        "http://localhost/feeds/videos.xml",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        # Both shapes a substring test for "youtube.com" would wave through —
        # the second is why this uses `hostname` and not `netloc`.
        "https://youtube.com.evil.example/feeds/videos.xml",
        "https://youtube.com@evil.example/feeds/videos.xml",
        "https://notyoutube.com/@handle",
        "",
    ],
)
def test_is_youtube_url_rejects_everything_else(url):
    assert not is_youtube_url(url)


def test_resolve_feed_url_rejects_a_non_youtube_host_before_any_network_call(monkeypatch):
    """yt-dlp not only fetches the URL, its error text reaches the client — so
    the rejection has to happen before the extraction, not inside it."""

    def fail(*args, **kwargs):
        raise AssertionError("yt-dlp must not be invoked for a non-YouTube URL")

    monkeypatch.setattr(extract.yt_dlp, "YoutubeDL", fail)

    with pytest.raises(ChannelResolutionError):
        resolve_feed_url("http://127.0.0.1:8000/feeds/videos.xml?channel_id=UC")


def test_resolve_feed_url_canonicalises_a_pasted_channel_feed_url():
    """A directly pasted feed URL used to be returned exactly as typed, and
    create_feed_from_rss_url dedups on that exact string — so following
    m.youtube.com's spelling of a channel already followed through
    www.youtube.com produced a second Feed row for the same channel."""
    channel_id = "UCX6OQ3DkcsbYNE6H8uQQuVA"
    for pasted in (
        f"http://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        f"https://m.youtube.com/feeds/videos.xml?channel_id={channel_id}",
    ):
        assert resolve_feed_url(pasted) == channel_feed_url(channel_id)


def test_resolve_feed_url_passes_a_playlist_feed_through_untouched():
    """The Videos-tab feed longform_feed_url builds has a playlist_id and no
    channel_id, so there is nothing to canonicalise — and rewriting it would
    silently swap a Shorts-free feed for one that isn't."""
    url = longform_feed_url("UCX6OQ3DkcsbYNE6H8uQQuVA")
    assert resolve_feed_url(url) == url


@pytest.mark.parametrize(
    "name, opts",
    [
        ("channel resolve", extract._CHANNEL_RESOLVE_OPTS),
        ("video channel resolve", extract._VIDEO_CHANNEL_RESOLVE_OPTS),
        ("duration fetch", extract._DURATION_FETCH_OPTS),
        ("backfill fetch", extract._BACKFILL_FETCH_OPTS),
    ],
)
def test_every_metadata_extraction_is_time_bounded(name, opts):
    """downloader.py has always set socket_timeout; none of these did, and they
    are the ones that run inside a request (POST /feeds, Explore search) or in
    feed_sync's 8-thread pool. The retry caps belong here too — yt-dlp's
    defaults (retries=10, extractor_retries=3) multiply the timeout back into
    minutes."""
    assert opts["socket_timeout"] == METADATA_SOCKET_TIMEOUT_SECONDS, name
    assert opts["retries"] == METADATA_RETRIES, name
    assert opts["extractor_retries"] == METADATA_RETRIES, name


def test_playlist_id_swaps_uc_prefix():
    assert _playlist_id("UCX6OQ3DkcsbYNE6H8uQQuVA", "UULF") == "UULFX6OQ3DkcsbYNE6H8uQQuVA"
    assert _playlist_id("UCX6OQ3DkcsbYNE6H8uQQuVA", "UU") == "UUX6OQ3DkcsbYNE6H8uQQuVA"


def test_playlist_id_passes_through_non_uc_ids():
    # Defensive fallback for an unexpected id shape — shouldn't happen in
    # practice (channel ids always start with UC), but shouldn't crash either.
    assert _playlist_id("not-a-channel-id", "UULF") == "not-a-channel-id"


def test_absolute_thumbnail_url_none_passthrough():
    assert absolute_thumbnail_url(None) is None
    assert absolute_thumbnail_url("") is None


def test_absolute_thumbnail_url_adds_scheme_to_protocol_relative_urls():
    assert absolute_thumbnail_url("//i.ytimg.com/vi/abc/hqdefault.jpg") == (
        "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    )


def test_absolute_thumbnail_url_leaves_already_absolute_urls_alone():
    url = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    assert absolute_thumbnail_url(url) == url


def test_absolute_thumbnail_url_rewrites_googleusercontent_to_ggpht():
    # The actual bug this session: yt3.googleusercontent.com avatar URLs get
    # intermittently blocked by Chrome's Opaque Response Blocking when
    # hotlinked; yt3.ggpht.com serves the same image and doesn't.
    raw = "https://yt3.googleusercontent.com/abc123=s900-c-k-c0x00ffffff-no-rj"
    assert absolute_thumbnail_url(raw) == "https://yt3.ggpht.com/abc123=s900-c-k-c0x00ffffff-no-rj"


def test_absolute_thumbnail_url_only_rewrites_the_expected_host():
    # Some other googleusercontent-hosted asset (not the yt3 avatar CDN)
    # should pass through untouched.
    raw = "https://lh3.googleusercontent.com/abc123"
    assert absolute_thumbnail_url(raw) == raw
