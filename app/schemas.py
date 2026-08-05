from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FeedCreate(BaseModel):
    channel_url: str


class FeedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rss_url: str
    channel_title: str | None
    avatar_url: str | None
    added_at: datetime


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    feed_id: int
    channel_title: str | None
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    published_at: datetime | None
    status: str
    added_at: datetime
    is_favorite: bool
    is_saved: bool


class StatusOut(BaseModel):
    id: int
    status: str
    error_message: str | None = None
    progress_percent: int | None = None
    phase: str | None = None


class FavoriteOut(BaseModel):
    id: int
    is_favorite: bool


class SavedOut(BaseModel):
    id: int
    is_saved: bool


class ChannelSearchResultOut(BaseModel):
    channel_id: str
    title: str
    thumbnail_url: str | None
    subscriber_count: int | None
    channel_url: str


class FeedAddResult(BaseModel):
    feed: FeedOut
    new_content_count: int


class RefreshResult(BaseModel):
    new_content_count: int


class BackfillStatusOut(BaseModel):
    feed_id: int
    phase: str | None = None
    done: int = 0
    total: int = 0


class SettingsOut(BaseModel):
    audio_quality: str


class SettingsUpdate(BaseModel):
    audio_quality: str
