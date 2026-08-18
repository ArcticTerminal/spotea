"""Disk cleanup (app/storage.py's sweep_orphans, sweep_startup_leftovers,
sweep_stale_previews) and the thumbnail half of delete_files_for_profile.

Measured live before any of this existed: thumbnails 1237 files/22MB with 22
orphans; avatars 1060 files/25MB with 977 orphans (92%, 16.4MB); one orphaned
.part file at 40.6MB; 491 stale preview rows; 217 followed=0 feeds (75% of
all feeds) with nothing ever cleaning any of it up. Every test here pins one
piece of that down.
"""

import os
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.config import settings
from app.models import Content, Feed, User
from app.storage import (
    PREVIEW_RETENTION,
    STALE_EXPORT_AGE,
    delete_files_for_profile,
    sweep_orphans,
    sweep_stale_previews,
    sweep_startup_leftovers,
)
from app.timeutil import utcnow

USER_ID = 1


def _feed(db_session, rss_url, **kwargs):
    feed = Feed(user_id=USER_ID, rss_url=rss_url, channel_title="Lifecycle Channel", **kwargs)
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)
    return feed


def _feed_row_exists(db_session, feed_id) -> bool:
    """Not db_session.get(Feed, feed_id): a prior sweep_stale_previews commit
    in the same session expires every loaded object, and a bulk `.delete()`
    is synchronize_session=False (it never marks the in-memory Feed as
    gone) — so .get() tries to refresh an object whose row is already gone
    and raises ObjectDeletedError instead of just returning None. A fresh
    query sidesteps the identity map entirely."""
    return db_session.query(Feed).filter(Feed.id == feed_id).first() is not None


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """settings.storage_dir/thumbnails_dir/avatars_dir point at one directory
    shared by the whole test session (see conftest.py) — fine for tests that
    only check a specific file, but these ones glob and count *everything* in
    each directory, so a previous test's leftovers would silently inflate the
    numbers. Redirected to a fresh tmp_path per test instead."""
    storage_dir = tmp_path / "storage"
    thumbnails_dir = tmp_path / "thumbnails"
    avatars_dir = tmp_path / "avatars"
    for directory in (storage_dir, thumbnails_dir, avatars_dir):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_dir", storage_dir)
    monkeypatch.setattr(settings, "thumbnails_dir", thumbnails_dir)
    monkeypatch.setattr(settings, "avatars_dir", avatars_dir)


# ---------------------------------------------------------------- sweep_orphans


def test_sweep_orphans_removes_audio_no_row_points_at(db_session):
    orphan = settings.storage_dir / "orphan0001.m4a"
    orphan.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not orphan.exists()


def test_sweep_orphans_keeps_audio_a_row_still_references(db_session):
    feed = _feed(db_session, "https://example.com/orphan-audio-kept")
    referenced = settings.storage_dir / "kept00001.m4a"
    referenced.write_bytes(b"x")
    db_session.add(
        Content(
            feed_id=feed.id, user_id=USER_ID, video_id="kept00001", title="Kept",
            status="ready", file_path=str(referenced),
        )
    )
    db_session.commit()

    sweep_orphans(db_session)

    assert referenced.exists()


def test_sweep_orphans_removes_thumbnails_no_row_points_at(db_session):
    orphan = settings.thumbnails_dir / "orphanthumb1.jpg"
    orphan.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not orphan.exists()


def test_sweep_orphans_keeps_a_thumbnail_any_row_still_references(db_session):
    """Keyed by video_id alone (see unlink_thumbnail_if_unshared) — a row
    doesn't need to be downloaded, favorited, or anything else to keep its
    thumbnail; just existing is enough."""
    feed = _feed(db_session, "https://example.com/orphan-thumb-kept")
    kept = settings.thumbnails_dir / "keptthumb01.jpg"
    kept.write_bytes(b"x")
    db_session.add(
        Content(feed_id=feed.id, user_id=USER_ID, video_id="keptthumb01", title="Kept")
    )
    db_session.commit()

    sweep_orphans(db_session)

    assert kept.exists()


def test_sweep_orphans_removes_avatars_no_feed_points_at(db_session):
    """The actual bug: 977 of 1060 avatar files (92%) were exactly this."""
    orphan = settings.avatars_dir / "UCorphanavatar00000000.jpg"
    orphan.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not orphan.exists()


def test_sweep_orphans_keeps_an_avatar_a_followed_feed_references(db_session):
    channel_id = "UCkeptavatar000000000000"
    kept = settings.avatars_dir / f"{channel_id}.jpg"
    kept.write_bytes(b"x")
    _feed(
        db_session, f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        avatar_url=f"/avatars/{channel_id}.jpg",
    )

    sweep_orphans(db_session)

    assert kept.exists()


def test_sweep_orphans_leaves_a_fresh_export_temp_file_alone(db_session):
    """A live export could be mid-write at the exact moment this runs — see
    STALE_EXPORT_AGE. A fresh file must survive."""
    fresh = settings.storage_dir / "abc123.export.tmp"
    fresh.write_bytes(b"x")

    sweep_orphans(db_session)

    assert fresh.exists()


def test_sweep_orphans_removes_an_abandoned_export_temp_file(db_session):
    """Older than STALE_EXPORT_AGE means the request that made it can only
    have been interrupted (a container killed mid-export) — nothing else
    leaves one behind that long."""
    stale = settings.storage_dir / "abandoned1.export.tmp"
    stale.write_bytes(b"x")
    old_time = (utcnow() - STALE_EXPORT_AGE - timedelta(minutes=5)).timestamp()
    os.utime(stale, (old_time, old_time))

    sweep_orphans(db_session)

    assert not stale.exists()


def test_sweep_orphans_never_touches_part_files(db_session):
    """A .part sweep here would risk deleting a real, in-progress download —
    see the module docstring on why that cleanup lives at startup instead."""
    part = settings.storage_dir / "inprogress1.part"
    part.write_bytes(b"x")

    sweep_orphans(db_session)

    assert part.exists()


# ------------------------------------------------------- sweep_startup_leftovers


def test_sweep_startup_leftovers_removes_every_part_file(db_session):
    part_one = settings.storage_dir / "leftover001.part"
    part_two = settings.storage_dir / "leftover002.part"
    part_one.write_bytes(b"x")
    part_two.write_bytes(b"x")
    unrelated = settings.storage_dir / "real00001.m4a"
    unrelated.write_bytes(b"x")

    removed = sweep_startup_leftovers()

    assert removed == 2
    assert not part_one.exists()
    assert not part_two.exists()
    assert unrelated.exists()


# ------------------------------------------------------------ sweep_stale_previews


def _preview(db_session, feed, video_id, *, age_days, **kwargs):
    defaults = {
        "is_preview": True,
        "added_at": utcnow() - timedelta(days=age_days),
    }
    defaults.update(kwargs)
    content = Content(feed_id=feed.id, user_id=USER_ID, video_id=video_id, title=video_id, **defaults)
    db_session.add(content)
    db_session.commit()
    return content


def test_an_old_untouched_preview_is_removed(db_session):
    feed = _feed(db_session, "https://example.com/stale-preview", followed=False)
    _preview(db_session, feed, "stalepreview1", age_days=PREVIEW_RETENTION.days + 1)

    removed = sweep_stale_previews(db_session)

    assert removed == 1
    assert db_session.query(Content).filter(Content.video_id == "stalepreview1").first() is None


def test_a_recent_preview_is_kept(db_session):
    feed = _feed(db_session, "https://example.com/fresh-preview", followed=False)
    _preview(db_session, feed, "freshpreview1", age_days=1)

    removed = sweep_stale_previews(db_session)

    assert removed == 0
    assert db_session.query(Content).filter(Content.video_id == "freshpreview1").first() is not None


def test_an_old_but_played_preview_is_kept(db_session):
    feed = _feed(db_session, "https://example.com/played-preview", followed=False)
    _preview(
        db_session, feed, "playedpreview1",
        age_days=PREVIEW_RETENTION.days + 1, last_played_at=utcnow(),
    )

    removed = sweep_stale_previews(db_session)

    assert removed == 0


def test_an_old_but_favorited_preview_is_kept(db_session):
    feed = _feed(db_session, "https://example.com/fav-preview", followed=False)
    _preview(db_session, feed, "favpreview001", age_days=PREVIEW_RETENTION.days + 1, is_favorite=True)

    removed = sweep_stale_previews(db_session)

    assert removed == 0


def test_an_old_but_saved_preview_is_kept(db_session):
    feed = _feed(db_session, "https://example.com/saved-preview", followed=False)
    _preview(db_session, feed, "savedpreview1", age_days=PREVIEW_RETENTION.days + 1, is_saved=True)

    removed = sweep_stale_previews(db_session)

    assert removed == 0


def test_an_old_but_downloaded_preview_is_kept(db_session, tmp_path):
    feed = _feed(db_session, "https://example.com/dl-preview", followed=False)
    audio = tmp_path / "dlpreview001.m4a"
    audio.write_bytes(b"x")
    _preview(
        db_session, feed, "dlpreview0001",
        age_days=PREVIEW_RETENTION.days + 1, status="ready", file_path=str(audio),
    )

    removed = sweep_stale_previews(db_session)

    assert removed == 0
    assert audio.exists()


def test_a_placeholder_feed_left_empty_by_the_sweep_is_also_removed(db_session):
    """The other half: 217 followed=0 feeds (75% of all feeds) accumulated
    forever with nothing cleaning them up either."""
    feed = _feed(db_session, "https://example.com/emptied-placeholder", followed=False)
    feed_id = feed.id  # captured before the sweep — see _feed_row_exists' docstring
    _preview(db_session, feed, "emptyplaceh1", age_days=PREVIEW_RETENTION.days + 1)

    sweep_stale_previews(db_session)

    assert not _feed_row_exists(db_session, feed_id)


def test_a_followed_feed_is_never_removed_even_if_emptied(db_session):
    """followed=True is a real subscription, not a placeholder — emptying its
    content (a followed channel whose only content was an old preview,
    unlikely but possible) must not delete the feed itself."""
    feed = _feed(db_session, "https://example.com/followed-not-removed", followed=True)
    feed_id = feed.id
    _preview(db_session, feed, "followedpre1", age_days=PREVIEW_RETENTION.days + 1)

    sweep_stale_previews(db_session)

    assert _feed_row_exists(db_session, feed_id)


def test_a_placeholder_feed_with_other_content_left_is_not_removed(db_session):
    feed = _feed(db_session, "https://example.com/placeholder-not-empty", followed=False)
    feed_id = feed.id
    _preview(db_session, feed, "sweptaway001", age_days=PREVIEW_RETENTION.days + 1)
    db_session.add(
        Content(feed_id=feed.id, user_id=USER_ID, video_id="stayingsafe1", title="Stays", is_favorite=True)
    )
    db_session.commit()

    sweep_stale_previews(db_session)

    assert _feed_row_exists(db_session, feed_id)


# ----------------------------------------------------- delete_files_for_profile


def test_delete_files_for_profile_removes_thumbnails_too(db_session):
    """The actual bug: this used to unlink only downloaded audio, so a
    deleted profile's thumbnails — cached independent of download status —
    were orphaned forever once the ORM cascade removed the Content rows
    (which never calls purge_content)."""
    feed = _feed(db_session, "https://example.com/profile-delete-thumb")
    thumbnail = settings.thumbnails_dir / "profiledel01.jpg"
    thumbnail.write_bytes(b"x")
    db_session.add(
        Content(feed_id=feed.id, user_id=USER_ID, video_id="profiledel01", title="Not even downloaded")
    )
    db_session.commit()

    delete_files_for_profile(db_session, USER_ID)

    assert not thumbnail.exists()


def test_delete_files_for_profile_still_removes_downloaded_audio(db_session, tmp_path):
    feed = _feed(db_session, "https://example.com/profile-delete-audio")
    audio = tmp_path / "profiledelaudio.m4a"
    audio.write_bytes(b"x")
    db_session.add(
        Content(
            feed_id=feed.id, user_id=USER_ID, video_id="profiledelau1", title="Downloaded",
            status="ready", file_path=str(audio),
        )
    )
    db_session.commit()

    delete_files_for_profile(db_session, USER_ID)

    assert not audio.exists()


def test_delete_files_for_profile_keeps_what_another_profile_still_uses(db_session, tmp_path):
    """Storage is keyed by video id alone, so two profiles that both follow an
    overlapping channel share one physical file and one cached thumbnail.
    Deleting either profile must leave both behind — this used to be asked
    per row ("is any row other than *this* one pointing at it?") and is now
    asked once for the whole profile ("what do the surviving rows still
    reference?"), which has to come out the same way."""
    other_profile = User(id=USER_ID + 50, name="Keeps Them", account_id=1)
    db_session.add(other_profile)
    feed = _feed(db_session, "https://example.com/profile-delete-shared")
    other_feed = Feed(
        user_id=other_profile.id,
        rss_url="https://example.com/profile-delete-shared-other",
        channel_title="Lifecycle Channel",
    )
    db_session.add(other_feed)
    db_session.commit()

    audio = tmp_path / "shareddel.m4a"
    audio.write_bytes(b"x")
    thumbnail = settings.thumbnails_dir / "shareddel001.jpg"
    thumbnail.write_bytes(b"x")
    for user_id, feed_id in ((USER_ID, feed.id), (other_profile.id, other_feed.id)):
        db_session.add(
            Content(
                feed_id=feed_id, user_id=user_id, video_id="shareddel001", title="Shared",
                status="ready", file_path=str(audio),
            )
        )
    db_session.commit()

    delete_files_for_profile(db_session, USER_ID)

    assert audio.exists(), "unlinked a file the other profile is still playing"
    assert thumbnail.exists(), "unlinked a thumbnail the other profile still shows"


def test_delete_files_for_profile_cost_does_not_grow_with_the_library(db_session):
    """It used to run two queries per content row. On the real 28,866-row
    profile that was 28,869 queries and 11.8 seconds — with the profile still
    sitting in the Manage profiles modal for all of it, since the request
    hadn't come back. The sharing question is asked once now, so the query
    count is flat and only the unlink syscalls scale."""
    feed = _feed(db_session, "https://example.com/profile-delete-cost")
    db_session.add_all(
        Content(feed_id=feed.id, user_id=USER_ID, video_id=f"costrow{index:05d}", title="Row")
        for index in range(50)
    )
    db_session.commit()

    statements: list[str] = []

    @event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    try:
        delete_files_for_profile(db_session, USER_ID)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", record)

    assert len(statements) <= 5, (
        f"delete_files_for_profile is back to querying per row: {len(statements)} "
        "statements for 50 rows"
    )


# ------------------------------------------------------------------ wired at startup


def test_sweep_startup_leftovers_runs_during_app_startup(monkeypatch):
    """Not just that the function works (see above) — that it's actually
    called, exactly once, as part of main.py's lifespan. A fresh TestClient
    re-runs ASGI startup/shutdown independent of any other TestClient
    already used this session (see conftest.py's own `with TestClient(app):
    pass`), so this doesn't need its own app instance to prove it."""
    from fastapi.testclient import TestClient

    import app.main as main_module

    calls = []
    monkeypatch.setattr(main_module, "sweep_startup_leftovers", lambda: calls.append(1) or 0)

    with TestClient(main_module.app):
        pass

    assert calls == [1]
