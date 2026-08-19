"""feeds.artist_browse_id arriving on a database that predates it.

The column is both a flag and an address (see Feed.artist_browse_id), and
what makes the upgrade safe is that NULL is the correct value for every row
already there: none of them were followed as an artist, so every existing
card keeps opening the track list it always opened. These pin that, by
dropping the column and running the migration the way an older install
would.
"""

from sqlalchemy import inspect, text

from app.database import engine
from app.migrations import run_migrations
from app.models import Feed


def _drop_column() -> None:
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE feeds DROP COLUMN artist_browse_id"))


def _columns() -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("feeds")}


def test_the_column_is_added_to_an_older_database(db_session):
    _drop_column()
    assert "artist_browse_id" not in _columns()

    run_migrations(engine)

    assert "artist_browse_id" in _columns()


def test_existing_feeds_are_not_marked_as_artists(db_session):
    """A pre-existing follow is a channel follow — its card must keep
    opening its own track list, not somebody's artist profile."""
    feed = Feed(user_id=1, rss_url="https://example.com/rss", channel_title="A Channel")
    db_session.add(feed)
    db_session.commit()

    _drop_column()
    run_migrations(engine)

    db_session.expire_all()
    assert db_session.query(Feed).filter(Feed.id == feed.id).one().artist_browse_id is None


def test_running_it_twice_is_a_no_op(db_session):
    """Every startup runs this — see main.py's lifespan."""
    run_migrations(engine)
    run_migrations(engine)

    assert "artist_browse_id" in _columns()
