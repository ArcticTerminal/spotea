"""Explore's search box, and which of the two YouTube indexes each half of
it reads.

The split is the whole point, so it's what these pin: songs come from
YouTube Music, channels stay on youtube.com, and neither borrows the
other's source. Channel search in particular is what finds podcasts, and
YouTube Music is measurably worse at that — see routers/explore.py's
search_feeds.

Both sources are monkeypatched out; nothing here touches the network.
"""

import pytest

from app.routers import explore as explore_router
from app.youtube.search import ChannelSearchResult, VideoSearchResult

SONG = VideoSearchResult(
    video_id="_efHZg9D9iE",
    title="Biliyorsun",
    thumbnail_url="https://yt3.ggpht.com/abc=w544-h544-l90-rj",
    duration_seconds=317,
    channel_title="Sezen Aksu",
    channel_id="UCNaGLJRPE3ohleIDM7RFtlQ",
)

TALK = VideoSearchResult(
    video_id="dQw4w9WgXcQ",
    title="Keynote: how we built it",
    thumbnail_url="https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
    duration_seconds=2400,
    channel_title="Some Conference",
    channel_id="UCYLY-BIq0sSOdNXGm1FPR-w",
)

PODCAST_CHANNEL = ChannelSearchResult(
    channel_id="UCGq-a57w-aPwyi3pW7XLiHw",
    title="The Diary Of A CEO",
    thumbnail_url="/avatar-proxy?u=x",
    subscriber_count=19_100_000,
    channel_url="https://www.youtube.com/channel/UCGq-a57w-aPwyi3pW7XLiHw",
)


@pytest.fixture
def sources(monkeypatch):
    """Replaces both search sources and records which ones were consulted."""
    calls: list[str] = []

    def install(*, songs=(), videos=(), channels=()):
        def record(name, results):
            def search(query):
                calls.append(name)
                return list(results)

            return search

        monkeypatch.setattr(explore_router, "search_songs", record("songs", songs))
        monkeypatch.setattr(explore_router, "search_videos", record("videos", videos))
        monkeypatch.setattr(explore_router, "search_channels", record("channels", channels))
        return calls

    return install


def test_songs_come_from_youtube_music(client, sources):
    calls = sources(songs=[SONG], videos=[TALK])

    res = client.get("/feeds/search-videos", params={"q": "sezen aksu"})

    assert res.status_code == 200
    assert [row["video_id"] for row in res.json()] == ["_efHZg9D9iE"]
    assert calls == ["songs"]


def test_a_song_row_carries_what_playing_it_needs(client, sources):
    """The row is handed straight back to POST /feeds/videos, so the
    artist's channel and the duration have to survive the round trip."""
    sources(songs=[SONG])

    (row,) = client.get("/feeds/search-videos", params={"q": "sezen aksu"}).json()

    assert row["channel_id"] == "UCNaGLJRPE3ohleIDM7RFtlQ"
    assert row["channel_title"] == "Sezen Aksu"
    assert row["duration_seconds"] == 317


def test_a_query_youtube_music_cannot_answer_falls_back(client, sources):
    """Somebody pasting the title of a talk into the same box should still
    find it — see search_video_feeds."""
    calls = sources(songs=[], videos=[TALK])

    res = client.get("/feeds/search-videos", params={"q": "keynote how we built it"})

    assert [row["video_id"] for row in res.json()] == ["dQw4w9WgXcQ"]
    assert calls == ["songs", "videos"]


def test_both_sources_coming_up_empty_is_an_empty_list(client, sources):
    sources(songs=[], videos=[])

    res = client.get("/feeds/search-videos", params={"q": "zzzzzz"})

    assert res.status_code == 200
    assert res.json() == []


def test_channel_search_never_reaches_for_youtube_music(client, sources):
    """The half of the box that finds podcasts. Moving it would swap the
    real 19M-subscriber channel for a reupload account — measured live, see
    app/youtube/music.py."""
    calls = sources(songs=[SONG], channels=[PODCAST_CHANNEL])

    res = client.get("/feeds/search", params={"q": "the diary of a ceo"})

    assert [row["channel_id"] for row in res.json()] == ["UCGq-a57w-aPwyi3pW7XLiHw"]
    assert calls == ["channels"]


@pytest.mark.parametrize("query", ["", "   "])
@pytest.mark.parametrize("path", ["/feeds/search-videos", "/feeds/search"])
def test_an_empty_query_searches_nothing(client, sources, path, query):
    calls = sources(songs=[SONG], videos=[TALK], channels=[PODCAST_CHANNEL])

    res = client.get(path, params={"q": query})

    assert res.json() == []
    assert calls == []
