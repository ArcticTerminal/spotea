"""Field limits on request bodies (app/schemas.py).

SQLite doesn't enforce a column's declared VARCHAR length, so these Field
bounds were the only thing standing between a request and either an unbounded
write or an unbounded amount of downstream work — a BulkImportCreate.urls line
becomes a yt-dlp resolution, so an unbounded paste is an unbounded number of
outbound requests, not just a big string in the database.
"""

import pytest
from pydantic import ValidationError

from app.schemas import _BULK_IMPORT_MAX_LINES as MAX_LINES
from app.schemas import _CONTENT_TITLE_MAX_LENGTH as TITLE_MAX
from app.schemas import _PROFILE_NAME_MAX_LENGTH as NAME_MAX
from app.schemas import _URL_MAX_LENGTH as URL_MAX
from app.schemas import (
    BulkImportCreate,
    FeedCreate,
    ProfileCreate,
    ProfileUpdate,
    VideoAddCreate,
)


@pytest.mark.parametrize("model, field, limit", [
    (FeedCreate, "channel_url", URL_MAX),
    (ProfileCreate, "name", NAME_MAX),
    (ProfileUpdate, "name", NAME_MAX),
])
def test_a_value_at_the_limit_is_accepted(model, field, limit):
    model(**{field: "x" * limit})


@pytest.mark.parametrize("model, field, limit", [
    (FeedCreate, "channel_url", URL_MAX),
    (ProfileCreate, "name", NAME_MAX),
    (ProfileUpdate, "name", NAME_MAX),
])
def test_one_character_past_the_limit_is_rejected(model, field, limit):
    with pytest.raises(ValidationError):
        model(**{field: "x" * (limit + 1)})


@pytest.mark.parametrize("model, field", [
    (FeedCreate, "channel_url"),
    (ProfileCreate, "name"),
    (ProfileUpdate, "name"),
])
def test_an_empty_value_is_rejected(model, field):
    with pytest.raises(ValidationError):
        model(**{field: ""})


def test_a_video_title_at_the_content_column_width_is_accepted():
    """Matches Content.title's own String(500) column, so a value that fits
    the schema always fits the row it's about to be inserted into."""
    VideoAddCreate(video_id="dQw4w9WgXcQ", title="x" * TITLE_MAX)


def test_a_video_title_past_the_content_column_width_is_rejected():
    with pytest.raises(ValidationError):
        VideoAddCreate(video_id="dQw4w9WgXcQ", title="x" * (TITLE_MAX + 1))


def test_a_bulk_import_at_the_line_cap_is_accepted():
    BulkImportCreate(urls="\n".join(f"@channel{i}" for i in range(MAX_LINES)))


def test_a_bulk_import_one_line_over_the_cap_is_rejected():
    """The finding this guards against: a pasted list resolving each line
    with its own yt-dlp lookup (services/bulk_import.py), so an unbounded
    paste is an unbounded number of outbound requests to YouTube, not just a
    big string sitting in memory."""
    with pytest.raises(ValidationError) as caught:
        BulkImportCreate(urls="\n".join(f"@channel{i}" for i in range(MAX_LINES + 1)))

    assert str(MAX_LINES) in str(caught.value)


def test_a_bulk_import_of_many_short_lines_is_capped_by_line_count_not_bytes():
    """10,000 one-character lines is well under any reasonable byte cap but
    is still 10,000 lines — the byte limit alone wouldn't have caught this."""
    with pytest.raises(ValidationError):
        BulkImportCreate(urls="\n".join("x" for _ in range(10_000)))


def test_an_empty_bulk_import_is_rejected():
    with pytest.raises(ValidationError):
        BulkImportCreate(urls="")
