import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")
os.environ.setdefault("TASK_EVENT_ENCRYPTION_KEY", "test-only-task-event-key-not-for-runtime")

import pytest
from pydantic import ValidationError

from config import Settings
from services.task_read_model import task_read_model_keys
from services.task_view_state import task_view_state_keys


def test_production_read_model_requires_explicit_identity_and_its_backend_redis():
    settings = Settings(
        _env_file=None,
        APP_ENVIRONMENT="production",
        MYSQL_PASSWORD="synthetic-password",
        TASK_READ_MODEL_ENABLED=True,
        TASK_READ_MODEL_RUN_ID="PRD-rollout-20261114",
        TASK_EVENT_ENCRYPTION_KEY="x" * 48,
        REDIS_URL="redis://:synthetic-password@redis:6379/0",
    )

    assert settings.TASK_READ_MODEL_ENABLED is True
    assert task_read_model_keys(
        "production", settings.TASK_READ_MODEL_RUN_ID, "t_fullchain:1",
    )[0].startswith("binhu:task-rm:v4:production:PRD-rollout-20261114:")
    assert task_view_state_keys(
        "production", settings.TASK_READ_MODEL_RUN_ID,
        "t_fullchain:1", "watch",
    )[0].startswith("binhu:task-rm:v1:production:PRD-rollout-20261114:")


@pytest.mark.parametrize("redis_url", [
    "redis://:synthetic-password@environment-redis:6379/0",
    "redis://:synthetic-password@redis:6379/1",
    "redis://:synthetic-password@other-host:6379/0",
])
def test_production_read_model_rejects_nonproduction_redis_identity(redis_url):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            APP_ENVIRONMENT="production",
            MYSQL_PASSWORD="synthetic-password",
            TASK_READ_MODEL_ENABLED=True,
            TASK_READ_MODEL_RUN_ID="PRD-rollout-20261114",
            TASK_EVENT_ENCRYPTION_KEY="x" * 48,
            REDIS_URL=redis_url,
        )


@pytest.mark.parametrize("run_id", ["production-current", "PRD-", "PRD-" + "x" * 49])
def test_production_read_model_rejects_invalid_run_identity(run_id):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            APP_ENVIRONMENT="production",
            MYSQL_PASSWORD="synthetic-password",
            TASK_READ_MODEL_ENABLED=True,
            TASK_READ_MODEL_RUN_ID=run_id,
            TASK_EVENT_ENCRYPTION_KEY="x" * 48,
            REDIS_URL="redis://:synthetic-password@redis:6379/0",
        )


def test_production_read_model_stays_disabled_by_default():
    settings = Settings(
        _env_file=None,
        APP_ENVIRONMENT="production",
        MYSQL_PASSWORD="synthetic-password",
    )

    assert settings.TASK_READ_MODEL_ENABLED is False


def test_production_backfill_and_activation_accept_only_production_identity(monkeypatch):
    from migrations import task_read_model_activation as activation
    from migrations import task_read_model_backfill as backfill

    for module in (activation, backfill):
        monkeypatch.setattr(module.settings, "APP_ENVIRONMENT", "production")
        monkeypatch.setattr(module.settings, "TASK_READ_MODEL_RUN_ID", "PRD-rollout-20261114")
        monkeypatch.setattr(module.settings, "TASK_EVENT_ENCRYPTION_KEY", "x" * 48)

    assert activation._identity() == ("production", "PRD-rollout-20261114")
    assert backfill.identity() == ("production", "PRD-rollout-20261114")
