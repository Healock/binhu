"""Environment-bound account naming rules."""

from config import settings


SHADOW_USERNAME_SUFFIX = "@shadow"


def is_shadow_username(username: str) -> bool:
    return str(username or "").strip().lower().endswith(SHADOW_USERNAME_SUFFIX)


def production_username_allowed(username: str) -> bool:
    return settings.APP_ENVIRONMENT != "production" or not is_shadow_username(username)

