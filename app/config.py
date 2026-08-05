from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_password: str
    secret_key: str
    database_url: str = "sqlite:////app/data/spotifrei.db"
    storage_dir: Path = Path("/app/data/storage")
    avatars_dir: Path = Path("/app/data/avatars")
    audio_format: str = "m4a"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")


settings = Settings()
