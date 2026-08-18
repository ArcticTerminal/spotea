"""Maintainer tool: resolves the onboarding wizard's suggested channels
against YouTube and writes the result to scripts/channel_profiles.py, which
is committed.

Nobody self-hosting spotea needs to run this. It exists so that they don't:
the file it generates ships with the repo, the seed scripts insert rows
already carrying their title and avatar URL, and a fresh install serves a
full onboarding wizard without ever contacting YouTube. See
app/services/genre_artists.py's module docstring for the history.

Run it after adding channels to either curated list (scripts/
seed_music_artists.py, scripts/seed_podcast_channels.py) — seed first so the
new rows exist, then this, then commit the regenerated profiles file:

    .venv/bin/python -m scripts.seed_music_artists
    .venv/bin/python -m scripts.seed_podcast_channels
    .venv/bin/python -m scripts.resolve_genre_artists

Re-running it with nothing new to do costs one database read and no network.
Occasionally worth running with --refresh anyway, which re-resolves every
channel rather than only the unresolved ones, to pick up avatars that have
changed since they were last frozen into the file.

Bounded and interruptible on purpose. This is several hundred yt-dlp calls
from one residential IP, which is the shape of traffic that earns a
temporary 403 — so concurrency is capped (--workers, default 4), progress is
committed batch by batch (kill it and the next run resumes from there), and
a run that starts failing repeatedly stops rather than hammering through the
rest of the list.
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.database import SessionLocal
from app.models import GenreArtist
from app.services.genre_artists import apply_profile, fetch_profile

# How many channel pages to read at once. Measured on the real list, a
# single-threaded run averaged 4.7s per channel — about 3.2s of that the
# yt-dlp extraction itself, which is latency, not local work, so overlapping
# them is nearly free. Four is a deliberate ceiling rather than a maximum:
# it is a several-fold increase in request rate against YouTube from one
# residential IP, which is exactly the traffic shape that earns a temporary
# 403, and this is a one-off job nobody is waiting on.
DEFAULT_WORKERS = 4

# A dead channel resolves to "no avatar" and is perfectly normal, one at a
# time. Several in an unbroken row means the failure is ours, not theirs
# (throttled, blocked, offline), and continuing would just deepen it. This is
# what actually protects the run — more so now that it is concurrent.
MAX_CONSECUTIVE_FAILURES = 5

PROFILES_PATH = Path(__file__).with_name("channel_profiles.py")

# Everything above the generated dict is preserved verbatim on rewrite, so
# the file's own explanation of itself lives in the file rather than here.
_DICT_MARKER = "# channel_id -> (title, avatar_url)"


def _write_profiles(rows: list[GenreArtist]) -> int:
    """Rewrites channel_profiles.py from `rows`, keeping its docstring.

    Regenerated wholesale rather than appended to, so a channel dropped from
    a curated list drops out of here too instead of lingering forever. Keyed
    by channel id and sorted by it: one artist can be curated under several
    genres, and a stable order keeps the diff to what actually changed.
    """
    header = PROFILES_PATH.read_text().split(_DICT_MARKER)[0]

    # Absolute upstream URLs only. An older build resolved rows in-request
    # and stored an already-built display URL ("/avatar-proxy?u=...") in the
    # same column; app/migrations.py clears those, but a database that has
    # not been through that yet must not be able to write them into a
    # committed file, where they would be double-wrapped and unservable.
    entries = {
        row.channel_id: (row.title or row.artist_name, row.thumbnail_url)
        for row in rows
        if row.thumbnail_url and row.thumbnail_url.startswith("http")
    }

    lines = [header, _DICT_MARKER, "PROFILES: dict[str, tuple[str, str]] = {"]
    for channel_id in sorted(entries):
        title, avatar_url = entries[channel_id]
        lines.append(f"    {channel_id!r}: ({title!r}, {avatar_url!r}),")
    lines.append("}")

    PROFILES_PATH.write_text("\n".join(lines) + "\n")
    return len(entries)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="re-resolve every channel, not just the ones with no profile yet",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="stop after this many channels — for checking a run works before committing to several hundred",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"channels to resolve concurrently (default {DEFAULT_WORKERS})",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        all_rows = db.query(GenreArtist).order_by(GenreArtist.id).all()
        if not all_rows:
            sys.exit("genre_artists is empty — run the seed scripts first.")

        pending = all_rows if args.refresh else [row for row in all_rows if row.resolved_at is None]
        if args.limit:
            pending = pending[: args.limit]
        print(f"{len(pending)} channel(s) to resolve, {len(all_rows)} total")

        consecutive_failures = 0
        resolved = failed = 0
        done = 0
        # One batch in flight at a time, sized to the worker count: the
        # fetches run concurrently, then everything is written and committed
        # from this thread, which is the only one that may touch the Session.
        # Committing per batch rather than at the end keeps the run
        # interruptible — kill it and the next one resumes from here.
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for start in range(0, len(pending), args.workers):
                batch = pending[start : start + args.workers]
                profiles = list(pool.map(fetch_profile, [row.channel_id for row in batch]))

                # strict: pool.map preserves order and length, and a
                # mismatch would silently pair a profile with the wrong row.
                for row, profile in zip(batch, profiles, strict=True):
                    if apply_profile(row, profile):
                        resolved += 1
                        consecutive_failures = 0
                    else:
                        failed += 1
                        consecutive_failures += 1
                    done += 1
                    print(f"[{done}/{len(pending)}] {row.artist_name}: {row.title or 'no answer'}")

                db.commit()

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"\nStopping: {consecutive_failures} channels in a row failed to "
                        "resolve. That usually means YouTube is refusing this IP rather "
                        "than that the channels are gone — wait a while, then re-run to "
                        "pick up where this left off.",
                        file=sys.stderr,
                    )
                    break

        # From every row, not just the ones this run touched, so the file
        # stays a complete picture even after an interrupted run.
        written = _write_profiles(db.query(GenreArtist).order_by(GenreArtist.id).all())

    print(f"\nresolved {resolved}, no answer {failed}")
    print(f"wrote {written} profile(s) to {PROFILES_PATH.name} — commit it")


if __name__ == "__main__":
    main()
