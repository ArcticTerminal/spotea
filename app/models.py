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
    already owned the feeds and the content, and `accounts` went away.

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
    feed_refresh_interval_minutes: Mapped[int] = mapped_column(default=30)
    # When the scheduler last refreshed it. None means never, which the
    # scheduler treats the same as "overdue" — so a fresh account's first
    # tick refreshes it immediately rather than waiting a full interval with
    # nothing to compare against.
    feeds_refreshed_at: Mapped[datetime | None] = mapped_column(default=None)

    feeds: Mapped[list["Feed"]] = relationship(back_populates="user", cascade="all, delete-orphan")
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
    # Set when this feed was followed from an artist's profile, and null for
    # every other feed — which makes it both the "is this an artist" flag and
    # the id that opens their page. The feed itself points at the artist's
    # "<Artist> - Topic" channel, so this could in principle be read back out
    # of rss_url; kept explicit because "we followed this as an artist" is a
    # decision worth recording rather than re-deriving from a URL shape.
    artist_browse_id: Mapped[str | None] = mapped_column(String(32), default=None)

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
        # Looked up by video_id alone — feed_sync.cache_thumbnail, which
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
