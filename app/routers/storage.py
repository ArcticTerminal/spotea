import io
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.deps import get_current_profile, get_db, require_login
from app.formatting import safe_filename
from app.models import Content, User
from app.storage import clear_all

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_login)])


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
