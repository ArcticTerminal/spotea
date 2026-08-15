import threading
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.content_query import query_content_page
from app.database import SessionLocal
from app.deps import get_current_profile, get_db, require_login
from app.downloader import DownloadError, download_audio
from app.feed_sync import cache_thumbnail
from app.formatting import safe_filename
from app.models import Content, User
from app.progress import ProgressRegistry
from app.schemas import ContentOut, ContentPageOut, FavoriteOut, SavedOut, StatusOut
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

# Bumped on every dispatch (a normal start or a restart — see
# restart_download). _run_download checks its own generation against the
# current one before writing anything, so a superseded attempt's eventual
# result — success, failure, or never finishing at all — can't clobber a
# newer attempt's. A plain dict rather than ProgressRegistry: a generation is
# meant to keep counting for the life of the row, not expire and reset.
_download_generation: dict[int, int] = {}
_download_generation_lock = threading.Lock()


def _next_generation(content_id: int) -> int:
    with _download_generation_lock:
        generation = _download_generation.get(content_id, 0) + 1
        _download_generation[content_id] = generation
        return generation


def _is_current_generation(content_id: int, generation: int) -> bool:
    return _download_generation.get(content_id) == generation

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


def _run_download(content_id: int, video_id: str, quality: str, generation: int) -> None:
    def is_current() -> bool:
        return _is_current_generation(content_id, generation)

    def on_progress(phase: str, percent: int | None) -> None:
        # A restart bumps the generation and discards this entry — if a
        # superseded attempt's hook still fires after that, letting it write
        # here would stomp the new attempt's own progress with stale numbers.
        if is_current():
            _download_progress.set(content_id, (phase, percent))

    try:
        file_path = download_audio(video_id, quality=quality, on_progress=on_progress)
    except DownloadError as exc:
        if is_current():
            _set_download_outcome(content_id, status="error", error_message=str(exc)[:1000])
        return
    finally:
        if is_current():
            _download_progress.discard(content_id)

    if not is_current():
        # Superseded by a restart while this attempt was still running.
        # Whatever it just produced — even a real, valid file — isn't what
        # the client asked to wait for anymore; the generation that's
        # actually current owns the DB row now, not this one.
        return

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

    if not VIDEO_ID_RE.match(content.video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video id")

    content.status = "downloading"
    content.error_message = None
    db.commit()

    generation = _next_generation(content.id)
    background_tasks.add_task(_run_download, content.id, content.video_id, profile.audio_quality, generation)

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)


@router.post("/{content_id}/download/restart", response_model=StatusOut)
def restart_download(
    content_id: int,
    background_tasks: BackgroundTasks,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> StatusOut:
    """Abandons whatever download attempt is currently running for this
    content (if any) and dispatches a fresh one — the client's fallback for
    a download that's shown no progress at all for a couple of seconds (see
    player.js). Unlike POST /download, this never 409s on "already
    downloading" — that's the whole point.

    The old attempt isn't killed; yt-dlp can't be cleanly interrupted from
    here. It's voided instead: _run_download compares its own generation
    against the current one before writing anything, so whatever the old
    attempt eventually does — succeed, fail, or hang forever — never reaches
    the DB or the progress the client is polling. There's a small residual
    window where the old and new attempt could both be writing the same
    output file at once; accepted as unlikely enough in practice not to be
    worth the bigger per-attempt-temp-path change that would rule it out.
    """
    content = _get_content_or_404(db, content_id, profile.id)

    if content.status == "ready":
        # Already finished (a race between this firing and the original
        # attempt actually succeeding) — nothing to restart.
        return StatusOut(id=content.id, status=content.status, error_message=content.error_message)

    if not VIDEO_ID_RE.match(content.video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video id")

    content.status = "downloading"
    content.error_message = None
    db.commit()

    generation = _next_generation(content.id)
    _download_progress.discard(content.id)
    background_tasks.add_task(_run_download, content.id, content.video_id, profile.audio_quality, generation)

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


@router.get("/{content_id}/stream")
def stream_content(
    content_id: int,
    download: bool = False,
    profile: User = Depends(get_current_profile),
    db: Session = Depends(get_db),
) -> FileResponse:
    content = _get_content_or_404(db, content_id, profile.id)

    if content.status != "ready" or not content.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Content is not ready")

    file_path = Path(content.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing on disk")

    # Skipped for a plain file export (?download=1) — that's not the user
    # actually listening, so it shouldn't count toward "recently played".
    if not download:
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
    db.commit()

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)
