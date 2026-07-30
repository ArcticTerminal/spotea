# spotifrei

A self-hosted app for following YouTube channels via RSS, downloading
selected videos as audio, and listening to them from your browser — like a
personal, free alternative to following musicians/creators on Spotify.

- Add the RSS feed of any YouTube channel you follow
- On every visit, feeds are re-parsed and new uploads show up automatically
- Pick what you want to keep, download it as audio (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- Play it back from a simple in-browser player
- Delete it later to free up space, or re-download anytime

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

   Set `APP_PASSWORD` to whatever password you want to log in with.

2. Start the app:

   ```bash
   docker compose up -d
   ```

3. Open `http://localhost:8000` and log in with `APP_PASSWORD`.

Downloaded audio and the SQLite database persist in `./data` on the host,
so they survive container restarts and rebuilds.

### Updating

```bash
git pull
docker compose up -d --build
```

Your `./data` volume is untouched by rebuilds.

### Exposing it beyond your local network

The built-in login is a single shared password, not a full auth system.
If you expose this instance to the internet, put it behind a reverse proxy
with HTTPS (e.g. Caddy, nginx, Traefik).

## Running locally without Docker

Requires Python 3.12+ and `ffmpeg` installed on your system.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env — for local dev, relative paths work well, e.g.:
#   DATABASE_URL=sqlite:///./data/spotifrei.db
#   STORAGE_DIR=./data/storage

uvicorn app.main:app --reload
```

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_PASSWORD` | yes | — | Shared login password for the instance |
| `SECRET_KEY` | yes | — | Random key used to sign session cookies |
| `DATABASE_URL` | no | `sqlite:////app/data/spotifrei.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | no | `/app/data/storage` | Where downloaded audio files are stored |
| `AUDIO_FORMAT` | no | `mp3` | Audio format yt-dlp extracts to |

## License

MIT — see [LICENSE](LICENSE).
