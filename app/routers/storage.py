from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_db, require_login
from app.storage import clear_all

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_login)])

DEFAULT_USER_ID = 1


@router.delete("")
def clear_storage(db: Session = Depends(get_db)) -> dict[str, int]:
    cleared = clear_all(db, DEFAULT_USER_ID)
    return {"cleared": cleared}
