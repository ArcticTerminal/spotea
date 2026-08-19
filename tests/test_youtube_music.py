"""app/youtube/music.py — the YouTube Music source, without the network.

Every response body below is a trimmed copy of a real one, captured live
against the unauthenticated API. What is being pinned here is the mapping
onto search.py's dataclasses (which is what lets this module be swapped in
behind the existing routers) and the handful of shapes that bite: browse
ids that carry a "VL" prefix, 60-pixel cover art, Topic channel ids standing
in for artists, and counts that arrive as "1.8M" rather than a number.
"""

import pytest

from app.youtube import music
from app.youtube.urls import cover_url_at_size, playlist_id_from_browse_id

SONG = {
    "title": "Biliyorsun",
    "videoId": "_efHZg9D9iE",
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "duration": "5:17",
    "duration_seconds": 317,
    "album": {"name": "Ağlamak Güzeldir", "id": "MPREb_3dKYrF4PXHQ"},
    "artists": [{"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"}],
    "thumbnails": [
        {"url": "https://yt3.googleusercontent.com/abc=w60-h60-l90-rj", "width": 60},
        {"url": "https://yt3.googleusercontent.com/abc=w120-h120-l90-rj", "width": 120},
    ],
}

FEATURED_PLAYLIST = {
    "title": "Turkish Rock Legends",
    "author": "YouTube Music",
    "browseId": "VLRDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk",
    "itemCount": 75,
    "thumbnails": [{"url": "https://yt3.googleusercontent.com/def=w226-h226-l90-rj"}],
}

CHART_ARTIST = {
    "title": "BLOK3",
    "browseId": "UCZpmeLoLLb3vmxgscRyLPgw",
    "subscribers": "1.8M",
    "rank": "1",
    "thumbnails": [{"url": "https://yt3.googleusercontent.com/ghi=w120-h120-l90-rj-dcJRaW7REL"}],
}


class FakeYTMusic:
    """Stands in for the client, recording what it was asked for. Every
    method returns whatever the test queued under its name."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        response = self.responses.get(name, [])
        return response(*args, **kwargs) if callable(response) else response

    def search(self, query, **kwargs):
        return self._record("search", query, **kwargs)

    def get_charts(self, country):
        return self._record("get_charts", country)

    def get_mood_categories(self):
        return self._record("get_mood_categories")

    def get_mood_playlists(self, params):
        return self._record("get_mood_playlists", params)

    def get_artist(self, browse_id):
        return self._record("get_artist", browse_id)


@pytest.fixture
def client(monkeypatch):
    """Installs a FakeYTMusic and hands it back, so no test in this file can
    accidentally reach the network."""

    def install(**responses):
        fake = FakeYTMusic(**responses)
        monkeypatch.setattr(music, "_client", lambda: fake)
        return fake

    return install


def test_a_song_becomes_a_video_search_result(client):
    client(search=[SONG])

    (result,) = music.search_songs("sezen aksu")

    assert result.video_id == "_efHZg9D9iE"
    assert result.title == "Biliyorsun"
    assert result.duration_seconds == 317


def test_a_songs_artists_become_its_channel(client):
    """The Topic channel id is what a preview row hangs its placeholder feed
    off (routers/explore.py), so it has to survive the mapping — and the
    artist names are what the card prints where a video would print its
    uploader."""
    client(search=[SONG])

    (result,) = music.search_songs("sezen aksu")

    assert result.channel_id == "UCNaGLJRPE3ohleIDM7RFtlQ"
    assert result.channel_title == "Sezen Aksu"


def test_several_artists_are_joined_into_one_line(client):
    client(
        search=[
            {
                **SONG,
                "artists": [
                    {"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"},
                    {"name": "Sertab Erener", "id": "UCVQJZE7dNPQdKPBPQnPHIQA"},
                ],
            }
        ]
    )

    (result,) = music.search_songs("duet")

    assert result.channel_title == "Sezen Aksu, Sertab Erener"
    assert result.channel_id == "UCNaGLJRPE3ohleIDM7RFtlQ"


def test_a_compilation_with_no_real_artist_channel_keeps_none(client):
    """"Various Artists" comes back with a name but no id. A None channel_id
    is the honest answer — the batch endpoint refuses those rows rather than
    inventing a feed for them."""
    client(search=[{**SONG, "artists": [{"name": "Various Artists", "id": None}]}])

    (result,) = music.search_songs("compilation")

    assert result.channel_title == "Various Artists"
    assert result.channel_id is None


def test_cover_art_is_requested_at_a_size_worth_rendering(client):
    """The API reports 60 and 120 pixel covers; the cards are drawn at
    roughly 200 and the panel hero at twice that."""
    client(search=[SONG])

    (result,) = music.search_songs("sezen aksu")

    assert result.thumbnail_url == "https://yt3.ggpht.com/abc=w544-h544-l90-rj"


def test_an_entry_with_no_video_id_is_dropped(client):
    client(search=[SONG, {**SONG, "videoId": None}, {**SONG, "videoId": "not-an-id"}])

    assert len(music.search_songs("sezen aksu")) == 1


def test_a_failing_call_is_an_empty_result_not_an_exception(monkeypatch):
    """Explore's search box fires while someone types — a bad response has
    to render as "nothing found", never as a 500. See the module docstring."""

    class Exploding:
        def search(self, *args, **kwargs):
            raise RuntimeError("InnerTube said no")

    monkeypatch.setattr(music, "_client", Exploding)

    assert music.search_songs("anything") == []


def test_search_never_passes_a_language(client):
    """The trap this module's docstring opens with: YTMusic(language="tr")
    returns empty lists for songs, artists and albums instead of failing, so
    the only safe rule is that nothing here ever sets one."""
    fake = client(search=[SONG])

    music.search_songs("sezen aksu")

    (_, _, kwargs), = fake.calls
    assert "language" not in kwargs


def test_a_playlist_browse_id_loses_its_vl_prefix(client):
    """Left on, it would build a youtube.com/playlist URL that resolves to
    nothing — and PLAYLIST_ID_RE accepts the prefixed form, so nothing
    downstream would catch it."""
    client(search=[FEATURED_PLAYLIST])

    (result,) = music.search_playlists("turkish rock")

    assert result.playlist_id == "RDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk"
    assert result.title == "Turkish Rock Legends"
    assert result.channel_title == "YouTube Music"


def test_playlist_search_only_falls_back_to_community_lists_when_short(client):
    fake = client(
        search=lambda query, **kwargs: (
            [FEATURED_PLAYLIST] if kwargs["filter"] == "featured_playlists" else []
        )
    )

    music.search_playlists("turkish rock", limit=1)

    assert [kwargs["filter"] for _, _, kwargs in fake.calls] == ["featured_playlists"]


def test_playlist_search_tops_up_from_community_lists_and_deduplicates(client):
    other = {**FEATURED_PLAYLIST, "browseId": "VLPLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"}
    fake = client(
        search=lambda query, **kwargs: (
            [FEATURED_PLAYLIST]
            if kwargs["filter"] == "featured_playlists"
            else [FEATURED_PLAYLIST, other]
        )
    )

    results = music.search_playlists("turkish rock", limit=4)

    assert [kwargs["filter"] for _, _, kwargs in fake.calls] == [
        "featured_playlists",
        "playlists",
    ]
    assert [result.playlist_id for result in results] == [
        "RDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk",
        "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm",
    ]


def test_a_mood_playlist_describes_itself_by_who_is_on_it(client):
    """Mood shelves report a `description` instead of an `author`, and it is
    the better subtitle of the two."""
    client(
        get_mood_playlists=[
            {
                "title": "Fall Hits",
                "playlistId": "RDCLAK5uy_k8d0XHQgAWWSZe7l7tUp0xLmEV_ncPxck",
                "description": "Taylor Swift, Lewis Capaldi",
                "thumbnails": [{"url": "https://yt3.googleusercontent.com/j=w226-h226-l90-rj"}],
            }
        ]
    )

    (result,) = music.fetch_mood_playlists("ggMPOg1uX3JBUDJTM2ZUUVJM")

    assert result.playlist_id == "RDCLAK5uy_k8d0XHQgAWWSZe7l7tUp0xLmEV_ncPxck"
    assert result.channel_title == "Taylor Swift, Lewis Capaldi"


def test_mood_categories_remember_which_section_they_came_from(client):
    client(
        get_mood_categories={
            "Moods & moments": [{"title": "Chill", "params": "aaa"}],
            "Genres": [{"title": "Blues", "params": "bbb"}],
        }
    )

    categories = music.fetch_mood_categories()

    assert [(c.title, c.section) for c in categories] == [
        ("Chill", "Moods & moments"),
        ("Blues", "Genres"),
    ]


def test_chart_playlists_come_from_the_videos_section(client):
    client(
        get_charts={
            "videos": [
                {
                    "title": "Trending 20 Turkey",
                    "playlistId": "OLAK5uy_mFBgHnPi7PIkt7vlG84rCduzVjFtuHnpM",
                    "thumbnails": [{"url": "https://yt3.googleusercontent.com/k=s192"}],
                }
            ],
            "artists": [],
        }
    )

    (result,) = music.fetch_chart_playlists("TR")

    assert result.playlist_id == "OLAK5uy_mFBgHnPi7PIkt7vlG84rCduzVjFtuHnpM"
    # Chart art names its size the other way round ("=s192", not
    # "=w226-h226"); cover_url_at_size handles both.
    assert result.thumbnail_url == "https://yt3.ggpht.com/k=s544"


def test_a_charting_artist_becomes_a_followable_channel(client):
    client(get_charts={"videos": [], "artists": [CHART_ARTIST]})

    (result,) = music.fetch_chart_artists("TR")

    assert result.channel_id == "UCZpmeLoLLb3vmxgscRyLPgw"
    assert result.subscriber_count == 1_800_000
    assert result.channel_url == "https://www.youtube.com/channel/UCZpmeLoLLb3vmxgscRyLPgw"


def test_an_artist_without_a_channel_behind_it_is_dropped(client):
    client(get_charts={"videos": [], "artists": [{**CHART_ARTIST, "browseId": "MPLAucbrowseid"}]})

    assert music.fetch_chart_artists("TR") == []


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("1.8M", 1_800_000),
        ("3.19M", 3_190_000),
        ("952K", 952_000),
        ("1.2B", 1_200_000_000),
        ("4,370,252,054 views", 4_370_252_054),
        ("no idea", None),
        (None, None),
    ],
)
def test_display_counts_become_numbers(reported, expected):
    assert music._parse_count(reported) == expected


def test_an_artist_page_resolves_the_official_channel(client):
    """The whole reason a follow action pays for this call: the browse id is
    the Topic channel, and `channelId` is the artist's real one."""
    client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "subscribers": "3.19M",
            "monthlyListeners": "1.9M monthly listeners",
            "description": "Turkish singer, songwriter and producer.",
            "thumbnails": [{"url": "https://lh3.googleusercontent.com/l=w120-h120-p-l90-rj"}],
            "songs": {"results": [SONG]},
            "videos": {"results": []},
        }
    )

    artist = music.fetch_artist("UCNaGLJRPE3ohleIDM7RFtlQ")

    assert artist.channel_id == "UC6OI7Crv96jgra5pwJNDFRQ"
    assert artist.subscriber_count == 3_190_000
    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE"]
    assert music.resolve_artist_channel("UCNaGLJRPE3ohleIDM7RFtlQ") == "UC6OI7Crv96jgra5pwJNDFRQ"


def test_an_artists_songs_and_videos_merge_without_repeats(client):
    """A release exists twice under different ids — the audio track and the
    official music video. Both are playable; the same id twice is a bug."""
    video = {**SONG, "videoId": "3q4cJ1G_on8", "title": "Aşk Dansı"}
    client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "songs": {"results": [SONG]},
            "videos": {"results": [SONG, video]},
        }
    )

    artist = music.fetch_artist("UCNaGLJRPE3ohleIDM7RFtlQ")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE", "3q4cJ1G_on8"]


def test_an_unknown_artist_is_none_not_an_empty_profile(client):
    client(get_artist=None)

    assert music.fetch_artist("UCnope") is None
    assert music.resolve_artist_channel("UCnope") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # YouTube Music's own dialect, with and without a trailing segment.
        ("https://x/a=w60-h60-l90-rj", "https://x/a=w544-h544-l90-rj"),
        ("https://x/a=w120-h120-l90-rj-dcJRaW7REL", "https://x/a=w544-h544-l90-rj-dcJRaW7REL"),
        ("https://x/a=w60-h60-p-l90-rj", "https://x/a=w544-h544-p-l90-rj"),
        # The "=s<n>" dialect, which chart art uses.
        ("https://x/a=s192", "https://x/a=s544"),
        # A video still: signed, not resizable, left alone.
        ("https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc", "https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc"),
        (None, None),
    ],
)
def test_cover_url_at_size_handles_both_size_dialects(url, expected):
    assert cover_url_at_size(url, 544) == expected


@pytest.mark.parametrize(
    ("browse_id", "expected"),
    [
        ("VLPLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm", "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"),
        ("VLRDCLAK5uy_mq6KpOULj", "RDCLAK5uy_mq6KpOULj"),
        # Already unprefixed — the mood shelves report ids this way.
        ("RDCLAK5uy_mq6KpOULj", "RDCLAK5uy_mq6KpOULj"),
        # Too short to be a playlist id once the prefix comes off.
        ("VLPL", None),
        (None, None),
    ],
)
def test_playlist_id_from_browse_id(browse_id, expected):
    assert playlist_id_from_browse_id(browse_id) == expected
