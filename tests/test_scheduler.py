"""The background refresh loop's per-account scheduling (app/scheduler.py).

The refresh interval used to be one AppSettings row shared by the whole
deployment; it's now Account.feed_refresh_interval_minutes, so the loop has
to decide *which* accounts are due on each tick rather than refreshing
everyone on one shared clock. These tests cover that decision and the
followed_feeds(account_id=...) scoping it depends on — the loop-survives-a-
failure regression test lives in test_health.py, which already covers the
try/except shape.
"""

from datetime import timedelta

from app.content_query import followed_feeds
from app.models import Account, Feed, User
from app.scheduler import _due_accounts, _refresh_due_accounts
from app.timeutil import utcnow

DEFAULT_ACCOUNT_ID = 1
DEFAULT_PROFILE_ID = 1


def _second_account(db_session, **account_kwargs) -> Account:
    """A whole second account (its own login), with one profile and one
    followed feed — for the account-scoping tests below. A real Account row
    rather than a bare user_id, same reasoning as test_partials.py's
    _other_profile_feed: foreign keys are enforced now (see app/database.py)."""
    defaults = {"email": "second@example.com", "password_hash": "x"}
    defaults.update(account_kwargs)
    account = Account(**defaults)
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)

    profile = User(name="Second Account's Profile", account_id=account.id)
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)

    feed = Feed(user_id=profile.id, rss_url="https://example.com/second-account", channel_title="Second")
    db_session.add(feed)
    db_session.commit()
    db_session.refresh(feed)

    return account


def test_a_never_refreshed_account_is_always_due(db_session):
    account = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    account.feeds_refreshed_at = None
    db_session.commit()

    assert account in _due_accounts(db_session)


def test_an_account_past_its_own_interval_is_due(db_session):
    account = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    account.feed_refresh_interval_minutes = 30
    account.feeds_refreshed_at = utcnow() - timedelta(minutes=31)
    db_session.commit()

    assert account in _due_accounts(db_session)


def test_an_account_inside_its_own_interval_is_not_due(db_session):
    account = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    account.feed_refresh_interval_minutes = 30
    account.feeds_refreshed_at = utcnow() - timedelta(minutes=10)
    db_session.commit()

    assert account not in _due_accounts(db_session)


def test_two_accounts_are_judged_by_their_own_interval_independently(db_session):
    """The whole point of moving this off a single shared AppSettings row —
    one account picking a short interval must not drag another account's
    refresh forward, and vice versa."""
    short = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    short.feed_refresh_interval_minutes = 15
    short.feeds_refreshed_at = utcnow() - timedelta(minutes=20)
    db_session.commit()

    long = _second_account(db_session, feed_refresh_interval_minutes=120)
    long.feeds_refreshed_at = utcnow() - timedelta(minutes=20)
    db_session.commit()

    due = _due_accounts(db_session)
    assert short in due
    assert long not in due


def test_followed_feeds_scoped_to_an_account_covers_every_one_of_its_profiles(db_session):
    """An account's refresh has to reach every profile under it, not just
    the profile that happens to share the account's own id."""
    account = db_session.get(Account, DEFAULT_ACCOUNT_ID)

    second_profile = User(name="Second Profile, Same Account", account_id=account.id)
    db_session.add(second_profile)
    db_session.commit()
    db_session.refresh(second_profile)

    feed_one = Feed(user_id=DEFAULT_PROFILE_ID, rss_url="https://example.com/p1", channel_title="P1")
    feed_two = Feed(user_id=second_profile.id, rss_url="https://example.com/p2", channel_title="P2")
    db_session.add_all([feed_one, feed_two])
    db_session.commit()

    other_account = _second_account(db_session)

    feed_ids = {f.id for f in followed_feeds(db_session, account_id=account.id).all()}
    assert feed_one.id in feed_ids
    assert feed_two.id in feed_ids
    other_feed_ids = {
        f.id for f in followed_feeds(db_session, account_id=other_account.id).all()
    }
    assert feed_ids.isdisjoint(other_feed_ids)


def test_refresh_due_accounts_stamps_feeds_refreshed_at(db_session, monkeypatch):
    import app.scheduler as scheduler_module

    account = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    account.feeds_refreshed_at = None
    db_session.commit()

    monkeypatch.setattr(scheduler_module, "refresh_feeds", lambda db, feeds: 0)

    _refresh_due_accounts()

    db_session.expire_all()
    refreshed = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    assert refreshed.feeds_refreshed_at is not None


def test_refresh_due_accounts_skips_an_account_that_is_not_due(db_session, monkeypatch):
    import app.scheduler as scheduler_module

    account = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    account.feed_refresh_interval_minutes = 120
    stamp = utcnow() - timedelta(minutes=5)
    account.feeds_refreshed_at = stamp
    db_session.commit()

    calls = []
    monkeypatch.setattr(
        scheduler_module, "refresh_feeds", lambda db, feeds: calls.append(1) or 0
    )

    _refresh_due_accounts()

    assert calls == []
    db_session.expire_all()
    untouched = db_session.get(Account, DEFAULT_ACCOUNT_ID)
    assert untouched.feeds_refreshed_at == stamp
