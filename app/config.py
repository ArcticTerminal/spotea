from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
    # Whose YouTube Music charts Explore shows (see
    # services/recommendations.py). One or more country codes, comma
    # separated: "TR", or "TR,US,GB,DE" to blend several a rank at a time
    # (see music.fetch_charts_for).
    #
    # "ZZ" is YouTube Music's own global chart and stays the default,
    # because it is the only defensible answer for an app that has no idea
    # where it is running. It is worth knowing what it actually contains,
    # though: measured 2026-08-21, nine of its top twenty artists were
    # Indian playback singers and the top five were all of them. That is not
    # a fault, it is what the world listens to — but if it isn't what you
    # want to browse, naming a few countries here is the fix. Nothing in the
    # API allows filtering the global chart itself: a chart entry carries no
    # market, and the names are Latin-script either way.
    #
    # One request per country, and only when Explore's batch is rebuilt (30
    # minute TTL), not per page view.
    music_chart_countries: str = "ZZ"
    # Superseded by music_chart_countries. Still read so that an existing
    # .env keeps working across the upgrade instead of silently falling back
    # to the global chart — pydantic's extra="ignore" would otherwise drop
    # it without a word.
    music_chart_country: str | None = None

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def chart_countries(self) -> list[str]:
        """The country codes to chart, in order. Always at least one."""
        raw = self.music_chart_countries
        if self.music_chart_country and self.music_chart_countries == "ZZ":
            # The old single-country setting is only honoured while the new
            # one is untouched, so setting both doesn't leave the deprecated
            # one quietly winning.
            raw = self.music_chart_country
        codes = [code.strip().upper() for code in raw.split(",") if code.strip()]
        return codes or ["ZZ"]


settings = Settings()
