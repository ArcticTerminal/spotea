# Spotea — Architecture

A self-hosted, open-source tool for archiving and listening to content from
YouTube channels via RSS. Users register an account, follow channels via
RSS, the app periodically parses them, lets the user download selected
videos as audio (via yt-dlp), and play them back from the browser. Designed
to be shared on GitHub and run by others via Docker on their own servers —
one deployment can now serve several independent households, each with
their own login and their own set of profiles.

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
data/thumbnails/{video_id}.jpg (Docker volume)
data/spotea.db                (Docker volume)
```

A background asyncio task (`app/scheduler.py`, started/cancelled in
`main.py`'s lifespan) refreshes every followed feed on a shared interval,
independent of any request — see §5's Refresh section.

## 2. Project structure

```
spotea/
  app/
    main.py               # FastAPI app + router mount + startup (create_all,
                           # migrations, AppSettings bootstrap, scheduler)
    config.py              # env-based settings
    database.py             # SQLAlchemy engine/session
    migrations.py            # lightweight "add column if missing" patcher +
                              # legacy-account backfill for pre-account installs
    content_query.py           # shared query building blocks: pagination,
                                # new_upload_filter(), followed_feeds()
    app_settings.py             # the singleton AppSettings row, created on
                                 # first access
    templating.py                # the one Jinja environment (filters included)
    page_context.py               # template context for index.html's
                                   # re-renderable regions, shared by the full
                                    # page render and the fragment endpoints;
                                    # also queue_thumbnail_caching()
    feed_sync.py                  # network+DB feed refresh, shared by the
                                   # on-demand /feeds/refresh route and the
                                   # background scheduler; also
                                   # cache_thumbnail() (see Channel avatars/
                                   # Thumbnail caching in §5)
    scheduler.py                   # background feed-refresh loop, on
                                    # AppSettings.feed_refresh_interval_minutes
    formatting.py                   # Jinja filters (duration, file size),
                                     # filesystem-safe filenames
    timeutil.py                      # utcnow() — the single naive-UTC source
                                      # every datetime column agrees on
    progress.py                       # ProgressRegistry: expiring in-memory
                                       # progress for downloads, backfills and
                                        # bulk imports (single-process only)
    storage.py                       # disk usage, cache clearing, orphan
                                      # sweep, zip export, content purge
    models.py                         # Account, User, AppSettings, Feed, Content
    schemas.py                         # Pydantic request/response models
    auth.py                             # password hashing (bcrypt), session-key
                                        # constants
    deps.py                              # get_db, require_login,
                                          # get_current_account, get_current_profile
    images.py                             # avatar/thumbnail fetch + on-disk cache
    downloader.py                          # yt-dlp audio extraction (audio only)
    youtube/
      urls.py                                # URL shapes, id conventions, regexes
                                              # (no network)
      rss.py                                 # feedparser: a channel's RSS feed —
                                              # the cheap path routine refreshes use
      extract.py                             # yt-dlp: channel resolution,
                                              # durations, avatars, full-history scan
      search.py                              # yt-dlp: Explore's channel/video search
    services/
      feed_add.py                            # create (or re-follow) a Feed from a
                                              # channel URL — shared by the single
                                              # add route and bulk import
      backfill.py                            # one-time full-history scan + its
                                              # progress registry
      bulk_import.py                         # parallel resolve, then sequential
                                              # create, for a pasted channel list
    routers/
      auth.py                                # login/register/logout
      profiles.py                            # profile CRUD + switch,
                                              # account-scoped
      feeds.py                               # follow/unfollow/refresh, backfill
                                              # status, bulk import endpoints
      explore.py                             # channel/video search + single-video
                                              # add/remove (still under /feeds)
      content.py                             # list/get/download/status/stream/
                                              # favorite/save/delete, plus the
                                              # play-queue id lists (see §4)
      storage.py                             # clear-all + zip export endpoints
      settings.py                            # per-profile audio quality +
                                              # deployment-wide refresh interval
      pages.py                               # home (shelves) rendering; the
                                              # rest are one-line redirects to
                                              # their hash route (see §5)
      partials.py                            # re-render one region of index.html
                                              # on demand, including the channel/
                                              # playlist detail panel (see §4)
    templates/
      _base.html                                   # page shell every full page
                                                     # extends (head/PWA meta,
                                                     # body class, script slot)
      _icons.html                                  # inline <symbol> sprite —
                                                     # every icon's geometry,
                                                     # defined once, referenced
                                                     # by <use href="#i-...">
      login.html
      register.html
      index.html                                   # Home/Library/Explore/Settings/
                                                     # detail — the whole app, one
                                                     # document (a single-page app)
      _player_overlay.html                          # persistent in-page player
                                                     # + mini-player bar, usable
                                                     # from anywhere in the app
      _player_controls.html                         # the transport (±15s, prev/
                                                     # next, play)/seek/volume/
                                                     # shuffle/favorite half of
                                                     # the player, shared verbatim
                                                     # by the overlay (its only
                                                     # caller)
      _home_shelves.html                           # Home's chips + four shelves
                                                     # (also GET /partials/home)
      _library_grid.html                           # Library's channel grid
                                                     # (also GET /partials/library)
      _downloads.html                              # Downloads modal body
                                                     # (also GET /partials/downloads)
      _detail_panel.html                           # one channel or pinned
                                                     # playlist's Play all /
                                                     # shuffle controls + track
                                                     # list — index.html's 5th
                                                     # panel, client-fetched
                                                     # (see §5)
      _fragment_{home,library,downloads,detail}.html  # the <template data-target>
                                                     # wrappers those endpoints return
      _content_card.html                           # grid card partial (Home
                                                     # shelves, Library's pinned
                                                     # virtual-playlist tiles)
      _content_row.html                             # list-row partial (the
                                                     # detail panel's track list)
      _pagination.html
    static/
      js/                                    # ES modules; each page loads one
                                              # entry point via <script type="module">
        core.js                              # escaping/formatting, the api()
                                              # JSON helper, toast, confirm
                                              # dialog, overlay open/close
        resume.js                            # playback resume across loads,
                                              # the bfcache reload rule, service
                                              # worker registration
        player.js                            # the audio player, driving the
                                              # overlay's markup (its only caller)
        fragments.js                         # re-render page regions from the
                                              # server after an action, instead
                                              # of patching the DOM by hand
        content-actions.js                   # save toggle + unfollow, on every
                                              # page that shows content
        home/tabs.js                         # Home/Library/Explore/Settings/
                                              # detail panel switching
        home/detail.js                       # the channel/playlist detail panel:
                                              # opening it, its pagination, hash
                                              # routing (see §5)
        home/library.js                      # channel chips, library search,
                                              # drag-scroll rows, mobile menu,
                                              # manual refresh
        home/overlay.js                      # in-page player + mini bar,
                                              # auto-advance and the transport's
                                              # previous/next/shuffle controls
        home/queue.js                        # the play queue behind "Play all"
                                              # — order, shuffle, position;
                                              # state only, no player access
        home/explore.js                      # search, add channel, backfill
                                              # progress overlay
        home/settings.js                     # settings controls + downloads modal
        home/bulk-import.js                  # paste-a-list import modal
        home/profiles.js                     # profile switcher + manage UI
        pages/index.js                       # index.html's one entry point
        sw.js                                # PWA service worker (installability
                                              # only, not offline-first; a classic
                                              # script, not a module)
      css/style.css
      manifest.json                                              # PWA manifest
  data/                                 # mounted as Docker volume
    storage/{video_id}.{AUDIO_FORMAT}
    avatars/{channel_id}.jpg              # cached channel avatars, re-served via
                                           # GET /avatars/{filename}
    thumbnails/{video_id}.jpg             # lazily cached video thumbnails,
                                           # re-served via GET /thumbnails/{filename}
    spotea.db
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

**accounts** — `id PK`, `email` (unique, always stored lowercased —
normalized at the auth-router call sites, so no case-insensitive collation
is needed), `password_hash` (bcrypt), `created_at`,
`last_active_profile_id` (nullable, plain int — see below). The real,
credentialed login: owns one or more `users` profiles (household model, one
account, several family-member profiles).

`last_active_profile_id` is deliberately **not** a real foreign key. Session-
stored `PROFILE_SESSION_KEY` doesn't survive logout (the whole session is
cleared), so without persisting it *somewhere* durable, logging back in
always fell back to the account's first profile regardless of which one was
active before. A real FK to `users.id` would make `accounts` and `users`
reference each other, and `Base.metadata.create_all()` can't topologically
order a table-creation cycle — so it's a plain column, and its validity
(still one of this account's own profiles) is checked at the application
layer in `get_current_profile` instead.

**users** — `id PK`, `account_id FK`, `name`, `audio_quality` (`high`/`low`,
default `high` — see Settings below; the CHECK constraint on a *pre-existing*
database can still say `high`/`medium`/`low`, since SQLite can't alter a
CHECK constraint in place — see Schema evolution). A profile: owns its own
feeds/content, but authenticates through its owning account, not on its own.

**app_settings** — `id PK` (fixed singleton row, `id=1`),
`feed_refresh_interval_minutes` (default 30). Deployment-wide, not per-
account or per-profile — one background refresh loop shared by every
account's feeds. Making this per-account was considered and deliberately
deferred (see Roadmap): it's a minor operational knob, and doing it properly
would need per-account last-refreshed tracking in the scheduler, not just an
extra column.

**feeds** — `id PK`, `user_id FK`, `rss_url`, `channel_title` (auto-filled on
first parse), `avatar_url` (nullable — same-origin path under `/avatars/`,
fetched once per channel), `added_at`, `followed` (default `True` — `False`
only for placeholder feeds auto-created to hold a single video added via
Explore's song search; see §5).
Unique: `(user_id, rss_url)`.

**content** — `id PK`, `feed_id FK`, `user_id FK` (denormalized for query
convenience), `video_id` (YouTube ID), `title`, `thumbnail_url`,
`duration_seconds` (nullable — backfilled from the channel's uploads
playlist, not in the RSS feed), `published_at`, `status`, `file_path`,
`file_size_bytes` (nullable — recorded once when a download finishes;
`NULL` means "not measured yet" and is filled in from disk the first time
`storage.collect_usage` sees the row. Stored rather than stat'ed on demand
because `collect_usage` runs on every Home render, and reading it from disk
cost one syscall per downloaded track just to show a total. Cleared
alongside `file_path` whenever a download is removed),
`error_message`, `added_at`, `downloaded_at`, `last_played_at` (nullable —
set on stream, not on download; drives "Recently played"/"Recently Played"),
`is_favorite`, `is_saved`, `is_preview` (default `False` — `True` for a
just-added Explore song preview that hasn't been favorited/saved yet, see
§5), `is_new_upload` (default `False` — `True` for a row inserted or
re-matched by an RSS parse, as opposed to a full-history backfill scan; this
is what "New Uploads" actually means, see §5).
`is_favorite` and `is_saved` are separate on purpose: **saving** is a
lightweight "come back to this" bookmark, toggled from a card without
opening anything; **favoriting** is a considered "I liked this," toggled
from the player while actually listening. Both are pinned virtual playlists
in Library.
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
only creates tables that don't exist yet; it never alters existing ones, so
a brand-new table (like `accounts`) needs no extra work here — new installs
get it for free. Each *column* added to an existing table after its initial
release is registered in `app/migrations.py`'s `_COLUMN_MIGRATIONS` list,
which runs on every startup and adds it via `ALTER TABLE ... ADD COLUMN` if
missing — a lightweight stand-in for a real migration framework (Alembic
would be overkill at this scale).

Two data backfills run alongside the column patcher:
- Existing `ready` rows predating `last_played_at` get it set from
  `downloaded_at` as the closest proxy for "this was actually played" (so
  "Recently played" doesn't wrongly treat already-downloaded content as
  never played), and any profile still on the retired `medium` audio-quality
  tier falls back to `low` (see Audio quality in §5).
- **The account backfill**: a pre-existing single-tenant database has
  `users` rows with no owning `account_id` (that column didn't exist before
  accounts did). `migrations.py` folds every such orphaned profile into one
  new legacy `Account` — email `owner@local`, password hashed from whatever
  `APP_PASSWORD` was already set in `.env` — so an existing deployment keeps
  working immediately after upgrading, no data re-entry needed. If orphaned
  profiles exist but `APP_PASSWORD` is unset, this raises loudly at startup
  rather than silently creating an account with no usable password (a
  quiet-lockout footgun). A fresh install has no orphaned rows, so this is a
  no-op and `APP_PASSWORD` isn't required at all — see §7.

## 4. Endpoints

**Auth**

| Method | Path | Purpose |
|---|---|---|
| GET | `/login` | Login page |
| POST | `/login` | Verify email+password (bcrypt), set session cookie |
| GET | `/register` | Registration page |
| POST | `/register` | Create an Account + its first profile, log in |
| POST | `/logout` | Clear session |

**Profiles** (all account-scoped — see §6)

| Method | Path | Purpose |
|---|---|---|
| GET | `/profiles` | List the current account's profiles |
| POST | `/profiles` | Create a profile, auto-switch into it |
| PUT | `/profiles/{id}` | Rename |
| DELETE | `/profiles/{id}` | Delete (refuses the account's last remaining profile) |
| POST | `/profiles/{id}/switch` | Switch the active profile |

**Feeds**

| Method | Path | Purpose |
|---|---|---|
| GET | `/feeds/search?q=` | Search YouTube channels by name (Explore) |
| GET | `/feeds/search-videos?q=` | Search YouTube videos/songs by name (Explore) |
| POST | `/feeds` | Add a channel by URL (resolved to RSS via yt-dlp), run first parse, kick off background history backfill |
| GET | `/feeds` | List followed feeds |
| DELETE | `/feeds/{id}` | Unfollow (keeps anything downloaded/played/favorited/saved, downgrades the rest) |
| POST | `/feeds/refresh` | Re-parse this profile's feeds, insert new content rows |
| GET | `/feeds/{id}/backfill-status` | Poll progress of the one-time full channel history scan |
| POST | `/feeds/import` | Start a bulk import job (many channels at once) |
| GET | `/feeds/import/{job_id}/status` | Poll bulk import progress |
| POST | `/feeds/videos` | Add one video without following its channel (Explore song search) |
| DELETE | `/feeds/videos/{content_id}` | Remove a video added this way |

**Partials** — server-rendered fragments of index.html

Every one of these returns the same markup the full page renders for that
region, from the same context functions (`app/page_context.py`). The client
swaps them in after an action instead of hand-patching the DOM; see
`routers/partials.py` for why. Each response is one or more
`<template data-target="…">` blocks. The two detail endpoints are the
exception to "same as the full page renders" — the detail panel isn't SSR'd
anywhere, so they're the *only* render of that markup, not a refresh of one
(see §5's Library section).

| Method | Path | Purpose |
|---|---|---|
| GET | `/partials/home` | Home's channel chips and its four shelves |
| GET | `/partials/library` | Library's channel grid and virtual-playlist counts |
| GET | `/partials/downloads` | Downloads modal body + the Settings storage line |
| GET | `/partials/detail/channel/{feed_id}` | One channel's track list (paginated) |
| GET | `/partials/detail/playlist/{kind}` | One pinned playlist's track list (paginated); `kind` is `favorites`/`saved`/`new-uploads`/`recently-played` |

**Content**

| Method | Path | Purpose |
|---|---|---|
| GET | `/content?page=&filter=` | JSON content page, newest-first (server-side filter/pagination; not currently called by the UI, but kept as a tested API surface — see §5) |
| GET | `/content/{id}` | Single-item JSON fetch, powers the Home/Library/Explore overlay |
| GET | `/content/queue/channel/{feed_id}` | Every content id in one channel, in track-list order — the "Play all" queue (unpaginated, capped at `QUEUE_MAX_ITEMS`) |
| GET | `/content/queue/playlist/{kind}` | Same for one pinned playlist |
| DELETE | `/content/recently-played` | Clear the "Recently played" shelf (doesn't delete downloads) |
| POST | `/content/{id}/download` | Start yt-dlp download in the background; a no-op for a track already on disk |
| GET | `/content/{id}/status` | Current status (+ download/convert progress) |
| GET | `/content/{id}/stream?download=` | Serve the audio file (Range-request support); `download=1` skips the `last_played_at` update |
| POST/DELETE | `/content/{id}/favorite` | Mark/unmark as favorite |
| POST/DELETE | `/content/{id}/save` | Save/un-save for later |
| DELETE | `/content/{id}` | Delete file, reset status to `not_downloaded` |

**Storage / Settings / static**

| Method | Path | Purpose |
|---|---|---|
| DELETE | `/storage` | Delete every downloaded file for this profile |
| GET | `/storage/export` | Download every ready file as one uncompressed zip |
| GET | `/settings` | Current profile's audio quality + the deployment's refresh interval |
| PUT | `/settings` | Update either |
| GET | `/avatars/{filename}` | Serve a cached channel avatar from our own origin |
| GET | `/thumbnails/{filename}` | Serve a cached video thumbnail from our own origin |
| GET | `/sw.js` | Service worker (no login required — installability checks may fetch it pre-session) |
| GET | `/health` | Liveness check |

**Pages**

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Home/Library/Explore/Settings/detail SPA |
| GET | `/favorites`, `/saved`, `/new-uploads`, `/recently-played`, `/channel/{feed_id}`, `/player/{content_id}` | Redirect to the equivalent hash route (`/#favorites`, `/#channel/{id}`, …) — kept only for old links/bookmarks; see §5 |

All routes except `/login`, `/register`, `/health`, `/sw.js`, and static
assets require an active session (enforced via `require_login` or, where the
handler needs the account/profile object, `get_current_account`/
`get_current_profile`).

## 5. User flows

**Register** — `GET/POST /register`: email, password, confirm password.
Validated server-side (`routers/auth.py`): a light regex for email shape
(no `email-validator`/`EmailStr` dependency added just for this), password
8–72 bytes (bcrypt silently truncates past 72, so the upper bound matters
too, not just a minimum), passwords must match, email not already
registered. On success, an `Account` and its first `User` profile ("My
Profile") are created in the same transaction, `db.flush()`'d so their ids
exist before `db.commit()`; a concurrent duplicate-email registration racing
past the pre-check is still caught by the `IntegrityError` on `Account.email`'s
unique constraint. The new session is logged in immediately (both
`ACCOUNT_SESSION_KEY` and `PROFILE_SESSION_KEY` set) — no separate confirm
step.

**Login** — `GET/POST /login`: email+password, verified with
`bcrypt.checkpw` against the stored hash. Failure is one generic "Invalid
email or password" either way (unregistered email vs. wrong password aren't
distinguished, so a login attempt can't be used to enumerate registered
emails). On success, `ACCOUNT_SESSION_KEY` is set to the real `Account.id`;
`PROFILE_SESSION_KEY` is seeded from `Account.last_active_profile_id` if
set, so a fresh login lands back on whichever profile was active before
logout rather than always defaulting to the first one. If that stored
profile id is stale (deleted since), `get_current_profile`'s self-heal
(see §6) picks the account's first profile on the next profile-scoped
request — no forced re-login.

**Page load & background refresh** — The page renders immediately with
existing DB content; there's no client-triggered "refresh once per session"
step anymore (an earlier version had one — see §11 — superseded once the
background scheduler existed). `app/scheduler.py`'s `run_scheduler()` runs
for the app's lifetime: it sleeps for `AppSettings.feed_refresh_interval_minutes`
(a presets-only Settings control — 15/30/60/120 — see Audio quality/refresh
interval below), then refreshes every `followed=True` feed across every
account/profile via `feed_sync.refresh_feeds`. Changing the interval in
Settings calls `scheduler.request_reschedule()`, which cuts the current
sleep short via an `asyncio.Event` (recreated per run — a module-level
singleton would be bound to whichever event loop created it, which breaks
across the test suite's per-test fresh app lifespan) so the new interval
takes effect immediately instead of after the old one finishes. A manual
"Refresh feeds" button (topbar / mobile menu) still exists for "I want it
now": `POST /feeds/refresh`, profile-scoped, unconditionally followed by a
full page reload (see the Refresh section below for why *unconditionally*).

**Tabs** — The SPA is split into **Home** (shelves), **Library** (a grid of
followed channels plus four pinned virtual-playlist tiles), **Explore**
(channel/song search + add, merged from an earlier standalone "Manage" tab),
and **Settings** — `<section>` panels toggled via the `hidden` attribute
driven by `html[data-active-tab]` (see style.css), not separate routes. An
inline `<head>` script resolves the active tab (URL hash, falling back to
`localStorage`) and sets it on `<html>` *before first paint*, so a reload
never flashes the wrong tab; `home/tabs.js`'s `setupTabs()` just syncs
button/URL state to match afterward. Tab switches use `history.replaceState`
(not `pushState`) so cycling tabs doesn't spam back-button history.

A fifth panel, **detail** (`data-active-tab="detail"`, `home/detail.js`),
holds one channel or pinned playlist's track list — reached from a Library
card/tile, a Home channel chip, or a shelf's "See more". Unlike the four
tabs, opening it *does* `pushState` (to `#channel/{id}` or `#favorites` etc.
— see `core.js`'s `classifyHash`), so the browser back button returns to
wherever you actually came from. It has no server-rendered content of its
own on first paint (see the Library section below) and no `.tab-btn` of its
own — the Library button stays visually selected while it's open, since
that's conceptually where "Back" returns to.

**Home page** — Assembled in `pages.py`'s `home()`, each shelf its own
bounded query (`HOME_SHELF_LIMIT = 12`) rather than one big query sliced in
Python — that used to get slow once full-channel backfills pushed a
profile's content well past a few thousand rows:
- **Recently followed** — avatar chips, most-recently-followed few (not
  every channel — with 100+ followed, the full list made this an endless
  scroll; Library's channel grid still lists every channel).
- **New uploads** — RSS-sourced content (`is_new_upload=True`) published
  within the last 14 days (`content_query.NEW_UPLOAD_MAX_AGE`) from a still-
  followed channel.
- **Recently played** — most recent by `last_played_at`; hidden if empty.
  Live-patched in without a reload when something plays via the overlay
  (see `syncRecentlyPlayedShelf` below) — this shelf used to always end up
  fresh "for free" because playing anything meant navigating to
  `/player/{id}` and back, which isn't true anymore now that Home/Library/
  Explore play through the in-page overlay instead.
- **Favorites** / **Saved for later** — same as their pinned-playlist pages;
  hidden if empty.

**New Uploads, in depth** — `Content.is_new_upload` alone would mean "RSS-
sourced, ever" forever, so it's always paired with a 14-day
`published_at` cutoff (`content_query.new_upload_cutoff()`) — the single
place that age limit is defined, shared by the Home shelf, the
`/new-uploads` page, and Library's New Uploads tile count, so they can never
disagree on what counts as new. `feed_sync.apply_feed_data` is the only
place a row is ever marked `is_new_upload=True`: both a channel's initial
follow and every later routine refresh set it on newly-inserted rows, and —
this is the self-healing part — also re-mark any *already-existing* row
that's still part of the channel's current RSS window but wasn't flagged yet
(e.g. it predates this column, or came in through backfill). That's what
lets New Uploads populate itself from whatever a channel's feed currently
shows, rather than staying permanently empty for anything older than the
column. `_run_backfill`'s own inserts bypass `apply_feed_data` entirely,
which is what keeps historical backfill out of "New Uploads" by design.

**Library** — No longer a searchable/filterable video grid (see §11 — an
earlier version worked that way). It's a grid of channel cards
(`channel-grid`): four pinned tiles (Favorites, Saved for later, New
Uploads, Recently Played) followed by every followed channel. Clicking any
of them — or a Home channel chip, or a shelf's "See more" — opens the
**detail** panel in place (`home/detail.js`'s `openDetail()`; see the Tabs
section above), not a navigation: it fetches
`GET /partials/detail/channel/{id}` or `GET /partials/detail/playlist/{kind}`
and swaps the response into `#detail-panel`. `ctrl`/`cmd`-click still opens
in a new tab, since every card's `href` is a real, navigable hash URL
(`/#channel/{id}`, `/#favorites`, …) — a cold load at one of those resolves
the same way (see `handleInitialRoute()`), just with a brief loading spinner
since, unlike Home/Library, the detail panel isn't server-rendered inline on
first paint. The only client-side filtering left here is
`setupLibrarySearch()` matching channel *names* — no server round trip,
since every card is already in the DOM. Per-video browsing (search substring
against title-or-channel, an exact channel filter, pagination) all lives
server-side in `content_query.query_content_page()`, shared by
`page_context.channel_detail_context()`/`playlist_detail_context()` behind
those same two `/partials/detail/...` endpoints. The old real pages this
replaced (`/channel/{id}`, `/favorites`, `/saved`, `/new-uploads`,
`/recently-played`) are now one-line redirects to their hash equivalent, kept
only so an old link or bookmark still lands somewhere real. The JSON
`GET /content?page=&filter=` endpoint that used to back an AJAX-swapped grid
still exists and is exercised by the test suite, but nothing in the current
UI calls it — the detail panel's list is server-rendered HTML per page, not
fetched-as-JSON and spliced into a shared grid.

**Search & add a channel** — Explore's one search box (debounced ~400ms)
fires both `GET /feeds/search-videos?q=` and `GET /feeds/search?q=` in
parallel (`Promise.allSettled`, so one slow/failed leg doesn't block the
other) and renders two sections, Songs and Channels. `search_channels()`
(`youtube/search.py`) runs a `yt-dlp` flat extraction of YouTube's channel-filtered
search results and returns channel_id/title/thumbnail/subscriber count.
Clicking "Add" on a channel result — or a URL pasted into `POST /feeds`
directly — both hit the same add-feed path. Search-result thumbnails get
the same same-origin avatar caching described below, so a channel seen in
search and then followed doesn't re-fetch its avatar.

**Search & play a song** — `search_videos()` (`youtube/search.py`) is a separate,
untyped YouTube search (no channel-type filter — Explore already wants mixed
video results). Clicking a song result hits `POST /feeds/videos`
(`add_single_video`), which resolves the video's authoritative channel via a
real (non-flat) yt-dlp lookup — a flat search result's `channel_id` can be
missing or ambiguous for a collab video — attaches it to a placeholder
`Feed` (`followed=False`, created on demand by
`_get_or_create_placeholder_feed`, invisible in Library and skipped by the
background scheduler until the channel is actually followed for real), and
inserts the `Content` row as `is_preview=True`. A preview plays normally
through the overlay like anything else, but stays out of Library/New
Uploads until the user favorites or saves it (`add_favorite`/`add_saved`
clear `is_preview` as a side effect) — otherwise "just tried a song" would
look identical to "deliberately added this." If the video already has a
`Content` row for this profile (an earlier preview, or a real upload from a
followed channel), this is a no-op that just hands back that row's id and
plays it.

**Add feed** — `POST /feeds {channel_url}` → the user pastes any regular
YouTube channel URL (`/@handle`, `/channel/UC..`, `/c/..`, `/user/..`), not a
raw RSS link. `resolve_feed_url()` (in `youtube/extract.py`) resolves that to a
`channel_id`: a `/channel/UC..` URL is matched directly via regex (no
network call), anything else goes through yt-dlp (`extract_flat`,
`playlist_items=0` — fast, no video listing). An already-direct RSS feed
link is passed through unchanged. If a matching `Feed` already exists with
`followed=False` (a placeholder from Explore's song search — see above),
following it for real *upgrades that same row in place* instead of creating
a duplicate — same `rss_url` shape (`channel_feed_url()`) is what makes that
lookup reliable. Otherwise a new row is created. Either way: RSS validated
with feedparser (400 on failure) → first parse run immediately (the RSS
feed's normal ~15 most recent entries) → if the URL resolved to a
`channel_id`, a one-time **backfill** (see below) is kicked off in the
background to pull in the rest of the channel's history.

**Refresh** (`POST /feeds/refresh`, on-demand; the background scheduler
above runs the same underlying `feed_sync.refresh_feeds()`) — Feeds are
synced in two passes so DB writes never happen off the calling session:
1. `fetch_feed_data()` (network-only, no DB writes) runs across all feeds in
   parallel via a small thread pool (`REFRESH_POOL_SIZE = 8`, kept modest to
   stay polite to YouTube's unauthenticated scraping). For a feed with a
   known `channel_id` it fetches RSS scoped to the channel's Videos-tab
   playlist (`longform_feed_url()`, the `UULF` playlist) instead of the
   plain channel feed — Shorts are excluded there for free. Durations
   (`fetch_channel_video_durations()`, same `UULF` playlist) are only
   fetched when some incoming video is new or some existing row is still
   missing `duration_seconds`. A channel avatar is fetched (and cached, see
   below) once per channel, ever.
2. `apply_feed_data()` (DB-only, always sequential on the caller's own
   session) inserts new content rows, marks/re-marks `is_new_upload` (see
   above), backfills missing durations onto existing rows, and stores a
   newly fetched avatar path. One feed's failure here is caught and logged
   per-feed (`refresh_feeds`), not summed in one expression — so it can't
   abort every other feed's refresh in the same batch, whether that batch is
   one profile's on-demand click or the scheduler's every-profile sweep.
   `new_content_count` in the response only counts freshly-*inserted* rows,
   not re-marked-as-new-upload existing ones or content some other trigger
   (another tab, another device, the background scheduler) already added —
   which is why the client-side refresh button always reloads unconditionally
   rather than gating on that count (see the JS side note below).

**Bulk import** (Settings → Channels → Import) — `POST /feeds/import` with a
newline-separated blob (bare `@handle`s, full URLs, or rows copied straight
from a Google Takeout `subscriptions.csv` export). Two phases, tracked
against an in-memory job id (`_import_progress`, polled via
`GET /feeds/import/{job_id}/status`): every line's channel URL is *resolved*
in parallel first (`ThreadPoolExecutor`, same pool-size reasoning as
routine refresh), then feeds are *created* strictly sequentially on one
session (SQLite doesn't handle concurrent writers well, and this is also
where duplicate detection naturally lives — a feed created earlier in the
same batch is already visible to a later line's existence check). Each
line's initial parse + backfill runs inline as part of that sequential pass,
not deferred to a background task the way a single add is — bulk import is
already running off the request thread. Original line order is preserved in
the results regardless of resolution order.

**Channel avatars** — Not present in the RSS feed or any playlist
extraction, so `fetch_channel_avatar_url()` does a separate lightweight
`yt-dlp` fetch of the channel page (`playlist_items=0`, no video listing).
Hotlinking Google's CDN URL directly from the browser turned out to
intermittently fail Chrome's Opaque Response Blocking (ORB) even for a URL
that loaded fine moments earlier — so `images.download_avatar()` fetches
the bytes once server-side and saves them to
`data/avatars/{channel_id}.jpg`, re-served same-origin via
`GET /avatars/{filename}` (path-traversal guarded). Channel search results
get the same caching (`_cached_or_downloaded_avatar()` in `youtube/search.py`).

**Thumbnail caching** — The same same-origin problem exists for video
thumbnails (`i*.ytimg.com`), but unlike avatars this cache is filled lazily,
per render, not at fetch/refresh time. `page_context.queue_thumbnail_caching()`
is called by every context function that renders content cards (Home shelves,
Library's detail panel, …); for each item still pointing at a remote
`ytimg.com` URL it queues a FastAPI `BackgroundTask` running
`feed_sync.cache_thumbnail()`, deduped per request so the same video queued
by two shelves in one response isn't fetched twice. That background task
calls `images.download_thumbnail()` to save the bytes to
`data/thumbnails/{video_id}.jpg`, then rewrites `Content.thumbnail_url` (for
every row across every profile sharing that `video_id`) to the local
`/thumbnails/{filename}` path — served same-origin, path-traversal guarded,
same as avatars. Because caching only happens as a side effect of a render,
the response that triggered it still goes out with the original remote URL;
the local path only benefits the *next* render of that content, anywhere.
`storage.unlink_thumbnail_if_unshared()` removes a cached thumbnail when its
last referencing `Content` row is actually deleted (unfollow purge, Explore
removal) — but unlike downloaded audio, cached thumbnails are **not** counted
in `storage.collect_usage()`'s total, not swept for orphans by
`storage.clear_all()`, and have no entry in `.env.example` — `THUMBNAILS_DIR`
exists as a `Settings` field (default `/app/data/thumbnails`, code default
only, same as `AVATARS_DIR`) but nothing currently surfaces this directory's
size or offers to clear it, so it grows unboundedly for every video ever
rendered, downloaded or not.

**Backfill** — A one-time full-history scan (`_run_backfill()` in
`routers/feeds.py`, background task), separate from routine refreshes (RSS
only exposes recent entries). `fetch_channel_all_videos()` does a single
flat `yt-dlp` extraction of the channel's `UULF` playlist to get every
long-form video's id, title, thumbnail, and duration in one pass. The
uploads playlist doesn't expose per-video publish dates, so backfilled rows
get synthetic `published_at` values — one second apart, counting back from
the oldest date already known for the feed — preserving true newest-to-
oldest order without claiming a real date. Progress
(`scanning` → `saving` → `done`) is tracked in-memory per feed and polled
via `GET /feeds/{id}/backfill-status`, shown behind a full-screen overlay.

**Audio quality** — A per-profile setting (`GET`/`PUT /settings`), applied
at download time, not retroactively. Only two tiers now: `high` and `low`,
both downloading YouTube's native `mp4a`/AAC stream (remux only, no local
transcoding) — `low` additionally caps to YouTube's own ≤64kbps pre-encoded
stream where available. The earlier third tier, `medium` (locally
re-encoded to mp3, the only tier that ever paid a transcode cost), has been
dropped entirely: `routers/settings.py`'s `AUDIO_QUALITIES = ("high", "low")`
rejects it going forward, and the migration backfill downgrades any profile
still set to it to `low` (the closer match in intent — smaller files — of
the two remaining tiers). A pre-existing database's CHECK constraint still
technically allows `medium` (SQLite can't alter it in place), but nothing
ever sets it again.

**Feed refresh interval** — A deployment-wide setting (not per-profile —
see `AppSettings` in §3), same `GET`/`PUT /settings` endpoint. Presets only
(`FEED_REFRESH_INTERVALS = (15, 30, 60, 120)` minutes) rather than a
free-form number, ruling out a value aggressive enough to risk YouTube
rate-limiting the unauthenticated RSS/yt-dlp calls. Changing it calls
`scheduler.request_reschedule()` (see Page load above).

**Export** — `GET /storage/export` streams every `ready` item as one
uncompressed (`ZIP_STORED`) zip — audio is already compressed, so
re-zipping it would just burn CPU. Duplicate filenames (same sanitized
title) get a `(2)`, `(3)`, … suffix. A single item can also be exported via
`GET /content/{id}/stream?download=1`, which skips the `last_played_at`
update a real playback triggers.

**Save for later** — A bookmark toggle (`POST`/`DELETE /content/{id}/save`)
present on both card and row layouts, matched by `data-content-id`/
`data-saved` attributes so every instance of the same item across shelves,
Library tiles, and track lists stays in sync from one click, not just the
one it happened in.

**Confirm dialogs & toasts** — Native `window.confirm`/`window.alert` are
never used. `static/js/ui.js` loads before every page-specific script and
exposes `confirmDialog(message, label) -> Promise<boolean>` (backdrop-click
and Escape both cancel, focus lands on the *safe* button) and
`showToast(message)` (bottom-centre, auto-dismissing). Both lazily create
their own DOM.

**In-page player overlay** — Every surface plays through one persistent
overlay + mini-player bar (`_player_overlay.html`, `GET /content/{id}` JSON,
`home/overlay.js`'s `openPlayer()`): Home's shelves, Explore's results, and
the channel/playlist detail panel (`home/detail.js`) all call it, and there
is no separate standalone player page anymore — a track link's `href` is a
real `/#player/{id}` URL for ctrl-click/bookmark purposes, but a plain click
is always intercepted and opens the overlay instead. The overlay reuses
`_player_controls.html` (its only caller now), so `player.js`'s
`setupPlayer`/`prepareAudio`/`setupMediaSession`/`setupFavorite` work against
it unmodified. Since the whole app — Home/Library/Explore/Settings and the
detail panel — is one document, opening a channel or a track never tears
down the `<audio>` element; that's what fixed the mini-player silently
stopping whenever you left Home/Library/Explore before this (see the
Library section above and §11).

That leaves one real seam: the audio element still doesn't survive a genuine
full document reload (an actual F5, reopening the tab, or landing via one of
the `/channel/{id}`-style compatibility redirects), so something has to
carry playback position — and whether the overlay was expanded or just the
mini bar — across *that*. `resume.js` listens for both:
- `pageshow` with `event.persisted` — fires when the page is restored from
  the browser's back/forward cache (bfcache) without a real server round
  trip, which would otherwise silently show stale server-rendered data
  (Recently Played, storage usage, Library's grid, …) with no signal
  anything's wrong. The handler snapshots playback state, then forces one
  real `window.location.reload()` — simpler and more robust than trying to
  track every specific action that could have changed something while away.
- `pagehide` — fires on *any* departure, bfcache-eligible or not (closing
  the tab, a hard refresh, following one of the compatibility redirects).
  Without this, landing back on a *fresh* load of `index.html` had nothing
  in `sessionStorage` to resume, and the mini-player just silently didn't
  come back.

Both call the same `saveResumeState()`, writing `{contentId, currentTime,
wasPlaying, wasExpanded}` to `sessionStorage['spotea-resume']` — but only if
`audio.src` is actually set (still-downloading playback that never started
has `audio.paused === true` for a reason that has nothing to do with an
intentional pause; saving that as `wasPlaying: false` would wrongly suppress
autoplay once the download finishes). `wasExpanded` reflects whether
`#player-overlay` was visible when the record was saved.

Two different consumers read this record back, for two different reasons:
- `player.js`'s `consumeResumeState(contentId)` — called once, deep inside
  `prepareAudio`'s `startPlayback()`, right as `audio.src` is (re-)assigned
  after a reload — restores `currentTime`/`wasPlaying` so a forced reload
  (from the `pageshow` case above) doesn't restart the track from 0:00.
  Removed from `sessionStorage` on read, so a stale record can never apply
  to some later, unrelated track load.
- `home/overlay.js`'s `resumeOverlayIfNeeded()` (called once on boot,
  `pages/index.js`) — the overlay always starts closed/empty on a fresh
  load. Reads the same record (without consuming it itself) and calls
  `openPlayer(contentId, { expanded: wasExpanded !== false })` to reopen
  whatever was playing, correctly collapsed or expanded. Runs before
  `handleInitialRoute()` (`home/detail.js`), which takes over from there if
  the URL itself names a different track (`#player/{id}`) or a detail view —
  an explicit URL wins over an implicit resume. If the resume fetch fails
  (e.g. the content id no longer resolves for the now-active profile — see
  the profile-switch note below), the stale record is explicitly cleared
  right there; otherwise a permanently-invalid record would re-trigger the
  same "Could not load this track" failure on every subsequent reload
  forever, since nothing else would ever consume it.

**Profile switch and this same mechanism** — `switchProfile()`
(`profiles.js`) calls `closePlayer()` (clearing `#player-root`'s dataset and
the audio element) *before* reloading, specifically so `pagehide`'s
`saveResumeState()` can't snapshot a track that's about to belong to a
profile it no longer exists under — without that, the reload's fresh
session (now the new profile) would try to resume a content id scoped to
the *old* profile, 404, and hit the same "repeats forever" failure mode the
paragraph above guards against from the other direction.

**Download** — There is no download button. Every card just says **Play**;
opening the player is what fetches the audio. Cards already on disk get a
small check badge — live-patched onto every matching card/row on the page
the moment a download completes (`markContentDownloaded`, since the overlay
plays without a page reload, so a badge only ever painted at server-render
time would stay stale for the rest of the session otherwise), not just
server-rendered once.

`POST /content/{id}/download` → 409 if already `downloading` →
`status='downloading'` set synchronously → yt-dlp job dispatched to the
background → response returns immediately. On completion: `status='ready'`
+ `file_path`; on failure: `status='error'` + `error_message`.

> **Never start a download on plain page load.** Browsers speculatively load
> links — a prerender executes the target page's JavaScript — so simply having
> `<a href="/#player/{id}">` on a card is enough for the browser to open the
> player behind your back and, if the player downloads unconditionally, fill
> the user's disk with things they never clicked. `player.js` therefore gates
> `prepareAudio()` behind `whenVisible()`, which waits out
> `document.prerendering` and a non-`visible` `visibilityState`.

**Polling** — While the player is preparing, it polls
`GET /content/{id}/status` every 1.5s until `ready` or `error`. The poll
timer and a `visibilitychange` re-check-in listener (mobile browsers
throttle/suspend timers for a backgrounded tab, so the interval alone might
not have ticked in a while by the time you switch back) are both tracked in
module-level variables and explicitly torn down — by a new `prepareAudio()`
call switching tracks, *and* by the poll's own terminal states (ready,
error, or giving up after repeated failures) — so a finished download can
never hijack whatever's playing now, and a stale `visibilitychange` handler
can never fire `startPlayback()` a second time on a track that's already
playing fine (which would reset it to 0:00).

**Downloaded audio** — Settings' "Manage downloads" modal lists everything
on disk with per-item sizes (read from disk at render time, so a file
deleted behind the app's back reports 0), a total, per-item removal, per-
item export, and "Clear all"/"Export all". Two things to keep in mind:
- Content rows cascade-delete with their feed, but **files do not** —
  cleanup functions run before the DB rows they depend on are gone.
- `clear_all()` also sweeps any orphaned audio files in the storage
  directory that no row points at.
- Files can be **shared across profiles** (`unlink_if_unshared` — keyed by
  `file_path`/`video_id`, not scoped to one profile): two profiles that both
  follow an overlapping channel at the same audio quality can point at the
  same physical file, so deleting one profile's copy only actually unlinks
  it from disk once no other `ready` row anywhere still references it.

**Play** — The overlay renders a custom player (`_player_controls.html`)
(no native `<audio controls>`, which renders as a light pill clashing with
the dark theme): large artwork, title/channel, a seek bar, ±15s skip
buttons, previous/next track, a large round play/pause, volume, a shuffle
toggle and the favorite toggle. Keyboard: Space toggles playback, ←/→ skip. Media Session API integration
(`setupMediaSession`) drives lock-screen/notification-shade transport
controls and Bluetooth/headset buttons; `openPlayer()` re-sets its metadata
explicitly on every track switch since `setupMediaSession` itself only reads
the DOM once, at page-load time.

The stream endpoint returns 409 if `status != ready`; the file path always
comes from the DB, never the request. Every real stream request stamps
`last_played_at = now()` (skipped for `?download=1`) — this is what feeds
"Recently played".

**Play all, shuffle, and the queue** — A channel or pinned playlist's detail
panel carries a round **Play** button and a **Shuffle** toggle above its
track list. Play fills the queue from the *whole* channel/playlist and
starts on whatever that order puts first; clicking any individual track row
does the same thing starting from that row, because "play this and keep
going" is what clicking a track in a list means. Either way the queue comes
from `GET /content/queue/{channel|playlist}/…`, which returns ids only, in
exactly the order the track list renders them, unpaginated (so "Play all" on
a 832-video channel means all 832, not the 20 rows on screen) and capped at
`content_query.QUEUE_MAX_ITEMS`. Both selections come from the same
`_content_query()` the detail panel's own page does — a second spelling of
those filters is how "Play all" would quietly play a different set than the
list it was launched from.

`static/js/home/queue.js` holds the state and nothing else: it never touches
the player, and everything that reacts to it (`home/overlay.js`'s transport,
`home/detail.js`'s shuffle button) listens for a `spotea:queuechange` event
rather than being called directly, which is what keeps the dependency
one-way. It keeps *two* orderings — the source's own order and the order
playback follows — because collapsing them into one shuffled array would
make turning shuffle back off mid-queue impossible. Shuffle is a standing
preference, not an action: toggling it reorders a live queue in place around
whatever is currently playing (so nothing restarts) and otherwise just
decides the order the next Play builds, which is why the same toggle appears
in the player footer and above the track list and both drive one value. The
whole record is mirrored into `sessionStorage`, since `resume.js` forces a
full reload on every bfcache restore and an in-memory queue would evaporate
mid-listen.

A finished track advances automatically (`audio`'s `ended`), keeping the
overlay exactly as expanded or collapsed as the user left it. Previous/next
appear in the transport either side of play/pause, disabled when there is
nowhere to go, and mirror onto the lock screen through Media Session's
`nexttrack`/`previoustrack`; the mini bar gets a skip button too, hidden
rather than disabled when no queue is loaded, since a permanently dead
control costs real width on a phone. Notably, previous/next stay live while
`.transport` is in its `is-disabled` (downloading/failed) state — a track
stuck on "Download failed" is exactly when someone wants to skip past it.

Because a download is only triggered by playing something, every track
change in a queue would otherwise cost the same 2-4s wait as the first (see
§8's "Where a play's time actually goes"). The overlay fetches one track
ahead to cover it — but only once the current track has genuinely been
listened to for `PREFETCH_AFTER_SECONDS`, so skipping quickly through a
queue doesn't pull down a file per track passed over. `POST
/content/{id}/download` is a no-op for anything already on disk, which is
what lets the prefetch fire without first asking about the next track's
status.

**Delete** — Removing a download deletes the file and resets the row to
`status='not_downloaded'` + `file_path=NULL`. The row is kept, so the item
stays in the Library and comes back just by playing it again.

**Unfollowing a channel** — `DELETE /feeds/{id}` doesn't blindly delete
everything: content that was actually downloaded, played, favorited, or
saved is *kept*, and the feed row itself is downgraded to `followed=False`
(same state a placeholder feed starts in) rather than deleted outright — it
drops out of Library/New Uploads/background refresh but keeps working
everywhere else (Storage, Recently Played, Favorites/Saved, direct
playback). Only content nobody ever touched is purged. Re-following the
same channel later picks the same row back up (see Add feed above) instead
of duplicating it.

**Profiles (switch/manage)** — Two separate, deliberately non-overlapping
overlays (`profiles.js`): "Switch profile" (topbar/mobile menu) is just a
click-to-switch list — no edit/delete/add there, that's not what someone
reaching for the header button mid-browse wants. "Manage profiles"
(Settings) is the reverse — rename/delete/add, no switching. Renaming reuses
the same "New profile name…" input as adding (its submit button relabels to
"Save" while `editingProfileId` is set) rather than a separate inline
editor. Deleting a profile removes its feeds/content (DB cascade) and its
downloaded files (`delete_files_for_profile`, since files don't cascade with
rows) — refused outright if it's the account's last remaining profile.

**PWA** — `static/manifest.json` + `static/js/sw.js` make the app
installable (Chrome/Android requires an active service worker with a fetch
handler before offering "Install app"), registered from every page (not
just `index.html`) so installing works regardless of which page happens to
be open when the browser offers the prompt. The service worker is
network-first, not offline-first: it exists purely for installability, and
only falls back to its cache when the network genuinely fails. It
deliberately never caches anything under `/content/`, `/feeds/`,
`/profiles/`, `/settings/`, or `/storage/` — the `<audio>` element issues
Range requests while seeking, and the Cache API keys purely on URL with no
concept of Range, so a cached response for one byte range would get
replayed for a request asking for a different one; a dynamic GET here can
also legitimately 404/409, and nothing in the fetch handler checks
`response.ok` before caching. A transient network hiccup hitting the
`catch()` fallback would otherwise be indistinguishable, to the `<audio>`
tag, from a real playback error. The cache name is versioned (`spotea-v2`)
specifically so an older client holding a cache that *did* cache API traffic
gets it purged on activate, not just left alone because nothing else
changed.

## 6. Concurrency & security

- yt-dlp is called via its **Python API** (`yt_dlp.YoutubeDL`), not a raw
  subprocess.
- The download URL is always constructed as
  `https://www.youtube.com/watch?v={video_id}` by the server; `video_id` is
  validated against `^[a-zA-Z0-9_-]{11}$` — user input never reaches a shell
  command directly.
- No concurrent-download limit (personal/small-scale use).
- **Passwords** are hashed with bcrypt (`app/auth.py`), never stored or
  compared in plaintext; the shared `secrets.compare_digest`-based single-
  password gate from before accounts existed is gone entirely.
- **Session cookies** are signed and HttpOnly (Starlette `SessionMiddleware`,
  `SECRET_KEY`).
- **Account isolation is the security-critical addition this session.**
  Before accounts existed, `get_current_profile`'s self-heal fell back to
  "the first profile, period" (`db.query(User).order_by(User.id).first()`)
  across *every* profile in the database — harmless when there was only ever
  one shared login, but a live cross-tenant leak once real, separate
  accounts share a deployment. Now:
  - `get_current_account` (`deps.py`) resolves the account from the session
    and **never self-heals to a different account** — a missing, stale, or
    forged account id is simply not authenticated. (Contrast with
    `get_current_profile` below, which *does* self-heal, but only ever
    within the caller's own account.)
  - `get_current_profile` scopes both its direct session lookup and its
    self-heal fallback by `account_id` — a stale/forged `profile_id` in the
    session can never resolve to another account's profile, even
    transiently.
  - Every handler in `routers/profiles.py` (list/create/update/delete/
    switch) explicitly checks `profile.account_id == current account's id`
    after loading a profile by id, returning a plain 404 (never 403) on
    mismatch so a probing request can't even learn whether that id exists.
  - `delete_profile`'s "can't delete the last profile" guard counts profiles
    scoped to the caller's `account_id` — an earlier, single-tenant-era
    version of this check counted *every* profile in the database, which
    would have let one account's profile count block (or fail to block)
    another account's deletion.
  - Registration is open (no invite code, no admin approval) — anyone who
    can reach the instance can create an account. This is a deliberate
    choice for a self-hosted deployment the operator controls network
    access to (see README's "Exposing it beyond your local network"), not
    an oversight.
- Known, currently-accepted gaps (see Roadmap): no password reset flow (no
  outbound email capability exists anywhere in the app), no email
  verification, no rate limiting on `/login` or `/register`.

## 7. Config & dependencies

Environment variables (`.env`, documented in `.env.example`):
- `SECRET_KEY` — session signing key (required)
- `APP_PASSWORD` — **optional**. Only consulted by `migrations.py`'s one-time
  legacy-account backfill (§3) for a pre-existing single-tenant deployment;
  a fresh install never needs it, since real accounts register themselves.
- `DATABASE_URL` — default `sqlite:////app/data/spotea.db`
- `STORAGE_DIR` — default `/app/data/storage`
- `AVATARS_DIR` — default `/app/data/avatars` (not currently exposed in
  `.env.example`, code default only)
- `THUMBNAILS_DIR` — default `/app/data/thumbnails` (same as `AVATARS_DIR`:
  not currently exposed in `.env.example`, code default only — see
  Thumbnail caching in §5)
- `AUDIO_FORMAT` — default `m4a` (the `high`/`low` tiers' extraction target;
  matches YouTube's native audio stream for almost every video, so
  extraction is a fast remux rather than a re-encode — see Audio quality in
  §5)
- `HOST_PORT` — default `8000`; docker-compose–only, not read by the app
  itself

`requirements.txt`: `fastapi`, `uvicorn[standard]`, `sqlalchemy`,
`feedparser`, `yt-dlp`, `jinja2`, `python-multipart`, `itsdangerous`,
`pydantic-settings`, `bcrypt`. `bcrypt` ships prebuilt manylinux wheels for
the `python:3.12-slim` base image the `Dockerfile` uses — no build tooling
needed to add it.

Everything except `yt-dlp` (and its `bgutil-ytdlp-pot-provider` plugin) is
pinned to an exact version. Unpinned, every `docker compose up --build`
silently picked up whatever was newest on PyPI, which is how a framework
behaviour change lands in production with no change to this repo — FastAPI
0.106 closing a yield-dependency's session *before* background tasks run
being the concrete example this codebase was exposed to. `yt-dlp` stays
floating on purpose: YouTube changes extraction constantly and a pin there
means downloads break until someone notices.

Lint/format config lives in `pyproject.toml` (`ruff`, dev-only — see
`requirements-dev.txt`). There's no `[project]` table: the app is run from
the repo, not installed as a package.

## 8. Error scenarios

| Case | Behavior |
|---|---|
| No/invalid session | Redirected to `/login` (`NotAuthenticated` exception handler in `main.py`) |
| Wrong email/password at login | 401, generic "Invalid email or password" |
| Invalid/duplicate email at registration, mismatched or too-short/long password | 400 + specific message |
| Profile id belonging to another account | 404 (never 403 — doesn't confirm the id exists) |
| Deleting an account's last remaining profile | 409 |
| Invalid RSS URL | 400 + message |
| yt-dlp download failure | `status=error`, `error_message` stored |
| Duplicate download request | 409 |
| Stream request while not `ready` | 409 |

### YouTube refusing a download (HTTP 403)

The common failure isn't extraction — it's YouTube resolving a media URL and
then refusing to serve it (`unable to download video data: HTTP Error 403`,
raised from `YoutubeDL.process_info`, i.e. the media transfer, not the player
API). **Which client resolved that URL is what decides whether it gets
served**, so the ladder in `app/downloader.py` is a sequence of client
changes, not of waits.

Measured per client against the live instance — one extraction each, plus a
2-byte range GET on the URL it produced:

| client | extract | usable mp4a URL | range GET |
|---|---|---|---|
| `android_vr` | ~1.4s | yes, **no** PO token | 206, but 403s at random |
| `tv_simply` | ~2.9s | yes, **PO-token-bound** | 206 |
| `web_safari` | ~1.5s | **none** — SABR-forced, formats have no URL | — |
| `mweb` | ~3.7s | yes, PO-token-bound | **403** |
| `web_embedded` | ~3.3s | yes, no PO token | **403** |
| `ios` | ~1.3s | **none** — needs a GVS token it can't get | — |

So the ladder is `android_vr` → `tv_simply` → `tv_simply`, with **no sleeps**:
fast client first because it works most of the time and costs ~1.4s to rule
out, then the one whose URL carries a PO token, which is the mechanism
YouTube actually checks. `tv_simply` needs a JS runtime for `nsig`, but the
image already ships Deno, so it needs no `remote_components` fetch. Format
selection lands on plain `140` either way — same bitrate, no audio change.

Two things that had been assumed here were false, and cost a lot of user-
visible waiting before being measured:

- **`web_safari` was never a fallback.** It was carried as `android_vr`'s
  safety net; YouTube forces SABR on it (yt-dlp#12482) so every https format
  comes back without a URL and yt-dlp drops all of them — silently, under
  `no_warnings`. Every download this app ever served came from `android_vr`
  alone, which is precisely why an `android_vr` 403 had nothing to fall back
  to.
- **`mweb` was the "different client family" last rung**, costing 3.7s and a
  JS solver fetch to produce a URL that 403s.

Waiting is not part of the fix. The refusals are per-URL: a fresh extraction
produces a fresh URL, and sitting still first doesn't make that URL more
acceptable. The transfer itself is never the bottleneck either — 5.4MB
arrived in under a second, and typical audio here averages ~44kbps (an hour
is ~19MB).

Things tested and rejected, so nobody repeats them: forcing IPv4; Chrome TLS
impersonation via `curl_cffi` (so it isn't a dependency); `fetch_pot=always`
(does **not** bind a PO token to `android_vr` — it has no GVS token support
at all); `sleep_interval_requests` (cost ~1.5s on every play including the
successful ones, no measured effect on refusals). The `tv` client is not
usable unauthenticated — every format comes back DRM protected without
cookies.

Cookies would unlock more (`tv` formats, age-restricted content) but carry a
real risk of the Google account being restricted, so the app stays
unauthenticated by default.

#### Retrying belongs to the server, not the client

For one day there was also a client-side "stall watchdog": `player.js` armed
a 3s timer and, if no byte progress had appeared, POSTed
`/content/{id}/download/restart` to dispatch a fresh attempt, up to three
rounds, showing "(attempt 2 of 3)" in the status text. It has been removed
entirely, along with the restart endpoint and the generation-number
bookkeeping that made a superseded attempt's result discardable. It was
making playback slower, not faster:

- **3s was shorter than a healthy attempt.** Resolving a URL takes 1.4-3s and
  moves no bytes, so the watchdog fired on downloads that were working fine.
- **It couldn't cancel what it abandoned.** yt-dlp can't be interrupted from
  Python, so a restart left the old attempt running and started a second one
  beside it — two, then three, concurrent yt-dlp runs per play, all writing
  the same `.part` path. One real play in the logs died on
  `Unable to rename file: ... .m4a.part -> .m4a`.
- **It threw away successes.** The abandoned attempts usually finished fine,
  but a superseded generation's result was discarded on arrival, so the user
  waited for a later attempt to redo work already done. That is exactly why
  "the third attempt" looked like the one that worked: it was simply the
  first attempt allowed to keep its result.

The server never had to guess in the first place — it catches the failure
itself, in ~1.4s, and moves to the next client immediately. The client just
polls. To keep one hung request from holding the whole ladder open,
`socket_timeout` is set (`SOCKET_TIMEOUT_SECONDS`), which is the real version
of what the watchdog was trying to do.

`download_audio` also reports an `extracting` phase at the start of every
attempt. That 1.4-3s is the slowest part of a play and produces no byte
progress of its own, so without it the UI would sit on "Preparing audio…" for
the entire wait.

### Where a play's time actually goes

Measured end to end on a fresh download (2.15MiB, `android_vr` first try):

| | |
|---|---|
| resolving a URL YouTube will honour | **1.42s** |
| transferring the audio | 0.33s |
| FFmpeg remux to `.m4a` | 0.04s |
| **total** | **1.79s** |

Broken down further, that 1.42s is two YouTube round trips plus yt-dlp
overhead:

| | |
|---|---|
| `YoutubeDL()` construction (1744 extractors, warm process) | 0.06s |
| `GET /watch?v=…` — the watch page | **0.71-0.93s** |
| `POST /youtubei/v1/player` | 0.14s |
| format sorting and selection | 0.10s |

**The watch page is the whole cost, and its size is not why.** It is ~1.24MB
of HTML, but served gzipped that's 306KB in 0.93s, brotli 140KB in 0.81s, and
*uncompressed* 1275KB in 0.83s — over four times the bytes for less time.
What's being waited on is YouTube generating the page, not sending it. So
`brotli` is not worth adding as a dependency: it halves the wire bytes and
buys ~0.1s, inside run-to-run noise.

Things that don't help, measured:

- **`webpage_client`** only accepts `web` and `web_safari` — both the same
  full watch page. There is no lighter page to ask for.
- **`player_skip=webpage`** alone fails outright: the player response comes
  back as "Sign in to confirm you're not a bot".
- **`player_skip=webpage` + a harvested `visitor_data`** does work, and gets
  `android_vr` from 1.32s to 0.93s — but only because yt-dlp swaps the watch
  page for a `/youtubei/v1/next` call that costs 0.55-0.68s of its own. ~0.3s
  for a request pattern (player calls from one long-lived visitor ID that
  never loads a watch page) that is a textbook bot signature. Not taken: the
  403s this app spent a day fixing are the exact thing it would risk.
- **Reusing one `YoutubeDL` instance** across downloads would save the 56ms
  construction, at the cost of sharing extractor state between plays.

The one real win was on this side of the wire — see the poll schedule in
`player.js`. The client used to ramp 250ms → 2500ms, leaving a 1s gap between
2s and 3s and a 1.5s gap between 3s and 4.5s, i.e. its widest gaps exactly
where downloads land. Average dead air after the file was already on disk:
**~700ms**, with a worst case of 2s. A flat 200ms through the first four
seconds brings that to ~50ms for 20 local SQLite reads.

> Note on diagnosing this: `logger.info` in this app used to go nowhere.
> uvicorn configures only its own loggers and leaves the root at WARNING, so
> the ladder's "Download attempt failed" line never reached a handler —
> which is why the retries stayed invisible for a day. `app/main.py` now
> calls `logging.basicConfig(level=INFO)`; uvicorn's own loggers don't
> propagate, so the access log isn't duplicated.

## 9. Deployment (Docker)

- `Dockerfile`: `python:3.12-slim` base, installs a static `ffmpeg` binary
  (avoids Debian's package dragging in ~450MB of unrelated GPU/TTS/SMT
  libraries), installs `requirements.txt`, copies `app/`, runs
  `uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- `docker-compose.yml`: single `app` service, maps `${HOST_PORT:-8000}:8000`,
  mounts `./data:/app/data` (persists the SQLite DB and downloaded audio
  across restarts/upgrades), reads env vars from `.env`.
- **Static assets are served with `Cache-Control: no-cache`**
  (`RevalidatingStaticFiles` in `main.py`). Starlette's `StaticFiles` sends
  ETag/Last-Modified but no `Cache-Control`, and browsers then fall back to
  heuristic freshness — serving cached CSS/JS for a while without asking the
  server at all, which after an upgrade means a user could keep running new
  templates against stale CSS/JS. `no-cache` still permits caching, it just
  forces revalidation; unchanged files come back as a cheap 304.
- **Upgrading a pre-accounts deployment**: keep `APP_PASSWORD` set in `.env`
  for the one upgrade so the legacy-account migration (§3) has a password to
  hash — see README's "Updating" section. It can be unset afterward.
- `.env.example` ships in the repo so users copy it to `.env` and fill in
  `SECRET_KEY` (and, only if upgrading, `APP_PASSWORD`) before first run.

## 10. Roadmap

Real multi-account support shipped this session (see §6) — accounts own one
or more profiles, fully isolated from each other, with a migration path for
existing single-tenant deployments. What's still deliberately deferred:

- **Per-account feed refresh interval.** `AppSettings` is a single
  deployment-wide row (§3) — every account shares one background-refresh
  cadence. Making it per-account is a bigger change than a schema tweak (the
  scheduler would need per-account last-refreshed tracking, not just a
  config value to read), and wasn't part of what this pass of work was
  actually asked to solve.
- **No password reset / no email verification.** The app has no outbound
  email capability at all — `register`/`login` treat email purely as a
  unique login identifier, never verified or used to deliver anything. A
  locked-out user's only recovery today is direct database access.
- **No rate limiting** on `/login` or `/register` — acceptable for a small
  self-hosted deployment behind the operator's own network/reverse-proxy
  controls, not something to expose broadly without adding it.
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
    (later superseded — see 33)
24. Per-user audio quality setting, originally three tiers:
    `high`/`medium`/`low` (later reduced to two — see 35)
25. One-time full channel history backfill on adding a feed (RSS alone only
    exposes recent entries), with a progress overlay; ArcticTerminal branding
26. **Home** tab added: curated shelves (followed channels, new uploads,
    recently played, favorites, saved); `last_played_at` tracking added to
    drive "Recently played" (set on stream, not on download)
27. Channel avatars: fetched once per channel and served from our own
    origin instead of hotlinked (Chrome's Opaque Response Blocking made
    direct hotlinking unreliable); **Manage**/**Settings** tabs replacing
    the old Channels/Storage tabs (Storage folded into a "Manage downloads"
    modal off Settings, later itself renamed/restructured — see 34); feed
    refresh parallelized across a thread pool; zip export of all downloaded
    audio, and per-item export
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
33. Explore video search added alongside channel search (search for a
    specific song, add+play it without following the channel — `is_preview`
    content); background feed refresh scheduler replacing the old "once per
    session" client-triggered refresh entirely; PWA support (manifest,
    service worker, installability)
34. "Manage" tab merged into a restructured **Explore** (one unified search
    box, sectioned Songs/Channels results); bulk import moved from an
    always-open Manage textarea into Settings; Explore/Settings full-width
    layout
35. Multiple profiles per (at-the-time single, shared-password) login,
    Netflix-style switcher; Library rebuilt from a video grid into a grid of
    channel cards, with per-video browsing (search/filter/pagination) moved
    to dedicated per-channel and per-virtual-playlist pages
    (`channel.html`/`content_list.html`); audio-quality tiers reduced from
    three to two (`medium` dropped — no YouTube-native tier matched it, so
    it was the only one paying a local re-encode cost)
36. In-page player overlay + mini-player bar for Home/Library/Explore
    (`_player_overlay.html`), replacing full-page navigation to
    `/player/{id}` for those three tabs specifically — reusing player.js's
    existing controls unmodified; `channel.html`/`content_list.html`/
    standalone `player.html` deliberately kept as real navigations
37. New Uploads and Recently Played promoted from Home-only shelves to full
    pinned virtual-playlist pages in Library (`Content.is_new_upload`,
    14-day window, self-healing re-mark on refresh); Home shelves live-patch
    without a reload now that playback no longer navigates away
38. A round of playback/navigation correctness fixes surfaced by the
    overlay + bfcache-reload interaction: stale resume state applying to a
    track that hadn't started playing yet, a zombie `visibilitychange`
    listener re-triggering playback on an already-finished download, the
    manual refresh button silently no-op'ing when content had actually
    changed, the "downloaded" badge not live-patching, the mini-player
    force-expanding (or disappearing entirely) when returning from a
    channel/playlist page instead of staying collapsed — see §5's
    "In-page player overlay & playback-state persistence" for how this
    settled
39. Real multi-tenant accounts (`Account`: email/password, bcrypt-hashed)
    layered on top of the existing profile system — each account owns one
    or more profiles (household model, unchanged); open registration; a
    one-time migration folds a pre-existing single-tenant deployment's
    profiles into one legacy account seeded from `APP_PASSWORD`; several
    latent cross-account data-isolation gaps fixed as part of this (see §6);
    `Account.last_active_profile_id` added so re-login returns to whichever
    profile was active before logout instead of always the first one
40. `channel.html`/`content_list.html`/standalone `player.html` (kept as
    real navigations by milestone 36, above) folded into `index.html` as a
    fifth panel, "detail" (`home/detail.js`, `GET /partials/detail/channel/
    {id}` and `/partials/detail/playlist/{kind}`), reversing that earlier
    scope decision — see §5's Tabs/Library sections and "In-page player
    overlay". Fixed the mini-player tearing down whenever leaving Home/
    Library/Explore for a channel or pinned playlist, since there's no
    longer a document boundary for it to fall off of; the old real routes
    became one-line redirects to their hash equivalent
41. A real play queue (`home/queue.js`): "Play all" and a shuffle toggle on
    every channel/playlist detail panel, previous/next in the transport and
    on the mini bar and lock screen, and auto-advance when a track ends.
    Playback had until now been strictly one track at a time — the app could
    play a channel's videos but not *the channel*. Two supporting changes
    fell out of it: `content_query`'s filter/order logic split into a shared
    `_content_query()` so the queue and the visible track list can't
    disagree about what a playlist contains, and `POST /content/{id}/
    download` made a no-op for a track already on disk, which is what lets
    the queue fetch one track ahead without first asking about its status —
    see §5's "Play all, shuffle, and the queue"

### Verifying UI work

Reasoning about CSS/JS without actually rendering it has produced
shipped-broken UI before, so real-browser verification is standard practice
here, not optional. This machine has a system-installed Chromium-based
browser (Helium) usable as a Playwright driver
(`chromium.launch(executable_path="/usr/bin/helium-browser")`) without a
separate ~300MB browser download — check for it before falling back to
`playwright install`. Log in, navigate, click through to a state (modal
open, filter applied, a track playing), and screenshot; check both desktop
and phone viewports. Assert on real post-interaction state (`dataset`
values, `sessionStorage` contents, response status codes after a reload or
a simulated back/forward-cache restore) rather than assuming a handler
worked — a passing screenshot alone doesn't prove the underlying state is
correct.
