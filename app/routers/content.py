import datetime as dt
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, joinedload

from app.deps import get_db, require_login
from app.downloader import DownloadError, download_audio
from app.models import Content
from app.rss import VIDEO_ID_RE
from app.schemas import ContentOut, FavoriteOut, SavedOut, StatusOut

router = APIRouter(prefix="/content", tags=["content"], dependencies=[Depends(require_login)])

DEFAULT_USER_ID = 1


def _get_content_or_404(db: Session, content_id: int) -> Content:
    content = (
        db.query(Content).filter(Content.id == content_id, Content.user_id == DEFAULT_USER_ID).first()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Content not found")
    return content


def _run_download(content_id: int, video_id: str, db: Session) -> None:
    try:
        file_path = download_audio(video_id)
    except DownloadError as exc:
        content = db.get(Content, content_id)
        if content is not None:
            content.status = "error"
            content.error_message = str(exc)[:1000]
            db.commit()
        return

    content = db.get(Content, content_id)
    if content is not None:
        content.status = "ready"
        content.file_path = str(file_path)
        content.downloaded_at = dt.datetime.utcnow()
        db.commit()


@router.get("", response_model=list[ContentOut])
def list_content(db: Session = Depends(get_db)) -> list[ContentOut]:
    rows = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.user_id == DEFAULT_USER_ID)
        .order_by(Content.published_at.desc())
        .all()
    )
    return [
        ContentOut(
            id=c.id,
            feed_id=c.feed_id,
            channel_title=c.feed.channel_title,
            video_id=c.video_id,
            title=c.title,
            thumbnail_url=c.thumbnail_url,
            duration_seconds=c.duration_seconds,
            published_at=c.published_at,
            status=c.status,
            added_at=c.added_at,
            is_favorite=c.is_favorite,
            is_saved=c.is_saved,
        )
        for c in rows
    ]


@router.post("/{content_id}/download", response_model=StatusOut)
def start_download(
    content_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
) -> StatusOut:
    content = _get_content_or_404(db, content_id)

    if content.status == "downloading":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already downloading")

    if not VIDEO_ID_RE.match(content.video_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid video id")

    content.status = "downloading"
    content.error_message = None
    db.commit()

    background_tasks.add_task(_run_download, content.id, content.video_id, db)

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)


@router.get("/{content_id}/status", response_model=StatusOut)
def get_status(content_id: int, db: Session = Depends(get_db)) -> StatusOut:
    content = _get_content_or_404(db, content_id)
    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)


@router.post("/{content_id}/favorite", response_model=FavoriteOut)
def add_favorite(content_id: int, db: Session = Depends(get_db)) -> FavoriteOut:
    content = _get_content_or_404(db, content_id)
    content.is_favorite = True
    db.commit()
    return FavoriteOut(id=content.id, is_favorite=content.is_favorite)


@router.delete("/{content_id}/favorite", response_model=FavoriteOut)
def remove_favorite(content_id: int, db: Session = Depends(get_db)) -> FavoriteOut:
    content = _get_content_or_404(db, content_id)
    content.is_favorite = False
    db.commit()
    return FavoriteOut(id=content.id, is_favorite=content.is_favorite)


@router.post("/{content_id}/save", response_model=SavedOut)
def add_saved(content_id: int, db: Session = Depends(get_db)) -> SavedOut:
    content = _get_content_or_404(db, content_id)
    content.is_saved = True
    db.commit()
    return SavedOut(id=content.id, is_saved=content.is_saved)


@router.delete("/{content_id}/save", response_model=SavedOut)
def remove_saved(content_id: int, db: Session = Depends(get_db)) -> SavedOut:
    content = _get_content_or_404(db, content_id)
    content.is_saved = False
    db.commit()
    return SavedOut(id=content.id, is_saved=content.is_saved)


@router.get("/{content_id}/stream")
def stream_content(content_id: int, db: Session = Depends(get_db)) -> FileResponse:
    content = _get_content_or_404(db, content_id)

    if content.status != "ready" or not content.file_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Content is not ready")

    file_path = Path(content.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File missing on disk")

    return FileResponse(file_path, media_type="audio/mpeg", filename=file_path.name)


@router.delete("/{content_id}", response_model=StatusOut)
def delete_content(content_id: int, db: Session = Depends(get_db)) -> StatusOut:
    content = _get_content_or_404(db, content_id)

    if content.file_path:
        file_path = Path(content.file_path)
        if file_path.exists():
            file_path.unlink()

    content.status = "not_downloaded"
    content.file_path = None
    content.error_message = None
    content.downloaded_at = None
    db.commit()

    return StatusOut(id=content.id, status=content.status, error_message=content.error_message)
