"""Explore drilling into a YouTube Music playlist the library doesn't have,
rendered through the ordinary detail panel (GET
/partials/detail/yt-playlist/{id}), plus the batch endpoint that makes that
page playable.

The fetch is monkeypatched out, same as test_recommendations.py. What's
under test is the id validation guarding the routes, the fact that
*browsing* one writes nothing, and that POST /explore/tracks/batch turns a
whole listing into an ordered queue without a single network call.
"""

from pathlib import Path

import pytest

from app.models import Artist, Content
from app.youtube.models import PlaylistDetail, VideoSearchResult

PLAYLIST_ID = "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"
CHANNEL_ID = "UCYLY-BIq0sSOdNXGm1FPR-w"
OTHER_CHANNEL_ID = "UCR5wZcXtOUka8jTA57flzMg"


def _track(video_id, channel_id=CHANNEL_ID):
    return VideoSearchResult(
        video_id=video_id,
        title="Track",
        thumbnail_url="https://i.ytimg.com/vi/x/hq720.jpg",
        duration_seconds=241,
        channel_title="Some Channel",
        channel_id=channel_id,
    )


@pytest.fixture
def fake_playlist(monkeypatch):
    """Installs a playlist for the fragment route to find, and records the ids
    it was asked for so tests can assert nothing else was fetched."""
    requested = []

    def fetch(playlist_id):
        requested.append(playlist_id)
        return PlaylistDetail(
            playlist_id=playlist_id,
            title="Best Of Turkish Rock",
            video_count=120,
            items=[_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb", OTHER_CHANNEL_ID)],
        )

    monkeypatch.setattr("app.services.remote_detail.fetch_playlist", fetch)
    return requested


# --- Browsing --------------------------------------------------------------


def test_a_remote_playlist_renders_the_detail_panel(client, fake_playlist):
    res = client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}")

    assert res.status_code == 200
    # The same panel a followed channel gets, not a second kind of page.
    assert "track-list" in res.text
    assert "detail-play-all" in res.text
    assert "Best Of Turkish Rock" in res.text
    # 120 tracks behind a 2-item fetch — say so rather than implying these are
    # all of them.
    assert "First 2 of 120 tracks" in res.text
    assert fake_playlist == [PLAYLIST_ID]


def test_a_remote_row_carries_what_the_batch_endpoint_needs(client, fake_playlist):
    text = client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").text

    assert 'data-video-id="aaaaaaaaaaa"' in text
    assert f'data-channel-id="{CHANNEL_ID}"' in text
    # No local-only affordances: nothing here has a Content row yet, so there
    # is no /#player/{id} to point at.
    assert "/#player/" not in text
    assert "data-content-id" not in text


def test_a_remote_playlist_has_no_pagination(client, fake_playlist):
    # One flat fetch, and no cheap way to ask YouTube for "page 2".
    assert "pagination" not in client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").text


def test_browsing_stores_nothing(client, db_session, fake_playlist):
    client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}")

    # Rows appear only when playback starts (POST /explore/tracks/batch).
    assert db_session.query(Artist).count() == 0
    assert db_session.query(Content).count() == 0


@pytest.mark.parametrize("playlist_id", ["short", "dQw4w9WgXcQ", "has spaces!"])
def test_a_non_playlist_id_is_rejected_without_being_fetched(client, fake_playlist, playlist_id):
    assert client.get(f"/partials/detail/yt-playlist/{playlist_id}").status_code == 404
    assert fake_playlist == []


def test_an_unreadable_playlist_is_a_404(client, monkeypatch):
    # search.fetch_playlist flattens every yt-dlp failure into an empty
    # result, so "no items" is the only signal the route gets for deleted,
    # private, or simply-failed.
    monkeypatch.setattr(
        "app.services.remote_detail.fetch_playlist",
        lambda playlist_id: PlaylistDetail(
            playlist_id=playlist_id, title=None, video_count=None, items=[]
        ),
    )

    assert client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").status_code == 404


def test_remote_detail_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    paths = (f"/partials/detail/yt-playlist/{PLAYLIST_ID}",)
    with TestClient(app) as anonymous:
        for path in paths:
            assert anonymous.get(path, follow_redirects=False).status_code == 303


# --- Making a remote listing playable --------------------------------------


def _batch_item(video_id, channel_id=CHANNEL_ID):
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": f"Track {video_id}",
        "thumbnail_url": "https://i.ytimg.com/vi/x/hq720.jpg",
        "duration_seconds": 200,
        "channel_title": "Some Channel",
    }


def test_batch_creates_preview_rows_in_the_order_given(client, db_session):
    res = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", OTHER_CHANNEL_ID)]},
    )

    assert res.status_code == 201
    ids = res.json()["content_ids"]
    rows = {content.id: content for content in db_session.query(Content)}
    # Order in equals order out — the caller uses it directly as the queue.
    assert [rows[i].video_id for i in ids] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert all(rows[i].is_preview for i in ids)


def test_batch_attaches_each_row_to_its_own_placeholder_feed(client, db_session):
    client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", OTHER_CHANNEL_ID)]},
    )

    artists = db_session.query(Artist).all()
    assert len(artists) == 2
    assert all(artist.followed is False for artist in artists)
    assert {artist.channel_id for artist in artists} == {CHANNEL_ID, OTHER_CHANNEL_ID}


def test_batch_reuses_one_placeholder_feed_across_a_channels_tracks(client, db_session):
    client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    )

    assert db_session.query(Artist).count() == 1


def test_batch_reuses_rows_that_already_exist(client, db_session):
    first = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}).json()
    second = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    ).json()

    assert second["content_ids"][0] == first["content_ids"][0]
    assert db_session.query(Content).count() == 2


def test_batch_handles_the_same_video_listed_twice(client, db_session):
    # Playlists do contain duplicates. Both positions must still resolve, and
    # to the same row — the unique (user_id, video_id) constraint leaves no
    # other option.
    ids = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("aaaaaaaaaaa")]},
    ).json()["content_ids"]

    assert ids[0] == ids[1]
    assert db_session.query(Content).count() == 1


def test_batch_makes_no_network_calls(client):
    """The whole reason this can be one synchronous request over fifty
    tracks: every field it needs came back with the listing, so nothing
    here reaches YouTube at all."""
    source = Path("app/routers/explore.py").read_text()
    network_imports = [
        line
        for line in source.splitlines()
        if line.startswith(("from app.youtube.music", "from app.youtube.extract"))
    ]
    assert network_imports == ["from app.youtube.music import search_artists, search_songs"], (
        "the add routes reach YouTube again — every field they need already "
        f"came back with the listing. Found: {network_imports}"
    )

    res = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]})
    assert res.status_code == 201


def test_batch_drops_items_with_an_unusable_channel_id(client, db_session):
    ids = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", "nonsense")]},
    ).json()["content_ids"]

    assert len(ids) == 1
    assert db_session.query(Content).count() == 1


def test_batch_with_nothing_usable_is_a_400(client):
    res = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa", "nope")]})
    assert res.status_code == 400


def test_batch_leaves_a_followed_channels_own_content_alone(client, db_session):
    artist = Artist(user_id=1, channel_id=CHANNEL_ID, name="Duman")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    existing = Content(artist_id=artist.id, user_id=1, video_id="aaaaaaaaaaa", title="Already here")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)
    existing_id = existing.id

    ids = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}).json()[
        "content_ids"
    ]

    db_session.expire_all()
    assert ids == [existing_id]
    # Reused, not turned back into a preview, re-titled, or duplicated behind
    # a second placeholder artist.
    reused = db_session.get(Content, existing_id)
    assert reused.is_preview is False
    assert reused.title == "Already here"
    assert db_session.query(Artist).count() == 1


def test_batch_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.post(
            "/explore/tracks/batch",
            json={"items": [_batch_item("aaaaaaaaaaa")]},
            follow_redirects=False,
        )
        assert res.status_code == 303


# --- A release: one track plays, more than one opens -----------------------

RELEASE_ID = "MPREb_aaaaaaaaaaa"


@pytest.fixture
def fake_release(monkeypatch):
    """Installs a release for the fragment route to find. `tracks` decides
    which of the route's two answers comes back, which is the whole point of
    these tests."""
    from app.youtube.music import ReleaseDetail

    holder = {"tracks": [_track("aaaaaaaaaaa")]}
    calls = []

    def fetch(browse_id):
        calls.append(browse_id)
        return ReleaseDetail(
            title="Miss Jamaica",
            year="2026",
            kind="Single",
            cover_url="https://lh3.googleusercontent.com/cover",
            artist_names="Jimmy Cliff",
            tracks=holder["tracks"],
        )

    monkeypatch.setattr("app.services.remote_detail.fetch_release", fetch)
    return holder, calls


def test_a_one_track_release_answers_with_the_track_instead_of_a_panel(client, fake_release):
    """The point of the whole change: a release with one track in it has no
    panel worth showing, so the route hands back what to play."""
    holder, _ = fake_release
    holder["tracks"] = [_track("aaaaaaaaaaa")]

    res = client.get(f"/partials/detail/yt-release/{RELEASE_ID}")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    # Keyed the way _remote_track_row.html writes its dataset, so
    # home/remote.js's playRemoteVideo takes it unchanged.
    assert res.json() == {
        "videoId": "aaaaaaaaaaa",
        "title": "Track",
        "channelId": CHANNEL_ID,
        "thumbnailUrl": "https://i.ytimg.com/vi/x/hq720.jpg",
        "durationSeconds": "241",
        "channelTitle": "Some Channel",
    }


def test_a_two_track_release_still_opens_the_panel(client, fake_release):
    """"Single" is YouTube Music's label, not a track count — it puts the
    word on plenty of two- and three-track releases. Going by the count
    keeps the other tracks reachable."""
    holder, _ = fake_release
    holder["tracks"] = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]

    res = client.get(f"/partials/detail/yt-release/{RELEASE_ID}")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "track-list" in res.text
    assert "Miss Jamaica" in res.text


def test_deciding_costs_exactly_one_fetch(client, fake_release):
    """Either answer comes out of the same single request — the track count
    and the video id arrive together, so there is no probe-then-open."""
    holder, calls = fake_release
    holder["tracks"] = [_track("aaaaaaaaaaa")]

    client.get(f"/partials/detail/yt-release/{RELEASE_ID}")

    assert calls == [RELEASE_ID]


def test_a_release_id_is_validated_before_anything_is_fetched(client, fake_release):
    _, calls = fake_release

    res = client.get("/partials/detail/yt-release/not-a-release-id")

    assert res.status_code == 404
    assert calls == []
