from sqlalchemy.orm import Session

from app.models import AppSettings

# AppSettings is a singleton row (see its docstring in models.py). The id
# was written as a bare literal 1 in pages.py and the scheduler, and as a
# separate APP_SETTINGS_ID constant in both main.py and the settings
# router — four places that had to agree on the same magic number.
APP_SETTINGS_ID = 1


def get_app_settings(db: Session) -> AppSettings:
    """The deployment-wide settings row, created on first access.

    Creating it here rather than only at startup means no caller has to
    handle "the row isn't there yet". The scheduler used to carry its own
    `if app_settings else 30` fallback for that case, and the settings
    router was annotated as returning AppSettings while actually being able
    to return None — two different answers to a situation that should
    simply not be representable.
    """
    app_settings = db.get(AppSettings, APP_SETTINGS_ID)
    if app_settings is None:
        app_settings = AppSettings(id=APP_SETTINGS_ID)
        db.add(app_settings)
        db.commit()
    return app_settings
