#!/usr/bin/env bash
#
# WAL-safe SQLite backup of Spotea's database.
#
# Usage (from the repo root, whether or not the app is running):
#   ./scripts/backup.sh
#   ./scripts/backup.sh /path/to/spotea.db /path/to/backup/dir
#
# Uses `sqlite3 <db> ".backup <dest>"` rather than `cp`: the app runs SQLite
# in WAL mode, so a raw file copy can land mid-write and capture a torn,
# inconsistent snapshot (the main .db file alone isn't the whole story while
# a -wal file exists next to it). sqlite3's .backup command takes a proper
# read lock via the SQLite API and produces a consistent copy even while the
# app is writing to the database concurrently.
#
# Defaults assume you're running this on the host against the bind-mounted
# ./data volume (docker-compose.yml mounts it at /app/data in the app
# container, same bytes either way) — no need to exec into the container.
# Requires the `sqlite3` CLI on whichever machine runs this script.
#
# This is a manual, one-shot script by design — no cron/scheduling is set up
# here. Run it yourself (e.g. before an upgrade), or wire it into your own
# host's scheduler if you want it recurring.
#
# Output lands at <dest-dir>/spotea.db.bak-<UTC timestamp>, matching the
# data/*.db.bak-* pattern .gitignore already excludes.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(dirname "$script_dir")"

db_path="${1:-$repo_root/data/spotea.db}"
dest_dir="${2:-$repo_root/data}"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "error: sqlite3 CLI not found on PATH — install it, or run this" >&2
    echo "       inside the app container instead: docker compose exec app sh -c" >&2
    echo "       'sqlite3 /app/data/spotea.db \".backup /app/data/spotea.db.bak-\$(date -u +%Y%m%dT%H%M%SZ)\"'" >&2
    exit 1
fi

if [ ! -f "$db_path" ]; then
    echo "error: database not found at $db_path" >&2
    exit 1
fi

mkdir -p "$dest_dir"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dest_path="$dest_dir/spotea.db.bak-$timestamp"

sqlite3 "$db_path" ".backup '$dest_path'"

echo "Backed up $db_path -> $dest_path"
