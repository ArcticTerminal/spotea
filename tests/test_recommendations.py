"""Explore's interest-based recommendations.

Every YouTube search is monkeypatched out — what's under test is the part
that decides *whether* to search at all (the cache), which interests get
searched, and how the results are merged. Actually calling yt-dlp here would
make the suite slow, flaky and, on a residential IP, a genuine rate-limiting
liability.
"""

from datetime import timedelta

import pytest

from app.models import Content, Feed, RecommendationCache, User
from app.services import recommendations as rec
from app.timeutil import utcnow
from app.youtube.models import ChannelSearchResult, PlaylistSearchResult, VideoSearchResult

USER_ID = 1


@pytest.fixture(autouse=True)
def _reset_interests(db_session):
    """The default profile isn't deleted between tests (see conftest), so its
    interests have to be cleared by hand or they leak into the next one."""
    yield
    profile = db_session.get(User, USER_ID)
    profile.interests = None
    db_session.commit()


def _video(video_id, title="Song"):
    return VideoSearchResult(
        video_id=video_id, title=title, thumbnail_url=None, duration_seconds=200, channel_title="Ch"
    )


def _channel(channel_id):
    return ChannelSearchResult(
        channel_id=channel_id,
        title=f"Channel {channel_id}",
        thumbnail_url=None,
        subscriber_count=10,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
    )


def _playlist(playlist_id):
    return PlaylistSearchResult(
        playlist_id=playlist_id,
        title=f"Playlist {playlist_id}",
        thumbnail_url=None,
        channel_title="Ch",
    )


@pytest.fixture(autouse=True)
def _no_browse_shelves(monkeypatch):
    """Charts and the mood shelf are built on every run, interests or not —
    so unlike the searches they can't be neutralised by leaving the interest
    list empty. Stubbed out for every test in this file for exactly the
    reason the module docstring gives; the two that are *about* them install
    their own (see fake_browse)."""
    monkeypatch.setattr(rec, "_BROWSE_BUILDERS", ())


@pytest.fixture
def fake_browse(monkeypatch):
    """Puts a known chart pair and mood shelf back, for the tests that check
    what happens to them."""

    def install(*, charts=(), chart_artists=(), mood=None):
        monkeypatch.setattr(
            rec,
            "_BROWSE_BUILDERS",
            (
                lambda: {
                    "charts": [_playlist(p).__dict__ for p in charts],
                    "chart_artists": [_channel(c).__dict__ for c in chart_artists],
                },
                lambda: {"mood": mood},
            ),
        )

    return install


@pytest.fixture
def fake_search(monkeypatch):
    """Replaces all three searches with deterministic, query-derived results,
    and records every query that was run so tests can assert on the request
    budget rather than only on the output."""
    calls = []

    def make(kind, factory):
        def search(query):
            calls.append((kind, query))
            return [factory(f"{query}-{i}") for i in range(3)]

        return search

    searchers = {
        "videos": make("videos", lambda seed: _video(seed)),
        "channels": make("channels", _channel),
        "playlists": make("playlists", _playlist),
    }
    monkeypatch.setattr(rec, "_SEARCHERS", searchers)
    return calls


def _set_interests(db_session, *interests):
    profile = db_session.get(User, USER_ID)
    profile.interests = "\n".join(interests)
    db_session.commit()
    return profile


def test_no_interests_means_no_interest_searches(client, db_session, fake_search):
    """The interest shelves stay empty and cost nothing. What a profile with
    no interests *does* get is the charts and the mood shelf, which don't
    come from the interest list — see the two tests below."""
    body = client.get("/recommendations").json()

    assert body["interests"] == []
    assert body["interests_used"] == []
    assert (body["videos"], body["channels"], body["playlists"]) == ([], [], [])
    assert fake_search == []


def test_a_profile_with_no_interests_still_gets_the_charts(client, db_session, fake_browse):
    """The case Explore used to answer with nothing but a nag."""
    fake_browse(charts=["top-40"], chart_artists=["UCchart"], mood={"title": "Chill", "section": "Moods & moments", "playlists": []})

    body = client.get("/recommendations").json()

    assert [p["playlist_id"] for p in body["charts"]] == ["top-40"]
    assert [c["channel_id"] for c in body["chart_artists"]] == ["UCchart"]
    assert body["mood"]["title"] == "Chill"
    assert body["generated_at"] is not None


def test_a_charting_artist_already_followed_is_dropped(client, db_session, fake_browse):
    """Same rule as the interest-based channels shelf: it's a list of
    channels to follow, and one already followed isn't."""
    fake_browse(chart_artists=["UCfollowed", "UCnew"])
    db_session.add(
        Feed(
            user_id=USER_ID,
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=UCfollowed",
            channel_title="Already Followed",
            followed=True,
        )
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [c["channel_id"] for c in body["chart_artists"]] == ["UCnew"]


def test_a_video_already_in_the_library_is_dropped_from_the_batch(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/already-owned-feed", channel_title="Owner")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    db_session.add(
        Content(feed_id=feed.id, user_id=USER_ID, video_id="jazz-1", title="Already have this")
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [v["video_id"] for v in body["videos"]] == ["jazz-0", "jazz-2"]


def test_a_followed_channel_is_dropped_from_the_batch(client, db_session, fake_search):
    """Observed live before this: a profile was recommended a channel it
    already followed."""
    _set_interests(db_session, "jazz")
    db_session.add(
        Feed(
            user_id=USER_ID,
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=jazz-0",
            channel_title="Already Followed",
            followed=True,
        )
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [c["channel_id"] for c in body["channels"]] == ["jazz-1", "jazz-2"]


def test_an_unfollowed_placeholder_feed_does_not_hide_a_channel(client, db_session, fake_search):
    """followed=False is an Explore placeholder (see routers/explore.py), not
    a real subscription — it must not suppress the recommendation the way an
    actually-followed channel does."""
    _set_interests(db_session, "jazz")
    db_session.add(
        Feed(
            user_id=USER_ID,
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=jazz-0",
            channel_title="Just A Preview",
            followed=False,
        )
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [c["channel_id"] for c in body["channels"]] == ["jazz-0", "jazz-1", "jazz-2"]


def test_the_library_filter_is_reapplied_on_every_read_even_from_cache(client, db_session, fake_search):
    """The filter can't be baked into the cached payload — build_batch's
    result is cached and reused across requests, but the library it's
    filtered against keeps changing (someone follows a recommended channel
    right after seeing it). Re-checking on every read is what makes that
    channel disappear on the very next load instead of waiting for the
    batch to expire and rebuild."""
    _set_interests(db_session, "jazz")
    client.get("/recommendations")  # builds and caches the batch
    fake_search.clear()

    db_session.add(
        Feed(
            user_id=USER_ID,
            rss_url="https://www.youtube.com/feeds/videos.xml?channel_id=jazz-0",
            channel_title="Followed After The Batch Was Built",
            followed=True,
        )
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert fake_search == []  # still served from cache, not rebuilt
    assert "jazz-0" not in [c["channel_id"] for c in body["channels"]]


def test_a_first_request_builds_a_batch_from_the_interests(client, db_session, fake_search):
    _set_interests(db_session, "jazz")

    body = client.get("/recommendations").json()

    assert body["interests"] == ["jazz"]
    assert body["interests_used"] == ["jazz"]
    assert body["generated_at"] is not None
    assert [v["video_id"] for v in body["videos"]] == ["jazz-0", "jazz-1", "jazz-2"]
    assert [c["channel_id"] for c in body["channels"]] == ["jazz-0", "jazz-1", "jazz-2"]
    assert [p["playlist_id"] for p in body["playlists"]] == ["jazz-0", "jazz-1", "jazz-2"]
    assert sorted(fake_search) == [("channels", "jazz"), ("playlists", "jazz"), ("videos", "jazz")]


def test_a_second_request_is_served_from_the_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz")

    first = client.get("/recommendations").json()
    fake_search.clear()
    second = client.get("/recommendations").json()

    assert second == first
    assert fake_search == []


def test_editing_the_interests_invalidates_the_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    client.put("/settings", json={"interests": ["funk"]})
    body = client.get("/recommendations").json()

    assert body["interests_used"] == ["funk"]
    assert [q for _, q in fake_search] == ["funk", "funk", "funk"]


def test_reordering_the_interests_does_not_invalidate_the_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz", "funk")
    client.get("/recommendations")
    fake_search.clear()

    client.put("/settings", json={"interests": ["funk", "jazz"]})
    client.get("/recommendations")

    assert fake_search == []


def test_a_batch_older_than_the_refresh_interval_is_rebuilt(client, db_session, fake_search):
    # The TTL is whatever Settings' feed-refresh interval is set to, rather
    # than a cadence of its own — see routers/recommendations.py.
    client.put("/settings", json={"feed_refresh_interval_minutes": 15})
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    cache = db_session.get(RecommendationCache, USER_ID)
    cache.generated_at = utcnow() - timedelta(minutes=16)
    db_session.commit()

    client.get("/recommendations")
    assert fake_search != []


def test_a_batch_inside_the_refresh_interval_is_not_rebuilt(client, db_session, fake_search):
    client.put("/settings", json={"feed_refresh_interval_minutes": 120})
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    cache = db_session.get(RecommendationCache, USER_ID)
    # Well past the 15-minute floor, comfortably inside the 2 hours actually
    # configured — a fixed TTL would have rebuilt here.
    cache.generated_at = utcnow() - timedelta(minutes=30)
    db_session.commit()

    client.get("/recommendations")
    assert fake_search == []


def test_refresh_rebuilds_even_when_the_cache_is_fresh(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    body = client.post("/recommendations/refresh").json()

    assert fake_search != []
    assert body["interests_used"] == ["jazz"]


def test_refresh_with_no_interests_runs_no_interest_searches(client, db_session, fake_search):
    body = client.post("/recommendations/refresh").json()

    assert body["interests_used"] == []
    assert fake_search == []


def test_only_a_sample_of_a_long_interest_list_is_searched(client, db_session, fake_search):
    _set_interests(db_session, *[f"tag{i}" for i in range(10)])

    body = client.get("/recommendations").json()

    assert len(body["interests_used"]) == rec.INTERESTS_PER_RUN
    assert set(body["interests_used"]) <= {f"tag{i}" for i in range(10)}
    # Three searches per sampled interest, and not one more — this is the
    # whole point of sampling.
    assert len(fake_search) == rec.INTERESTS_PER_RUN * 3
    # The full list is still reported, so Explore can say what it's working
    # from even though only a few of them were used.
    assert len(body["interests"]) == 10


def test_results_from_several_interests_are_interleaved(client, db_session, fake_search):
    _set_interests(db_session, "a", "b")

    videos = [v["video_id"] for v in client.get("/recommendations").json()["videos"]]

    # Round-robin, so the front of the shelf represents both interests rather
    # than exhausting the first.
    assert videos[:4] == ["a-0", "b-0", "a-1", "b-1"]


def test_a_result_two_interests_share_is_only_listed_once(client, db_session, monkeypatch):
    monkeypatch.setattr(
        rec,
        "_SEARCHERS",
        {
            "videos": lambda query: [_video("shared"), _video(f"{query}-own")],
            "channels": lambda query: [],
            "playlists": lambda query: [],
        },
    )
    _set_interests(db_session, "a", "b")

    videos = [v["video_id"] for v in client.get("/recommendations").json()["videos"]]

    assert videos == ["shared", "a-own", "b-own"]


def test_each_shelf_is_capped(client, db_session, monkeypatch):
    many = [_video(f"v{i}") for i in range(rec.RESULTS_PER_SHELF + 20)]
    monkeypatch.setattr(
        rec,
        "_SEARCHERS",
        {
            "videos": lambda query: many,
            "channels": lambda query: [],
            "playlists": lambda query: [],
        },
    )
    _set_interests(db_session, "jazz")

    assert len(client.get("/recommendations").json()["videos"]) == rec.RESULTS_PER_SHELF


def test_one_failing_search_does_not_sink_the_batch(client, db_session, monkeypatch):
    # search_* already flatten yt-dlp failures to an empty list (see
    # youtube/search.py) — this asserts the batch treats that as "this shelf
    # is empty", not as an error.
    monkeypatch.setattr(
        rec,
        "_SEARCHERS",
        {
            "videos": lambda query: [_video("v1")],
            "channels": lambda query: [],
            "playlists": lambda query: [_playlist("p1")],
        },
    )
    _set_interests(db_session, "jazz")

    body = client.get("/recommendations").json()

    assert [v["video_id"] for v in body["videos"]] == ["v1"]
    assert body["channels"] == []
    assert [p["playlist_id"] for p in body["playlists"]] == ["p1"]


def test_a_corrupt_cached_payload_is_treated_as_a_miss(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    cache = db_session.get(RecommendationCache, USER_ID)
    cache.payload = "{not json"
    db_session.commit()

    assert client.get("/recommendations").json()["interests_used"] == ["jazz"]
    assert fake_search != []


def test_each_profile_gets_its_own_batch(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")

    other = client.post("/profiles", json={"name": "Second"}).json()
    client.post(f"/profiles/{other['id']}/switch")
    client.put("/settings", json={"interests": ["funk"]})

    assert client.get("/recommendations").json()["interests_used"] == ["funk"]


def test_deleting_a_profile_takes_its_cached_batch_with_it(client, db_session, fake_search):
    other = client.post("/profiles", json={"name": "Second"}).json()
    client.post(f"/profiles/{other['id']}/switch")
    client.put("/settings", json={"interests": ["funk"]})
    client.get("/recommendations")
    assert db_session.get(RecommendationCache, other["id"]) is not None

    client.post(f"/profiles/{USER_ID}/switch")
    assert client.delete(f"/profiles/{other['id']}").status_code == 204

    db_session.expire_all()
    assert db_session.get(RecommendationCache, other["id"]) is None


def test_recommendation_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get("/recommendations", follow_redirects=False).status_code == 303
        assert anonymous.post("/recommendations/refresh", follow_redirects=False).status_code == 303
