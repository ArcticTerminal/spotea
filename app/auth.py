import secrets

from app.config import settings

SESSION_KEY = "authenticated"


def check_password(password: str) -> bool:
    return secrets.compare_digest(password, settings.app_password)
