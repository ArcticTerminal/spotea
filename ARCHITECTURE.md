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
data/storage/{video_id}.{fmt} (Docker volume)
data/avatars/{channel_id}.jpg (Docker volume)
data/spotifrei.db             (Docker volume)
```

## 2. Project structure

```
spotifrei/
  app/
    main.py               # FastAPI app + router mount + migrations on startup
    config.py              # env-based settings
    database.py             # SQLAlchemy engine/session
    migrations.py            # lightweight "add column if missing" patcher
    content_query.py           # shared filter/paginate query (newest-first), used
                                # by both the server-rendered page 1 and the AJAX endpoint
    formatting.py              # Jinja filters (duration, file size)
    storage.py                  # disk usage, cache clearing, orphan sweep, zip export
    models.py                    # User, Feed, Content
    schemas.py                     # Pydantic request/response models
    auth.py                          # login/session handling
    deps.py                            # get_db, require_login
    rss.py                               # feedparser + yt-dlp metadata helpers,
                                          # channel search, avatar/duration lookups,
                                          # full-history backfill scan
    downloader.py                          # yt-dlp wrapper + background download +
                                          # channel avatar fetch
    routers/
      auth.py
      feeds.py                               # feeds, search, refresh, backfill status
      content.py                               # download/status/stream/favorite/save/delete
      storage.py                                 # clear-all + zip export endpoints
      settings.py                                # per-user audio quality
      pages.py                                   # home (shelves) + player page rendering
    templates/
      login.html
      index.html                                   # Home/Library/Manage/Settings tabs
      _content_card.html                           # single card partial, reused per item
      player.html
    static/
      js/ui.js                                       # shared confirm modal + toast (loaded first)
      js/app.js                                        # tabs, search, server-side filter/
                                                        # pagination fetches, polling,
                                                        # download/save/delete triggers
      js/player.js                                       # custom audio player controls
      css/style.css
  data/                                 # mounted as Docker volume
    storage/{video_id}.{AUDIO_FORMAT}
    avatars/{channel_id}.jpg              # cached channel avatars, re-served via
                                           # GET /avatars/{filename}
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

**users** — `id PK`, `name`, `audio_quality` (`high`/`medium`/`low`, default
`high` — see Settings below). Single row (`id=1`, "local"). The app
currently has one shared login gate rather than per-account identity; this
table stays in place so real multi-account support can be layered on later
without a schema change.

**feeds** — `id PK`, `user_id FK`, `rss_url`, `channel_title` (auto-filled on
first parse), `avatar_url` (nullable — same-origin path under `/avatars/`,
fetched once per channel), `added_at`.
Unique: `(user_id, rss_url)`.

**content** — `id PK`, `feed_id FK`, `user_id FK` (denormalized for query
convenience), `video_id` (YouTube ID), `title`, `thumbnail_url`,
`duration_seconds` (nullable — backfilled from the channel's uploads
playlist, not in the RSS feed), `published_at`, `status`, `file_path`,
`error_message`, `added_at`, `downloaded_at`, `last_played_at` (nullable —
set on stream, not on download; drives the "Recently played" home shelf),
`is_favorite`, `is_saved`.
`is_favorite` and `is_saved` are separate on purpose: **saving** is a
lightweight "come back to this" bookmark, toggled from the Library grid
without opening anything; **favoriting** is a considered "I liked this",
toggled from the player while you're actually listening. Both are filter
options in the Library.
Unique: `(user_id, video_id)`.
Index: `(user_id, status)`, `(user_id, published_at DESC)`.

**Status state machine:**
```
not_downloaded ──▶ downloading ──▶ ready
                        │
                        ▼
                      error ──(retry)──▶ downloading
```

**Schema evolution** — `Base.metadata.create_all()` (in `main.py`'s startup)
only creates tables that don't exist yet; it never alters existing ones. Each
column added after the initial release (`is_favorite`, `duration_seconds`,
`is_saved`, `audio_quality`, `last_played_at`, `avatar_url`) is also
registered in `app/migrations.py`, which runs on every startup and adds
the column via `ALTER TABLE ... ADD COLUMN` if it's missing — a lightweight
stand-in for a real migration framework (Alembic would be overkill at this
scale). New installs get every column for free via `create_all()`; upgrading
existing installs is what `migrations.py` is for. It also does one data
backfill: existing `ready` rows predating `last_played_at` get it set from
`downloaded_at` as the closest proxy for "this was actually played," so the
Home page's "Recently played" shelf doesn't wrongly treat already-downloaded
content as never played.

## 4. Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/login` | Login page |
| POST | `/login` | Check password, set session cookie |
| POST | `/logout` | Clear session |
| GET | `/` | Home page (Home/Library/Manage/Settings tabs), requires login |
| GET | `/feeds/search?q=` | Search YouTube channels by name (for the Manage tab) |
| POST | `/feeds` | Add a channel by URL (resolved to RSS via yt-dlp), run first parse, kick off background history backfill |
| GET | `/feeds` | List followed feeds |
| DELETE | `/feeds/{id}` | Unfollow (deletes cached audio for the feed first) |
| POST | `/feeds/refresh` | Re-parse all feeds in parallel, insert new content rows, backfill durations/avatar |
| GET | `/feeds/{id}/backfill-status` | Poll progress of the one-time full channel history scan |
| GET | `/content?page=&filter=` | JSON content page, newest-first (server-side filter/pagination; also used for polling) |
| POST | `/content/{id}/download` | Start yt-dlp download in the background |
| GET | `/content/{id}/status` | Current status (+ download/convert progress) |
| GET | `/content/{id}/stream?download=` | Serve the audio file (Range-request support); `download=1` skips the `last_played_at` update |
| POST | `/content/{id}/favorite` | Mark as favorite (from the player) |
| DELETE | `/content/{id}/favorite` | Unmark as favorite |
| POST | `/content/{id}/save` | Save for later (from the Library grid) |
| DELETE | `/content/{id}/save` | Un-save |
| DELETE | `/storage` | Delete every downloaded file ("Clear all" in the Manage downloads modal) |
| GET | `/storage/export` | Download every ready file as one uncompressed zip |
| DELETE | `/content/{id}` | Delete file, reset status to `not_downloaded` |
| GET | `/settings` | Current per-user settings (audio quality) |
| PUT | `/settings` | Update audio quality |
| GET | `/player/{id}` | Player page |
| GET | `/avatars/{filename}` | Serve a cached channel avatar from our own origin |

All routes except `/login` and static assets require an active session
(enforced via a `require_login` dependency).

## 5. User flows

**Login** — Single shared password, set via the `APP_PASSWORD` env var. On
`POST /login`, the submitted password is compared with `secrets.compare_digest`
against `APP_PASSWORD`; on match, a signed session cookie is set (Starlette
`SessionMiddleware`, backed by `SECRET_KEY`). No per-user accounts — this is a
gate for the whole instance, appropriate for a small self-hosted deployment.

**Page load** — The page renders immediately with existing DB content. JS
then triggers `POST /feeds/refresh` behind a full-screen loading overlay
(dark backdrop + spinner) and reloads the page if new content arrived — no
blank-screen wait for RSS fetches, and no layout-shifting inline "Refreshing…"
text competing with the filter controls. Refreshing every followed
channel's RSS is several network calls per channel, so this auto-refresh
only fires once per browser session (`sessionStorage`, `maybeAutoRefresh()`
in `app.js`), not on every reload — a manual refresh button covers
everything else.

**Tabs** — The home page is split into **Home** (shelves: followed channels,
new uploads, recently played, favorites, saved), **Library** (the full,
searchable/filterable/paginated content grid), **Manage** (channel search,
add-by-URL, followed list) and **Settings** (audio quality + a "Manage
downloads" modal, which is where the old standalone Storage tab moved) —
`<section>` panels toggled via `hidden`, not separate routes. The active tab
persists in `localStorage` across visits.

**Home page** — Four shelves (`app/templates/index.html`, data assembled in
`pages.py`'s `home()`), each its own bounded query capped at 12 items rather
than one big query sliced in Python — that used to get slow once full-channel
backfills pushed a user's content well past a few thousand rows:
- **Followed channels** — avatar chips; clicking one jumps to the Library
  tab pre-filtered to that channel.
- **New uploads** — most recent by `published_at`.
- **Recently played** — most recent by `last_played_at` (set on stream, not
  on download — see Play below); hidden if empty.
- **Favorites** / **Saved for later** — same as their Library filters;
  hidden if empty.

**Search & add a channel** — Typing in the Manage tab's search box
(debounced ~400ms) hits `GET /feeds/search?q=` → `search_channels()` (in
`rss.py`) runs a `yt-dlp` flat extraction of YouTube's channel-filtered
search results (`sp=EgIQAg%3D%3D` query param) and returns channel_id,
title, thumbnail, and subscriber count. Clicking "Add" on a result — or
pasting a URL directly in the "Add by URL" form — both hit the same
`POST /feeds {channel_url}`. Search-result thumbnails get the same
same-origin avatar caching treatment described below, so a channel seen in
search and then followed doesn't re-fetch its avatar.

**Add feed** — `POST /feeds {channel_url}` → the user pastes any regular
YouTube channel URL (`/@handle`, `/channel/UC..`, `/c/..`, `/user/..`), not a
raw RSS link. `resolve_feed_url()` (in `rss.py`) resolves that to a
`channel_id`: a `/channel/UC..` URL is matched directly via regex (no
network call), anything else goes through yt-dlp (`extract_flat`,
`playlist_items=0` — fast, no video listing). An already-direct RSS feed
link is passed through unchanged. The resolved RSS URL is then validated
with feedparser (400 on failure either way) → feed saved → first parse run
immediately, writing new content rows (this initial parse is just the RSS
feed's normal ~15 most recent entries). If the URL resolved to a
`channel_id`, a one-time **backfill** (see below) is then kicked off in the
background to pull in the rest of the channel's history.

**Refresh** (`POST /feeds/refresh`, also run automatically once per browser
session — see Page load above) — Feeds are synced in two passes so DB writes
never happen off the request's own session:
1. `_fetch_feed_data()` (network-only, no DB writes) runs across all feeds
   in parallel via a small thread pool (`_REFRESH_POOL_SIZE = 8`, kept
   modest to stay polite to YouTube's unauthenticated scraping). For a feed
   with a known `channel_id` it fetches RSS scoped to the channel's
   Videos-tab playlist (`longform_feed_url()`, the `UULF` playlist) instead
   of the plain channel feed — Shorts are excluded there for free, no
   separate Shorts-tab check needed. Durations
   (`fetch_channel_video_durations()`, same `UULF` playlist — chosen over
   the `/videos` tab because some channels override that tab's default
   sort, e.g. "Popular", which can silently drop recent uploads from a
   bounded flat listing) are only fetched when some incoming video is new
   or some existing row is still missing `duration_seconds`. A channel
   avatar is fetched (and cached, see below) once per channel, ever —
   skipped as soon as the feed already has `avatar_url`.
2. `_apply_feed_data()` (DB-only, always sequential on the request's own
   session) inserts new content rows, backfills missing durations onto
   existing rows, and stores a newly fetched avatar path.

**Channel avatars** — Not present in the RSS feed or any playlist
extraction, so `fetch_channel_avatar_url()` does a separate lightweight
`yt-dlp` fetch of the channel page (`playlist_items=0`, no video listing).
Hotlinking Google's CDN URL directly from the browser (`<img src=googleusercontent-url>`)
turned out to intermittently fail Chrome's Opaque Response Blocking (ORB)
even for a URL that loaded fine moments earlier — so `downloader.download_avatar()`
fetches the bytes once server-side and saves them to `data/avatars/{channel_id}.jpg`,
re-served same-origin via `GET /avatars/{filename}` (path-traversal guarded:
rejects any name containing `/`, `\`, or a leading `.`). Channel search
results get the same caching (`_cached_or_downloaded_avatar()` in `rss.py`),
so a channel seen in search and then followed doesn't re-fetch its avatar.

**Backfill** — A one-time full-history scan (`_run_backfill()` in
`routers/feeds.py`, background task), separate from routine refreshes which
stay RSS-only (RSS only exposes recent entries). `fetch_channel_all_videos()`
does a single flat `yt-dlp` extraction of the channel's `UULF` playlist
(same one refresh uses for durations) to get every long-form video's id,
title, thumbnail, and duration in one pass. The uploads playlist doesn't
expose per-video publish dates, so backfilled rows get synthetic
`published_at` values — one second apart, counting back from the oldest
`published_at` already known for the feed — which preserves the true
newest-to-oldest order for date sort/filter without claiming a real date.
Progress is tracked in-memory per feed (`_backfill_progress`, phases
`scanning` → `saving` → `done`) and polled via
`GET /feeds/{id}/backfill-status`; the UI shows this behind a full-screen
overlay (`#backfill-overlay`) so a large channel's slow initial scan doesn't
read as the app being broken.

**Audio quality** — A per-user setting (`Settings` tab, `GET`/`PUT
/settings`) applied at download time, not retroactively — files already on
disk keep whatever quality they were downloaded at. `high` and `low` both
download YouTube's native `mp4a`/AAC stream (remux only, no local
transcoding); `low` additionally caps to YouTube's own ≤64kbps pre-encoded
stream where available. `medium` has no matching YouTube-native tier, so it
downloads the same high-quality source and re-encodes it locally to mp3 at
96kbps — the only tier that pays a transcode cost, which the Settings copy
calls out ("takes a bit longer to prepare").

**Export** — `GET /storage/export` streams every `ready` item as one
uncompressed (`ZIP_STORED`) zip — audio is already compressed, so
re-zipping it would just burn CPU for no size benefit. Duplicate filenames
(same sanitized title) get a `(2)`, `(3)`, … suffix. A single item can also
be exported individually via `GET /content/{id}/stream?download=1`, which
skips the `last_played_at` update a real playback triggers.

**Library controls (filter / pagination)** — Always newest-first
(`Content.published_at.desc()`, hardcoded); there's no sort control. An
earlier version had one (newest/oldest/title A–Z/Z–A/channel A–Z), but once
the filter box could search titles too, alphabetical sort mostly duplicated
"search for the thing you want," and oldest-first duplicated one click on
"Last »" — dropped rather than kept as unused surface. `content_query.py`'s
`query_content_page()` is the single shared implementation, used both for
the server-rendered page 1 (`pages.py`'s `home()`) and for every subsequent
filter/page change, so the two never disagree on what "page 1, no filter"
contains. `app.js`'s `refreshGridView()` calls `GET /content?page=&filter=`,
swaps `#content-grid`'s innerHTML with the returned page, and re-renders
pagination controls from the response's `total_pages` — no client-side
comparator or slicing logic. A monotonically increasing request sequence
number (`gridRequestSeq`) drops any response that arrives after a newer
request was already sent, so a slow stale fetch (e.g. after rapidly typing
into the search box) can't overwrite a newer one.
- **Filter** is two controls sharing one piece of state
  (`currentChannelFilter` in `app.js`), kept mutually exclusive — picking one
  clears the other:
  - A dropdown for the non-text filters: "All channels" (`filter=""`), "★
    Favorites" (`filter=__favorites__`), "Saved for later"
    (`filter=__saved__`), "Previously played" (`filter=__played__`,
    `Content.last_played_at IS NOT NULL` — set on stream, not on download).
  - A debounced (~300ms) search box matching, case-insensitively, either the
    video title or the channel title (`Feed.channel_title.ilike()` OR
    `Content.title.ilike()`) — so searching doesn't require knowing which
    field the match is in. Clicking a channel chip on Home
    (`setupHomeChannels()`) fills this box with that channel's name rather
    than picking it from a dropdown list of every followed channel.
- **Pagination**: `DEFAULT_PAGE_SIZE = 20`; `hidden` attribute toggles page
  visibility (see the CSS gotcha note below). Prev/Next scroll the window
  back to the top. An out-of-range page is clamped server-side.
- The filter persists to `localStorage` (the current page number resets on
  reload); `initializeLibraryGrid()` only re-fetches on load if a restored
  filter differs from the server-rendered default (page 1, no filter) —
  otherwise it just seeds pagination state from what's already in the DOM,
  avoiding a redundant fetch.

  > **CSS gotcha to remember**: any element whose class sets `display`
  > (`.card`, `.pagination`, `.tab-panel`, `.overlay` all set `display: flex`)
  > needs an explicit `.the-class[hidden] { display: none; }` rule. The
  > native `hidden` attribute and an author class rule both have equal CSS
  > specificity, and the author rule wins — so `element.hidden = true` alone
  > silently does nothing if the class already sets `display`. This caused a
  > real bug where pagination didn't actually hide the previous page's
  > cards. Any new toggle-visibility element needs this pattern from the
  > start.

**Save for later** — A bookmark toggle at the right end of each card's action
row (`margin-left: auto`, so it lines up across every card regardless of
whether the card shows Download / Play+delete / a spinner) calls
`POST`/`DELETE /content/{id}/save`. It deliberately does *not* float over the
artwork — an icon pinned onto a busy thumbnail reads as a stray artifact
rather than a control. Because the save toggle shares the action row, the
`updateCard()` re-render on any status change has to re-emit it, which is why
`cardActionHtml()` is composed of `statusActionHtml() + saveButtonHtml()`.

**Confirm dialogs & toasts** — Native `window.confirm`/`window.alert` are not
used anywhere. `static/js/ui.js` loads before the page script on every page
and exposes `confirmDialog(message, label) -> Promise<boolean>` (centered
modal, backdrop-click and Escape both cancel, focus lands on the *safe*
button so a reflexive Enter never confirms a destructive action) and
`showToast(message)` (bottom-centre, auto-dismissing). Both lazily create
their own DOM, so no template needs to carry markup for them.

**Download** — There is no download button. Every card just says **Play**;
opening the player is what fetches the audio, so downloads are a side effect
of listening rather than a separate chore, and the downloaded files are
treated as a disposable cache (see Downloaded audio below). Cards that are already on
disk get a small check badge on the artwork so you can tell what will start
instantly.

`POST /content/{id}/download` → 409 if already `downloading` →
`status='downloading'` set synchronously → yt-dlp job dispatched to the
background → response returns immediately. On completion: `status='ready'` +
`file_path`; on failure: `status='error'` + `error_message`.

> **Never start a download on plain page load.** Browsers speculatively load
> links — a prerender executes the target page's JavaScript — so simply having
> `<a href="/player/{id}">` on a card is enough for the browser to open the
> player behind your back and, if the player downloads unconditionally, fill
> the user's disk with things they never clicked. This actually happened
> during development (server logs showed `GET /player/N` + `POST
> /content/N/download` for pages nobody visited). `player.js` therefore gates
> `prepareAudio()` behind `whenVisible()`, which waits out
> `document.prerendering` and a non-`visible` `visibilityState`.

**Polling** — While the player is preparing, it polls
`GET /content/{id}/status` every 1.5s until `ready` or `error`.

**Downloaded audio** — The Settings tab's "Manage downloads" modal
(`#downloads-overlay`) lists everything on disk with per-item sizes, a
total, per-item removal, per-item export, and "Clear all"/"Export all".
Sizes are read from disk at render time rather than stored, so a file
deleted behind the app's back reports 0 instead of inflating the total. Two
things to keep in mind:
- Content rows cascade-delete with their feed, but **files do not** —
  `delete_files_for_feed()` runs before a feed is deleted, otherwise
  unfollowing a channel strands its audio on disk permanently.
- `clear_all()` also sweeps any `*.{AUDIO_FORMAT}` in the storage directory
  that no row points at, so "Clear all" really does free everything it
  claims (older builds leaked exactly these orphans).

**Play** — `GET /player/{id}` renders a custom player rather than a native
`<audio controls>` bar (which renders as a light pill that clashes badly with
the dark theme). The `<audio>` element is hidden and driven by
`static/js/player.js`: large artwork, title/channel, a seek bar with
elapsed/total times, ±15s skip buttons flanking a large round play/pause
button, and a footer row with volume and the favorite toggle. Keyboard:
Space toggles playback, ←/→ skip. `<input type=range>` can't style its
already-played portion, so both sliders paint it with a gradient driven by a
`--fill` custom property that the JS keeps in sync.

The stream endpoint returns 409 if `status != ready`; the file path always
comes from the DB, never from the request (prevents path traversal). Every
stream request stamps `last_played_at = now()` (skipped for a plain
`?download=1` export, which isn't the user actually listening) — this is
what feeds the Home page's "Recently played" shelf.

**Delete** — Removing a download (Manage downloads modal, per item or in
bulk) deletes the file and resets the row to `status='not_downloaded'` +
`file_path=NULL`. The row is kept, so the item stays in the Library and
comes back just by playing it again. Confirmed through the shared modal, not
`window.confirm`.

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
- `AUDIO_FORMAT` — default `m4a` (the `high`/`low` audio-quality tiers'
  extraction target; matches YouTube's native audio stream for almost every
  video, so extraction is a fast remux rather than a re-encode — see Audio
  quality above. `medium` always re-encodes to mp3 regardless of this
  setting)
- `HOST_PORT` — default `8000`; docker-compose–only (publishes the container's
  fixed internal port 8000 on this host port), not read by the app itself

Also configurable (not currently exposed in `.env.example`, code default
only): `AVATARS_DIR` — default `/app/data/avatars`.

`requirements.txt`: `fastapi`, `uvicorn[standard]`, `sqlalchemy`,
`feedparser`, `yt-dlp`, `jinja2`, `python-multipart`, `itsdangerous`,
`pydantic-settings`

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
  maps `${HOST_PORT:-8000}:8000` (override `HOST_PORT` in `.env` if 8000 is
  taken on the host), mounts `./data:/app/data` (persists the SQLite DB and
  downloaded audio across container restarts/upgrades), reads env vars from
  `.env`.
- **Static assets are served with `Cache-Control: no-cache`**
  (`RevalidatingStaticFiles` in `main.py`). Starlette's `StaticFiles` sends
  ETag/Last-Modified but no `Cache-Control`, and browsers then fall back to
  *heuristic* freshness — serving cached CSS/JS for a while without asking the
  server at all. After `docker compose up -d --build` that means a user can be
  running new templates against stale CSS/JS, which renders as a subtly or
  completely broken UI (buttons unpositioned, dialogs invisible) with nothing
  in the logs. `no-cache` still permits caching, it just forces revalidation;
  unchanged files come back as a cheap 304. Do not "optimise" this into a long
  max-age without adding content-hashed asset URLs first.
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

MVP (v1), one milestone at a time, each tested end-to-end before moving on:

1. Project scaffold + DB models, FastAPI boots empty
2. Login/session (shared password) wired in, gating all routes
3. Feed add + RSS parse (backend, tested via curl)
4. Home page UI: feed form + content grid
5. Download + polling + loading/play state
6. Player page + stream endpoint
7. Delete
8. Dockerfile + docker-compose + README for self-hosting

Post-MVP additions, same one-at-a-time/test-then-continue approach:

9. Resolve any pasted channel URL to its RSS feed via yt-dlp (no more asking
   users to construct an RSS link by hand)
10. Mobile-responsive layout (sidebar/grid stacking, 2-column mobile grid)
    and client-side content sorting
11. Library/Channels tabs, replacing the old sidebar+content two-column
    layout
12. Channel search (YouTube search results, channel-type filtered) with
    add-from-results, alongside the existing add-by-URL form
13. Client-side pagination and channel filtering over the content grid,
    unified with sorting in one `refreshGridView()` pass
14. Favorites (star toggle, `is_favorite` column + migration), later folded
    into the channel filter dropdown as a filter value rather than a
    separate checkbox
15. Full-screen loading overlay for background refreshes, replacing inline
    status text that was breaking the filter row's layout
16. Card polish: fixed-height titles (so action buttons align regardless of
    title length), a plain SVG trash icon instead of a colored emoji, video
    duration badges (backfilled via the channel's uploads playlist)
17. Exclude YouTube Shorts from import entirely (checked against the
    channel's Shorts tab, not a duration heuristic) — doesn't fit a
    "listen to it" library
18. Serve static assets with `Cache-Control: no-cache` — without it,
    browsers heuristically cached CSS/JS and kept rendering post-upgrade
    pages with pre-upgrade styles
19. Replaced `window.confirm`/`window.alert` with a shared modal + toast
    (`static/js/ui.js`)
20. Custom player (seek, ±15s, volume, keyboard) replacing native
    `<audio controls>`; favorite toggle moved from the grid into the player
21. "Save for later" (`is_saved`) as a separate bookmark toggle in the card
    action row, filterable alongside channels and favorites
22. Cards reduced to a single **Play** action — playing is what downloads,
    with the player showing a preparing state — plus a **Storage** tab for
    per-item and bulk cache clearing, a downloaded badge on cached items,
    a guard so speculative link prerendering can't start downloads, and
    cleanup of audio orphaned by unfollowing a channel
23. Refresh only on first load per session (not every visit), download
    progress percentage surfaced during download/convert, faster conversion
24. Per-user audio quality setting (`high`/`medium`/`low`, `Settings` tab)
25. One-time full channel history backfill on adding a feed (RSS alone only
    exposes recent entries), with a progress overlay; ArcticTerminal branding
26. **Home** tab added: curated shelves (followed channels, new uploads,
    recently played, favorites, saved); `last_played_at` tracking added to
    drive "Recently played" (set on stream, not on download)
27. Channel avatars: fetched once per channel and served from our own
    origin instead of hotlinked (Chrome's Opaque Response Blocking made
    direct hotlinking unreliable); **Manage**/**Settings** tabs replacing
    the old Channels/Storage tabs (Storage folded into a "Manage downloads"
    modal off Settings); feed refresh parallelized across a thread pool;
    zip export of all downloaded audio, and per-item export
28. Same local-avatar caching applied to channel search results, not just
    followed channels
29. Library pagination (and sort/filter) moved server-side
    (`content_query.py`, shared by the server-rendered page 1 and the AJAX
    endpoint), replacing the earlier client-side comparator/slice approach;
    duration-formatting logic deduplicated
30. Starter pytest suite (auth, content API, content_query, formatting/rss
    helpers)
31. Card titles/channel names made clickable (not just the thumbnail);
    shelf drag-to-scroll no longer hijacked by native link/image drag;
    Manage tab bulk import (paste a list of handles/URLs/Takeout CSV rows,
    resolved in parallel, imported sequentially with live progress)
32. Library's channel dropdown replaced by a debounced search box matching
    title or channel (clicking a Home channel chip now fills this box
    instead of picking from a dropdown of every followed channel); added a
    "Previously played" filter; removed the sort control entirely — once
    the search box covered "find a specific thing," alphabetical sort mostly
    duplicated it, and oldest-first duplicated one click on "Last »"

### Verifying UI work

Browser tooling isn't wired into this environment, and reasoning about CSS
without rendering it has produced shipped-broken UI before. The reliable loop
is a headless screenshot: drive the locally installed Chromium-based browser
with `puppeteer-core` (`executablePath` pointed at it — no browser download
needed), log in, navigate, optionally click through to a state (modal open,
filter applied), and screenshot. Check both desktop and phone viewports, and
assert on real post-interaction state (`dataset` values after a reload) rather
than assuming the handler worked.
