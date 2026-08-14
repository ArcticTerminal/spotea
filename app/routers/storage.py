import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_current_profile, get_db, require_login
from app.formatting import format_size, safe_filename
from app.models import Content, User
from app.schemas import StorageItemOut, StorageUsageOut
from app.storage import clear_all, collect_usage

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_login)])


@router.get("", response_model=StorageUsageOut)
def get_usage(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> StorageUsageOut:
    """Same data pages.py's settings route renders server-side — used to
    live-patch the Settings/Downloads storage summary after a track finishes
    downloading mid-session, instead of leaving it stale until reload (see
    app.js's refreshStorageUsage)."""
    usage = collect_usage(db, profile.id)
    return StorageUsageOut(
        total_formatted=format_size(usage.total_bytes),
        count=usage.count,
        items=[
            StorageItemOut(
                id=item.id,
                title=item.title,
                channel_title=item.channel_title,
                size_formatted=format_size(item.size_bytes),
            )
            for item in usage.items
        ],
    )


@router.delete("")
def clear_storage(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> dict[str, int]:
    cleared = clear_all(db, profile.id)
    return {"cleared": cleared}


@router.get("/export")
def export_all(
    profile: User = Depends(get_current_profile), db: Session = Depends(get_db)
) -> StreamingResponse:
    rows = (
        db.query(Content).filter(Content.user_id == profile.id, Content.status == "ready").all()
    )

    buffer = io.BytesIO()
    used_names: set[str] = set()
    # ZIP_STORED (no compression) — audio is already compressed (mp3/m4a/opus),
    # so re-compressing it just burns CPU for no size benefit.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        for row in rows:
            if not row.file_path:
                continue
            file_path = Path(row.file_path)
            if not file_path.exists():
                continue

            name = safe_filename(row.title) + file_path.suffix
            if name in used_names:
                stem = safe_filename(row.title)
                n = 2
                while name in used_names:
                    name = f"{stem} ({n}){file_path.suffix}"
                    n += 1
            used_names.add(name)

            zf.write(file_path, arcname=name)

    if not used_names:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Nothing to export")

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="spotea-downloads.zip"'},
    )
