"""The background refresh loop's per-user scheduling (app/scheduler.py).

The refresh interval used to be one AppSettings row shared by the whole
deployment, then per-Account across the profiles under it. It's now
User.feed_refresh_interval_minutes, so the loop has to decide *which* users
are due on each tick rather than refreshing everyone on one shared clock.
These tests cover that decision and the followed_feeds(user_id=...) scoping
it depends on — the loop-survives-a-failure regression test lives in
test_health.py, which already covers the try/except shape.
"""

import asyncio
from datetime import timedelta

from app.auth import hash_password
from app.content_query import followed_feeds
from app.models import Feed, User
from app.scheduler import _due_users, _refresh_due_users
from app.timeutil import utcnow

DEFAULT_USER_ID = 1


def _second_user(db_session, **user_kwargs) -> User:
    """A whole second login, with one followed feed — for the scoping tests
    below. A real User row rather than a bare user_id: foreign keys are
    enforced now (see app/database.py)."""
    defaults = {"email": "second@example.com", "password_hash": hash_password("x")}
    defaults.update(user_kwargs)
    user = User(**defaults)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    feed = Feed(user_id=user.id, rss_url="https://example.com/second-user", channel_title="Second")
    db_session.add(feed)
    db_session.commit()

    return user


def test_a_never_refreshed_user_is_always_due(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.feeds_refreshed_at = None
    db_session.commit()

    assert user in _due_users(db_session)


def test_a_user_past_their_own_interval_is_due(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.feed_refresh_interval_minutes = 30
    user.feeds_refreshed_at = utcnow() - timedelta(minutes=31)
    db_session.commit()

    assert user in _due_users(db_session)


def test_a_user_inside_their_own_interval_is_not_due(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.feed_refresh_interval_minutes = 30
    user.feeds_refreshed_at = utcnow() - timedelta(minutes=10)
    db_session.commit()

    assert user not in _due_users(db_session)


def test_two_users_are_judged_by_their_own_interval_independently(db_session):
    """The whole point of moving this off a single shared AppSettings row —
    one user picking a short interval must not drag another's refresh
    forward, and vice versa."""
    short = db_session.get(User, DEFAULT_USER_ID)
    short.feed_refresh_interval_minutes = 15
    short.feeds_refreshed_at = utcnow() - timedelta(minutes=20)
    db_session.commit()

    long = _second_user(db_session, feed_refresh_interval_minutes=120)
    long.feeds_refreshed_at = utcnow() - timedelta(minutes=20)
    db_session.commit()

    due = _due_users(db_session)
    assert short in due
    assert long not in due


def test_followed_feeds_scoped_to_a_user_excludes_everyone_elses(db_session):
    feed = Feed(user_id=DEFAULT_USER_ID, rss_url="https://example.com/mine", channel_title="Mine")
    db_session.add(feed)
    db_session.commit()

    other = _second_user(db_session)

    feed_ids = {f.id for f in followed_feeds(db_session, user_id=DEFAULT_USER_ID).all()}
    other_ids = {f.id for f in followed_feeds(db_session, user_id=other.id).all()}

    assert feed.id in feed_ids
    assert feed_ids.isdisjoint(other_ids)


def test_refresh_due_users_stamps_feeds_refreshed_at(db_session, monkeypatch):
    import app.scheduler as scheduler_module

    user = db_session.get(User, DEFAULT_USER_ID)
    user.feeds_refreshed_at = None
    db_session.commit()

    monkeypatch.setattr(scheduler_module, "refresh_feeds", lambda db, feeds: 0)

    _refresh_due_users()

    db_session.expire_all()
    assert db_session.get(User, DEFAULT_USER_ID).feeds_refreshed_at is not None


def test_run_scheduler_sweeps_disk_every_tick(monkeypatch):
    """The disk/DB sweeps (storage.sweep_orphans, sweep_stale_previews — see
    scheduler._sweep_disk) have to run every tick regardless of which, if
    any, users were due — they're not user-scoped at all."""
    import app.scheduler as scheduler_module

    calls = []
    monkeypatch.setattr(scheduler_module, "_refresh_due_users", lambda: None)
    monkeypatch.setattr(scheduler_module, "_sweep_disk", lambda: calls.append(1))

    async def drive():
        scheduler_module.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.005)
                if calls:
                    break
        finally:
            await scheduler_module.stop()

    asyncio.run(drive())

    assert calls, "the scheduler tick never called _sweep_disk"


def test_refresh_due_users_skips_a_user_that_is_not_due(db_session, monkeypatch):
    import app.scheduler as scheduler_module

    user = db_session.get(User, DEFAULT_USER_ID)
    user.feed_refresh_interval_minutes = 120
    stamp = utcnow() - timedelta(minutes=5)
    user.feeds_refreshed_at = stamp
    db_session.commit()

    calls = []
    monkeypatch.setattr(
        scheduler_module, "refresh_feeds", lambda db, feeds: calls.append(1) or 0
    )

    _refresh_due_users()

    assert calls == []
    db_session.expire_all()
    assert db_session.get(User, DEFAULT_USER_ID).feeds_refreshed_at == stamp
