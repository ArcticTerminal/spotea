"""Connection settings for the SQLite engine."""

from app.database import engine


def test_sqlite_runs_in_wal_mode_with_foreign_keys_on():
    """WAL because a writer in rollback-journal mode blocks readers outright,
    and the background refresh commits once per channel across dozens of them.
    foreign_keys because SQLite leaves them off per connection, so the FKs the
    schema declares were never actually enforced."""
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
