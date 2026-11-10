import os

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-application-key")

from migrations.task_command_read_model import (
    REQUIRED_COMMAND_COLUMNS,
    REQUIRED_OUTBOX_COLUMNS,
    schema_status,
)
from config import settings


def complete_schema():
    return {
        "database": "test",
        "tables": ["_domain_event_outbox", "_task_command_records"],
        "columns": {
            "_domain_event_outbox": sorted(REQUIRED_OUTBOX_COLUMNS),
            "_task_command_records": sorted(REQUIRED_COMMAND_COLUMNS),
        },
        "indexes": ["idx_domain_event_retention"],
    }


def test_schema_status_reports_missing_v4_outbox_command_id():
    schema = complete_schema()
    schema["columns"]["_domain_event_outbox"].remove("command_id")

    status = schema_status(settings.MYSQL_ONLINE_DATA_DB, schema)

    assert status["consistent"] is False
    assert status["missing"]["outbox_columns"] == ["command_id"]
    assert status["missing"]["outbox_indexes"] == []
    assert status["missing"]["command_columns"] == []


def test_schema_status_requires_command_ledger_only_in_online_database():
    schema = complete_schema()
    schema["columns"]["_task_command_records"] = []

    online = schema_status(settings.MYSQL_ONLINE_DATA_DB, schema)
    platform = schema_status(settings.MYSQL_PLATFORM_DB, schema)

    assert "command_id" in online["missing"]["command_columns"]
    assert online["consistent"] is False
    assert platform["missing"]["command_columns"] == []
    assert platform["consistent"] is True


def test_schema_status_reports_missing_retention_index():
    schema = complete_schema()
    schema["indexes"] = []

    status = schema_status(settings.MYSQL_PLATFORM_DB, schema)

    assert status["consistent"] is False
    assert status["missing"]["outbox_indexes"] == ["idx_domain_event_retention"]
