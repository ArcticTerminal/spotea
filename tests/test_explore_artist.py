"""The artist detail panel: the two things it exists to get right, and the
fallback that lets every channel result go through it.

Getting right: following the artist's *official* channel rather than the
auto-generated "<Artist> - Topic" one every music result is attributed to,
and channel search no longer offering those Topic channels to follow at
all.

The fallback: this panel is what a channel card opens now, so an id that
isn't an artist — or is one with nothing playable — has to come back as the
plain channel listing rather than a 404. Both sources are monkeypatched
out.
"""

import pytest

from app.models import Feed
from app.services import remote_detail
from app.youtube import search as yt_search
from app.youtube.music import ArtistProfile
from app.youtube.search import VideoSearchResult

USER_ID = 1
BROWSE_ID = "UCNaGLJRPE3ohleIDM7RFtlQ"
OFFICIAL_ID = "UC6OI7Crv96jgra5pwJNDFRQ"


def _track(video_id="_efHZg9D9iE"):
    return VideoSearchResult(
        video_id=video_id,
        title="Biliyorsun",
        thumbnail_url="https://yt3.ggpht.com/abc=w544-h544-l90-rj",
        duration_seconds=317,
        channel_title="Sezen Aksu",
        channel_id=BROWSE_ID,
    )


def _profile(**overrides):
    fields = {
        "browse_id": BROWSE_ID,
        "channel_id": OFFICIAL_ID,
        "name": "Sezen Aksu",
        "description": "Turkish singer, songwriter and producer.",
        "subscriber_count": 3_190_000,
        "monthly_listeners": "28.9M",
        "avatar_url": "https://lh3.googleusercontent.com/x=w544-h544-p-l90-rj",
        "tracks": [_track()],
    }
    fields.update(overrides)
    return ArtistProfile(**fields)


@pytest.fixture
def fake_artist(monkeypatch):
    def install(profile):
        monkeypatch.setattr(remote_detail, "fetch_artist", lambda browse_id: profile)

    return install


@pytest.fixture
def fake_channel_fallback(monkeypatch):
    """Stands in for remote_channel_context and records what it was handed,
    so the tests below can tell "fell back" apart from "404" and check that
    the avatar hint survived the trip."""
    calls: list[dict] = []

    def fallback(db, user_id, channel_id, avatar_url=None):
        calls.append({"channel_id": channel_id, "avatar_url": avatar_url})
        return {
            "kind": "yt-channel",
            "remote": True,
            "feed": None,
            "title": "A Plain Channel",
            "content": [],
            "empty_message": "Nothing playable here.",
            "back_label": "Explore",
            "page": 1,
            "total_pages": 1,
            "start_index": 1,
            "base_url": "#",
            "video_count": 0,
        }

    monkeypatch.setattr(remote_detail, "remote_channel_context", fallback)
    return calls


def test_the_panel_lists_the_artists_tracks(client, fake_artist):
    fake_artist(_profile())

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert res.status_code == 200
    assert "Sezen Aksu" in res.text
    assert "Biliyorsun" in res.text
    assert "28.9M monthly listeners" in res.text


def test_follow_targets_the_official_channel_not_the_topic_one(client, fake_artist):
    """The whole reason this panel is its own kind. The tracks below it are
    attributed to the Topic channel; the button has to point somewhere
    else."""
    fake_artist(_profile())

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert f"https://www.youtube.com/channel/{OFFICIAL_ID}" in res.text
    assert f"https://www.youtube.com/channel/{BROWSE_ID}" not in res.text


def test_an_artist_with_no_official_channel_falls_back_to_the_browse_id(client, fake_artist):
    """A button pointing at the Topic channel is a worse answer than the
    right one, and a better answer than a button that does nothing."""
    fake_artist(_profile(channel_id=None))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert f"https://www.youtube.com/channel/{BROWSE_ID}" in res.text


def test_an_already_followed_artist_points_at_the_library_copy(client, db_session, fake_artist):
    """Followed-ness is checked against the official channel too — following
    from this panel creates a feed for that id, so anything else would leave
    the button saying "Follow" forever."""
    fake_artist(_profile())
    feed = Feed(
        user_id=USER_ID,
        rss_url=f"https://www.youtube.com/feeds/videos.xml?channel_id={OFFICIAL_ID}",
        channel_title="Sezen Aksu",
        followed=True,
    )
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert "In your library" in res.text
    assert "Follow" not in res.text


def test_an_id_that_is_not_an_artist_opens_as_a_channel(
    client, fake_artist, fake_channel_fallback
):
    """Every channel result routes through here, and most channels aren't
    artists — a podcast that opened as a 404 would be a broken app."""
    fake_artist(None)

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert res.status_code == 200
    assert "A Plain Channel" in res.text


def test_an_artist_with_nothing_playable_opens_as_a_channel_too(
    client, fake_artist, fake_channel_fallback
):
    """YouTube Music knowing the name is not the same as it having tracks.
    The channel's own uploads are still a real answer, so this falls back
    rather than 404ing on a page it could have shown."""
    fake_artist(_profile(tracks=[]))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert res.status_code == 200
    assert "A Plain Channel" in res.text


def test_the_cards_avatar_hint_survives_the_fallback(
    client, fake_artist, fake_channel_fallback
):
    """An artist page brings its own portrait, but the channel listing has
    none to fetch cheaply — so the hint the card sent has to reach it (see
    remote_channel_context)."""
    fake_artist(None)

    client.get(f"/partials/detail/yt-artist/{BROWSE_ID}?avatar=/avatars/{BROWSE_ID}.jpg")

    assert fake_channel_fallback == [
        {"channel_id": BROWSE_ID, "avatar_url": f"/avatars/{BROWSE_ID}.jpg"}
    ]


@pytest.mark.parametrize("browse_id", ["not-an-id", "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm", "UC"])
def test_a_non_channel_id_is_rejected_without_being_fetched(client, monkeypatch, browse_id):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not fetch an id that failed validation")

    monkeypatch.setattr(remote_detail, "fetch_artist", fail_if_called)

    assert client.get(f"/partials/detail/yt-artist/{browse_id}").status_code == 404


def _channel_entry(title, channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA"):
    return {
        "channel_id": channel_id,
        "title": title,
        "channel_follower_count": 100,
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
        "thumbnails": [],
    }


def test_channel_search_drops_the_topic_channels(monkeypatch):
    """Measured live: "sezen aksu" returns the real channel (3.19M) and then
    three Topic channels for the same artist with 587, 12 and 1 subscribers.
    Offering those as things to follow is offering the wrong answer three
    times."""
    monkeypatch.setattr(
        yt_search,
        "_search_entries",
        lambda url: [
            _channel_entry("Sezen Aksu", "UC6OI7Crv96jgra5pwJNDFRQ"),
            _channel_entry("Sezen Aksu - Topic", "UCNaGLJRPE3ohleIDM7RFtlQ"),
        ],
    )
    monkeypatch.setattr(yt_search, "cached_avatar_path", lambda channel_id: None)

    results = yt_search.search_channels("sezen aksu")

    assert [result.channel_id for result in results] == ["UC6OI7Crv96jgra5pwJNDFRQ"]


def test_a_channel_that_merely_mentions_topic_is_kept(monkeypatch):
    """The rule is the auto-generated suffix, not the word."""
    monkeypatch.setattr(
        yt_search, "_search_entries", lambda url: [_channel_entry("On Topic with Dan")]
    )
    monkeypatch.setattr(yt_search, "cached_avatar_path", lambda channel_id: None)

    assert len(yt_search.search_channels("on topic")) == 1
