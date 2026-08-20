# Spotea — Architecture

A self-hosted music player over YouTube Music. One login owns one library of
followed artists; a background sync notices what they release; yt-dlp turns a
track into a file on disk; the browser plays it.

This document is the map. The reasoning behind individual decisions lives in
the code comments, which are the primary source — this file says what the
pieces are and how they fit, not why each line is the way it is.

---

## 1. Component diagram

```
                 ┌──────────────────────────────────────────┐
   browser ────▶ │  FastAPI (app/main.py)                   │
                 │                                          │
                 │  routers/    HTTP surface                │
                 │  services/   follow, sync, recommend     │
                 │  youtube/    the YouTube Music client    │
                 │  downloader  yt-dlp, audio only          │
                 └───────┬──────────────────┬───────────────┘
                         │                  │
                 ┌───────▼───────┐  ┌───────▼────────────────┐
                 │ SQLite        │  │ ./data                 │
                 │ users         │  │   storage/  *.m4a      │
                 │ artists       │  │   avatars/  *.jpg      │
                 │ content       │  │   thumbnails/ *.jpg    │
                 └───────────────┘  └────────────────────────┘

   outbound:  music.youtube.com  (ytmusicapi — everything except audio)
              youtube.com        (yt-dlp — audio extraction only)
```

Two outbound dependencies, each with exactly one job. `ytmusicapi` answers
every question about music: search, artists, releases, playlists, charts,
moods. `yt-dlp` is imported in exactly one module — `app/downloader.py` — and
only ever to turn a video id into an audio file.

---

## 2. Project structure

```
app/
  main.py            app wiring, lifespan, image routes, avatar proxy
  models.py          User, Artist, Content, RecommendationCache
  schemas.py         request/response shapes
  database.py        engine, session, SQLite pragmas
  deps.py            get_db, get_current_user, require_login
  auth.py            password hashing, session key
  config.py          env-backed settings
  middleware.py      selective gzip, security headers
  scheduler.py       background loop: refresh due users, sweep disk
  storage.py         disk accounting, purge, orphan sweeps, export
  downloader.py      yt-dlp audio extraction — the only yt-dlp importer
  images.py          avatar/thumbnail fetch + cache, /image-proxy helpers
  content_query.py   one place that decides what a filter/playlist means
  page_context.py    the context every page and fragment renders from
  progress.py        expiring in-memory registries (downloads, syncs)
  interests.py       the free-text interest list's format
  timeutil.py        naive-UTC helpers
  formatting.py      filename/size/duration formatting
  templating.py      Jinja environment

  routers/
    pages.py         GET / — the whole app is one document
    partials.py      /partials/* — fragment re-renders of one region
    artists.py       follow, unfollow, refresh, which are still syncing
    explore.py       search, and turning a remote row into a playable one
    content.py       download, stream, favorite, save, queues
    recommendations.py  the "For you" batch
    settings.py      audio quality, interests, refresh interval
    storage.py       clear all, export zip
    auth.py          register, login, logout
    debug.py         playback breadcrumbs the server can't otherwise see

  services/
    artist_follow.py   "is this an artist?" — the one throat every follow goes through
    artist_sync.py     release-snapshot diff; the only writer of new Content
    initial_sync.py    the background first sync, and its progress registry
    remote_detail.py   artist / release / playlist panels
    recommendations.py the Explore batch, its cache and its TTL

  youtube/
    music.py         the ytmusicapi client — search, artist, release, charts, moods
    models.py        the result shapes routers and templates speak
    urls.py          id regexes, URL builders, thumbnail sizing

  templates/         index.html is the app; _*.html are its regions
  static/js/         ES modules, no build step
  static/css/        one stylesheet

tests/               pytest; every network call monkeypatched out
scripts/backup.sh    sqlite backup helper
```

There is no build step and no JS test runner. `tests/test_static_js.py`
guards the shipped JavaScript at source level instead — it pins mistakes
that have already been made once, and says so rather than pretending to
execute anything.

---

## 3. Data model

### `users`

One login, one library. Was two tables (`accounts` + household `users`
profiles); the profile model is gone.

| column | notes |
|---|---|
| `email`, `password_hash` | email lowercased at the router; unique |
| `audio_quality` | `high` / `low`, both remux rather than re-encode |
| `interests` | newline-separated free text; owned by `app/interests.py` |
| `refresh_interval_minutes` | 15 / 30 / 60 / 120 |
| `refreshed_at` | NULL means never, which the scheduler treats as overdue |

### `artists`

| column | notes |
|---|---|
| `channel_id` | the artist's "<Artist> - Topic" channel. **The key** — it is what a track carries, so a song grabbed from Explore lands on the same row as a deliberate follow |
| `browse_id` | how YouTube Music addresses their page; opens their profile, and what the sync asks about. NULL only on placeholder rows |
| `name`, `avatar_url` | display, filled in by the first sync |
| `followed` | False for a placeholder created to hold one Explore track, and for an artist unfollowed while keeping some of their content |
| `release_snapshot` | JSON array of every release browse id the page listed last time. The whole change-detection mechanism. NULL means never synced |

### `content`

One track. `artist_id` + `user_id`, `video_id` unique per user, plus the
download state (`status`, `file_path`, `file_size_bytes`, `is_unavailable`),
the engagement flags (`is_favorite`, `is_saved`, `last_played_at`) and two
that decide where a row shows up:

- `is_new_upload` — the sync inserted it, meaning it was released after the
  follow. That is what Home's **New releases** shelf means.
- `is_preview` — added from Explore and not favorited or saved yet. Plays
  normally, stays out of Library, swept after 7 days.

Indexes are not cosmetic: measured on a 30k-row library they took the ten
hottest queries from 81.7ms of SQLite time to 3.8ms. See the comments on
`Content.__table_args__`.

### `recommendation_cache`

One row: the last Explore batch, keyed by a hash of the interest list plus a
payload version. Bumping `PAYLOAD_VERSION` invalidates every stored batch
without a migration.

### Schema changes

There is no migration framework. `Base.metadata.create_all()` builds a fresh
database; a schema change means a fresh database. The one that existed was
deleted along with the tables it patched forward.

---

## 4. The two syncs

### Following an artist

`POST /artists` with a channel URL. `services/artist_follow.py` extracts the
channel id, asks YouTube Music who that is, and refuses the follow if the
answer isn't an artist — the music-only scope in one rule. It stores the
Topic channel as the key and the page's own browse id as the address (which
is not always the id that was asked for: a VEVO container redirects to the
page that actually has the music).

The route answers as soon as the row exists. The first sync runs as a
background task and Library's card reports on it (`data-preparing`, polled
against `GET /artists/syncing`).

### Noticing a release

`services/artist_sync.py`, on the scheduler's tick and on the Refresh button.
For each followed artist: read their page, take albums + singles, diff the
release ids against `release_snapshot`, open each genuinely new release for
its tracks, insert them.

Measured live per artist per refresh: **0.38–0.76s** for the page, plus
**0.09–0.20s** per new release — and usually there is no new release at all.

A first sync records the snapshot and imports nothing. Following means "tell
me what they put out from now on"; the back catalogue is a click away on
their profile.

**What this replaced.** An RSS read of the Topic channel, plus a yt-dlp call
per channel to find out how long anything was, plus a Shorts filter. What it
gives up is the exact publish timestamp — YouTube Music reports a year and
nothing finer, so a new release is stamped with when it was first seen, which
on a 30-minute interval is within half an hour of the truth. What it gains:
durations and cover art arrive with the tracks, and a guest verse on someone
else's record is caught, which never reaches the artist's own Topic channel
at all.

---

## 5. Endpoints

| method | path | what |
|---|---|---|
| GET | `/` | the whole app, one document |
| GET | `/partials/{home,library,downloads,storage-summary}` | re-render one region |
| GET | `/partials/detail/playlist/{kind}` | a pinned playlist |
| GET | `/partials/detail/yt-artist/{browse_id}` | an artist's profile |
| GET | `/partials/detail/yt-artist-songs/{browse_id}` | their whole song list |
| GET | `/partials/detail/yt-release/{browse_id}` | an album or single |
| GET | `/partials/detail/yt-playlist/{playlist_id}` | a YouTube Music playlist |
| POST/DELETE | `/artists`, `/artists/{id}` | follow, unfollow |
| POST | `/artists/refresh` | sync now |
| GET | `/artists/syncing` | which are still filling in |
| GET | `/explore/artists`, `/explore/songs` | search |
| POST | `/explore/tracks`, `/explore/tracks/batch` | make a remote row playable |
| DELETE | `/explore/tracks/{content_id}` | drop a preview |
| POST/GET | `/content/{id}/download`, `/status` | fetch audio, poll it |
| GET | `/content/{id}/stream` | play it (Range-capable FileResponse) |
| GET | `/content/queue/playlist/{kind}` | the ids behind a Play all |
| POST/DELETE | `/content/{id}/{favorite,save}` | engagement flags |
| GET/PUT | `/settings` | quality, interests, interval |
| GET/POST | `/recommendations`, `/recommendations/refresh` | the For you batch |
| DELETE/GET | `/storage`, `/storage/export` | clear all, zip |
| GET | `/avatars/*`, `/thumbnails/*`, `/image-proxy` | images |
| POST | `/register`, `/login`, `/logout` | auth |
| GET | `/health` | db + scheduler liveness |

Everything except auth and `/health` requires a session.

---

## 6. The client

`index.html` is the entire app: Home, Library, Explore, Settings and the
detail panel are tab panels in one document, routed by the URL hash. An
inline head script paints the right tab before any module loads, so a reload
never flashes the wrong one.

Two mechanisms carry almost all of the interactivity:

- **Fragment refresh.** Anything that changes what a region shows re-fetches
  that region from `/partials/*` and swaps it in (`static/js/fragments.js`).
  Nothing hand-patches the DOM; a shelf and its count can't disagree.
- **The detail panel.** One panel body renders four remote kinds. Remote
  fragments are cached per URL for the session, since nothing this app does
  changes what YouTube Music would answer.

The player is a single `<audio>` element. It is the only one, deliberately:
adding a second to solve an iOS background-playback problem is what caused
the problem the second element was added to fix.

---

## 7. Concurrency & security

- **One worker, always.** In-memory registries (download progress, sync
  progress, login-failure counts) are process-local, and the app asserts a
  single worker at startup rather than silently misbehaving under two.
- **SQLite in WAL mode** with foreign keys on. Fetches fan out across a small
  thread pool; DB writes never happen inside it.
- **Sessions** are signed cookies (`itsdangerous`). Passwords are bcrypt, and
  a login for an unknown email still pays a real bcrypt check so timing
  doesn't reveal which emails are registered.
- **Login is rate limited** per client, because each attempt costs ~420ms of
  a shared threadpool worker.
- **Every outbound URL is host-checked** before it is fetched, so an
  authenticated user can't point a follow at `127.0.0.1`.
- **The avatar proxy** only fetches from an allowlist of Google image hosts,
  and answers anything it can't fetch with a transparent pixel rather than a
  broken image.
- **CSP and security headers** are set in `app/middleware.py`; gzip is
  applied selectively, never to audio or images.

---

## 8. Known failure modes

**YouTube refusing a download (HTTP 403).** The retry ladder in
`downloader.py` walks client impersonations. A track that every client
refuses is marked `is_unavailable` and skipped instantly on the next play
rather than spending an extraction to be told the same thing — usually a
Topic-channel track licensed for other countries. Deleting the download
clears the flag, which is the manual "try again".

**ytmusicapi is unofficial.** Its playlist parser already fails on 25 of
YouTube Music's 40 mood categories, which is why the mood shelf reads only
one section. A parser break takes out discovery; it cannot take out the
library, because playback is a local file and a local row.

**Release detection lag.** The sync sees a release when the artist's page
lists it. How quickly that happens after a real release is not measured.

---

## 9. Deployment

```bash
cp .env.example .env      # set SECRET_KEY
docker compose up -d --build
```

`./data` holds the database, the audio, and the image caches, and survives
rebuilds. `yt-dlp` and `ytmusicapi` are installed in their own Docker layer
after `COPY app`, so any real change to the app picks up current versions of
both — pinning them in `requirements.txt` meant the running image sat six
weeks behind upstream while looking, from the file, like it floated.

Runtime dependencies: FastAPI, uvicorn, SQLAlchemy, pydantic-settings,
Jinja2, python-multipart, itsdangerous, bcrypt — plus yt-dlp and ytmusicapi
from the Dockerfile layer. `ffmpeg` for remuxing.

---

## 10. Verifying UI work

The test suite covers the server and guards the shipped JS at source level,
but it cannot click anything. Anything that changes what a person sees is
verified by running the real app:

```bash
docker compose up -d --build
curl -s localhost:${HOST_PORT:-8000}/health
```

then walking the round it exists to serve: register, search an artist, follow
them, add a song, download it, play it.
