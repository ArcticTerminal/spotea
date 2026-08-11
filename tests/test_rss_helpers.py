import pytest

from app.rss import _absolute_thumbnail_url, _playlist_id, channel_feed_url, extract_channel_id, resolve_feed_url


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
    # Placeholder feeds (routers/feeds.py's _get_or_create_placeholder_feed)
    # must build their rss_url identically to a real follow's, or the
    # upgrade-in-place dedup lookup in _create_feed_from_rss_url silently
    # misses — this pins resolve_feed_url's direct-match branch to the same
    # helper channel_feed_url exposes.
    channel_id = "UCX6OQ3DkcsbYNE6H8uQQuVA"
    url = f"https://www.youtube.com/channel/{channel_id}"
    assert resolve_feed_url(url) == channel_feed_url(channel_id)


def test_playlist_id_swaps_uc_prefix():
    assert _playlist_id("UCX6OQ3DkcsbYNE6H8uQQuVA", "UULF") == "UULFX6OQ3DkcsbYNE6H8uQQuVA"
    assert _playlist_id("UCX6OQ3DkcsbYNE6H8uQQuVA", "UU") == "UUX6OQ3DkcsbYNE6H8uQQuVA"


def test_playlist_id_passes_through_non_uc_ids():
    # Defensive fallback for an unexpected id shape — shouldn't happen in
    # practice (channel ids always start with UC), but shouldn't crash either.
    assert _playlist_id("not-a-channel-id", "UULF") == "not-a-channel-id"


def test_absolute_thumbnail_url_none_passthrough():
    assert _absolute_thumbnail_url(None) is None
    assert _absolute_thumbnail_url("") is None


def test_absolute_thumbnail_url_adds_scheme_to_protocol_relative_urls():
    assert _absolute_thumbnail_url("//i.ytimg.com/vi/abc/hqdefault.jpg") == (
        "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    )


def test_absolute_thumbnail_url_leaves_already_absolute_urls_alone():
    url = "https://i.ytimg.com/vi/abc/hqdefault.jpg"
    assert _absolute_thumbnail_url(url) == url


def test_absolute_thumbnail_url_rewrites_googleusercontent_to_ggpht():
    # The actual bug this session: yt3.googleusercontent.com avatar URLs get
    # intermittently blocked by Chrome's Opaque Response Blocking when
    # hotlinked; yt3.ggpht.com serves the same image and doesn't.
    raw = "https://yt3.googleusercontent.com/abc123=s900-c-k-c0x00ffffff-no-rj"
    assert _absolute_thumbnail_url(raw) == "https://yt3.ggpht.com/abc123=s900-c-k-c0x00ffffff-no-rj"


def test_absolute_thumbnail_url_only_rewrites_the_expected_host():
    # Some other googleusercontent-hosted asset (not the yt3 avatar CDN)
    # should pass through untouched.
    raw = "https://lh3.googleusercontent.com/abc123"
    assert _absolute_thumbnail_url(raw) == raw
