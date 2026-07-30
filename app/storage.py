from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import Content


@dataclass
class StoredItem:
    id: int
    title: str
    channel_title: str | None
    size_bytes: int


@dataclass
class StorageUsage:
    items: list[StoredItem]
    total_bytes: int

    @property
    def count(self) -> int:
        return len(self.items)


def collect_usage(db: Session, user_id: int) -> StorageUsage:
    """Downloaded content plus the disk each file actually occupies.

    Sizes are read from disk rather than stored, so a file removed out from
    under the app simply reports 0 instead of inflating the total.
    """
    rows = (
        db.query(Content)
        .options(joinedload(Content.feed))
        .filter(Content.user_id == user_id, Content.status == "ready")
        .order_by(Content.downloaded_at.desc())
        .all()
    )

    items: list[StoredItem] = []
    for row in rows:
        size = 0
        if row.file_path:
            path = Path(row.file_path)
            if path.exists():
                size = path.stat().st_size
        items.append(
            StoredItem(
                id=row.id,
                title=row.title,
                channel_title=row.feed.channel_title,
                size_bytes=size,
            )
        )

    return StorageUsage(items=items, total_bytes=sum(item.size_bytes for item in items))


def clear_all(db: Session, user_id: int) -> int:
    """Delete every downloaded file and reset its row. Returns rows cleared.

    Also sweeps any audio left in the storage directory that no row points at —
    e.g. files whose content rows were removed when a channel was unfollowed.
    Without that sweep "Clear all" wouldn't actually free all the disk it claims.
    """
    rows = db.query(Content).filter(Content.user_id == user_id, Content.status == "ready").all()

    for row in rows:
        if row.file_path:
            Path(row.file_path).unlink(missing_ok=True)
        row.status = "not_downloaded"
        row.file_path = None
        row.error_message = None
        row.downloaded_at = None

    db.commit()

    for leftover in settings.storage_dir.glob(f"*.{settings.audio_format}"):
        leftover.unlink(missing_ok=True)

    return len(rows)


def delete_files_for_feed(db: Session, feed_id: int) -> None:
    """Remove downloaded audio belonging to a feed.

    Content rows cascade-delete with their feed, but the files they point at do
    not — call this before deleting a feed so unfollowing doesn't strand audio
    on disk forever.
    """
    rows = db.query(Content).filter(Content.feed_id == feed_id).all()
    for row in rows:
        if row.file_path:
            Path(row.file_path).unlink(missing_ok=True)
