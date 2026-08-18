from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timeutil import utcnow

CONTENT_STATUSES = ("not_downloaded", "downloading", "ready", "error")


class Account(Base):
    """The real, credentialed login — owns one or more `User` profiles
    (household model: one account, several family-member profiles). Email is
    always stored lowercased (normalized at the auth-router call sites), so
    a plain unique constraint is enough without a case-insensitive collation."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Which profile to land on right after login — session-stored
    # PROFILE_SESSION_KEY doesn't survive logout (the whole session is
    # cleared), so without this a fresh login always fell back to the
    # account's first profile regardless of which one was active before.
    # Deliberately a plain int, not a relationship/ForeignKey: a real FK to
    # users.id would make Account and User mutually reference each other,
    # and Base.metadata.create_all() can't topologically order a table-
    # creation cycle. Validity (still one of this account's own profiles) is
    # checked at the application layer instead, in get_current_profile.
    last_active_profile_id: Mapped[int | None] = mapped_column(default=None)
    # How often the background scheduler refreshes this account's feeds — see
    # scheduler.py. One account's choice covers every one of its profiles
    # (household model: kids' and parents' profiles share one refresh
    # cadence); a different account picks independently. Used to live as a
    # single AppSettings row shared by the whole deployment, from before
    # multiple real accounts existed.
    feed_refresh_interval_minutes: Mapped[int] = mapped_column(default=30)
    # When the scheduler last refreshed this account's feeds. None means
    # never (a brand-new account, or one migrated from the old shared
    # AppSettings row) — the scheduler treats that the same as "overdue",
    # so a fresh account's first tick refreshes it immediately rather than
    # waiting a full interval with nothing to compare against.
    feeds_refreshed_at: Mapped[datetime | None] = mapped_column(default=None)

    profiles: Mapped[list["User"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("audio_quality IN ('high', 'low')", name="ck_user_audio_quality"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    name: Mapped[str] = mapped_column(String(100))
    audio_quality: Mapped[str] = mapped_column(String(10), default="high")
    # Newline-separated free-text tags — genres, artists, topics — that
    # Explore's recommendations are built from. Parsed and written only
    # through app/interests.py, which owns the format (and the reason it
    # isn't a table of its own).
    interests: Mapped[str | None] = mapped_column(Text, default=None)

    account: Mapped["Account"] = relationship(back_populates="profiles")
    feeds: Mapped[list["Feed"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    content: Mapped[list["Content"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    recommendation_cache: Mapped["RecommendationCache | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class RecommendationCache(Base):
    """One profile's last batch of interest-based Explore recommendations.

    Cached in the database rather than recomputed per request because
    building a batch means several live YouTube searches — seconds of
    latency, and request volume this app has good reason to keep low (see
    services/recommendations.py). One row per profile: a batch is only ever
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


class GenreArtist(Base):
    """One real artist, tagged with one of the onboarding wizard's predefined
    genres (see app/genres.py), whose MusicBrainz record links straight to a
    YouTube channel — the onboarding wizard's channel-suggestion source
    (see services/genre_artists.py), instead of a generic YouTube search on
    the genre name (which mostly surfaces "Best Jazz Mix 2024" compilation
    channels, not artists).

    Not one row per genre (unlike RecommendationCache's one-blob-per-profile
    shape): title/thumbnail_url/subscriber_count/resolved_at get filled in
    independently, artist by artist, the first time any profile's onboarding
    actually needs that genre — a shared JSON blob would need a read-modify-
    write on every single one of those resolutions instead of a plain row
    update. `resolved_at is None` means MusicBrainz has already supplied the
    channel_id/channel_url but nobody has paid the YouTube round trip for
    the display metadata yet.
    """

    __tablename__ = "genre_artists"
    __table_args__ = (UniqueConstraint("genre", "channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    genre: Mapped[str] = mapped_column(String(60), index=True)
    artist_name: Mapped[str] = mapped_column(String(200))
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_url: Mapped[str] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), default=None)
    subscriber_count: Mapped[int | None] = mapped_column(default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)


class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (UniqueConstraint("user_id", "rss_url", name="uq_feed_user_rss_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rss_url: Mapped[str] = mapped_column(String(500))
    channel_title: Mapped[str | None] = mapped_column(String(200), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(500), default=None)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    # False only for placeholder feeds auto-created to hold a single video
    # added via Explore (see routers/explore.py's _get_or_create_placeholder_feed)
    # — invisible in Library, skipped by the background refresh scheduler,
    # until the user actually follows the channel for real.
    followed: Mapped[bool] = mapped_column(default=True)

    user: Mapped["User"] = relationship(back_populates="feeds")
    content: Mapped[list["Content"]] = relationship(back_populates="feed", cascade="all, delete-orphan")


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
        # A channel's track list and its count: both filter user_id + feed_id
        # and order by published_at, which the (user_id, published_at) index
        # above could only answer by walking every row the user has.
        Index("ix_content_user_feed_published", "user_id", "feed_id", "published_at"),
        # Looked up by video_id alone — feed_sync.cache_thumbnail and
        # storage.unlink_thumbnail_if_unshared, both of which run per rendered
        # item and per purged row. The (user_id, video_id) unique constraint
        # can't serve these: video_id is its second column.
        Index("ix_content_video_id", "video_id"),
        # storage.unlink_if_unshared, once per file removed. Was a full table
        # scan, which is what made clearing a large channel take tens of
        # seconds.
        Index("ix_content_file_path", "file_path", sqlite_where=text("file_path IS NOT NULL")),
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
    feed_id: Mapped[int] = mapped_column(ForeignKey("feeds.id"))
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
    # the same thing again, which is request volume that feeds the very
    # rate-limiting the retry ladder exists for. Cleared by a successful
    # download and by DELETE /content/{id}, which is the manual "try this
    # again" path.
    is_unavailable: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(default=None)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    is_saved: Mapped[bool] = mapped_column(default=False)
    last_played_at: Mapped[datetime | None] = mapped_column(default=None)
    # Incremented alongside last_played_at (see routers/content.py's
    # stream_content) — a play-frequency counter, since last_played_at alone
    # only says *when* something was last played, not how often.
    # Approximate by design, not a precise listen counter: a single play can
    # issue more than one range request as the browser buffers/seeks, and
    # each one increments this — acceptable for ranking "played a lot" vs.
    # "played once", not meant for exact counts. Deliberately untouched by
    # Settings' "Clear recently played" (which only resets last_played_at).
    # Not currently surfaced anywhere in the UI (the "On Repeat" smart
    # playlist that read this was removed) — kept as a tracked signal in
    # case it's worth building on later.
    play_count: Mapped[int] = mapped_column(default=0)
    # True for a just-added Explore row that hasn't been favorited or saved
    # yet (see routers/content.py's add_favorite/add_saved, which clear this
    # as a side effect) — plays normally but stays out of Library and New
    # Uploads until then. Still shows on the Recently Played shelf once
    # played (see routers/pages.py's home_recently_played). No automatic
    # cleanup — it stays around indefinitely otherwise.
    is_preview: Mapped[bool] = mapped_column(default=False)
    # True for a row inserted by an RSS parse (a channel's initial fetch when
    # followed, or any later routine refresh — see feed_sync.apply_feed_data,
    # the only place this is set) — False for services/backfill.py's direct inserts
    # (the full-history scan) and Explore's add_single_video. This is what
    # "New Uploads" (Home shelf and Library's full playlist) actually means:
    # RSS-sourced content, not a channel's backfilled-in history.
    is_new_upload: Mapped[bool] = mapped_column(default=False)

    feed: Mapped["Feed"] = relationship(back_populates="content")
    user: Mapped["User"] = relationship(back_populates="content")
