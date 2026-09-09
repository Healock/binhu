import os
os.environ.setdefault('MYSQL_PASSWORD', 'test-password')
os.environ.setdefault('ENCRYPTION_KEY', 'test-key')
import pytest
from config import settings
from services.environment_runtime import validate_target


def isolated():
    config = settings.model_copy()
    config.APP_ENVIRONMENT = 'development'
    config.MYSQL_HOST = 'environment-mysql'
    config.MYSQL_USER = 'environment_app'
    for domain in ('ONLINE_DATA', 'ARCHIVE', 'DAILY_REPORT', 'PLATFORM', 'VISIT', 'DISPATCH', 'REGISTRY', 'WORKFLOW'):
        setattr(config, f'MYSQL_{domain}_DB', 'Dev_' + domain)
    return config


def test_accepts_explicit_isolated_target():
    validate_target(isolated())


@pytest.mark.parametrize('field,value', [('MYSQL_ONLINE_DATA_DB', 'OnlineData'),
    ('MYSQL_HOST', 'mysql'), ('MYSQL_USER', 'binhu'), ('TXDOCS_ENABLED', True),
    ('VENUE_CLOUD_PULL_ENABLED', True), ('QMF_REGISTRATION_ENABLED', True)])
def test_rejects_production_and_external_configuration(field, value):
    config = isolated()
    setattr(config, field, value)
    with pytest.raises(ValueError):
        validate_target(config)
