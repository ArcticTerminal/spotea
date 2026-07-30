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
