from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.content_query import query_content_ids, query_content_page
from app.database import SessionLocal
from app.deps import get_current_profile, get_db, require_login
from app.downloader import DownloadError, VideoUnavailableError, download_audio
from app.feed_sync import cache_thumbnail
from app.formatting import safe_filename
from app.models import Content, Feed, User
from app.page_context import playlist_filter
from app.progress import ProgressRegistry
from app.schemas import ContentOut, ContentPageOut, FavoriteOut, QueueOut, SavedOut, StatusOut
from app.storage import unlink_if_unshared
from app.timeutil import utcnow
from app.youtube.urls import VIDEO_ID_RE

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_login)])

# In-memory only: fine for a single-process app, and progress ticks too
# frequently to justify a DB write on every hook call. Entries are dropped
# as soon as the download settles (see _run_download's finally), so the
# registry's expiry never actually comes into play here — it's the same
# type the backfill/import trackers use (see app/progress.py) rather than a
# fourth hand-rolled dict.
_download_progress: ProgressRegistry[int, tuple[str, int | None]] = ProgressRegistry()

# Keyed by extension rather than the configured AUDIO_FORMAT so files
# downloaded under a previous format setting still get a correct Content-Type.
AUDIO_MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}


def _get_content_or_404(db: Session, content_id: int, user_id: int) -> Content:
    content = (
        db.query(Content).filter(Content.id == content_id, Content.user_id == user_id).first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return content


def _set_download_outcome(content_id: int, **fields) -> None:
    """Write a finished download's result on a session of this task's own.

    Deliberately NOT the request's `Depends(get_db)` session: since FastAPI
    0.106 a yield-dependency's exit code (get_db's `db.close()`) runs before
    the response is sent, i.e. before any BackgroundTask starts — so the
    session handed to a background task is already closed. SQLAlchemy
    happens to re-acquire a connection on next use, which is the only reason
    passing it here ever appeared to work; nothing guarantees that keeps
    being true. Opening a session inside the task is also what makes it safe
    to run for minutes on a worker thread, independent of the request that
    scheduled it."""
    with SessionLocal() as db:
        content = db.get(Content, content_id)
        if content is None:
            return
        for field, value in fields.items():
            setattr(content, field, value)
        db.commit()


def _run_download(content_id: int, video_id: str, quality: str) -> None:
    def on_progress(phase: str, percent: int | None) -> None:
        _download_progress.set(content_id, (phase, percent))

    try:
        file_path = download_audio(video_id, quality=quality, on_progress=on_progress)
    except VideoUnavailableError as exc:
        # Settled, not provisional — start_download won't attempt it again
        # and the player skips it without waiting. See Content.is_unavailable.
        _set_download_outcome(
            content_id, status="error", error_message=str(exc)[:1000], is_unavailable=True
        )
        return
    except DownloadError as exc:
        _set_download_outcome(content_id, status="error", error_message=str(exc)[:1000])
        return
    finally:
        _download_progress.discard(content_id)

    # Measured here, once, rather than on every render that wants a storage
    # total — see Content.file_size_bytes. The file is guaranteed to exist
    # at this point (download_audio raises otherwise), but a stat failure
    # still shouldn't lose the download itself, so it falls back to
    # "unmeasured" and lets collect_usage's lazy backfill retry later.
    try:
        size_bytes = file_path.stat().st_size
    except OSError:
        size_bytes = None

    _set_download_outcome(
        content_id,
        status="ready",
        file_path=str(file_path),
        file_size_bytes=size_bytes,
        downloaded_at=utcnow(),
        # A row can only reach here after being playable, so whatever made it
        # unavailable before (a licence that has since landed in this
        # country, a re-upload) no longer holds.
        is_unavailable=False,
    )


@router.get("", response_model=ContentPageOut)
def list_content(
    page: int = 1,
    filter: str = "",
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> ContentPageOut:
    items, page, total_pages = query_content_page(db, profile.id, page, filter)
    return ContentPageOut(
        items=[ContentOut.from_content(c) for c in items],
        page=page,
        total_pages=total_pages,
    )


# Both registered ahead of /{content_id} for the same reason
# /recently-played is (see its comment below) — three path segments can't
# collide with a one-segment route, but keeping every literal-prefixed route
# above the catch-all is what stops the next one from being subtly shadowed.
@router.get("/queue/channel/{feed_id}", response_model=QueueOut)
def channel_queue(
    feed_id: int,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> QueueOut:
    """Every track in one channel, in the order its detail panel lists them.

    The detail panel is paginated and the queue deliberately isn't: "Play
    all" on a channel means the channel, not the twenty rows that happen to
    be on screen.
    """
    exists = db.query(Feed.id).filter(Feed.id == feed_id, Feed.user_id == profile.id).first()
    if exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    return QueueOut(ids=query_content_ids(db, profile.id, feed_id=feed_id))


@router.get("/queue/playlist/{kind}", response_model=QueueOut)
def playlist_queue(
    kind: str,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> QueueOut:
    """Same, for one of the four pinned virtual playlists."""
    filter_value = playlist_filter(kind)
    if filter_value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown playlist")
    return QueueOut(ids=query_content_ids(db, profile.id, filter=filter_value))


@router.get("/{content_id}", response_model=ContentOut)
def get_content(
    content_id: int,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> ContentOut:
    """Single-item fetch — used by the Home player overlay (see home/overlay.js's
    openPlayer) to populate itself for a track without a full page
    navigation. joinedload's needed here (unlike _get_content_or_404, whose
    other callers never touch .feed) since channel_title comes from it."""
    content = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.id == content_id, Content.user_id == profile.id)
        .first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")

    # Caches for next time only, same as pages.py's _queue_thumbnail_caching
    # — this response still carries whatever thumbnail_url is on file now.
    if content.thumbnail_url and "ytimg.com" in content.thumbnail_url:
        background_tasks.add_task(cache_thumbnail, content.video_id, content.thumbnail_url)

    return ContentOut.from_content(content)


# Registered ahead of the /{content_id}/... routes below — a literal segment
# placed after them would otherwise be swallowed by /{content_id} (Starlette
# matches path structure first and only fails int conversion once the
# request is already committed to that route), and no request would ever
# reach this one.
@router.delete("/recently-played")
def clear_recently_played(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> dict[str, int]:
    cleared = (
        db.query(Content)
        .filter(Content.user_id == profile.id, Content.last_played_at.isnot(None))
        .update({"last_played_at": None}, synchronize_session=False)
    )
    db.commit()
    return {"cleared": cleared}


@router.post("/{content_id}/download", response_model=StatusOut)
def start_download(
    content_id: int,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> StatusOut:
    content = _get_content_or_404(db, content_id, profile.id)

    if content.status == "downloading":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already downloading")

    # Already on disk — say so instead of fetching it a second time. The
    # player never asks for a ready track (prepareAudio checks its own
    # dataset first), but the queue's one-track-ahead prefetch
    # (home/overlay.js) fires without knowing the next track's status, and
    # re-downloading everything it looks at would be the opposite of what
    # it's for. Still re-downloads when the row says ready but the file is
    # gone (storage cleared out from under us), which is the one case where
    # taking "ready" at face value would strand playback.
    if content.status == "ready" and content.file_path and Path(content.file_path).exists():
        return StatusOut(id=content.id, status=content.status, error_message=None)

    # YouTube has already told us, on every client, that it won't serve this
    # one (see Content.is_unavailable). Answering from the row costs nothing
    # and keeps the queue's prefetch — which fires for whatever is next
    # without knowing anything about it — from re-running the whole ladder
    # against YouTube on every pass over a track that can't work. DELETE
    # /content/{id} clears the flag, which is the way back if this ever
    # becomes wrong.
    if content.is_unavailable:
        return StatusOut(
            id=content.id,
            status=content.status,
            error_message=content.error_message,
            is_unavailable=True,
        )

    if not VIDEO_ID_RE.match(content.video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video id")

    content.status = "downloading"
    content.error_message = None
    db.commit()

    background_tasks.add_task(_run_download, content.id, content.video_id, profile.audio_quality)

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)


@router.get("/{content_id}/status", response_model=StatusOut)
def get_status(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> StatusOut:
    content = _get_content_or_404(db, content_id, profile.id)
    phase, percent = _download_progress.get(content_id, (None, None))
    return StatusOut(
        id=content.id,
        status=content.status,
        error_message=content.error_message,
        progress_percent=percent,
        phase=phase,
        is_unavailable=content.is_unavailable,
    )


@router.post("/{content_id}/favorite", response_model=FavoriteOut)
def add_favorite(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> FavoriteOut:
    content = _get_content_or_404(db, content_id, profile.id)
    content.is_favorite = True
    # Favoriting an Explore preview is a strong enough "keep this" signal on
    # its own to promote it out of preview status.
    content.is_preview = False
    db.commit()
    return FavoriteOut(id=content.id, is_favorite=content.is_favorite)


@router.delete("/{content_id}/favorite", response_model=FavoriteOut)
def remove_favorite(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> FavoriteOut:
    content = _get_content_or_404(db, content_id, profile.id)
    content.is_favorite = False
    db.commit()
    return FavoriteOut(id=content.id, is_favorite=content.is_favorite)


@router.post("/{content_id}/save", response_model=SavedOut)
def add_saved(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> SavedOut:
    content = _get_content_or_404(db, content_id, profile.id)
    content.is_saved = True
    # Same auto-promote reasoning as add_favorite above.
    content.is_preview = False
    db.commit()
    return SavedOut(id=content.id, is_saved=content.is_saved)


@router.delete("/{content_id}/save", response_model=SavedOut)
def remove_saved(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> SavedOut:
    content = _get_content_or_404(db, content_id, profile.id)
    content.is_saved = False
    db.commit()
    return SavedOut(id=content.id, is_saved=content.is_saved)


@router.post("/{content_id}/played", status_code=status.HTTP_204_NO_CONTENT)
def mark_played(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> None:
    """Record a play that the stream request itself deliberately didn't.

    The player reads the next queued track ahead of time, while the current
    one is still playing, so that the handoff needs no network (see
    player.js's deck helpers). That read-ahead asks for ?preload=1 precisely
    so it doesn't count as listening — the listener may well skip past it, and
    it has no business in Recently Played until it actually starts. This is
    what the player calls when one does.
    """
    content = _get_content_or_404(db, content_id, profile.id)
    content.last_played_at = utcnow()
    db.commit()


@router.get("/{content_id}/stream")
def stream_content(
    content_id: int,
    download: bool = False,
    preload: bool = False,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> FileResponse:
    content = _get_content_or_404(db, content_id, profile.id)

    if content.status != "ready" or not content.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Content is not ready")

    file_path = Path(content.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing on disk")

    # Skipped for a plain file export (?download=1) and for the player's
    # read-ahead of the next queued track (?preload=1) — neither is anyone
    # actually listening. A preloaded track that does go on to play reports
    # itself through mark_played above.
    if not download and not preload:
        content.last_played_at = utcnow()
        db.commit()

    media_type = AUDIO_MEDIA_TYPES.get(file_path.suffix, "application/octet-stream")
    return FileResponse(
        file_path, media_type=media_type, filename=safe_filename(content.title) + file_path.suffix
    )


@router.delete("/{content_id}", response_model=StatusOut)
def delete_content(
    content_id: int, profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> StatusOut:
    content = _get_content_or_404(db, content_id, profile.id)

    if content.file_path:
        unlink_if_unshared(db, content.file_path, content.id)

    content.status = "not_downloaded"
    content.file_path = None
    content.file_size_bytes = None
    content.error_message = None
    content.downloaded_at = None
    # Removing a download is the app's only "start over on this track"
    # action, so it doubles as the way to re-attempt one that was written off
    # as unavailable — YouTube licensing does change, and a flag with no way
    # back would make that permanent on our side even after it stopped being
    # true on theirs.
    content.is_unavailable = False
    db.commit()

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)
