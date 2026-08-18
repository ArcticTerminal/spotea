from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Only consulted by migrations.py's one-time legacy-account backfill for
    # a pre-existing single-tenant deployment — fresh installs register real
    # accounts via /register and never need this set at all.
    app_password: str | None = None
    secret_key: str
    database_url: str = "sqlite:////app/data/spotea.db"
    storage_dir: Path = Path("/app/data/storage")
    avatars_dir: Path = Path("/app/data/avatars")
    thumbnails_dir: Path = Path("/app/data/thumbnails")
    audio_format: str = "m4a"
    # Marks the session cookie Secure, so it is only ever sent over HTTPS.
    # Off by default: plenty of installs are reached over plain HTTP on a LAN,
    # and turning it on there would make login silently impossible. Turn it on
    # once the app sits behind a TLS proxy (see README).
    session_https_only: bool = False
    # MusicBrainz rejects requests without a descriptive User-Agent (see
    # services/genre_artists.py) — a generic default rather than a required
    # setting, since nobody self-hosting this needs to think about it unless
    # they want their own contact address on record with MusicBrainz.
    musicbrainz_user_agent: str = "Spotea/1.0 ( self-hosted; no contact set )"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
