"""A write-only breadcrumb sink for the one thing server logs can't see.

Every playback failure this app has had that was hard to pin down was a
client-side one: playback that didn't advance, a download that finished but
never started playing, a play() the browser quietly refused. From the
server's side all of those look identical to "the user stopped listening" —
there is no request to log, which is exactly the problem.

So the player posts a breadcrumb at each step of a track handoff (see
player.js's reportPlayback) and this writes it to the same log everything
else goes to, where it can be read next to the download it belongs with.

Deliberately minimal: no storage, no read side, no UI. It exists so that the
next occurrence of "it didn't move to the next song" can be settled by
looking rather than guessed at, and the volume is a handful of lines per
track played.
"""

import logging

from fastapi import APIRouter, Depends, Request

from app.deps import require_login

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[Depends(require_login)])

# sendBeacon can't be given a timeout and won't tell the page whether it
# arrived, so a page being frozen mid-handoff may flush a backlog at once.
# Bounded so a bug on the client side can't turn into unbounded log volume.
MAX_EVENTS_PER_REQUEST = 20


@router.post("/playback", status_code=204)
async def record_playback_events(request: Request) -> None:
    """Log a batch of player breadcrumbs.

    Takes the raw body rather than a schema: this is diagnostic output whose
    shape follows whatever the player currently finds worth recording, and a
    breadcrumb that fails validation is a breadcrumb lost at precisely the
    moment it mattered. Malformed input is dropped, never raised — the player
    fires these with sendBeacon and can neither see nor act on an error.
    """
    try:
        events = await request.json()
    except ValueError:
        return
    if not isinstance(events, list):
        events = [events]
    for event in events[:MAX_EVENTS_PER_REQUEST]:
        logger.info("playback: %s", event)
