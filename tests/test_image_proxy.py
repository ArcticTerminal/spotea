"""GET /image-proxy (app/main.py) — streams an avatar or a song/album/
playlist cover from an allowlisted YouTube CDN host without ever writing it
to disk. See images.cached_avatar_or_hotlink and youtube/music.py's
_proxied_cover_url for why images that haven't earned a permanent local
copy go through this instead of a direct browser hotlink.
"""

from app import main


def test_a_proxied_image_is_streamed_through(client, monkeypatch):
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff-jpeg-bytes", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"})

    assert res.status_code == 200
    assert res.content == b"\xff\xd8\xff-jpeg-bytes"
    assert res.headers["content-type"] == "image/jpeg"
    assert fetched == ["https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"]


def test_an_lh3_portrait_is_proxied_too(client, monkeypatch):
    """YouTube Music serves artist portraits — and song/album covers —
    from lh3.googleusercontent.com nearly as often as from yt3, measured.
    Leaving it off the allowlist rejected those before they were ever
    fetched, which rendered as an empty circle or a broken card image."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"\xff\xd8\xff", "image/jpeg"))

    res = client.get(
        "/image-proxy", params={"u": "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"}
    )

    assert res.status_code == 200


def test_a_mood_playlist_track_thumbnail_is_proxied_too(client, monkeypatch):
    """A mood/mix playlist's tracks report their thumbnails on
    i.ytimg.com — YouTube's ordinary video-thumbnail host, not YouTube
    Music's cover CDN — measured live on every one of a "Fall Hits"
    playlist's 200 tracks. Missing this host rejected every one of them
    before ever fetching, which is what every row on that page looked
    like until this was added."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"\xff\xd8\xff", "image/jpeg"))

    res = client.get(
        "/image-proxy",
        params={"u": "https://i.ytimg.com/vi/1lrFsXkT_rM/hqdefault.jpg?sqp=-oaymwEWCJADEOEBIAQ"},
    )

    assert res.status_code == 200


def test_a_non_allowlisted_host_is_rejected_without_being_fetched(client, monkeypatch):
    """The host check is what stops a tampered `u` from turning this into an
    open fetch of arbitrary hosts on the server's own behalf."""

    def fail_if_called(url):
        raise AssertionError("must never fetch a non-allowlisted host")

    monkeypatch.setattr(main, "fetch_image_bytes", fail_if_called)

    res = client.get("/image-proxy", params={"u": "https://evil.example/tracker.png"})

    assert res.status_code == 400


def test_a_lookalike_host_is_rejected(client, monkeypatch):
    """Exact hostname match only — a substring/endswith check would let
    something like evil.example/yt3.ggpht.com or notyt3.ggpht.com through."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"x", "image/jpeg"))

    res = client.get("/image-proxy", params={"u": "https://notyt3.ggpht.com/abc"})

    assert res.status_code == 400


def test_a_failed_upstream_fetch_serves_a_blank_pixel(client, monkeypatch):
    """Not a 404, which is what this used to answer.

    An <img> whose src fails paints the browser's own broken-image glyph.
    Every avatar in the app renders through .search-result-thumb, which
    already draws the grey circle used for a channel with no avatar at all,
    so a transparent pixel lands in exactly that placeholder — no markup and
    no onerror handler anywhere.
    """
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: None)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc"})

    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG")


def test_the_blank_pixel_is_not_cached(client, monkeypatch):
    """A real image is cached for a day; this stand-in must not be, or one
    transient upstream hiccup freezes a blank circle in the browser until
    tomorrow. Nothing is stored server-side either, so the next render
    retries."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: None)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc"})

    assert res.headers["cache-control"] == "no-store"


def test_image_proxy_requires_login():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as anonymous:
        res = anonymous.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc"}, follow_redirects=False)

    assert res.status_code == 303


def test_a_video_still_is_fetched_exactly_as_asked_for(client, monkeypatch):
    """The proxy used to try `maxresdefault` first and fall back, to sharpen
    the 400x225 cover a music-video playlist entry carries.

    Gone with the reason for it: a video row is swapped for its song before it
    plays (see routers/content.py's swap_in_song_version), and the song's
    cover is square album art — so the player, the one surface that drew a
    still large enough for 400px to look soft, no longer draws one. What was
    left was a list of 200px cards paying for 1280x720 frames: measured over
    24 real covers, 496 KB became 2151 KB.
    """
    original = "https://i.ytimg.com/vi/1lrFsXkT_rM/hqdefault.jpg?sqp=-oaymwEWCJADEOEBIAQ"
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/image-proxy", params={"u": original})

    assert res.status_code == 200
    assert fetched == [original], "one fetch, and the URL the caller named"


def test_a_square_cover_is_fetched_once_and_unchanged(client, monkeypatch):
    """Only video stills have a larger variant to try. YouTube Music's own
    square art is already asked for at COVER_SIZE where the URL is built, so
    a second speculative fetch here would be pure waste."""
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/image-proxy", params={"u": "https://yt3.ggpht.com/abc=w544-h544-l90-rj"})

    assert res.status_code == 200
    assert fetched == ["https://yt3.ggpht.com/abc=w544-h544-l90-rj"]
