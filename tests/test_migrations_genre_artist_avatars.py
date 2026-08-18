"""Clearing display metadata the old lazy resolution wrote into
genre_artists (app/migrations.py's _reset_lazily_resolved_genre_artists).

Suggested channels used to be resolved in-request, and stored an
already-built display URL ("/avatar-proxy?u=...") in thumbnail_url. The
column holds the remote upstream URL now, wrapped for display at read time
instead, so an existing install carries rows in a shape the new code would
double-wrap into something unservable. See app/services/genre_artists.py.
"""

from app.migrations import run_migrations
from app.models import GenreArtist
from app.timeutil import utcnow


def _row(db_session, *, channel_id, thumbnail_url, resolved=True):
    row = GenreArtist(
        genre="Jazz",
        artist_name=f"Artist {channel_id}",
        channel_id=channel_id,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
        title="Some Title",
        thumbnail_url=thumbnail_url,
        resolved_at=utcnow() if resolved else None,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_a_lazily_resolved_proxy_url_is_cleared(db_session):
    row = _row(db_session, channel_id="UClegacy0000000000", thumbnail_url="/avatar-proxy?u=x")

    run_migrations(db_session.get_bind())
    db_session.expire_all()

    assert row.thumbnail_url is None
    # resolved_at goes with it: that flag is what marks a row as having
    # usable display metadata, and leaving it set would hide the missing
    # avatar behind an "already resolved" no re-seed would clear.
    assert row.resolved_at is None


def test_a_remote_url_is_left_alone(db_session):
    """What the new resolution writes — an absolute upstream URL — is
    exactly what the column is supposed to hold."""
    row = _row(db_session, channel_id="UCcurrent000000000", thumbnail_url="https://yt3.ggpht.com/a=s0")

    run_migrations(db_session.get_bind())
    db_session.expire_all()

    assert row.thumbnail_url == "https://yt3.ggpht.com/a=s0"
    assert row.resolved_at is not None


def test_a_dead_channel_with_no_avatar_is_left_alone(db_session):
    """A row stamped resolved with no thumbnail at all is the documented
    dead-channel state, not stale data — clearing it would put the generator
    back to retrying channels that have already been shown not to answer."""
    row = _row(db_session, channel_id="UCdead0000000000000", thumbnail_url=None)

    run_migrations(db_session.get_bind())
    db_session.expire_all()

    assert row.resolved_at is not None


def test_the_migration_does_not_repeat_on_a_second_startup(db_session):
    """Gated on a match rather than run as a bare UPDATE, so it stops
    rewriting rows once there is nothing left in the old shape — the same
    mistake the audio_quality backfill above it made for the life of a
    deployment."""
    _row(db_session, channel_id="UClegacy0000000000", thumbnail_url="/avatar-proxy?u=x")

    run_migrations(db_session.get_bind())
    db_session.expire_all()

    # A row resolved between the two startups must survive the second one.
    fresh = _row(db_session, channel_id="UCafter000000000000", thumbnail_url="https://yt3.ggpht.com/b=s0")

    run_migrations(db_session.get_bind())
    db_session.expire_all()

    assert fresh.thumbnail_url == "https://yt3.ggpht.com/b=s0"
    assert fresh.resolved_at is not None
