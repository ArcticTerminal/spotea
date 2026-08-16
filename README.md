# Spotea

A self-hosted app for following YouTube channels via RSS, downloading
selected videos as audio, and listening to them from your browser — like a
personal, free alternative to following musicians/creators on Spotify.

- Register your own account and log in with it — every account is fully
  isolated, and can hold multiple profiles (household-style, like a
  streaming service) that each get their own followed channels, library, and
  downloads
- Follow a channel by pasting any YouTube URL, or search for it by name from
  the **Explore** tab — no need to know how to construct an RSS feed link
  yourself; bulk-import a whole list of channels at once from Settings
- Feeds refresh automatically in the background on an interval you choose
  (Settings), and new uploads show up on Home and in Library's **New
  Uploads** shelf; YouTube Shorts are filtered out since they don't fit a
  "listen to it" library
- **Explore** also searches for individual songs directly — add and play one
  without following the whole channel
- Tell it what you're into — list genres, artists or moods under **Interests**
  in Settings, and Explore's **For you** shelves fill up with channels,
  contents and ready-made playlists searched from them. They refresh on the
  same schedule as your feeds, with no separate control to keep track of
- Open a recommended playlist, or any channel you don't follow yet, and browse
  its tracks on the same page a followed channel gets — Play all and shuffle
  included. Nothing is stored until you press play, and following is one
  button away if you like what you hear
- Library search by title/channel, filter by a channel or by the pinned
  **Favorites** / **Saved for later** / **New Uploads** / **Recently
  Played** virtual playlists, with pagination
- Just hit **Play** — the audio is fetched in the background
  (via [yt-dlp](https://github.com/yt-dlp/yt-dlp)); on Home/Library/Explore
  it opens in a persistent in-page player that keeps playing while you keep
  browsing, with a mini-player bar when collapsed. Anything already
  downloaded is marked so you know it starts instantly
- A proper player either way — seek bar, ±15s skip, volume, keyboard
  shortcuts, media-session/lock-screen controls — and favorite what you
  liked while it's playing
- The **Storage** tab shows exactly what's using disk, and clears it per
  item or all at once; anything you clear comes back next time you play it
- Installable as a PWA (works like a native app on your phone's home screen)

See [ARCHITECTURE.md](ARCHITECTURE.md) for how it's built.

## Running with Docker (recommended)

1. Copy the example env file and fill in real values:

   ```bash
   cp .env.example .env
   ```

   Generate a `SECRET_KEY`:

   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. Start the app:

   ```bash
   docker compose up -d
   ```

3. Open `http://localhost:8000` (or whatever `HOST_PORT` you set in `.env`)
   and register an account — registration is open to anyone who can reach
   the instance, so keep that in mind if you expose it publicly (see below).

Downloaded audio and the SQLite database persist in `./data` on the host,
so they survive container restarts and rebuilds.

### Updating

```bash
git pull
docker compose up -d --build
```

Your `./data` volume is untouched by rebuilds.

If you're upgrading an older deployment that predates accounts (i.e. it
only ever had `APP_PASSWORD`-based login), keep `APP_PASSWORD` set in `.env`
for this one upgrade — a one-time migration uses it to create a legacy
account (`owner@local`, that same password) so your existing profiles and
data keep working without being re-entered. You can register a real account
and stop needing it afterwards.

### Exposing it beyond your local network

Login is now real per-account authentication (hashed passwords, isolated
data per account), not a single shared password — but there's still no
email verification or rate limiting on login/registration. If you expose
this instance to the internet, put it behind a reverse proxy with HTTPS
(e.g. Caddy, nginx, Traefik), and consider whether you want registration
open to anyone who finds the URL.

## Running locally without Docker

Requires Python 3.12+ and `ffmpeg` installed on your system.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — for local dev, relative paths work well, e.g.:
#   DATABASE_URL=sqlite:///./data/spotea.db
#   STORAGE_DIR=./data/storage

uvicorn app.main:app --reload
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | yes | — | Random key used to sign session cookies |
| `APP_PASSWORD` | no | — | Only used by the one-time migration for a pre-accounts deployment (see "Updating" above); fresh installs don't need it |
| `DATABASE_URL` | no | `sqlite:////app/data/spotea.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | no | `/app/data/storage` | Where downloaded audio files are stored |
| `AVATARS_DIR` | no | `/app/data/avatars` | Where fetched channel avatars are stored |
| `AUDIO_FORMAT` | no | `m4a` | Audio format yt-dlp extracts to |
| `HOST_PORT` | no | `8000` | Host port docker-compose publishes the app on (Docker only) |

## License

MIT — see [LICENSE](LICENSE).
