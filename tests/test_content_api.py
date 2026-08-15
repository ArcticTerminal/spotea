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


def test_restart_download_is_a_noop_once_ready(client, db_session, monkeypatch):
    from app.routers import content as content_router

    item = _seed_one(db_session, status="ready", file_path="/nonexistent.m4a")
    called = []
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: called.append(1))

    res = client.post(f"/content/{item.id}/download/restart")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"
    assert not called  # no attempt dispatched at all


def test_restart_download_does_not_409_while_already_downloading(client, db_session, monkeypatch, tmp_path):
    """Unlike POST /download, this is exactly for the case where a download
    is already running — that's the whole point (see player.js's stall
    watchdog)."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="downloading")
    fake_file = tmp_path / f"{item.video_id}.m4a"
    fake_file.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: fake_file)

    res = client.post(f"/content/{item.id}/download/restart")
    assert res.status_code == 200

    db_session.refresh(item)
    assert item.status == "ready"


def test_a_superseded_generations_result_never_touches_the_row(client, db_session, monkeypatch, tmp_path):
    """The core of the stall-restart feature: once a restart bumps the
    generation, whatever the abandoned attempt does later — even succeeding
    — must not reach the DB, since a fresher attempt now owns the row."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="downloading")
    fake_file = tmp_path / f"{item.video_id}.m4a"
    fake_file.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: fake_file)

    stale_generation = content_router._next_generation(item.id)
    current_generation = content_router._next_generation(item.id)

    # The stale attempt "finishes" (successfully!) after being superseded.
    content_router._run_download(item.id, item.video_id, "high", stale_generation)
    db_session.refresh(item)
    assert item.status == "downloading"
    assert item.file_path is None

    # The attempt that's actually current still writes normally.
    content_router._run_download(item.id, item.video_id, "high", current_generation)
    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(fake_file)


def test_a_superseded_generations_failure_is_also_ignored(client, db_session, monkeypatch):
    from app.downloader import DownloadError
    from app.routers import content as content_router

    item = _seed_one(db_session, status="downloading")

    def fail(*a, **k):
        raise DownloadError("boom")

    monkeypatch.setattr(content_router, "download_audio", fail)

    stale_generation = content_router._next_generation(item.id)
    content_router._next_generation(item.id)  # a newer attempt supersedes it

    content_router._run_download(item.id, item.video_id, "high", stale_generation)

    db_session.refresh(item)
    assert item.status == "downloading"  # not downgraded to "error"
    assert item.error_message is None


def test_download_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for path in ["/content/1/download", "/content/1/download/restart"]:
            res = anonymous.post(path, follow_redirects=False)
            assert res.status_code == 303
            assert res.headers["location"] == "/login"
