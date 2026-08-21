from fastapi.templating import Jinja2Templates

from app.formatting import format_duration, format_size
from app.images import proxied_image_url
from app.youtube.urls import video_still_url


def track_cover(item) -> str | None:
    """What to draw for a track: its own cover, or the video's still.

    A track is normally stored with the square art YouTube Music serves for
    it. Some are not, and nothing ever revisits a row once written — so a
    track that arrived without a cover kept a blank square forever, which is
    what this fixes. Measured on the live library: seventeen rows, one whole
    album, materialized eight hours before the release-cover fallback in
    music.fetch_release landed (see its comment — an album's track entries
    carry `thumbnails: None`, since the release has one cover rather than
    each track having its own). That hole is closed at the source now; these
    rows are simply older than the fix and cannot heal themselves.

    Derived at render time rather than backfilled into the column. The
    fallback needs no network call, so storing it would buy nothing and cost
    the ability to tell a real cover from a stand-in — and any row that slips
    through in future is covered without a second repair.

    Accepts an ORM Content row or one of the remote dataclasses; both carry
    `thumbnail_url` and `video_id`, which is all this reads.
    """
    if item.thumbnail_url:
        return item.thumbnail_url
    still = video_still_url(item.video_id)
    # Proxied like every other remote cover: i.ytimg.com is allowed by the
    # img-src CSP, but going through /image-proxy keeps this on the same
    # path as the rest and dodges the ORB refusals that path exists for.
    return proxied_image_url(still) if still else None


# One Jinja environment for the whole app. There used to be two — one in
# routers/pages.py with these filters registered, one in routers/auth.py
# without them — so which templates could use `| duration` / `| filesize`
# depended on which router happened to render them. Nothing caught that,
# because the only templates auth.py renders (login, register) don't use
# either filter; a page moved between routers, or a new filter added to the
# "wrong" instance, would have failed at render time with no warning.
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["duration"] = format_duration
templates.env.filters["filesize"] = format_size
templates.env.filters["cover"] = track_cover
