from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FeedCreate(BaseModel):
    channel_url: str


class FeedOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rss_url: str
    channel_title: str | None
    added_at: datetime


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    feed_id: int
    channel_title: str | None
    video_id: str
    title: str
    thumbnail_url: str | None
    published_at: datetime | None
    status: str
    added_at: datetime


class StatusOut(BaseModel):
    id: int
    status: str
    error_message: str | None = None


class FeedAddResult(BaseModel):
    feed: FeedOut
    new_content_count: int


class RefreshResult(BaseModel):
    new_content_count: int
