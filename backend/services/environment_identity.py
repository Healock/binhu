"""Environment-bound account naming rules."""

from config import settings


SHADOW_USERNAME_SUFFIX = "@shadow"
ENVIRONMENT_USERNAME_SUFFIXES = {
    "production": None,
    "staging": "@staging",
    "development": "@dev",
    "shadow": "@shadow",
}


def is_shadow_username(username: str) -> bool:
    return str(username or "").strip().lower().endswith(SHADOW_USERNAME_SUFFIX)


def production_username_allowed(username: str) -> bool:
    """Ensure an account can only be used in the environment it belongs to.

    Production accounts remain unsuffixed for backwards compatibility; every
    non-production account is explicitly bound to its environment.  This is
    checked before session creation and again for authenticated requests.
    """
    normalized = str(username or "").strip().lower()
    suffix = ENVIRONMENT_USERNAME_SUFFIXES.get(settings.APP_ENVIRONMENT)
    if suffix is None:
        return not any(normalized.endswith(item) for item in ("@shadow", "@staging", "@dev"))
    return normalized.endswith(suffix)


def username_allowed_in_environment(username: str, environment: str | None = None) -> bool:
    normalized = str(environment or settings.APP_ENVIRONMENT).strip().lower()
    suffix = ENVIRONMENT_USERNAME_SUFFIXES.get(normalized)
    value = str(username or "").strip().lower()
    if suffix is None:
        return not any(value.endswith(item) for item in ("@shadow", "@staging", "@dev"))
    return value.endswith(suffix)
