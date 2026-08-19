"""app/youtube/music.py — the YouTube Music source, without the network.

Every response body below is a trimmed copy of a real one, captured live
against the unauthenticated API. What is being pinned here is the mapping
onto search.py's dataclasses (which is what lets this module be swapped in
behind the existing routers) and the handful of shapes that bite: browse
ids that carry a "VL" prefix, 60-pixel cover art, Topic channel ids standing
in for artists, and counts that arrive as "1.8M" rather than a number.
"""

import logging

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

    def get_playlist(self, playlist_id, limit=None):
        return self._record("get_playlist", playlist_id, limit=limit)

    def get_album(self, browse_id):
        return self._record("get_album", browse_id)


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


MOOD_MENU = {
    "Moods & moments": [{"title": "Chill", "params": "aaa"}],
    "Genres": [{"title": "Blues", "params": "bbb"}],
}


def test_mood_categories_skip_the_section_that_cannot_be_parsed(client):
    """Measured across all 40 categories: every "Genres" entry raises a
    parse error from inside ytmusicapi. See music.MOOD_SECTION."""
    client(get_mood_categories=MOOD_MENU)

    categories = music.fetch_mood_categories()

    assert [(c.title, c.section) for c in categories] == [("Chill", "Moods & moments")]


def test_mood_categories_remember_which_section_they_came_from(client):
    client(get_mood_categories=MOOD_MENU)

    categories = music.fetch_mood_categories(section=None)

    assert [(c.title, c.section) for c in categories] == [
        ("Chill", "Moods & moments"),
        ("Blues", "Genres"),
    ]


CHART_PLAYLIST = {
    "title": "Trending 20 Turkey",
    "playlistId": "OLAK5uy_mFBgHnPi7PIkt7vlG84rCduzVjFtuHnpM",
    "thumbnails": [{"url": "https://yt3.googleusercontent.com/k=s192"}],
}


def test_both_chart_shelves_come_from_one_request(client):
    fake = client(get_charts={"videos": [CHART_PLAYLIST], "artists": [CHART_ARTIST]})

    charts = music.fetch_charts("TR")

    assert len(fake.calls) == 1
    (playlist,) = charts.playlists
    assert playlist.playlist_id == "OLAK5uy_mFBgHnPi7PIkt7vlG84rCduzVjFtuHnpM"
    # Chart art names its size the other way round ("=s192", not
    # "=w226-h226"); cover_url_at_size handles both.
    assert playlist.thumbnail_url == "https://yt3.ggpht.com/k=s544"


def test_a_charting_artist_becomes_a_followable_channel(client):
    client(get_charts={"videos": [], "artists": [CHART_ARTIST]})

    (result,) = music.fetch_charts("TR").artists

    assert result.channel_id == "UCZpmeLoLLb3vmxgscRyLPgw"
    assert result.subscriber_count == 1_800_000
    assert result.channel_url == "https://www.youtube.com/channel/UCZpmeLoLLb3vmxgscRyLPgw"


def test_an_artist_without_a_channel_behind_it_is_dropped(client):
    client(get_charts={"videos": [], "artists": [{**CHART_ARTIST, "browseId": "MPLAucbrowseid"}]})

    assert music.fetch_charts("TR").artists == []


def test_a_country_with_no_charts_at_all_is_two_empty_shelves(client):
    client(get_charts=None)

    charts = music.fetch_charts("ZZ")

    assert charts.playlists == []
    assert charts.artists == []


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


def test_the_videos_section_is_left_out(client):
    """It used to be merged in. Measured on Drake and Shirin David: 8 of the
    10 videos were the same song as an entry already in the list under a
    different id (audio track vs official video), and a video entry carries
    no duration at all — so the merge bought a handful of duplicate,
    duration-less rows at the bottom of the panel."""
    music_video = {"videoId": "3q4cJ1G_on8", "title": "Aşk Dansı", "views": "1.7B"}
    client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "songs": {"results": [SONG]},
            "videos": {"results": [music_video]},
        }
    )

    artist = music.fetch_artist("UCNaGLJRPE3ohleIDM7RFtlQ")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE"]


def test_the_same_id_is_never_listed_twice(client):
    """A playlist can repeat an entry; the panel shouldn't."""
    client(
        get_artist={"name": "Sezen Aksu", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": [SONG, SONG]},
    )

    assert [track.video_id for track in music.fetch_artist("UCx").tracks] == ["_efHZg9D9iE"]


def test_an_artist_page_lists_the_whole_top_songs_playlist(client):
    """The page itself previews five songs and keeps the rest behind a
    browse id — 56 of them for a mid-size artist, measured. Five is not an
    artist page worth opening, so the playlist is what gets listed."""
    deep_cut = {**SONG, "videoId": "3q4cJ1G_on8", "title": "Aşk Dansı"}
    fake = client(
        get_artist={
            "name": "Shirin David",
            "channelId": "UC5ZkRnYd3__WBBGnAnWO9Cg",
            "songs": {"browseId": "VLOLAK5uy_mcACjdxLHv", "results": [SONG]},
            "videos": {"results": []},
        },
        get_playlist={"title": "Top songs", "tracks": [SONG, deep_cut]},
    )

    artist = music.fetch_artist("UC5ZkRnYd3__WBBGnAnWO9Cg")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE", "3q4cJ1G_on8"]
    assert ("get_playlist", ("VLOLAK5uy_mcACjdxLHv",), {"limit": music.ARTIST_TRACK_LIMIT}) in fake.calls


def test_the_previewed_songs_stand_in_when_the_playlist_cannot_be_read(client):
    """A five-track page is a worse artist page, and an empty one falls
    through to the channel listing (see services/remote_detail.py) — which
    for a vlogging artist is the listing this whole route exists to avoid."""
    client(
        get_artist={
            "name": "Shirin David",
            "songs": {"browseId": "VLOLAK5uy_mcACjdxLHv", "results": [SONG]},
        },
        get_playlist=None,
    )

    artist = music.fetch_artist("UC5ZkRnYd3__WBBGnAnWO9Cg")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE"]


def test_the_cap_is_above_anything_youtube_music_serves(client):
    """A "Top songs" playlist stops at 150 for everyone — Taylor Swift,
    Drake, Bach — so the longest real page is that plus the music videos.
    The cap is a bound against an unbounded remote list, not something the
    catalogue is expected to hit; if it starts biting, the panel needs
    pagination rather than a bigger number here."""
    songs = [{**SONG, "videoId": f"_efHZg9D{n:03d}"} for n in range(150)]
    client(
        get_artist={"name": "Sezen Aksu", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": songs, "trackCount": 150},
    )

    artist = music.fetch_artist("UCx")

    assert len(artist.tracks) == 150 < music.ARTIST_TRACK_LIMIT
    assert artist.track_count == 150


def test_an_absurd_list_is_still_capped(client):
    tracks = [{**SONG, "videoId": f"_efHZg9{n:04d}"} for n in range(250)]
    client(
        get_artist={"name": "Someone", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": tracks, "trackCount": 250},
    )

    assert len(music.fetch_artist("UCx").tracks) == music.ARTIST_TRACK_LIMIT


def test_entries_youtube_drops_are_reported_as_missing(client):
    """Measured: Drake's playlist reports 150 tracks and yields 143, Bach's
    147. The shortfall is what `track_count` exists to carry — the panel
    says "first 143 of 150" instead of implying 143 is the whole list."""
    tracks = [{**SONG, "videoId": f"_efHZg9D{n:03d}"} for n in range(143)]
    client(
        get_artist={"name": "Drake", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": tracks, "trackCount": 150},
    )

    artist = music.fetch_artist("UCx")

    assert len(artist.tracks) == 143
    assert artist.track_count == 150


def test_a_short_catalogue_is_not_reported_as_truncated(client):
    """56 songs is the whole page — a count higher than the list would put a
    "first N of M" on a page that is showing all of them."""
    tracks = [{**SONG, "videoId": f"_efHZg9D{n:03d}"} for n in range(56)]
    client(
        get_artist={"name": "Shirin David", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": tracks, "trackCount": 56},
    )

    artist = music.fetch_artist("UCx")

    assert len(artist.tracks) == 56
    assert artist.track_count == 56


def test_resolving_a_channel_does_not_pay_for_the_track_list(client):
    """A follow click wants one field off the page header. The second
    request the track list costs would buy nothing there."""
    fake = client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "songs": {"browseId": "VLOLAK5uy_mcACjdxLHv", "results": [SONG]},
        }
    )

    assert music.resolve_artist_channel("UCNaGLJRPE3ohleIDM7RFtlQ") == "UC6OI7Crv96jgra5pwJNDFRQ"
    assert [call[0] for call in fake.calls] == ["get_artist"]


def test_a_channel_that_is_not_an_artist_is_none(client, caplog):
    """Measured live: asking for a podcast or a tech channel raises
    KeyError('musicImmersiveHeaderRenderer') from inside ytmusicapi. That is
    what makes it safe to try any channel id here and let the answer decide
    — see services/remote_detail.py.

    And it must not warn. Every podcast opened from Explore now asks this
    question first, so a traceback per failure is a log nobody can read."""

    def raise_key_error(browse_id):
        raise KeyError("musicImmersiveHeaderRenderer")

    client(get_artist=raise_key_error)

    with caplog.at_level(logging.INFO, logger="app.youtube.music"):
        assert music.fetch_artist("UCGq-a57w-aPwyi3pW7XLiHw") is None

    assert [record.levelno for record in caplog.records] == [logging.INFO]


def test_an_artist_page_carries_its_releases(client):
    """All of it off the one response the songs came from, which is what
    makes a profile cost what a bare track list cost."""
    client(
        get_artist={
            "name": "Shirin David",
            "songs": {"results": [SONG]},
            "albums": {
                "results": [
                    {
                        "title": "Schlau aber blond",
                        "browseId": "MPREb_HIQTwIoDtEM",
                        "year": "2025",
                        "audioPlaylistId": "OLAK5uy_niuCyuWWZYKv6jIwsWqDkVsYiBq9C_Plg",
                        "thumbnails": [{"url": "https://x/c=w226-h226-l90-rj"}],
                    }
                ]
            },
            "singles": {
                "results": [
                    {"title": "Gut Genug", "browseId": "MPREb_5Y3mCZ5XtG3", "year": "2026", "type": "Single"}
                ]
            },
            "related": {"results": [CHART_ARTIST]},
        }
    )

    artist = music.fetch_artist("UCx")

    (album,) = artist.albums
    assert (album.browse_id, album.year, album.kind) == ("MPREb_HIQTwIoDtEM", "2025", "Album")
    assert album.cover_url == "https://x/c=w544-h544-l90-rj"
    (single,) = artist.singles
    # Singles report their own type; albums report none, so the shelf names it.
    assert single.kind == "Single"
    assert [artist.title for artist in artist.related] == ["BLOK3"]


def test_a_release_with_no_browse_id_is_dropped(client):
    """A card with nothing to open is worse than one card fewer."""
    client(
        get_artist={
            "name": "Shirin David",
            "songs": {"results": [SONG]},
            "albums": {"results": [{"title": "Nameless"}, {"browseId": "MPREb_ok"}]},
        }
    )

    assert music.fetch_artist("UCx").albums == []


def test_an_album_and_a_single_open_the_same_way(client):
    """YouTube Music answers a one-track single and a fourteen-track album
    with the identical structure, which is why one route serves both."""
    client(
        get_album={
            "title": "Schlau aber blond",
            "year": "2025",
            "type": "Album",
            "artists": [{"name": "Shirin David"}],
            "thumbnails": [{"url": "https://x/c=w226-h226-l90-rj"}],
            "tracks": [SONG],
        }
    )

    release = music.fetch_release("MPREb_HIQTwIoDtEM")

    assert (release.title, release.year, release.kind) == ("Schlau aber blond", "2025", "Album")
    assert release.artist_names == "Shirin David"
    assert [track.video_id for track in release.tracks] == ["_efHZg9D9iE"]
    assert release.cover_url == "https://x/c=w544-h544-l90-rj"


@pytest.mark.parametrize("response", [None, {"title": "Gone", "tracks": []}, {"tracks": [SONG]}])
def test_a_release_that_cannot_be_read_is_none(client, response):
    client(get_album=response)

    assert music.fetch_release("MPREb_x") is None


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
