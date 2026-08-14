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


def unlink_thumbnail_if_unshared(db: Session, video_id: str, exclude_content_id: int) -> None:
    """Same sharing check as unlink_if_unshared, for a cached thumbnail
    instead of a downloaded audio file — a thumbnail is keyed by video_id
    alone (see downloader.download_thumbnail), so it can legitimately be
    shared by more than one profile's Content row for the same video (e.g.
    two profiles both followed an overlapping channel, or one followed it
    after the other already had it as an Explore preview). Only ever called
    where a Content row is actually being deleted (unfollow purge, Explore
    removal) — content.py's delete_content resets a row's download status in
    place without deleting it, so it never needs this."""
    still_referenced = (
        db.query(Content.id)
        .filter(Content.video_id == video_id, Content.id != exclude_content_id)
        .first()
    )
    if still_referenced is None:
        (settings.thumbnails_dir / f"{video_id}.jpg").unlink(missing_ok=True)


def unlink_if_unshared(db: Session, file_path: str, exclude_content_id: int) -> None:
    """Only remove the file if no other ready Content row (any profile) still
    points at it. Storage is keyed by video_id alone, so the same physical
    file can legitimately be shared by two profiles that both follow an
    overlapping channel at the same quality — deleting it out from under the
    other profile would silently break its playback."""
    still_referenced = (
        db.query(Content.id)
        .filter(
            Content.file_path == file_path,
            Content.status == "ready",
            Content.id != exclude_content_id,
        )
        .first()
    )
    if still_referenced is None:
        Path(file_path).unlink(missing_ok=True)


def clear_all(db: Session, user_id: int) -> int:
    """Delete every downloaded file and reset its row. Returns rows cleared.

    Also sweeps any audio left in the storage directory that no row points at —
    e.g. files whose content rows were removed when a channel was unfollowed.
    Without that sweep "Clear all" wouldn't actually free all the disk it claims.
    """
    rows = db.query(Content).filter(Content.user_id == user_id, Content.status == "ready").all()

    for row in rows:
        if row.file_path:
            unlink_if_unshared(db, row.file_path, row.id)
        row.status = "not_downloaded"
        row.file_path = None
        row.error_message = None
        row.downloaded_at = None

    db.commit()

    # Sweeps orphans directory-wide (not scoped to this profile) — the same
    # file can be shared across profiles (see unlink_if_unshared), so a file
    # still gets to stay if some other profile's ready content still points
    # at it, even though this profile just cleared everything of its own.
    referenced_paths = {
        path for (path,) in db.query(Content.file_path).filter(Content.status == "ready")
    }
    for leftover in settings.storage_dir.glob(f"*.{settings.audio_format}"):
        if str(leftover) not in referenced_paths:
            leftover.unlink(missing_ok=True)

    return len(rows)


def delete_files_for_profile(db: Session, user_id: int) -> None:
    """Remove downloaded audio belonging to every feed under a profile — call
    this before deleting a profile so it doesn't strand audio on disk forever
    (mirrors delete_files_for_feed, scoped to a whole profile instead)."""
    rows = db.query(Content).filter(Content.user_id == user_id, Content.status == "ready").all()
    for row in rows:
        if row.file_path:
            unlink_if_unshared(db, row.file_path, row.id)
