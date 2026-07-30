from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

CONTENT_STATUSES = ("not_downloaded", "downloading", "ready", "error")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))

    feeds: Mapped[list["Feed"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    content: Mapped[list["Content"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (UniqueConstraint("user_id", "rss_url", name="uq_feed_user_rss_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rss_url: Mapped[str] = mapped_column(String(500))
    channel_title: Mapped[str | None] = mapped_column(String(200), default=None)
    added_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="feeds")
    content: Mapped[list["Content"]] = relationship(back_populates="feed", cascade="all, delete-orphan")


class Content(Base):
    __tablename__ = "content"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_content_user_video_id"),
        Index("ix_content_user_status", "user_id", "status"),
        Index("ix_content_user_published_at", "user_id", "published_at"),
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
    error_message: Mapped[str | None] = mapped_column(String(1000), default=None)
    added_at: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(default=None)
    is_favorite: Mapped[bool] = mapped_column(default=False)

    feed: Mapped["Feed"] = relationship(back_populates="content")
    user: Mapped["User"] = relationship(back_populates="content")
