from datetime import datetime, timedelta

from app.models import Content, Feed, User

USER_ID = 1
# Must match conftest.py's own DEFAULT_ACCOUNT_ID (duplicated rather than
# imported — see test_profiles_api.py for why).
DEFAULT_ACCOUNT_ID = 1


def _seed(db_session, count=25):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/feed", channel_title="Test Channel")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    now = datetime(2026, 1, 1)
    items = [
        Content(
            feed_id=feed.id,
            user_id=USER_ID,
            video_id=f"vid{i:04d}"[:11],
            title=f"Title {count - i:03d}",
            published_at=now - timedelta(days=i),
            duration_seconds=120 + i,
            is_favorite=(i == 0),
            status="not_downloaded",
        )
        for i in range(count)
    ]
    db_session.add_all(items)
    db_session.commit()
    return feed, items


def test_get_content_default_page_shape(client, db_session):
    _seed(db_session, count=25)

    res = client.get("/content")
    assert res.status_code == 200

    body = res.json()
    assert set(body.keys()) == {"items", "page", "total_pages"}
    assert body["page"] == 1
    assert body["total_pages"] == 2
    assert len(body["items"]) == 20

    first = body["items"][0]
    assert set(first.keys()) == {
        "id",
        "feed_id",
        "channel_title",
        "video_id",
        "title",
        "thumbnail_url",
        "duration_seconds",
        "published_at",
        "status",
        "added_at",
        "is_favorite",
        "is_saved",
        "is_played",
        "is_unavailable",
    }
    assert first["channel_title"] == "Test Channel"
    assert first["title"] == "Title 025"


def test_get_content_out_of_range_page_clamps_instead_of_erroring(client, db_session):
    _seed(db_session, count=5)

    res = client.get("/content", params={"page": 999})
    assert res.status_code == 200

    body = res.json()
    assert body["page"] == 1
    assert body["total_pages"] == 1
    assert len(body["items"]) == 5


def test_get_content_favorites_filter(client, db_session):
    _seed(db_session, count=25)

    res = client.get("/content", params={"filter": "__favorites__"})
    assert res.status_code == 200

    body = res.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["is_favorite"] is True


def test_get_content_channel_filter(client, db_session):
    _seed(db_session, count=5)

    res = client.get("/content", params={"filter": "Test Channel"})
    assert res.status_code == 200
    assert len(res.json()["items"]) == 5

    res = client.get("/content", params={"filter": "Nonexistent Channel"})
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_get_single_content_returns_full_shape(client, db_session):
    _feed, items = _seed(db_session, count=3)

    res = client.get(f"/content/{items[0].id}")
    assert res.status_code == 200

    body = res.json()
    assert set(body.keys()) == {
        "id",
        "feed_id",
        "channel_title",
        "video_id",
        "title",
        "thumbnail_url",
        "duration_seconds",
        "published_at",
        "status",
        "added_at",
        "is_favorite",
        "is_saved",
        "is_played",
        "is_unavailable",
    }
    assert body["id"] == items[0].id
    assert body["channel_title"] == "Test Channel"


def test_get_single_content_404_for_nonexistent_id(client, db_session):
    res = client.get("/content/999999")
    assert res.status_code == 404


def test_get_single_content_404_for_another_users_content(client, db_session):
    other_profile = User(name="Music", account_id=DEFAULT_ACCOUNT_ID)
    db_session.add(other_profile)
    db_session.commit()
    db_session.refresh(other_profile)

    other_feed = Feed(user_id=other_profile.id, rss_url="https://example.com/other", channel_title="Other")
    db_session.add(other_feed)
    db_session.commit()
    db_session.refresh(other_feed)

    other_content = Content(
        feed_id=other_feed.id, user_id=other_profile.id, video_id="otheruser01", title="Not yours"
    )
    db_session.add(other_content)
    db_session.commit()
    db_session.refresh(other_content)

    res = client.get(f"/content/{other_content.id}")
    assert res.status_code == 404


def test_channel_queue_is_the_whole_channel_in_list_order(client, db_session):
    """The queue behind "Play all" deliberately ignores pagination — the
    detail panel shows twenty rows at a time, but playing a channel means the
    channel."""
    feed, _ = _seed(db_session, count=25)

    ids = client.get(f"/content/queue/channel/{feed.id}").json()["ids"]
    assert len(ids) == 25

    # Same order the track list renders, newest-first — a queue that agreed
    # on the set but not the order would look like shuffle was stuck on.
    listed = [item["id"] for page in (1, 2) for item in client.get(f"/content?page={page}").json()["items"]]
    assert ids == listed


def test_playlist_queue_matches_its_detail_panel(client, db_session):
    feed, items = _seed(db_session, count=25)
    ids = client.get("/content/queue/playlist/favorites").json()["ids"]
    # _seed favorites exactly one row (i == 0, the newest).
    assert ids == [item["id"] for item in client.get("/content?filter=__favorites__").json()["items"]]
    assert len(ids) == 1


def test_queue_endpoints_404_for_unknown_targets(client, db_session):
    assert client.get("/content/queue/channel/9999").status_code == 404
    assert client.get("/content/queue/playlist/bogus").status_code == 404


def test_channel_queue_404s_for_another_profiles_channel(client, db_session):
    other_profile = User(name="Music", account_id=DEFAULT_ACCOUNT_ID)
    db_session.add(other_profile)
    db_session.commit()
    db_session.refresh(other_profile)

    other_feed = Feed(user_id=other_profile.id, rss_url="https://example.com/queue-other")
    db_session.add(other_feed)
    db_session.commit()
    db_session.refresh(other_feed)

    assert client.get(f"/content/queue/channel/{other_feed.id}").status_code == 404


def test_queue_is_capped(client, db_session, monkeypatch):
    """A backfilled channel can be thousands of videos deep; the queue stops
    well before that rather than shipping (and storing) all of it."""
    from app import content_query

    monkeypatch.setattr(content_query, "QUEUE_MAX_ITEMS", 5)
    feed, _ = _seed(db_session, count=25)

    assert len(client.get(f"/content/queue/channel/{feed.id}").json()["ids"]) == 5


def _seed_one(db_session, **overrides):
    feed = Feed(user_id=USER_ID, rss_url="https://example.com/dl-feed", channel_title="Download Channel")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    fields = {"status": "not_downloaded", **overrides}
    item = Content(
        feed_id=feed.id, user_id=USER_ID, video_id="downloadvi1", title="Download Me", **fields
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def test_start_download_dispatches_and_settles_to_ready(client, db_session, monkeypatch, tmp_path):
    from app.routers import content as content_router

    item = _seed_one(db_session)
    fake_file = tmp_path / f"{item.video_id}.m4a"
    fake_file.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: fake_file)

    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 200
    # TestClient runs BackgroundTasks synchronously right after the response
    # is built, so the response body still reflects the pre-task state —
    # this is the same "status flips synchronously, the actual work happens
    # after" contract the real deployment relies on (see ARCHITECTURE.md §5).
    assert res.json()["status"] == "downloading"

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(fake_file)


def test_start_download_409s_while_already_downloading(client, db_session):
    item = _seed_one(db_session, status="downloading")
    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 409


def test_start_download_leaves_a_track_thats_already_on_disk_alone(client, db_session, monkeypatch, tmp_path):
    """The queue's one-track-ahead prefetch (home/overlay.js) fires without
    knowing the next track's status, so asking for something already
    downloaded has to be free — not a second fetch of the same audio."""
    from app.routers import content as content_router

    on_disk = tmp_path / "already-here.m4a"
    on_disk.write_bytes(b"audio")
    item = _seed_one(db_session, status="ready", file_path=str(on_disk))

    def fail(*args, **kwargs):
        raise AssertionError("re-downloaded a track that was already on disk")

    monkeypatch.setattr(content_router, "download_audio", fail)

    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(on_disk)


def test_start_download_refetches_when_the_file_is_gone(client, db_session, monkeypatch, tmp_path):
    """"ready" taken at face value would strand playback whenever storage was
    cleared out from under the row."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="ready", file_path=str(tmp_path / "vanished.m4a"))
    replacement = tmp_path / f"{item.video_id}.m4a"
    replacement.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: replacement)

    assert client.post(f"/content/{item.id}/download").json()["status"] == "downloading"

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(replacement)


def test_a_video_youtube_wont_serve_is_recorded_as_settled(client, db_session, monkeypatch):
    """Distinguishing this from an ordinary failure is what stops the app
    re-attempting it forever — see Content.is_unavailable."""
    from app.routers import content as content_router

    item = _seed_one(db_session)

    def unavailable(*args, **kwargs):
        raise content_router.VideoUnavailableError("ERROR: [youtube] x: Video unavailable")

    monkeypatch.setattr(content_router, "download_audio", unavailable)
    client.post(f"/content/{item.id}/download")

    db_session.refresh(item)
    assert item.status == "error"
    assert item.is_unavailable is True
    assert client.get(f"/content/{item.id}/status").json()["is_unavailable"] is True


def test_an_ordinary_failure_stays_worth_retrying(client, db_session, monkeypatch):
    from app.routers import content as content_router

    item = _seed_one(db_session)

    def refused(*args, **kwargs):
        raise content_router.DownloadError("ERROR: HTTP Error 403: Forbidden")

    monkeypatch.setattr(content_router, "download_audio", refused)
    client.post(f"/content/{item.id}/download")

    db_session.refresh(item)
    assert item.status == "error"
    assert item.is_unavailable is False


def test_an_unavailable_track_is_never_attempted_again(client, db_session, monkeypatch):
    """The queue's prefetch fires for whatever is next without knowing
    anything about it, so without this every pass over a broken track pays
    for a full extraction against YouTube to be told what the row already
    says."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="error")
    item.is_unavailable = True
    db_session.commit()

    def fail(*args, **kwargs):
        raise AssertionError("re-attempted a track YouTube has already refused")

    monkeypatch.setattr(content_router, "download_audio", fail)

    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 200
    assert res.json()["is_unavailable"] is True

    db_session.refresh(item)
    assert item.status == "error"


def test_removing_a_download_reopens_an_unavailable_track(client, db_session):
    """YouTube licensing changes, so writing a track off has to be
    reversible — this is the only "start over" action the app has."""
    item = _seed_one(db_session, status="error")
    item.is_unavailable = True
    db_session.commit()

    client.delete(f"/content/{item.id}")

    db_session.refresh(item)
    assert item.is_unavailable is False
    assert item.status == "not_downloaded"


def test_there_is_no_restart_endpoint(client, db_session):
    """The client used to POST this every 3s while a download showed no byte
    progress, which restarted attempts that were working and left the
    abandoned ones running. Retrying is downloader.py's ladder's job now, and
    nothing should be able to dispatch a second concurrent attempt for one
    row — two yt-dlp runs writing the same .part file is how a real play died
    on "Unable to rename file"."""
    item = _seed_one(db_session, status="downloading")
    res = client.post(f"/content/{item.id}/download/restart")
    assert res.status_code == 404


def test_the_extracting_phase_reaches_the_status_endpoint(client, db_session, monkeypatch, tmp_path):
    """Resolving a URL YouTube will honour is the slow part of a play and
    moves no bytes, so the phase downloader.py reports during it has to make
    it out to the poller — otherwise the UI sits on "Preparing audio…" for
    the entire wait."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="downloading")
    fake_file = tmp_path / f"{item.video_id}.m4a"
    fake_file.write_bytes(b"audio")

    def fake_download(video_id, quality="high", on_progress=None):
        on_progress("extracting", None)
        assert client.get(f"/content/{item.id}/status").json()["phase"] == "extracting"
        return fake_file

    monkeypatch.setattr(content_router, "download_audio", fake_download)
    content_router._run_download(item.id, item.video_id, "high")

    # ...and it's cleared once the download settles, rather than leaving a
    # finished track reporting a phase forever.
    assert client.get(f"/content/{item.id}/status").json()["phase"] is None


def test_download_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.post("/content/1/download", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/login"


def _seed_ready(db_session, tmp_path):
    audio = tmp_path / "track.m4a"
    audio.write_bytes(b"audio")
    return _seed_one(db_session, status="ready", file_path=str(audio))


def test_streaming_a_track_records_it_as_played(client, db_session, tmp_path):
    item = _seed_ready(db_session, tmp_path)
    assert item.last_played_at is None

    assert client.get(f"/content/{item.id}/stream").status_code == 200

    db_session.refresh(item)
    assert item.last_played_at is not None


def test_preloading_the_next_track_is_not_a_play(client, db_session, tmp_path):
    """The player reads the next queued track ahead of time, while the current
    one is still playing, so that the handoff needs no network (player.js's
    deck helpers). Counting that as listening would put a track the user may
    well skip past into Recently Played a minute before it ever starts."""
    item = _seed_ready(db_session, tmp_path)

    assert client.get(f"/content/{item.id}/stream?preload=1").status_code == 200

    db_session.refresh(item)
    assert item.last_played_at is None


def test_a_preloaded_track_is_recorded_once_it_really_starts(client, db_session, tmp_path):
    item = _seed_ready(db_session, tmp_path)
    client.get(f"/content/{item.id}/stream?preload=1")

    assert client.post(f"/content/{item.id}/played").status_code == 204

    db_session.refresh(item)
    assert item.last_played_at is not None


def test_marking_another_profiles_track_as_played_404s(client, db_session, tmp_path):
    other = User(name="Someone Else", account_id=DEFAULT_ACCOUNT_ID)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    audio = tmp_path / "theirs.m4a"
    audio.write_bytes(b"audio")
    feed = Feed(user_id=other.id, rss_url="https://example.com/theirs", channel_title="Theirs")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    theirs = Content(
        feed_id=feed.id,
        user_id=other.id,
        video_id="theirsvid1",
        title="Theirs",
        status="ready",
        file_path=str(audio),
    )
    db_session.add(theirs)
    db_session.commit()

    assert client.post(f"/content/{theirs.id}/played").status_code == 404
