# spotifrei — Architecture

A self-hosted, open-source tool for archiving and listening to content from
YouTube channels via RSS. Users add RSS feeds of YouTube channels they follow,
the app periodically parses them, lets the user download selected videos as
audio (via yt-dlp), and play them back from the browser. Designed to be
shared on GitHub and run by others via Docker on their own servers.

## 1. Component diagram

```
Browser (vanilla JS)
   │  fetch()
   ▼
FastAPI (Uvicorn, single process, inside Docker container)
   │           │              │
   ▼           ▼              ▼
SQLite    yt-dlp (thread   feedparser
(SQLAlchemy) pool /        (HTTP GET to
             subprocess)    YouTube RSS)
   │
   ▼
data/storage/{video_id}.mp3   (Docker volume)
data/spotifrei.db             (Docker volume)
```

## 2. Project structure

```
spotifrei/
  app/
    main.py               # FastAPI app + router mount
    config.py              # env-based settings
    database.py             # SQLAlchemy engine/session
    models.py                # User, Feed, Content
    schemas.py                 # Pydantic request/response models
    auth.py                     # login/session handling
    deps.py                       # get_db, require_login
    rss.py                         # feedparser-based RSS fetching/parsing
    downloader.py                    # yt-dlp wrapper + background download
    routers/
      auth.py
      feeds.py
      content.py
    templates/
      login.html
      index.html
      player.html
    static/
      js/app.js                       # polling, download trigger
      css/style.css
  data/                                 # mounted as Docker volume
    storage/{video_id}.mp3
    spotifrei.db
  Dockerfile
  docker-compose.yml
  .dockerignore
  .env.example
  requirements.txt
  LICENSE                                 # MIT
  README.md
  ARCHITECTURE.md
```

## 3. Data model

**users** — `id PK`, `name`. Single row (`id=1`, "local"). The app currently
has one shared login gate rather than per-account identity; this table stays
in place so real multi-account support can be layered on later without a
schema change.

**feeds** — `id PK`, `user_id FK`, `rss_url`, `channel_title` (auto-filled on
first parse), `added_at`.
Unique: `(user_id, rss_url)`.

**content** — `id PK`, `feed_id FK`, `user_id FK` (denormalized for query
convenience), `video_id` (YouTube ID), `title`, `thumbnail_url`,
`published_at`, `status`, `file_path`, `error_message`, `added_at`,
`downloaded_at`.
Unique: `(user_id, video_id)`.
Index: `(user_id, status)`, `(user_id, published_at DESC)`.

**Status state machine:**
```
not_downloaded ──▶ downloading ──▶ ready
                        │
                        ▼
                      error ──(retry)──▶ downloading
```

## 4. Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/login` | Login page |
| POST | `/login` | Check password, set session cookie |
| POST | `/logout` | Clear session |
| GET | `/` | Home page (feeds + content list), requires login |
| POST | `/feeds` | Add a new RSS URL, run first parse |
| DELETE | `/feeds/{id}` | Unfollow |
| POST | `/feeds/refresh` | Re-parse all feeds, insert new content rows |
| GET | `/content` | JSON content list (used for polling) |
| POST | `/content/{id}/download` | Start yt-dlp download in the background |
| GET | `/content/{id}/status` | Current status |
| GET | `/content/{id}/stream` | Serve the audio file (Range-request support) |
| DELETE | `/content/{id}` | Delete file, reset status to `not_downloaded` |
| GET | `/player/{id}` | Player page |

All routes except `/login` and static assets require an active session
(enforced via a `require_login` dependency).

## 5. User flows

**Login** — Single shared password, set via the `APP_PASSWORD` env var. On
`POST /login`, the submitted password is compared with `secrets.compare_digest`
against `APP_PASSWORD`; on match, a signed session cookie is set (Starlette
`SessionMiddleware`, backed by `SECRET_KEY`). No per-user accounts — this is a
gate for the whole instance, appropriate for a small self-hosted deployment.

**Page load** — The page renders immediately with existing DB content, then
JS triggers `POST /feeds/refresh` in the background and updates the grid when
it completes — no blank-screen wait for RSS fetches.

**Add feed** — `POST /feeds {rss_url}` → validated with feedparser (400 if
invalid) → feed saved → first parse run immediately, writing new content
rows.

**Refresh** — For each feed, RSS `video_id`s are checked against the DB;
missing ones are inserted as `status=not_downloaded`. Existing rows are left
untouched.

**Download** — `POST /content/{id}/download` → 409 if already `downloading`
→ `status='downloading'` set synchronously → yt-dlp job dispatched to the
background → response returns immediately. On completion: `status='ready'` +
`file_path`; on failure: `status='error'` + `error_message`.

**Polling** — For each card in `downloading` state, the frontend polls
`GET /content/{id}/status` every 1.5–2s; stops once `ready` or `error`.

**Play** — `GET /player/{id}` → `<audio src="/content/{id}/stream">`. The
stream endpoint returns 409 if `status != ready`; the file path always comes
from the DB, never from the request (prevents path traversal).

**Delete** — File removed from disk, DB row reset to `status='not_downloaded'`
+ `file_path=NULL` (row kept, not deleted) — re-downloading later doesn't
require refreshing the feed again.

## 6. Concurrency & security

- yt-dlp is called via its **Python API** (`yt_dlp.YoutubeDL`), not a raw
  subprocess — cleaner error handling, and progress hooks can be added later.
- The download URL is always constructed as
  `https://www.youtube.com/watch?v={video_id}` by the server;
  `video_id` is validated against `^[a-zA-Z0-9_-]{11}$` — user input never
  reaches a shell command directly.
- No concurrent-download limit in the MVP (personal/small-scale use); a
  semaphore can be added later if needed.
- Session cookies are signed and HttpOnly; `SECRET_KEY` and `APP_PASSWORD`
  are required env vars with no insecure defaults baked into the image.
- README will recommend running behind HTTPS (reverse proxy) for anyone
  exposing the instance beyond their local network.

## 7. Config & dependencies

Environment variables (`.env`, documented in `.env.example`):
- `APP_PASSWORD` — shared login password (required)
- `SECRET_KEY` — session signing key (required)
- `DATABASE_URL` — default `sqlite:////app/data/spotifrei.db`
- `STORAGE_DIR` — default `/app/data/storage`
- `AUDIO_FORMAT` — default `mp3`

`requirements.txt`: `fastapi`, `uvicorn[standard]`, `sqlalchemy`,
`feedparser`, `yt-dlp`, `jinja2`, `python-multipart`, `itsdangerous`

## 8. Error scenarios

| Case | Behavior |
|---|---|
| No/invalid session | 401 → redirect to `/login` |
| Invalid RSS URL | 400 + message |
| yt-dlp download failure | `status=error`, `error_message` stored |
| Duplicate download request | 409 |
| Stream request while not `ready` | 409 |

## 9. Deployment (Docker)

- `Dockerfile`: `python:3.12-slim` base, installs `ffmpeg` (required by
  yt-dlp for audio extraction), installs `requirements.txt`, copies `app/`,
  runs `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: single `app` service, builds from the Dockerfile,
  maps `8000:8000`, mounts `./data:/app/data` (persists the SQLite DB and
  downloaded audio across container restarts/upgrades), reads env vars from
  `.env`.
- `.env.example` ships in the repo so users copy it to `.env` and fill in
  `APP_PASSWORD` / `SECRET_KEY` before first run.
- README will document: `docker compose up -d`, how to set the password, and
  how to update (pull + rebuild, volume persists data).

## 10. Roadmap

- Real multi-account support (distinct logins, per-account feed/content
  isolation) is deferred; today's `user_id` columns and shared-password gate
  make that a later addition, not a rewrite.
- License: MIT.

## 11. Implementation milestones

1. Project scaffold + DB models, FastAPI boots empty
2. Login/session (shared password) wired in, gating all routes
3. Feed add + RSS parse (backend, tested via curl)
4. Home page UI: feed form + content grid
5. Download + polling + loading/play state
6. Player page + stream endpoint
7. Delete
8. Dockerfile + docker-compose + README for self-hosting
