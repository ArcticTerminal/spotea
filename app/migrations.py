from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# Base.metadata.create_all() only creates tables that don't exist yet — it never
# alters existing ones. Each entry here patches an existing SQLite database (from
# before the column existed) by adding it if missing. New installs get every
# column for free via create_all(), so this is a no-op for them.
_COLUMN_MIGRATIONS = [
    ("content", "is_favorite", "BOOLEAN NOT NULL DEFAULT 0"),
    ("content", "duration_seconds", "INTEGER"),
    ("content", "is_saved", "BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "audio_quality", "VARCHAR(10) NOT NULL DEFAULT 'high'"),
    ("content", "last_played_at", "DATETIME"),
    ("feeds", "avatar_url", "VARCHAR(500)"),
    ("feeds", "followed", "BOOLEAN NOT NULL DEFAULT 1"),
    ("content", "is_preview", "BOOLEAN NOT NULL DEFAULT 0"),
    ("content", "is_new_upload", "BOOLEAN NOT NULL DEFAULT 0"),
]


def run_migrations(engine: Engine) -> None:
    existing_tables = set(inspect(engine).get_table_names())

    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            if table not in existing_tables:
                continue
            columns = {col["name"] for col in inspect(engine).get_columns(table)}
            if column in columns:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

        # Downloading has always been a play-triggered action (see
        # content.py), so any 'ready' row from before last_played_at existed
        # represents a genuine past play — backfill it from downloaded_at as
        # the closest proxy, or "haven't played yet" would wrongly include
        # already-downloaded content. A no-op once every such row is backfilled.
        if "content" in existing_tables:
            columns = {col["name"] for col in inspect(engine).get_columns("content")}
            if "last_played_at" in columns:
                conn.execute(
                    text(
                        "UPDATE content SET last_played_at = downloaded_at "
                        "WHERE last_played_at IS NULL AND status = 'ready' AND downloaded_at IS NOT NULL"
                    )
                )

        # "medium" quality (locally re-encoded, the only tier that paid a
        # transcode cost — see downloader.py) has been dropped in favor of
        # just "high"/"low", both of which remux instead of re-encoding. Any
        # profile still set to it falls back to "low", the closer match in
        # intent (smaller files) of the two remaining tiers. The table's
        # CHECK constraint predates this on already-created databases and
        # SQLite can't alter it in place, but it still only allows
        # 'high'/'medium'/'low' — 'low' passes it fine.
        if "users" in existing_tables:
            columns = {col["name"] for col in inspect(engine).get_columns("users")}
            if "audio_quality" in columns:
                conn.execute(text("UPDATE users SET audio_quality = 'low' WHERE audio_quality = 'medium'"))
