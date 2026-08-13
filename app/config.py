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
    audio_format: str = "m4a"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
