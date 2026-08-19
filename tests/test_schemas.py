"""Field limits on request bodies (app/schemas.py).

SQLite doesn't enforce a column's declared VARCHAR length, so these Field
bounds are the only thing standing between a request and an unbounded write.
"""

import pytest
from pydantic import ValidationError

from app.schemas import _CONTENT_TITLE_MAX_LENGTH as TITLE_MAX
from app.schemas import _URL_MAX_LENGTH as URL_MAX
from app.schemas import FeedCreate, VideoAddCreate


@pytest.mark.parametrize("model, field, limit", [
    (FeedCreate, "channel_url", URL_MAX),
])
def test_a_value_at_the_limit_is_accepted(model, field, limit):
    model(**{field: "x" * limit})


@pytest.mark.parametrize("model, field, limit", [
    (FeedCreate, "channel_url", URL_MAX),
])
def test_one_character_past_the_limit_is_rejected(model, field, limit):
    with pytest.raises(ValidationError):
        model(**{field: "x" * (limit + 1)})


@pytest.mark.parametrize("model, field", [
    (FeedCreate, "channel_url"),
])
def test_an_empty_value_is_rejected(model, field):
    with pytest.raises(ValidationError):
        model(**{field: ""})


def test_a_video_title_at_the_content_column_width_is_accepted():
    """Matches Content.title's own String(500) column, so a value that fits
    the schema always fits the row it's about to be inserted into."""
    VideoAddCreate(video_id="dQw4w9WgXcQ", title="x" * TITLE_MAX, channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA")


def test_a_video_title_past_the_content_column_width_is_rejected():
    with pytest.raises(ValidationError):
        VideoAddCreate(video_id="dQw4w9WgXcQ", title="x" * (TITLE_MAX + 1), channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA")
