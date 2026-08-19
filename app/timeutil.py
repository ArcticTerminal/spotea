from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC "now" — the single datetime convention across the app.

    Every datetime column here (Content.published_at/added_at/downloaded_at/
    last_played_at, Feed.added_at, User.created_at) holds naive UTC.
    SQLite has no timezone type and SQLAlchemy's DATETIME storage format
    drops tzinfo when writing, so a tz-aware value ends up stored as naive
    anyway — but only after having been compared against naive ones in
    Python first, where mixing the two raises. Producing naive UTC at every
    source (this function, plus rss._parse_published) is what keeps that
    from being a latent bug rather than relying on the write path to
    launder it.

    datetime.utcnow() did exactly this, but is deprecated since 3.12 and
    scheduled for removal; this is its documented replacement.
    """
    return datetime.now(UTC).replace(tzinfo=None)
