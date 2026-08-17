"""Compression and security headers (app/middleware.py).

The compression half is as much about what is *not* compressed as what is:
gzipping the audio stream would corrupt the byte ranges the <audio> element
seeks with, and Starlette's own GZipMiddleware excludes only
`text/event-stream`, so the filtering is ours to get right.
"""

import re

from app.config import settings
from app.database import engine


def test_html_is_compressed(client):
    res = client.get("/", headers={"Accept-Encoding": "gzip"})

    assert res.status_code == 200
    # httpx decodes transparently, so the header is what proves it happened.
    assert res.headers.get("content-encoding") == "gzip"


def test_small_responses_are_left_uncompressed(client):
    """minimum_size=1000: below it, the gzip header and trailer cost more than
    the compression saves. An empty library's Home fragment is ~150 bytes."""
    res = client.get("/partials/home", headers={"Accept-Encoding": "gzip"})

    assert res.status_code == 200
    assert int(res.headers["content-length"]) < 1000
    assert "content-encoding" not in res.headers


def test_a_range_request_is_never_compressed(client, db_session, tmp_path):
    """The case that would break playback rather than merely waste CPU.

    A gzipped 206 carries a different number of bytes than the range it claims
    to be, which the <audio> element cannot reconcile — and it issues these on
    every seek.
    """
    from app.models import Content, Feed

    feed = Feed(user_id=1, rss_url="https://example.com/range", channel_title="Range Channel")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    audio = tmp_path / "rangevid001.m4a"
    audio.write_bytes(b"\x00" * 4096)
    content = Content(
        feed_id=feed.id,
        user_id=1,
        video_id="rangevid001",
        title="Ranged",
        status="ready",
        file_path=str(audio),
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)

    res = client.get(
        f"/content/{content.id}/stream",
        headers={"Accept-Encoding": "gzip", "Range": "bytes=0-99"},
    )

    assert res.status_code == 206
    assert "content-encoding" not in res.headers
    assert len(res.content) == 100


def test_cached_images_are_not_compressed(client):
    """Already-compressed bytes; gzipping them is pure CPU."""
    settings.thumbnails_dir.mkdir(parents=True, exist_ok=True)
    # Larger than the middleware's minimum_size, so a pass-through is the only
    # reason it could come back uncompressed.
    (settings.thumbnails_dir / "imgtest0001.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 4096)

    res = client.get("/thumbnails/imgtest0001.jpg", headers={"Accept-Encoding": "gzip"})

    assert res.status_code == 200
    assert "content-encoding" not in res.headers


def test_security_headers_are_present(client):
    res = client.get("/")

    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in res.headers["Permissions-Policy"]

    csp = res.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # The point of the nonce: script-src must not fall back to allowing any
    # inline script, or the CSP buys nothing against an injected handler.
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


def test_the_inline_script_carries_the_nonce_from_the_header(client):
    """A nonce that doesn't match the header blocks the pre-paint script, which
    would leave every load rendering the wrong tab for a frame."""
    res = client.get("/")

    header_nonce = re.search(r"'nonce-([^']+)'", res.headers["Content-Security-Policy"]).group(1)
    assert f'<script nonce="{header_nonce}">' in res.text


def test_each_response_gets_a_fresh_nonce(client):
    first = client.get("/").headers["Content-Security-Policy"]
    second = client.get("/").headers["Content-Security-Policy"]

    assert first != second, "a reused nonce defeats the point of having one"


def test_login_page_is_protected_too(client):
    """Headers come from middleware, not a per-route decorator, so the pages
    rendered outside the authenticated routers get them as well."""
    res = client.get("/login", follow_redirects=False)

    assert "Content-Security-Policy" in res.headers
    assert res.headers["X-Frame-Options"] == "DENY"


def test_sqlite_runs_in_wal_mode_with_foreign_keys_on():
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
