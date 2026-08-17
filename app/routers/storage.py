import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.config import settings
from app.deps import get_current_profile, get_db, require_login
from app.formatting import format_size, safe_filename
from app.models import Content, User
from app.storage import EXPORT_TEMP_SUFFIX, clear_all

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_login)])


@router.delete("")
def clear_storage(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> dict[str, int]:
    cleared = clear_all(db, profile.id)
    return {"cleared": cleared}


def _archive_name(title: str, suffix: str, used: set[str]) -> str:
    """A unique entry name inside the archive."""
    stem = safe_filename(title)
    name = stem + suffix
    counter = 2
    while name in used:
        name = f"{stem} ({counter}){suffix}"
        counter += 1
    used.add(name)
    return name


@router.get("/export")
def export_all(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> FileResponse:
    """Every downloaded track as one zip.

    Built on disk and streamed from there, not assembled in memory. It used to
    be written into an `io.BytesIO`, which — with ZIP_STORED, i.e. no
    compression at all — meant the process allocated the full size of every
    downloaded file for the duration of one request. A 1 GB library made
    "Export all" a one-click 1 GB allocation, unbounded and one button away in
    the Downloads modal.

    The temp file lives beside the audio rather than in the system temp dir:
    it is the same order of magnitude as the content itself, and /tmp is
    routinely a small tmpfs, so writing it there would just move the same
    failure onto RAM again.
    """
    rows = (
        db.query(Content).filter(Content.user_id == profile.id, Content.status == "ready").all()
    )
    exportable = [(row, Path(row.file_path)) for row in rows if row.file_path]
    exportable = [(row, path) for row, path in exportable if path.is_file()]
    if not exportable:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Nothing to export")

    # ZIP_STORED means the archive is the sum of its inputs, so this is an
    # accurate requirement rather than an estimate. Checked against real free
    # space instead of an arbitrary cap: the only thing a fixed limit would
    # add is refusing exports that would have worked fine.
    needed = sum(path.stat().st_size for _row, path in exportable)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(settings.storage_dir).free
    if needed > free:
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=f"Not enough free disk space to build the export ({format_size(needed)} needed)",
        )

    # mkstemp rather than NamedTemporaryFile: the archive is written by
    # ZipFile and then handed to FileResponse to read back, so nothing here
    # wants an open handle in between — only a path nobody else can claim.
    descriptor, temp_name = tempfile.mkstemp(dir=settings.storage_dir, suffix=EXPORT_TEMP_SUFFIX)
    os.close(descriptor)
    archive = Path(temp_name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            used_names: set[str] = set()
            for row, path in exportable:
                zf.write(path, arcname=_archive_name(row.title, path.suffix, used_names))
    except Exception:
        archive.unlink(missing_ok=True)
        raise

    return FileResponse(
        archive,
        media_type="application/zip",
        filename="spotea-downloads.zip",
        # Removed once the response has been sent — including when the client
        # disconnects partway, which is the case that would otherwise leave a
        # full copy of the library on disk.
        background=BackgroundTask(_remove_archive, archive),
    )


def _remove_archive(archive: Path) -> None:
    try:
        archive.unlink(missing_ok=True)
    except OSError:
        # A leftover is swept later (see EXPORT_TEMP_SUFFIX); failing to
        # delete it must not turn a successful download into an error.
        logger.warning("Could not remove export archive %s", archive)
