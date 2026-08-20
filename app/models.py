from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutil import utcnow

CONTENT_STATUSES = ("not_downloaded", "downloading", "ready", "error")


class User(Base):
    """One login, one library.

    Was two tables — an `Account` holding the credentials and one or more
    `User` profiles under it, Netflix-style. The household model is gone
    (one person, one library), so the credentials moved onto the row that
    already owned the artists and the content, and `accounts` went away.

    Email is always stored lowercased (normalized at the auth-router call
    sites), so a plain unique constraint is enough without a case-insensitive
    collation.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("audio_quality IN ('high', 'low')", name="ck_user_audio_quality"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    audio_quality: Mapped[str] = mapped_column(String(10), default="high")
    # Newline-separated free-text tags — genres, artists, moods — that
    # Explore's recommendations are built from. Parsed and written only
    # through app/interests.py, which owns the format (and the reason it
    # isn't a table of its own).
    interests: Mapped[str | None] = mapped_column(Text, default=None)
    # How often the background scheduler refreshes this library — see
    # scheduler.py.
    refresh_interval_minutes: Mapped[int] = mapped_column(default=30)
    # When the scheduler last refreshed it. None means never, which the
    # scheduler treats the same as "overdue" — so a fresh account's first
    # tick refreshes it immediately rather than waiting a full interval with
    # nothing to compare against.
    refreshed_at: Mapped[datetime | None] = mapped_column(default=None)

    artists: Mapped[list["Artist"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    content: Mapped[list["Content"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recommendation_cache: Mapped["RecommendationCache | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RecommendationCache(Base):
    """The last batch of interest-based Explore recommendations.

    Cached in the database rather than recomputed per request because
    building a batch means several live YouTube searches — seconds of
    latency, and request volume this app has good reason to keep low (see
    services/recommendations.py). One row per user: a batch is only ever
    read and replaced whole, never merged, so there's nothing to gain from
    storing the individual results as rows.

    `payload` is the JSON the API hands back verbatim; `interests_signature`
    is what the profile's interests hashed to when it was built (see
    interests.interests_signature), which is how an edit to the interest list
    invalidates it without anything having to explicitly delete this row.
    """

    __tablename__ = "recommendation_cache"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    interests_signature: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    generated_at: Mapped[datetime] = mapped_column(default=utcnow)

    user: Mapped["User"] = relationship(back_populates="recommendation_cache")


class Artist(Base):
    """A musician in the library.

    Was `Feed`, keyed by the RSS URL a sync used to read. Nothing reads RSS
    any more (see services/artist_sync.py), and what the library actually
    holds is artists — so the row is named for what it is and keyed by the
    ids that address one.

    `channel_id` is the artist's "<Artist> - Topic" channel: the container
    YouTube publishes their licensed audio to. It is the *key* rather than
    `browse_id` because it is what a track carries — a song grabbed from
    Explore hangs off the Topic channel, and it has to land on the same row
    as a deliberate follow of the same artist rather than making a second one.

    `browse_id` is how YouTube Music addresses their page, which for an
    artist with an official channel is that channel's id. It opens their
    profile and it is what the sync asks about. Null only on the placeholder
    rows below, which are created from a track and never resolved further.
    """

    __tablename__ = "artists"
    __table_args__ = (UniqueConstraint("user_id", "channel_id", name="uq_artist_user_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(500), default=None)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    # False only for placeholder rows auto-created to hold a single track
    # added via Explore (see routers/explore.py's _get_or_create_placeholder)
    # — invisible in Library, skipped by the background refresh scheduler,
    # until the user actually follows the artist for real.
    followed: Mapped[bool] = mapped_column(default=True)
    browse_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # Every release browse id YouTube Music listed for this artist last time
    # we looked, as a JSON array. The whole change-detection mechanism: what
    # is on the page now and not in here is something they put out since (see
    # services/artist_sync.py). NULL means "never synced", which is what makes
    # a first sync record the catalogue without importing it.
    release_snapshot: Mapped[str | None] = mapped_column(Text, default=None)
    # YouTube Music's own bare count string ("1.91M"), refreshed on every
    # sync — see services/artist_sync.py. Comes back with every get_artist
    # call a sync already makes, so this costs nothing extra to keep, unlike
    # subscriber_count and description, which the same response carries but
    # nothing here persists.
    monthly_listeners: Mapped[str | None] = mapped_column(String(32), default=None)
    # YouTube Music's own "fans also like" list for this artist, as a JSON
    # array of ChannelSearchResult dicts — same free-data reasoning as
    # monthly_listeners above, and refreshed the same way on every sync.
    # What powers Explore's "Similar artists" shelf (see
    # services/recommendations.py._similar_to_followed): merged across every
    # artist this user follows, rather than fetched fresh at request time.
    related_artists: Mapped[str | None] = mapped_column(Text, default=None)
    # The artist's own page-preview songs — YouTube Music's page shows five
    # before you have to open the full list (see youtube/music.py's
    # _artist_songs, which is what a sync's all_songs=False call reads) — as
    # a JSON array of VideoSearchResult dicts. Same free-data reasoning as
    # related_artists:
    # arrives on the same get_artist call a sync already makes. Powers
    # Explore's "Songs" shelf (see
    # services/recommendations.py._songs_from_followed) — merged across
    # every followed artist rather than searched from typed interests, which
    # went the same way genre-as-artist-search did (see similar_artists).
    top_tracks: Mapped[str | None] = mapped_column(Text, default=None)

    user: Mapped["User"] = relationship(back_populates="artists")
    content: Mapped[list["Content"]] = relationship(back_populates="artist", cascade="all, delete-orphan")


class Content(Base):
    __tablename__ = "content"
    # Indexes below the first two were added after measuring the query plans on
    # a real 30k-row library: every one of them was answering a SCAN or a
    # USE TEMP B-TREE. Across the ten hottest queries this took 81.7ms of
    # SQLite time down to 3.8ms, and one operation from ~35 seconds to ~0.13
    # (unfollowing a 6,540-video channel, where purge_content does two
    # unindexed lookups per row). They cost nothing measurable in size: the
    # partial ones only cover the rows that actually match.
    #
    # The `sqlite_where` clauses are what makes them partial, and they are
    # dialect-specific — on any other backend these degrade to full indexes,
    # which is slower to write but still correct.
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_content_user_video_id"),
        Index("ix_content_user_status", "user_id", "status"),
        Index("ix_content_user_published_at", "user_id", "published_at"),
        # An artist's track list and its count: both filter user_id + artist_id
        # and order by published_at, which the (user_id, published_at) index
        # above could only answer by walking every row the user has.
        Index("ix_content_user_artist_published", "user_id", "artist_id", "published_at"),
        # Looked up by video_id alone — artist_sync.cache_thumbnail, which
        # runs per rendered item. The (user_id, video_id) unique constraint
        # can't serve it: video_id is its second column.
        Index("ix_content_video_id", "video_id"),
        # The three pinned-playlist shelves and their counts. Partial, because
        # "played", "favorite" and "saved" are each a small slice of a library.
        Index(
            "ix_content_user_played",
            "user_id",
            "last_played_at",
            sqlite_where=text("last_played_at IS NOT NULL"),
        ),
        Index(
            "ix_content_user_favorite",
            "user_id",
            "published_at",
            sqlite_where=text("is_favorite = 1"),
        ),
        Index("ix_content_user_saved", "user_id", "published_at", sqlite_where=text("is_saved = 1")),
        Index(
            "ix_content_user_newupload",
            "user_id",
            "published_at",
            sqlite_where=text("is_new_upload = 1"),
        ),
        CheckConstraint(
            "status IN ('not_downloaded', 'downloading', 'ready', 'error')",
            name="ck_content_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    video_id: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(500))
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), default=None)
    duration_seconds: Mapped[int | None] = mapped_column(default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(String(20), default="not_downloaded")
    file_path: Mapped[str | None] = mapped_column(String(500), default=None)
    # Size of file_path on disk, recorded once when the download finishes
    # (see routers/content.py's _run_download). Stored rather than stat'ed
    # on demand because storage.collect_usage runs on every Home render —
    # reading it from disk meant one stat syscall per downloaded track just
    # to render a total. Cleared alongside file_path whenever a download is
    # removed, and backfilled lazily for rows downloaded before this column
    # existed (see collect_usage).
    file_size_bytes: Mapped[int | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    # True when the last download failed for a reason no retry can fix —
    # YouTube refuses this video id to every client there is, usually because
    # it's a "- Topic" art track licensed for other countries but not this
    # one (see downloader.is_permanent_failure). A separate flag rather than
    # a fifth `status` value because SQLite can't alter the CHECK constraint
    # above on an existing database, and because it *is* orthogonal: the row
    # is still an errored row, it just has a settled answer rather than a
    # provisional one. What it buys is that nothing re-attempts it — the
    # player skips it instantly instead of spending an extraction to be told
    # the same thing again, which is request volume that artists the very
    # rate-limiting the retry ladder exists for. Cleared by a successful
    # download and by DELETE /content/{id}, which is the manual "try this
    # again" path.
    is_unavailable: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(default=None)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    is_saved: Mapped[bool] = mapped_column(default=False)
    last_played_at: Mapped[datetime | None] = mapped_column(default=None)
    # True for a just-added Explore row that hasn't been favorited or saved
    # yet (see routers/content.py's add_favorite/add_saved, which clear this
    # as a side effect) — plays normally but stays out of Library and New
    # Uploads until then. Still shows on the Recently Played shelf once
    # played (see routers/pages.py's home_recently_played). No automatic
    # cleanup — it stays around indefinitely otherwise.
    is_preview: Mapped[bool] = mapped_column(default=False)
    # True for a row the sync inserted — a release that appeared after the
    # artist was followed (see artist_sync.apply_artist_data, the only place
    # this is set). False for a track added deliberately from Explore. That
    # is what "New releases" (Home shelf and Library's full playlist) means:
    # what they put out since, not everything they have.
    is_new_upload: Mapped[bool] = mapped_column(default=False)

    artist: Mapped["Artist"] = relationship(back_populates="content")
    user: Mapped["User"] = relationship(back_populates="content")
