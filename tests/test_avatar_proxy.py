"""GET /avatar-proxy (app/main.py) — streams a channel avatar from an
allowlisted YouTube CDN host without ever writing it to disk. See
youtube/search.py's _cached_avatar_or_hotlink for why a never-followed
channel's avatar goes through this instead of a permanent local copy or a
direct browser hotlink.
"""

from app import main


def test_a_proxied_avatar_is_streamed_through(client, monkeypatch):
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"\xff\xd8\xff-jpeg-bytes", "image/jpeg"

    monkeypatch.setattr(main, "fetch_image_bytes", fake_fetch)

    res = client.get("/avatar-proxy", params={"u": "https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"})

    assert res.status_code == 200
    assert res.content == b"\xff\xd8\xff-jpeg-bytes"
    assert res.headers["content-type"] == "image/jpeg"
    assert fetched == ["https://yt3.ggpht.com/abc=s900-c-k-c0x00ffffff-no-rj"]


def test_an_lh3_portrait_is_proxied_too(client, monkeypatch):
    """YouTube Music serves artist portraits from lh3.googleusercontent.com
    nearly as often as from yt3 — four of twelve charting artists, measured.
    Leaving it off the allowlist rejected those before they were ever
    fetched, which rendered as an empty circle on the card."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"\xff\xd8\xff", "image/jpeg"))

    res = client.get(
        "/avatar-proxy", params={"u": "https://lh3.googleusercontent.com/abc=w544-h544-p-l90-rj"}
    )

    assert res.status_code == 200


def test_a_non_allowlisted_host_is_rejected_without_being_fetched(client, monkeypatch):
    """The host check is what stops a tampered `u` from turning this into an
    open fetch of arbitrary hosts on the server's own behalf."""

    def fail_if_called(url):
        raise AssertionError("must never fetch a non-allowlisted host")

    monkeypatch.setattr(main, "fetch_image_bytes", fail_if_called)

    res = client.get("/avatar-proxy", params={"u": "https://evil.example/tracker.png"})

    assert res.status_code == 400


def test_a_lookalike_host_is_rejected(client, monkeypatch):
    """Exact hostname match only — a substring/endswith check would let
    something like evil.example/yt3.ggpht.com or notyt3.ggpht.com through."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: (b"x", "image/jpeg"))

    res = client.get("/avatar-proxy", params={"u": "https://notyt3.ggpht.com/abc"})

    assert res.status_code == 400


def test_a_failed_upstream_fetch_serves_a_blank_pixel(client, monkeypatch):
    """Not a 404, which is what this used to answer.

    An <img> whose src fails paints the browser's own broken-image glyph.
    Every avatar in the app renders through .search-result-thumb, which
    already draws the grey circle used for a channel with no avatar at all,
    so a transparent pixel lands in exactly that placeholder — no markup and
    no onerror handler anywhere. The onboarding wizard needs it: its avatar
    URLs ship committed in scripts/channel_profiles.py and go stale whenever
    a channel changes its picture.
    """
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: None)

    res = client.get("/avatar-proxy", params={"u": "https://yt3.ggpht.com/abc"})

    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.content.startswith(b"\x89PNG")


def test_the_blank_pixel_is_not_cached(client, monkeypatch):
    """A real avatar is cached for a day; this stand-in must not be, or one
    transient upstream hiccup freezes a blank circle in the browser until
    tomorrow. Nothing is stored server-side either, so the next render
    retries."""
    monkeypatch.setattr(main, "fetch_image_bytes", lambda url: None)

    res = client.get("/avatar-proxy", params={"u": "https://yt3.ggpht.com/abc"})

    assert res.headers["cache-control"] == "no-store"


def test_avatar_proxy_requires_login():
    from fastapi.testclient import TestClient

    with TestClient(main.app) as anonymous:
        res = anonymous.get("/avatar-proxy", params={"u": "https://yt3.ggpht.com/abc"}, follow_redirects=False)

    assert res.status_code == 303
