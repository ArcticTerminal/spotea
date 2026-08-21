"""Timed lyrics for the player's Lyrics tab, fetched once per recording.

The whole design here follows from two measurements taken before it was
written (see music.fetch_timed_lyrics for the full numbers):

  * a miss costs **two** live YouTube requests, and
  * about two thirds of tracks have no lyrics at all.

So this never runs on its own. Nothing fetches lyrics because a track
started playing; the only caller is the route behind the Lyrics tab, and
that tab is only opened deliberately. On a library where nobody ever opens
it, this module makes zero requests for the life of the install.

The other half of that is caching the *absence*. A row with `lines` NULL
means "asked YouTube, there are none" — the common answer — and it is what
stops the two-request miss repeating every time the tab is opened on a track
that will never have lyrics.
"""

import json
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TrackLyrics
from app.youtube.music import fetch_timed_lyrics

logger = logging.getLogger(__name__)


def _to_payload(row: TrackLyrics) -> dict:
    """The shape the client renders. `lines: null` is "no lyrics for this
    track", which the panel says out loud rather than leaving blank."""
    if row.lines is None:
        return {"lines": None, "source": None}
    return {"lines": json.loads(row.lines), "source": row.source}


def lyrics_for(db: Session, video_id: str) -> dict:
    """This track's timed lyrics, from the cache or from YouTube Music.

    Always returns the payload shape above — a track with no lyrics is a
    normal answer here, not an error, and the caller has nothing different
    to do with it.
    """
    cached = db.get(TrackLyrics, video_id)
    if cached is not None:
        return _to_payload(cached)

    fetched = fetch_timed_lyrics(video_id)
    row = TrackLyrics(
        video_id=video_id,
        lines=(
            json.dumps(
                [
                    {"text": line.text, "start_ms": line.start_ms, "end_ms": line.end_ms}
                    for line in fetched.lines
                ]
            )
            if fetched
            else None
        ),
        source=fetched.source if fetched else None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Two tabs asking for the same track at once. The other request
        # already stored the same answer, so take theirs rather than failing
        # a read that has the data in hand either way.
        db.rollback()
        existing = db.get(TrackLyrics, video_id)
        if existing is not None:
            return _to_payload(existing)
        raise

    return _to_payload(row)
