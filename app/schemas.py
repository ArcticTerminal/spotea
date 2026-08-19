from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # import cycle otherwise — models has no reason to know about schemas
    from app.models import Content

# SQLite doesn't enforce a column's declared VARCHAR length — these bounds are
# the only thing standing between a request body and an unbounded write or an
# unbounded amount of downstream work (a bulk import line becomes a yt-dlp
# resolution; interests.py already does this at the function level, see its
# MAX_INTEREST_LENGTH). Chosen to match the column each field ends up in
# rather than picked arbitrarily, so a value that fits here always fits there.
_URL_MAX_LENGTH = 2048  # generous browser-address-bar bound; no column caps it directly
_CONTENT_TITLE_MAX_LENGTH = 500  # Content.title: String(500)
# Above any real subscriptions export (a few hundred channels); guards
# against a pasted list turning into thousands of yt-dlp resolutions.


class ArtistCreate(BaseModel):
    """Following an artist. Only the channel is sent — which artist it is,
    and whether it is one at all, is the server's answer (see
    services/artist_follow.py)."""

    channel_url: str = Field(min_length=1, max_length=_URL_MAX_LENGTH)


class ArtistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: str
    name: str | None
    avatar_url: str | None
    added_at: datetime
    # Reported back because the client never decides it: a follow arrives as
    # a channel URL and the server works out which artist that is (see
    # services/artist_follow.py). This is how the page that asked finds out
    # whose profile to open next.
    browse_id: str | None = None


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    artist_id: int
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
    is_played: bool
    # See Content.is_unavailable — openPlayer reads it off this payload to
    # decide whether to even attempt a download.
    is_unavailable: bool = False

    @classmethod
    def from_content(cls, content: "Content") -> "ContentOut":
        """Build from a Content row. Requires `content.artist` to be loaded —
        every caller uses joinedload(Content.artist) for exactly this reason.

        Not `model_validate`: two of the fields aren't columns. channel_title
        comes from the related Artist, and is_played is a derived boolean rather
        than the raw last_played_at timestamp (the client only ever needs
        "has this been played", and the timestamp isn't the client's business).
        This was written out field-by-field at both call sites, which is a
        long list of trivially-forgettable lines to keep in sync.
        """
        return cls(
            id=content.id,
            artist_id=content.artist_id,
            channel_title=content.artist.name,
            video_id=content.video_id,
            title=content.title,
            thumbnail_url=content.thumbnail_url,
            duration_seconds=content.duration_seconds,
            published_at=content.published_at,
            status=content.status,
            added_at=content.added_at,
            is_favorite=content.is_favorite,
            is_saved=content.is_saved,
            is_played=content.last_played_at is not None,
            is_unavailable=content.is_unavailable,
        )


class QueueOut(BaseModel):
    """One channel's or pinned playlist's full track order, ids only — what
    "Play all" loads. Capped at content_query.QUEUE_MAX_ITEMS."""

    ids: list[int]


class StatusOut(BaseModel):
    id: int
    status: str
    error_message: str | None = None
    progress_percent: int | None = None
    phase: str | None = None
    # See Content.is_unavailable. The player treats this as "skip now" rather
    # than "failed", so it has to travel with every status the player reads.
    is_unavailable: bool = False


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


class VideoSearchResultOut(BaseModel):
    video_id: str
    title: str
    thumbnail_url: str | None
    duration_seconds: int | None
    channel_title: str | None
    # See VideoSearchResult in youtube/search.py. Reliable for playlist and
    # channel listings, and now for song search too — YouTube Music
    # attributes every track to its artist's "Topic" channel, which is what
    # makes the artist's name a link into their page (home/explore.js).
    # Still absent or unreliable on the yt-dlp fallback search, so it stays
    # optional and every consumer has to cope with None.
    channel_id: str | None = None


class PlaylistSearchResultOut(BaseModel):
    playlist_id: str
    title: str
    thumbnail_url: str | None
    channel_title: str | None


class MoodShelfOut(BaseModel):
    """One of YouTube Music's moods or genres and its playlists. `title` is
    the shelf's heading ("Chill", "Bollywood & Indian") and `section` is
    which of the two menus it came from, so the client can say whether it's
    showing a mood or a genre."""

    title: str
    section: str
    playlists: list[PlaylistSearchResultOut]


class RecommendationsOut(BaseModel):
    """Explore's browse shelves. Every result list reuses a shape the search
    box already returns, because they come from the same searches — the
    client renders a recommended song and a searched one identically, and
    "listen" on either goes through POST /artists/videos."""

    # Everything the interest list holds, so Explore can say what it's working
    # from (and tell "no interests set" apart from "interests set, nothing
    # found") without a second request to /settings.
    interests: list[str]
    # The subset this batch was actually built from — see
    # services/recommendations.py's INTERESTS_PER_RUN.
    interests_used: list[str]
    generated_at: datetime
    videos: list[VideoSearchResultOut]
    channels: list[ChannelSearchResultOut]
    playlists: list[PlaylistSearchResultOut]
    # Below here: shelves that don't come from the interest list at all, so
    # they're filled in even for a library that has listed none.
    charts: list[PlaylistSearchResultOut]
    chart_artists: list[ChannelSearchResultOut]
    mood: MoodShelfOut | None


class VideoAddCreate(BaseModel):
    """One track being turned into a playable row.

    `channel_id` is the artist this track hangs off, sent by the client
    rather than resolved here: every row Explore renders — a song search
    result, a recommendation card, a track on an album or playlist page —
    already carries it, because YouTube Music returns it alongside the track
    (see music._artist_names). Resolving it server-side used to cost a
    yt-dlp call per "listen" click."""

    video_id: str
    title: str = Field(min_length=1, max_length=_CONTENT_TITLE_MAX_LENGTH)
    channel_id: str
    thumbnail_url: str | None = None
    duration_seconds: int | None = None
    channel_title: str | None = None


class VideoAddResult(BaseModel):
    content_id: int


class VideoBatchItem(VideoAddCreate):
    """One track of a remote listing being turned into a playable row. Same
    shape as a single add — the batch exists to save round trips, not to
    carry anything extra."""


class VideoBatchCreate(BaseModel):
    items: list[VideoBatchItem]


class VideoBatchResult(BaseModel):
    """The created/reused rows, in exactly the order they were sent — that
    order is the play queue, so it can't be reshuffled by the server."""

    content_ids: list[int]


class ArtistAddResult(BaseModel):
    artist: ArtistOut
    new_content_count: int


class RefreshResult(BaseModel):
    new_content_count: int


class SettingsOut(BaseModel):
    audio_quality: str
    refresh_interval_minutes: int
    interests: list[str]


class SettingsUpdate(BaseModel):
    audio_quality: str | None = None
    refresh_interval_minutes: int | None = None
    # Always the complete list, never a single tag to add or remove: the
    # Settings editor holds the whole list client-side anyway, and a
    # whole-list PUT means add, remove and reorder are one code path instead
    # of three endpoints.
    interests: list[str] | None = None
