"""Fetching a channel's RSS feed — the network half of app/youtube/rss.py.

Every test here exists because `feedparser.parse(url)` reports a *failed fetch*
as a successful parse of an empty feed: it sets `bozo` and returns. There is no
exception, so the retry loop's `except` clause was unreachable, and a DNS
failure, a refused connection, a timeout and YouTube's 429 all reached the user
as "URL is not a valid YouTube channel RSS feed" — the one explanation that is
certainly wrong for all four. It also has no timeout parameter, which is the
other reason the fetch moved into our own code.
"""

import http.client
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from app.youtube import rss
from app.youtube.rss import (
    FETCH_ATTEMPTS,
    FETCH_TIMEOUT_SECONDS,
    MAX_FEED_BYTES,
    RETRY_DELAY_SECONDS,
    FeedUnavailableError,
    InvalidFeedError,
    fetch_feed,
)

CHANNEL_ID = "UCX6OQ3DkcsbYNE6H8uQQuVA"
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

VALID_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
  <yt:channelId>{CHANNEL_ID}</yt:channelId>
  <title>Test Channel</title>
  <entry>
    <yt:videoId>abcdefghijk</yt:videoId>
    <title>A real upload</title>
    <published>2026-08-01T10:00:00+00:00</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"/>
    </media:group>
  </entry>
</feed>
""".encode()


class _FakeResponse:
    def __init__(self, body: bytes, read_sizes: list[int]):
        self._body = body
        self._read_sizes = read_sizes

    def read(self, size: int = -1) -> bytes:
        self._read_sizes.append(size)
        return self._body[:size] if size >= 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def network(monkeypatch):
    """Stands in for the one call rss.py makes to the network.

    `install(*outcomes)` queues one outcome per attempt — bytes to return or an
    exception to raise — and hands back a list of the requests actually made, so
    a test can assert on the URL, the headers and the timeout as well as the
    result. Sleeps are recorded rather than slept.
    """
    requests: list[urllib.request.Request] = []
    timeouts: list[float | None] = []
    sleeps: list[float] = []
    read_sizes: list[int] = []
    monkeypatch.setattr(rss.time, "sleep", sleeps.append)

    recorder = SimpleNamespace(
        requests=requests, timeouts=timeouts, sleeps=sleeps, read_sizes=read_sizes
    )

    def install(*outcomes):
        def fake_urlopen(request, timeout=None):
            requests.append(request)
            timeouts.append(timeout)
            outcome = outcomes[min(len(requests), len(outcomes)) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return _FakeResponse(outcome, read_sizes)

        monkeypatch.setattr(rss.urllib.request, "urlopen", fake_urlopen)
        return recorder

    return install


def test_a_valid_feed_is_parsed_from_the_fetched_bytes(network):
    """The happy path has to keep working through bytes rather than a URL —
    feedparser's YouTube-namespace fields (yt:videoId, media:thumbnail) and its
    date parsing are what the whole refresh depends on."""
    recorder = network(VALID_FEED)

    parsed = fetch_feed(FEED_URL)

    assert [request.full_url for request in recorder.requests] == [FEED_URL]
    assert parsed.channel_title == "Test Channel"
    assert len(parsed.entries) == 1
    entry = parsed.entries[0]
    assert entry.video_id == "abcdefghijk"
    assert entry.title == "A real upload"
    assert entry.thumbnail_url == "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg"
    # Naive UTC, as every datetime reaching a model column has to be.
    assert entry.published_at.isoformat() == "2026-08-01T10:00:00"
    assert entry.published_at.tzinfo is None


def test_the_fetch_carries_a_timeout(network):
    """The finding this file is mostly about: feedparser has no timeout at all,
    so a host that accepts the connection and then goes quiet held the thread
    for the OS TCP timeout — on a request thread for POST /feeds, or one of
    only REFRESH_POOL_SIZE threads during a refresh."""
    recorder = network(VALID_FEED)

    fetch_feed(FEED_URL)

    assert recorder.timeouts == [FETCH_TIMEOUT_SECONDS]


def test_a_network_failure_is_unavailable_not_invalid(network):
    """The actual user-visible bug: this used to say the URL was wrong."""
    network(urllib.error.URLError("Name or service not known"))

    with pytest.raises(FeedUnavailableError) as caught:
        fetch_feed(FEED_URL)

    assert "Name or service not known" in str(caught.value)
    assert "not a valid" not in str(caught.value)


def test_an_http_error_status_is_unavailable(network):
    """YouTube answers a too-eager refresh with 429, which is the one failure
    here most likely to hit a whole library at once — and the one for which
    "your URL is wrong" is most misleading."""
    network(urllib.error.HTTPError(FEED_URL, 429, "Too Many Requests", {}, None))

    with pytest.raises(FeedUnavailableError) as caught:
        fetch_feed(FEED_URL)

    assert "429" in str(caught.value)


def test_a_protocol_level_failure_is_unavailable(network):
    """http.client.IncompleteRead is not an OSError, so catching URLError (or
    OSError) alone would let it escape as a 500."""
    network(http.client.IncompleteRead(b"", 512))

    with pytest.raises(FeedUnavailableError):
        fetch_feed(FEED_URL)


def test_a_fetch_failure_is_retried_once_then_gives_up(network):
    recorder = network(urllib.error.URLError("down"), urllib.error.URLError("down"))

    with pytest.raises(FeedUnavailableError):
        fetch_feed(FEED_URL)

    assert len(recorder.requests) == FETCH_ATTEMPTS == 2
    assert recorder.sleeps == [RETRY_DELAY_SECONDS]


def test_a_transient_failure_recovers_on_the_second_attempt(network):
    """Why the retry exists at all — a momentary blip shouldn't cost the user a
    channel."""
    recorder = network(urllib.error.URLError("blip"), VALID_FEED)

    parsed = fetch_feed(FEED_URL)

    assert len(recorder.requests) == 2
    assert parsed.channel_title == "Test Channel"


def test_a_response_that_is_not_a_feed_is_not_retried(network):
    """Only fetch failures are worth a second attempt: a response that arrived
    and isn't a channel feed will say exactly the same thing next time, and
    retrying it doubles the requests this app makes to YouTube for nothing."""
    recorder = network(b"<?xml version='1.0'?><feed><unclosed>")

    with pytest.raises(InvalidFeedError):
        fetch_feed(FEED_URL)

    assert len(recorder.requests) == 1
    assert recorder.sleeps == []


def test_an_unparseable_response_keeps_the_reason(network):
    """feedparser puts a parse failure in `bozo_exception` rather than raising
    it, and the old code discarded that — leaving an unexplained "not a valid
    feed" as the only trace of a truncated or non-XML response."""
    network(b"<?xml version='1.0'?><feed><unclosed>")

    with pytest.raises(InvalidFeedError) as caught:
        fetch_feed(FEED_URL)

    assert "no element found" in str(caught.value)


def test_a_page_that_parses_but_is_not_a_channel_feed_is_invalid(network):
    """The case the old message was written for, and the only one it fitted."""
    network(b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><title>x</title></feed>")

    with pytest.raises(InvalidFeedError) as caught:
        fetch_feed(FEED_URL)

    assert str(caught.value) == "URL is not a valid YouTube channel RSS feed"


def test_an_oversized_response_is_rejected_without_a_retry(network):
    """read() is unbounded, and this is our socket now rather than feedparser's.
    The cap is checked in two places for one reason: the read itself is bounded,
    so an enormous response is never pulled into memory, and then what arrived
    over the limit is refused. Far above any real channel feed (~50 KB), so
    tripping it means the response isn't one."""
    recorder = network(b"x" * (MAX_FEED_BYTES + 1))

    with pytest.raises(FeedUnavailableError) as caught:
        fetch_feed(FEED_URL)

    assert str(MAX_FEED_BYTES) in str(caught.value)
    assert recorder.read_sizes == [MAX_FEED_BYTES + 1]
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/feeds/videos.xml?channel_id=UC",
        "http://192.168.1.1/",
        "http://[::1]/feeds/videos.xml",
        "file:///etc/passwd",
        # The two shapes a substring test for "youtube.com" would wave through.
        "https://youtube.com.evil.example/feeds/videos.xml",
        "https://youtube.com@evil.example/feeds/videos.xml",
    ],
)
def test_a_non_youtube_url_is_rejected_without_opening_a_socket(network, url):
    """fetch_feed is where the socket is actually opened, so the host check
    belongs here as well as in resolve_feed_url. Nothing persists a feed row
    from a probe like this, but the error text alone told an authenticated user
    what was listening on the LAN."""
    recorder = network(VALID_FEED)

    with pytest.raises(InvalidFeedError):
        fetch_feed(url)

    assert recorder.requests == []
