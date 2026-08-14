"""collect_usage and the /storage endpoints.

Sizes moved from "stat the file on every call" to "read Content.file_size_bytes,
recorded once at download time" — these pin down both halves of that: the
stored value is what's reported, and a row from before the column existed
still gets measured (once) rather than reporting 0 forever.
"""

from app.models import Content, Feed
from app.storage import clear_all, collect_usage

USER_ID = 1


def _ready_content(db_session, tmp_path, *, video_id, size_bytes, stored_size=None):
    feed = (
        db_session.query(Feed).filter(Feed.user_id == USER_ID).first()
        or Feed(user_id=USER_ID, rss_url="https://example.com/feed", channel_title="Storage Channel")
    )
    if feed.id is None:
        db_session.add(feed)
        db_session.commit()
        db_session.refresh(feed)

    audio = tmp_path / f"{video_id}.m4a"
    audio.write_bytes(b"x" * size_bytes)

    content = Content(
        feed_id=feed.id,
        user_id=USER_ID,
        video_id=video_id,
        title=f"Track {video_id}",
        status="ready",
        file_path=str(audio),
        file_size_bytes=stored_size,
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return content, audio


def test_usage_reports_the_stored_size(db_session, tmp_path):
    _ready_content(db_session, tmp_path, video_id="stored00001", size_bytes=10, stored_size=4096)

    usage = collect_usage(db_session, USER_ID)

    assert usage.count == 1
    # The stored value wins over what's actually on disk — proof this no
    # longer stats the file on every call.
    assert usage.total_bytes == 4096


def test_unmeasured_rows_are_backfilled_from_disk_once(db_session, tmp_path):
    """Rows downloaded before file_size_bytes existed have NULL; the first
    collect_usage measures them and writes the result back."""
    content, _audio = _ready_content(
        db_session, tmp_path, video_id="legacy00001", size_bytes=2048, stored_size=None
    )

    usage = collect_usage(db_session, USER_ID)

    assert usage.total_bytes == 2048
    db_session.refresh(content)
    assert content.file_size_bytes == 2048


def test_a_missing_file_backfills_as_zero_rather_than_failing(db_session, tmp_path):
    content, audio = _ready_content(
        db_session, tmp_path, video_id="vanished001", size_bytes=512, stored_size=None
    )
    audio.unlink()

    usage = collect_usage(db_session, USER_ID)

    assert usage.total_bytes == 0
    db_session.refresh(content)
    assert content.file_size_bytes == 0


def test_totals_add_up_across_rows(db_session, tmp_path):
    _ready_content(db_session, tmp_path, video_id="multi000001", size_bytes=1, stored_size=1000)
    _ready_content(db_session, tmp_path, video_id="multi000002", size_bytes=1, stored_size=2000)

    usage = collect_usage(db_session, USER_ID)

    assert usage.count == 2
    assert usage.total_bytes == 3000


def test_clear_all_resets_the_stored_size_too(db_session, tmp_path):
    """A stale size left behind would make the next collect_usage skip its
    own backfill and report a size for a row with no file at all."""
    content, audio = _ready_content(
        db_session, tmp_path, video_id="cleared0001", size_bytes=64, stored_size=64
    )

    cleared = clear_all(db_session, USER_ID)

    assert cleared == 1
    assert not audio.exists()
    db_session.refresh(content)
    assert content.status == "not_downloaded"
    assert content.file_path is None
    assert content.file_size_bytes is None
    assert collect_usage(db_session, USER_ID).total_bytes == 0


def test_delete_endpoint_resets_the_stored_size_too(client, db_session, tmp_path):
    content, audio = _ready_content(
        db_session, tmp_path, video_id="deleted0001", size_bytes=64, stored_size=64
    )

    res = client.delete(f"/content/{content.id}")

    assert res.status_code == 200
    assert not audio.exists()
    db_session.refresh(content)
    assert content.file_size_bytes is None


def test_storage_endpoint_shape(client, db_session, tmp_path):
    content, _audio = _ready_content(
        db_session, tmp_path, video_id="endpoint001", size_bytes=1, stored_size=2 * 1024 * 1024
    )

    res = client.get("/storage")

    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    assert data["total_formatted"] == "2.0 MB"
    assert data["items"][0]["id"] == content.id
    assert data["items"][0]["channel_title"] == "Storage Channel"
