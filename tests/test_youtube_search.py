"""search_channels' avatar handling (app/youtube/search.py).

Measured live before this: 977 of 1060 avatar files on disk (92%, 16.4 MB)
were orphans — search_channels downloaded a fresh copy for every result,
and nothing anywhere ever deleted one. Per the locked decision, only a
channel someone actually follows gets a local copy now (see
feed_sync.fetch_feed_data); search reuses what's already there or hotlinks
the remote URL directly.
"""

from app.youtube import search as yt_search
from app.youtube.search import search_channels

CHANNEL_ID = "UCX6OQ3DkcsbYNE6H8uQQuVA"


def _entry(channel_id=CHANNEL_ID, **overrides):
    entry = {
        "channel_id": channel_id,
        "title": "Some Channel",
        "channel_follower_count": 1000,
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "thumbnails": [{"url": "https://yt3.googleusercontent.com/abc=s900-c-k-c0x00ffffff-no-rj"}],
    }
    entry.update(overrides)
    return entry


def test_a_never_cached_channel_is_hotlinked_not_downloaded(monkeypatch):
    """The core behaviour change: no download call happens at all — search
    results for a channel nobody follows get the remote (ORB-safe-rewritten)
    URL directly."""
    monkeypatch.setattr(yt_search, "_search_entries", lambda url: [_entry()])
    monkeypatch.setattr(yt_search, "cached_avatar_path", lambda channel_id: None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("search_channels must never download an avatar")

    monkeypatch.setattr(yt_search, "download_avatar", fail_if_called, raising=False)

    results = search_channels("query")

    assert len(results) == 1
    # Rewritten to ggpht.com (via absolute_thumbnail_url/_best_thumbnail_url)
    # rather than the googleusercontent.com host Chrome's ORB blocks when
    # hotlinked.
    assert results[0].thumbnail_url == "https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"


def test_an_already_cached_channel_reuses_its_local_avatar(monkeypatch):
    """A channel already followed (or found in an earlier search) costs
    nothing extra here — its cached path wins over the remote URL."""
    monkeypatch.setattr(yt_search, "_search_entries", lambda url: [_entry()])
    monkeypatch.setattr(
        yt_search,
        "cached_avatar_path",
        lambda channel_id: f"/avatars/{channel_id}.jpg" if channel_id == CHANNEL_ID else None,
    )

    results = search_channels("query")

    assert results[0].thumbnail_url == f"/avatars/{CHANNEL_ID}.jpg"


def test_a_result_with_no_thumbnail_at_all_gets_none(monkeypatch):
    monkeypatch.setattr(yt_search, "_search_entries", lambda url: [_entry(thumbnails=[])])
    monkeypatch.setattr(yt_search, "cached_avatar_path", lambda channel_id: None)

    results = search_channels("query")

    assert results[0].thumbnail_url is None


def test_search_channels_no_longer_imports_a_download_step(monkeypatch):
    """A structural guard on top of the behavioural ones above: the module
    shouldn't even import download_avatar or ThreadPoolExecutor any more —
    both existed solely to support the per-result download this removes."""
    import inspect

    source = inspect.getsource(yt_search)
    assert "download_avatar" not in source
    assert "ThreadPoolExecutor" not in source
