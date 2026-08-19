import logging

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.engine import Engine

from app.auth import hash_password
from app.config import settings
from app.downloader import is_permanent_failure
from app.youtube.urls import SHORT_MAX_DURATION_SECONDS

logger = logging.getLogger(__name__)

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
    # Null for every pre-existing feed, which is correct: none of them were
    # followed as an artist. See Feed.artist_browse_id.
    ("feeds", "artist_browse_id", "VARCHAR(32)"),
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
    # Both moved from a single AppSettings row (one interval shared by the
    # whole deployment) onto Account, once real accounts existed and "one
    # interval for everyone" stopped making sense. DEFAULT 30 here is only
    # what a *new* account with no history gets — an existing install's
    # accounts are overwritten with whatever the old shared row actually held
    # right below, in _migrate_refresh_interval_off_app_settings.
    ("accounts", "feed_refresh_interval_minutes", "INTEGER NOT NULL DEFAULT 30"),
    ("accounts", "feeds_refreshed_at", "DATETIME"),
    # No backfill: a pre-existing row's real play count is unknowable after
    # the fact (last_played_at only ever recorded the most recent play, not
    # how many), so 0 is the honest answer for every row that predates this
    # column, not an approximation of one.
    ("content", "play_count", "INTEGER NOT NULL DEFAULT 0"),
]


def _create_missing_indexes(conn, engine: Engine, existing_tables: set[str]) -> None:
    """Add the indexes declared on the models to a database that predates them.

    `Base.metadata.create_all()` skips a table that already exists, and its
    indexes with it, so a pre-existing install would never get these — which
    is exactly the install that has enough rows to need them. Creating an index
    on 30k rows takes a few milliseconds each; ANALYZE runs only when something
    was actually added, since on a large table it is not free.
    """
    if "content" not in existing_tables:
        return  # brand new database: create_all() already built them

    from app.models import Content

    existing = {index["name"] for index in inspect(engine).get_indexes("content")}
    created = 0
    for index in Content.__table__.indexes:
        if index.name in existing:
            continue
        index.create(bind=conn)
        created += 1

    if created:
        logger.info("Created %d missing index(es) on content; running ANALYZE", created)
        conn.execute(text("ANALYZE"))


def _remove_legacy_shorts(conn, existing_tables: set[str]) -> None:
    """Delete Shorts that were imported before anything filtered them out.

    The guard is applied at insert time now (see feed_sync.apply_feed_data and
    services/backfill.py), but rows that predate it stay in the library
    forever. Only untouched ones go: the same "did the user do anything with
    this" test routers/feeds.py's delete_feed uses when deciding what an
    unfollow may remove. A Short someone downloaded, played, favorited or
    saved is theirs to keep, however it got there.
    """
    if "content" not in existing_tables:
        return

    result = conn.execute(
        text(
            "DELETE FROM content WHERE duration_seconds IS NOT NULL "
            "AND duration_seconds <= :max_short "
            "AND status != 'ready' AND last_played_at IS NULL "
            "AND is_favorite = 0 AND is_saved = 0"
        ),
        {"max_short": SHORT_MAX_DURATION_SECONDS},
    )
    if result.rowcount:
        logger.info("Removed %d untouched Short(s) imported before the duration filter", result.rowcount)


def _migrate_refresh_interval_off_app_settings(conn, existing_tables: set[str]) -> None:
    """One-time move of the shared refresh interval onto every account, then
    drop the table it used to live on.

    Before real accounts existed, `feed_refresh_interval_minutes` was a
    single value on a singleton `app_settings` row, shared by the whole
    deployment. The column migration above already gave every `accounts` row
    a DEFAULT of 30 — correct for a brand-new account, but wrong for an
    existing install whose owner had actually picked something else; this
    copies the old row's real value over that default. Self-disabling: the
    only gate is `app_settings` still existing, since dropping it here is
    what stops this running (and overwriting a since-changed per-account
    value) on every later startup.
    """
    if "app_settings" not in existing_tables:
        return

    old_interval = conn.execute(
        text("SELECT feed_refresh_interval_minutes FROM app_settings WHERE id = 1")
    ).scalar()
    if old_interval is not None:
        conn.execute(
            text("UPDATE accounts SET feed_refresh_interval_minutes = :interval"),
            {"interval": old_interval},
        )
        logger.info("Migrated shared feed refresh interval (%d min) onto every account", old_interval)

    conn.execute(text("DROP TABLE app_settings"))


def run_migrations(engine: Engine) -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    # Reflected once per table rather than once per migration entry. The loop
    # below used to call inspect(engine).get_columns() on every iteration —
    # fourteen fresh reflections of the schema on every single startup.
    columns_by_table = {table: {col["name"] for col in inspector.get_columns(table)} for table in existing_tables}

    def has_column(table: str, column: str) -> bool:
        return column in columns_by_table.get(table, ())

    with engine.begin() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            if table not in existing_tables or has_column(table, column):
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            columns_by_table[table].add(column)

        _create_missing_indexes(conn, engine, existing_tables)
        _remove_legacy_shorts(conn, existing_tables)
        _migrate_refresh_interval_off_app_settings(conn, existing_tables)

        # Downloading has always been a play-triggered action (see
        # content.py), so any 'ready' row from before last_played_at existed
        # represents a genuine past play — backfill it from downloaded_at as
        # the closest proxy, or "haven't played yet" would wrongly include
        # already-downloaded content. A no-op once every such row is backfilled.
        #
        # Gated on a row actually matching first: as a bare UPDATE this was a
        # full table scan on every single startup, long after the last row it
        # could apply to had been backfilled.
        if has_column("content", "last_played_at"):
            needs_backfill = conn.execute(
                text(
                    "SELECT 1 FROM content "
                    "WHERE last_played_at IS NULL AND status = 'ready' AND downloaded_at IS NOT NULL "
                    "LIMIT 1"
                )
            ).scalar()
            if needs_backfill:
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
        if has_column("content", "is_unavailable"):
            rows = conn.execute(
                text(
                    "SELECT id, error_message FROM content "
                    "WHERE status = 'error' AND is_unavailable = 0 AND error_message IS NOT NULL"
                )
            ).all()
            permanent_ids = [row.id for row in rows if is_permanent_failure(row.error_message)]
            if permanent_ids:
                conn.execute(
                    text("UPDATE content SET is_unavailable = 1 WHERE id IN :ids").bindparams(
                        bindparam("ids", expanding=True)
                    ),
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
        # Gated on a match, same reasoning as the backfill above — as a bare
        # UPDATE it rewrote nothing on every startup for the life of the
        # deployment.
        if has_column("users", "audio_quality"):
            has_medium = conn.execute(
                text("SELECT 1 FROM users WHERE audio_quality = 'medium' LIMIT 1")
            ).scalar()
            if has_medium:
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
        if has_column("users", "account_id"):
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
