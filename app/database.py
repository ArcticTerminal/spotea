from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

# check_same_thread=False because background tasks and the refresh pool use
# sessions on worker threads, not the request thread.
#
# NullPool, i.e. a fresh connection per session rather than a pooled one. The
# default QueuePool caps out at pool_size=5 + max_overflow=10, and a single
# background artist refresh already wants nine of those (one for the scheduler,
# eight for the fetch pool — see artist_sync.REFRESH_POOL_SIZE, whose workers
# each open their own read session). Six left over is fewer than one user
# action needs: refreshFragments() alone fires three requests, the queue's
# prefetch two, the download poll one. Past the cap SQLAlchemy blocks for 30
# seconds and then raises, so pressing "save" during a refresh of 73 channels
# could stall outright. Opening a SQLite connection is a file open, not a
# network handshake, so there is nothing worth pooling to begin with.
connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    poolclass=NullPool if _is_sqlite else None,
)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        """Per-connection SQLite settings.

        WAL is the one that matters: in the default rollback-journal mode a
        writer blocks readers outright, and this app writes from a background
        thread pool while serving reads — a refresh across 73 channels commits
        once per channel, and every one of those commits locked out the
        requests happening at the same time. WAL lets them run concurrently.

        synchronous=NORMAL is WAL's usual companion: durable against a process
        crash, and only at risk of losing the last transactions if the host
        itself loses power, which is the right trade for a media library.

        foreign_keys=ON because SQLite leaves them off per connection by
        default, so the FKs the schema declares were never actually enforced.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            # Wait rather than fail immediately if another connection holds the
            # write lock — WAL makes this rare, but a checkpoint can still
            # collide with a commit.
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass
