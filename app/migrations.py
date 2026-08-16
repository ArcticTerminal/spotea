from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine

from app.auth import hash_password
from app.config import settings
from app.downloader import is_permanent_failure

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
    # Nullable with no backfill here: NULL means "not measured yet", which
    # storage.collect_usage fills in from disk the first time it sees the
    # row. Doing it there rather than as a bulk UPDATE keeps startup from
    # stat'ing every downloaded file on an install with a large library.
    ("content", "file_size_bytes", "INTEGER"),
    # Nullable — unlike the other entries above there's no single sane
    # default across pre-existing rows here; backfilled explicitly below
    # instead, once there's a legacy Account to point them at.
    ("users", "account_id", "INTEGER REFERENCES accounts(id)"),
    # Not a real FK (see Account.last_active_profile_id's docstring) — no
    # sane default either, NULL just means "no explicit preference yet",
    # which get_current_profile already treats the same as any other
    # unresolved session.
    ("accounts", "last_active_profile_id", "INTEGER"),
    # NULL and "" both mean "no interests set yet" — interests.parse_interests
    # treats them the same, so no backfill is needed here.
    ("users", "interests", "TEXT"),
    # Defaults to "not settled yet", which is the right answer for every
    # pre-existing row except the already-errored ones — those are classified
    # from their stored error_message below.
    ("content", "is_unavailable", "BOOLEAN NOT NULL DEFAULT 0"),
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

        # Rows that already failed permanently were recorded before anything
        # distinguished that from a transient failure — they'd otherwise sit
        # there being re-attempted (three extractions apiece) every time
        # someone opened them. Their stored error_message is exactly what
        # download_audio classifies on, so re-run the same test over it.
        # Bounded by "status = 'error'", which is a handful of rows even on a
        # large library, and idempotent: a row it marks is no longer selected.
        if "content" in existing_tables:
            columns = {col["name"] for col in inspect(engine).get_columns("content")}
            if "is_unavailable" in columns:
                rows = conn.execute(
                    text(
                        "SELECT id, error_message FROM content "
                        "WHERE status = 'error' AND is_unavailable = 0 AND error_message IS NOT NULL"
                    )
                ).all()
                permanent_ids = [row.id for row in rows if is_permanent_failure(row.error_message)]
                if permanent_ids:
                    conn.execute(
                        text(
                            "UPDATE content SET is_unavailable = 1 WHERE id IN :ids"
                        ).bindparams(bindparam("ids", expanding=True)),
                        {"ids": permanent_ids},
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

        # Pre-account-model deployments have `users` rows with no owning
        # Account (this column didn't exist before). Fold them all into one
        # legacy Account so an existing single-tenant install keeps working
        # after upgrading — its owner logs in with owner@local + whatever
        # APP_PASSWORD already was. accounts is guaranteed to exist by this
        # point: create_all() (which creates any brand-new table) always
        # runs before run_migrations() — see main.py's lifespan. A no-op
        # once every such row is backfilled, or on any install that never
        # had pre-account data.
        if "users" in existing_tables:
            columns = {col["name"] for col in inspect(engine).get_columns("users")}
            if "account_id" in columns:
                orphan_count = conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE account_id IS NULL")
                ).scalar()
                if orphan_count:
                    if not settings.app_password:
                        raise RuntimeError(
                            "Existing profiles have no owning account and APP_PASSWORD is "
                            "unset in .env — set it to whatever it used to be before "
                            "upgrading, so the legacy account can be created with a working "
                            "password. It can be removed again once you've logged in."
                        )
                    legacy_email = "owner@local"
                    legacy_account_id = conn.execute(
                        text("SELECT id FROM accounts WHERE email = :email"), {"email": legacy_email}
                    ).scalar()
                    if legacy_account_id is None:
                        conn.execute(
                            text(
                                "INSERT INTO accounts (email, password_hash, created_at) "
                                "VALUES (:email, :password_hash, CURRENT_TIMESTAMP)"
                            ),
                            {"email": legacy_email, "password_hash": hash_password(settings.app_password)},
                        )
                        legacy_account_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
                    conn.execute(
                        text("UPDATE users SET account_id = :id WHERE account_id IS NULL"),
                        {"id": legacy_account_id},
                    )
