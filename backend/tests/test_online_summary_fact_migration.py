import pytest

from migrations.online_summary_fact_outbox import inspect_schema
from services.online_summary_updates import online_summary_fact_outbox_schema_sql


class Cursor:
    def __init__(self, columns, indexes):
        self.columns = list(columns)
        self.indexes = list(indexes)
        self.query = ""

    async def execute(self, query, params=None):
        self.query = query

    async def fetchall(self):
        if "information_schema.columns" in self.query:
            return [(name,) for name in self.columns]
        return [(name,) for name in self.indexes]


@pytest.mark.asyncio
async def test_outbox_migration_verify_requires_every_column_and_index():
    from migrations.online_summary_fact_outbox import REQUIRED_COLUMNS, REQUIRED_INDEXES

    complete = await inspect_schema(Cursor(REQUIRED_COLUMNS, REQUIRED_INDEXES))
    assert complete["consistent"] is True

    incomplete = await inspect_schema(Cursor(
        REQUIRED_COLUMNS - {"fact_json"},
        REQUIRED_INDEXES - {"idx_online_summary_fact_due"},
    ))
    assert incomplete["consistent"] is False
    assert incomplete["missing_columns"] == ["fact_json"]
    assert incomplete["missing_indexes"] == ["idx_online_summary_fact_due"]


def test_outbox_migration_is_confined_to_development_and_staging_databases():
    from migrations.online_summary_fact_outbox import validate_target

    validate_target("development", "Dev_daily_report")
    validate_target("staging", "Staging_daily_report")
    with pytest.raises(ValueError):
        validate_target("production", "daily_report")
    with pytest.raises(ValueError):
        validate_target("staging", "daily_report")


def test_fact_outbox_ddl_is_additive_and_does_not_include_business_body():
    ddl = online_summary_fact_outbox_schema_sql()

    assert "CREATE TABLE IF NOT EXISTS _online_summary_fact_outbox" in ddl
    assert "UNIQUE KEY uk_online_summary_fact_revision" in ddl
    assert "fact_json JSON" in ddl
    assert "locked_until DATETIME(6)" in ddl
    assert "idx_online_summary_fact_due" in ddl
    assert "address" not in ddl.lower()
    assert "identity_number" not in ddl.lower()
