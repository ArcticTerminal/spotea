"""The background work a newly followed artist needs: their first sync.

It used to happen inline, inside POST /artists, and it used to be followed by
a one-time full-history scan of the channel. Both are gone: the scan
because an artist's back catalogue is browsable on their profile and
following means "tell me when they release something", and the inline part
because none of it has to happen before the answer — measured per channel,
1.32s to read the durations and 0.84s for the avatar, against 0.11s for the
sync itself. The artist row is what Library renders a card from, and the card
already knows how to say it is still filling in.
"""

import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Artist
from app.progress import ProgressRegistry
from app.services.artist_sync import apply_artist_data, fetch_artist_data

logger = logging.getLogger(__name__)


# In-memory only: fine for a single-process app, mirrors the download-progress
# pattern in content.py. Keyed by artist_id. Terminal phase is "done" (done ==
# total, possibly 0) rather than removing the entry — small/fast channels can
# finish scanning+saving in well under a second, faster than the client's
# first poll; if we just deleted the key, that client would see "nothing
# happening" and have no way to tell "finished with nothing new" apart from
# "never ran". Entries are dropped when their artist is deleted (see routers/artists.py's delete_feed).
sync_progress: ProgressRegistry[int, tuple[str, int, int]] = ProgressRegistry()

# The phase this is actually working in. The registry keeps terminal entries
# readable for a while after the fact (see progress.py), so "has an entry"
# and "is running" are different questions.
ACTIVE_PHASES = frozenset({"syncing"})


def syncing_artist_ids(feed_ids: Iterable[int]) -> set[int]:
    """Which of `feed_ids` have a history scan running right now.

    A dict lookup each, no query — the registry is in-memory. Library's grid
    asks this for every card it renders (see page_context.library_context) so
    a channel still filling in can say so on its own card, which is what let
    the onboarding wizard stop making anyone wait for a backfill at all: a
    scan of a 6,500-video channel is minutes long, and nothing on the first
    screen after onboarding needs it — the RSS sync that POST /artists already
    did before answering is what puts the channel's recent uploads there.
    """
    return {
        artist_id
        for artist_id in feed_ids
        if (sync_progress.get(artist_id) or ("", 0, 0))[0] in ACTIVE_PHASES
    }


def mark_syncing(artist_id: int) -> None:
    """Puts a artist into the "filling in" state without doing any of the work.

    Called by POST /artists before it answers, so the card Library renders off
    that answer is already reporting itself — a background task cannot be
    relied on to have started by the time the client comes back asking for
    fragments, and losing that race would render a confident "0 videos" and
    then never poll, because polling only happens while such a card is on
    the page (see home/library.js). run_initial_sync sets it again for every
    other caller; the registry takes the same value twice happily."""
    sync_progress.set(artist_id, ("syncing", 0, 0))


def run_initial_sync(artist_id: int, db: Session) -> None:
    """A newly followed artist's first sync.

    Registered as "syncing" before anything is fetched, so the card Library
    renders the moment POST /artists answers already says "Fetching uploads…"
    instead of sitting there claiming zero videos. That claim was the reason
    this had to be inline before: a card that appears empty and stays empty
    for two seconds reads as an artist that failed to add.
    """
    artist = db.get(Artist, artist_id)
    if artist is None:
        return

    mark_syncing(artist_id)
    try:
        result = fetch_artist_data(artist.browse_id, artist.release_snapshot, artist.avatar_url)
        apply_artist_data(db, artist, result)
    except Exception:
        # fetch_artist_data already flattens an unreadable page into "nothing",
        # so anything reaching here is unexpected — and it must still clear
        # the phase, or the card says "Fetching uploads…" forever.
        logger.exception("Initial sync failed for artist %s", artist_id)
        sync_progress.set(artist_id, ("done", 0, 0))
        return

    sync_progress.set(artist_id, ("done", 0, 0))


def run_initial_sync_task(artist_id: int) -> None:
    """BackgroundTasks entry point for run_initial_sync, on a session of its
    own.

    The request's `Depends(get_db)` session must not be used here: since
    FastAPI 0.106 a yield-dependency's exit code (get_db's `db.close()`) runs
    before the response is sent, so it is already closed by the time a
    background task starts."""
    with SessionLocal() as db:
        run_initial_sync(artist_id, db)
