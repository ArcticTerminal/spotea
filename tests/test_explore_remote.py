"""Explore drilling into something the library doesn't have yet: a YouTube
playlist or an unfollowed channel, both rendered through the ordinary detail
panel (GET /partials/detail/yt-playlist|yt-channel/{id}), plus the batch
endpoint that makes those pages playable.

yt-dlp is monkeypatched out, same as test_recommendations.py. What's under
test is the id validation guarding the URLs these build, the fact that
*browsing* one writes nothing, and that POST /feeds/videos/batch turns a whole
listing into an ordered queue without a single network call.
"""

import pytest

from app.models import Content, Feed
from app.youtube import search as yt_search
from app.youtube.search import ChannelUploads, PlaylistDetail, VideoSearchResult

PLAYLIST_ID = "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"
CHANNEL_ID = "UCYLY-BIq0sSOdNXGm1FPR-w"
OTHER_CHANNEL_ID = "UCR5wZcXtOUka8jTA57flzMg"


def _feed_url(channel_id):
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


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


@pytest.fixture
def fake_channel(monkeypatch):
    requested = []

    def fetch(channel_id):
        requested.append(channel_id)
        return ChannelUploads(
            channel_id=channel_id,
            title="Duman",
            subscriber_count=1_200_000,
            items=[_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")],
        )

    monkeypatch.setattr("app.services.remote_detail.fetch_channel_uploads", fetch)
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
    # No local-only affordances: nothing here has a Content row yet.
    assert "btn-save" not in text
    assert "/#player/" not in text


def test_a_remote_playlist_has_no_pagination(client, fake_playlist):
    # One flat fetch, and no cheap way to ask YouTube for "page 2".
    assert "pagination" not in client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").text


def test_browsing_stores_nothing(client, db_session, fake_playlist, fake_channel):
    client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}")
    client.get(f"/partials/detail/yt-channel/{CHANNEL_ID}")

    # Rows appear only when playback starts (POST /feeds/videos/batch).
    assert db_session.query(Feed).count() == 0
    assert db_session.query(Content).count() == 0


def test_a_remote_channel_offers_follow(client, fake_channel):
    text = client.get(f"/partials/detail/yt-channel/{CHANNEL_ID}").text

    assert "follow-channel-btn" in text
    assert f"https://www.youtube.com/channel/{CHANNEL_ID}" in text
    assert "unfollow-channel-btn" not in text
    assert "Latest 2 uploads" in text
    assert fake_channel == [CHANNEL_ID]


def test_an_already_followed_channel_points_at_the_library_copy(client, db_session, fake_channel):
    db_session.add(Feed(user_id=1, rss_url=_feed_url(CHANNEL_ID), channel_title="Duman"))
    db_session.commit()

    text = client.get(f"/partials/detail/yt-channel/{CHANNEL_ID}").text

    assert "follow-channel-btn" not in text
    assert "open-followed-channel-btn" in text


def test_a_placeholder_feed_does_not_count_as_followed(client, db_session, fake_channel):
    # An Explore preview leaves a followed=False feed behind (see
    # _get_or_create_placeholder_feed) — that isn't "in your library".
    db_session.add(Feed(user_id=1, rss_url=_feed_url(CHANNEL_ID), followed=False))
    db_session.commit()

    assert "follow-channel-btn" in client.get(f"/partials/detail/yt-channel/{CHANNEL_ID}").text


@pytest.mark.parametrize("playlist_id", ["short", "dQw4w9WgXcQ", "has spaces!"])
def test_a_non_playlist_id_is_rejected_without_being_fetched(client, fake_playlist, playlist_id):
    assert client.get(f"/partials/detail/yt-playlist/{playlist_id}").status_code == 404
    assert fake_playlist == []


@pytest.mark.parametrize("channel_id", ["notachannel", "UCtooshort", "UC../../etc"])
def test_a_non_channel_id_is_rejected_without_being_fetched(client, fake_channel, channel_id):
    assert client.get(f"/partials/detail/yt-channel/{channel_id}").status_code == 404
    assert fake_channel == []


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

    paths = (f"/partials/detail/yt-playlist/{PLAYLIST_ID}", f"/partials/detail/yt-channel/{CHANNEL_ID}")
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
        "/feeds/videos/batch",
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
        "/feeds/videos/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", OTHER_CHANNEL_ID)]},
    )

    feeds = db_session.query(Feed).all()
    assert len(feeds) == 2
    assert all(feed.followed is False for feed in feeds)
    assert {feed.rss_url for feed in feeds} == {_feed_url(CHANNEL_ID), _feed_url(OTHER_CHANNEL_ID)}


def test_batch_reuses_one_placeholder_feed_across_a_channels_tracks(client, db_session):
    client.post(
        "/feeds/videos/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    )

    assert db_session.query(Feed).count() == 1


def test_batch_reuses_rows_that_already_exist(client, db_session):
    first = client.post("/feeds/videos/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}).json()
    second = client.post(
        "/feeds/videos/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    ).json()

    assert second["content_ids"][0] == first["content_ids"][0]
    assert db_session.query(Content).count() == 2


def test_batch_handles_the_same_video_listed_twice(client, db_session):
    # Playlists do contain duplicates. Both positions must still resolve, and
    # to the same row — the unique (user_id, video_id) constraint leaves no
    # other option.
    ids = client.post(
        "/feeds/videos/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("aaaaaaaaaaa")]},
    ).json()["content_ids"]

    assert ids[0] == ids[1]
    assert db_session.query(Content).count() == 1


def test_batch_makes_no_network_calls(client, monkeypatch):
    # The whole reason this can be one synchronous request over fifty tracks.
    def explode(video_id):
        raise AssertionError("batch must not resolve channels over the network")

    monkeypatch.setattr("app.routers.explore.resolve_video_channel", explode)

    res = client.post("/feeds/videos/batch", json={"items": [_batch_item("aaaaaaaaaaa")]})
    assert res.status_code == 201


def test_batch_drops_items_with_an_unusable_channel_id(client, db_session):
    ids = client.post(
        "/feeds/videos/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", "nonsense")]},
    ).json()["content_ids"]

    assert len(ids) == 1
    assert db_session.query(Content).count() == 1


def test_batch_with_nothing_usable_is_a_400(client):
    res = client.post("/feeds/videos/batch", json={"items": [_batch_item("aaaaaaaaaaa", "nope")]})
    assert res.status_code == 400


def test_batch_leaves_a_followed_channels_own_content_alone(client, db_session):
    feed = Feed(user_id=1, rss_url=_feed_url(CHANNEL_ID), channel_title="Duman")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    existing = Content(feed_id=feed.id, user_id=1, video_id="aaaaaaaaaaa", title="Already here")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)
    existing_id = existing.id

    ids = client.post("/feeds/videos/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}).json()[
        "content_ids"
    ]

    db_session.expire_all()
    assert ids == [existing_id]
    # Reused, not turned back into a preview, re-titled, or duplicated behind
    # a second placeholder feed.
    reused = db_session.get(Content, existing_id)
    assert reused.is_preview is False
    assert reused.title == "Already here"
    assert db_session.query(Feed).count() == 1


def test_batch_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.post(
            "/feeds/videos/batch",
            json={"items": [_batch_item("aaaaaaaaaaa")]},
            follow_redirects=False,
        )
        assert res.status_code == 303


# --- Thumbnail selection ---------------------------------------------------


def test_yt_dlps_unsigned_maxres_guess_is_not_used_as_the_thumbnail(monkeypatch):
    """An auto-generated mix's artwork is only served behind a signed `sqp`
    query — yt-dlp appends an unsigned maxresdefault.jpg as the largest
    candidate, and it 404s, so every YouTube Music playlist card rendered
    with a broken image before this."""
    mix_id = "RDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk"
    base = f"https://i9.ytimg.com/s_p/{mix_id}"
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {
            "entries": [
                {
                    "id": mix_id,
                    "title": "Cool Jazz Moods",
                    "thumbnails": [
                        {"url": f"{base}/mqdefault.jpg?sqp=abc", "width": 180},
                        {"url": f"{base}/sddefault.jpg?sqp=abc", "width": 640},
                        {"url": f"{base}/maxresdefault.jpg", "width": 1200},
                    ],
                }
            ]
        },
    )

    (playlist,) = yt_search.search_playlists("jazz")

    assert playlist.thumbnail_url == f"{base}/sddefault.jpg?sqp=abc"


def test_the_largest_thumbnail_is_still_used_when_none_are_signed(monkeypatch):
    # Channel avatars are plain ggpht.com paths with no query at all — the
    # rule above must not start discarding those.
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {
            "entries": [
                {
                    "id": "PLaaaaaaaaaaaa",
                    "title": "Plain",
                    "thumbnails": [
                        {"url": "https://i.ytimg.com/small.jpg"},
                        {"url": "https://i.ytimg.com/large.jpg"},
                    ],
                }
            ]
        },
    )

    (playlist,) = yt_search.search_playlists("jazz")

    assert playlist.thumbnail_url == "https://i.ytimg.com/large.jpg"


# --- Flat extraction -------------------------------------------------------


def test_non_video_entries_are_dropped_from_a_playlist(monkeypatch):
    """The RD… Mix pseudo-entries YouTube mixes into playlist pages have ids
    that aren't 11 characters — _video_result drops them, so they can't reach
    the client as unplayable rows."""
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {
            "title": "Mixed",
            "playlist_count": 3,
            "entries": [
                {"id": "aaaaaaaaaaa", "title": "Real", "duration": 100},
                {"id": "RDCLAK5uy_notavideoid", "title": "Radio"},
                {"id": None, "title": "Broken"},
            ],
        },
    )

    playlist = yt_search.fetch_playlist(PLAYLIST_ID)

    assert [item.video_id for item in playlist.items] == ["aaaaaaaaaaa"]
    assert playlist.title == "Mixed"


def test_a_single_uploader_playlist_takes_its_channel_from_the_page(monkeypatch):
    """YouTube repeats a per-entry channel on a mixed playlist but omits it
    on a single-uploader one — every entry of a course playlist comes back
    with channel_id and channel both None. Those rows used to reach the client
    unusable, the batch endpoint refused all of them, and the whole playlist
    reported "Nothing to play here"."""
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {
            "title": "DEVOPS ZERO TO HERO COURSE",
            "channel_id": CHANNEL_ID,
            "channel": "Abhishek.Veeramalla",
            "playlist_count": 59,
            "entries": [
                {"id": "aaaaaaaaaaa", "title": "Day-1", "duration": 100},
                {"id": "bbbbbbbbbbb", "title": "Day-2", "duration": 200},
            ],
        },
    )

    playlist = yt_search.fetch_playlist(PLAYLIST_ID)

    assert [item.channel_id for item in playlist.items] == [CHANNEL_ID, CHANNEL_ID]
    assert [item.channel_title for item in playlist.items] == ["Abhishek.Veeramalla"] * 2


def test_a_mixed_playlists_own_entries_are_not_overwritten(monkeypatch):
    # The owner is only a fallback — on a compilation playlist the per-entry
    # uploader is the real one and must win.
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {
            "channel_id": CHANNEL_ID,
            "channel": "Playlist Owner",
            "entries": [
                {
                    "id": "aaaaaaaaaaa",
                    "title": "Someone else's song",
                    "channel_id": OTHER_CHANNEL_ID,
                    "channel": "Real Uploader",
                },
                {"id": "bbbbbbbbbbb", "title": "No attribution"},
            ],
        },
    )

    playlist = yt_search.fetch_playlist(PLAYLIST_ID)

    assert [item.channel_id for item in playlist.items] == [OTHER_CHANNEL_ID, CHANNEL_ID]
    assert [item.channel_title for item in playlist.items] == ["Real Uploader", "Playlist Owner"]


def test_a_playlist_with_no_owner_either_leaves_the_channel_unset(monkeypatch):
    # Nothing to fall back to — the batch endpoint drops these rather than
    # attaching them to the wrong channel.
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {"entries": [{"id": "aaaaaaaaaaa", "title": "Orphan"}]},
    )

    assert yt_search.fetch_playlist(PLAYLIST_ID).items[0].channel_id is None


def test_channel_uploads_fill_in_the_channel_id_entries_omit(monkeypatch):
    # Without this the batch endpoint would drop every row of a channel
    # preview, since it refuses items with no usable channel id.
    monkeypatch.setattr(
        yt_search,
        "_extract_flat",
        lambda url, limit: {
            "channel": "Duman",
            "channel_follower_count": 1000,
            "entries": [{"id": "aaaaaaaaaaa", "title": "Song", "duration": 100}],
        },
    )

    uploads = yt_search.fetch_channel_uploads(CHANNEL_ID)

    assert [item.channel_id for item in uploads.items] == [CHANNEL_ID]
    assert uploads.title == "Duman"
