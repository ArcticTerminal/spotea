"""Avatar handling for a search result (app/images.py) and URL sizing
(app/youtube/urls.py).

Measured live before this: 977 of 1060 avatar files on disk (92%, 16.4 MB)
were orphans — search downloaded a fresh copy for every result, and nothing
anywhere ever deleted one. Per the locked decision, only an artist someone
actually follows gets a local copy now (see artist_sync.fetch_artist_data);
search reuses what's already there or routes the remote URL through
/image-proxy (see app/main.py) instead of handing it to the browser to
hotlink directly.
"""

from app import images
from app.youtube import urls

CHANNEL_ID = "UCX6OQ3DkcsbYNE6H8uQQuVA"
REMOTE = "https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"


def test_a_never_cached_artist_is_proxied_not_downloaded(monkeypatch):
    """The core behaviour: no download call happens at all — a result for an
    artist nobody follows gets a same-origin /image-proxy URL wrapping the
    remote one, not a permanent local copy."""
    monkeypatch.setattr(images, "cached_avatar_path", lambda channel_id: None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("a search result must never download an avatar")

    monkeypatch.setattr(images, "download_avatar", fail_if_called)

    assert images.cached_avatar_or_hotlink(CHANNEL_ID, REMOTE) == (
        "/image-proxy?u=https%3A%2F%2Fyt3.ggpht.com%2Fabc%3Ds900-c-k-c0x00ffffff-no-rj"
    )


def test_an_already_cached_artist_reuses_its_local_avatar(monkeypatch):
    """An artist already followed (or found in an earlier search) costs
    nothing extra — the cached path wins over the remote URL."""
    monkeypatch.setattr(
        images,
        "cached_avatar_path",
        lambda channel_id: f"/avatars/{channel_id}.jpg" if channel_id == CHANNEL_ID else None,
    )

    assert images.cached_avatar_or_hotlink(CHANNEL_ID, REMOTE) == f"/avatars/{CHANNEL_ID}.jpg"


def test_a_result_with_no_thumbnail_at_all_gets_none(monkeypatch):
    monkeypatch.setattr(images, "cached_avatar_path", lambda channel_id: None)

    assert images.cached_avatar_or_hotlink(CHANNEL_ID, None) is None


def test_avatar_url_at_size_replaces_the_size_segment():
    """Google's image CDN resizes server-side from the trailing "=s<n>".
    Avatars are reported as "=s0" — the original upload, measured live at
    390 KB for something drawn in a 36px circle."""
    assert (
        urls.avatar_url_at_size("https://yt3.ggpht.com/abc=s0", 176)
        == "https://yt3.ggpht.com/abc=s176"
    )


def test_avatar_url_at_size_replaces_a_decorated_size_segment():
    """Some avatar URLs carry crop/format flags after the size."""
    assert (
        urls.avatar_url_at_size("https://yt3.ggpht.com/ytc/abc=s900-c-k-c0x00ffffff-no-rj", 176)
        == "https://yt3.ggpht.com/ytc/abc=s176"
    )


def test_avatar_url_at_size_passes_through_a_url_with_no_size_segment():
    assert urls.avatar_url_at_size("https://yt3.ggpht.com/abc", 176) == "https://yt3.ggpht.com/abc"


def test_avatar_url_at_size_of_nothing_is_nothing():
    assert urls.avatar_url_at_size(None, 176) is None
