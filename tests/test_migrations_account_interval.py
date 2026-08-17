"""The one-time move of the shared refresh interval off app_settings and onto
every Account (app/migrations.py's _migrate_refresh_interval_off_app_settings).

app_settings no longer has a model — Base.metadata.create_all() never creates
it on a fresh install — so these tests build the legacy table by hand with
raw SQL to stand in for an existing install upgrading into this change.
"""

from sqlalchemy import text

from app.database import engine
from app.migrations import run_migrations
from app.models import Account


def _create_legacy_app_settings(interval_minutes: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS app_settings"))
        conn.execute(
            text(
                "CREATE TABLE app_settings ("
                "id INTEGER PRIMARY KEY, "
                "feed_refresh_interval_minutes INTEGER NOT NULL DEFAULT 30)"
            )
        )
        conn.execute(
            text("INSERT INTO app_settings (id, feed_refresh_interval_minutes) VALUES (1, :v)"),
            {"v": interval_minutes},
        )


def _table_exists(name: str) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
                {"name": name},
            ).scalar()
            is not None
        )


def test_every_accounts_interval_is_overwritten_from_the_legacy_row(db_session):
    """The actual point: an existing install's owner had already picked
    something other than the default, and the migration must preserve that
    choice rather than silently resetting everyone to 30."""
    _create_legacy_app_settings(90)

    run_migrations(engine)
    db_session.expire_all()

    account = db_session.get(Account, 1)
    assert account.feed_refresh_interval_minutes == 90


def test_the_legacy_table_is_dropped(db_session):
    _create_legacy_app_settings(45)

    run_migrations(engine)

    assert not _table_exists("app_settings")


def test_a_fresh_install_with_no_legacy_table_is_untouched(db_session):
    """No app_settings table ever existed on a fresh install — the migration
    must be a no-op, not an error, and every account keeps the model's own
    default (30) rather than being overwritten with nothing."""
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS app_settings"))

    run_migrations(engine)
    db_session.expire_all()

    account = db_session.get(Account, 1)
    assert account.feed_refresh_interval_minutes == 30


def test_the_migration_does_not_repeat_on_a_second_startup(db_session):
    """Self-disabling: dropping the table is what stops this from running
    again, so a second startup must not re-read a since-changed per-account
    value back down to whatever the old shared row happened to hold."""
    _create_legacy_app_settings(90)
    run_migrations(engine)
    db_session.expire_all()

    account = db_session.get(Account, 1)
    account.feed_refresh_interval_minutes = 15
    db_session.commit()

    run_migrations(engine)
    db_session.expire_all()

    account = db_session.get(Account, 1)
    assert account.feed_refresh_interval_minutes == 15
